#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF 文字层抽取（纯标准库，不联网）。

只做一件事：把 PDF 里"本来就是文字"的字符还原成按页分行的文本。

不支持、也不打算支持的：
  * 图片（扫描件/拍照）——抽不出文字时会如实返回 scanned=True，交给视觉模型或人工；
  * 加密 PDF —— 直接抛 PdfError，不猜密码；
  * 排版重建 —— 不做多栏、表格、图文混排的版面分析，只按行的纵坐标断行。

关键实现点：
  1) 页序必须走 /Pages 树的 /Kids，不能按对象号排——对象号顺序与页序无关；
  2) 换行看文本矩阵的纵坐标（Chrome/WPS 每行是独立的 BT…Tm…Tj），
     不能靠空格或字符数猜；
  3) 字体分两类：/Type0 是双字节码（Identity-H 常见于中文），其余按单字节；
     有 /ToUnicode 用它查表，没有就按 WinAnsi/latin-1 尽力解；
  4) 支持对象流（ObjStm）：新版 Word / WPS 会把对象压进流里。
"""

import re
import struct
import zlib


class PdfError(Exception):
    """PDF 无法处理（加密、损坏、非 PDF）。"""


# ---------------------------------------------------------------- 词法

#: 空白与分隔符，PDF 规范里的定义
_WSC = b" \t\r\n\x0c\x00"
_DELIM = b"()<>[]{}/%"


def _read_literal(buf, i):
    """读 `(...)` 字面量，返回 (bytes, 下一个位置)。处理转义与嵌套括号。"""
    n = len(buf)
    i += 1
    depth = 1
    out = bytearray()
    while i < n:
        c = buf[i]
        if c == 0x5C:  # 反斜杠
            i += 1
            if i >= n:
                break
            e = buf[i]
            if e in b"nrtbf":
                out.append({0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}[e])
                i += 1
            elif 0x30 <= e <= 0x37:  # 八进制 \ddd
                j = i
                oct_digits = b""
                while j < n and len(oct_digits) < 3 and 0x30 <= buf[j] <= 0x37:
                    oct_digits += buf[j:j + 1]
                    j += 1
                out.append(int(oct_digits, 8) & 0xFF)
                i = j
            elif e == 0x0A:
                i += 1
            elif e == 0x0D:
                i += 2 if buf[i + 1:i + 2] == b"\n" else 1
            else:
                out.append(e)
                i += 1
            continue
        if c == 0x28:  # (
            depth += 1
        elif c == 0x29:  # )
            depth -= 1
            if depth == 0:
                return bytes(out), i + 1
        out.append(c)
        i += 1
    return bytes(out), i


def _hex_to_bytes(h):
    h = re.sub(rb"[^0-9A-Fa-f]", b"", h)
    if len(h) % 2:
        h += b"0"
    try:
        return bytes.fromhex(h.decode("ascii"))
    except ValueError:
        return b""


def tokens(buf):
    """把内容流切成 token：('num',float) / ('str',bytes) / ('name',str) / ('op',str)。"""
    i, n = 0, len(buf)
    while i < n:
        c = buf[i:i + 1]
        if c in (b" ", b"\t", b"\r", b"\n", b"\x0c", b"\x00"):
            i += 1
            continue
        if c == b"%":
            j = buf.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue
        if c == b"(":
            s, i = _read_literal(buf, i)
            yield ("str", s)
            continue
        if c == b"<":
            if buf[i:i + 2] == b"<<":
                i += 2
                yield ("op", "<<")
                continue
            j = buf.find(b">", i)
            if j < 0:
                return
            yield ("str", _hex_to_bytes(buf[i + 1:j]))
            i = j + 1
            continue
        if c == b">":
            if buf[i:i + 2] == b">":
                i += 2
                yield ("op", ">>")
                continue
            i += 1
            continue
        if c in (b"[", b"]", b"{", b"}"):
            yield ("op", c.decode())
            i += 1
            continue
        if c == b"/":
            j = i + 1
            while j < n and buf[j:j + 1] not in _WSC and buf[j:j + 1] not in _DELIM:
                j += 1
            yield ("name", buf[i + 1:j].decode("latin-1", "ignore"))
            i = j
            continue
        j = i
        while j < n and buf[j:j + 1] not in _WSC and buf[j:j + 1] not in _DELIM:
            j += 1
        if j == i:
            i += 1
            continue
        raw = buf[i:j]
        i = j
        try:
            yield ("num", float(raw))
        except ValueError:
            yield ("op", raw.decode("latin-1", "ignore"))


# ---------------------------------------------------------------- 对象

_OBJ = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b(.*?)endobj", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)


def _objects(raw):
    """扫出所有间接对象：{对象号: 对象体}。"""
    out = {}
    for m in _OBJ.finditer(raw):
        out[int(m.group(1))] = m.group(3)

    # 对象流：把 /Type /ObjStm 里的对象拆出来
    for body in list(out.values()):
        if not re.search(rb"/Type\s*/ObjStm", body):
            continue
        data = _stream_data(body)
        if not data:
            continue
        m = re.search(rb"/N\s+(\d+)", body)
        f = re.search(rb"/First\s+(\d+)", body)
        if not (m and f):
            continue
        count, first = int(m.group(1)), int(f.group(1))
        head = data[:first].split()
        nums, offs = [], []
        for k in range(0, min(len(head), count * 2) - 1, 2):
            try:
                nums.append(int(head[k]))
                offs.append(int(head[k + 1]))
            except ValueError:
                break
        for idx, (num, off) in enumerate(zip(nums, offs)):
            end = offs[idx + 1] if idx + 1 < len(offs) else len(data) - first
            if num not in out:
                out[num] = data[first + off:first + end].strip()
    return out


def _stream_data(body):
    m = _STREAM.search(body)
    if not m:
        return None
    data = m.group(1)
    # 可能带多个过滤器；只要含 FlateDecode 就先解压（其余如 ASCIIHex 罕见，解不动就放弃）
    if b"FlateDecode" in body:
        try:
            return zlib.decompress(data)
        except Exception:
            try:
                return zlib.decompressobj().decompress(data)
            except Exception:
                return None
    return data


def _ref(body, key):
    """取 `/Key N 0 R` 的对象号。"""
    m = re.search(rb"/" + key + rb"\s+(\d+)\s+\d+\s+R", body)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- CMap

def _utf16(h):
    try:
        b = bytes.fromhex(h if len(h) % 2 == 0 else "0" + h)
        return b.decode("utf-16-be", "ignore")
    except Exception:
        return ""


def parse_cmap(data):
    """解析 ToUnicode CMap：返回 {码位: 字符}。"""
    table = {}
    text = data.decode("latin-1", "ignore")

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            table[int(src, 16)] = _utf16(dst)

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            lo_i, hi_i = int(lo, 16), int(hi, 16)
            base = int(dst, 16)
            if hi_i - lo_i > 65535:
                continue
            for k in range(lo_i, hi_i + 1):
                try:
                    table[k] = chr(base + (k - lo_i))
                except ValueError:
                    pass
        # 数组形式：<lo> <hi> [<d1> <d2> ...]
        for lo, hi, arr in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            lo_i = int(lo, 16)
            for idx, dst in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", arr)):
                table[lo_i + idx] = _utf16(dst)
    return table


# ---------------------------------------------------------------- 字体

class _Font:
    __slots__ = ("cmap", "two_byte")

    def __init__(self, cmap=None, two_byte=False):
        self.cmap = cmap
        self.two_byte = two_byte

    def decode(self, data):
        if self.cmap:
            out = []
            if self.two_byte:
                for i in range(0, len(data) - 1, 2):
                    out.append(self.cmap.get((data[i] << 8) | data[i + 1], ""))
            else:
                for b in data:
                    out.append(self.cmap.get(b, ""))
            text = "".join(out)
            if text:
                return text
            # 有 CMap 但一个都查不到：字体的码宽与猜测不符，退回单字节再试一次
            if self.two_byte:
                return "".join(self.cmap.get(b, "") for b in data)
            return ""
        # 无 ToUnicode：简单字体基本都是 WinAnsi/latin-1
        return data.decode("latin-1", "ignore")


def _fonts_of(objs, page_body):
    """页面用到的字体：{资源名: _Font}。"""
    holder = page_body
    ref = _ref(page_body, b"Resources")
    if ref is not None and ref in objs:
        holder = objs[ref]

    m = re.search(rb"/Font\s*<<(.*?)>>", holder, re.S)
    if not m:
        fr = re.search(rb"/Font\s+(\d+)\s+\d+\s+R", holder)
        if not fr:
            return {}
        holder = objs.get(int(fr.group(1)), b"")
        m = re.search(rb"/Font\s*<<(.*?)>>", holder, re.S)
        if not m:
            return {}
    pairs = re.findall(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+\d+\s+R", m.group(1))

    out = {}
    for name, num in pairs:
        body = objs.get(int(num), b"")
        cmap = None
        ref = _ref(body, b"ToUnicode")
        if ref is not None and ref in objs:
            stream = _stream_data(objs[ref])
            if stream:
                cmap = parse_cmap(stream)
        two = bool(re.search(rb"/Subtype\s*/Type0", body))
        if two and cmap is None:
            # 无 ToUnicode 的 Type0：老排版软件（方正等）导出的中文 PDF 都是这副
            # 样子——Identity-H + 字体不内嵌，内容流里全是 2 字节 CID。中文这类
            # 字体在 /DescendantFonts 里标明 Adobe-GB1 字符集，CID→Unicode 可用
            # 官方映射表硬解（gb1map），否则整份文件读出来全是乱码。
            for dref in re.findall(rb"/DescendantFonts\s*\[\s*(\d+)\s+\d+\s+R", body):
                dbody = objs.get(int(dref), b"")
                if re.search(rb"Registry\s*\(?Adobe\)?", dbody) and \
                        re.search(rb"Ordering\s*\(?GB1\)?", dbody):
                    import gb1map
                    cmap = gb1map.MAP
                    break
        out[name.decode("latin-1", "ignore")] = _Font(cmap, two)
    return out


# ---------------------------------------------------------------- 页面树

def _page_order(objs):
    """按 /Pages 树的 /Kids 顺序返回页面对象号。失败则按对象号兜底。"""
    root = None
    for num, body in objs.items():
        if re.search(rb"/Type\s*/Pages\b", body) and b"/Parent" not in body:
            root = num
            break
    order = []

    def walk(num, depth):
        if depth > 40 or num in order:
            return
        body = objs.get(num, b"")
        if re.search(rb"/Type\s*/Page(?![A-Za-z])", body):
            order.append(num)
            return
        kids = re.search(rb"/Kids\s*\[(.*?)\]", body, re.S)
        if not kids:
            return
        for child in re.findall(rb"(\d+)\s+\d+\s+R", kids.group(1)):
            walk(int(child), depth + 1)

    if root is not None:
        walk(root, 0)
    if order:
        return order
    pages = [n for n, b in objs.items()
             if re.search(rb"/Type\s*/Page(?![A-Za-z])", b)]
    return sorted(pages)


def _page_contents(objs, body):
    ref = _ref(body, b"Contents")
    if ref is not None:
        return _stream_data(objs.get(ref, b"")) or b""
    m = re.search(rb"/Contents\s*\[(.*?)\]", body, re.S)
    if not m:
        return b""
    data = b""
    for r in re.findall(rb"(\d+)\s+\d+\s+R", m.group(1)):
        data += _stream_data(objs.get(int(r), b"")) or b""
    return data


# ---------------------------------------------------------------- 文本

_LINE_TOL = 0.6      # 纵坐标变化超过这个值就算换行（PDF 单位）


def page_text(content, fonts):
    """把一个页面的内容流还原成带换行的文本。"""
    cur = _Font()
    stack = []           # 操作数栈
    arr = None           # 正在收集的数组
    last_y = None
    pieces = []
    tm_y = 0.0

    def emit(text):
        if text:
            pieces.append(text)

    def newline_if_needed(y):
        nonlocal last_y
        if last_y is not None and abs(y - last_y) > _LINE_TOL:
            pieces.append("\n")
        last_y = y

    for kind, val in tokens(content):
        if kind == "op":
            op = val
            if op == "[":
                arr = []
                continue
            if op == "]":
                stack.append(("arr", arr or []))
                arr = None
                continue
            if arr is not None and op not in ("]",):
                # 数组里只可能有数字与字符串，name/op 之类忽略
                if kind == "num" or kind == "str":
                    pass
                else:
                    continue
            if op == "BT":
                stack = []
                continue
            if op == "ET":
                stack = []
                continue
            if op == "Tf":
                names = [v for k, v in stack if k == "name"]
                if names:
                    cur = fonts.get(names[-1], _Font())
                stack = []
                continue
            if op == "Tm":
                nums = [v for k, v in stack if k == "num"]
                if len(nums) >= 6:
                    tm_y = nums[5]
                    newline_if_needed(tm_y)
                stack = []
                continue
            if op in ("Td", "TD"):
                nums = [v for k, v in stack if k == "num"]
                if len(nums) >= 2:
                    tm_y += nums[1]
                    if abs(nums[1]) > _LINE_TOL:
                        newline_if_needed(tm_y)
                stack = []
                continue
            if op == "T*":
                pieces.append("\n")
                stack = []
                continue
            if op == "Tj":
                strs = [v for k, v in stack if k == "str"]
                if strs:
                    emit(cur.decode(strs[-1]))
                stack = []
                continue
            if op == "TJ":
                for k, v in stack:
                    if k == "arr":
                        for ak, av in v:
                            if ak == "str":
                                emit(cur.decode(av))
                            elif ak == "num" and av < -120:
                                emit(" ")
                stack = []
                continue
            if op in ("'", '"'):
                pieces.append("\n")
                strs = [v for k, v in stack if k == "str"]
                if strs:
                    emit(cur.decode(strs[-1]))
                stack = []
                continue
            stack = []
            continue

        # 非操作符：进栈
        if arr is not None:
            arr.append((kind, val))
        else:
            stack.append((kind, val))
        if len(stack) > 64:
            stack = stack[-64:]
    return "".join(pieces)


# ---------------------------------------------------------------- 页面图片
#
# 图片型 PDF（WPS「输出为 PDF」、扫描软件、拍照转存）每页正文就是一张位图，
# 没有文字层。这类文件不能靠读文字，只能把页面图取出来交给视觉模型。
# 好消息是不需要 PDF 渲染器：这些页面图本来就是内嵌的 XObject，
# FlateDecode + Indexed/DeviceRGB（或 DeviceGray/DeviceRGB）的组合用 zlib
# 就能解，PNG 也能用 zlib+struct 直接写——零第三方依赖。


def _page_xobjects(objs, page_body):
    """页面引用的 XObject：{资源名: 对象号}（含图片、表单）。"""
    holder = page_body
    ref = _ref(page_body, b"Resources")
    if ref is not None and ref in objs:
        holder = objs[ref]
    m = re.search(rb"/XObject\s*<<(.*?)>>", holder, re.S)
    if not m:
        xr = re.search(rb"/XObject\s+(\d+)\s+\d+\s+R", holder)
        if not xr:
            return {}
        inner = objs.get(int(xr.group(1)), b"")
        m = re.search(rb"<<(.*)>>", inner, re.S)
        if not m:
            return {}
    return {name.decode("latin-1"): int(num)
            for name, num in re.findall(rb"/([^\s/<>\[\]]+)\s+(\d+)\s+\d+\s+R", m.group(1))}


def _color_space(objs, body):
    """把 /ColorSpace 归一成一段文本（支持内联数组、单个名字、间接引用）。"""
    m = re.search(rb"/ColorSpace\s*\[(.*?)\]", body, re.S)
    if m:
        return m.group(1)
    m = re.search(rb"/ColorSpace\s*/([A-Za-z0-9]+)", body)
    if m:
        return m.group(1)
    ref = _ref(body, b"ColorSpace")
    if ref is not None and ref in objs:
        return objs[ref].strip(b" \r\n\t<>")
    return b""


def _pixel_rows(objs, body):
    """把 8 位图像对象解成 RGB 行；解不了返回 None（交给上层跳过）。

    支持 DeviceRGB / DeviceGray / Indexed[DeviceRGB] 三种常见形态。
    CMYK、JPX（JPEG2000）、CCITT 传真压缩解不动——扫描件里偶见，
    与其解出错色不如老实跳过。
    """
    if not re.search(rb"/Subtype\s*/Image", body):
        return None
    mw = re.search(rb"/Width\s+(\d+)", body)
    mh = re.search(rb"/Height\s+(\d+)", body)
    mb = re.search(rb"/BitsPerComponent\s+(\d+)", body)
    if not (mw and mh):
        return None
    if mb and int(mb.group(1)) != 8:
        return None
    cs = _color_space(objs, body)
    if b"JPXDecode" in body or b"CCITTFaxDecode" in body:
        return None
    data = _stream_data(body)
    if not data:
        return None
    w, h = int(mw.group(1)), int(mh.group(1))

    if b"Indexed" in cs and b"DeviceRGB" in cs:
        ref = re.search(rb"(\d+)\s+\d+\s+R", cs)
        pal = _stream_data(objs.get(int(ref.group(1)), b"")) if ref else b""
        if len(pal) < 3:
            return None
        table = [pal[i * 3:i * 3 + 3].ljust(3, b"\xff") for i in range(256)]
        need = w * h
        if len(data) < need:
            return None
        flat = b"".join(map(table.__getitem__, data[:need]))
    elif b"DeviceGray" in cs or (b"DeviceRGB" not in cs and b"Indexed" not in cs
                                 and len(data) >= w * h and len(data) < w * h * 3):
        # 无声明时按体量猜：正好 w*h 就是灰度，w*h*3 才是彩色
        need = w * h
        if len(data) < need:
            return None
        flat = b"".join(bytes((v, v, v)) for v in data[:need])
    elif b"DeviceRGB" in cs:
        need = w * h * 3
        if len(data) < need:
            return None
        flat = data[:need]
    else:
        return None

    return w, h, [flat[y * w * 3:(y + 1) * w * 3] for y in range(h)]


_CM_RE = re.compile(rb"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+"
                    rb"(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+cm")


def _image_matrix(content, name):
    """图片被 `Do` 摆上去时的变换矩阵（最近的 cm）。"""
    for m in re.finditer(rb"/" + re.escape(name.encode("latin-1")) + rb"\s+Do\b", content):
        found = _CM_RE.findall(content[:m.start()])
        if found:
            return [float(x) for x in found[-1]]
    return None


def _png(w, h, rows):
    """RGB 行 → PNG 字节（只用 zlib）。"""
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _decodable(objs, body):
    """不解压也能判断"这张图我们解不解得动"（计数与提示用，快）。"""
    if not re.search(rb"/Subtype\s*/Image", body):
        return False
    if b"JPXDecode" in body or b"CCITTFaxDecode" in body:
        return False
    mb = re.search(rb"/BitsPerComponent\s+(\d+)", body)
    if mb and int(mb.group(1)) != 8:
        return False
    if not re.search(rb"/Width\s+\d+", body) or not re.search(rb"/Height\s+\d+", body):
        return False
    cs = _color_space(objs, body)
    if b"Indexed" in cs and b"DeviceRGB" in cs:
        return True
    return b"DeviceGray" in cs or b"DeviceRGB" in cs


def page_images(raw, decode=True):
    """抽出每页的内嵌图片。

    返回 [{page, name, width, height, png}]；decode=False 时只数个数（png=None），
    用于「先告诉用户有多少页图」这种不需要像素的场合。

    **方向**：PDF 用户空间 y 轴朝上、位图行序朝下，所以排版软件几乎都在 `cm`
    里写负的 d（这份卷子就是 `595.2 0 0 -770.4 0 806.13 cm`）。照原样导出会得到
    上下颠倒的图，视觉模型能认字但卷面错位、题号对不上，所以按矩阵把行序翻回来。
    a 为负时同理左右镜像。
    """
    if not raw[:1024].lstrip().startswith(b"%PDF") and b"%PDF" not in raw[:1024]:
        raise PdfError("这不是 PDF 文件")
    objs = _objects(raw)
    if not objs:
        raise PdfError("PDF 结构无法识别（可能是损坏文件）")

    out = []
    for page_no, num in enumerate(_page_order(objs), 1):
        body = objs.get(num, b"")
        content = _page_contents(objs, body)
        seen = set()
        for name, onum in _page_xobjects(objs, body).items():
            if onum in seen:
                continue
            seen.add(onum)
            ibody = objs.get(onum, b"")
            if not re.search(rb"/Subtype\s*/Image", ibody):
                continue
            if not _decodable(objs, ibody):
                continue
            if not decode:
                out.append({"page": page_no, "name": name, "width": 0,
                            "height": 0, "png": None})
                continue
            got = _pixel_rows(objs, ibody)
            if not got:
                continue
            w, h, rows = got
            mtx = _image_matrix(content, name)
            if mtx:
                if mtx[3] < 0:
                    rows = rows[::-1]
                if mtx[0] < 0:
                    rows = [b"".join(r[x:x + 3] for x in
                                     range(len(r) - 3, -1, -3)) for r in rows]
            out.append({"page": page_no, "name": name, "width": w,
                        "height": h, "png": _png(w, h, rows)})
    return out


def page_image_count(raw):
    """只数页面图，不落像素（给"要不要视觉模型"这类提示用，快得多）。"""
    return len(page_images(raw, decode=False))


def extract_pages(raw):
    """返回每页文本（列表长度 = 页数）。"""
    if not raw[:1024].lstrip().startswith(b"%PDF") and b"%PDF" not in raw[:1024]:
        raise PdfError("这不是 PDF 文件")
    if re.search(rb"/Encrypt\s", raw):
        raise PdfError("PDF 已加密，请先解除密码保护再导入")

    objs = _objects(raw)
    if not objs:
        raise PdfError("PDF 结构无法识别（可能是损坏文件）")

    pages = []
    for num in _page_order(objs):
        body = objs.get(num, b"")
        fonts = _fonts_of(objs, body)
        page = page_text(_page_contents(objs, body), fonts)
        # 页首/页尾孤立的页码行（「1」「12」独占一行）：PDF 的页码是排版系统
        # 加的，不是题目内容。留着它，下一层的拆题会把页 1 的「1」当成第一道
        # 题的题号，把卷头标题整个吞进题干。页码只在版心边缘：跳过纯空白行后
        # 看首、尾各两行，正文里的孤立数字（如「共 3 台空调，已开启 2」的续行）
        # 不受影响。
        lines = page.split("\n")
        first = next((i for i, l in enumerate(lines) if l.strip()), 0)
        last = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()),
                    len(lines) - 1)
        for idx in (first, first + 1, last - 1, last):
            if 0 <= idx < len(lines) and re.fullmatch(r"\s*\d{1,3}\s*", lines[idx]):
                lines[idx] = ""
        pages.append("\n".join(lines))
    return pages


def read_pdf(raw):
    """给导入用的统一入口。

    返回 {pages, text, chars, page_count, scanned}：
      scanned=True 表示"翻得开但几乎没有文字"，通常是扫描件或拍照转的 PDF。

    阈值取"每页平均不到 10 个非空白字符"：支撑一道题的题干加选项远不止 10 个字，
    低于这个量只能是页眉页脚残留或纯图片。定得太高会冤枉正常的短卷子（比如只有
    几行指令的实操卷），定得太低又会把扫描件当成"读出来了但内容为空"。
    """
    pages = extract_pages(raw)
    text = "\n".join(p.strip() for p in pages)
    chars = len(re.sub(r"\s", "", text))
    return {
        "pages": pages,
        "text": text,
        "chars": chars,
        "page_count": len(pages),
        "scanned": chars < max(10, 10 * len(pages)),
    }


#: 兼容旧调用（tests/pdftext.py 与 P7 打印断言都用这个名字）
def extract_text(raw):
    return extract_pages(raw)
