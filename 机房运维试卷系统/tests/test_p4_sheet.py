#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 测试：卷面数据完整性（渲染所需字段、题号、分值合计、答案）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BaseCase


class P4SheetData(BaseCase):
    def setUp(self):
        super().setUp()
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.assertTrue(r["ok"], r.get("errors"))
        self.pid = r["id"]
        self.paper = self.pe.get_paper(self.pid)["paper"]

    def test_sections_order_and_numbering(self):
        order = ["single", "multi", "judge", "fill", "qa"]
        got = [s["qtype"] for s in self.paper["sections"]]
        self.assertEqual(got, [t for t in order if t in got])
        for s in self.paper["sections"]:
            self.assertEqual([i["no_in_type"] for i in s["items"]],
                             list(range(1, s["count"] + 1)),
                             "%s 的题号不连续" % s["name"])

    def test_subtotal_and_total(self):
        total = 0
        for s in self.paper["sections"]:
            self.assertAlmostEqual(s["subtotal"], sum(i["score"] for i in s["items"]), places=2)
            total += s["subtotal"]
        self.assertAlmostEqual(total, self.paper["total_score"], places=2)
        self.assertEqual(sum(s["count"] for s in self.paper["sections"]), self.paper["q_count"])

    def test_every_item_renderable(self):
        for it in self.paper["items"]:
            self.assertTrue(it["stem"].strip(), "题目 %s 题干为空" % it["seq"])
            self.assertTrue(it["answer"].strip(), "题目 %s 答案为空" % it["seq"])
            self.assertGreater(it["score"], 0)
            self.assertIn(it["qtype"], ("single", "multi", "judge", "fill", "qa"))
            if it["qtype"] in ("single", "multi"):
                self.assertGreaterEqual(len(it["options"]), 2, "选择题 %s 选项不足" % it["seq"])

    def test_fill_answer_by_blank(self):
        fills = [i for i in self.paper["items"] if i["qtype"] == "fill"]
        self.assertTrue(fills)
        for it in fills:
            self.assertGreaterEqual(it["blanks"], 1)
            self.assertEqual(len(it["answer_list"]), it["blanks"],
                             "填空第 %s 题的答案个数与空数不一致" % it["seq"])

    def test_header_fields(self):
        p = self.paper
        self.assertRegex(p["no"], r"^ZJLab-\d{8}-\d{3}$")
        self.assertTrue(p["title"])
        self.assertEqual(p["duration"], 45)
        self.assertTrue(p["created_at"])
        self.assertIsInstance(p["sections"], list)


if __name__ == "__main__":
    unittest.main(verbosity=2)
