"""复合文档容器（OLE2 / CFB）读取，只依赖标准库。

.doc 与 .xls 的旧格式都是把若干条"流"装进一个复合文档里：Word 的正文在
`WordDocument` 流，Excel 的工作簿在 `Workbook` 流。想读正文就得先能按名字把流
取出来，所以先做这一层，上面再谈 BIFF 与 FIB。

结构要点（MS-CFB）：
  - 头部 512 字节，签名 D0CF11E0A1B11AE1；扇区大小由 sector shift 决定
    （9 → 512，12 → 4096），扇区 N 的数据从 (N+1) × 扇区大小开始。
  - 扇区之间的连接关系存在 FAT 链里。FAT 自身也占扇区，位置记在头部；
    FAT 表项超过 109 个时，多出来的位置由 DIFAT 扇区接着记。
  - 目录是一串 128 字节的目录项，其中有类型（根 / 存储 / 流）与起始扇区。
  - 小于 4096 字节的流不占普通扇区，而是塞进根目录项指向的"迷你流"里，
    按 64 字节一段、由 MiniFAT 串起来。

这个实现只读不写，遇不到的写法（比如扇区大小非 512/4096）直接报错，
不猜——猜错了会把二进制垃圾当正文，比报错更糟。
"""

import struct

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# FAT 表项的特殊值
_FREESECT = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE
_MAXREGSECT = 0xFFFFFFFA
_DIFSECT = 0xFFFFFFFC
_FATSECT = 0xFFFFFFFD
_NOSTREAM = 0xFFFFFFFF


class OleError(Exception):
    """复合文档读不出来。文案要能直接给人看。"""


def is_cfb(raw):
    return raw[:8] == SIGNATURE


class _Entry(object):
    __slots__ = ("name", "kind", "start", "size", "left", "right", "child")

    def __init__(self, name, kind, start, size, left, right, child):
        self.name = name
        self.kind = kind          # 1=存储 2=流 5=根
        self.start = start
        self.size = size
        self.left = left
        self.right = right
        self.child = child

    def __repr__(self):
        return "<Entry %s kind=%d size=%d>" % (self.name, self.kind, self.size)


class CompoundFile(object):
    """按名字取流。用法：`CompoundFile(raw).stream("Workbook")`。"""

    def __init__(self, raw):
        if not is_cfb(raw):
            raise OleError("不是复合文档（OLE2）格式，可能不是 .doc/.xls 本身")
        self.raw = raw
        self.sector_size, self.mini_size, self.mini_cutoff = self._read_header()
        self.fat = self._read_fat()
        self.entries = self._read_dir()
        self._mini = None          # 用到才建，避免为不含迷你流的文件白读一遍

    # ------------------------------------------------------------ 头部

    def _read_header(self):
        h = self.raw[:512]
        if len(h) < 512:
            raise OleError("文件太小，不像是完整的复合文档")
        shift = struct.unpack_from("<H", h, 0x1E)[0]
        if shift not in (9, 12):
            raise OleError("复合文档扇区大小异常（shift=%d），无法解析" % shift)
        sector = 1 << shift
        mini_shift = struct.unpack_from("<H", h, 0x20)[0]
        if mini_shift not in (6,):
            # 规范只允许 64 字节的迷你扇区；别的值说明文件被改坏了
            raise OleError("复合文档迷你扇区大小异常（shift=%d）" % mini_shift)
        self._fat_sectors = struct.unpack_from("<I", h, 0x2C)[0]
        self._dir_start = struct.unpack_from("<I", h, 0x30)[0]
        cutoff = struct.unpack_from("<I", h, 0x38)[0]
        self._minifat_start = struct.unpack_from("<I", h, 0x3C)[0]
        self._minifat_count = struct.unpack_from("<I", h, 0x40)[0]
        self._difat_start = struct.unpack_from("<I", h, 0x44)[0]
        self._difat_count = struct.unpack_from("<I", h, 0x48)[0]
        self._difat_first = list(struct.unpack_from("<109I", h, 0x4C))
        return sector, 1 << mini_shift, (cutoff or 4096)

    def _sector(self, idx):
        """取第 idx 个扇区的原始字节。

        复合文档的扇区总是整块的，短了就是文件被截断——不能把半块当一块返回，
        否则后面按固定长度解包会拿到错位的数字，错误会比"文件损坏"更难懂。
        """
        off = (idx + 1) * self.sector_size
        if off + self.sector_size > len(self.raw):
            raise OleError("复合文档扇区越界，文件可能被截断")
        return self.raw[off:off + self.sector_size]

    # ------------------------------------------------------------ FAT

    def _read_fat(self):
        ids = [i for i in self._difat_first if i <= _MAXREGSECT]
        # DIFAT 链：每个 DIFAT 扇区末尾一项指向下一个 DIFAT 扇区
        per = self.sector_size // 4 - 1
        cur = self._difat_start
        guard = 0
        while cur <= _MAXREGSECT and guard < 4096:
            buf = self._sector(cur)
            vals = struct.unpack_from("<%dI" % (per + 1), buf, 0)
            ids.extend(v for v in vals[:per] if v <= _MAXREGSECT)
            cur = vals[per]
            guard += 1
        if len(ids) < self._fat_sectors:
            raise OleError("复合文档 FAT 索引不全，文件可能已损坏")

        fat = []
        for sid in ids[:self._fat_sectors]:
            buf = self._sector(sid)
            fat.extend(struct.unpack_from("<%dI" % (self.sector_size // 4), buf, 0))
        return fat

    def _chain(self, start, table, limit=None):
        """顺着链把扇区号收集起来。带上限，防止损坏文件里的环把内存吃光。"""
        out = []
        cur = start
        seen = 0
        cap = limit if limit is not None else len(table) + 8
        while cur <= _MAXREGSECT and seen <= cap:
            out.append(cur)
            if cur >= len(table):
                break
            cur = table[cur]
            seen += 1
        return out

    def _read_chain(self, start, size=None):
        parts = [self._sector(s) for s in self._chain(start, self.fat)]
        data = b"".join(parts)
        return data[:size] if size is not None else data

    # ------------------------------------------------------------ 目录

    def _read_dir(self):
        data = self._read_chain(self._dir_start)
        entries = []
        for off in range(0, len(data) - 127, 128):
            e = self._parse_entry(data[off:off + 128])
            if e is not None:
                entries.append(e)
        if not entries:
            raise OleError("复合文档目录为空，文件可能已损坏")
        return entries

    @staticmethod
    def _parse_entry(b):
        nlen = struct.unpack_from("<H", b, 0x40)[0]
        if nlen < 2 or nlen > 64:
            return None
        name = b[:nlen - 2].decode("utf-16-le", "ignore")
        kind = b[0x42]
        if kind not in (1, 2, 5):
            return None
        left, right, child = struct.unpack_from("<III", b, 0x44)
        start = struct.unpack_from("<I", b, 0x74)[0]
        size = struct.unpack_from("<Q", b, 0x78)[0]
        # 有些写入器把 64 位大小的高 32 位写成垃圾，小文件按低 32 位取更稳
        if size > 0xFFFFFFFF:
            size = size & 0xFFFFFFFF
        return _Entry(name, kind, start, size, left, right, child)

    @property
    def root(self):
        for e in self.entries:
            if e.kind == 5:
                return e
        raise OleError("复合文档缺少根目录项，文件可能已损坏")

    def names(self):
        return [e.name for e in self.entries if e.kind == 2]

    # ------------------------------------------------------------ 迷你流

    def _build_mini(self):
        if self._mini is not None:
            return self._mini
        root = self.root
        # 迷你流本体是根目录项指向的一段普通扇区；根项大小为 0 时就是空的
        big = self._read_chain(root.start) if root.size else b""
        table = []
        if self._minifat_count and self._minifat_start <= _MAXREGSECT:
            buf = self._read_chain(self._minifat_start)[:self._minifat_count * self.sector_size]
            table = list(struct.unpack_from("<%dI" % (len(buf) // 4), buf, 0))
        self._mini = (big, table)
        return self._mini

    def _read_mini(self, start, size):
        big, table = self._build_mini()
        if not table:
            raise OleError("流位于迷你流中，但文件没有 MiniFAT")
        chunks = []
        for sid in self._chain(start, table, limit=len(table) + 8):
            off = sid * self.mini_size
            chunks.append(big[off:off + self.mini_size])
        return b"".join(chunks)[:size]

    # ------------------------------------------------------------ 对外

    def stream(self, name):
        """按名字取流内容；没有这个流时返回 None（调用方决定怎么报错）。"""
        for e in self.entries:
            if e.kind == 2 and e.name.lower() == name.lower():
                if e.size == 0:
                    return b""
                if e.size < self.mini_cutoff:
                    return self._read_mini(e.start, e.size)
                return self._read_chain(e.start, e.size)
        return None


def open_cfb(raw):
    return CompoundFile(raw)
