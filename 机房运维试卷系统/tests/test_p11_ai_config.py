#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P11 接入网络 AI：配置与连通性。

密钥只落在本机 data/ai.json 里，界面和接口返回一律脱敏——这是把题库发到外部
模型之前必须先立住的规矩，所以单独立一组用例盯着它。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import BaseCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-test-1234567890abcdefWXYZ"


class P11Config(BaseCase):
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
        import ai_parse
        self.ai = ai_parse
        self.fake.reset()
        # 每个用例从"干净配置"开始，避免上一条的密钥串进来
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)

    # ------------------------------------------------------ 存取与脱敏

    def test_config_saved_in_data_dir(self):
        """配置必须落在本机数据目录，跟数据库放一起，备份/迁移时才不会漏。"""
        self.ai.save_config({"base_url": self.fake.base_url, "api_key": SECRET,
                             "model": "fake-chat", "enabled": True})
        path = os.path.join(self.tmp, "ai.json")
        self.assertTrue(os.path.exists(path), "配置没写到 data 目录")
        raw = json.load(open(path, encoding="utf-8"))
        self.assertEqual(raw["api_key"], SECRET, "密钥没存进去，下次还得重填")

    def test_key_is_masked_in_public_view(self):
        """给界面看的配置里不能出现完整密钥——它会被写进页面 DOM。"""
        self.ai.save_config({"base_url": self.fake.base_url, "api_key": SECRET,
                             "model": "fake-chat"})
        pub = self.ai.public_config()
        self.assertTrue(pub["has_key"])
        self.assertNotIn(SECRET, json.dumps(pub, ensure_ascii=False), "公开配置泄露了完整密钥")
        self.assertTrue(pub["api_key_masked"].endswith(SECRET[-4:]),
                        "应只露出末 4 位，方便确认填的是哪把钥匙")

    def test_empty_key_does_not_wipe_existing(self):
        """界面上密钥是密码框、回显为空，只改模型名时不能把密钥抹掉。"""
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url, "model": "a"})
        self.ai.save_config({"model": "b"})
        cfg = self.ai.load_config()
        self.assertEqual(cfg["api_key"], SECRET, "改模型名把密钥清空了")
        self.assertEqual(cfg["model"], "b")

    def test_clear_key_on_request(self):
        """要清就明确清，不能靠"填空字符串"这种含糊操作。"""
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url})
        self.ai.save_config({"clear_key": True})
        self.assertFalse(self.ai.load_config()["api_key"])
        self.assertFalse(self.ai.public_config()["has_key"])

    def test_ready_only_when_key_and_url(self):
        self.ai.save_config({"clear_key": True})
        self.assertFalse(self.ai.ready(), "没有密钥不该声称可用")
        self.ai.save_config({"api_key": SECRET, "base_url": ""})
        self.assertFalse(self.ai.ready(), "没有接口地址不该声称可用")
        self.ai.save_config({"base_url": self.fake.base_url})
        self.assertTrue(self.ai.ready())

    # ------------------------------------------------------ 连通性

    def test_connection_ok(self):
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat"})
        self.fake.queue(content="好")
        r = self.ai.test_connection()
        self.assertTrue(r.get("ok"), r)
        self.assertIn("model", r)

    def test_connection_sends_bearer_token(self):
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat"})
        self.fake.queue(content="好")
        self.ai.test_connection()
        req = self.fake.last
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer " + SECRET)
        self.assertEqual(req["body"]["model"], "fake-chat")

    def test_connection_surfaces_upstream_message(self):
        """连不上时要把对方给的错误原样带出来，别只说"失败"。"""
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat"})
        self.fake.queue_fail(401, "Invalid API key")
        r = self.ai.test_connection()
        self.assertFalse(r.get("ok"))
        self.assertIn("Invalid API key", r.get("error", ""))

    def test_connection_never_returns_key(self):
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat"})
        self.fake.queue_fail(500, "boom")
        r = self.ai.test_connection()
        self.assertNotIn(SECRET, json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
