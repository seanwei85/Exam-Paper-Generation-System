#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 测试：txt / csv / docx / xlsx 拆题、字段归一、异常格式提示、入库。"""

import io
import os
import sys
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import BaseCase

import importer

CATS = [
    {"id": 1, "name": "电气"}, {"id": 2, "name": "暖通"}, {"id": 3, "name": "弱电"},
    {"id": 4, "name": "消防"}, {"id": 5, "name": "管理"},
]

SAMPLE = """一、单项选择题
1. 机房精密空调的加湿用水应使用（　）。
A. 自来水
B. 软化水
C. 海水
D. 中水
答案：B
解析：自来水硬度高，易在加湿罐结垢。

2. 【难】UPS 电池组的后备时间取决于（　）。
A. 负载大小
B. 机房面积
C. 维护周期
D. 室温
【答案】A

二、多项选择题
3. 机房消防系统包括（　）。
A. 气体灭火
B. 火灾报警
C. 门禁
D. 灭火器材
答案：ABD

三、判断题
4. 精密空调可以频繁启停。
答案：错误

四、填空题
5. 机房温度应控制在 18~____ ℃，湿度 40%~____。
答案：27|60

五、问答题
6. 简述市电中断后的应急处置流程。
答案：确认告警→启动柴油发电机→切换负荷→持续监控
"""


def make_docx(paragraphs):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           + "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p.replace("&", "&amp;").replace("<", "&lt;")
                     for p in paragraphs)
           + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_xlsx(rows):
    shared = []
    index = {}

    def sid(text):
        if text not in index:
            index[text] = len(shared)
            shared.append(text)
        return index[text]

    body = []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(row):
            if val is None or val == "":
                continue
            col = chr(65 + c)
            cells.append('<c r="%s%d" t="s"><v>%d</v></c>' % (col, r, sid(str(val))))
        body.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    sheet = ('<?xml version="1.0"?><worksheet><sheetData>' + "".join(body) + "</sheetData></worksheet>")
    shared_xml = ('<?xml version="1.0"?><sst>' +
                  "".join("<si><t>%s</t></si>" % s.replace("&", "&amp;") for s in shared) +
                  "</sst>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/sharedStrings.xml", shared_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


class ImportParse(unittest.TestCase):
    def parse(self, text, filename="题库.txt"):
        return importer.parse(text.encode("utf-8"), filename, cats=CATS)

    def test_split_five_types(self):
        res = self.parse(SAMPLE)
        items = res["items"]
        self.assertEqual(len(items), 6, [i["stem"][:10] for i in items])
        self.assertEqual([i["qtype"] for i in items],
                         ["single", "single", "multi", "judge", "fill", "qa"])

    def test_options_and_answers(self):
        items = self.parse(SAMPLE)["items"]
        self.assertEqual([o["key"] for o in items[0]["options"]], ["A", "B", "C", "D"])
        self.assertEqual(items[0]["answer"], "B")
        self.assertIn("结垢", items[0]["analysis"])
        self.assertEqual(items[2]["answer"], "ABD")
        self.assertEqual(items[3]["answer"], "错误")
        self.assertEqual(items[4]["answer"], "27|60")

    def test_difficulty_marker(self):
        items = self.parse(SAMPLE)["items"]
        self.assertEqual(items[1]["diff"], "hard")
        self.assertNotIn("【难】", items[1]["stem"])
        self.assertEqual(items[0]["diff"], "mid")

    def test_category_detected(self):
        items = self.parse(SAMPLE)["items"]
        self.assertEqual(items[2]["cat_id"], 4, "含「消防」的题应识别为消防类")

    def test_judge_variants(self):
        text = """三、判断题
1. 机房可以堆放杂物。
答案：错
2. 灭火器应定期检查。
答案：√
"""
        items = self.parse(text)["items"]
        self.assertEqual(items[0]["answer"], "错误")
        self.assertEqual(items[1]["answer"], "正确")

    def test_number_own_line_and_inline_answer(self):
        """厂商模板常见版式：题号独占一行，答案直接写在题干末尾的括号里。"""
        text = """90#暖通精密空调试题

1
90#精密空调密码（   C  ）

A.1111
B.6656
C.3388

2
90#精密空调群控模式下送风温度设定是否群控（ B    ）

A.是
B.否
C.以上皆可以

3
机房可以堆放杂物。（√）

"""
        items = self.parse(text)["items"]
        self.assertEqual(len(items), 3, [i["stem"][:16] for i in items])
        self.assertEqual(items[0]["answer"], "C")
        self.assertEqual(items[0]["qtype"], "single")
        # 题干里的字母被挖空成空白括号，答案单独存字段
        self.assertEqual(items[0]["stem"], "90#精密空调密码（　　）")
        self.assertEqual(items[1]["answer"], "B")
        self.assertEqual(items[2]["answer"], "正确")
        self.assertEqual(items[2]["qtype"], "judge")

    def test_inline_multi_answer(self):
        """括号里一次给多个字母 = 多选。"""
        text = """1
机房消防系统包括哪些（ABD）
A. 气体灭火
B. 火灾报警
C. 门禁
D. 灭火器材
"""
        items = self.parse(text)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["qtype"], "multi")
        self.assertEqual(items[0]["answer"], "ABD")
        self.assertEqual(items[0]["stem"], "机房消防系统包括哪些（　　）")

    def test_explicit_answer_wins_over_inline(self):
        """题干里有括号也有答案行时，以答案行为准，括号内容不吞。"""
        text = """1
下列哪项正确（X）
A. 甲
B. 乙
答案：B
"""
        items = self.parse(text)["items"]
        self.assertEqual(items[0]["answer"], "B")
        self.assertIn("（X）", items[0]["stem"], "有明确答案行时不应动题干里的括号")

    def test_inline_answer_mid_stem(self):
        """答案括号在题干中间（厂商模板常见）：……（ B ）m³ 这种也要抠出来。"""
        text = """1
蓄冷池（  B  ）m³
A.2000
B.3000
C.4000

2
变频器上需要修改为（  A  ）模式后才能调速
A.Rem
B.Loc
C.CCN
"""
        items = self.parse(text)["items"]
        self.assertEqual(items[0]["answer"], "B")
        self.assertEqual(items[0]["stem"], "蓄冷池（　　）m³")
        self.assertEqual(items[1]["answer"], "A")
        self.assertEqual(items[1]["stem"], "变频器上需要修改为（　　）模式后才能调速")

    def test_inline_answer_ambiguous_parens_skipped(self):
        """题干里有多个带字母的括号时不好判断哪个是答案，宁可不入库当草稿。"""
        text = """1
共（A）台空调，已开启（B）台
A.2
B.3
C.4
"""
        items = self.parse(text)["items"]
        self.assertEqual(items[0]["answer"], "", "多括号歧义时不应乱猜")
        self.assertIn("（A）", items[0]["stem"])

    def test_number_restart_per_sheet(self):
        """多工作表各自从 1 编号：上一题已完整（有选项/答案）时，孤立的 1 应重新开题。"""
        text = """1
第一张表最后一题。
A. 甲
B. 乙
答案：A

1
第二张表第一题（ C ）。
A. 丙
B. 丁
C. 戊
"""
        items = self.parse(text)["items"]
        self.assertEqual(len(items), 2, [i["stem"][:12] for i in items])
        self.assertEqual(items[1]["stem"], "第二张表第一题（　　）。")
        self.assertEqual(items[1]["answer"], "C")

    def test_lone_number_not_question(self):
        """孤立的数字若不接续序号（如页码），不能当成新题开题。"""
        text = """1. 正常题目甲。
A. 选项一
B. 选项二
答案：A

7
3. 下一道正常题。
答案：B
"""
        items = self.parse(text)["items"]
        self.assertEqual(len(items), 2, [i["stem"][:14] for i in items])
        self.assertEqual(items[1]["stem"], "下一道正常题。")

    def test_missing_answer_warns(self):
        text = """一、单项选择题
1. 没有答案的题（　）。
A. 甲
B. 乙
"""
        res = self.parse(text)
        self.assertTrue(res["items"], "缺答案的题仍应先解析出来")
        self.assertEqual(res["items"][0]["answer"], "")
        # 只有一两题时逐条说更清楚；整份缺答案的汇总提示见下一个用例。
        self.assertTrue(any("没有识别到答案" in w for w in res["warnings"]), res["warnings"])

    def test_missing_answer_warns_one_line_only(self):
        """整份都没答案时只给一条汇总提示，不要逐题刷几百条。"""
        text = "".join(
            "%d. 缺答案第 %d 题（　）。\nA. 甲\nB. 乙\n" % (i, i) for i in range(1, 31))
        res = self.parse("一、单项选择题\n" + text)
        hits = [w for w in res["warnings"] if "答案" in w]
        self.assertEqual(len(hits), 1, "缺答案提示刷屏了：%d 条" % len(hits))
        self.assertIn("30 题", hits[0])

    def test_docx(self):
        paras = [p for p in SAMPLE.split("\n")]
        raw = make_docx(paras)
        res = importer.parse(raw, "题库.docx", cats=CATS)
        self.assertEqual(len(res["items"]), 6)
        self.assertEqual(res["items"][0]["answer"], "B")

    def test_xlsx(self):
        rows = [
            ["一、单项选择题"],
            ["1. 机房精密空调加湿用水应使用（　）。", "A. 自来水", "B. 软化水", "C. 海水", "D. 中水", "答案：B"],
            ["2. UPS 电池后备时间取决于（　）。", "A. 负载大小", "B. 机房面积", "C. 维护周期", "D. 室温", "答案：A"],
        ]
        raw = make_xlsx(rows)
        res = importer.parse(raw, "题库.xlsx", cats=CATS)
        self.assertEqual(len(res["items"]), 2, [i["stem"][:12] for i in res["items"]])
        self.assertEqual(res["items"][0]["qtype"], "single")
        self.assertEqual(res["items"][0]["answer"], "B")

    def test_csv_table(self):
        text = "题干,A,B,C,D,答案\n1. 两空题 ____ 与 ____,A. 甲,B. 乙,C. 丙,D. 丁,答案：AB\n"
        res = importer.parse(text.encode("utf-8"), "题库.csv", cats=CATS)
        self.assertGreaterEqual(len(res["items"]), 1)

    def test_pdf_rejected(self):
        with self.assertRaises(importer.ImportError_) as ctx:
            importer.parse(b"%PDF-1.4", "题库.pdf", cats=CATS)
        self.assertIn("PDF", str(ctx.exception))

    def test_fake_doc_rejected(self):
        """扩展名是 .doc 但内容不是 Word：要说清"这不是 Word 文档"，
        而不是让用户去猜"另存为 docx"（他手上本来就是 doc）。"""
        with self.assertRaises(importer.ImportError_) as ctx:
            importer.parse(b"xx", "题库.doc", cats=CATS)
        self.assertIn("不是 Word 文档", str(ctx.exception))

    def test_empty_rejected(self):
        with self.assertRaises(importer.ImportError_):
            importer.parse(b"   \n\n  ", "空.txt", cats=CATS)

    def test_unstructured_rejected(self):
        with self.assertRaises(importer.ImportError_) as ctx:
            importer.parse("今天天气不错，机房一切正常。".encode("utf-8"), "笔记.txt", cats=CATS)
        self.assertIn("没有识别到题目", str(ctx.exception))

    def test_oversize_rejected(self):
        with self.assertRaises(importer.ImportError_) as ctx:
            importer.parse(b"x" * (21 * 1024 * 1024), "大.txt", cats=CATS)
        self.assertIn("20MB", str(ctx.exception))


class ImportCommit(BaseCase):
    def test_commit_to_bank(self):
        raw = SAMPLE.encode("utf-8")
        res = importer.parse(raw, "题库.txt", cats=CATS)
        sub = self.core.connect().execute(
            "SELECT id FROM subcategory WHERE cat_id=1 LIMIT 1").fetchone()["id"]
        imported = 0
        for it in res["items"]:
            r = self.core.save_question({
                "cat_id": it["cat_id"] or 1, "sub_id": sub,
                "qtype": it["qtype"], "diff": it["diff"], "stem": it["stem"],
                "options": it["options"], "answer": it["answer"],
                "analysis": it["analysis"],
            })
            if r.get("ok"):
                imported += 1
        self.assertEqual(imported, 6, "6 道题应全部入库")
        conn = self.core.connect()
        n = conn.execute("SELECT COUNT(*) c FROM question").fetchone()["c"]
        conn.close()
        self.assertEqual(n, 6)


class ImportHeader(BaseCase):
    """按表头取列：模板下载 → 填写 → 导回必须闭环。"""

    CATS = [
        {"id": 1, "name": "电气", "subs": [{"id": 11, "name": "供配电"}]},
        {"id": 2, "name": "暖通", "subs": [{"id": 21, "name": "精密空调"}]},
        {"id": 4, "name": "消防", "subs": [{"id": 41, "name": "气体灭火"}]},
        {"id": 5, "name": "管理", "subs": [{"id": 51, "name": "应急演练"}]},
    ]

    def test_template_is_valid_xlsx(self):
        raw = importer.template_xlsx()
        self.assertTrue(raw.startswith(b"PK"), "模板不是 zip 包")
        zf = zipfile.ZipFile(io.BytesIO(raw))
        names = zf.namelist()
        for part in ("[Content_Types].xml", "_rels/.rels",
                     "xl/workbook.xml", "xl/worksheets/sheet1.xml"):
            self.assertIn(part, names, "模板缺少 OOXML 部件：" + part)
        sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        for col in importer.TEMPLATE_HEADER:
            self.assertIn(col, sheet, "模板缺少表头列：" + col)

    def test_template_roundtrip(self):
        """模板自带的 5 行示例必须能被自己完整读回，且字段无损。"""
        res = importer.parse(importer.template_xlsx(), "模板.xlsx", cats=self.CATS)
        self.assertEqual(res["by"], "header", "模板应按表头模式解析")
        self.assertEqual(len(res["items"]), 5, "模板示例行数应为 5")
        got = [(i["qtype"], i["answer"], i["diff"], i["cat_id"]) for i in res["items"]]
        self.assertEqual(got[0], ("single", "C", "easy", 1))
        self.assertEqual(got[1], ("multi", "ABCD", "mid", 4))
        self.assertEqual(got[2], ("judge", "错误", "mid", 2))
        self.assertEqual(got[3], ("fill", "27|60", "mid", 1))
        self.assertEqual(got[4], ("qa", got[4][1], "hard", 5))
        self.assertEqual(res["warnings"], [], "模板示例不应产生告警")
        # 选项与二级分类
        self.assertEqual([o["key"] for o in res["items"][1]["options"]], ["A", "B", "C", "D"])
        self.assertEqual(res["items"][3]["options"], [])
        self.assertEqual(res["items"][0]["sub_id"], 11)

    def test_header_any_column_order(self):
        """列顺序打乱、夹一个无关列，也要按列名对上。"""
        wb = _xlsx([
            ["备注", "答案", "题干", "难度", "题型", "C", "A", "B", "分类"],
            ["随便写", "B", "题干内容一号", "易", "单选题", "丙", "甲", "乙", "电气"],
        ])
        res = importer.parse(wb, "乱序.xlsx", cats=self.CATS)
        self.assertEqual(res["by"], "header")
        q = res["items"][0]
        self.assertEqual(q["qtype"], "single")
        self.assertEqual(q["stem"], "题干内容一号")
        self.assertEqual(q["answer"], "B")
        self.assertEqual(q["diff"], "easy")
        self.assertEqual(q["cat_id"], 1)
        self.assertEqual([o["key"] for o in q["options"]], ["A", "B", "C"])

    def test_header_alias_and_optional_type(self):
        """题型列可省：靠选项与答案推断。列名允许别名（题目/正确答案/难易程度）。"""
        wb = _xlsx([
            ["题目", "A", "B", "正确答案", "难易程度", "专业"],
            ["缺少题型列的题", "甲", "乙", "A", "较难", "消防"],
        ])
        res = importer.parse(wb, "别名.xlsx", cats=self.CATS)
        q = res["items"][0]
        self.assertEqual(q["qtype"], "single")
        self.assertEqual(q["answer"], "A")
        self.assertEqual(q["diff"], "hard")
        self.assertEqual(q["cat_id"], 4)

    def test_no_header_falls_back_to_text(self):
        """没有表头的表格仍走原拆题逻辑，不能被表头模式吞掉。"""
        wb = _xlsx([
            ["1. 第一道题"],
            ["A. 甲"],
            ["B. 乙"],
            ["答案：A"],
        ])
        res = importer.parse(wb, "无表头.xlsx", cats=self.CATS)
        self.assertEqual(res["by"], "text")
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["answer"], "A")

    def test_blank_rows_skipped(self):
        wb = _xlsx([
            ["题型", "题干", "答案"],
            ["", "", ""],
            ["判断题", "空行不应变成题。", "正确"],
        ])
        res = importer.parse(wb, "空行.xlsx", cats=self.CATS)
        self.assertEqual(len(res["items"]), 1)


def _xlsx(rows):
    """测试用：把二维表写成最小的 xlsx（内联字符串）。"""
    body = []
    for r, values in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(values):
            if not val:
                continue
            cells.append('<c r="%s%d" t="inlineStr"><is><t>%s</t></is></c>'
                         % (chr(65 + c), r, val))
        body.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>%s</sheetData></worksheet>' % "".join(body))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
