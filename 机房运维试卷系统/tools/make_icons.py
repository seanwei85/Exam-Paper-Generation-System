"""把 assets/logo.svg 导出成多尺寸 PNG 与 ico（纯标准库 + 本机 Chrome 截图）。

用法：
    python tools/make_icons.py

产物（assets/）：
    logo-16.png ... logo-512.png
    logo.ico            Windows 图标（PNG 压缩条目，Vista 及以上支持）
    logo.png            512px 单图，便于对外发图

不引入 Pillow：光栅化交给本机 Chrome 的 --screenshot，
ico 容器自己拼（ICONDIR + ICONDIRENTRY + PNG 数据）。
"""

import os
import struct
import subprocess
import sys
import tempfile

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


def main():
    browser = find_browser()
    if not os.path.exists(SVG):
        raise SystemExit("缺少 assets/logo.svg")
    pngs = []
    for size in SIZES:
        out = os.path.join(ASSETS, "logo-%d.png" % size)
        render_png(browser, size, out, SVG_SMALL if size <= SMALL_MAX else SVG)
        pngs.append(out)
        print("PNG %3dpx  %s" % (size, os.path.basename(out)))
    ico = build_ico([os.path.join(ASSETS, "logo-%d.png" % s) for s in ICO_SIZES],
                    os.path.join(ASSETS, "logo.ico"))
    with open(os.path.join(ASSETS, "logo-512.png"), "rb") as src, \
            open(os.path.join(ASSETS, "logo.png"), "wb") as dst:
        dst.write(src.read())
    print("ICO        %s  (%.1f KB)" % (os.path.basename(ico), os.path.getsize(ico) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
