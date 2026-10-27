#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P4 端到端渲染测试：起独立服务 + 无头浏览器抓真实 DOM，验证页面元素。

不依赖开发时的手工截图；任何一个页面元素缺失都会失败。
服务启动与浏览器抓取由 renderbase.RenderCase 提供。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from renderbase import RenderCase  # noqa: E402


class P4Render(RenderCase):
    def setUp(self):
        super().setUp()
        self.seed(per_cell=2)
        r = self.core.save_question({
            "cat_id": 1, "sub_id": 1, "qtype": "fill", "diff": "easy",
            "stem": "端到端双空题：机房温度 ____ ℃，湿度 ____ ％。",
            "options": [], "answer": "27|60",
        })
        self.assertTrue(r.get("ok"), r)
        self.fill_id = r["id"]
        cfg = self.base_cfg()
        cfg["rows"] = [
            {"qtype": "single", "enabled": True, "count": 4, "score": 1},
            {"qtype": "multi", "enabled": True, "count": 2, "score": 2},
            {"qtype": "judge", "enabled": True, "count": 2, "score": 1},
            {"qtype": "fill", "enabled": True, "count": 2, "score": 1},
            {"qtype": "qa", "enabled": True, "count": 2, "score": 12},
        ]
        cfg["title"] = "机房运维上岗考试"
        cfg["unit"] = "ZJLab 数据中心"
        cfg["diff_target"] = 10
        cfg["rules"] = {"avoid_recent": {"enabled": False, "papers": 0},
                        "must": {"enabled": True, "ids": [self.fill_id]},
                        "scope": {"enabled": False}}
        r = self.pe.build(cfg)
        self.assertTrue(r["ok"], r.get("errors"))
        self.pid, self.no = r["id"], r["no"]

    def test_paper_page_elements(self):
        html = self.dom_text("paper")
        self.assertIn("第一步 · 组卷方式", html)
        self.assertIn("按分类配额", html)
        self.assertIn("题型与分值", html)
        self.assertIn("附加规则", html)
        self.assertIn("实时预检", html)
        self.assertIn("排除近期已用", html)
        self.assertIn("必考锁定", html)

    def test_sheet_rendered(self):
        html = self.dom_text("paper-detail?id=%d" % self.pid)
        self.assertIn(self.no, html, "卷面缺少试卷编号")
        self.assertIn("密 封 线 内 不 要 答 题", html, "卷面缺少密封线")
        self.assertIn("一、单项选择题", html)
        self.assertIn("二、多项选择题", html)
        self.assertIn("姓名", html)
        self.assertIn("工号", html)
        self.assertIn("ZJLab 数据中心", html)
        self.assertIn("共 2 空", html, "填空题未标注空数")
        self.assertIn("标准答案", html)

    def test_answer_sheet_rendered(self):
        html = self.dom_text("paper-detail?id=%d&tab=ans" % self.pid)
        self.assertIn("标准答案", html)
        self.assertIn("第 1 空", html, "填空答案未按空给出")
        self.assertIn("阅 卷 参 考", html)
        self.assertIn("试卷编号", html)

    def test_papers_list_rendered(self):
        html = self.dom_text("papers")
        self.assertIn(self.no, html)
        self.assertIn("机房运维上岗考试", html)
        self.assertIn("难度积分", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
