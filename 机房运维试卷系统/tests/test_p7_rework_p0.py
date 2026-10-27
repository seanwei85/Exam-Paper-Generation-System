#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P7 补漏：需求书里写在 P0 但没落地的三处。

1) FR-QB-03 排序要能按「更新时间」（服务端支持 sort=updated，界面却没给入口）；
2) FR-QE-02 选项要能「拖动排序」（原来只能增删，改不了顺序）；
3) FR-PP-02 打印页脚要有「第 X 页 共 Y 页」。
"""

import base64
import json
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
import pdftext  # noqa: E402

OPT_ORDER = ("Array.from(document.querySelectorAll('#optArea .opt-row'))"
             ".map(r=>r.querySelector('.key').textContent+':'+r.querySelector('input').value)"
             ".join(',')")


class P7Api(HttpCase):
    def add(self, stem):
        cat = self.get_json("/api/categories")["categories"][0]
        return self.post_json("/api/questions", {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"], "qtype": "single",
            "diff": "mid", "stem": stem,
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "A", "analysis": "", "tags": "",
        })

    def test_sort_by_updated(self):
        a = self.add("先录的一道题，题干够长以便识别")
        b = self.add("后录的一道题，题干也够长以便识别")
        # 把先录的那道改成"刚改过"
        conn = self.core.connect()
        conn.execute("UPDATE question SET updated_at=? WHERE id=?",
                     (self.core.now_str(), a["id"]))
        conn.execute("UPDATE question SET updated_at=? WHERE id=?", ("2020-01-01 00:00:00", b["id"]))
        conn.commit()
        conn.close()

        r = self.get_json("/api/questions?sort=updated")
        self.assertEqual([it["id"] for it in r["items"]][0], a["id"],
                         "按更新时间排序时最近改过的应排在最前")


class P7Interact(RenderCase):
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

    # ------------------------------------------------------------ 排序入口

    def test_bank_has_updated_sort_option(self):
        """题库页排序下拉里要有「最近修改」，否则服务端支持也没人能用。"""
        c = self.goto("bank", "!!document.querySelector('.filters')")
        vals = c.eval("Array.from(document.querySelectorAll('.filters select option'))"
                      ".map(o=>o.value).join(',')")
        self.assertIn("updated", vals, "排序下拉缺少按更新时间排序：%s" % vals)
        labels = c.eval("Array.from(document.querySelectorAll('.filters select option'))"
                        ".map(o=>o.textContent).join(',')")
        self.assertIn("最近修改", labels)

    # ------------------------------------------------------------ 选项排序

    def open_editor(self):
        c = self.goto("editor", "document.querySelectorAll('#optArea .opt-row').length===4")
        for i, txt in enumerate(["甲选项", "乙选项", "丙选项", "丁选项"]):
            c.eval("(function(){var e=document.querySelectorAll('#optArea .opt-row input')[%d];"
                   "e.value=%s;e.dispatchEvent(new Event('input'));})()" % (i, json.dumps(txt)))
        self.assertEqual(c.eval(OPT_ORDER), "A:甲选项,B:乙选项,C:丙选项,D:丁选项")
        return c

    def test_drag_to_reorder_options(self):
        """把第一个选项拖到第三位：内容跟着走，标号重新从 A 排。"""
        c = self.open_editor()
        c.eval("(function(){"
               "var r=document.querySelectorAll('#optArea .opt-row');"
               "var dt=new DataTransfer();"
               "r[0].dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:dt}));"
               "r[2].dispatchEvent(new DragEvent('dragover',{bubbles:true,cancelable:true,dataTransfer:dt}));"
               "r[2].dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:dt}));"
               "return true;})()")
        c.wait(OPT_ORDER.replace(".join(',')", ".join(',')") + "==='A:乙选项,B:丙选项,C:甲选项,D:丁选项'",
               timeout=15)
        self.assertEqual(c.eval(OPT_ORDER), "A:乙选项,B:丙选项,C:甲选项,D:丁选项",
                         "拖动后选项顺序/标号不对")

    def test_move_buttons_reorder_options(self):
        """除了拖动，还要有可点（键盘也能用）的上移/下移。"""
        c = self.open_editor()
        c.eval("document.querySelectorAll('#optArea .opt-row')[3]"
               ".querySelector('.opt-up').click()")
        c.wait(OPT_ORDER + "==='A:甲选项,B:乙选项,C:丁选项,D:丙选项'", timeout=15)
        self.assertEqual(c.eval(OPT_ORDER), "A:甲选项,B:乙选项,C:丁选项,D:丙选项")

        c.eval("document.querySelectorAll('#optArea .opt-row')[0]"
               ".querySelector('.opt-down').click()")
        c.wait(OPT_ORDER + "==='A:乙选项,B:甲选项,C:丁选项,D:丙选项'", timeout=15)
        self.assertEqual(c.eval(OPT_ORDER), "A:乙选项,B:甲选项,C:丁选项,D:丙选项")

    # ------------------------------------------------------------ 打印页脚

    def test_print_page_footer_rule_survives_css_parser(self):
        """页脚「第 X 页 共 Y 页」要靠 @page 的 margin box 实现。

        只写字符串不算数——浏览器不认的规则会被 CSS 解析器整段丢掉，
        所以这里通过 CSSOM 读回真实解析结果，确认规则还在。
        """
        c = self.goto("papers", "!!document.querySelector('.page-head')")
        text = c.eval("(function(){var out=[];"
                      "function walk(rs){for(var i=0;i<rs.length;i++){var r=rs[i];"
                      "if(r.cssText&&r.cssText.indexOf('@page')>=0)out.push(r.cssText);"
                      "if(r.cssRules)walk(r.cssRules);}}"
                      "for(var i=0;i<document.styleSheets.length;i++){"
                      "try{walk(document.styleSheets[i].cssRules)}catch(e){}}"
                      "return out.join('\\n');})()")
        self.assertIn("@page", text, "样式表里找不到 @page 规则")
        self.assertIn("bottom-center", text,
                      "浏览器把页脚 margin box 规则丢掉了（该版本可能不支持）")
        self.assertIn("counter(pages)", text, "页脚缺少总页数")
        self.assertIn("counter(page)", text, "页脚缺少当前页号")


class P7Print(RenderCase):
    """打印真实结果断言：把卷子打成 PDF，再把文字抽出来看。

    只检查 DOM/CSS 是不够的——打印出来到底有没有侧栏、有没有多余空白页，
    只有看真实 PDF 才知道。抽取器见 tests/pdftext.py（纯标准库）。
    """

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
        self.seed(per_cell=2)  # 5 类 × 5 题型 × 3 难度 × 2 题
        cfg = self.get_json("/api/paper/default")
        cfg["rows"] = [
            {"qtype": "single", "enabled": True, "count": 8, "score": 3},
            {"qtype": "multi", "enabled": True, "count": 4, "score": 4},
            {"qtype": "judge", "enabled": True, "count": 6, "score": 2},
            {"qtype": "fill", "enabled": True, "count": 4, "score": 2},
            {"qtype": "qa", "enabled": True, "count": 2, "score": 10},
        ]
        cfg["mode"] = "auto"
        cfg["diff_mode"] = "points"
        cfg["diff_target"] = 0   # 全易：种子数据每格 2 道，稳够
        r = self.post_json("/api/papers", cfg)
        self.assertTrue(r.get("ok"), "组卷失败：%s" % (r.get("errors"),))
        return r["id"]

    def printed_pages(self, pid):
        c = self.chrome
        c.goto("http://127.0.0.1:%d/#paper-detail?id=%d" % (self.port, pid))
        c.wait("document.querySelectorAll('.a4').length>0", timeout=25)
        data = c.call("Page.printToPDF", printBackground=True,
                      paperWidth=8.27, paperHeight=11.69,
                      marginTop=0, marginBottom=0, marginLeft=0, marginRight=0)["data"]
        raw = base64.b64decode(data)
        return [re.sub(r"\s+", "", t) for t in pdftext.extract_text(raw)]

    def test_print_has_page_numbers_on_every_page(self):
        """每页页脚都要有「第 X 页 共 Y 页」，且 X 递增、Y 等于总页数。"""
        pages = self.printed_pages(self.build_paper())
        texts = [p for p in pages if p]
        self.assertGreaterEqual(len(texts), 2, "这道卷子应当跨页，实际只有 %d 页" % len(texts))
        total = len(texts)
        for i, t in enumerate(texts, 1):
            self.assertIn("第%d页共%d页" % (i, total), t,
                          "第 %d 页页脚不对（该页开头是：%s）" % (i, t[:40]))

    def test_print_contains_no_app_chrome(self):
        """卷面只能有试卷内容：侧栏导航、数据路径、页面标题与按钮都不能印上去。"""
        pages = self.printed_pages(self.build_paper())
        body = "".join(pages)
        for word in ("批量导入", "单题录入", "试卷库", "数据文件", "系统设置"):
            self.assertNotIn(word, body, "打印结果里混进了界面元素：%s" % word)
        self.assertNotIn("生成于", body, "页面标题区的副标题被印上去了")

    def test_print_has_no_trailing_blank_page(self):
        """最后不能多出一张空白页（侧栏 100vh 撑出来的那种）。"""
        pages = self.printed_pages(self.build_paper())
        self.assertTrue(pages[-1].strip(), "最后多了一张空白页，共 %d 页" % len(pages))

    def test_answer_sheet_prints_clean(self):
        """切到答案卷再打印：页脚在、答案内容在、界面元素不在。"""
        pid = self.build_paper()
        c = self.chrome
        c.goto("http://127.0.0.1:%d/#paper-detail?id=%d" % (self.port, pid))
        c.wait("document.querySelectorAll('.a4').length>0", timeout=25)
        self.click_text("标准答案")
        c.wait("!document.getElementById('ansWrap').classList.contains('hide')", timeout=15)

        data = c.call("Page.printToPDF", printBackground=True,
                      paperWidth=8.27, paperHeight=11.69,
                      marginTop=0, marginBottom=0, marginLeft=0, marginRight=0)["data"]
        pages = [re.sub(r"\s+", "", t)
                 for t in pdftext.extract_text(base64.b64decode(data))]
        body = "".join(pages)
        self.assertIn("第1页共", body, "答案卷没有页脚")
        self.assertIn("答案", body, "答案卷打印出来没有答案内容")
        for word in ("批量导入", "单题录入", "数据文件"):
            self.assertNotIn(word, body, "答案卷里混进了界面元素：%s" % word)

    def click_text(self, text):
        self.chrome.eval(
            "(function(){var bs=document.querySelectorAll('.tabs button');"
            "for(var i=0;i<bs.length;i++){if(bs[i].textContent.indexOf(%s)>=0){bs[i].click();return true;}}"
            "throw new Error('找不到按钮 '+%s);})()" % (json.dumps(text), json.dumps(text)))


if __name__ == "__main__":
    unittest.main(verbosity=2)