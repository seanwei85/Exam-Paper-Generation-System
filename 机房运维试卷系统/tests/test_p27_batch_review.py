#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P27：批量审校。

单题好用不代表批量好用。批量跑最怕三件事：
  * 一题卡住把整批拖死（所以一题一个请求，失败只丢一题）；
  * 界面上什么都看不到，用户以为死机了（所以沿用导入那套进度上报）；
  * 一口气跑 300 题，跑到一半断网，前面全白跑（所以有硬上限、分批提交）。

另外一条容易被忽略的：题删了，挂在它上面的建议必须跟着走，
否则"待处理建议"会永久多出一条点不开的僵尸。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from common import BaseCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-test-p27-abcdefg123456"


def reply(answer="B", verdict="filled"):
    return {"answer": answer, "analysis": "解析", "verdict": verdict,
            "reason": "原理", "confidence": 0.7}


class P27Batch(HttpCase):
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
               "model": "fake-k3", "timeout": 60}
        cfg.update(kw)
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def add_q(self, n=1, answer=""):
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        ids = []
        for i in range(n):
            r = self.post_json("/api/questions", {
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "mid",
                "stem": "第 %d 道批量审校测试题的题干（　）。" % (i + 1),
                "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                "answer": answer, "analysis": "", "tags": "", "allow_dup": True,
            })
            self.assertTrue(r.get("ok"), r)
            ids.append(r["id"])
        return ids

    def test_batch_reviews_every_question(self):
        ids = self.add_q(3)
        for _ in ids:
            self.fake.queue_json(reply())
        r = self.post_json("/api/ai/review", {"ids": ids})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(r["done"]), 3)
        self.assertEqual(len(r["failed"]), 0)
        for qid in ids:
            rec = self.get_json("/api/questions/%d" % qid)["ai_review"]
            self.assertIsNotNone(rec)
            self.assertEqual(rec["state"], "pending")

    def test_one_bad_reply_does_not_kill_batch(self):
        """第 2 题模型乱答，第 1、3 题必须照常出结果。"""
        ids = self.add_q(3)
        self.fake.queue_json(reply())
        self.fake.queue(content="这题我看不懂")
        self.fake.queue_json(reply())
        r = self.post_json("/api/ai/review", {"ids": ids})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(len(r["done"]), 2)
        self.assertEqual([x["id"] for x in r["failed"]], [ids[1]])
        self.assertIsNone(self.get_json("/api/questions/%d" % ids[1])["ai_review"])
        self.assertIsNotNone(self.get_json("/api/questions/%d" % ids[2])["ai_review"])

    def test_over_limit_is_refused(self):
        r = self.post_json("/api/ai/review", {"ids": list(range(1, 62))})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("60", r.get("error") or "")

    def test_empty_ids_is_refused(self):
        r = self.post_json("/api/ai/review", {"ids": []})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("没有选中", r.get("error") or "")

    def test_progress_finishes_cleanly(self):
        """跑完要收尾，不能留一个永远 running 的任务让界面一直转圈。"""
        ids = self.add_q(2)
        for _ in ids:
            self.fake.queue_json(reply())
        r = self.post_json("/api/ai/review", {"ids": ids, "job": "p27job"})
        self.assertTrue(r.get("ok"), r)
        p = self.get_json("/api/ai/review/progress?job=p27job")
        self.assertEqual(p["state"], "done", p)
        self.assertEqual(p["page"], 2)
        self.assertEqual(p["pages"], 2)

    def test_batch_without_ai_writes_nothing(self):
        os.remove(os.path.join(self.tmp, "ai.json"))
        ids = self.add_q(2)
        r = self.post_json("/api/ai/review", {"ids": ids})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("系统设置", r.get("error") or "")
        self.assertEqual(self.get_json("/api/questions?ai=pending")["total"], 0)

    def test_delete_question_removes_its_review(self):
        """题删了建议不能留着，否则待处理里会多出点不开的一条。"""
        qid = self.add_q(1)[0]
        self.fake.queue_json(reply())
        self.post_json("/api/ai/review", {"ids": [qid]})
        self.assertEqual(self.get_json("/api/ai/review/summary")["pending"], 1)
        self.post_json("/api/questions/bulk", {"action": "delete", "ids": [qid]})
        s = self.get_json("/api/ai/review/summary")
        self.assertEqual(s["pending"], 0, s)
        self.assertEqual(self.get_json("/api/ai/review")["reviews"], [])


class P27Migrate(BaseCase):
    """老库升级：这一版给 ai_review 加了 old_* 两列，正式库里已有几百道题，
    绝不能靠删库重建来升级结构。"""

    def test_old_table_gains_new_columns(self):
        conn = self.core.connect()
        conn.execute("DROP TABLE IF EXISTS ai_review")
        conn.execute(
            "CREATE TABLE ai_review (qid INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'fill',"
            " verdict TEXT NOT NULL DEFAULT 'filled', suggest_answer TEXT NOT NULL DEFAULT '',"
            " suggest_analysis TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
            " confidence REAL NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',"
            " valid INTEGER NOT NULL DEFAULT 1, problem TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL)")
        conn.commit()
        conn.close()

        self.core.init_db()

        conn = self.core.connect()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_review)")}
        n = conn.execute("SELECT COUNT(*) c FROM question").fetchone()["c"]
        conn.close()
        for name in ("old_answer", "old_analysis"):
            self.assertIn(name, cols)
        self.assertGreater(n, 0, "升级不能顺手把题库清空")


if __name__ == "__main__":
    unittest.main()
