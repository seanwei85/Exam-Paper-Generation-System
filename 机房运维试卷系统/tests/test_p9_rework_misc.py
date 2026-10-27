#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P9 补漏：剩下三个 P1。

- FR-HM-03 工作台题库健康度提示
- FR-AG-04 难度目标留空时按"中等偏易"（总上限 30%）推荐
- FR-PL-04 试卷详情显示组卷参数与随机种子
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


class P9Api(HttpCase):
    def cfg_single(self, count=10, target=None):
        """只用单选题，方便算难度积分：易 0 / 中 1 / 难 2，上限 = count × 2。"""
        cfg = self.get_json("/api/paper/default")
        cfg["rows"] = [{"qtype": "single", "enabled": True, "count": count, "score": 10}]
        cfg["mode"] = "auto"
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = target
        return cfg

    def paper(self, pid):
        return self.get_json("/api/papers/%d" % pid)["paper"]

    def test_blank_target_uses_recommendation(self):
        """留空 → 按总上限 30% 推荐（10 题 × 上限 2 分 = 20 分上限 → 推荐 6 分）。"""
        self.seed(per_cell=2)
        r = self.post_json("/api/papers", self.cfg_single(10, None))
        self.assertTrue(r.get("ok"), r.get("errors"))
        self.assertEqual(r.get("diff_recommended"), 6, "没有给出 30% 推荐值")
        self.assertEqual(r["diff_score"], 6, "生成结果没用推荐值")
        self.assertTrue(self.paper(r["id"])["config"].get("diff_target_auto"),
                        "试卷快照里应记下「这个目标是系统推荐的」，便于事后追溯")

    def test_explicit_zero_is_kept(self):
        """明确填 0 就是"全易"，不能被推荐值覆盖。"""
        self.seed(per_cell=2)
        r = self.post_json("/api/papers", self.cfg_single(10, 0))
        self.assertTrue(r.get("ok"), r.get("errors"))
        self.assertEqual(r["diff_score"], 0)
        self.assertFalse(self.paper(r["id"])["config"].get("diff_target_auto"))

    def test_precheck_reports_recommendation(self):
        self.seed(per_cell=2)
        r = self.post_json("/api/papers/precheck", self.cfg_single(10, None))
        self.assertEqual(r.get("diff_recommend"), 6, "预检没给出推荐值")
        self.assertTrue(r.get("diff_auto"), "预检没有说明当前用的是推荐值")


class P9Ui(RenderCase):
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

    def goto(self, hash_path, wait):
        """导航并等新文档就绪（序号由基类保证唯一，见 renderbase.RenderCase.nav）。"""
        return self.nav(hash_path, wait)

    # ------------------------------------------------------ 健康度

    HEALTH_READY = ("(function(){var b=document.getElementById('healthBox');"
                    "return !!b && b.innerText.indexOf('正在检查')<0;})()")
    HEALTH_TEXT = "document.getElementById('healthBox').closest('.card').innerText"

    def test_health_hint_when_pool_too_small(self):
        """空题库时工作台要明确说"不够用"，而不是等用户点了组卷才发现。"""
        c = self.goto("dashboard", self.HEALTH_READY)
        text = c.eval(self.HEALTH_TEXT)
        self.assertIn("题库健康度", text)
        self.assertIn("不足", text, "空题库居然没提示不足")
        self.assertNotIn("够用", text)

    def test_health_ok_when_pool_enough(self):
        """题量充足时同一块区域要给"够用"的结论。"""
        self.seed(per_cell=2)
        c = self.goto("dashboard", self.HEALTH_READY)
        text = c.eval(self.HEALTH_TEXT)
        self.assertIn("题库健康度", text)
        self.assertIn("够用", text, "150 道题应当足够支撑默认模板")
        self.assertNotIn("不足", text)

    # ------------------------------------------------------ 试卷详情参数

    def build_paper(self):
        self.seed(per_cell=2)
        cfg = self.get_json("/api/paper/default")
        cfg["rows"] = [{"qtype": "single", "enabled": True, "count": 8, "score": 6},
                       {"qtype": "judge", "enabled": True, "count": 6, "score": 5}]
        cfg["mode"] = "auto"
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = 0
        cfg["rules"]["avoid_recent"] = {"enabled": False, "papers": 0}
        r = self.post_json("/api/papers", cfg)
        self.assertTrue(r.get("ok"), "组卷失败：%s" % (r.get("errors"),))
        return r

    def test_paper_detail_shows_config_and_seed(self):
        r = self.build_paper()
        pid, seed = r["id"], self.get_json("/api/papers/%d" % r["id"])["paper"]["seed"]
        c = self.goto("paper-detail?id=%d" % pid, "document.querySelectorAll('.a4').length>0")
        text = c.eval("document.body.innerText")
        self.assertIn("随机种子", text, "详情页没有显示随机种子")
        self.assertIn(str(seed), text, "详情页的种子值与试卷不一致")
        self.assertIn("组卷参数", text, "详情页没有组卷参数区")
        self.assertIn("系统自动", text, "参数区没有说明组卷方式")
        self.assertIn("45", text, "参数区没有考试时长")


if __name__ == "__main__":
    unittest.main(verbosity=2)
