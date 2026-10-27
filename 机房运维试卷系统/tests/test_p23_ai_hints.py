#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P23：AI 模型技术要求的提醒位置。

用户问"模型技术要求在哪个界面提醒比较好"。结论：**设置页常驻 + 导入页动态**——
配置时必看要求（选错模型第一时间知道原因），使用时才知道当前用的是谁。

验收点：
  * 设置页 AI 卡片的提示要写清"读扫描件必须多模态视觉模型，纯文本模型看不了图"；
  * 导入页的识别方式提示要显示当前模型名，图片走谁（视觉模型 or 文本模型兜底）
    要说得出来——不然"为什么识别失败"要靠猜；
  * 本地模式不发请求，不该提模型。
"""

import os
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402

SECRET = "sk-test-p23-abcdefg123456"

IMPORT_READY = "!!document.getElementById('impEngineTip')"


class P23Hints(RenderCase):
    """两处提醒要真的出现在页面上，光改文案不看渲染等于没做。"""

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

    def setUp(self):
        super().setUp()
        self.post_json("/api/ai/config", {"base_url": "http://127.0.0.1:1/v1",
                                          "api_key": SECRET, "model": "fake-chat",
                                          "vision_model": "",
                                          "timeout": 300, "enabled": True})

    def tip(self, engine=None):
        """先导航清掉浏览器里跨用例共享的偏好，再按需设引擎并重新加载。"""
        c = self.nav("import", IMPORT_READY)
        c.eval("try{localStorage.clear();}catch(e){}")
        if engine:
            c.eval("localStorage.setItem('zjlab_imp_engine','%s')" % engine)
        c = self.nav("import", IMPORT_READY)
        return str(c.eval("document.getElementById('impEngineTip').textContent") or "")

    # ---------------------------------------------------------- 设置页

    def test_settings_hint_states_vision_requirement(self):
        """设置页要常驻说清：读扫描件必须多模态模型，纯文本模型看不了图。"""
        c = self.nav("settings", "!!document.getElementById('aiTimeout')")
        text = str(c.eval("document.body.textContent") or "")
        self.assertIn("多模态", text, "缺'多模态'要求")
        self.assertIn("视觉模型", text, "缺'视觉模型'填法")
        self.assertIn("看不了图", text, "要说明纯文本模型的后果")

    # ---------------------------------------------------------- 导入页

    def test_ai_tip_shows_model_and_vision_fallback(self):
        """只用 AI：要报当前模型名；没配视觉模型时要说明图片用文本模型兜底。"""
        tip = self.tip("ai")
        self.assertIn("fake-chat", tip, tip)
        self.assertIn("视觉模型", tip, tip)
        self.assertIn("看图", tip, tip)

    def test_ai_tip_shows_vision_model_when_set(self):
        """配了视觉模型：图片走谁要说名道姓。"""
        self.post_json("/api/ai/config", {"base_url": "http://127.0.0.1:1/v1",
                                          "api_key": SECRET, "model": "fake-chat",
                                          "vision_model": "qwen-vl-test",
                                          "timeout": 300, "enabled": True})
        tip = self.tip("ai")
        self.assertIn("qwen-vl-test", tip, tip)

    def test_auto_tip_also_names_model(self):
        """自动档兜底时也会用到 AI，模型名一样要亮出来。"""
        tip = self.tip("auto")
        self.assertIn("fake-chat", tip, tip)

    def test_local_tip_does_not_mention_model(self):
        """本地模式一个请求都不发，不该拿模型来误导。"""
        tip = self.tip("local")
        self.assertNotIn("fake-chat", tip, tip)

    def test_unconfigured_import_tip_keeps_guidance(self):
        """AI 没配时维持原引导：指向系统设置。"""
        self.post_json("/api/ai/config", {"enabled": False, "clear_key": True})
        os.remove(os.path.join(self.tmp, "ai.json"))
        tip = self.tip("ai")
        self.assertIn("系统设置", tip, tip)
        self.assertNotIn("fake-chat", tip, tip)


if __name__ == "__main__":
    unittest.main(verbosity=2)
