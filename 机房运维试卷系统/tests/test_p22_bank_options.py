#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P22：题库列表行内显示选项摘要。

用户的反馈是"批量导入后，有选择题题干，没看到选项部分"。排查结论：选项都在
（库里 48 道选择题全部有选项，导入预览表也有选项列），问题出在**题库管理列表
只显示题干**——用户在列表里看不见选项，就会以为丢了。

修法：列表的题目单元格里，题干下加一行小字选项摘要（A. xx B. xx …），选择题
一眼看到选项；问答题/填空题没有选项，不显示这行。
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


class P22BankOptions(RenderCase):
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

    BANK_READY = ("!!document.querySelector('.opt-line') && "
                  "document.querySelectorAll('#main table tbody tr').length > 0")

    def setUp(self):
        super().setUp()
        # 基类 setUp 清空题库，这里按 分类×题型×难度 铺回种子（选择题带 ABCD 选项）
        self.seed()

    def test_bank_list_shows_option_summary(self):
        """列表里每道选择题的题干下要能看到选项，不用点开预览。"""
        c = self.nav("bank", self.BANK_READY)
        first = c.eval("(function(){"
                       "  var lines = document.querySelectorAll('#main .opt-line');"
                       "  return lines.length ? lines[0].textContent : '';"
                       "})()")
        self.assertIn("A.", first, first)
        self.assertIn("B.", first, first)

    def test_option_line_belongs_to_choice_question(self):
        """选项摘要要跟题型对得上：问答题/填空题的行不该有选项行。"""
        c = self.nav("bank", self.BANK_READY)
        bad = c.eval("(function(){"
                     "  var rows = document.querySelectorAll('#main table tbody tr');"
                     "  var bad = 0;"
                     "  for (var i = 0; i < rows.length; i++){"
                     "    var isChoice = rows[i].querySelector('.t-single, .t-multi');"
                     "    var hasLine  = rows[i].querySelector('.opt-line');"
                     "    if (isChoice && !hasLine) bad++;"
                     "    if (!isChoice && hasLine) bad++;"
                     "  }"
                     "  return bad;"
                     "})()")
        self.assertEqual(bad, 0, "%d 行的选项摘要与题型不匹配" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
