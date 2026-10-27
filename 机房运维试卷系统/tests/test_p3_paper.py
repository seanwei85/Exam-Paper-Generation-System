#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P3 测试：组卷预检、难度分解、抽题、编号、快照、配额、锁定、改分与配平。"""

import os
import re
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import BaseCase


class P3Precheck(BaseCase):
    def test_gap_report_precise(self):
        self.seed(per_cell=1, cats=["电气"])
        cfg = self.base_cfg()
        pre = self.pe.precheck(cfg)
        self.assertFalse(pre["ok"])
        gaps = {g["qtype"]: g for g in pre["gaps"]}
        self.assertIn("single", gaps)
        self.assertEqual(gaps["single"]["need"], 8)
        self.assertLess(gaps["single"]["avail"], 8)
        self.assertEqual(gaps["single"]["lack"], 8 - gaps["single"]["avail"])

    def test_points_out_of_range(self):
        self.seed(per_cell=2)
        cfg = self.base_cfg(diff_target=999)
        pre = self.pe.precheck(cfg)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("超出可达范围" in e for e in pre["errors"]))

    def test_points_feasible(self):
        self.seed(per_cell=2)
        pre = self.pe.precheck(self.base_cfg())
        self.assertTrue(pre["ok"], pre["errors"])
        self.assertTrue(pre["diff"]["feasible"])

    def test_avail_diff_reported(self):
        self.seed(per_cell=2)
        pre = self.pe.precheck(self.base_cfg())
        single = next(i for i in pre["by_type"] if i["qtype"] == "single")
        self.assertGreaterEqual(single["avail_diff"].get("easy", 0), 1)

    def test_quota_sum_equals_total(self):
        self.seed(per_cell=2)
        cfg = self.base_cfg(mode="quota", quotas={"1": 30, "2": 25, "3": 20, "4": 15, "5": 10})
        pre = self.pe.precheck(cfg)
        self.assertTrue(pre["ok"], pre["errors"])
        self.assertEqual(sum(q["count"] for q in pre["quota"]["items"]), pre["total_count"])

    def test_quota_blank_auto_split(self):
        self.seed(per_cell=2)
        cfg = self.base_cfg(mode="quota", quotas={"1": 50, "2": 50})
        pre = self.pe.precheck(cfg)
        counts = {q["name"]: q["count"] for q in pre["quota"]["items"]}
        self.assertEqual(sum(counts.values()), 24)
        self.assertEqual(counts["电气"], 12)

    def test_quota_over_100_rejected(self):
        self.seed(per_cell=2)
        cfg = self.base_cfg(mode="quota", quotas={"1": 60, "2": 60})
        pre = self.pe.precheck(cfg)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("超过 100" in e for e in pre["errors"]))


class P3Build(BaseCase):
    def test_counts_and_score(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.assertTrue(r["ok"], r.get("errors"))
        d = self.pe.get_paper(r["id"])["paper"]
        c = Counter(i["qtype"] for i in d["items"])
        self.assertEqual(c["single"], 8)
        self.assertEqual(c["multi"], 4)
        self.assertEqual(c["judge"], 6)
        self.assertEqual(c["fill"], 4)
        self.assertEqual(c["qa"], 2)
        self.assertEqual(d["q_count"], 24)
        # 8*1 + 4*2 + 6*1 + 4*(2空*1分) + 2*12 = 8+8+6+8+24 = 54
        self.assertAlmostEqual(d["total_score"], 54.0, places=2)

    def test_diff_points_exact(self):
        self.seed(per_cell=2)
        for target in (0, 8, 20, 36, 52):
            r = self.pe.build(self.base_cfg(diff_target=target))
            self.assertTrue(r["ok"], "%s: %s" % (target, r.get("errors")))
            self.assertEqual(r["diff_score"], target, "目标 %d 未精确命中" % target)

    def test_no_duplicate_question(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        d = self.pe.get_paper(r["id"])["paper"]
        ids = [i["qid"] for i in d["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_paper_no_format_and_increment(self):
        self.seed(per_cell=3)
        r1 = self.pe.build(self.base_cfg())
        r2 = self.pe.build(self.base_cfg())
        self.assertTrue(r1["ok"] and r2["ok"])
        for no in (r1["no"], r2["no"]):
            self.assertRegex(no, r"^ZJLab-\d{8}-\d{3}$")
        self.assertNotEqual(r1["no"], r2["no"])
        self.assertEqual(int(r2["no"][-3:]), int(r1["no"][-3:]) + 1)

    def test_snapshot_isolated(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        d1 = self.pe.get_paper(r["id"])["paper"]
        qid = d1["items"][0]["qid"]
        conn = self.core.connect()
        conn.execute("UPDATE question SET stem='已被修改的题干' WHERE id=?", (qid,))
        conn.commit()
        conn.close()
        d2 = self.pe.get_paper(r["id"])["paper"]
        self.assertEqual([i["stem"] for i in d1["items"]], [i["stem"] for i in d2["items"]])
        self.assertNotIn("已被修改的题干", [i["stem"] for i in d2["items"]])

    def test_used_count_incremented(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        conn = self.core.connect()
        used = conn.execute(
            "SELECT COUNT(*) c FROM question WHERE used_count > 0"
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(used, r["q_count"])

    def test_ratio_mode(self):
        self.seed(per_cell=3)
        cfg = self.base_cfg(diff_mode="ratio", diff_ratio={"easy": 50, "mid": 30, "hard": 20})
        r = self.pe.build(cfg)
        self.assertTrue(r["ok"], r.get("errors"))
        d = self.pe.get_paper(r["id"])["paper"]
        c = Counter(i["diff"] for i in d["items"])
        self.assertGreater(c["easy"], c["hard"])
        self.assertAlmostEqual(c["easy"] / 24, 0.5, delta=0.15)


class P3Rules(BaseCase):
    def test_avoid_recent_no_overlap(self):
        self.seed(per_cell=3)
        cfg = self.base_cfg(rules={"avoid_recent": {"enabled": True, "papers": 3},
                                   "must": {"enabled": False, "ids": []},
                                   "scope": {"enabled": False}})
        r1 = self.pe.build(cfg)
        r2 = self.pe.build(cfg)
        self.assertTrue(r1["ok"] and r2["ok"], (r1.get("errors"), r2.get("errors")))
        s1 = {i["qid"] for i in self.pe.get_paper(r1["id"])["paper"]["items"]}
        s2 = {i["qid"] for i in self.pe.get_paper(r2["id"])["paper"]["items"]}
        self.assertEqual(s1 & s2, set(), "两套卷出现重复题目")

    def test_must_lock_included(self):
        self.seed(per_cell=3)
        ids = self.qids(qtype="qa")[:2]
        cfg = self.base_cfg(rules={"avoid_recent": {"enabled": False}, "papers": 0,
                                   "must": {"enabled": True, "ids": ids},
                                   "scope": {"enabled": False}})
        r = self.pe.build(cfg)
        self.assertTrue(r["ok"], r.get("errors"))
        got = {i["qid"] for i in self.pe.get_paper(r["id"])["paper"]["items"]}
        for i in ids:
            self.assertIn(i, got, "锁定题 %d 未进卷" % i)

    def test_must_overflow_rejected(self):
        self.seed(per_cell=3)
        ids = self.qids(qtype="qa")[:3]
        cfg = self.base_cfg(rules={"avoid_recent": {"enabled": False},
                                   "must": {"enabled": True, "ids": ids},
                                   "scope": {"enabled": False}})
        pre = self.pe.precheck(cfg)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("超过该题型题量" in e for e in pre["errors"]), pre["errors"])

    def test_tag_scope_filter(self):
        self.seed(per_cell=2)
        cfg = self.base_cfg(rules={"avoid_recent": {"enabled": False},
                                   "must": {"enabled": False, "ids": []},
                                   "scope": {"enabled": True, "tags": "电气"}})
        pre = self.pe.precheck(cfg)
        self.assertFalse(pre["ok"])
        self.assertTrue(pre["gaps"], "限定标签后应报缺口")


class P3Quota(BaseCase):
    def test_quota_distribution_close(self):
        self.seed(per_cell=4)
        cfg = self.base_cfg(mode="quota", quotas={"1": 20, "2": 20, "3": 20, "4": 20, "5": 20})
        r = self.pe.build(cfg)
        self.assertTrue(r["ok"], r.get("errors"))
        for cid, want in r["quota"].items():
            got = r["cat_taken"].get(cid, 0)
            self.assertLessEqual(abs(got - want), 1, "分类 %s 配额 %s 实际 %s" % (cid, want, got))

    def test_quota_skewed(self):
        self.seed(per_cell=4)
        cfg = self.base_cfg(mode="quota", quotas={"1": 50, "2": 25, "3": 10, "4": 10, "5": 5})
        r = self.pe.build(cfg)
        self.assertTrue(r["ok"], r.get("errors"))
        self.assertGreater(r["cat_taken"].get(1, 0), r["cat_taken"].get(5, 0))


class P3ScoreOps(BaseCase):
    def test_set_item_score_recalc(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        d = self.pe.get_paper(r["id"])["paper"]
        item = d["items"][0]
        res = self.pe.set_item_score(r["id"], item["id"], 9)
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["total_score"], d["total_score"] - item["score"] + 9, places=2)

    def test_balance_to_100(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        res = self.pe.balance(r["id"], 100)
        self.assertTrue(res["ok"])
        self.assertAlmostEqual(res["total_score"], 100.0, places=1)
        self.assertLessEqual(len(res["changes"]), 24)

    def test_balance_has_floor(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.pe.balance(r["id"], 1)
        d = self.pe.get_paper(r["id"])["paper"]
        self.assertTrue(all(i["score"] >= 1 for i in d["items"]))

    def test_delete_paper(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.pe.delete_paper(r["id"])
        self.assertFalse(self.pe.get_paper(r["id"])["ok"])
        self.assertEqual(self.pe.list_papers({})["total"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
