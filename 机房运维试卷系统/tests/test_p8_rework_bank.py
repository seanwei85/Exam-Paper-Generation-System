#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P8 补漏：题库管理三个 P1 项 + 单题录入查重。

- FR-QB-06 批量改分类/难度（原来只有批量删除）
- FR-QB-07 按当前筛选导出 Excel
- FR-QB-08 单题预览弹窗
- FR-QE-06 保存前重复检测（可"仍然保存"）
"""

import json
import os
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402

import importer  # noqa: E402

# 题干带「【测试】」前缀，避开首次启动自动铺的示例题库，判重结果才可预期
STEM_A = "【测试】冷冻水系统补水量应按蒸发量的百分之几估算（　）。"
STEM_B = "【测试】柴油发电机启动后应先空载暖机再带载。"


class P8Api(HttpCase):
    def cat(self, name="电气"):
        for c in self.get_json("/api/categories")["categories"]:
            if c["name"] == name:
                return c
        self.fail("缺少分类 " + name)

    def add(self, stem, qtype="single", cat="电气", allow_dup=True):
        c = self.cat(cat)
        return self.post_json("/api/questions", {
            "cat_id": c["id"], "sub_id": c["subs"][0]["id"], "qtype": qtype,
            "diff": "mid", "stem": stem,
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"] if qtype == "single" else [],
            "answer": "A" if qtype == "single" else "正确",
            "analysis": "解析内容", "tags": "", "allow_dup": allow_dup,
        })

    # ------------------------------------------------------ 导出

    def test_export_xlsx_has_columns_and_rows(self):
        self.add(STEM_A)
        self.add(STEM_B, qtype="judge")
        raw = urllib.request.urlopen(
            self.url("/api/questions/export"), timeout=20).read()
        self.assertTrue(raw.startswith(b"PK"), "导出结果不是 xlsx（缺少 zip 头）")

        rows = importer._xlsx_rows(raw)
        header = rows[0]
        self.assertEqual(header, ["题型", "分类", "二级分类", "难度", "题干", "选项", "答案", "解析"],
                         "导出的列与需求不一致")
        flat = json.dumps(rows, ensure_ascii=False)
        self.assertIn("冷冻水系统", flat)
        self.assertIn("柴油发电机", flat)
        # 值要转成中文名，而不是内部的 single/judge
        self.assertIn("单项选择题", flat)
        self.assertIn("判断题", flat)

    def test_export_respects_filters(self):
        self.add(STEM_A, cat="电气")
        self.add("消防主机报警后应先确认火情再处置。", cat="消防")
        c = self.cat("消防")
        raw = urllib.request.urlopen(
            self.url("/api/questions/export?cat=%d" % c["id"]), timeout=20).read()
        flat = json.dumps(importer._xlsx_rows(raw), ensure_ascii=False)
        self.assertIn("消防主机", flat)
        self.assertNotIn("冷冻水系统", flat, "导出没有按筛选过滤")

    def test_export_filename_is_chinese(self):
        resp = urllib.request.urlopen(self.url("/api/questions/export"), timeout=20)
        cd = resp.headers.get("Content-Disposition", "")
        self.assertIn("filename*=UTF-8''", cd, "缺少带中文名的下载头")

    # ------------------------------------------------------ 批量修改

    def test_bulk_change_category_resets_subcategory(self):
        """改一级分类时二级必须跟着换，否则会出现"消防/空调水系统"这种脏数据。"""
        qid = self.add(STEM_A)["id"]
        fire = self.cat("消防")
        r = self.post_json("/api/questions/bulk", {
            "action": "update", "field": "cat_id", "value": fire["id"], "ids": [qid]})
        self.assertTrue(r.get("ok"), r)

        conn = self.core.connect()
        row = conn.execute("SELECT cat_id, sub_id FROM question WHERE id=?", (qid,)).fetchone()
        ok = conn.execute("SELECT 1 FROM subcategory WHERE id=? AND cat_id=?",
                          (row["sub_id"], fire["id"])).fetchone()
        conn.close()
        self.assertEqual(row["cat_id"], fire["id"])
        self.assertIsNotNone(ok, "改分类后二级分类还挂在旧分类下")

    def test_bulk_change_diff(self):
        qid = self.add(STEM_A)["id"]
        r = self.post_json("/api/questions/bulk", {
            "action": "update", "field": "diff", "value": "hard", "ids": [qid]})
        self.assertTrue(r.get("ok"), r)
        conn = self.core.connect()
        self.assertEqual(conn.execute("SELECT diff FROM question WHERE id=?", (qid,)).fetchone()["diff"], "hard")
        conn.close()

    def test_bulk_rejects_bad_value(self):
        qid = self.add(STEM_A)["id"]
        r = self.post_json("/api/questions/bulk", {
            "action": "update", "field": "diff", "value": "很难", "ids": [qid]})
        self.assertFalse(r.get("ok"), "非法难度值应当被拒绝")
        conn = self.core.connect()
        self.assertEqual(conn.execute("SELECT diff FROM question WHERE id=?", (qid,)).fetchone()["diff"],
                         "mid", "非法值居然把数据改坏了")
        conn.close()

    def test_bulk_rejects_unknown_category(self):
        qid = self.add(STEM_A)["id"]
        r = self.post_json("/api/questions/bulk", {
            "action": "update", "field": "cat_id", "value": 99999, "ids": [qid]})
        self.assertFalse(r.get("ok"), "不存在的分类应当被拒绝")

    # ------------------------------------------------------ 单题查重

    def test_save_requires_confirm_on_duplicate(self):
        self.add(STEM_A)
        r = self.add(STEM_A.replace("（　）", "( )"), allow_dup=False)
        self.assertFalse(r.get("ok"), "重复题干直接存进去了")
        self.assertTrue(r.get("need_dup_confirm"), r)
        self.assertIn("冷冻水系统", r["dup"]["stem"])
        self.assertGreaterEqual(r["dup"]["score"], 0.9)

    def test_save_with_allow_dup_works(self):
        self.add(STEM_A)
        r = self.post_json("/api/questions", {
            "cat_id": self.cat()["id"], "sub_id": self.cat()["subs"][0]["id"],
            "qtype": "single", "diff": "mid", "stem": STEM_A,
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "A", "analysis": "", "tags": "", "allow_dup": True})
        self.assertTrue(r.get("ok"), r)

    def test_save_update_does_not_flag_itself(self):
        """改自己不能被判成"和自己重复"。"""
        qid = self.add(STEM_A)["id"]
        r = self.post_json("/api/questions", {
            "id": qid, "cat_id": self.cat()["id"], "sub_id": self.cat()["subs"][0]["id"],
            "qtype": "single", "diff": "hard", "stem": STEM_A + "（改了难度）",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "A", "analysis": "", "tags": ""})
        self.assertTrue(r.get("ok"), "编辑自身被误判为重复：%s" % (r,))


class P8Interact(RenderCase):
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

    def add(self, stem, qtype="single", cat="电气"):
        cats = self.get_json("/api/categories")["categories"]
        c = next(x for x in cats if x["name"] == cat)
        return self.post_json("/api/questions", {
            "cat_id": c["id"], "sub_id": c["subs"][0]["id"], "qtype": qtype, "diff": "mid",
            "stem": stem,
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"] if qtype == "single" else [],
            "answer": "A" if qtype == "single" else "正确", "analysis": "这是解析内容",
            "tags": "", "allow_dup": True,
        })

    def goto(self, hash_path, wait):
        """导航并等新文档就绪（序号由基类保证唯一，见 renderbase.RenderCase.nav）。"""
        return self.nav(hash_path, wait)

    # ------------------------------------------------------ 预览

    def test_preview_shows_full_question(self):
        self.add(STEM_A)
        c = self.goto("bank", "document.querySelectorAll('tbody tr').length>=1")
        c.eval("document.querySelector('.pv-btn').click()")
        c.wait("!!document.getElementById('qPreview')", timeout=15)
        text = c.eval("document.getElementById('qPreview').innerText")
        self.assertIn("冷冻水系统", text, "预览里没有题干")
        self.assertIn("选项A", text, "预览里没有选项")
        self.assertIn("答案", text)
        self.assertIn("解析", text)
        self.assertIn("积分", text, "预览里没有难度积分")
        c.eval("document.querySelector('#qPreview .modal-f button, #qPreview .close').click()")
        c.wait("!document.getElementById('qPreview')", timeout=15)

    # ------------------------------------------------------ 批量修改

    def test_bulk_change_category_through_ui(self):
        self.add(STEM_A)
        self.add(STEM_B, qtype="judge")
        c = self.goto("bank", "document.querySelectorAll('tbody tr').length>=2")
        c.eval("document.querySelectorAll('.pick').forEach(x=>x.checked=true)")
        c.eval("document.querySelector('#bulkBtn').click()")
        c.wait("!!document.getElementById('bulkPanel')", timeout=15)
        fire_id = c.eval("(function(){var c=STATE.cats.find(x=>x.name==='消防');return c?c.id:0;})()")
        c.eval("(function(){var s=document.querySelector('#bulkPanel .b-cat');"
               "s.value=String(%d);s.dispatchEvent(new Event('change'));})()" % fire_id)
        c.eval("document.querySelector('#bulkPanel .b-apply').click()")
        # 等面板关掉（applyBulk 是异步的，面板关闭才代表请求已返回并重渲染完）
        c.wait("!document.getElementById('bulkPanel')", timeout=20)
        c.wait("(function(){var n=Array.from(document.querySelectorAll('tbody tr'))"
               ".filter(tr=>tr.innerText.indexOf('消防')>=0).length;return n===2;})()", timeout=15)
        # 两行都应变成消防
        n = c.eval("Array.from(document.querySelectorAll('tbody tr'))"
                   ".filter(tr=>tr.innerText.indexOf('消防')>=0).length")
        self.assertEqual(n, 2, "批量改分类没有作用到所有勾选的题")

    # ------------------------------------------------------ 查重提示

    def total(self):
        with urllib.request.urlopen(self.url("/api/state"), timeout=10) as r:
            return json.loads(r.read())["stats"]["total"]

    def test_editor_warns_duplicate_and_allows_force_save(self):
        self.add(STEM_A)
        base = self.total()
        c = self.goto("editor", "!!document.getElementById('eStem')")
        c.eval("(function(){var e=document.getElementById('eStem');e.value=%s;"
               "e.dispatchEvent(new Event('input'));})()" % json.dumps(STEM_A))
        # 选项内容要填，否则会先被"选择题至少 2 个选项"拦住，根本走不到查重
        for i, txt in enumerate(["甲", "乙", "丙", "丁"]):
            c.eval("(function(){var e=document.querySelectorAll('#optArea .opt-row input')[%d];"
                   "e.value=%s;e.dispatchEvent(new Event('input'));})()" % (i, json.dumps(txt)))
        c.eval("(function(){var e=document.getElementById('eAnswer');e.value='A';"
               "e.dispatchEvent(new Event('input'));})()")
        c.eval("document.querySelector('.save-btn').click()")
        c.wait("!!document.getElementById('dupWarn')", timeout=20)
        warn = c.eval("document.getElementById('dupWarn').innerText")
        self.assertIn("相似", warn, "查重提示没有说明是相似题")
        self.assertIn("冷冻水系统", warn)

        # 未确认前不应入库
        self.assertEqual(self.total(), base, "提示了却还是存进去了")

        c.eval("document.querySelector('#dupWarn .force-save').click()")
        c.wait("!document.getElementById('dupWarn')", timeout=20)
        self.assertEqual(self.total(), base + 1, "点「仍然保存」没有存进去")
        self.assertIn("已保存", c.eval("document.getElementById('toast').textContent"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
