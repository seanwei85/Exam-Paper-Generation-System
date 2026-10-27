#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P14：图片/拍照走视觉模型，以及「识别方式」三档（自动 / 只用 AI / 只用本地规则）。

用户的真实痛点是"手上已有的一堆卷子格式不统一"，所以这里的验收重点不是
"AI 能不能用"，而是**什么时候该用、什么时候不该用**：

  * 标准格式的文本，本地规则已经够了，别为了它去花 token；
  * 规则拆不出来的（版式乱、没有题号、整页图片），自动转 AI，不要逼用户去手工重排；
  * 用户选了"只用本地规则"，就一次请求都不能发出去（有些内容不能出网）；
  * 图片没有文字层，只能走视觉模型，没配 Key 时必须说清楚去哪配，而不是报"格式不支持"。
"""

import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

#: 1x1 透明 PNG。测协议形状不需要真图像，内容由假模型应答决定。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

SECRET = "sk-test-p14-abcdefg123456"

QUESTION = {"qtype": "single", "cat": "电气", "diff": "易",
            "stem": "柴油发电机启动后应先空载暖机再带负荷，这种说法（　）。",
            "options": [{"key": "A", "text": "正确"}, {"key": "B", "text": "错误"}],
            "answer": "A", "analysis": ""}

#: 版式散乱、没有题号的一页，专门用来逼出"本地拆不出 → 自动转 AI"
MESSY = """
机房运维岗前培训复习要点
发电机启动以后不要马上带负荷，要先空载跑一会儿，等水温油温上来再说，否则缸体磨损很快
冷冻水的补水一般按蒸发量的百分之一估算，超过这个数就要查是不是哪里漏了
UPS 电池组不建议在满载下长时间放电，会明显缩短寿命
"""


class P14AiImport(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeAI().start()

    @classmethod
    def tearDownClass(cls):
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

    # ---------------------------------------------------------- 辅助

    def configure(self, **kw):
        payload = {"base_url": self.fake.base_url, "api_key": SECRET,
                   "model": "fake-chat", "enabled": True}
        payload.update(kw)
        r = self.post_json("/api/ai/config", payload)
        self.assertTrue(r.get("ok"), r)

    def clear_ai(self):
        self.post_json("/api/ai/config", {"clear_key": True, "base_url": "",
                                          "model": "", "enabled": False})

    def upload(self, raw, filename, engine=None):
        payload = {"filename": filename,
                   "content": base64.b64encode(raw).decode("ascii")}
        if engine:
            payload["engine"] = engine
        return self.post_json("/api/import/parse", payload, timeout=60)

    def ai_requests(self):
        return [r for r in self.fake.requests if r["body"].get("messages")]

    # ---------------------------------------------------------- 图片

    def test_image_without_ai_config_is_actionable(self):
        """没配 AI 就传图片：要说清是没配 Key、去哪配，不能报"格式不支持"。"""
        self.clear_ai()
        r = self.upload(PNG, "拍照.png")
        self.assertFalse(r.get("ok"))
        msg = r.get("error") or ""
        self.assertIn("AI", msg.upper(), "提示里要出现 AI，让用户知道要开哪项")
        self.assertTrue("配置" in msg or "设置" in msg or "密钥" in msg,
                        "要指明下一步去哪配，实际：%s" % msg)

    def test_image_goes_to_vision_model(self):
        """图片必须以 data URL 塞进 image_url，走多模态消息。"""
        self.configure()
        self.fake.queue_json([QUESTION])
        r = self.upload(PNG, "拍照.png")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(len(r["items"]), 1)
        self.assertEqual(r["items"][0]["qtype"], "single")

        msgs = self.ai_requests()[-1]["body"]["messages"]
        user = [m for m in msgs if m["role"] == "user"][-1]
        parts = user["content"] if isinstance(user["content"], list) else []
        urls = [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
        self.assertEqual(len(urls), 1, "没把图片发过去：%s" % parts)
        self.assertTrue(urls[0].startswith("data:image/png;base64,"),
                        "图片格式没带对，模型会当二进制乱码：%s" % urls[0][:40])

    def test_image_uses_vision_model_when_configured(self):
        """配了专门的视觉模型就用它，不要拿纯文本模型硬顶上。"""
        self.configure(vision_model="fake-vl")
        self.fake.queue_json([QUESTION])
        self.upload(PNG, "拍照.jpg")
        self.assertEqual(self.ai_requests()[-1]["body"]["model"], "fake-vl")

    def test_image_reports_engine(self):
        """返回里要标明是哪条通道出的结果，方便用户判断要不要逐条核对。"""
        self.configure()
        self.fake.queue_json([QUESTION])
        r = self.upload(PNG, "拍照.png")
        self.assertIn("ai", (r.get("by") or "").lower(),
                      "没说明用了 AI 识别：%s" % r.get("by"))

    def test_unsupported_image_type_says_convert(self):
        """不支持的类型要告诉用户转成什么，而不是一句"失败"。"""
        self.configure()
        r = self.upload(b"BM" + b"\x00" * 200, "截图.bmp")
        self.assertFalse(r.get("ok"))
        self.assertIn("png", (r.get("error") or "").lower())
        self.assertEqual(len(self.ai_requests()), 0, "不支持的格式不该白发一次请求")

    def test_vision_failure_keeps_upstream_detail(self):
        """模型报错要把原文带出来，别只剩"识别失败"。"""
        self.configure()
        self.fake.queue_fail(400, "image too large")
        r = self.upload(PNG, "拍照.png")
        self.assertFalse(r.get("ok"))
        self.assertIn("image too large", r.get("error") or "")

    # ---------------------------------------------------------- 识别方式

    def test_local_engine_never_calls_ai(self):
        """选「只用本地规则」就一次请求都不能发（有内容不能出网的场合）。"""
        self.configure()
        self.fake.queue(content="[]")
        r = self.upload("1. 柴油发电机启动前应检查机油液位。\n答案：正确\n".encode("utf-8"),
                        "题.txt", engine="local")
        self.assertEqual(len(self.ai_requests()), 0, "选了本地规则却还是发了 AI 请求")
        self.assertTrue(r.get("ok"), r.get("error"))

    def test_ai_engine_forces_ai_for_text(self):
        """选「只用 AI」时，哪怕是标准 txt 也交给模型。"""
        self.configure()
        self.fake.queue_json([QUESTION])
        r = self.upload("1. 随便一段文字\n答案：正确\n".encode("utf-8"),
                        "题.txt", engine="ai")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(len(self.ai_requests()), 1)

    def test_auto_does_not_call_ai_when_rules_work(self):
        """标准格式本地就能拆，别花 token。"""
        self.configure()
        text = ("一、单项选择题\n"
                "1. 柴油发电机启动后应先空载暖机，这种说法（　）。\n"
                "A. 正确\nB. 错误\n答案：A\n"
                "2. 冷冻水补水按蒸发量的多少估算？\n"
                "A. 0.5%\nB. 1%\n答案：B\n").encode("utf-8")
        r = self.upload(text, "题.txt")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(len(self.ai_requests()), 0,
                         "本地规则已经拆出来了还发请求，白花钱")

    def test_auto_falls_back_to_ai_when_rules_fail(self):
        """版式散乱、没有题号时自动转 AI，别让用户回去手工重排。"""
        self.configure()
        self.fake.queue_json([QUESTION, QUESTION])
        r = self.upload(MESSY.encode("utf-8"), "培训要点.txt")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(len(r["items"]), 2)
        self.assertEqual(len(self.ai_requests()), 1, "没触发 AI 兜底")
        self.assertIn("ai", (r.get("by") or "").lower())

    def test_auto_fallback_needs_ai_configured(self):
        """没配 AI 时，散乱文本要老实报拆不出并给出办法，不能假装成功。"""
        self.clear_ai()
        r = self.upload(MESSY.encode("utf-8"), "培训要点.txt")
        self.assertFalse(r.get("ok"))
        self.assertTrue(r.get("error"))

    def test_response_never_leaks_key(self):
        self.configure()
        self.fake.queue_json([QUESTION])
        r = self.upload(PNG, "拍照.png")
        self.assertNotIn(SECRET, str(r))


if __name__ == "__main__":
    unittest.main(verbosity=2)
