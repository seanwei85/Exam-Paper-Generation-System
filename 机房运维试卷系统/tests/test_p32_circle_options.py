# -*- coding: utf-8 -*-
"""P32 圆点选项版式（○ 错项 / ● 正确项）。

真实样本：数据中心值班专员测试题—翁晓宁.docx，选项不用 A. B. C. 而用圆点标记：
  人体安全电压是多少伏？
  ○12
  ○24
  ●36        ← 实心圆 = 正确答案
  ○48
此前 OPT_RE 要求圆点后必须跟字母，且分隔符类不含逗号，两处都认不出，
整题被当成问答题、选项全留在题干里（用户反馈：无答案草稿题大量识别错）。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import importer  # noqa: E402


class P32CircleOptions(unittest.TestCase):

    def test_bare_circle_options_single(self):
        """无字母圆点选项：○ 错项、● 正确项，自动编 A/B/C/D。

        段落标题写「问答题」也不行——题目带了选项就是选择题（真实文件里
        这批题正是被标题按成了 qa）。"""
        text = "三、问答题\n12. 人体安全电压是多少伏？\n○12\n○24\n●36\n○48\n"
        res = importer.parse_text(text)
        items = res["items"]
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["stem"], "人体安全电压是多少伏？")
        self.assertEqual([o["key"] for o in it["options"]], ["A", "B", "C", "D"])
        self.assertEqual([o["text"] for o in it["options"]], ["12", "24", "36", "48"])
        self.assertEqual(it["answer"], "C", "实心圆 ● 应成为正确答案")
        self.assertEqual(it["qtype"], "single", "只有一个 ● 是单选")

    def test_bare_circle_multi(self):
        """多个 ● 是多选。"""
        text = "14. 下列属于安全措施的有（）\n●戴绝缘手套\n●穿绝缘鞋\n○穿棉衣\n"
        res = importer.parse_text(text)
        it = res["items"][0]
        self.assertEqual(it["qtype"], "multi")
        self.assertEqual(it["answer"], "AB")

    def test_letter_comma_separator(self):
        """字母选项用逗号分隔（●A, 可能是电池失效;）：此前逗号不在分隔符类里。"""
        text = ("15. 市电中断输出也中断,原因可能是（）\n"
                "●A, 电池失效;\n○B, 静态转换开关故障;\n○C, 电池开关未闭合;\n")
        res = importer.parse_text(text)
        it = res["items"][0]
        self.assertEqual(len(it["options"]), 3)
        self.assertEqual(it["options"][0]["text"], "电池失效;")
        self.assertEqual(it["answer"], "A")

    def test_plain_stem_not_swallowed(self):
        """普通正文不受影响：没有圆点时规则不能误吞题干。"""
        text = "16. 机房精密空调的加湿用水应使用（　）。\nA. 自来水\nB. 软化水\n答案：B\n"
        res = importer.parse_text(text)
        it = res["items"][0]
        self.assertEqual(it["qtype"], "single")
        self.assertEqual(len(it["options"]), 2)
        self.assertEqual(it["answer"], "B")

    def test_single_bare_circle_kept_as_text(self):
        """孤立的○行更像正文（不是选项组）：不拆选项、不误编 key。"""
        text = "17. 圆圈符号○在数学里表示什么？\n答案：圆\n"
        res = importer.parse_text(text)
        it = res["items"][0]
        self.assertEqual(it["qtype"], "qa")
        self.assertIn("○", it["stem"], "孤立圆点行不能被当成选项吞掉")
        self.assertEqual(it["options"], [])


if __name__ == "__main__":
    unittest.main()
