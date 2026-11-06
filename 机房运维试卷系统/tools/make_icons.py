"""把 assets/logo.svg 导出成多尺寸 PNG 与 ico（纯标准库 + 本机 Chrome 截图）。

用法：
    python tools/make_icons.py                # 重新渲染 SVG 并生成全部产物
    python tools/make_icons.py --dib-only     # 只用已有 PNG 重建 exe 专用 ico

产物（assets/）：
    logo-16.png ... logo-512.png
    logo.ico            Windows 图标（PNG 压缩条目，Vista 及以上支持）
    logo_exe.ico        标准 DIB 帧图标，**打包 exe 专用**
    logo.png            512px 单图，便于对外发图

不引入 Pillow：光栅化交给本机 Chrome 的 --screenshot，ico 容器自己拼。
为什么要两份 ico：PNG 帧 ico 在部分 Explorer 图标缓存 / 旧系统上会退化成
默认图标；exe 的 RT_ICON 用 DIB(BMP) 帧才全版本可靠。界面里的图标是
base64 内嵌的，不依赖这两个文件。
"""

import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ASSETS = os.path.join(ROOT, "assets")
SVG = os.path.join(ASSETS, "logo.svg")
SVG_SMALL = os.path.join(ASSETS, "logo-small.svg")

#: 16/24 像素用简化版：对勾徽章在这个尺寸下会糊成一团，改画两张纸 + 加粗横线
SMALL_MAX = 24

SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    for p in BROWSERS:
        if os.path.exists(p):
            return p
    raise SystemExit("找不到 Chrome/Edge，无法光栅化 SVG")


def render_png(browser, size, out_path, svg=None):
    """用无头浏览器把 svg 截图成指定尺寸的透明 PNG。"""
    svg = svg or SVG
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:transparent}"
        "img{display:block;width:%dpx;height:%dpx}</style></head>"
        "<body><img src='file:///%s'></body></html>"
        % (size, size, svg.replace("\\", "/").lstrip("/"))
    )
    with tempfile.TemporaryDirectory() as tmp:
        page = os.path.join(tmp, "page.html")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        profile = os.path.join(tmp, "profile")
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--allow-file-access-from-files",
            "--user-data-dir=" + profile,
            "--default-background-color=00000000",
            "--window-size=%d,%d" % (size, size),
            "--screenshot=" + out_path,
            "file:///" + page.replace("\\", "/").lstrip("/"),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if not os.path.exists(out_path):
            raise SystemExit("截图失败 size=%d\n%s" % (size, r.stderr.decode("utf-8", "ignore")[:800]))
    return out_path


def build_ico(png_paths, out_path):
    """拼一个多尺寸 ico；每帧用 PNG 压缩数据（Vista+ 原生支持）。"""
    blobs = []
    for p in png_paths:
        with open(p, "rb") as f:
            blobs.append((png_side(p), f.read()))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    for side, data in blobs:
        dim = 0 if side >= 256 else side
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(out_path, "wb") as f:
        f.write(header + entries + b"".join(d for _, d in blobs))
    return out_path


def png_side(path):
    for size in SIZES:
        if path.endswith("-%d.png" % size):
            return size
    raise SystemExit("无法从文件名推断尺寸：" + path)


def decode_png(data):
    """把 8bit RGBA/RGB 非交错 PNG 解成 BGRA 像素（纯标准库）。

    DIB 图标要的是 BITMAPINFOHEADER + 32bpp BGRA + AND mask，
    不能直接把 PNG 数据塞进 RT_ICON —— 那是 PNG 帧 ico 兼容性问题的根源。
    """
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    pos, idat, w, h, bd, ct, inter = 8, b"", 0, 0, 0, 0, 0
    while pos + 8 <= len(data):
        ln = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            w, h, bd, ct, _, _, inter = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + ln
    assert bd == 8 and ct in (2, 6) and inter == 0, \
        "只支持 8bit RGB/RGBA 非交错 PNG，实际 bd=%d ct=%d inter=%d" % (bd, ct, inter)
    bpp = 4 if ct == 6 else 3
    raw = zlib.decompress(idat)
    stride = w * bpp
    rows, prev, p = [], bytearray(stride), 0
    for _ in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 1:
                line[i] = (line[i] + a) & 255
            elif ft == 2:
                line[i] = (line[i] + b) & 255
            elif ft == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif ft == 4:
                q = a + b - c
                pa, pb, pc = abs(q - a), abs(q - b), abs(q - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        rows.append(bytes(line))
        prev = line
    out = bytearray()
    for row in rows:
        for x in range(w):
            px = row[x * bpp:(x + 1) * bpp]
            r, g, b = px[0], px[1], px[2]
            a = px[3] if bpp == 4 else 255
            out += bytes((b, g, r, a))
    return w, h, bytes(out)


def dib_frame(w, h, bgra):
    """一帧 ICO 的 DIB 数据：BITMAPINFOHEADER + 自下而上 BGRA + 1bpp AND mask。"""
    xor = b"".join(bgra[(h - 1 - y) * w * 4:(h - y) * w * 4] for y in range(h))
    mask_row = ((w + 31) // 32) * 4
    and_mask = b"\x00" * (mask_row * h)  # 32bpp 用 alpha，mask 全 0 即可
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                         len(xor) + len(and_mask), 0, 0, 0, 0)
    return header + xor + and_mask


def build_ico_dib(png_paths, out_path):
    """标准 DIB 帧 ico：所有 Windows 版本/资源管理器都能显示，不依赖 PNG 帧支持。"""
    blobs = []
    for p in png_paths:
        side = png_side(p)
        with open(p, "rb") as f:
            w, h, bgra = decode_png(f.read())
        assert (w, h) == (side, side), "%s 尺寸不符 %dx%d" % (p, w, h)
        blobs.append((side, dib_frame(w, h, bgra)))
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries = b""
    for side, data in blobs:
        dim = 0 if side >= 256 else side
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    with open(out_path, "wb") as f:
        f.write(header + entries + b"".join(d for _, d in blobs))
    return out_path


def main():
    # --dib-only：不重新渲染 SVG，只用已有 PNG 重建 exe 专用 ico（无需浏览器）
    dib_only = "--dib-only" in sys.argv
    ico_pngs = [os.path.join(ASSETS, "logo-%d.png" % s) for s in ICO_SIZES]
    if not dib_only:
        browser = find_browser()
        if not os.path.exists(SVG):
            raise SystemExit("缺少 assets/logo.svg")
        for size in SIZES:
            out = os.path.join(ASSETS, "logo-%d.png" % size)
            render_png(browser, size, out, SVG_SMALL if size <= SMALL_MAX else SVG)
            print("PNG %3dpx  %s" % (size, os.path.basename(out)))
        ico = build_ico(ico_pngs, os.path.join(ASSETS, "logo.ico"))
        print("ICO        %s  (%.1f KB)" % (os.path.basename(ico),
                                            os.path.getsize(ico) / 1024.0))
        with open(os.path.join(ASSETS, "logo-512.png"), "rb") as src, \
                open(os.path.join(ASSETS, "logo.png"), "wb") as dst:
            dst.write(src.read())
    dib = build_ico_dib(ico_pngs, os.path.join(ASSETS, "logo_exe.ico"))
    print("ICO(DIB)   %s  (%.1f KB)  <- 打包用，exe 图标" %
          (os.path.basename(dib), os.path.getsize(dib) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
