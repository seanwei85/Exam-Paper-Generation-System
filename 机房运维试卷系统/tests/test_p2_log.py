#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P2 导入日志（FR-IM-10）。

需求：记录每次导入的文件名、时间、成功数、失败数。
"记了"和"记得对"是两件事，所以这里既验证写没写，也验证四个字段是不是真实的数。
"""

import base64
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase

SAMPLE = """一、单项选择题
1. 机房精密空调的加湿用水应使用（　）。
A. 自来水
B. 软化水
C. 海水
D. 中水
答案：B

2. UPS 电池组的后备时间取决于（　）。
A. 负载大小
B. 机房面积
C. 维护周期
D. 室温
答案：A
"""


class P2ImportLog(HttpCase):
    def parse(self, text):
        return self.post_json("/api/import/parse", {
            "filename": "题库.txt",
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")})

    def ready_items(self, text=SAMPLE, n=None):
        """解析结果补上分类（界面也是这么补的），凑成可提交的数据。"""
        items = self.parse(text)["items"]
        cat = self.get_json("/api/categories")["categories"][0]
        for it in items:
            it["cat_id"] = cat["id"]
            it["sub_id"] = cat["subs"][0]["id"]
        return items[:n] if n else items

    def logs(self, query=""):
        return self.get_json("/api/import/logs" + query)["logs"]

    def rows(self):
        conn = self.core.connect()
        rs = conn.execute("SELECT * FROM import_log ORDER BY id").fetchall()
        conn.close()
        return rs

    # ------------------------------------------------------------ 用例

    def test_log_written_after_commit(self):
        items = self.ready_items()
        res = self.post_json("/api/import/commit", {"items": items, "filename": "十月题库.xlsx"})
        self.assertEqual(res.get("imported"), 2, res)

        logs = self.logs()
        self.assertEqual(len(logs), 1, "提交后应有一条导入记录")
        log = logs[0]
        self.assertEqual(log["filename"], "十月题库.xlsx", "文件名没记住")
        self.assertEqual(log["total"], 2, "total 应为本次提交的题数")
        self.assertEqual(log["imported"], 2)
        self.assertEqual(log["failed"], 0)
        self.assertRegex(log["created_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                         "时间格式应为 YYYY-MM-DD HH:MM:SS")

    def test_failed_count_is_real(self):
        """故意让一题答案非法：失败数要如实记下来，不能写成 0。"""
        items = self.ready_items()
        items[0]["answer"] = "Z"
        res = self.post_json("/api/import/commit", {"items": items, "filename": "半坏.txt"})
        self.assertEqual((res["imported"], res["failed"]), (1, 1), res)

        log = self.logs()[0]
        self.assertEqual((log["total"], log["imported"], log["failed"]), (2, 1, 1))

    def test_all_failed_still_logged(self):
        """全部失败也要留痕，否则用户以为没导过。"""
        items = self.ready_items()
        for it in items:
            it["answer"] = "Z"
        res = self.post_json("/api/import/commit", {"items": items, "filename": "全坏.txt"})
        self.assertEqual((res["imported"], res["failed"]), (0, 2), res)
        log = self.logs()[0]
        self.assertEqual((log["imported"], log["failed"]), (0, 2))

    def test_empty_commit_not_logged(self):
        """空提交不该往日志里塞噪音。"""
        res = self.post_json("/api/import/commit", {"items": [], "filename": "空.txt"})
        self.assertTrue(res.get("ok"))
        self.assertEqual(self.rows(), [], "空提交不应产生导入记录")

    def test_logs_newest_first_and_limited(self):
        for i in range(4):
            self.post_json("/api/import/commit",
                           {"items": self.ready_items(n=1), "filename": "第%d批.txt" % (i + 1)})
        all_logs = self.logs()
        self.assertEqual([l["filename"] for l in all_logs],
                         ["第4批.txt", "第3批.txt", "第2批.txt", "第1批.txt"],
                         "导入记录应按时间倒序")
        self.assertEqual([l["filename"] for l in self.logs("?limit=2")],
                         ["第4批.txt", "第3批.txt"], "limit 没生效")

    def test_missing_filename_gets_placeholder(self):
        """老前端或脚本调接口没带文件名时，也要能记一条能看的记录。"""
        self.post_json("/api/import/commit", {"items": self.ready_items(n=1)})
        self.assertEqual(self.logs()[0]["filename"], "未命名文件")

    def test_log_persisted_in_db_file(self):
        """日志是留档用的：必须落在 sqlite 文件里，不能只存在内存。"""
        self.post_json("/api/import/commit", {"items": self.ready_items(n=1),
                                              "filename": "重启前.txt"})
        self.assertEqual(len(self.rows()), 1)
        logs = self.logs()
        self.assertEqual(logs[0]["filename"], "重启前.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
