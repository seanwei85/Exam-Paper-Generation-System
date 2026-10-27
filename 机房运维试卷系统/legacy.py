"""旧版 Office 格式的正文抽取：Word 97（.doc）与 Excel 97（.xls）。

只依赖标准库。这两种格式都是二进制复合文档，正文不在文件开头，必须按各自的
结构找：

  - .doc：`WordDocument` 流里的 FIB 记着片段表（CLX）在哪个表流的什么位置。
    片段表把正文切成若干段"片段"，每段指向 `WordDocument` 里的字节偏移，并标明
    这段是 1 字节/字符（压缩）还是 2 字节/字符（UTF-16LE）。中文正常情况下都是
    UTF-16LE，压缩片段只放 ASCII。
  - .xls：`Workbook` 流是一串 BIFF8 记录。字符串集中在 SST（共享字符串表）里，
    单元格记录只存索引；SST 可能被拆到多个 CONTINUE 记录中，跨段时字符编码会
    变化，必须按段重新读编码标志，不能把各段拼起来当一段解析。

另外做一件很实用的事：**按内容判断真实格式**。现实里 .doc 常常是 RTF 或 HTML
（某些系统"导出 Word"就是这么干的），.xls 常常是 HTML 表格。只看扩展名会把这些
文件判成"损坏"，所以先嗅探再决定怎么读。
"""

import re
import struct

import ole

DOC = "doc"
XLS = "xls"


class LegacyError(Exception):
    """旧格式读不出来。文案要能直接给人看。"""


# ---------------------------------------------------------------- 格式嗅探

_OLE_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff(raw):
    """按内容判断真实格式，返回 ole / zip / rtf / html / text。"""
    head = raw[:2048]
    if head[:8] == _OLE_SIG:
        return "ole"
    if head[:2] == b"PK":
        return "zip"
    if head[:5].lower().replace(b" ", b"").startswith(b"{\\rtf"):
        return "rtf"
    low = head.lower()
    if b"<html" in low or b"<!doctype html" in low or b"<table" in low:
        return "html"
    if b"mime-version" in low and b"content-type" in low:
        return "mht"
    return "text"


# ---------------------------------------------------------------- Excel 97

_EOF = 0x000A
_BOF = 0x0809
_BOUNDSHEET = 0x0085
_SST = 0x00FC
_CONTINUE = 0x003C
_LABELSST = 0x00FD
_LABEL = 0x0204
_NUMBER = 0x0203
_RK = 0x027E
_MULRK = 0x00BD
_BOOLERR = 0x0205
_FORMULA = 0x0006
_STRING = 0x0207
_MAX_SHEETS = 3


def _records(data, pos=0):
    """逐条吐记录。偏移越界就停，不抛异常——损坏的文件能读多少读多少。"""
    while pos + 4 <= len(data):
        rtype, rlen = struct.unpack_from("<HH", data, pos)
        yield rtype, data[pos + 4:pos + 4 + rlen]
        pos += 4 + rlen


def _short_str(body, off, wide_ok=True):
    """BIFF8 短字符串：cch(1) + grbit(1) + 字符。"""
    cch = body[off]
    grbit = body[off + 1]
    data = body[off + 2:]
    if grbit & 0x01:
        return data[:cch * 2].decode("utf-16-le", "ignore")
    return data[:cch].decode("latin-1", "ignore")


class _Segs(object):
    """把 SST 与它的 CONTINUE 当成一个可跨段的游标。

    跨段的规则很关键：字符数据一旦进入下一段，**段首会多一个 grbit 字节**，
    说明后面这段是压缩还是 UTF-16。所以不能把各段直接拼成一个 bytes 再解析。
    """

    def __init__(self, segs):
        self.segs = segs
        self.i = 0
        self.p = 0

    @property
    def cur(self):
        return self.segs[self.i]

    def avail(self):
        return len(self.segs[self.i]) - self.p

    def enough(self, n):
        return self.avail() >= n

    def advance_seg(self):
        if self.i + 1 >= len(self.segs):
            return False
        self.i += 1
        self.p = 0
        return True

    def take(self, n):
        """连续取 n 字节，跨段时直接续上（用于 RT/音标等附加块）。"""
        out = b""
        while n > 0:
            if self.p >= len(self.segs[self.i]):
                if not self.advance_seg():
                    break
                continue
            chunk = self.segs[self.i][self.p:self.p + n]
            self.p += len(chunk)
            out += chunk
            n -= len(chunk)
        return out

    def u16(self):
        b = self.take(2)
        return struct.unpack("<H", b)[0] if len(b) == 2 else 0

    def u32(self):
        b = self.take(4)
        return struct.unpack("<I", b)[0] if len(b) == 4 else 0


def _parse_sst(segs):
    """解析共享字符串表，返回 [str]。容错优先：读不全就返回已读到的部分。"""
    cur = _Segs(segs)
    cur.take(4)                      # cstTotal：格式规定值，实际条数以下面的为准
    unique = cur.u32()
    if unique > 1000000:             # 明显是垃圾值，别把内存吃光
        raise LegacyError("Excel 共享字符串表异常（条数 %d），文件可能已损坏" % unique)
    out = []
    for _ in range(unique):
        if not cur.enough(3):
            if not cur.advance_seg():
                break
            if not cur.enough(3):
                break
        cch = cur.u16()
        grbit = cur.cur[cur.p]
        cur.p += 1
        rt = 0
        phon = 0
        if grbit & 0x08:
            rt = cur.u16()
        if grbit & 0x04:
            phon = cur.u32()
        chars = []
        left = cch
        while left > 0:
            if cur.avail() <= 0:
                if not cur.advance_seg():
                    break
                # 新段开头重新给一次编码标志
                grbit = cur.cur[cur.p] if cur.avail() > 0 else grbit
                cur.p += 1
                continue
            if grbit & 0x01:
                n = min(cur.avail() // 2, left)
                if n <= 0:
                    cur.advance_seg()
                    continue
                chars.append(cur.take(n * 2).decode("utf-16-le", "ignore"))
            else:
                n = min(cur.avail(), left)
                chars.append(cur.take(n).decode("latin-1", "ignore"))
            left -= n
        if rt:
            cur.take(rt * 4)
        if phon:
            cur.take(phon)
        out.append("".join(chars))
    return out


def _rk(rk):
    if rk & 0x02:
        v = rk >> 2
        if v & 0x20000000:
            v -= 0x40000000
        num = float(v)
    else:
        num = struct.unpack("<d", b"\0\0\0\0" + struct.pack("<I", rk & 0xFFFFFFFC))[0]
    return num / 100.0 if rk & 0x01 else num


def _num(v):
    """数字转文本：整数不带小数点，避免「2 分」变成「2.0 分」。"""
    if v != v:                       # NaN
        return ""
    if abs(v - round(v)) < 1e-9 and abs(v) < 1e15:
        return str(int(round(v)))
    return ("%.10f" % v).rstrip("0").rstrip(".")


def _xls_biff(data):
    """BIFF8 工作簿流 → [ { (行,列): 文本 }, ... ]，每个工作表一项。"""
    # 全局区里 BOUNDSHEET 与 SST 的先后并不固定（Excel 把 BOUNDSHEET 放前面，
    # 有些写入器反过来），所以一次扫完、各取所需，不能见到 SST 就停。
    sst_at = None
    sheets = []
    version = 0x0600
    pos = 0
    while pos + 4 <= len(data):
        rtype, rlen = struct.unpack_from("<HH", data, pos)
        body = data[pos + 4:pos + 4 + rlen]
        if rtype == _BOF and len(body) >= 2:
            version = struct.unpack_from("<H", body, 0)[0]
        elif rtype == _BOUNDSHEET and len(body) >= 6:
            sheets.append((struct.unpack_from("<I", body, 0)[0], _short_str(body, 6)))
        elif rtype == _SST and sst_at is None:
            sst_at = pos
        elif rtype == _EOF:
            break
        pos += 4 + rlen

    if version < 0x0600:
        raise LegacyError("这是 Excel 5.0/95 的老格式（BIFF%d），系统读不了。"
                          "请用 WPS 或 Excel 打开后另存为 .xlsx 再导入" % (version >> 8))
    if not sheets:
        raise LegacyError("这个 Excel 文件里没有工作表")

    # SST 后面可能跟多个 CONTINUE，必须一并交给解析器
    sst = []
    if sst_at is not None:
        rtype, rlen = struct.unpack_from("<HH", data, sst_at)
        segs = [data[sst_at + 4:sst_at + 4 + rlen]]
        p2 = sst_at + 4 + rlen
        while p2 + 4 <= len(data):
            t2, l2 = struct.unpack_from("<HH", data, p2)
            if t2 != _CONTINUE:
                break
            segs.append(data[p2 + 4:p2 + 4 + l2])
            p2 += 4 + l2
        sst = _parse_sst(segs)

    rows = []
    for pos, name in sheets[:_MAX_SHEETS]:
        cells = {}
        pending = None              # FORMULA 的字符串结果在下一条 STRING 记录里
        for rtype, body in _records(data, pos):
            if rtype == _EOF:
                break
            if rtype == _LABELSST and len(body) >= 10:
                r, c, _xf, idx = struct.unpack_from("<HHHI", body, 0)
                cells[(r, c)] = sst[idx] if idx < len(sst) else ""
            elif rtype == _LABEL and len(body) >= 9:
                r, c = struct.unpack_from("<HH", body, 0)
                cells[(r, c)] = _short_str(body, 6)
            elif rtype == _NUMBER and len(body) >= 14:
                r, c = struct.unpack_from("<HH", body, 0)
                v = struct.unpack_from("<d", body, 6)[0]
                cells[(r, c)] = _num(v)
            elif rtype == _RK and len(body) >= 10:
                r, c = struct.unpack_from("<HH", body, 0)
                rk = struct.unpack_from("<I", body, 6)[0]
                cells[(r, c)] = _num(_rk(rk))
            elif rtype == _MULRK and len(body) >= 12:
                r, c0 = struct.unpack_from("<HH", body, 0)
                c1 = struct.unpack_from("<H", body, len(body) - 2)[0]
                n = c1 - c0 + 1
                for i in range(n):
                    off = 4 + i * 6
                    if off + 6 > len(body):
                        break
                    rk = struct.unpack_from("<I", body, off + 2)[0]
                    cells[(r, c0 + i)] = _num(_rk(rk))
            elif rtype == _BOOLERR and len(body) >= 8:
                r, c, _xf, val, is_err = struct.unpack_from("<HHHBB", body, 0)
                cells[(r, c)] = "" if is_err else ("TRUE" if val else "FALSE")
            elif rtype == _FORMULA and len(body) >= 20:
                r, c = struct.unpack_from("<HH", body, 0)
                res = body[6:14]
                if res[6:8] == b"\xff\xff":
                    code = res[0]
                    if code == 0:
                        pending = (r, c)   # 结果在 STRING 记录里
                    elif code == 3:
                        cells[(r, c)] = ""  # 空
                    elif code == 1:
                        cells[(r, c)] = "TRUE" if res[2] else "FALSE"
                    else:
                        cells[(r, c)] = ""
                else:
                    cells[(r, c)] = _num(struct.unpack("<d", res)[0])
            elif rtype == _STRING and pending:
                cells[pending] = _short_str(body, 0)
                pending = None
        if cells:
            rows.append(cells)
    return rows


def _grid(cells):
    """{ (行,列): 文本 } → [[单元格, ...], ...]，保留列位置。

    按表头取列时必须知道每个值在第几列，所以空单元格要占位，不能像纯文本路径
    那样把空值丢掉——丢掉以后「题干/A/B/C/D/答案」就会整体左移。
    """
    if not cells:
        return []
    maxr = max(r for r, c in cells)
    maxc = max(c for r, c in cells)
    grid = [["" for _ in range(maxc + 1)] for _ in range(maxr + 1)]
    for (r, c), v in cells.items():
        grid[r][c] = (v or "").strip()
    while grid and not any(grid[-1]):
        grid.pop()
    return grid


def xls_sheets(raw):
    """返回 [[[单元格, ...], ...], ...]：每个工作表一张二维表。"""
    fmt = sniff(raw)
    if fmt == "ole":
        cf = ole.CompoundFile(raw)
        for name in ("Workbook", "Book"):
            data = cf.stream(name)
            if data:
                return [_grid(cells) for cells in _xls_biff(data)]
        raise LegacyError("这个文件里没有 Excel 工作簿数据，可能不是表格文件")
    if fmt == "html":
        return [_grid(_html_table(raw))]
    if fmt == "zip":
        raise LegacyError("这个 .xls 其实是 .xlsx（新格式），系统会按 xlsx 读，"
                          "但扩展名写成了 .xls。请把它改名为 .xlsx 后重试")
    if fmt == "rtf":
        raise LegacyError("这个 .xls 其实是 RTF 文档，不是表格，读不出题目")
    raise LegacyError("这个文件不是 Excel 表格（可能是文本改的扩展名），"
                      "读不出题目。请确认文件本身是 .xls 或用 Excel 另存为 .xlsx")


def xls_rows(raw):
    """所有工作表按顺序拼成一个二维表，与 xlsx 路径的形状保持一致。"""
    rows = []
    for grid in xls_sheets(raw):
        rows.extend(grid)
    return rows


def xls_text(raw):
    blocks = []
    for grid in xls_sheets(raw):
        blocks.extend(_cells_to_blocks(grid))
    return "\n\n".join(blocks)


def _cells_to_blocks(grid):
    """二维表 → 每行一段、单元格用换行连接，交给通用拆题逻辑。"""
    blocks = []
    for row in grid:
        vals = [c for c in row if c]
        if vals:
            blocks.append("\n".join(vals))
    return blocks


# ---------------------------------------------------------------- Word 97

def _doc_pieces(raw):
    """按片段表取正文。返回 (text, 是否用了片段表)。"""
    cf = ole.CompoundFile(raw)
    wd = cf.stream("WordDocument")
    if not wd or len(wd) < 0x20:
        raise LegacyError("这个文件里没有 Word 正文数据，可能不是 Word 文档")
    if struct.unpack_from("<H", wd, 0)[0] != 0xA5EC:
        raise LegacyError("Word 文档头标识不对（%s），文件可能已损坏"
                          % wd[:2].hex())
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    tbl_name = "1Table" if flags & 0x0200 else "0Table"
    table = cf.stream(tbl_name)
    if not table:
        table = cf.stream("0Table" if tbl_name == "1Table" else "1Table")
    if not table or len(wd) < 0x1AA:
        return "", False

    fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
    if not lcb_clx or fc_clx + lcb_clx > len(table):
        return "", False
    clx = table[fc_clx:fc_clx + lcb_clx]

    pos = 0
    plc = None
    while pos < len(clx):
        kind = clx[pos]
        if kind == 0x01:                     # Prc：格式修订，跳过
            if pos + 3 > len(clx):
                break
            cb = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + cb
        elif kind == 0x02:                   # Pcdt：片段表本体
            if pos + 5 > len(clx):
                break
            lcb = struct.unpack_from("<I", clx, pos + 1)[0]
            plc = clx[pos + 5:pos + 5 + lcb]
            break
        else:
            break
    if not plc or len(plc) < 16:
        return "", False

    n = (len(plc) - 4) // 12
    if n <= 0:
        return "", False
    cps = struct.unpack_from("<%dI" % (n + 1), plc, 0)
    base = (n + 1) * 4
    out = []
    for i in range(n):
        cch = cps[i + 1] - cps[i]
        if cch <= 0:
            continue
        off = base + i * 8
        if off + 8 > len(plc):
            break
        fc = struct.unpack_from("<I", plc, off + 2)[0]
        if fc & 0x40000000:                  # 压缩片段：1 字节/字符
            start = (fc & 0x3FFFFFFF) >> 1
            chunk = wd[start:start + cch]
            # 规范上压缩片段只有 ASCII，但现实中偶有按 GBK 写的，一并兜住
            out.append(chunk.decode("gb18030", "ignore"))
        else:
            start = fc & 0x3FFFFFFF
            chunk = wd[start:start + cch * 2]
            out.append(chunk.decode("utf-16-le", "ignore"))
    return "".join(out), True


_CTRL = {
    "\r": "\n", "\x07": "\t", "\x0b": "\n", "\x0c": "\n",
    "\x1e": "-", "\x1f": "", "\x01": "", "\x02": "",
    "\x05": "", "\x08": "",
}


def _doc_clean(text):
    """去掉 Word 正文里的域指令、图片占位符与其它不可读控制符。

    域的写法是 `\\x13 指令 \\x14 结果 \\x15`。目录、页码、超链接都是域，指令部分
    其实是给人看的源码（`TOC \\o "1-3" \\h \\z \\u HYPERLINK \\l "_Toc..."`），
    留着会把整篇目录的十几行"代码"灌进正文，前几段全是垃圾。

    注意 \\x14 之后是**结果**，要留下——只有 \\x13 到 \\x14 之间才是指令。
    域可以嵌套（目录项里套超链接），所以用栈：True 表示当前处于指令段。
    """
    out = []
    stack = []
    for ch in text:
        if ch == "\x13":
            stack.append(True)
            continue
        if ch == "\x14":
            if stack:
                stack[-1] = False
            continue
        if ch == "\x15":
            if stack:
                stack.pop()
            continue
        if any(stack):
            continue                 # 指令段：丢弃
        if ch in _CTRL:
            out.append(_CTRL[ch])
        elif ch == "\x00":
            continue
        elif ord(ch) < 0x20 and ch not in "\n\t":
            continue
        else:
            out.append(ch)
    s = "".join(out)
    s = re.sub(r"[ \u00a0]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _doc_scan(wd):
    """片段表读不出来时的兜底：扫 UTF-16LE 的可读片段。

    中文在 .doc 里几乎都是 UTF-16LE，前面两个字节常为 0，因此"偶数字节 + 高字节
    为 0 或汉字"的连续片段就是正文。这会漏字，但不至于一个都读不出——配合后面的
    人工核对表，比直接报"不支持"有用。
    """
    runs = []
    i = 0
    n = len(wd)
    buf = []
    while i + 1 < n:
        lo, hi = wd[i], wd[i + 1]
        ch = None
        if hi == 0 and (0x20 <= lo <= 0x7E or lo in (0x09, 0x0D)):
            ch = chr(lo)
        elif 0x4E <= hi <= 0x9F and lo >= 0x20:
            ch = bytes((lo, hi)).decode("utf-16-le", "ignore")
        if ch:
            buf.append(ch)
            i += 2
        else:
            if len(buf) >= 6:
                runs.append("".join(buf))
            buf = []
            i += 2
    if len(buf) >= 6:
        runs.append("".join(buf))
    return "\n".join(runs)


def doc_text(raw):
    fmt = sniff(raw)
    if fmt == "zip":
        raise LegacyError("这个 .doc 其实是 .docx（新格式），请把扩展名改成 .docx 后重试")
    if fmt == "rtf":
        return rtf_text(raw)
    if fmt == "html":
        return html_text(raw)
    if fmt != "ole":
        raise LegacyError("这个文件不是 Word 文档（可能是文本改的扩展名），"
                          "读不出可拆的题目")
    try:
        text, from_pieces = _doc_pieces(raw)
    except ole.OleError as exc:
        raise LegacyError("Word 文档结构损坏：%s" % exc)
    text = _doc_clean(text)
    if text.strip() and from_pieces:
        return text
    wd = ole.CompoundFile(raw).stream("WordDocument") or b""
    fallback = _doc_clean(_doc_scan(wd))
    if len(fallback) > len(text.strip()):
        return fallback
    if text.strip():
        return text
    raise LegacyError("这个 Word 文档里没有可读的正文（可能全是图片），"
                      "请把内容拍成照片用 AI 视觉识别，或另存为 .docx 后重试")


# ---------------------------------------------------------------- RTF / HTML

#: RTF 里"只用于排版、不是正文"的目标组：字体表、颜色表、样式表、文档信息、
#: 以及 `\*` 开头的自定义组。不跳过的话字体名（SimSun;）会混进题目里。
_RTF_SKIP = ("fonttbl", "colortbl", "stylesheet", "info", "pict", "object",
             "header", "footer", "footnote", "xmlnstbl", "listtable",
             "listoverridetable", "rsidtbl", "generator", "themedata",
             "colorschememapping", "datastore", "latentstyles")


def _rtf_skip_group(s, i):
    """从 `{` 位置跳过一个完整的组，返回 `}` 之后的下标。"""
    depth = 0
    n = len(s)
    while i < n:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        elif s[i] == "\\":
            i += 1                 # 转义字符/控制字里可能有花括号，别当结构
        i += 1
    return n


def _rtf_text(raw):
    s = raw.decode("latin-1", "ignore")
    out = []
    i = 0
    n = len(s)
    codepage = "gb18030"
    m = re.search(r"\\ansicpg(\d+)", s[:2000])
    if m and m.group(1) not in ("1252", "936", "0"):
        codepage = "cp%s" % m.group(1)
    while i < n:
        ch = s[i]
        if ch == "{":
            m = re.match(r"\{\s*\\(?:\*)?\s*([a-zA-Z]+)", s[i:])
            if m and (m.group(1) in _RTF_SKIP or "\\*" in m.group(0)):
                i = _rtf_skip_group(s, i)
                continue
            i += 1
            continue
        if ch == "}":
            i += 1
            continue
        if ch == "\\":
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)?[ ]?", s[i:])
            if m:
                word, arg = m.group(1), m.group(2)
                if word in ("par", "line", "sect", "row"):
                    out.append("\n")
                elif word in ("tab", "cell"):
                    out.append("\t")
                elif word == "u" and arg:
                    try:
                        code = int(arg)
                        if code < 0:
                            code += 65536
                        out.append(chr(code))
                    except ValueError:
                        pass
                i += m.end()
                continue
            m = re.match(r"\\'([0-9a-fA-F]{2})", s[i:])
            if m:
                out.append(bytes([int(m.group(1), 16)]).decode("latin-1"))
                i += m.end()
                continue
            i += 2
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    # \'hh 收集到的是原始字节，按声明的代码页还原
    try:
        text = text.encode("latin-1", "ignore").decode(codepage, "ignore")
    except Exception:
        pass
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_tags(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        s = s.replace(a, b)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", s)).strip()


def _decode_any(raw):
    for enc in ("utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def html_text(raw):
    return _strip_tags(_decode_any(raw))


def _html_table(raw):
    """把 HTML 表格读成 { (行,列): 文本 }。导出报表型 .xls 基本都是这个。"""
    s = _decode_any(raw)
    cells = {}
    r = 0
    for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", s):
        c = 0
        for m in re.finditer(r"(?is)<t([dh])([^>]*)>(.*?)</t\1>", tr):
            attrs, body = m.group(2), m.group(3)
            span = re.search(r'colspan\s*=\s*"?(\d+)', attrs, re.I)
            text = _strip_tags(body).replace("\t", " ")
            cells[(r, c)] = text
            c += int(span.group(1)) if span else 1
        r += 1
    return cells


def rtf_text(raw):
    text = _rtf_text(raw)
    if not text.strip():
        raise LegacyError("RTF 文档里没有可读文字")
    return text
