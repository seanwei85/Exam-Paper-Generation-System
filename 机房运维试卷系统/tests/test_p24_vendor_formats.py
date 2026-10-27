#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P24：真实平台上导出的三种版式（第三轮入库当场暴露）。

三份真实文件导不进去或导成"全草稿"，都是**答案其实就在文件里**：

  * 「答题解析」导出（○A. ●B.）：正确选项用实心圆点标出来，题干后面跟「分值1分」，
    每题后面跟「回答正确（+1分）」。软件原来不认 ○●，80 道题全成草稿，
    还把「回答正确（+1分）」粘进最后一个选项里。
  * 「一行挤四个选项」：`A. 5500*1400   B、5500*1600  C、6000*1600`——原来只认出第一个 A，
    另外三个变成 A 的正文，17 道题里 12 道选项报废。
  * 「答案写在题干行尾」：`……副边三相电压为    。答案：B` + 下一行四个选项，
    答案在题干里，原来的规则只在"独占一行的答案行"里找。

外加两处小账：题干的「分值1分」要剔掉；分类关键词兜底（界面早就写了"题干中出现
电气/暖通/弱电/消防/管理会自动归类"，文本路径其实没做）。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import importer  # noqa: E402

CATS = [{"id": 1, "name": "电气", "subs": [{"id": 11, "name": "配电"}]},
        {"id": 2, "name": "暖通", "subs": [{"id": 21, "name": "冷机"}]}]


def parse(text):
    return importer.split_questions(text, cats=CATS)


class P24CircleMarkers(unittest.TestCase):
    """「答题解析」导出：○ 是错项、● 是正确答案。"""

    TEXT = """电1  单项选择题
1. 低压配电系统的相电压、线电压、工频分别为（）  分值1分
●A.220V 380V 50HZ
○B.220V 380V 60HZ
○C.380V 220V 50HZ
○D.380V 220V 60HZ
回答正确（+1分）
2. 变压器的温度达到（）会自动启动冷却风扇  分值1分
○A. 80℃
○B. 100℃
●C. 130℃
○D. 150℃
回答错误（+0分）
正确答案: C
"""

    def test_dot_marks_answer(self):
        items = parse(self.TEXT)
        self.assertEqual(len(items), 2, [i["stem"] for i in items])
        self.assertEqual(items[0]["answer"], "A", items[0])
        self.assertEqual(items[1]["answer"], "C", items[1])

    def test_dot_is_not_part_of_option_text(self):
        """「●」是标记，不能留在选项正文里。"""
        it = parse(self.TEXT)[0]
        self.assertEqual([o["key"] for o in it["options"]], ["A", "B", "C", "D"], it)
        self.assertEqual(it["options"][0]["text"], "220V 380V 50HZ", it["options"][0])

    def test_feedback_lines_do_not_pollute_option(self):
        """「回答正确（+1分）」是评分反馈，不是选项内容。"""
        for it in parse(self.TEXT):
            for o in it["options"]:
                self.assertNotIn("回答", o["text"], o)
                self.assertNotIn("+1分", o["text"], o)

    def test_fen_value_stripped_from_stem(self):
        """题干的「分值1分」要剔掉，否则每道题的题干尾巴都挂着它。"""
        items = parse(self.TEXT)
        for it in items:
            self.assertNotIn("分值", it["stem"], it["stem"])

    def test_multi_with_two_dots(self):
        """多选题有几个 ● 就取几个，按字母序拼。"""
        text = """二、多项选择题
1. UPS 的电池开关有哪些要求（）  分值2分
●A. 能切断额定电流
○B. 可以带载断开
●C. 有明显的断开点
○D. 不需要维护
回答错误（+0分）
"""
        items = parse(text)
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["qtype"], "multi", items[0])
        self.assertEqual(items[0]["answer"], "AC", items[0])


class P24InlineOptions(unittest.TestCase):
    """一行里挤着多条选项。"""

    def test_dot_then_dun_separators(self):
        text = ("1. 阳光电源EnerNeo-SST宽深尺寸（　　）mm\n"
                "A. 5500*1400   B、5500*1600  C、6000*1600  D、6000*1400\n")
        it = parse(text)[0]
        self.assertEqual([o["key"] for o in it["options"]], ["A", "B", "C", "D"], it)
        self.assertEqual(it["options"][1]["text"], "5500*1600", it["options"][1])

    def test_full_inline_line(self):
        """「A、220V    B、380V    C、500V」整行四个选项。"""
        text = ("1、供电系统常用的三相电力变压器主边电压为10kV，副边三相电压为    。答案：B\n"
                "A、220V    B、380V    C、500V    D、600V\n")
        it = parse(text)[0]
        self.assertEqual([o["key"] for o in it["options"]], ["A", "B", "C", "D"], it)
        self.assertEqual(it["answer"], "B", it)

    def test_inline_answer_in_stem_line(self):
        """答案写在题干行尾时，要从题干里摘出来，不能留在卷面上。"""
        text = ("1、空气断路器的技术参数Icu表示的含义是     。\n"
                "答案：极限短路电流或极限短路分断能力\n")
        it = parse(text)[0]
        self.assertEqual(it["qtype"], "fill", it)
        self.assertIn("极限短路电流", it["answer"], it)

    def test_option_marker_only_when_sequential(self):
        """「B、」出现在句子中间（不是选项位置）时不许乱切。"""
        text = ("1. 题干里提到 A、B、C 三类负荷，问哪一类必须双电源\n"
                "A. 一类   B. 二类   C. 三类\n")
        it = parse(text)[0]
        self.assertEqual([o["key"] for o in it["options"]], ["A", "B", "C"], it)


class P24CategoryFallback(unittest.TestCase):
    """界面写着"题干中出现电气/暖通/弱电/消防/管理会自动归类"，文本路径要真做到。"""

    def test_keyword_assigns_category(self):
        it = parse("1. 冷水机组的冷凝器结垢会导致（）\nA. 能效下降   B. 能效上升\n")[0]
        self.assertEqual(it["cat_id"], 2, it)   # 暖通

    def test_no_keyword_leaves_empty_for_ui_default(self):
        it = parse("1. 下列哪项属于规章制度（）\nA. 交接班   B. 巡检\n")[0]
        self.assertIsNone(it["cat_id"], it)


if __name__ == "__main__":
    unittest.main(verbosity=2)
