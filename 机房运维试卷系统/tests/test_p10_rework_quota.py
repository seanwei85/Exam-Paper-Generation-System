#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P10 补漏：FR-AG-20 配额执行情况（配置 / 实际 / 偏差）。

原来只在生成结果里拼了一句"分类配额实际分布有偏差：电气 +2"的文字提醒，
试卷一关就查不到了。这条要求的是**列表**：每个分类配置多少、实际抽到多少、
差多少，偏差超过 ±1 的行要显眼标出来。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


class P10Api(HttpCase):
    def pad(self, cat, n, tag="A"):
        """给某个分类补 n 道单选题（全易），让配额有题可抽。

        seed() 是每格 2 道，配额如果设 6 题就凑不出来——那种情况下"配额没被满足"
        是题库的错，不是引擎的错。要验证"题够时配额必须满足"，就得先把题备足。
        """
        for i in range(n):
            r = self.core.save_question({
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "easy",
                "stem": "【测试】%s 配额补题 %s%d" % (cat["name"], tag, i + 1),
                "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                "answer": "A",
            })
            self.assertTrue(r.get("ok"), "补题失败：%s" % (r.get("errors"),))

    def quota_cfg(self, per_cat=None):
        """配额模式：只用单选题，配额按题数指定，难度全易（避免难度干扰抽题）。"""
        cats = self.get_json("/api/categories")["categories"]
        cfg = self.get_json("/api/paper/default")
        cfg["mode"] = "quota"
        cfg["quota_mode"] = "count"
        cfg["rows"] = [{"qtype": "single", "enabled": True, "count": 10, "score": 10}]
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = 0
        cfg["rules"]["avoid_recent"] = {"enabled": False, "papers": 0}
        cfg["quotas"] = per_cat or {str(cats[0]["id"]): 6, str(cats[1]["id"]): 4}
        return cfg, cats

    def cat_of_question(self):
        """qid → 一级分类 id，用来核对"实际分布"是不是真的。"""
        res = self.get_json("/api/questions?size=200&page=1")
        return {q["id"]: q["cat_id"] for q in res["items"]}

    def test_config_records_actual_distribution(self):
        """试卷快照里要记下各类实际抽了几题，否则事后无法复盘配额执行情况。"""
        self.seed(per_cell=2)
        cfg, cats = self.quota_cfg()
        r = self.post_json("/api/papers", cfg)
        self.assertTrue(r.get("ok"), r.get("errors"))

        p = self.get_json("/api/papers/%d" % r["id"])["paper"]
        actual = p["config"].get("cat_actual")
        self.assertIsNotNone(actual, "试卷配置里没有 cat_actual，配额执行情况没留痕")

        want = {str(c["id"]): 0 for c in cats}
        for it in p["items"]:
            want[str(self.cat_of_question()[it["qid"]])] += 1
        got = {str(k): v for k, v in actual.items()}
        for k, v in want.items():
            self.assertEqual(got.get(k, 0), v,
                             "分类 %s 记的实际题量 %s 与卷面不符（应为 %d）"
                             % (k, got.get(k, 0), v))

    def test_quota_is_met_when_pool_is_enough(self):
        """题够的时候配额必须被满足——否则"偏差表"里全是红字，反而掩盖真问题。"""
        self.seed(per_cell=2)
        cfg, cats = self.quota_cfg()
        self.pad(cats[0], 4)          # 2 + 4 = 6
        self.pad(cats[1], 2)          # 2 + 2 = 4
        r = self.post_json("/api/papers", cfg)
        self.assertTrue(r.get("ok"), r.get("errors"))
        actual = {str(k): v for k, v in
                  self.get_json("/api/papers/%d" % r["id"])["paper"]["config"]["cat_actual"].items()}
        self.assertEqual(actual.get(str(cats[0]["id"])), 6)
        self.assertEqual(actual.get(str(cats[1]["id"])), 4)


class P10Ui(RenderCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.chrome = Chrome(cls.browser)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.chrome.close()
        finally:
            super().tearDownClass()

    def build_paper(self):
        self.seed(per_cell=2)
        cats = self.get_json("/api/categories")["categories"]
        for cat, extra in ((cats[0], 4), (cats[1], 2)):
            for i in range(extra):
                self.core.save_question({
                    "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                    "qtype": "single", "diff": "easy",
                    "stem": "【测试】%s 配额补题 B%d" % (cat["name"], i + 1),
                    "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                    "answer": "A",
                })
        cfg = self.get_json("/api/paper/default")
        cfg["mode"] = "quota"
        cfg["quota_mode"] = "count"
        cfg["rows"] = [{"qtype": "single", "enabled": True, "count": 10, "score": 10}]
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = 0
        cfg["rules"]["avoid_recent"] = {"enabled": False, "papers": 0}
        cfg["quotas"] = {str(cats[0]["id"]): 6, str(cats[1]["id"]): 4}
        r = self.post_json("/api/papers", cfg)
        self.assertTrue(r.get("ok"), "组卷失败：%s" % (r.get("errors"),))
        return r, cats

    def test_detail_shows_quota_table(self):
        """详情页要有"配置 / 实际 / 偏差"三列，而不是只有一句文字提醒。"""
        r, cats = self.build_paper()
        self.nav("paper-detail?id=%d" % r["id"], "document.querySelectorAll('.a4').length>0")
        text = self.chrome.eval("document.body.innerText")
        self.assertIn("配额执行", text, "详情页没有配额执行情况")
        for col in ("配置", "实际", "偏差"):
            self.assertIn(col, text, "配额表缺「%s」列" % col)
        self.assertIn(cats[0]["name"], text, "配额表里没有分类名")

    def test_deviation_over_one_is_highlighted(self):
        """偏差超过 ±1 的行要标出来；差 1 题属于取整正常波动，不标。"""
        self.nav("dashboard", "!!document.getElementById('healthBox')")
        html = self.chrome.eval("quotaDevHtml({1:5,2:3,3:4},{1:2,2:3,3:0})")

        rows = [r for r in html.split("<tr") if "<td" in r]
        self.assertEqual(len(rows), 3, "三行数据都要渲染出来：%s" % html)
        self.assertIn('class="off"', rows[0], "偏差 -3 的行没被标出来")
        self.assertNotIn('class="off"', rows[1], "偏差 0 的行不该标")
        self.assertIn('class="off"', rows[2], "偏差 -4 的行没被标出来")

    def test_deviation_sign_is_readable(self):
        """正偏差要带 + 号，否则"2"和"-2"看起来只差一个横杠，容易读反。"""
        self.nav("dashboard", "!!document.getElementById('healthBox')")
        html = self.chrome.eval("quotaDevHtml({1:3},{1:6})")
        self.assertIn("+3", html, "多抽的题没写成 +3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
