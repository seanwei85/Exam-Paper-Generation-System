#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P34 品牌标识：logo 资源、浏览器标签页图标、侧边栏标识。

图标一律内嵌（data URI），因为本机版是离线跑的单文件界面，
外链图片一旦断网就会变成破图；同时界面里也不允许出现 http 图标地址。
"""

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402

UI = os.path.join(ROOT, "ui.html")
ASSETS = os.path.join(ROOT, "assets")

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def read_ui():
    with open(UI, "r", encoding="utf-8") as f:
        return f.read()


def parse_ico(path):
    """返回 [(宽, 高, 数据字节)]；宽高 0 表示 256。"""
    with open(path, "rb") as f:
        raw = f.read()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert reserved == 0 and kind == 1, "不是合法 ico"
    frames = []
    for i in range(count):
        off = 6 + 16 * i
        w, h, _colors, _res, _planes, _bpp, size, data_off = struct.unpack("<BBBBHHII", raw[off:off + 16])
        frames.append((w or 256, h or 256, raw[data_off:data_off + size]))
    return frames


class P34BrandStatic(unittest.TestCase):
    """资源与静态结构：不需要起服务。"""

    def test_svg_source_exists(self):
        svg = os.path.join(ASSETS, "logo.svg")
        self.assertTrue(os.path.exists(svg), "缺少 assets/logo.svg 源文件")
        with open(svg, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn('viewBox="0 0 128 128"', text)
        self.assertIn("#114A8F", text, "主色应保持深蓝")

    def test_ico_is_multisize_with_png_frames(self):
        ico = os.path.join(ASSETS, "logo.ico")
        self.assertTrue(os.path.exists(ico), "缺少 assets/logo.ico")
        frames = parse_ico(ico)
        sizes = [w for w, _, _ in frames]
        for need in (16, 32, 48, 256):
            self.assertIn(need, sizes, "ico 缺少 %dpx 帧" % need)
        for w, _h, data in frames:
            self.assertTrue(data.startswith(PNG_SIG), "%dpx 帧不是 PNG 数据" % w)

    def test_png_export_is_rgba(self):
        png = os.path.join(ASSETS, "logo-256.png")
        self.assertTrue(os.path.exists(png), "缺少 assets/logo-256.png")
        with open(png, "rb") as f:
            head = f.read(26)
        self.assertTrue(head.startswith(PNG_SIG))
        width, height, bitdepth, colortype = struct.unpack(">IIBB", head[16:26])
        self.assertEqual((width, height), (256, 256))
        self.assertEqual((bitdepth, colortype), (8, 6), "必须是带透明通道的 RGBA")

    def test_favicon_is_inlined_not_linked(self):
        html = read_ui()
        self.assertIn('rel="icon"', html, "缺少浏览器标签页图标")
        self.assertIn('type="image/svg+xml"', html)
        self.assertIn("data:image/svg+xml;base64,", html)
        self.assertNotIn('rel="icon" href="http', html, "图标不允许外链，断网会变破图")

    def test_brand_shows_logo_mark(self):
        html = read_ui()
        self.assertIn('class="brand-logo"', html, "侧边栏品牌区没有 logo")
        self.assertIn(".brand-logo{width:30px;height:30px", html, "logo 尺寸样式缺失")


class P34BrandRender(RenderCase):
    """真实浏览器里确认图标真的解码出来了，而不是 404 后的空占位。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.chrome = Chrome(cls.browser)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.chrome.close()
        finally:
            super().tearDownClass()

    def test_brand_logo_decoded_in_browser(self):
        c = self.nav("dashboard", "!!document.querySelector('.brand-logo')", timeout=25)
        c.wait("document.querySelector('.brand-logo').complete===true", timeout=15)
        w = c.eval("document.querySelector('.brand-logo').naturalWidth")
        self.assertGreater(int(w or 0), 0, "logo 图片未解码（data URI 可能写坏了）")
        box = c.eval(
            "(function(){var r=document.querySelector('.brand-logo').getBoundingClientRect();"
            "return [Math.round(r.width),Math.round(r.height)];})()")
        self.assertEqual(box, [30, 30], "logo 应固定 30×30，不能被文字挤扁")
        self.assertTrue(c.eval("document.title==='机房运维试卷系统'"))

    def test_favicon_link_present_in_dom(self):
        c = self.nav("dashboard", "!!document.querySelector('link[rel=icon]')", timeout=25)
        href = c.eval("document.querySelector('link[rel=icon]').getAttribute('href')")
        self.assertTrue(href.startswith("data:image/svg+xml;base64,"), "标签页图标未内嵌")


if __name__ == "__main__":
    unittest.main(verbosity=2)
