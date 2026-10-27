#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P18：规模验收（AC-14）。

需求书写的是"1 万题规模下组卷 ≤ 2 秒，列表操作无明显卡顿"，但一直没有用例盯着。
性能验收不测的话，两个东西会悄悄退化：
  * `load_pool()` 改成先取全表再在 Python 里筛，题一多就慢在 IO 上；
  * 列表与导出各写一份 SQL，加了筛选条件后忘了建索引。

阈值取需求书原话（组卷 2 秒），其余给到 1 秒——真实机器是办公室的老台式，
不能按开发机的速度定线，否则用例会变成"总有一天会红"的噪声。
"""

import json
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import BaseCase  # noqa: E402

TOTAL = 10000


class PerfCase(BaseCase):
    """铺满 1 万题再测，不用 seed（那会按分类×题型×难度铺成小批量）。"""

    def fill(self, total=TOTAL):
        conn = self.core.connect()
        tree = self.core.categories_tree(conn)
        ts = self.core.now_str()
        rows = []
        n = 0
        while n < total:
            for c in tree:
                sub = c["subs"][0]["id"] if c["subs"] else None
                for t in self.core.TYPES:
                    for d in self.core.DIFFS:
                        if n >= total:
                            break
                        opts, ans, blanks = [], "", 0
                        if t in ("single", "multi"):
                            opts = [{"key": k, "text": "选项" + k} for k in "ABCD"]
                            ans = "A" if t == "single" else "AB"
                        elif t == "judge":
                            ans = "正确"
                        elif t == "fill":
                            ans, blanks = "甲|乙", 2
                        else:
                            ans = "参考答案：按要点作答。"
                        rows.append((c["id"], sub, t, d,
                                     "压测题库-%s-%s-%s-#%d 的题干内容" % (c["name"], t, d, n),
                                     json.dumps(opts, ensure_ascii=False), ans, blanks,
                                     None, "", "%s,%s" % (c["name"], t), ts, ts))
                        n += 1
        conn.executemany(
            """INSERT INTO question(cat_id, sub_id, qtype, diff, stem, options, answer,
               blanks, score, analysis, tags, created_at, updated_at, used_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)""", rows)
        conn.commit()
        conn.close()
        return n

    def cfg(self):
        return {
            "mode": "auto", "title": "压测卷", "unit": "ZJLab", "duration": 45,
            "rows": [
                {"qtype": "single", "enabled": True, "count": 30, "score": 1},
                {"qtype": "multi", "enabled": True, "count": 10, "score": 2},
                {"qtype": "judge", "enabled": True, "count": 10, "score": 1},
                {"qtype": "fill", "enabled": True, "count": 10, "score": 2},
                {"qtype": "qa", "enabled": True, "count": 4, "score": 7},
            ],
            "diff_mode": "points", "diff_target": 30,
            "diff_ratio": {"easy": 50, "mid": 30, "hard": 20},
            "quota_mode": "percent", "quotas": {},
            "rules": {"avoid_recent": {"enabled": False, "papers": 0},
                      "must": {"enabled": False, "ids": []},
                      "scope": {"enabled": False, "tags": "", "new_days": 0,
                                "new_ratio": 0}},
            "target_score": 100,
        }

    # ---------------------------------------------------------------- 组卷

    def test_build_under_2s_at_10k(self):
        """1 万题规模组卷必须在 2 秒内——需求书 AC-14 的原话。"""
        self.fill()
        cfg = self.cfg()
        worst = 0.0
        for _ in range(3):
            t0 = time.time()
            r = self.pe.build(cfg)
            worst = max(worst, time.time() - t0)
            self.assertTrue(r.get("ok"), r.get("errors"))
        self.assertLess(worst, 2.0, "1 万题组卷最慢 %.2fs，超出 2 秒验收线" % worst)

    def test_quota_mode_under_2s_at_10k(self):
        """配额模式多一层分类分配，同样不能超线。"""
        self.fill()
        cfg = self.cfg()
        cfg["mode"] = "quota"
        t0 = time.time()
        r = self.pe.build(cfg)
        self.assertTrue(r.get("ok"), r.get("errors"))
        self.assertLess(time.time() - t0, 2.0)

    def test_precheck_under_1s_at_10k(self):
        """预检是边改参数边算的，慢一点就会顿。"""
        self.fill()
        t0 = time.time()
        r = self.pe.precheck(self.cfg())
        self.assertLess(time.time() - t0, 1.0, "1 万题预检过慢")
        self.assertTrue(r)

    # ---------------------------------------------------------------- 列表

    def test_list_and_filter_fast_at_10k(self):
        """列表、关键词筛选、分类筛选都要能在 1 秒内出结果。"""
        self.fill()
        conn = self.core.connect()
        cat = self.core.categories_tree(conn)[0]
        conn.close()

        t0 = time.time()
        res = self.core.list_questions({"page": 1, "size": 20})
        dt = time.time() - t0
        self.assertEqual(res["total"], TOTAL, "总数不对：%s" % res["total"])
        self.assertLess(dt, 1.0, "首页列表 %.2fs" % dt)

        t0 = time.time()
        res = self.core.list_questions({"page": 1, "size": 20, "kw": "压测题库"})
        self.assertLess(time.time() - t0, 1.0, "关键词筛选过慢")
        self.assertEqual(res["total"], TOTAL)

        t0 = time.time()
        res = self.core.list_questions({"page": 1, "size": 20, "cat_id": cat["id"]})
        self.assertLess(time.time() - t0, 1.0, "分类筛选过慢")
        self.assertGreater(res["total"], 0)

    def test_stats_fast_at_10k(self):
        """工作台统计每次打开都要算，慢了首页就白屏。"""
        self.fill()
        conn = self.core.connect()
        t0 = time.time()
        st = self.core.stats(conn)
        conn.close()
        self.assertLess(time.time() - t0, 1.0, "工作台统计过慢")
        self.assertEqual(st["total"], TOTAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
