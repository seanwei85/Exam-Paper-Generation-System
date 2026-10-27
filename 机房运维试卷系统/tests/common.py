#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试公共设施：独立临时数据库、题库种子、断言辅助。

所有用例都在临时库上跑，不碰 data/library.db。
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zjlab_test_")
        import server as core

        core.DATA_DIR = cls.tmp
        core.BACKUP_DIR = os.path.join(cls.tmp, "backup")
        core.DB_PATH = os.path.join(cls.tmp, "library.db")
        core.LOG_PATH = os.path.join(cls.tmp, "test.log")
        os.makedirs(core.BACKUP_DIR, exist_ok=True)
        core.init_db()

        import paper_engine

        importlib.reload(paper_engine)
        cls.core = core
        cls.pe = paper_engine

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        """每个用例前清空题库、试卷与导入日志，保证相互独立。"""
        conn = self.core.connect()
        conn.execute("DELETE FROM paper_item")
        conn.execute("DELETE FROM paper")
        conn.execute("DELETE FROM question")
        conn.execute("DELETE FROM import_log")
        conn.commit()
        conn.close()

    # ---------------------------------------------------------- 辅助

    def seed(self, per_cell=2, cats=None):
        """按 分类 × 题型 × 难度 均匀铺题，默认每格 2 道。"""
        conn = self.core.connect()
        tree = self.core.categories_tree(conn)
        if cats:
            tree = [c for c in tree if c["name"] in cats]
        ts = self.core.now_str()
        n = 0
        for c in tree:
            sub = c["subs"][0]["id"] if c["subs"] else None
            for t in self.core.TYPES:
                for d in self.core.DIFFS:
                    for i in range(per_cell):
                        stem = "%s-%s-%s-#%d 的题干内容" % (c["name"], t, d, i + 1)
                        opts = []
                        ans = ""
                        blanks = 0
                        if t in ("single", "multi"):
                            opts = [{"key": k, "text": "选项" + k} for k in "ABCD"]
                            ans = "A" if t == "single" else "AB"
                        elif t == "judge":
                            ans = "正确"
                        elif t == "fill":
                            stem += "：____ 与 ____"
                            blanks = 2
                            ans = "甲|乙"
                        else:
                            ans = "参考答案：按要点作答。"
                        conn.execute(
                            """INSERT INTO question(cat_id, sub_id, qtype, diff, stem, options,
                               answer, blanks, score, analysis, tags, created_at, updated_at,
                               used_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                            (c["id"], sub, t, d, stem, json.dumps(opts, ensure_ascii=False),
                             ans, blanks, None, "", "%s,%s" % (c["name"], t), ts, ts),
                        )
                        n += 1
        conn.commit()
        conn.close()
        return n

    def qids(self, **kw):
        conn = self.core.connect()
        sql = "SELECT id FROM question WHERE 1=1"
        args = []
        for k, v in kw.items():
            sql += " AND %s=?" % k
            args.append(v)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return [r["id"] for r in rows]

    def base_cfg(self, **kw):
        cfg = {
            "mode": "auto",
            "title": "测试卷",
            "unit": "ZJLab",
            "duration": 45,
            "rows": [
                {"qtype": "single", "enabled": True, "count": 8, "score": 1},
                {"qtype": "multi", "enabled": True, "count": 4, "score": 2},
                {"qtype": "judge", "enabled": True, "count": 6, "score": 1},
                {"qtype": "fill", "enabled": True, "count": 4, "score": 1},
                {"qtype": "qa", "enabled": True, "count": 2, "score": 12},
            ],
            "diff_mode": "points",
            "diff_target": 20,
            "diff_ratio": {"easy": 50, "mid": 30, "hard": 20},
            "quota_mode": "percent",
            "quotas": {},
            "rules": {
                "avoid_recent": {"enabled": False, "papers": 0},
                "must": {"enabled": False, "ids": []},
                "scope": {"enabled": False, "tags": "", "new_days": 0, "new_ratio": 0},
            },
            "target_score": 100,
        }
        cfg.update(kw)
        return cfg
