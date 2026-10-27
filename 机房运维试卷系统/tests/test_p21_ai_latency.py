#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P21：AI 通道收口——超时可配 + 逐页进度。

真实文件验收第二轮暴露的两件事，都不是"能不能识别"的问题，而是**用户等不等得到**：

  * 推理模型（Kimi-K3 这类）识别一页真实卷子要 200 秒以上，而软件里写死 120 秒，
    整份必然全超时，报错还是"模型可能不支持看图"——把用户引到完全错误的方向；
  * 图片型 PDF 逐页送模型，一页三五分钟，界面上只有一句"正在让 AI 识别…"，
    看起来就是卡死，用户只会去点关闭。

所以这一阶段验收的是：超时能在界面上改、默认值够推理模型用；解析过程中能查到
"第几页/共几页"；解析结束后进度要收干净，不能留下永远 running 的僵尸任务。
"""

import base64
import json
import os
import sys
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402
from fake_ai import FakeAI  # noqa: E402
import ai_parse  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "image_paper.pdf")
SECRET = "sk-test-p21-abcdefg123456"

QUESTION = {"qtype": "single", "cat": "电气", "diff": "易",
            "stem": "机房温度一般应保持在（　）。",
            "options": [{"key": "A", "text": "10-20℃"}, {"key": "B", "text": "15-25℃"}],
            "answer": "B", "analysis": ""}


class P21TimeConfig(HttpCase):
    """超时：默认值要够推理模型用，界面上要能改。"""

    def setUp(self):
        super().setUp()
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)

    def test_default_timeout_fits_reasoning_model(self):
        """默认超时要够推理模型用：一页真实卷子实测 >200 秒，120 秒必然全超。"""
        cfg = self.get_json("/api/ai/config")["config"]
        self.assertGreaterEqual(cfg["timeout"], 300, cfg)

    def test_timeout_saved_and_read_back(self):
        r = self.post_json("/api/ai/config", {"base_url": "http://127.0.0.1:1/v1",
                                             "api_key": SECRET, "model": "kimi-k3",
                                             "timeout": 480, "enabled": True})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["config"]["timeout"], 480)
        self.assertEqual(self.get_json("/api/ai/config")["config"]["timeout"], 480)

    def test_bad_timeout_falls_back_to_default(self):
        """填了非法值不能崩，也不能变成 0（0 秒等于一个请求都发不出去）。"""
        for bad in ("abc", 0, -5, None):
            self.post_json("/api/ai/config", {"base_url": "http://127.0.0.1:1/v1",
                                             "api_key": SECRET, "timeout": bad})
            cfg = self.get_json("/api/ai/config")["config"]
            self.assertGreaterEqual(cfg["timeout"], 60, (bad, cfg))

    def test_settings_page_has_timeout_field(self):
        """超时必须在界面上能改——只在 json 里能改，等于用户改不了。"""
        html = urllib.request.urlopen(self.url("/"), timeout=20).read().decode("utf-8")
        self.assertIn('id="aiTimeout"', html)
        self.assertIn("timeout", html)


class P21Progress(HttpCase):
    """进度：逐页送模型的过程中要能查到"第几页/共几页"，结束后收干净。"""

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
        self.post_json("/api/ai/config", {"base_url": self.fake.base_url,
                                          "api_key": SECRET, "model": "fake-chat",
                                          "timeout": 120, "enabled": True})

    # ---------------------------------------------------------- 辅助

    def progress(self, job):
        return self.get_json("/api/import/progress?job=" + job)

    def wait_for(self, job, pred, limit=25.0):
        started = time.time()
        seen = None
        while time.time() - started < limit:
            seen = self.progress(job)
            if pred(seen):
                return seen
            time.sleep(0.1)
        return seen

    # ---------------------------------------------------------- 用例

    def test_unknown_job_is_idle(self):
        """查一个不存在的任务要老实说 idle，不能让页面倒着猜。"""
        p = self.progress("nope-xyz")
        self.assertEqual(p["state"], "idle", p)

    def test_vision_pages_reported_while_running(self):
        """图片型 PDF 逐页送模型时，进度要报出页码（2 页的夹具，应看到 1/2）。"""
        with open(FIXTURE, "rb") as fh:
            raw = fh.read()
        self.fake.set_default(content=json.dumps([QUESTION], ensure_ascii=False),
                              delay=1.2)
        job = "job-pages-1"
        payload = {"filename": "image_paper.pdf", "job": job,
                   "content": base64.b64encode(raw).decode("ascii"), "engine": "auto"}
        box = {}

        def run():
            box["res"] = self.post_json("/api/import/parse", payload, timeout=90)

        th = threading.Thread(target=run)
        th.start()
        seen = self.wait_for(job, lambda p: p.get("pages", 0) >= 2 and p.get("page", 0) >= 1)
        th.join(90)
        self.assertTrue(box.get("res", {}).get("ok"), box.get("res"))
        self.assertEqual(seen["state"], "running", seen)
        self.assertEqual(seen["pages"], 2, seen)
        self.assertGreaterEqual(seen["page"], 1, seen)
        self.assertIn("页", seen["stage"], seen)

    def test_progress_cleared_after_finish(self):
        """解析结束要把任务收掉，否则界面会一直显示"正在识别"。"""
        with open(FIXTURE, "rb") as fh:
            raw = fh.read()
        self.fake.set_default(content=json.dumps([QUESTION], ensure_ascii=False), delay=0)
        job = "job-done-1"
        res = self.post_json("/api/import/parse", {
            "filename": "image_paper.pdf", "job": job,
            "content": base64.b64encode(raw).decode("ascii"), "engine": "auto"}, timeout=90)
        self.assertTrue(res.get("ok"), res)
        p = self.progress(job)
        self.assertNotEqual(p["state"], "running", p)
        self.assertIn(p["state"], ("done", "idle"), p)

    def test_failed_parse_also_clears_progress(self):
        """失败也要收尾：报错后进度条卡在 running 比不显示更糟。"""
        with open(FIXTURE, "rb") as fh:
            raw = fh.read()
        self.fake.set_default(content="这不是 JSON", delay=0)
        job = "job-fail-1"
        res = self.post_json("/api/import/parse", {
            "filename": "image_paper.pdf", "job": job,
            "content": base64.b64encode(raw).decode("ascii"), "engine": "ai"}, timeout=90)
        self.assertFalse(res.get("ok"), res)
        p = self.progress(job)
        self.assertNotEqual(p["state"], "running", p)

    def test_progress_without_job_is_harmless(self):
        """不带 job 的老请求（脚本、老页面）不能因为进度机制报错。"""
        res = self.post_json("/api/import/parse", {
            "filename": "a.txt", "content": base64.b64encode(
                "一、单选题\n1. 机房温度应保持在（ ）\nA. 10℃  B. 20℃\n答案：B\n".encode("utf-8")
            ).decode("ascii"), "engine": "local"})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["items"][0]["answer"], "B")

    def test_ui_polls_progress(self):
        """页面要真的去轮询，光有接口没人看等于没做。"""
        html = urllib.request.urlopen(self.url("/"), timeout=20).read().decode("utf-8")
        self.assertIn("/api/import/progress", html)

    # ---------------------------------------------------------- 报错指向

    def test_timeout_error_points_to_timeout_setting(self):
        """超时的报错要指向「调大超时」，不能一句"可能不支持看图"把人推去换模型。"""
        hint = ai_parse.vision_error_hint("图片识别失败：调用失败：timed out")
        self.assertIn("超时", hint, hint)
        self.assertNotIn("不支持看图", hint, hint)

    def test_vision_error_still_points_to_vision_model(self):
        """真·看不了图的时候，还是得指向「视觉模型」——两种错不能混成一句。"""
        hint = ai_parse.vision_error_hint("图片识别失败：模型没有返回内容")
        self.assertIn("视觉模型", hint, hint)
        self.assertNotIn("超时", hint, hint)

    def test_timeout_hint_reaches_user(self):
        """分岔逻辑要真的接在视觉通道的报错上，不能只是有个函数没人用。"""
        src = open(os.path.join(os.path.dirname(HERE), "ai_parse.py"), encoding="utf-8").read()
        self.assertIn("vision_error_hint(first)", src)


class P21SettingsUi(RenderCase):
    """设置页要真的能改超时：只改 json 里的默认值，用户还是改不了。"""

    SETTINGS_READY = "!!document.getElementById('aiTimeout')"

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

    def test_settings_shows_and_saves_timeout(self):
        self.post_json("/api/ai/config", {"base_url": "http://127.0.0.1:1/v1",
                                          "api_key": SECRET, "model": "fake-chat",
                                          "timeout": 480, "enabled": True})
        c = self.nav("settings", self.SETTINGS_READY)
        shown = str(c.eval("document.getElementById('aiTimeout').value") or "").strip()
        self.assertEqual(shown, "480", "回填的是已存的超时值")
        c.eval("document.getElementById('aiTimeout').value='600'")
        c.eval("saveAiConfig()")
        c.wait("document.getElementById('toast').textContent.indexOf('已保存')>=0", timeout=15)
        self.assertEqual(self.get_json("/api/ai/config")["config"]["timeout"], 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
