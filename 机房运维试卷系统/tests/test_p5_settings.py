#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P5 测试：设置读写、分类维护、备份与恢复、清理。"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import BaseCase


class P5Settings(BaseCase):
    def test_save_and_read(self):
        r = self.core.save_settings({
            "unit_name": "某某数据中心", "paper_prefix": "ZJTEST",
            "duration": "60", "score_single": "2",
        })
        self.assertTrue(r["ok"])
        self.assertEqual(self.core.get_setting("unit_name"), "某某数据中心")
        self.assertEqual(self.core.get_setting("paper_prefix"), "ZJTEST")
        self.assertEqual(self.core.get_setting("duration"), "60")

    def test_unknown_key_ignored(self):
        self.core.save_settings({"hacker": "1"})
        self.assertIsNone(self.core.get_setting("hacker"))

    def test_prefix_used_in_paper_no(self):
        self.core.save_settings({"paper_prefix": "ZJTEST"})
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.assertTrue(r["ok"], r.get("errors"))
        self.assertTrue(r["no"].startswith("ZJTEST-"), r["no"])


class P5Categories(BaseCase):
    def test_add_rename_delete_category(self):
        r = self.core.category_action({"action": "add_cat", "name": "动环"})
        self.assertTrue(r["ok"], r)
        cid = r["id"]
        self.assertEqual(len(self.core.categories_tree()), 6)
        self.assertTrue(self.core.category_action(
            {"action": "rename_cat", "id": cid, "name": "动环监控"} )["ok"])
        names = [c["name"] for c in self.core.categories_tree()]
        self.assertIn("动环监控", names)
        self.assertTrue(self.core.category_action({"action": "del_cat", "id": cid})["ok"])
        self.assertEqual(len(self.core.categories_tree()), 5)

    def test_duplicate_name_rejected(self):
        r = self.core.category_action({"action": "add_cat", "name": "电气"})
        self.assertFalse(r["ok"])
        self.assertIn("已存在", r["error"])

    def test_delete_category_with_questions_rejected(self):
        self.seed(per_cell=1)
        r = self.core.category_action({"action": "del_cat", "id": 1})
        self.assertFalse(r["ok"])
        self.assertIn("道题", r["error"])

    def test_subcategory_crud(self):
        r = self.core.category_action({"action": "add_sub", "cat_id": 1, "name": "列头柜"})
        self.assertTrue(r["ok"], r)
        sid = r["id"]
        tree = self.core.categories_tree()
        self.assertIn("列头柜", [s["name"] for s in tree[0]["subs"]])
        self.assertTrue(self.core.category_action(
            {"action": "rename_sub", "id": sid, "name": "列头柜与母线"})["ok"])
        self.assertTrue(self.core.category_action({"action": "del_sub", "id": sid})["ok"])

    def test_delete_sub_with_questions_rejected(self):
        self.seed(per_cell=1)
        conn = self.core.connect()
        sid = conn.execute("SELECT sub_id FROM question LIMIT 1").fetchone()["sub_id"]
        conn.close()
        r = self.core.category_action({"action": "del_sub", "id": sid})
        self.assertFalse(r["ok"])


class P5Backup(BaseCase):
    def test_backup_created_and_listed(self):
        self.seed(per_cell=1)
        path = self.core.make_backup("t")
        self.assertTrue(os.path.exists(path))
        info = self.core.list_backups()
        self.assertTrue(info["ok"])
        self.assertGreaterEqual(len(info["items"]), 1)
        self.assertEqual(info["db_size"], os.path.getsize(self.core.DB_PATH))

    def test_restore_brings_data_back(self):
        self.seed(per_cell=1)
        before = self.core.stats()["total"]
        path = self.core.make_backup("t")
        self.core.maintenance("clear_questions")
        self.assertEqual(self.core.stats()["total"], 0)
        r = self.core.restore_backup(os.path.basename(path))
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.core.stats()["total"], before, "恢复后题量应与备份一致")
        self.assertTrue(r["safety"], "恢复前应自动留一份当前数据")

    def test_restore_missing_file(self):
        r = self.core.restore_backup("不存在的备份.db")
        self.assertFalse(r["ok"])

    def test_restore_path_traversal_blocked(self):
        r = self.core.restore_backup("../../library.db")
        self.assertFalse(r["ok"])


class P5Maintenance(BaseCase):
    def test_clear_questions(self):
        self.seed(per_cell=1)
        self.assertGreater(self.core.stats()["total"], 0)
        self.assertTrue(self.core.maintenance("clear_questions")["ok"])
        self.assertEqual(self.core.stats()["total"], 0)

    def test_clear_papers_keeps_questions(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.assertTrue(r["ok"])
        self.assertTrue(self.core.maintenance("clear_papers")["ok"])
        self.assertEqual(self.pe.list_papers({})["total"], 0)
        self.assertGreater(self.core.stats()["total"], 0, "清空试卷不应影响题库")

    def test_unknown_action(self):
        self.assertFalse(self.core.maintenance("rm -rf")["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
