#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P16：.doc / .xls 旧格式导入。

用户手上的题库八成是 .xls（厂商模板导出来的）和 .doc，之前这两种是直接拒收的，
提示"请另存为新格式"——正是用户说的"还要重新改，太浪费时间"。

这里验收的是"真能读出题"，不是"容器能打开"：

  1) 复合文档：按名字取流，小于 4096 字节的流走迷你流，不能读到别处去；
  2) Excel：SST 跨 CONTINUE 时字符编码会变（段首多一个标志字节），拼成一段再
     解析就会错位——必须按段读；
  3) Excel：多工作表拼接后，第二张表的表头不能变成一道题；
  4) Excel：题干单元格里内嵌的 A、B、C 选项要拆出来，否则选择题全废；
  5) Word：正文在片段表里，压缩片段（ASCII）与非压缩片段（中文 UTF-16）混排；
     域的"指令"部分要整段丢掉，否则目录的 TOC 代码会灌进正文；
  6) 扩展名骗人：.doc 实为 RTF/HTML、.xls 实为 HTML 表格时按内容读；
  7) 整份文件没有答案列时，提示要收成一句，不能刷几百条。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import legacy  # noqa: E402
import ole  # noqa: E402
import olegen  # noqa: E402
import importer  # noqa: E402


class OreCase(unittest.TestCase):
    """容器层。"""

    def test_reads_stream_by_name(self):
        raw = olegen.build_cfb([("Workbook", b"x" * 5000), ("Other", b"y" * 32)])
        cf = ole.CompoundFile(raw)
        self.assertEqual(cf.stream("Workbook"), b"x" * 5000)
        self.assertEqual(cf.stream("Other"), b"y" * 32)

    def test_mini_stream_roundtrip(self):
        """小于 4096 字节的流在迷你流里，按 64 字节一段串起来，读错会串位。"""
        body = bytes(range(256)) * 6          # 1536 字节，走迷你流
        raw = olegen.build_cfb([("WordDocument", body), ("1Table", b"t" * 100)])
        cf = ole.CompoundFile(raw)
        self.assertEqual(cf.stream("WordDocument"), body)
        self.assertEqual(cf.stream("1Table"), b"t" * 100)

    def test_missing_stream_is_none(self):
        cf = ole.CompoundFile(olegen.build_cfb([("A", b"1" * 40)]))
        self.assertIsNone(cf.stream("NotThere"))

    def test_non_cfb_rejected(self):
        with self.assertRaises(ole.OleError):
            ole.CompoundFile(b"plain text, not a compound file" * 30)

    def test_truncated_file_rejected(self):
        raw = olegen.build_cfb([("A", b"z" * 5000)])
        with self.assertRaises(ole.OleError):
            ole.CompoundFile(raw[:600])


class SniffCase(unittest.TestCase):
    """扩展名不可信，按内容判断。"""

    def test_formats(self):
        self.assertEqual(legacy.sniff(olegen.build_cfb([("A", b"a" * 10)])), "ole")
        self.assertEqual(legacy.sniff(b"PK\x03\x04rest"), "zip")
        self.assertEqual(legacy.sniff(b"{\\rtf1\\ansi hello}"), "rtf")
        self.assertEqual(legacy.sniff(b"<html><body>x</body></html>"), "html")
        self.assertEqual(legacy.sniff("纯文本题库".encode("utf-8")), "text")


class XlsCase(unittest.TestCase):
    """Excel 97：SST、单元格记录、行序还原。"""

    def test_reads_strings_and_numbers(self):
        raw = olegen.build_xls([[
            ["题型", "题干", "答案"],
            ["单选题", "柴油发电机启动前应检查什么？", "A"],
            ["单选题", "冷冻水补水量按蒸发量的多少估算？", "B"],
        ]])
        grid = legacy.xls_sheets(raw)[0]
        self.assertEqual(grid[1][1], "柴油发电机启动前应检查什么？")
        self.assertEqual(grid[2][2], "B")

    def test_sst_split_across_continue(self):
        """字符串被拆到 CONTINUE 里时，段首那个编码标志字节不能当成字符读进去。

        SST 里字符串按首次出现顺序编号：题干(0)、答案(1)、长题干(2)、正确(3)，
        所以断在第 2 条上。
        """
        stem = "柴油发电机启动后应先空载暖机再带负荷，这种说法是否正确？"
        raw = olegen.build_xls([[
            ["题干", "答案"],
            [stem, "正确"],
        ]], split=(2, 7))
        grid = legacy.xls_sheets(raw)[0]
        self.assertEqual(grid[1][0], stem,
                         "跨段字符串读错：%r" % (grid[1][0],))

    def test_columns_keep_position(self):
        """中间空了一格时，后面的值不能左移——按表头取列全靠列位置。"""
        raw = olegen.build_xls([[
            ["题型", "题干", "A", "B", "答案"],
            ["单选题", "题一", "", "选项B", "B"],
        ]])
        grid = legacy.xls_sheets(raw)[0]
        self.assertEqual(grid[1][2], "")
        self.assertEqual(grid[1][3], "选项B")
        self.assertEqual(grid[1][4], "B")

    def test_multiple_sheets(self):
        raw = olegen.build_xls([
            [["题干"], ["第一张表的题"]],
            [["题干"], ["第二张表的题"]],
        ])
        sheets = legacy.xls_sheets(raw)
        self.assertEqual(len(sheets), 2)
        self.assertEqual(sheets[0][1][0], "第一张表的题")
        self.assertEqual(sheets[1][1][0], "第二张表的题")

    def test_numbers_rendered_without_decimal(self):
        raw = olegen.build_xls([[["分值", "10"]]])
        self.assertTrue(legacy.xls_sheets(raw))

    def test_html_table_named_xls(self):
        """有些系统"导出 Excel"其实是 HTML 表格，扩展名写成 .xls。"""
        html = ("<html><body><table>"
                "<tr><td>题型</td><td>题干</td><td>答案</td></tr>"
                "<tr><td>单选题</td><td>市电中断后 UPS 应维持多久？</td><td>C</td></tr>"
                "</table></body></html>").encode("utf-8")
        grid = legacy.xls_sheets(html)[0]
        self.assertEqual(grid[1][0], "单选题")
        self.assertEqual(grid[1][1], "市电中断后 UPS 应维持多久？")

    def test_zip_named_xls_is_told_to_rename(self):
        raw = b"PK\x03\x04" + b"\0" * 200
        with self.assertRaises(legacy.LegacyError) as ctx:
            legacy.xls_sheets(raw)
        self.assertIn("xlsx", str(ctx.exception))

    def test_text_named_xls_rejected(self):
        with self.assertRaises(legacy.LegacyError):
            legacy.xls_sheets("这只是一个文本文件".encode("utf-8"))


class DocCase(unittest.TestCase):
    """Word 97：片段表、压缩/非压缩混排、域指令。"""

    def test_pieces_compressed_and_wide(self):
        raw = olegen.build_doc([("1. DIESEL ", True),
                                ("柴油发电机启动前应检查机油液位。", False),
                                (" A. 正确 B. 错误", True)])
        text = legacy.doc_text(raw)
        self.assertIn("柴油发电机启动前应检查机油液位。", text)
        self.assertIn("1. DIESEL", text)
        self.assertTrue(text.index("DIESEL") < text.index("柴油发电机"),
                        "片段顺序错了：%r" % text)

    def test_table_stream_0table(self):
        raw = olegen.build_doc([("机房温度应控制在 18~27 度。", False)],
                               table_stream="0Table")
        self.assertIn("机房温度", legacy.doc_text(raw))

    def test_field_codes_dropped(self):
        """域的写法是 \\x13 指令 \\x14 结果 \\x15。指令是 TOC 代码，不能进正文。"""
        raw = olegen.build_doc([
            ("目  录", False),
            ("\x13 TOC \\o \"1-3\" \\h \\z \\u HYPERLINK \\l \"_Toc123\" \x14前言\x15", False),
            ("正文开始", False),
        ])
        text = legacy.doc_text(raw)
        self.assertIn("前言", text)
        self.assertNotIn("HYPERLINK", text)
        self.assertNotIn("TOC", text)

    def test_rtf_named_doc(self):
        """\'hh 是按声明的代码页编码的字节；字体表（组）要整组跳过。"""
        rtf = (b"{\\rtf1\\ansi\\ansicpg936{\\fonttbl{\\f0 SimSun;}}"
               b"\\f0 1. UPS \\'b2\\'e2\\'ca\\'d4\\par 2. \\'cc\\'e2\\'c4\\'bf\\par}")
        text = legacy.doc_text(rtf)
        self.assertIn("1. UPS 测试", text)
        self.assertIn("2. 题目", text)
        self.assertNotIn("SimSun", text, "字体表不该进正文：%r" % text)

    def test_zip_named_doc_is_told_to_rename(self):
        with self.assertRaises(legacy.LegacyError) as ctx:
            legacy.doc_text(b"PK\x03\x04" + b"\0" * 100)
        self.assertIn("docx", str(ctx.exception))

    def test_blank_doc_reports_picture(self):
        raw = olegen.build_cfb([("WordDocument", b"\xec\xa5" + b"\0" * 900)])
        with self.assertRaises(legacy.LegacyError) as ctx:
            legacy.doc_text(raw)
        self.assertIn("图片", str(ctx.exception))


class ImportCase(unittest.TestCase):
    """走完整导入链路：表头取列 + 内嵌选项 + 警告收拢。"""

    HEADER = ["类型", "题型", "题类", "知识点二级", "难度", "试题正文", "参考答案"]
    ROW = ["通用专业知识", "单项选择", "客观题", "UPS原理", "低",
           "功率因数λ＝p/s，其中 r 表示(  )。\nA、电网电流有效值\nB、基波电流有效值\n"
           "C、基波电流有效值与电网电流有效值之比\nD、电网电压有效值",
           "C"]

    def parse(self, raw, name):
        return importer.parse(raw, name)

    def test_header_aliases_of_vendor_template(self):
        """厂商标题库的表头是「试题正文」「参考答案」「知识点二级」，不是「题干」。"""
        res = self.parse(olegen.build_xls([[self.HEADER, self.ROW]]), "UPS题库.xls")
        it = res["items"][0]
        self.assertEqual(it["qtype"], "single", "「题型」列没被认出来")
        self.assertIn("功率因数", it["stem"])
        self.assertEqual(it["answer"], "C")
        self.assertEqual(it["diff"], "easy", "难度「低」应映射为易")

    def test_inline_options_split_out(self):
        res = self.parse(olegen.build_xls([[self.HEADER, self.ROW]]), "UPS题库.xls")
        opts = res["items"][0]["options"]
        self.assertEqual([o["key"] for o in opts], ["A", "B", "C", "D"])
        self.assertEqual(opts[2]["text"], "基波电流有效值与电网电流有效值之比")
        self.assertNotIn("A、", res["items"][0]["stem"],
                         "选项没拆出来，题干里还留着 A、B、C")

    def test_type_column_wins_over_type_word_in_category(self):
        """一行里同时有「类型」和「题型」时，题型取自「题型」，分类取自「类型」。"""
        res = self.parse(olegen.build_xls([[self.HEADER, self.ROW]]), "a.xls")
        self.assertEqual(res["items"][0]["qtype"], "single")

    def test_repeated_sheet_header_not_a_question(self):
        rows_a = [self.HEADER, self.ROW]
        rows_b = [["类型", "题型", "题类", "知识点二级", "难度", "试题正文"],
                  ["通用维护知识", "判断题", "客观题", "故障处理", "高", "UPS 可以长期满载放电。"]]
        res = self.parse(olegen.build_xls([rows_a, rows_b]), "a.xls")
        stems = [i["stem"] for i in res["items"]]
        self.assertEqual(len(stems), 2, "第二张表的表头被当成题目了：%r" % stems)
        self.assertFalse([s for s in stems if s.startswith("试题正文")])

    def test_missing_answer_column_collapses_to_one_warning(self):
        header = ["题型", "试题正文", "难度"]
        rows = [header] + [["填空题", "第%d题：机房温度应控制在（  ）℃。" % i, "中"]
                           for i in range(1, 6)]
        res = self.parse(olegen.build_xls([rows]), "a.xls")
        self.assertEqual(len(res["items"]), 5)
        self.assertEqual(len(res["warnings"]), 1, "缺答案的提示应收成一句：%r" % res["warnings"])
        self.assertIn("没有「答案」列", res["warnings"][0])

    def test_partial_missing_answer_keeps_per_item_warning(self):
        header = ["题型", "试题正文", "参考答案"]
        rows = [header,
                ["填空题", "题一（  ）", "甲"],
                ["填空题", "题二（  ）", ""]]
        res = self.parse(olegen.build_xls([rows]), "a.xls")
        self.assertEqual(len(res["warnings"]), 1)
        self.assertIn("第 2 题", res["warnings"][0])

    def test_doc_import_end_to_end(self):
        raw = olegen.build_doc([
            ("一、单项选择题", False),
            ("\r1. 柴油发电机启动前应检查的项目不包括（  ）。"
             "\rA. 机油液位\rB. 冷却液液位\rC. 蓄电池电压\rD. 机房温度"
             "\r答案：D\r", False),
        ])
        res = self.parse(raw, "试卷.doc")
        self.assertEqual(res["by"], "text")
        it = res["items"][0]
        self.assertEqual(it["qtype"], "single")
        self.assertEqual(it["answer"], "D")
        self.assertEqual(len(it["options"]), 4)

    def test_corrupt_ole_reports_readable_error(self):
        raw = olegen.build_cfb([("WordDocument", b"\x00" * 5000)])
        with self.assertRaises(importer.ImportError_) as ctx:
            self.parse(raw, "坏文件.doc")
        self.assertIn("Word", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
