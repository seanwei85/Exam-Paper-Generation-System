#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 导入页交互测试：真点按钮、真改输入框。

--dump-dom 只能看静态结构，删行、标黄、入库拦截这些是"点了才知道"的行为，
所以这里用 CDP 驱动无头 Chrome 做真实交互。
"""

import json
import os
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402

ROWS = "document.querySelectorAll('.imp-table tbody tr').length"


class P2ImportInteract(RenderCase):
    """一个浏览器实例跨用例复用；每条用例都从干净的示例页开始。"""

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

    # ------------------------------------------------------------ 辅助

    def open_demo(self):
        # 带一个变化的查询串强制整页重载：只改 hash 的话浏览器不会重新加载文档，
        # 上一用例残留的 IMP 状态会串进来。序号由基类按类级自增，不会和上一个
        # 用例撞成同一个 URL（实例级计数器每个用例都从 1 开始，等于没变）。
        return self.nav("import?demo=1", ROWS + "===6", timeout=20)

    def count_text(self):
        return self.chrome.eval("document.getElementById('impCount').textContent")

    def toast_text(self):
        return self.chrome.eval(
            "(function(){var t=document.getElementById('toast');"
            "return t.classList.contains('on')?t.textContent:'';})()")

    def click(self, selector):
        self.chrome.eval(
            "(function(){var e=document.querySelector(%s);"
            "if(!e) throw new Error('找不到元素'); e.click(); return true;})()" % json.dumps(selector))

    def set_value(self, selector, value):
        """改值并派发 change —— onchange 才是页面真正监听的事件。"""
        self.chrome.eval(
            "(function(){var e=document.querySelector(%s);"
            "e.value=%s;e.dispatchEvent(new Event('change'));return e.value;})()"
            % (json.dumps(selector), json.dumps(value)))

    def add_question(self, stem, qtype="single", answer="B", options=None):
        """直接往库里塞一道题，用来制造"导入的题和它重复"。"""
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
        conn.close()

    # ------------------------------------------------------------ 用例

    def test_dup_row_marked_and_unselected_by_default(self):
        """库里已有同一道题：该行标出来、默认不勾选、表头给汇总。"""
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        c = self.open_demo()

        tr = "document.querySelector('.imp-table tbody tr[data-row=\"0\"]')"
        self.assertTrue(c.eval(tr + ".classList.contains('dup-row')"),
                        "重复行没有加标记样式")
        self.assertFalse(c.eval(tr + ".querySelector('input[type=checkbox]').checked"),
                         "重复行应当默认不勾选")
        self.assertEqual(c.eval(tr + ".querySelector('.dup-tag') && "
                                    + tr + ".querySelector('.dup-tag').textContent"), "疑重复")
        title = c.eval(tr + ".querySelector('.dup-tag').getAttribute('title')")
        self.assertIn("题库已有", title, "标记要说清为什么判重")
        self.assertIn("加湿用水", title, "标记里应带出题库里那道题的题干")

        self.assertEqual(self.count_text(), "共 6 题，已选 5 题")
        note = c.eval("document.getElementById('impDupNote').textContent")
        self.assertIn("1 题与题库疑似重复", note)
        self.assertIn("默认未勾选", note)

    def test_dup_can_still_be_imported_on_demand(self):
        """判重只是提示，不是禁止：用户点全选后仍能把它导进去。"""
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        c = self.open_demo()
        self.assertIn("已选 5 题", self.count_text())

        self.click(".card-h .actions button:first-child")  # 全选
        self.assertEqual(self.count_text(), "共 6 题，已选 6 题")
        self.click(".card-h .actions button.primary")
        c.wait("location.hash.indexOf('bank')>=0", timeout=20)
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            st = json.loads(r.read())
        self.assertEqual(st["stats"]["total"], 7, "全选后应把重复题也一起入库（1 已有 + 6 新）")

    def test_import_log_listed_on_page(self):
        """导入页要能看到最近导入记录：文件名 + 成功/失败题数。"""
        # 只等元素出现不够：容器先渲染成「正在加载…」，记录要等接口回来才有内容，
        # 断言会打在占位文字上。等"占位文字消失"才算真加载完。
        c = self.nav("import", "document.getElementById('impLogs') && "
                               "document.getElementById('impLogs').textContent.indexOf('正在加载')<0",
                     timeout=20)
        self.assertIn("暂无导入记录", c.eval("document.getElementById('impLogs').textContent"),
                      "没导过东西时应明确显示暂无记录，而不是空着")

        # 用接口真导一次，制造一条记录
        cat = self.get_json("/api/categories")["categories"][0]
        self.post_json("/api/import/commit", {"filename": "十月题库.xlsx", "items": [{
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"], "qtype": "single",
            "diff": "mid", "stem": "柴油发电机启动后应先空载暖机，对还是错？",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "B", "analysis": "",
        }]})

        c = self.nav("import",
                     "document.getElementById('impLogs') && "
                     "document.getElementById('impLogs').textContent.indexOf('十月题库')>=0",
                     timeout=20)
        logs = c.eval("document.getElementById('impLogs').textContent")
        self.assertIn("成功 1", logs)
        self.assertIn("失败 0", logs)

    def test_commit_skips_duplicates_when_left_unticked(self):
        """按默认（不勾重复题）提交，入库 5 题，不产生第二份重复。"""
        self.add_question("机房精密空调的加湿用水应使用（　）。")
        c = self.open_demo()
        self.click(".card-h .actions button.primary")
        c.wait("location.hash.indexOf('bank')>=0", timeout=20)
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            st = json.loads(r.read())
        self.assertEqual(st["stats"]["total"], 6, "默认提交应只入库 5 题")
        conn = self.core.connect()
        n = conn.execute("SELECT COUNT(*) AS c FROM question WHERE stem LIKE '%加湿用水%'"
                         ).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1, "库里出现了两份重复题")

    def test_delete_row_keeps_selection_aligned(self):
        """删掉一行后：行数减一、被删题干消失、其余勾选状态跟着左移。"""
        c = self.open_demo()
        # 先取消第 2 行（UPS 那道）的勾选，再删掉第 1 行，检验下标重排
        c.eval("document.querySelectorAll('.imp-table tbody tr')[1]"
               ".querySelector('input[type=checkbox]').click()")
        self.assertEqual(self.count_text(), "共 6 题，已选 5 题")

        deleted = c.eval("document.querySelectorAll('.imp-table tbody tr')[0]"
                         ".querySelector('.stem-in').value")
        self.click(".imp-table tbody tr[data-row='0'] .del-btn")

        c.wait(ROWS + "===5")
        self.assertEqual(self.count_text(), "共 5 题，已选 4 题",
                         "删除后勾选数没跟着重排")
        self.assertNotIn(deleted, c.eval("document.body.innerText"),
                         "被删的题还留在表格里")
        # 原先第 2 行（未勾选）现在应该是第一行，且仍未勾选
        first = c.eval("(function(){var tr=document.querySelectorAll('.imp-table tbody tr')[0];"
                       "return {stem:tr.querySelector('.stem-in').value,"
                       "checked:tr.querySelector('input[type=checkbox]').checked};})()")
        self.assertFalse(first["checked"], "未勾选状态没跟着行一起左移")
        self.assertIn("后备时间", first["stem"], "删除后行顺序不对")

    def test_missing_answer_marks_row_and_commits_as_draft(self):
        """清空答案：该行标黄 + 顶部提示缺答案；确认后仍能入库，只是记为草稿。

        老题库（厂商模板导出的面试题库）常常整份没有答案列，一律拦下等于白导，
        所以这里拦的是"缺题干"，缺答案只提醒、可入库。
        """
        c = self.open_demo()
        self.set_value(".imp-table tbody tr[data-row='1'] .c-ans input", "")

        self.assertTrue(c.eval("document.querySelector('.imp-table tbody tr[data-row=\"1\"]')"
                               ".classList.contains('warn-ans')"),
                        "缺答案的行没有标黄")
        self.assertEqual(c.eval("document.querySelector('.imp-table tbody tr[data-row=\"1\"]')"
                               ".querySelector('.c-ans input').getAttribute('placeholder')"),
                         "缺答案")
        self.assertIn("1 题缺答案", self.count_text())
        self.assertIn("可先入库再补", self.count_text(), "提示没说明可以先入库")

        # 入库前会弹确认框；无头浏览器里直接放行，模拟用户点"继续"
        c.eval("window.confirm=function(){return true;}")
        self.click(".card-h .actions button.primary")
        c.wait("location.hash.indexOf('bank')>=0", timeout=20)
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            st = json.loads(r.read())
        self.assertEqual(st["stats"]["total"], 6, "缺答案的题被挡在库外了")
        self.assertEqual(st["stats"]["draft"], 1, "缺答案的题没有标成草稿")

    def test_missing_stem_still_blocks_commit(self):
        """缺题干才是真的不成题：必须拦下并指出第几题。"""
        c = self.open_demo()
        self.set_value(".imp-table tbody tr[data-row='3'] .stem-in", "")
        self.click(".card-h .actions button.primary")
        text = self.toast_text()
        self.assertIn("缺题干", text, "缺题干没有被拦下")
        self.assertIn("4", text, "没有指出是第几题")
        self.assertEqual(c.eval(ROWS), 6, "被拦下后题目不该消失")
        self.assertIn("识别结果", c.eval("document.body.innerText"))

    def test_fill_answer_clears_warning(self):
        """补上答案后标黄立即消失，不需要刷新页面。"""
        c = self.open_demo()
        self.set_value(".imp-table tbody tr[data-row='2'] .c-ans input", "")
        self.assertIn("1 题缺答案", self.count_text())

        self.set_value(".imp-table tbody tr[data-row='2'] .c-ans input", "ABD")
        self.assertFalse(c.eval("document.querySelector('.imp-table tbody tr[data-row=\"2\"]')"
                                ".classList.contains('warn-ans')"), "补全后标黄没消掉")
        self.assertEqual(self.count_text(), "共 6 题，已选 6 题")

    def test_delete_all_returns_to_picker(self):
        """把行删光要回到选择文件的状态，不能留一张空表。"""
        c = self.open_demo()
        for _ in range(6):
            self.click(".imp-table tbody tr .del-btn")
            c.wait("document.querySelectorAll('.imp-table tbody tr').length<" + str(7 - _))
        c.wait("document.querySelectorAll('.imp-table').length===0")
        self.assertIn("拖入题库文件", c.eval("document.body.innerText"))

    def test_commit_through_ui_persists(self):
        """在页面上点"确认入库"，题目要真的进库（走完前端 → 接口 → 数据库）。"""
        c = self.open_demo()
        self.click(".card-h .actions button.primary")
        c.wait("location.hash.indexOf('bank')>=0", timeout=20)
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % self.port, timeout=10) as r:
            st = json.loads(r.read())
        self.assertEqual(st["stats"]["total"], 6, "示例 6 题没有全部入库")

    def test_category_change_moves_subcategory(self):
        """改一级分类，二级分类必须跟着换到该分类下，不能留着上一个分类的二级。"""
        c = self.open_demo()
        cid = c.eval("(function(){var x=STATE.cats.find(y=>y.name==='消防');return x?x.id:null;})()")
        first_sub = c.eval("(function(){var x=STATE.cats.find(y=>y.name==='消防');"
                           "return x&&x.subs.length?x.subs[0].id:null;})()")
        self.assertIsNotNone(cid, "种子分类里应有「消防」")

        self.set_value(".imp-table tbody tr[data-row='0'] .c-cat select", str(cid))
        picked = c.eval("Number(document.querySelector('.imp-table tbody tr[data-row=\"0\"] "
                        ".c-sub select').value)")
        self.assertEqual(picked, first_sub, "二级分类没有跟着一级分类切换")
        # 界面上也要真的显示成消防，而不是只有数据变了
        self.assertEqual(c.eval("document.querySelector('.imp-table tbody tr[data-row=\"0\"] "
                                ".c-cat select').selectedOptions[0].textContent"), "消防")

    def test_invalid_answer_reports_inline_not_alert(self):
        """答案非法时：留在本页、在表格上方列出未入库原因，不弹阻塞式对话框。"""
        c = self.open_demo()
        self.set_value(".imp-table tbody tr[data-row='0'] .c-ans input", "Z")
        self.click(".card-h .actions button.primary")
        c.wait("document.body.innerText.indexOf('题未入库')>=0", timeout=20)
        body = c.eval("document.body.innerText")
        self.assertIn("答案中的选项不在选项范围内", body, "没有给出具体失败原因")
        self.assertIn("识别结果", body, "失败后应留在导入页继续改")
        # 失败那行被取消勾选，避免原样再提交一次
        self.assertIn("已选 5 题", self.count_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
