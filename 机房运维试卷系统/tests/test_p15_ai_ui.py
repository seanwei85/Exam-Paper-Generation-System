#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P15：识别方式与 AI 配置的界面。

后端能做的事，界面上找不到入口等于没做。这里验收的是"用户能不能自己走完"：
拖入区认不认 PDF 和图片、识别方式能不能切换、没配 AI 时提示去不去得对地方、
设置页能否填地址/密钥/模型并测通——以及密钥在页面上会不会被回显。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-ui-test-987654321wxyz"


class P15AiUi(RenderCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.chrome = Chrome(cls.browser)
        cls.fake = FakeAI().start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.chrome.close()
        finally:
            try:
                cls.fake.stop()
            finally:
                super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.fake.reset()
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)
        # 识别方式是存在 localStorage 里的，同一个浏览器实例跨用例共享：
        # 上一个用例选了「只用本地规则」，下一个用例读到的就是 local 的文案。
        # 每个用例开始先站到同源页面上把它清掉，保证都从默认档开始。
        try:
            self.chrome.goto("http://127.0.0.1:%d/api/ping" % self.port)
            self.chrome.eval("localStorage.removeItem('zjlab_imp_engine')")
        except Exception:
            pass

    def configure_ai(self, **kw):
        payload = {"base_url": self.fake.base_url, "api_key": SECRET,
                   "model": "fake-chat", "enabled": True}
        payload.update(kw)
        self.post_json("/api/ai/config", payload)

    IMPORT_READY = ("!!document.getElementById('impEngineBox') && "
                    "!!document.getElementById('impEngineTip') && "
                    "!!document.getElementById('fileInput')")

    # ---------------------------------------------------------- 导入页

    def test_import_page_offers_three_engines(self):
        """自动 / 只用 AI / 只用本地规则 三个都要给，且默认「自动」。"""
        c = self.nav("import", self.IMPORT_READY)
        labels = c.eval("Array.from(document.querySelectorAll('#impEngineBox button'))"
                        ".map(b=>b.textContent).join('|')")
        self.assertIn("自动", labels)
        self.assertIn("AI", labels)
        self.assertIn("本地", labels)
        on = c.eval("(document.querySelector('#impEngineBox button.on')||{}).textContent||''")
        self.assertIn("自动", on, "默认没落在「自动」上：%r" % on)

    def test_import_accepts_pdf_and_images(self):
        """拖入区必须认 PDF 和图片，否则用户根本选不到这些文件。

        doc / xls 也算在这里：后端 P16 起就支持旧版 Office 解析（FR-IM-18），
        但 accept 漏了这两个扩展名，文件对话框里直接灰掉，等于后端白做。
        """
        c = self.nav("import", self.IMPORT_READY)
        accept = c.eval("document.getElementById('fileInput').getAttribute('accept')")
        for ext in (".pdf", ".png", ".jpg", ".xlsx", ".docx", ".doc", ".xls"):
            self.assertIn(ext, accept, "选择文件时选不到 %s：%s" % (ext, accept))
        tip = c.eval("document.getElementById('dropZone').innerText")
        self.assertIn("pdf", tip.lower(), "拖入区提示里没写 PDF 也能导")
        self.assertIn("doc", tip.lower(), "拖入区提示里没写 doc 也能导")
        self.assertIn("xls", tip.lower(), "拖入区提示里没写 xls 也能导")

    def test_local_engine_tip_says_no_network(self):
        """选了「只用本地规则」要明确说不会联网，这是能不能用的前提。"""
        c = self.nav("import", self.IMPORT_READY)
        c.eval("setEngine('local')")
        c.wait("document.getElementById('impEngineTip').innerText.indexOf('网络')>=0", timeout=15)
        tip = c.eval("document.getElementById('impEngineTip').innerText")
        self.assertIn("不会发出任何网络请求", tip)
        self.assertIn("图片", tip, "要说明图片读不出来，否则用户会白试")

    def test_unconfigured_ai_points_to_settings(self):
        """没配 AI 时要指路到系统设置，而不是干说"不可用"。"""
        c = self.nav("import", self.IMPORT_READY)
        tip = c.eval("document.getElementById('impEngineTip').innerText")
        self.assertIn("AI", tip)
        self.assertIn("系统设置", tip, "没告诉用户去哪配：%s" % tip)

    def test_configured_ai_tip_changes(self):
        """配好之后提示要变，不能一直显示"未配置"。"""
        self.configure_ai()
        c = self.nav("import", self.IMPORT_READY)
        tip = c.eval("document.getElementById('impEngineTip').innerText")
        self.assertNotIn("还没配置", tip, "AI 已经配好了，提示还在说没配置：%s" % tip)

    def test_engine_choice_persists(self):
        """选过一次「只用 AI」，下次进来还是它——不能每次都重选。"""
        self.configure_ai()
        c = self.nav("import", self.IMPORT_READY)
        c.eval("setEngine('ai')")
        c = self.nav("import", self.IMPORT_READY)
        on = c.eval("(document.querySelector('#impEngineBox button.on')||{}).textContent||''")
        self.assertIn("AI", on, "选择没记住：%r" % on)
        c.eval("setEngine('auto')")

    # ---------------------------------------------------------- 设置页

    SETTINGS_READY = "!!document.getElementById('aiBase') && !!document.getElementById('aiKey')"

    def test_settings_has_ai_card(self):
        """设置页要有地址、密钥、模型、测试连通四件套。"""
        c = self.nav("settings", self.SETTINGS_READY)
        for fid in ("aiBase", "aiKey", "aiModel", "aiVision"):
            self.assertTrue(c.eval("!!document.getElementById('%s')" % fid),
                            "缺少输入框 %s" % fid)
        self.assertTrue(c.eval("!!document.getElementById('aiResult')"), "缺少测试结果区")
        text = c.eval("document.body.innerText")
        self.assertIn("AI 识别", text)
        self.assertIn("测试连通", text)

    def test_key_field_is_password_and_not_prefilled(self):
        """密钥框必须是密码框、且不回填——页面 DOM 不能被 copy 出真密钥。"""
        self.configure_ai()
        c = self.nav("settings", self.SETTINGS_READY)
        typ = c.eval("document.getElementById('aiKey').type")
        self.assertEqual(typ, "password", "密钥框不是密码框，会被旁观者看到")
        val = c.eval("document.getElementById('aiKey').value")
        self.assertEqual(val, "", "密钥被回填到页面里了")
        self.assertNotIn(SECRET, c.eval("document.documentElement.outerHTML"),
                         "页面源码里出现了完整密钥")
        self.assertIn(SECRET[-4:], c.eval("document.body.innerText"),
                      "没有露末 4 位，用户无法确认填的是哪把钥匙")

    def test_save_then_test_connection(self):
        """在界面上填地址和密钥 → 保存 → 测试连通，应当一路走通。"""
        c = self.nav("settings", self.SETTINGS_READY)
        c.eval("(function(){document.getElementById('aiBase').value=%s;"
               "document.getElementById('aiKey').value=%s;"
               "document.getElementById('aiModel').value='fake-chat';})()"
               % (json_str(self.fake.base_url), json_str(SECRET)))
        c.eval("saveAiConfig()")
        c.wait("document.getElementById('aiResult') && "
               "(STATE.ai && STATE.ai.ready)", timeout=20)

        self.fake.queue(content="好")
        c.eval("testAi()")
        c.wait("document.getElementById('aiResult').innerText.indexOf('正在测试')<0 "
               "&& document.getElementById('aiResult').innerText.length>0", timeout=25)
        msg = c.eval("document.getElementById('aiResult').innerText")
        self.assertIn("连通", msg, "测试连通没有反馈成功：%s" % msg)
        self.assertNotIn(SECRET, msg, "连通结果里带出了密钥")


def json_str(s):
    import json
    return json.dumps(s, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
