#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 重复题提示（FR-IM-07）。

需求：导入的题与库内已有题干相似度超过阈值时标记出来，界面上默认不勾选。
这里先钉死两件事：
  1) 判定本身要稳 —— 只差空格/全半角/一个错别字算是重复，讲别的事不算；
  2) 解析接口要把判定结果带出去（dup / dup_of / dup_score），并有汇总 dup_count。
"""

import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase

import dupe

SAMPLE = """一、单项选择题
1. 机房精密空调的加湿用水应使用（　）。
A. 自来水
B. 软化水
C. 海水
D. 中水
答案：B

2. UPS 电池组的后备时间取决于（　）。
A. 负载大小
B. 机房面积
C. 维护周期
D. 室温
答案：A

3. 柴油发电机启动后应先（　）。
A. 直接带载
B. 空载暖机
C. 立即停机
D. 断开市电
答案：B
"""


class DupeUnit(unittest.TestCase):
    """判定逻辑本身（不碰数据库）。"""

    def test_identical(self):
        self.assertAlmostEqual(dupe.similarity("机房温度过高会怎样", "机房温度过高会怎样"), 1.0, 3)

    def test_width_and_space_ignored(self):
        """全角/半角、空格、括号差异不该影响判定 —— 用户从不同来源拷来的题常这样。"""
        a = "机房精密空调的加湿用水应使用（　）。"
        b = "机房精密空调的加湿用水应使用( )。"
        self.assertAlmostEqual(dupe.similarity(a, b), 1.0, 3)

    def test_one_typo_still_high(self):
        s = dupe.similarity("机房精密空调的加湿用水应使用软化水", "机房精密空调的加湿用水应使用软水")
        self.assertGreater(s, 0.9, "差一个字的题干应判为高度相似")

    def test_unrelated_low(self):
        s = dupe.similarity("机房精密空调的加湿用水应使用软化水",
                            "消防气体灭火系统喷放前应确认人员全部撤离")
        self.assertLess(s, 0.5)

    def test_empty_never_matches(self):
        """极短/空白题干归一化后不能变成"人人相同"。"""
        self.assertEqual(dupe.similarity("（　）", "（　）"), 0.0)
        self.assertEqual(dupe.similarity("", ""), 0.0)

    def test_find_picks_best(self):
        pool = [(7, "机房精密空调的加湿用水应使用（　）。"),
                (9, "消防气体灭火系统喷放前应确认人员全部撤离")]
        hit = dupe.find("机房精密空调的加湿用水应使用()。", pool)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], 7)
        self.assertGreaterEqual(hit[1], 0.99)
        self.assertIn("加湿用水", hit[2], "要带回库内那道题的原文，界面才能提示重复的是哪题")

    def test_find_returns_none_when_clear(self):
        pool = [(7, "机房精密空调的加湿用水应使用（　）。")]
        self.assertIsNone(dupe.find("柴油发电机启动后应先空载暖机", pool))


class P2DupApi(HttpCase):
    def add_question(self, stem, qtype="single", answer="B", options=None):
        conn = self.core.connect()
        cat = conn.execute("SELECT id FROM category ORDER BY sort,id LIMIT 1").fetchone()
        sub = conn.execute("SELECT id FROM subcategory WHERE cat_id=? ORDER BY sort,id LIMIT 1",
                           (cat["id"],)).fetchone()
        ts = self.core.now_str()
        opts = options if options is not None else [{"key": k, "text": "选项" + k} for k in "ABCD"]
        conn.execute(
            """INSERT INTO question(cat_id, sub_id, qtype, diff, stem, options, answer, blanks,
               score, analysis, tags, created_at, updated_at, used_count)
               VALUES(?,?,?,?,?,?,?,0,NULL,'','',?,?,0)""",
            (cat["id"], sub["id"], qtype, "mid", stem,
             json.dumps(opts, ensure_ascii=False), answer, ts, ts))
        conn.commit()
        qid = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
        conn.close()
        return qid

    def parse(self, text):
        return self.post_json("/api/import/parse", {
            "filename": "库.txt",
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        })

    def by_stem(self, items, keyword):
        for it in items:
            if keyword in it["stem"]:
                return it
        self.fail("解析结果里找不到含「%s」的题" % keyword)

    def test_parse_marks_duplicate_and_reports_count(self):
        qid = self.add_question("机房精密空调的加湿用水应使用（　）。")
        r = self.parse(SAMPLE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["items"]), 3)
        hit = self.by_stem(r["items"], "加湿用水")
        self.assertTrue(hit.get("dup"), "与库内题干相同的题没有被标记为重复")
        self.assertEqual(hit.get("dup_of"), qid, "dup_of 应指向库内那道题")
        self.assertGreaterEqual(hit.get("dup_score") or 0, 0.99)
        self.assertEqual(r.get("dup_count"), 1, "dup_count 汇总不对")

    def test_duplicate_ignores_punctuation_noise(self):
        """库里的题干带全角括号，导入的是半角 —— 仍要认出来。"""
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        text = SAMPLE.replace("机房精密空调的加湿用水应使用（　）。",
                              "机房精密空调的加湿用水应使用( )。")
        r = self.parse(text)
        hit = self.by_stem(r["items"], "加湿用水")
        self.assertTrue(hit.get("dup"), "只差括号全半角就认不出来了")

    def test_clean_questions_not_marked(self):
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        r = self.parse(SAMPLE)
        for kw in ("后备时间", "柴油发电机"):
            self.assertFalse(self.by_stem(r["items"], kw).get("dup"),
                             "不该把无关题目标成重复：" + kw)
        self.assertEqual(r.get("dup_count"), 1)

    def test_empty_library_marks_nothing(self):
        r = self.parse(SAMPLE)
        self.assertEqual(r.get("dup_count"), 0)
        self.assertTrue(all(not it.get("dup") for it in r["items"]))
        self.assertTrue(all(it.get("dup_of") is None for it in r["items"]))

    def test_judge_and_fill_also_compared(self):
        """重复判定不限题型：判断题、填空题也要能认出来。"""
        self.add_question("精密空调可以频繁启停。", qtype="judge", answer="错误", options=[])
        self.add_question("机房温度应控制在 18~____ ℃，湿度 40%~____。", qtype="fill",
                          answer="27|60", options=[])
        text = """三、判断题
1. 精密空调可以频繁启停。
答案：错误

四、填空题
2. 机房温度应控制在 18~____ ℃，湿度 40%~____。
答案：27|60
"""
        r = self.parse(text)
        self.assertEqual(len(r["items"]), 2)
        for it in r["items"]:
            self.assertTrue(it.get("dup"), "同一题干的%s题没被标记" % it["qtype"])

    def test_dup_fields_accepted_by_commit(self):
        """解析结果提交入库时，多出来的 dup 字段不能被当成非法字段。

        分类从 P24 起由解析侧按题干关键词猜（暖通/电气/弱电/消防/管理），
        猜不出来才留空等界面按第一分类补齐。这里先确认 cat_id 只会是
        "库里的某一类"或"空"，再照界面的做法补齐后提交，验证 dup 字段不影响入库。
        """
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        r = self.parse(SAMPLE)
        first = r["items"][0]
        self.assertTrue(first.get("dup"))
        ids = [c["id"] for c in self.get_json("/api/categories")["categories"]]
        self.assertIn(first.get("cat_id"), ids + [None],
                      "cat_id 只能是库里的分类或空，不能是臆造的值")

        cat = self.get_json("/api/categories")["categories"][0]
        item = dict(first)
        item["cat_id"] = cat["id"]
        item["sub_id"] = cat["subs"][0]["id"]
        res = self.post_json("/api/import/commit", {"items": [item], "filename": "库.txt"})
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("imported"), 1, res)
        # 重复字段不该被写进题库
        conn = self.core.connect()
        row = conn.execute("SELECT stem FROM question WHERE stem LIKE '%加湿用水%' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
