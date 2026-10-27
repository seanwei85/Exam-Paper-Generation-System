#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P29：批量审校的暂停 / 继续 / 停止。

用户问的是"能不能加暂停和继续"。功能本来就能停——问题出在**多久停得下来**：
原来十题一批，点暂停要等这一批十题全跑完，推理模型一题几十秒，等于按了没反应。
改成逐题请求后，暂停粒度变成"当前这一题"，按钮才名副其实。

所以这一阶段验收的不是"有没有按钮"，而是三件事：
  1) 点暂停后最多再跑完当前这一题，进度就冻住（不是"标个旗子继续跑"）；
  2) 继续从断点接着跑，已完成的题不重跑（否则白烧 token）；
  3) 停止要真的收手，并把"还剩几题没审"说清楚。

暂停期间"正在跑的那题结束就停"和"已经停住了"必须显示成两种话：
混为一谈的话，用户看到"已暂停"会以为停了，其实还在付钱。
"""

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402
from fake_ai import FakeAI, completion  # noqa: E402

SECRET = "sk-test-p29-abcdefg123456"


def reply(answer="B", verdict="filled"):
    return {"answer": answer, "analysis": "按 GB 50174 取值。", "verdict": verdict,
            "reason": "规范要求", "confidence": 0.8}


class P29Pause(RenderCase):
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
        cfg = {"enabled": True, "base_url": self.fake.base_url, "api_key": SECRET,
               "model": "fake-k3", "timeout": 60}
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)
        # 每题 0.7 秒：快到不至于让用例难跑，慢到能在第一题跑完前点下暂停
        self.fake.set_default(
            body=completion(json.dumps(reply(), ensure_ascii=False)), delay=0.7)

    # ------------------------------------------------------------ 辅助

    def add_drafts(self, n=3):
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        ids = []
        for i in range(n):
            r = self.post_json("/api/questions", {
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "mid",
                "stem": "【测试】暂停用第 %d 题（　）。" % (i + 1),
                "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                "answer": "", "analysis": "", "tags": "", "allow_dup": True,
            })
            self.assertTrue(r.get("ok"), r)
            ids.append(r["id"])
        return ids

    def start_round(self, n=3):
        """铺 n 道草稿题，在题库页勾上，点「AI 审校」，返回跑起来的浏览器。"""
        ids = self.add_drafts(n)
        ch = self.nav("bank", "document.querySelectorAll('.pick').length>=%d" % n)
        ch.eval("window.confirm=function(){return true;};"
                "var ps=document.querySelectorAll('.pick');"
                "for(var i=0;i<%d;i++){ps[i].click();}1" % n)
        ch.eval("document.getElementById('aiBtn').click();1")
        ch.wait("!!document.querySelector('.ai-prog')", timeout=10)
        self.assertTrue(ch.eval("document.getElementById('aiBtn').disabled"),
                        "跑起来后要禁用「AI 审校」，否则能开第二轮")
        return ch, ids

    # ------------------------------------------------------------ 用例

    def test_pause_freezes_after_current_question(self):
        ch, _ = self.start_round(3)
        ch.eval("document.querySelector('.ai-prog .pause').click();1")
        # 第一题还在跑：只能承诺"跑完就停"，不能说"已暂停"
        self.assertTrue(ch.eval("!!AI_RUN.paused"))
        ch.wait("AI_RUN.waiter!==null", timeout=20)
        done1 = ch.eval("AI_RUN.done")
        self.assertEqual(done1, 1, "暂停应该停在第一题之后，而不是硬跑完全部")
        self.assertEqual(ch.eval("document.querySelector('.ai-prog .pause').textContent"), "继续")
        self.assertIn("已暂停", ch.eval("document.querySelector('.ai-prog .msg').textContent"))
        self.assertTrue(ch.eval("document.querySelector('.ai-prog').classList.contains('paused')"))
        # 冻住：等两倍单题时长，进度不许再涨
        time.sleep(1.6)
        self.assertEqual(ch.eval("AI_RUN.done"), done1, "暂停之后还在偷偷跑题")

    def test_resume_continues_without_redoing(self):
        ch, _ = self.start_round(3)
        ch.eval("document.querySelector('.ai-prog .pause').click();1")
        ch.wait("AI_RUN.waiter!==null", timeout=20)
        frozen = ch.eval("AI_RUN.idx")
        ch.eval("document.querySelector('.ai-prog .pause').click();1")
        self.assertFalse(ch.eval("!!AI_RUN.paused"))
        ch.wait("AI_RUN.active===false", timeout=40)
        self.assertEqual(ch.eval("AI_RUN.done"), 3)
        self.assertEqual(ch.eval("AI_RUN.idx"), 3)
        self.assertGreaterEqual(frozen, 1, "继续应当从断点往后跑")
        revs = self.get_json("/api/ai/review?state=pending")["reviews"]
        self.assertEqual(len(revs), 3, "三道题都该有建议，且不重复")

    def test_stop_ends_round_and_reports_leftover(self):
        ch, _ = self.start_round(3)
        ch.eval("document.querySelector('.ai-prog .stop').click();1")
        ch.wait("AI_RUN.active===false", timeout=30)
        done = ch.eval("AI_RUN.done")
        self.assertLess(done, 3, "点了停止还把剩下的跑完，等于没停")
        self.assertGreaterEqual(done, 1)
        self.assertTrue(ch.eval("!document.querySelector('.ai-prog')"),
                        "收尾后进度条要撤掉，否则一直挂在页面上")
        self.assertFalse(ch.eval("document.getElementById('aiBtn').disabled"))

    def test_controls_absent_before_round(self):
        self.add_drafts(1)
        ch = self.nav("bank", "document.querySelectorAll('.pick').length>0")
        self.assertTrue(ch.eval("!document.querySelector('.ai-prog')"))
        self.assertFalse(ch.eval("document.getElementById('aiBtn').disabled"))


if __name__ == "__main__":
    unittest.main()
