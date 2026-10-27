#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 / P1 测试：数据层、分类、录题校验、分值计算、持久化。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BaseCase


class P0DataLayer(BaseCase):
    def test_default_categories(self):
        conn = self.core.connect()
        tree = self.core.categories_tree(conn)
        conn.close()
        self.assertEqual([c["name"] for c in tree], ["电气", "暖通", "弱电", "消防", "管理"])
        self.assertEqual(sum(len(c["subs"]) for c in tree), 25)

    def test_db_file_created(self):
        self.assertTrue(os.path.exists(self.core.DB_PATH))

    def test_settings_defaults(self):
        conn = self.core.connect()
        s = self.core.all_settings(conn)
        conn.close()
        self.assertEqual(s["paper_prefix"], "ZJLab")
        self.assertEqual(s["duration"], "45")


class P1Questions(BaseCase):
    def _single(self, **kw):
        p = {
            "cat_id": 1, "sub_id": 1, "qtype": "single", "diff": "mid",
            "stem": "UPS 电池后备时间取决于（　）。",
            "options": [{"key": "A", "text": "负载大小"}, {"key": "B", "text": "机房面积"}],
            "answer": "A", "analysis": "", "tags": "UPS",
        }
        p.update(kw)
        return self.core.save_question(p)

    def test_save_five_types(self):
        cases = [
            ("single", [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}], "A"),
            ("multi", [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}], "AB"),
            ("judge", [], "正确"),
            ("fill", [], "甲|乙"),
            ("qa", [], "按要点作答"),
        ]
        for qtype, opts, ans in cases:
            r = self.core.save_question({
                "cat_id": 1, "sub_id": 1, "qtype": qtype, "diff": "mid",
                "stem": "测试题干-" + qtype + ("：____ 与 ____" if qtype == "fill" else ""),
                "options": opts, "answer": ans,
            })
            self.assertTrue(r["ok"], "%s 保存失败：%s" % (qtype, r))

    def test_reject_single_two_answers(self):
        r = self._single(answer="AB")
        self.assertFalse(r["ok"])
        self.assertIn("单选题答案只能是 1 个选项", " ".join(r["errors"]))

    def test_reject_multi_one_answer(self):
        r = self._single(qtype="multi", answer="A")
        self.assertFalse(r["ok"])
        self.assertIn("多选题答案至少 2 个选项", " ".join(r["errors"]))

    def test_reject_answer_out_of_range(self):
        r = self._single(answer="D")
        self.assertFalse(r["ok"])
        self.assertIn("答案中的选项不在选项范围内", " ".join(r["errors"]))

    def test_reject_judge_bad_answer(self):
        r = self._single(qtype="judge", options=[], answer="对")
        self.assertFalse(r["ok"])
        self.assertIn("判断题答案只能是", " ".join(r["errors"]))

    def test_reject_empty_stem(self):
        r = self._single(stem="  ")
        self.assertFalse(r["ok"])
        self.assertIn("题干不能为空", " ".join(r["errors"]))

    def test_fill_blanks_auto_count(self):
        r = self.core.save_question({
            "cat_id": 1, "sub_id": 1, "qtype": "fill", "diff": "easy",
            "stem": "机房温度应控制在 18~____ ℃，湿度 40%~____。",
            "options": [], "answer": "27|60",
        })
        self.assertTrue(r["ok"])
        q = self.core.get_question(r["id"])
        self.assertEqual(q["blanks"], 2)

    def test_fill_score_by_blank(self):
        r = self.core.save_question({
            "cat_id": 1, "sub_id": 1, "qtype": "fill", "diff": "easy",
            "stem": "两空题：____ 与 ____。", "options": [], "answer": "甲|乙",
        })
        q = self.core.get_question(r["id"])
        self.assertEqual(q["final_score"], 2.0)

    def test_question_score_override(self):
        r = self._single(score=5)
        q = self.core.get_question(r["id"])
        self.assertEqual(q["final_score"], 5.0)

    def test_points_rule(self):
        for qtype, pts in [("single", 2), ("multi", 2), ("judge", 2),
                           ("fill", 2), ("qa", 4)]:
            r = self._single(qtype=qtype, diff="hard", answer="AB" if qtype == "multi" else ("正确" if qtype == "judge" else "A"))
            if not r.get("ok"):
                r = self.core.save_question({
                    "cat_id": 1, "sub_id": 1, "qtype": qtype, "diff": "hard",
                    "stem": "难度积分测试-" + qtype + " ____", "options": [],
                    "answer": "按要点" if qtype == "qa" else "甲",
                })
            q = self.core.get_question(r["id"])
            self.assertEqual(q["points"], pts, "%s 的难题积分应为 %d" % (qtype, pts))

    def test_persist_after_reconnect(self):
        r = self._single(stem="持久化测试题干")
        qid = r["id"]
        conn = self.core.connect()
        conn.close()
        q = self.core.get_question(qid)
        self.assertEqual(q["stem"], "持久化测试题干")

    def test_update_question(self):
        r = self._single()
        r2 = self._single(id=r["id"], stem="改过的题干")
        self.assertTrue(r2["ok"])
        self.assertEqual(self.core.get_question(r["id"])["stem"], "改过的题干")

    def test_delete_question(self):
        r = self._single()
        self.core.delete_questions([r["id"]])
        self.assertFalse(self.core.get_question(r["id"])["ok"])

    def test_list_filter_and_paging(self):
        self.seed(per_cell=1)
        res = self.core.list_questions({"type": "single", "size": "10", "page": "1"})
        self.assertGreater(res["total"], 0)
        self.assertTrue(all(i["qtype"] == "single" for i in res["items"]))
        self.assertLessEqual(len(res["items"]), 10)
        self.assertLessEqual(res["size"], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
