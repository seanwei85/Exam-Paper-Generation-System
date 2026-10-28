#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P31：视觉能力探测与图片型 PDF 双路兜底（FR-IM-29）。

动机：图片型 PDF 此前的链路是"Windows OCR 认字 → 本地规则硬拆"，实测 12 题只能
拆出 7 题（题号认成 I./生、选项字母认成小写、换行错位）；而同样的 OCR 文字交给
模型整理是 12 题全对，直接把页面图给有视觉的模型也是 12 题全对。于是路由改成：

    视觉探测（一次，缓存）→ 有视觉：直接看图（方式2，成功率优先）
                          → 无视觉/失败：OCR 认字 → AI 整理（方式1）
                          → 没配 AI：OCR → 本地规则（P30 现状兜底）

探测为什么不能看模型名猜：DeepSeek-V4-Flash 与 V4.1-Flash 只差一个点号，前者
纯文本后者带视觉；名字里没有任何"视觉"字样的 Kimi-K3 反而能看图。所以只能发
一张写着 AB12 的小图、检查回答里有没有 AB12——注意 DeepSeek-V4-Flash 收图后
返回 HTTP 200 但内容是"请上传图片"（图被静默丢弃），只看状态码会上当。

进度可见（用户要求）：探测、OCR 认字、AI 整理各阶段都写进进度通道；并按已
完成的页耗时给出"预计剩余时间"，随实际速度动态更新。
"""

import base64
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import ai_parse  # noqa: E402
import server as core  # noqa: E402
from fake_ai import FakeAI  # noqa: E402
from httpbase import HttpCase  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "image_paper.pdf")
FAKE_PS1 = os.path.join(HERE, "fixtures", "fake_ocr.ps1")

SECRET = "sk-test-p31-abcdefg123456"

#: 版式规整的 OCR 文本（真机形态），方式1 用它喂 AI
OCR_TEXT = """1、机房 UPS 蓄电池放电终止电压一般不低于（　）。
A、10.8V
B、12V
C、14V
答案：A"""

ONE = {"qtype": "single", "cat": "电气", "diff": "易",
       "stem": "机房温度一般应保持在（　）。",
       "options": [{"key": "A", "text": "10 - 20℃"},
                   {"key": "B", "text": "15 - 25℃"},
                   {"key": "C", "text": "20 - 30℃"}],
       "answer": "B", "analysis": ""}


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


class ProbeUnit(unittest.TestCase):
    """进程内：探测函数的判定标准与缓存语义。"""

    def setUp(self):
        self.fake = FakeAI().start()
        self._old_dir = core.DATA_DIR
        core.DATA_DIR = self._tmp = tempfile.mkdtemp(prefix="p31_probe_")
        self._old_backoff = ai_parse.RETRY_BACKOFF
        ai_parse.RETRY_BACKOFF = 0
        ai_parse.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                              "model": "fake-chat", "vision_model": "fake-vl"})

    def tearDown(self):
        self.fake.stop()
        core.DATA_DIR = self._old_dir
        ai_parse.RETRY_BACKOFF = self._old_backoff
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _cfg(self):
        return ai_parse.load_config()

    def test_probe_png_is_valid_image(self):
        png = ai_parse._probe_png_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertGreater(len(png), 200, "小图太小不像话，大图浪费 token")

    def test_probe_true_is_cached(self):
        self.fake.set_default(content="图片里写的是 AB12。")
        self.assertTrue(ai_parse.probe_vision())
        cfg = self._cfg()
        self.assertTrue(cfg.get("vision_ok"))
        self.assertEqual(cfg.get("vision_checked"), "fake-vl",
                         "缓存要记在模型名上，换模型自动失效")
        self.assertEqual(self.fake.count(), 1)

    def test_probe_refusal_is_false(self):
        """纯文本模型的典型回答：礼貌拒绝——必须判成"无视觉"并缓存。"""
        self.fake.set_default(content="抱歉，我无法处理图片，请直接把文字粘贴给我。")
        self.assertFalse(ai_parse.probe_vision())
        self.assertFalse(self._cfg().get("vision_ok"))

    def test_probe_hallucination_is_false(self):
        """最险的一种：看不见图还瞎编内容。没抄中 AB12 就是无视觉。"""
        self.fake.set_default(content="图片上写着：您好")
        self.assertFalse(ai_parse.probe_vision())

    def test_probe_network_error_not_cached(self):
        self.fake.queue_fail(500, "boom")
        self.assertIsNone(ai_parse.probe_vision(), "网络失败不能当成无视觉")
        self.assertIsNone(self._cfg().get("vision_ok"), "失败结果不许写进缓存")
        self.assertEqual(self._cfg().get("vision_checked") or "", "", "判定对象也不许残留")

    def test_verdict_reuses_cache_without_request(self):
        self.fake.set_default(content="AB12")
        ai_parse.probe_vision()
        self.assertEqual(self.fake.count(), 1)
        self.assertTrue(ai_parse.vision_verdict())
        self.assertEqual(self.fake.count(), 1, "缓存有效期内不许再发请求")

    def test_model_change_invalidates_cache(self):
        self.fake.set_default(content="AB12")
        ai_parse.probe_vision()
        ai_parse.save_config({"vision_model": "fake-vl2"})
        self.assertIsNone(ai_parse.vision_verdict(), "换了模型必须重新探测")
        ai_parse.probe_vision()
        self.assertEqual(self.fake.count(), 2)


class ProgressEta(unittest.TestCase):
    """进度通道：按已完成页的实际耗时估算剩余时间。"""

    def setUp(self):
        core.PROGRESS.clear()

    def tearDown(self):
        core.PROGRESS.clear()

    def test_eta_from_page_timings(self):
        core.progress_start("j")
        core.PROGRESS["j"]["_page_ts"] = time.time() - 10     # 第 1 页跑了 10 秒
        core.progress_update("j", stage="视觉模型识别 第 2/3 页", page=2, pages=3)
        d = core.progress_get("j")
        self.assertEqual(d.get("page_secs_count"), 1)
        self.assertTrue(15 <= d.get("eta_sec", 0) <= 25,
                        "均速 10 秒/页 × 剩 2 页 = 约 20 秒，实测 %s" % d.get("eta_sec"))
        self.assertGreaterEqual(d.get("elapsed", 0), 0)

    def test_reset_eta_clears_history(self):
        """换路（视觉失败改走 OCR）后旧页耗时不再代表新阶段的速度。"""
        core.progress_start("j")
        core.PROGRESS["j"]["_page_ts"] = time.time() - 99
        core.progress_update("j", reset_eta=True, stage="本地 OCR 认字 第 1/2 页",
                             page=1, pages=2)
        d = core.progress_get("j")
        self.assertEqual(d.get("page_secs_count"), 0)
        self.assertIsNone(d.get("eta_sec"), "没有样本就不许瞎估")


class ImportRoute(HttpCase):
    """接口级：探测→路由→兜底，全程走真实 HTTP + 假模型 + 假 OCR。"""

    @classmethod
    def setUpClass(cls):
        _fake_engine("questions", OCR_TEXT)
        super().setUpClass()
        cls.fake = FakeAI().start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.fake.stop()
        finally:
            super().tearDownClass()
            _restore_engine()

    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.ai = ai_parse
        self.ai.RETRY_BACKOFF = 0
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat", "vision_model": "fake-vl"})
        self.cats = self.core.categories_tree(self.core.connect())

    def upload(self, engine=None, filename="机房运维试题及答案.pdf"):
        payload = {"filename": filename,
                   "content": base64.b64encode(_fixture()).decode("ascii")}
        if engine:
            payload["engine"] = engine
        return self.post_json("/api/import/parse", payload, timeout=120)

    def _image_requests(self):
        out = []
        for r in self.fake.requests:
            parts = (r["body"].get("messages") or [{}])[-1].get("content")
            if isinstance(parts, list) and any(p.get("type") == "image_url" for p in parts):
                out.append(r)
        return out

    def test_vision_first_when_supported(self):
        """确认有视觉 → 直接把页面图给模型，一次探测之后不再重复。"""
        self.fake.queue(content="AB12")
        self.fake.queue_json([ONE])
        self.fake.queue_json([ONE])
        res = self.upload("auto")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "ai-vision", "有视觉就走方式2（成功率优先）")
        self.assertEqual(self.fake.count(), 3, "1 次探测 + 2 页")
        # 第二次导入：缓存生效，不再探测
        self.fake.queue_json([ONE])
        self.fake.queue_json([ONE])
        res = self.upload("auto")
        self.assertEqual(res["by"], "ai-vision")
        self.assertEqual(self.fake.count(), 5, "缓存生效：第二次只发 2 页")

    def test_no_vision_uses_ocr_plus_ai(self):
        """确认无视觉 → OCR 认字后交给 AI 整理（不再走本地规则硬拆）。"""
        self.ai.save_config({"vision_ok": False, "vision_checked": "fake-vl"})
        self.fake.set_default(content=json.dumps([ONE], ensure_ascii=False))
        res = self.upload("auto")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr+ai")
        self.assertGreaterEqual(len(res["items"]), 1)
        self.assertEqual(len(self._image_requests()), 0,
                         "无视觉的模型不该收到任何图片")
        self.assertTrue(self.fake.count() >= 1, "OCR 文字必须交给 AI 整理")

    def test_vision_failure_falls_back_to_ocr_ai(self):
        """方式2 半路失败 → 自动降级方式1，用户不用重试。"""
        self.fake.queue(content="AB12")            # 探测通过
        self.fake.queue_fail(400, "image too large")
        self.fake.queue_fail(400, "image too large")
        self.fake.queue(content=json.dumps([ONE], ensure_ascii=False))
        res = self.upload("auto")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr+ai", "视觉失败要落到 OCR+AI")
        self.assertGreaterEqual(len(res["items"]), 1)

    def test_local_engine_never_calls_ai(self):
        """「只用本地规则」一_次网络请求都不发（密钥配了也不许发）。"""
        self.ai.save_config({"vision_ok": False, "vision_checked": "fake-vl"})
        res = self.upload("local")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["by"], "local-ocr")
        self.assertEqual(self.fake.count(), 0)

    def test_settings_probe_endpoint(self):
        """设置页的"检测看图能力"按钮：探测结果写缓存并回显。"""
        self.fake.set_default(content="AB12")
        r = self.post_json("/api/ai/vision-probe", {}, timeout=60)
        self.assertTrue(r.get("ok"), r)
        self.assertTrue(r.get("vision_ok"))
        self.assertEqual((r.get("config") or {}).get("vision_checked"), "fake-vl")
        cfg = self.ai.load_config()
        self.assertTrue(cfg.get("vision_ok"))

    def test_settings_probe_refusal_reports_false(self):
        self.fake.set_default(content="抱歉，我无法处理图片")
        r = self.post_json("/api/ai/vision-probe", {}, timeout=60)
        self.assertTrue(r.get("ok"))
        self.assertFalse(r.get("vision_ok"))


class UiContract(unittest.TestCase):
    """界面契约：检测状态、重新检测按钮、ETA 文案都要在页面上有位置。"""

    def test_ui_has_probe_and_eta(self):
        html = open(os.path.join(os.path.dirname(HERE), "ui.html"),
                    encoding="utf-8").read()
        self.assertIn("/api/ai/vision-probe", html)
        self.assertIn("重新检测", html)
        self.assertIn("预计剩余", html, "进度里要有 ETA 文案")
        self.assertIn("不支持看图", html, "探测结果的两种状态都要能显示")


if __name__ == "__main__":
    unittest.main()
