#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P30：Windows 自带 OCR 通道（FR-IM-28）。

动机：图片型 PDF 此前只有视觉模型一条路，模型不支持看图就全卡死。
Windows 10/11 自带中文 OCR（Windows.Media.Ocr），认出的文字交给现有
本地规则解析——零联网、零安装、不挑模型。本组用例验证三件事：

1. OCR 通道打通后，auto 与 local 引擎都能吃图片型 PDF，且不发任何网络请求；
2. OCR 各环节失败（引擎缺失、认出字但拆不出题）都要安静退回原视觉模型
   流程，错误文案保持原来的可操作性；
3. ocr_png.ps1 与 ocr_win.py 的契约：脚本存在、关键调用齐全、NOOCR 约定。

注入方式：服务端是子进程，进程内 monkeypatch 够不着；改用环境变量把
ocr_png.ps1 换成假脚本（fake_ocr.ps1），行为由环境变量切换。环境变量
只在各场景类的 setUpClass 里设置、tearDownClass 里清掉——服务进程是
在 setUpClass 里拉起来的，环境必须在它出生前就位。
"""

import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ocr_win  # noqa: E402
from httpbase import HttpCase  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "image_paper.pdf")
FAKE_PS1 = os.path.join(HERE, "fixtures", "fake_ocr.ps1")

#: 版式规整的一段"OCR 结果"，本地规则必须能拆出这道单选
OCR_TEXT = """1、机房 UPS 蓄电池放电终止电压一般不低于（　）。
A、10.8V
B、12V
C、14V
答案：A"""

#: 去假空格会顺带压掉"中文与英文之间"的空格（"机房 UPS" → "机房UPS"），
#: 所以金标准按压缩后的写法断言——判重本来就忽略空白，显示上无碍。
GOLD_STEM = "UPS蓄电池放电终止电压"


def _fixture():
    with open(FIXTURE, "rb") as f:
        return f.read()


def _fake_engine(mode, text=""):
    """在服务进程出生前把假 OCR 就位（继承给子进程）。"""
    os.environ["ZJLAB_OCR_PS1"] = FAKE_PS1
    os.environ["FAKE_OCR_MODE"] = mode
    os.environ["FAKE_OCR_TEXT"] = text


def _restore_engine():
    for k in ("ZJLAB_OCR_PS1", "FAKE_OCR_MODE", "FAKE_OCR_TEXT"):
        os.environ.pop(k, None)


class ScannedCase(HttpCase):
    """公共骨架：喂两页的图片型 PDF。"""

    def setUp(self):
        super().setUp()
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)          # 保持"AI 未配置"，退回路径的报错才可断言

    def upload(self, engine=None):
        payload = {"filename": "image_paper.pdf",
                   "content": base64.b64encode(_fixture()).decode("ascii")}
        if engine:
            payload["engine"] = engine
        return self.post_json("/api/import/parse", payload, timeout=60)


class OcrWorks(ScannedCase):
    """假 OCR 返回规整文字：两条引擎档都应直接出题、零网络请求。"""

    @classmethod
    def setUpClass(cls):
        _fake_engine("questions", OCR_TEXT)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            _restore_engine()

    def test_auto_engine_goes_through_ocr(self):
        res = self.upload("auto")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr", "来源要标明是本地 OCR，不能冒充规则解析")
        self.assertEqual(len(res["items"]), 2, "两页各出一题（判重发生在入库，不在解析）")
        it = res["items"][0]
        self.assertIn(GOLD_STEM, it["stem"])
        self.assertEqual(it["answer"], "A")
        self.assertEqual(it["qtype"], "single")

    def test_local_engine_also_uses_ocr(self):
        """「只用本地规则」现在也能吃图片型 PDF——这是本功能最大的收益。"""
        res = self.upload("local")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr")


class OcrSpaced(ScannedCase):
    """Windows OCR 的真实输出：汉字之间夹假空格。

    假空格必须在 ocr_png_bytes 出口就清掉——下游按"行首题号 + 选项字母"
    拆题，字缝里的空格会让题号和选项一起认不出来。此前 _despace 只有单元测试，
    链路里没接上，真机跑出来是"认了 467 个字却只拆出 2 道题"。
    """

    #: 与 OCR_TEXT 同题，只是每个汉字之间都插了空格（真机形态）
    SPACED = """1、机 房 U P S 蓄 电 池 放 电 终 止 电 压 一 般 不 低 于 （ 　 ） 。
A、10.8V
B、12V
C、14V
答 案 ： A"""

    @classmethod
    def setUpClass(cls):
        _fake_engine("questions", cls.SPACED)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            _restore_engine()

    def test_stem_is_free_of_spurious_spaces(self):
        res = self.upload("auto")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr")
        it = res["items"][0]
        self.assertNotIn("机 房", it["stem"])
        self.assertIn(GOLD_STEM.replace(" ", ""), it["stem"].replace(" ", ""))
        self.assertEqual(it["answer"], "A", "答案也得从'答 案 ： A'里抽出来")


class OcrGone(ScannedCase):
    """假 OCR 报 NOOCR（等价于没装中文语言包）：要安静退回原视觉模型流程。"""

    @classmethod
    def setUpClass(cls):
        _fake_engine("gone")
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            _restore_engine()

    def test_auto_engine_falls_back_to_ai_guidance(self):
        res = self.upload("auto")
        self.assertFalse(res.get("ok"))
        self.assertIn("AI", res.get("error", ""), "要指引去配置 AI 识别")

    def test_local_engine_keeps_original_message(self):
        """local 档退无可退：原始的"图片版 PDF"说明要原样保留。"""
        res = self.upload("local")
        self.assertFalse(res.get("ok"))
        self.assertIn("图片版", res.get("error", ""))


class OcrNoQuestions(ScannedCase):
    """OCR 认出了字但拆不出题：同样退回视觉模型，不能给空结果。"""

    @classmethod
    def setUpClass(cls):
        _fake_engine("questions", "今天天气很好，适合晾晒。")
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            _restore_engine()

    def test_falls_back_to_ai_guidance(self):
        res = self.upload("auto")
        self.assertFalse(res.get("ok"))
        self.assertIn("AI", res.get("error", ""))


class OcrModule(unittest.TestCase):
    """ocr_win 模块自身的行为（进程内，不涉及子进程）。"""

    def tearDown(self):
        ocr_win._cache["ready"] = None
        _restore_engine()

    def test_engine_ready_is_cached(self):
        ocr_win._cache["ready"] = "zh-CN"
        self.assertEqual(ocr_win.engine_ready(), "zh-CN")
        ocr_win._cache["ready"] = "zh-Hans-CN"
        self.assertEqual(ocr_win.engine_ready(), "zh-Hans-CN",
                         "第二次调用必须走缓存，不能又去探测")

    def test_missing_script_is_actionable(self):
        ocr_win._cache["ready"] = "zh-CN"
        orig = ocr_win.PS_SCRIPT
        ocr_win.PS_SCRIPT = os.path.join(HERE, "no_such.ps1")
        try:
            with self.assertRaises(ocr_win.OcrError) as ctx:
                ocr_win.ocr_png_bytes(b"\x89PNG\r\n\x1a\n")
        finally:
            ocr_win.PS_SCRIPT = orig
        self.assertIn("找不到", str(ctx.exception))

    def test_despace_cleans_cjk_only(self):
        """OCR 假空格：中文之间去掉；两侧都是半角的保留。"""
        t = ocr_win._despace("机 房 温 度 （ \u3000 ） 答 案 ： B\nA. 10.8V B. 巧 一 25 ℃")
        self.assertIn("答案：B", t)
        self.assertIn("A. 10.8V", t, "半角之间的空格不能被误删")
        self.assertIn("B.巧一25", t)
        self.assertNotIn("机 房", t)

    def test_ocr_output_is_despaced(self):
        """去假空格必须接在 ocr_png_bytes 出口上，不能只留个 `_despace` 函数没人调。

        踩过的坑：函数写了、用例也单测了，但出口没调，线上拿到的还是
        "机 房 温 度"——下游按行首题号拆题，字之间全是空格就只拆得出两题。
        """
        orig_ps = ocr_win.PS_SCRIPT
        ocr_win.PS_SCRIPT = FAKE_PS1
        os.environ["FAKE_OCR_MODE"] = "questions"
        os.environ["FAKE_OCR_TEXT"] = "机 房 温 度 （ ）\nA. 10.8V B. 12V"
        try:
            t = ocr_win.ocr_png_bytes(b"\x89PNG\r\n\x1a\n")
        finally:
            ocr_win.PS_SCRIPT = orig_ps
        self.assertIn("机房温度", t)
        self.assertIn("A. 10.8V", t, "半角之间的空格不能被误删")

    def test_ocr_output_is_despaced(self):
        """去假空格必须在 ocr_png_bytes 里生效，而不是只留一个函数给下游调用。"""
        _fake_engine("questions", "机 房 温 度 （ ） 答 案 ： B")
        orig = ocr_win.PS_SCRIPT
        ocr_win.PS_SCRIPT = FAKE_PS1
        try:
            text = ocr_win.ocr_png_bytes(b"\x89PNG\r\n\x1a\n")
        finally:
            ocr_win.PS_SCRIPT = orig
            _restore_engine()
        self.assertNotIn("机 房", text)
        self.assertIn("答案：B", text)

    def test_ps1_contract(self):
        """ps1 与模块的约定：入口参数、引擎探测、NOOCR 约定、UTF-8 输出。"""
        self.assertTrue(os.path.exists(ocr_win.PS_SCRIPT), "ocr_png.ps1 没随程序发布")
        src = open(ocr_win.PS_SCRIPT, encoding="utf-8").read()
        for token in ("param([string]$Path", "-Check",
                      "TryCreateFromUserProfileLanguages", "RecognizeAsync",
                      "NOOCR", "OutputEncoding"):
            self.assertIn(token, src, "脚本缺少约定要素：%s" % token)


@unittest.skipUnless(ocr_win.engine_ready(), "本机没有可用的 Windows OCR 引擎")
class RealEngineSmoke(unittest.TestCase):
    """真机冒烟：引擎在就真识别一页，验证质量上限（沙箱/无语言包自动跳过）。"""

    def test_real_page_recognizes_something(self):
        import pdftext
        pages = pdftext.page_images(_fixture())
        text = ocr_win.ocr_png_bytes(pages[0]["png"])
        self.assertGreater(len(text), 50, "一页试卷认出这么点字，OCR 通道形同虚设")


if __name__ == "__main__":
    unittest.main()
