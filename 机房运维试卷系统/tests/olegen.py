"""构造旧版 Office 二进制样本，供 test_p16_legacy 使用。

必须自己造，不能靠外部文件：真实试卷是用户的数据，不能进仓库；而且真实文件里
恰好"缺答案列""跨段字符串""压缩片段"这些边界不会同时出现，靠真实文件测不全。

只造到"够用"的复杂度，不做通用写入器：
  * `build_cfb`     —— 复合文档，支持迷你流（<4096 字节的流走 MiniFAT）
  * `build_xls`     —— BIFF8 工作簿：SST（可强制跨 CONTINUE）+ 各类单元格记录
  * `build_doc`     —— Word 97：FIB + 片段表（压缩/UTF-16 两种片段）
"""

import struct

MINI_CUTOFF = 4096
SECTOR = 512
MINI = 64
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD


def build_cfb(streams):
    """streams: [(名字, bytes), ...] → 复合文档字节。"""
    small = [(n, d) for n, d in streams if len(d) < MINI_CUTOFF]
    big = [(n, d) for n, d in streams if len(d) >= MINI_CUTOFF]

    mini_body = b""
    minifat = []
    mini_at = {}
    for name, data in small:
        start = len(mini_body) // MINI
        chunks = max(1, (len(data) + MINI - 1) // MINI)
        for i in range(chunks):
            minifat.append(start + i + 1 if i < chunks - 1 else ENDOFCHAIN)
        mini_body += data + b"\0" * (chunks * MINI - len(data))
        mini_at[name] = (start, len(data))
    if mini_body:
        pad = (SECTOR - len(mini_body) % SECTOR) % SECTOR
        mini_body += b"\0" * pad

    entries = 1 + len(streams)
    dir_sectors = (entries * 128 + SECTOR - 1) // SECTOR
    minifat_bytes = struct.pack("<%dI" % len(minifat), *minifat) if minifat else b""
    minifat_sectors = (len(minifat_bytes) + SECTOR - 1) // SECTOR
    mini_sectors = (len(mini_body) + SECTOR - 1) // SECTOR

    # 扇区编排：0=FAT，接着是目录，再是 MiniFAT、迷你流本体、普通流
    sec_dir = 1
    sec_minifat = sec_dir + dir_sectors
    sec_mini = sec_minifat + minifat_sectors
    sec_big = sec_mini + mini_sectors

    total = sec_big
    big_at = {}
    for name, data in big:
        chunks = (len(data) + SECTOR - 1) // SECTOR
        big_at[name] = (total, len(data), chunks)
        total += chunks
    if total > 128:
        raise AssertionError("样本扇区数超过 128，FAT 一扇区放不下")

    fat = [FREESECT] * 128
    fat[0] = FATSECT

    def chain(start, n):
        for i in range(n):
            fat[start + i] = start + i + 1 if i < n - 1 else ENDOFCHAIN

    chain(sec_dir, dir_sectors)
    if minifat_sectors:
        chain(sec_minifat, minifat_sectors)
    if mini_sectors:
        chain(sec_mini, mini_sectors)
    for name, data in big:
        start, _size, chunks = big_at[name]
        chain(start, chunks)

    # 目录项
    dir_bytes = b""
    root_start = sec_mini if mini_sectors else ENDOFCHAIN
    dir_bytes += _dir_entry("Root Entry", 5, root_start, len(mini_body))
    for name, data in streams:
        if name in mini_at:
            start, size = mini_at[name]
        else:
            start, size, _c = big_at[name]
        dir_bytes += _dir_entry(name, 2, start, size)
    dir_bytes += b"\0" * (dir_sectors * SECTOR - len(dir_bytes))

    sectors = [b"\0" * SECTOR]
    sectors += [dir_bytes[i:i + SECTOR] for i in range(0, len(dir_bytes), SECTOR)]
    if minifat_sectors:
        pad = minifat_bytes + b"\xff" * (minifat_sectors * SECTOR - len(minifat_bytes))
        sectors += [pad[i:i + SECTOR] for i in range(0, len(pad), SECTOR)]
    if mini_sectors:
        sectors += [mini_body[i:i + SECTOR] for i in range(0, len(mini_body), SECTOR)]
    for name, data in big:
        body = data + b"\0" * ((len(data) + SECTOR - 1) // SECTOR * SECTOR - len(data))
        sectors += [body[i:i + SECTOR] for i in range(0, len(body), SECTOR)]

    sectors[0] = struct.pack("<128I", *fat)
    sectors = sectors[:total]

    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 0x0003)
    struct.pack_into("<H", header, 0x1C, 0xFFFE)
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x28, 0)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, sec_dir)
    struct.pack_into("<I", header, 0x38, MINI_CUTOFF)
    struct.pack_into("<I", header, 0x3C, sec_minifat if minifat_sectors else ENDOFCHAIN)
    struct.pack_into("<I", header, 0x40, minifat_sectors)
    struct.pack_into("<I", header, 0x44, ENDOFCHAIN)
    struct.pack_into("<I", header, 0x48, 0)
    struct.pack_into("<I", header, 0x4C, 0)
    return bytes(header) + b"".join(sectors)


def _dir_entry(name, kind, start, size):
    b = bytearray(128)
    raw = name.encode("utf-16-le")
    b[0:len(raw)] = raw
    struct.pack_into("<H", b, 0x40, len(raw) + 2)
    b[0x42] = kind
    b[0x43] = 1
    struct.pack_into("<III", b, 0x44, FREESECT, FREESECT, FREESECT)
    struct.pack_into("<I", b, 0x74, start)
    struct.pack_into("<Q", b, 0x78, size)
    return bytes(b)


# ---------------------------------------------------------------- BIFF8

def rec(rtype, data=b""):
    return struct.pack("<HH", rtype, len(data)) + data


def _bof(dt):
    return rec(0x0809, struct.pack("<HHHHII", 0x0600, dt, 0x0DBB, 0x07CC, 0xC1, 0x06))


def _u16(v):
    return struct.pack("<H", v)


def _xl_string(s):
    """BIFF8 XLUnicodeString：cch(2) + grbit(1) + 数据。"""
    try:
        raw = s.encode("latin-1")
        return _u16(len(s)) + b"\x00" + raw
    except UnicodeEncodeError:
        raw = s.encode("utf-16-le")
        return _u16(len(s)) + b"\x01" + raw


def build_xls(sheets, split=None):
    """sheets: [[[单元格,...], ...], ...]；数字用 int/float，其余按字符串。

    split=(字符串序号, 字符位置)：把这个字符串的字符数据从这里断开，后半段放进
    CONTINUE 记录，**并在段首补一个编码标志字节**——这是 SST 跨段的规矩，
    拼成一段再解析的实现会在这里露出马脚。
    """
    used = []
    index = {}

    def sst_id(s):
        if s not in index:
            index[s] = len(used)
            used.append(s)
        return index[s]

    sheet_blocks = []
    for rows in sheets:
        body = b""
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                if cell is None or cell == "":
                    continue
                if isinstance(cell, (int, float)):
                    body += rec(0x0203, struct.pack("<HHHd", r, c, 0, float(cell)))
                else:
                    body += rec(0x00FD, struct.pack("<HHHI", r, c, 0, sst_id(str(cell))))
        sheet_blocks.append(_bof(0x0010) + body + rec(0x000A))

    # SST 分段：默认一整条，给了 split 就断开成 SST + CONTINUE
    payload = struct.pack("<II", len(used), len(used))
    boundary = None
    for i, s in enumerate(used):
        if split and split[0] == i:
            at = max(1, min(split[1], len(s) - 1))
            head, tail = s[:at], s[at:]
            wide = not _ascii_ok(s)
            payload += _u16(len(s)) + (b"\x01" if wide else b"\x00")
            payload += head.encode("utf-16-le" if wide else "latin-1")
            boundary = len(payload)
            payload += b"\x01" if wide else b"\x00"
            payload += tail.encode("utf-16-le" if wide else "latin-1")
        else:
            payload += _xl_string(s)
    pieces = [payload] if boundary is None else [payload[:boundary], payload[boundary:]]

    sst_bytes = rec(0x00FC, pieces[0])
    for p in pieces[1:]:
        sst_bytes += rec(0x003C, p)

    # BOUNDSHEET 里要写每张表的绝对偏移，长度固定，所以先按占位长度算总体大小
    def bounds(offsets):
        out = b""
        for i, off in enumerate(offsets):
            out += rec(0x0085, struct.pack("<IBB", off, 0, 0) + _xl_string("Sheet%d" % (i + 1)))
        return out

    head_len = len(_bof(0x0005)) + len(sst_bytes)
    glob_len = head_len + len(bounds([0] * len(sheets))) + len(rec(0x000A))
    offsets = []
    pos = glob_len
    for block in sheet_blocks:
        offsets.append(pos)
        pos += len(block)

    body = _bof(0x0005) + bounds(offsets) + sst_bytes + rec(0x000A) + b"".join(sheet_blocks)
    return build_cfb([("Workbook", body)])


def _ascii_ok(s):
    try:
        s.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


# ---------------------------------------------------------------- Word 97

def build_doc(pieces, table_stream="1Table"):
    """pieces: [("text", compressed), ...]，按顺序拼成正文。

    compressed=True 的片段按 1 字节/字符写（ASCII），否则按 UTF-16LE 写。
    """
    base = 0x800
    body = b""
    cps = [0]
    pcds = b""
    for text, compressed in pieces:
        if compressed:
            raw = text.encode("gb18030")
            fc = ((base + len(body)) << 1) | 0x40000000
            cch = len(text)
        else:
            raw = text.encode("utf-16-le")
            fc = base + len(body)
            cch = len(text)
        body += raw
        cps.append(cps[-1] + cch)
        pcds += b"\0\0" + struct.pack("<I", fc) + b"\0\0"

    plc = struct.pack("<%dI" % len(cps), *cps) + pcds
    clx = b"\x02" + struct.pack("<I", len(plc)) + plc

    wd = bytearray(base)
    struct.pack_into("<H", wd, 0, 0xA5EC)
    struct.pack_into("<H", wd, 2, 0x00C1)          # nFib：Word 97
    flags = 0x0200 if table_stream == "1Table" else 0x0000
    struct.pack_into("<H", wd, 0x0A, flags)
    struct.pack_into("<I", wd, 0x18, base)
    struct.pack_into("<I", wd, 0x1C, base + len(body))
    # fcClx/lcbClx 在 FibRgFcLcb97 里，位置对 Word 97~2003 是固定的
    struct.pack_into("<I", wd, 0x01A2, 0)
    struct.pack_into("<I", wd, 0x01A6, len(clx))
    wd = bytes(wd) + body

    return build_cfb([("WordDocument", wd), (table_stream, clx)])
