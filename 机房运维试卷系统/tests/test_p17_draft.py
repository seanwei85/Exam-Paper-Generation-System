#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P17：没有答案的题（草稿态）。

现实：厂商模板导出的题库（开关电源/油机/蓄电池/高低配/UPS 那批）里，多数**没有
答案列**——它们是面试/口试题库，答案在主管脑子里。原来校验一律要求答案非空，
等于这类文件整份白导。

现在的规则：答案可以为空，但按"草稿"处理——
  * 能入库、能改；
  * 组卷**绝不抽**（卷子要附标准答案，抽到空答案等于出一份无法评分的卷）；
  * 题库页能一眼看出哪几题缺答案，并能只看缺答案；
  * 补上答案后自动进入组卷池。
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


class DraftApi(HttpCase):
    def seed(self, n=6, answer="A", prefix="正题"):
        conn = self.core.connect()
        cats = self.core.categories_tree(conn)
        conn.close()
        cat = cats[0]
        ids = []
        for i in range(n):
            r = self.core.save_question({
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "easy",
                "stem": "【测试】%s %d 题干" % (prefix, i + 1),
                "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                "answer": answer,
            })
            self.assertTrue(r.get("ok"), r.get("errors"))
            ids.append(r["id"])
        return ids

    def test_can_save_without_answer(self):
        cat = self.get_json("/api/categories")["categories"][0]
        r = self.post_json("/api/questions", {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"], "qtype": "fill",
            "diff": "mid", "stem": "机房温度应控制在 18~____ ℃。",
            "options": [], "answer": "",
        })
        self.assertTrue(r.get("ok"), "缺答案的题应能入库：%s" % (r.get("errors"),))

    def test_option_answer_still_validated_when_present(self):
        """答案为空放行，但写了答案就还得合法——不能顺手把校验也拆了。"""
        cat = self.get_json("/api/categories")["categories"][0]
        r = self.post_json("/api/questions", {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
            "qtype": "single", "diff": "mid",
            "stem": "答案不在选项范围内的题", "answer": "Z",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
        })
        self.assertFalse(r.get("ok"))
        self.assertTrue(any("选项范围" in e for e in r.get("errors") or []),
                        r.get("errors"))

    def test_multi_draft_still_needs_options(self):
        cat = self.get_json("/api/categories")["categories"][0]
        r = self.post_json("/api/questions", {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
            "qtype": "multi", "diff": "mid",
            "stem": "没有选项的多选题", "answer": "", "options": [],
        })
        self.assertFalse(r.get("ok"))
        self.assertTrue(any("2 个选项" in e for e in r.get("errors") or []))

    def test_import_commit_accepts_answerless_rows(self):
        cats = self.get_json("/api/categories")["categories"]
        cat = cats[0]
        items = [{"cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                  "qtype": "fill", "diff": "mid",
                  "stem": "导入缺答案第 %d 题（  ）" % i, "options": [], "answer": ""}
                 for i in range(3)]
        r = self.post_json("/api/import/commit", {"filename": "面试题库.xls", "items": items})
        self.assertEqual(r["imported"], 3, r)

    def test_draft_filter(self):
        self.seed(3, answer="A", prefix="有答案")
        self.seed(2, answer="", prefix="缺答案")
        miss = self.get_json("/api/questions?answer=missing&size=100")
        has = self.get_json("/api/questions?answer=has&size=100")
        self.assertTrue(miss["items"], "只看缺答案应有结果")
        self.assertTrue(all(not q["answer"] for q in miss["items"]))
        self.assertTrue(all(q["answer"] for q in has["items"]))
        self.assertEqual(miss["total"] + has["total"],
                         self.get_json("/api/questions?size=100")["total"])

    def paper_cfg(self, count):
        cfg = self.get_json("/api/paper/default")
        cfg["mode"] = "count"
        cfg["rows"] = [{"qtype": "single", "enabled": True, "count": count, "score": 10}]
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = 0
        cfg["rules"]["avoid_recent"] = {"enabled": False, "papers": 0}
        return cfg

    def test_draft_never_drawn_into_paper(self):
        """组卷不能抽缺答案的题：抽到就等于出一份没法评分的卷。"""
        self.seed(5, answer="A", prefix="可抽")
        self.seed(5, answer="", prefix="缺答案")
        # 库里一共 10 道单选题，但只有 5 道有答案；要 6 道就必须报"题量不足"，
        # 如果引擎把缺答案的题也算进池子，这里就会"组卷成功"——那才是 bug。
        bad = self.post_json("/api/papers", self.paper_cfg(6))
        self.assertFalse(bad.get("ok"), "缺答案的题被抽进卷子了：%s" % bad.get("id"))
        r = self.post_json("/api/papers", self.paper_cfg(5))
        self.assertTrue(r.get("ok"), r.get("errors"))
        paper = self.get_json("/api/papers/%d" % r["id"])["paper"]
        stems = [i["stem"] for i in paper["items"]]
        self.assertEqual(len(stems), 5)
        self.assertFalse([s for s in stems if "缺答案" in s], stems)
        for it in paper["items"]:
            self.assertTrue((it.get("answer") or "").strip(), "卷子里混进了空答案：%r" % it)

    def test_draft_joins_pool_after_answer_filled(self):
        ids = self.seed(1, answer="", prefix="待补答案")
        self.assertFalse(self.post_json("/api/papers", self.paper_cfg(1)).get("ok"),
                         "唯一一道题还没答案，不该组出卷子")

        q = self.get_json("/api/questions/%d" % ids[0])
        q["answer"] = "B"
        self.assertTrue(self.post_json("/api/questions", q).get("ok"))
        r = self.post_json("/api/papers", self.paper_cfg(1))
        self.assertTrue(r.get("ok"), "补上答案后应能组卷：%s" % (r.get("errors"),))


    def test_stats_expose_ready_count(self):
        """健康度要用"能组卷的题数"，所以统计里必须同时给总数和可用数。"""
        self.seed(2, answer="A", prefix="可用")
        self.seed(3, answer="", prefix="缺答案")
        stats = self.get_json("/api/state")["stats"]
        single = [t for t in stats["by_type"] if t["type"] == "single"][0]
        self.assertEqual(single["count"], 5)
        self.assertEqual(single["ready"], 2)
        self.assertEqual(stats["draft"], 3)


class DraftUi(RenderCase):
    """工作台的健康度不能把缺答案的题算成"够用"。"""

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

    def test_health_uses_ready_count(self):
        conn = self.core.connect()
        cats = self.core.categories_tree(conn)
        conn.close()
        cat = cats[0]
        for i in range(25):
            self.core.save_question({
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "easy",
                "stem": "【测试】缺答案备选 %d" % (i + 1),
                "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
                "answer": "",
            })
        c = self.nav("dashboard",
                     "document.getElementById('healthBox') && "
                     "document.getElementById('healthBox').textContent.indexOf('单项选择题')>=0")
        txt = re.sub(r"\s+", "", c.eval("document.getElementById('healthBox').textContent"))
        self.assertIn("单项选择题0/25", txt,
                      "缺答案的题被算成可用题量，健康度会误报够用：%r" % txt)
        self.assertIn("25道题缺答案", txt, "缺答案的题数没提示：%r" % txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
