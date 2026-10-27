#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 批量导入页端到端渲染测试。

覆盖两个曾经真实发生的缺陷：
  A. 路由带 ?demo=1 时参数不被消费，parseB64 成功后 route() 又命中 demo，
     对 /api/import/parse 形成循环请求 —— 页面上表现为「正在解析…」与结果表同时存在；
  B. 识别结果表既渲染在页面尾部、又被 rerenderImport() 写进 #impStage，
     勾选/改分类后出现两份表。
所以这里的断言不只看"有没有渲染出来"，还要求：只渲染一次、且落在 #impStage 内、
且不存在残留的解析中提示。
"""

import base64
import json
import os
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from renderbase import RenderCase  # noqa: E402


class P2ImportRender(RenderCase):
    def setUp(self):
        super().setUp()
        # 导入页只依赖分类字典，不需要铺题库

    # ------------------------------------------------------------ 用例

    def test_empty_import_page(self):
        html = self.dom_text("import")
        self.assertIn("拖入题库文件，或点击选择", html, "缺少拖拽区")
        self.assertIn("载入示例看看效果", html, "缺少示例入口")
        self.assertNotIn("识别结果", html, "未选择文件时不应出现识别结果表")

    def test_demo_parsed_and_rendered_once(self):
        """示例走完整链路：前端 base64 → 后端解析 → 表格渲染。"""
        html = self.dom_text("import?demo=1")

        self.assertIn("识别结果", html, "示例解析后没有渲染识别结果表")
        # 缺陷 B：只允许出现一份结果表
        self.assertEqual(html.count("识别结果"), 1,
                         "识别结果表渲染了 %d 次（应为 1 次）" % html.count("识别结果"))
        # 缺陷 A：解析完成后不应残留"正在解析…"，残留即说明有请求在原路打转
        self.assertNotIn("正在解析", html, "页面残留解析中提示，说明解析请求在循环")

    def test_demo_result_content(self):
        """识别结果的内容必须与示例题库逐项对上。"""
        html = self.dom_text("import?demo=1")

        self.assertIn("共 6 题，已选 6 题", html, "题数统计不对")
        # 六道题的题干都在
        for stem in ("加湿用水", "后备时间", "机房消防系统包括", "频繁启停",
                     "湿度 40%", "市电中断后的应急处置"):
            self.assertIn(stem, html, "缺少题干：" + stem)
        # 选择题答案与填空题多空答案（填空题多空用竖线分隔存储）
        self.assertIn('value="B"', html, "单选题答案未识别")
        self.assertIn('value="ABD"', html, "多选题答案未识别")
        self.assertIn('value="27|60"', html, "填空题多空答案未识别")
        # 表头七列齐全
        for col in ("题型", "题干", "选项", "难度", "分类"):
            self.assertIn(">%s</th>" % col, html, "缺少表头列：" + col)

    def test_result_table_inside_stage(self):
        """结果表必须落在 #impStage 内 —— 这是避免重渲染出两份表的唯一容器约定。"""
        html = self.dom_text("import?demo=1")
        idx = html.find('id="impStage"')
        self.assertNotEqual(idx, -1, "页面缺少 #impStage 容器")
        self.assertIn("识别结果", html[idx:],
                      "结果表没有渲染在 #impStage 内，rerender 时会与页面尾部那份重复")

    def test_demo_rows_editable(self):
        """每一行都要可编辑：题型/难度/分类是下拉，题干/答案是输入框。"""
        html = self.dom_text("import?demo=1")
        self.assertEqual(html.count('class="stem-in"'), 6, "题干输入框数量应为 6")
        # 六行 × 3 个下拉（题型/难度/分类）= 18
        self.assertGreaterEqual(html.count("<select"), 18, "行内下拉数量不足")

    def test_template_download_closes_the_loop(self):
        """下载模板 → 原样提交给解析接口 → 必须认出 5 道题。

        这是"下载模板、填好、导回"整条链路的验收：模板本身读不回来，模板就等于没用。
        """
        base = "http://127.0.0.1:%d" % self.port
        with urllib.request.urlopen(base + "/api/import/template", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("spreadsheetml.sheet", resp.headers.get("Content-Type", ""),
                          "模板的 Content-Type 不对，浏览器会当文本打开")
            self.assertIn("attachment", resp.headers.get("Content-Disposition", ""),
                          "没有强制下载")
            raw = resp.read()
        self.assertTrue(raw.startswith(b"PK"), "下载到的不是 xlsx")

        payload = json.dumps({
            "filename": "机房运维题库导入模板.xlsx",
            "content": base64.b64encode(raw).decode("ascii"),
        }).encode("utf-8")
        req = urllib.request.Request(base + "/api/import/parse", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            res = json.loads(resp.read())
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(len(res["items"]), 5, "模板示例应解析出 5 道题")
        self.assertEqual(res["warnings"], [], "模板示例不应有识别告警")
        types = [i["qtype"] for i in res["items"]]
        self.assertEqual(types, ["single", "multi", "judge", "fill", "qa"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
