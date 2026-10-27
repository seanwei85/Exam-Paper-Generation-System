#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P25：AI 补答案与解析。

第三轮真实入库之后暴露出最扎手的一件事：库里 300 多道草稿题没有答案，
都是老题库搬过来的（厂商模板导出的面试题库整份没有答案列）。人工一道一道补
不现实，但直接让模型"顺手把答案写进库"更危险——模型会看走眼，而且错答案进了卷子
没人能发现。

所以这一阶段验收的是三件事，缺一件这功能就不该上线：
  1) 建议不能自动落库，必须用户逐题点采纳；
  2) 建议必须能被机器校验（字母落在选项里、判断只能是正确/错误、空数对得上），
     校验不过的连采纳按钮都不给；
  3) 通道没配好时要给人话，不能报 500 让人去猜。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-test-p25-abcdefg123456"


def reply(answer="B", analysis="按 GB 50174，机房温度宜保持 18~27℃。",
          verdict="filled", reason="GB 50174-2017 第 8.1 节", confidence=0.9):
    return {"answer": answer, "analysis": analysis, "verdict": verdict,
            "reason": reason, "confidence": confidence}


class ReviewCase(HttpCase):
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
        self.write_ai()

    def write_ai(self, **kw):
        cfg = {"enabled": True, "base_url": self.fake.base_url, "api_key": SECRET,
               "model": "fake-k3", "timeout": 60, "chunk_chars": 6000}
        cfg.update(kw)
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    # ---------------------------------------------------------- 辅助

    def add_q(self, **kw):
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        payload = {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
            "qtype": "single", "diff": "mid",
            "stem": "机房内的温度一般应控制在（　）。",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "", "analysis": "", "tags": "",
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


class P25FillAnswer(ReviewCase):
    def test_no_ai_config_says_where_to_configure(self):
        """没配 AI 时要指路，不能只回一句失败。"""
        os.remove(os.path.join(self.tmp, "ai.json"))
        qid = self.add_q()
        r = self.review(qid)
        self.assertFalse(r.get("ok"), r)
        self.assertIn("系统设置", r.get("error") or "")

    def test_suggestion_does_not_touch_question(self):
        """只出建议：题目答案在采纳之前必须保持为空。"""
        qid = self.add_q()
        self.fake.queue_json(reply())
        r = self.review(qid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(r["done"]), 1)
        self.assertEqual(r["done"][0]["verdict"], "filled")
        q = self.question(qid)
        self.assertEqual(q["answer"], "", "建议不能自动写进题库")
        self.assertIsNotNone(q["ai_review"], "建议要存下来给界面看")
        self.assertEqual(q["ai_review"]["state"], "pending")
        self.assertEqual(q["ai_review"]["suggest_answer"], "B")

    def test_adopt_writes_answer_and_analysis(self):
        qid = self.add_q()
        self.fake.queue_json(reply(answer="B", analysis="理由如下"))
        self.review(qid)
        r = self.post_json("/api/ai/review/adopt", {"ids": [qid]})
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["adopted"]), 1)
        self.assertTrue(r["adopted"][0]["was_draft"], "原本缺答案，采纳后应从草稿转正")
        q = self.question(qid)
        self.assertEqual(q["answer"], "B")
        self.assertIn("理由如下", q["analysis"])
        self.assertEqual(q["ai_review"]["state"], "adopted")

    def test_adopt_only_answer_keeps_analysis(self):
        """只采纳答案时，解析不能被顺手改掉——用户没点的东西不许动。"""
        qid = self.add_q(analysis="原解析，写得糙但没错")
        self.fake.queue_json(reply(answer="C", analysis="AI 写的新解析"))
        self.review(qid)
        r = self.post_json("/api/ai/review/adopt", {"ids": [qid], "fields": ["answer"]})
        self.assertTrue(r["ok"], r)
        q = self.question(qid)
        self.assertEqual(q["answer"], "C")
        self.assertEqual(q["analysis"], "原解析，写得糙但没错")

    def test_dismiss_leaves_question_untouched(self):
        qid = self.add_q()
        self.fake.queue_json(reply())
        self.review(qid)
        r = self.post_json("/api/ai/review/dismiss", {"ids": [qid]})
        self.assertTrue(r["ok"], r)
        q = self.question(qid)
        self.assertEqual(q["answer"], "")
        self.assertEqual(q["ai_review"]["state"], "dismissed")
        self.assertEqual(self.get_json("/api/questions?ai=pending")["total"], 0)

    def test_answer_out_of_options_is_not_adoptable(self):
        """模型给了个不存在的选项：可以展示，但绝不许采纳。"""
        qid = self.add_q()
        self.fake.queue_json(reply(answer="E"))
        r = self.review(qid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["done"][0]["valid"], 0)
        q = self.question(qid)
        self.assertIn("不在选项范围", q["ai_review"]["problem"])

        r = self.post_json("/api/ai/review/adopt", {"ids": [qid]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["adopted"], [])
        self.assertTrue(r["errors"])
        self.assertIn("不可采纳", r["errors"][0]["error"])
        self.assertEqual(self.question(qid)["answer"], "")

    def test_judge_answer_normalized(self):
        """判断题模型爱答“对/√”，要归一到系统认的“正确”。"""
        qid = self.add_q(qtype="judge", options=[], stem="UPS 电池室应保持通风。")
        self.fake.queue_json(reply(answer="对", analysis="氢气积聚风险"))
        self.review(qid)
        self.assertEqual(self.question(qid)["ai_review"]["suggest_answer"], "正确")

    def test_fill_blank_count_mismatch_is_rejected(self):
        """填空题空数是硬约束：题干两个空只给一个答案，采纳进去就是错题。"""
        qid = self.add_q(qtype="fill", options=[], blanks=2,
                         stem="机房温度宜保持在 ____ ℃，相对湿度宜为 ____%。")
        self.fake.queue_json(reply(answer="18~27"))
        r = self.review(qid)
        self.assertEqual(r["done"][0]["valid"], 0)
        self.assertIn("2 个空", self.question(qid)["ai_review"]["problem"])

    def test_fill_two_blanks_ok(self):
        qid = self.add_q(qtype="fill", options=[], blanks=2,
                         stem="机房温度宜保持在 ____ ℃，相对湿度宜为 ____%。")
        self.fake.queue_json(reply(answer="18~27|40~60"))
        r = self.review(qid)
        self.assertEqual(r["done"][0]["valid"], 1)
        self.assertEqual(self.question(qid)["ai_review"]["suggest_answer"], "18~27|40~60")

    def test_fenced_json_is_accepted(self):
        """模型爱用 ``` 包一层，这种必须能吃下来，否则一半建议都白跑。"""
        qid = self.add_q()
        body = "```json\n" + json.dumps(reply(answer="D"), ensure_ascii=False) + "\n```"
        self.fake.queue(content=body)
        r = self.review(qid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(self.question(qid)["ai_review"]["suggest_answer"], "D")

    def test_adopt_can_be_undone(self):
        """采纳是人工点头，但人工也会点错——必须能退回采纳前的样子。"""
        qid = self.add_q(analysis="原解析")
        self.fake.queue_json(reply(answer="B", analysis="AI 新解析"))
        self.review(qid)
        self.post_json("/api/ai/review/adopt", {"ids": [qid]})
        self.assertEqual(self.question(qid)["answer"], "B")

        r = self.post_json("/api/ai/review/undo", {"ids": [qid]})
        self.assertTrue(r["ok"], r)
        q = self.question(qid)
        self.assertEqual(q["answer"], "", "撤销后要回到采纳前的空答案")
        self.assertEqual(q["analysis"], "原解析")
        self.assertEqual(q["ai_review"]["state"], "pending")

    def test_undo_without_adopt_is_refused(self):
        qid = self.add_q()
        r = self.post_json("/api/ai/review/undo", {"ids": [qid]})
        self.assertEqual(r["restored"], [])
        self.assertTrue(r["errors"])
        self.assertIn("撤销", r["errors"][0]["error"])

    def test_garbage_reply_fails_without_writing(self):
        """模型胡言乱语时要明确失败，绝不能把半截内容当建议存下来。"""
        qid = self.add_q()
        self.fake.queue(content="这道题我判断不了，建议查规范。")
        r = self.review(qid)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["done"], [])
        self.assertEqual(len(r["failed"]), 1)
        self.assertIn("JSON", r["failed"][0]["error"])
        self.assertIsNone(self.question(qid)["ai_review"])


if __name__ == "__main__":
    unittest.main()
