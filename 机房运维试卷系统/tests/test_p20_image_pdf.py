#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P20：图片型 PDF 自己抽页面图 → 视觉模型。

背景：用户手上有一份《机房运维试题及答案.pdf》，每页就是一张整幅位图
（WPS「输出为 PDF」导出的、FlateDecode + Indexed/DeviceRGB），PDF 里一个字体
都没有。此前这种文件只能让用户"每页另存为 PNG 再逐张上传"——几十页的卷子
没人愿意干。

做法（纯标准库，不引渲染器）：
  * 页面图本来就是内嵌的 XObject，zlib 解压 + 调色板展开就能拿到像素；
  * PNG 用 zlib+struct 自己写；
  * 方向：PDF 用户空间 y 轴朝上，排版软件普遍写负的 d（这份是
    `595.2 0 0 -770.4 0 806.13 cm`），不翻回来会得到上下颠倒的图；
  * 逐页送视觉模型，单页失败保留其余页（和文本分块失败一个道理）。

方向这类事没法靠断言"看起来对"，所以第一页的 PNG 哈希是**人工核对过方向之后**
固定的金标准：谁改了矩阵逻辑，这里立刻红。
"""

import base64
import hashlib
import os
import re
import struct
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pdftext  # noqa: E402
from httpbase import HttpCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "image_paper.pdf")

#: 人工核对过方向的第一页 PNG 哈希（992x1284）
PAGE1_SHA = "368a88513677fca2c957fc5fb82026f36c6aa19fbc66b020fae9af2d1c7f44e8"
PAGE2_SHA = "88a9b69dc0b68bb24820a543c36af26d7a1d0b7ab1562adb296d68b576dba99a"

SECRET = "sk-test-p20-abcdefg123456"

THREE = {"qtype": "single", "cat": "电气", "diff": "易",
         "stem": "机房温度一般应保持在（　）。",
         "options": [{"key": "A", "text": "10 - 20℃"},
                     {"key": "B", "text": "15 - 25℃"},
                     {"key": "C", "text": "20 - 30℃"}],
         "answer": "B", "analysis": ""}


def _fixture():
    with open(FIXTURE, "rb") as f:
        return f.read()


def _png_size(png):
    """从 IHDR 读宽高，顺带证明这真是张能打开的 PNG。"""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _cmyk_pdf():
    """一张 DeviceCMYK 图的 PDF：本地解不动，必须老实跳过而不是解成花屏。"""
    w = h = 4
    raw = b"\x00" * (w * h * 4)
    comp = zlib.compress(raw)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /XObject << /IM1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream"
        % (len(b"q 200 0 0 200 0 0 cm /IM1 Do Q"), b"q 200 0 0 200 0 0 cm /IM1 Do Q"),
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /BitsPerComponent 8 "
        b"/ColorSpace /DeviceCMYK /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream"
        % (w, h, len(comp), comp),
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objs) + 1, xref))
    return out


class P20Extract(unittest.TestCase):
    """抽图本身：页数、尺寸、方向、解不动的要跳过。"""

    def test_extracts_every_page(self):
        pages = pdftext.page_images(_fixture())
        self.assertEqual(len(pages), 2, "两页的卷子应该抽出两张页面图")
        self.assertEqual([p["page"] for p in pages], [1, 2], "页序不对")

    def test_png_is_valid_and_full_size(self):
        for p in pdftext.page_images(_fixture()):
            w, h = _png_size(p["png"])
            self.assertEqual((w, h), (p["width"], p["height"]))
            self.assertEqual((w, h), (992, 1284), "页面图被缩放或裁过")

    def test_image_is_upright(self):
        """负 d 的 cm 必须把行序翻回来，否则视觉模型看到的是一张倒置的卷子。"""
        pages = pdftext.page_images(_fixture())
        self.assertEqual(hashlib.sha256(pages[0]["png"]).hexdigest(), PAGE1_SHA,
                         "第一页方向/像素变了：确认 cm 翻转逻辑没被改坏")
        self.assertEqual(hashlib.sha256(pages[1]["png"]).hexdigest(), PAGE2_SHA,
                         "第二页方向/像素变了")

    def test_count_without_decoding(self):
        """只数页数时不落像素——给"要不要视觉模型"的提示用，得够快。"""
        self.assertEqual(pdftext.page_image_count(_fixture()), 2)

    def test_unsupported_colorspace_is_skipped(self):
        """CMYK/JPEG2000 解不动就跳过，不能解成花屏丢给模型。"""
        self.assertEqual(pdftext.page_images(_cmyk_pdf()), [])
        self.assertEqual(pdftext.page_image_count(_cmyk_pdf()), 0)

    def test_not_a_pdf(self):
        with self.assertRaises(pdftext.PdfError):
            pdftext.page_images(b"hello world")


class P20Vision(HttpCase):
    """接视觉模型：逐页送、单页失败保留其余页。"""

    @classmethod
    def setUpClass(cls):
        cls._no_ocr()            # 必须在服务进程出生前就位
        super().setUpClass()
        cls.fake = FakeAI().start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.fake.stop()
        finally:
            super().tearDownClass()
            cls._restore_ocr()
    @classmethod
    def _no_ocr(cls):
        """P30 起 OCR 通道先于视觉模型，测视觉路径必须显式声明"引擎不可用"。"""
        os.environ["ZJLAB_OCR_PS1"] = os.path.join(HERE, "fixtures", "fake_ocr.ps1")
        os.environ["FAKE_OCR_MODE"] = "gone"

    @classmethod
    def _restore_ocr(cls):
        for k in ("ZJLAB_OCR_PS1", "FAKE_OCR_MODE", "FAKE_OCR_TEXT"):
            os.environ.pop(k, None)


    def setUp(self):
        super().setUp()
        self.fake.reset()
        import ai_parse
        self.ai = ai_parse
        self.ai.RETRY_BACKOFF = 0
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat", "vision_model": "fake-vl"})
        self.cats = self.core.categories_tree(self.core.connect())

    def test_pages_are_sent_one_by_one(self):
        for _ in range(2):
            self.fake.queue_json([THREE])
        res = self.ai.extract_pdf_images(_fixture(), cats=self.cats)
        self.assertEqual(len(res["items"]), 2, "两页应各出一题")
        self.assertEqual(res["by"], "ai-vision")
        self.assertEqual(res["chunks"], 2)
        sent = [r for r in self.fake.requests if r["body"].get("messages")]
        self.assertEqual(len(sent), 2, "应该按页发两次请求，而不是整份 PDF 硬塞")
        for r in sent:
            parts = r["body"]["messages"][-1]["content"]
            self.assertTrue(any(p.get("type") == "image_url" for p in parts),
                            "请求里没有图，视觉模型看不到东西")

    def test_one_bad_page_keeps_the_rest(self):
        """第 1 页模型拒收（400 不重试），第 2 页正常——不能因为一页废掉整份。"""
        self.fake.queue_fail(400, "image too large")
        self.fake.queue_json([THREE])
        res = self.ai.extract_pdf_images(_fixture(), cats=self.cats)
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["by"], "ai-vision+partial")
        self.assertTrue(any("第 1 页" in w for w in res["warnings"]), res["warnings"])

    def test_all_pages_fail_is_actionable(self):
        self.fake.queue_fail(400, "no vision")
        self.fake.queue_fail(400, "no vision")
        with self.assertRaises(self.ai.AIError) as ctx:
            self.ai.extract_pdf_images(_fixture(), cats=self.cats)
        self.assertIn("视觉模型", str(ctx.exception), "要给出去哪配视觉模型的指引")

    # ---------------------------------------------------------- 导入接口

    def upload(self, raw, filename, engine=None):
        payload = {"filename": filename,
                   "content": base64.b64encode(raw).decode("ascii")}
        if engine:
            payload["engine"] = engine
        return self.post_json("/api/import/parse", payload, timeout=60)

    def test_import_scanned_pdf_goes_through_vision(self):
        self.fake.queue_json([THREE])
        self.fake.queue_json([THREE])
        res = self.upload(_fixture(), "机房运维试题及答案.pdf", engine="ai")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual(res["by"], "ai-vision")

    def test_local_mode_refuses_image_pdf_with_reason(self):
        res = self.upload(_fixture(), "机房运维试题及答案.pdf", engine="local")
        self.assertFalse(res.get("ok"), "本地模式不该去解图片")
        text = res.get("error") or ""
        self.assertIn("图片版", text)
        self.assertIn("页面图", text, "要告诉用户：软件已经把页面图抽出来了")

    def test_no_ai_config_message_mentions_vision_model(self):
        self.ai.save_config({"clear_key": True, "base_url": "", "model": "",
                             "vision_model": ""})
        res = self.upload(_fixture(), "机房运维试题及答案.pdf")
        self.assertFalse(res.get("ok"))
        self.assertIn("视觉模型", res.get("error") or "")


if __name__ == "__main__":
    unittest.main()
