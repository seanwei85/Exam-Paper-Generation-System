#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P26：AI 复核已有答案。

比补答案更微妙：补答案是从零给一个值，错了顶多回到"缺答案"；
复核是**改一个已经能用的值**，判错一次就可能把对的题改坏。

所以这一阶段验收的重点不是"能不能判"，而是：
  * 判「一致」时不许动题目（尤其不许顺手重写解析）；
  * 判「不一致」时必须能被筛出来（AI 判错/存疑是一个筛选项，不是埋在详情里的备注）；
  * 模型自相矛盾时降级为存疑——说"错了"却给出同样的答案，这不是结论。
"""

import json
import os
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-test-p26-abcdefg123456"


def reply(answer="A", analysis="解析", verdict="ok", reason="依据原理", confidence=0.8):
    return {"answer": answer, "analysis": analysis, "verdict": verdict,
            "reason": reason, "confidence": confidence}


class CheckCase(HttpCase):
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
        cfg = {"enabled": True, "base_url": self.fake.base_url, "api_key": SECRET,
               "model": "fake-k3", "timeout": 60}
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def add_q(self, **kw):
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        payload = {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
            "qtype": "single", "diff": "mid",
            "stem": "UPS 电池组的均衡充电周期一般是（　）。",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "A", "analysis": "原解析", "tags": "",
            # 用例里同一题干会建多道题（比不同结论），把查重让过去，免得干扰断言
            "allow_dup": True,
        }
        payload.update(kw)
        r = self.post_json("/api/questions", payload)
        self.assertTrue(r.get("ok"), r)
        return r["id"]

    def question(self, qid):
        return self.get_json("/api/questions/%d" % qid)

    def review(self, qid):
        return self.post_json("/api/ai/review", {"id": qid})


class P26Check(CheckCase):
    def test_ok_verdict_keeps_question(self):
        """判一致：题目一个字都不能变。"""
        qid = self.add_q(answer="A", analysis="原解析")
        self.fake.queue_json(reply(answer="A", analysis="AI 版解析", verdict="ok"))
        r = self.review(qid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["done"][0]["verdict"], "ok")
        q = self.question(qid)
        self.assertEqual(q["answer"], "A")
        self.assertEqual(q["analysis"], "原解析")
        self.assertEqual(q["ai_review"]["state"], "pending")

    def test_wrong_verdict_is_filterable(self):
        """判不一致必须能一键筛出来，否则 500 道题里没法找。"""
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="C", analysis="应当是 C", verdict="wrong"))
        self.review(qid)
        self.assertEqual(self.question(qid)["ai_review"]["verdict"], "wrong")
        r = self.get_json("/api/questions?ai=suspect")
        self.assertEqual(r["total"], 1)
        self.assertEqual(r["items"][0]["id"], qid)

    def test_draft_question_not_in_suspect(self):
        """补答案的建议（filled）不算存疑，不能污染"AI 判错"这个筛选。"""
        qid = self.add_q(answer="")
        self.fake.queue_json(reply(answer="B", verdict="filled"))
        self.review(qid)
        self.assertEqual(self.get_json("/api/questions?ai=suspect")["total"], 0)
        self.assertEqual(self.get_json("/api/questions?ai=pending")["total"], 1)

    def test_adopt_wrong_suggestion_clears_suspect(self):
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="C", verdict="wrong"))
        self.review(qid)
        r = self.post_json("/api/ai/review/adopt", {"ids": [qid], "fields": ["answer"]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.question(qid)["answer"], "C")
        self.assertEqual(self.get_json("/api/questions?ai=suspect")["total"], 0)

    def test_dismiss_clears_suspect(self):
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="C", verdict="wrong"))
        self.review(qid)
        self.post_json("/api/ai/review/dismiss", {"ids": [qid]})
        self.assertEqual(self.get_json("/api/questions?ai=suspect")["total"], 0)
        self.assertEqual(self.question(qid)["answer"], "A")

    def test_check_never_touches_analysis(self):
        """复核时 AI 也给了解析，但用户没点采纳就不能覆盖已有的。"""
        qid = self.add_q(answer="A", analysis="原解析")
        self.fake.queue_json(reply(answer="C", analysis="AI 版解析", verdict="wrong"))
        self.review(qid)
        self.assertEqual(self.question(qid)["analysis"], "原解析")

    def test_wrong_with_same_answer_downgraded(self):
        """说“错了”却给出和原答案一样的答案 —— 自相矛盾，按存疑处理。"""
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="A", verdict="wrong", reason=""))
        self.review(qid)
        rec = self.question(qid)["ai_review"]
        self.assertEqual(rec["verdict"], "unsure")
        self.assertIn("按存疑处理", rec["reason"])

    def test_ok_with_other_answer_downgraded(self):
        """说“一致”却给了另一个答案 —— 同样按存疑处理。"""
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="D", verdict="ok"))
        self.review(qid)
        self.assertEqual(self.question(qid)["ai_review"]["verdict"], "unsure")

    def test_unsure_is_reported_as_is(self):
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="A", verdict="unsure",
                                   reason="题干缺图，无法判断"))
        self.review(qid)
        rec = self.question(qid)["ai_review"]
        self.assertEqual(rec["verdict"], "unsure")
        self.assertIn("缺图", rec["reason"])

    def test_summary_counts_pending(self):
        q1 = self.add_q(answer="A")
        q2 = self.add_q(answer="")
        self.fake.queue_json(reply(answer="B", verdict="wrong"))
        self.fake.queue_json(reply(answer="B", verdict="filled"))
        self.review(q1)
        self.review(q2)
        s = self.get_json("/api/ai/review/summary")
        self.assertEqual(s["pending"], 2)
        self.assertEqual(s["by_verdict"].get("wrong"), 1)
        self.assertEqual(s["by_verdict"].get("filled"), 1)
        self.assertEqual(s["draft"], 1)

    def test_list_row_carries_ai_state(self):
        """列表要能直接标出 AI 结论，不必点进详情才知道。"""
        qid = self.add_q(answer="A")
        self.fake.queue_json(reply(answer="C", verdict="wrong"))
        self.review(qid)
        row = self.get_json("/api/questions")["items"][0]
        self.assertEqual(row["ai"]["verdict"], "wrong")
        self.assertEqual(row["ai"]["kind"], "check")

    def test_bank_page_has_ai_entry(self):
        """入口要在界面上看得见：没有按钮，功能等于不存在。"""
        html = urllib.request.urlopen(self.url("/"), timeout=20).read().decode("utf-8")
        for token in ('id="aiBtn"', 'id="fAi"', "aiReviewPicked", "adoptAi", "aiSuggestHtml"):
            self.assertIn(token, html)


if __name__ == "__main__":
    unittest.main()
