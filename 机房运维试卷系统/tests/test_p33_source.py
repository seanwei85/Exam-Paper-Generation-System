#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P33：AI 审校建议附原文出处（FR-AI-07）。

问题出在"可核对性"上：模型给一个答案，用户只能选择信或不信。
补答案的场景尤其危险——题干被截断、选项被吞进题干（P32 那批圆点题）时，
模型照样能给出一个"看起来合理"的答案，用户没有第二个参照点。

所以这一阶段做两件事：
  * **来源**：导入时把文件名与页码记在题目上，审校时一并告诉模型；
  * **依据**：要求模型引用题干/选项/来源里的原句作为证据（evidence），
    与"为什么这么判"（reason）分开存——一个是引用，一个是推理，混在一起就没法核对。

依据缺失不等于建议无效：模型偶尔漏字段，不能因此把一条可能正确的建议作废，
所以 evidence 为空时 valid 仍为 1，界面上只是少一行引用。
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from common import BaseCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402

SECRET = "sk-test-p33-abcdefg123456"

Q = {
    "id": 7, "qtype": "single", "stem": "UPS 电池组的均衡充电周期一般是（　）。",
    "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
    "answer": "A", "analysis": "",
}


def reply(**kw):
    obj = {"answer": "A", "analysis": "解析", "verdict": "ok", "confidence": 0.8,
           "reason": "常规做法是同容量电池组每季度一次"}
    obj.update(kw)
    return obj


class ReviewReplyUnit(BaseCase):
    """纯函数：回复解析与提问拼装。"""

    def setUp(self):
        super().setUp()
        import ai_review
        self.rv = ai_review

    def test_parses_evidence(self):
        rec = self.rv.parse_reply(json.dumps(reply(
            evidence="题干「均衡充电周期一般是（　）」与选项 A「选项A」均未提周期数值")), Q)
        self.assertIn("均衡充电周期", rec["evidence"], "模型给的原文引用要原样留下")
        self.assertEqual(rec["valid"], 1)

    def test_missing_evidence_stays_valid(self):
        """漏了 evidence 只是少一行引用，不能把建议作废。"""
        rec = self.rv.parse_reply(json.dumps(reply()), Q)
        self.assertEqual(rec["evidence"], "")
        self.assertEqual(rec["valid"], 1)
        self.assertTrue(rec["suggest_answer"], "答案还是要留下")

    def test_evidence_truncated(self):
        rec = self.rv.parse_reply(json.dumps(reply(evidence="引" * 900)), Q)
        self.assertLessEqual(len(rec["evidence"]), 400, "引用过长要截断，否则界面撑爆")

    def test_evidence_differs_from_reason(self):
        """引用与推理是两个字段：混成一个就没法核对。"""
        rec = self.rv.parse_reply(json.dumps(reply(
            evidence="原文：每季度一次", reason="运维惯例如此")), Q)
        self.assertEqual(rec["evidence"], "原文：每季度一次")
        self.assertEqual(rec["reason"], "运维惯例如此")

    def test_build_messages_carries_source(self):
        q = dict(Q, src_file="数据中心值班专员测试题.docx", src_page=3)
        text = self.rv.build_messages(q, self.rv.KIND_CHECK)[1]["content"]
        self.assertIn("数据中心值班专员测试题.docx", text, "要告诉模型题从哪来")
        self.assertIn("第 3 页", text)

    def test_build_messages_omits_unknown_source(self):
        """没来源就不写这一行——「未知文件 第 0 页」是噪音，还会诱导模型编造。"""
        text = self.rv.build_messages(Q, self.rv.KIND_CHECK)[1]["content"]
        self.assertNotIn("第 0 页", text)
        self.assertNotIn("未知文件", text)

    def test_prompt_requires_quoted_evidence(self):
        prompt = self.rv.SYSTEM_PROMPT
        self.assertIn("evidence", prompt, "系统提示词里要明确要求 evidence 字段")
        self.assertIn("原句", prompt, "要要求引用原句，而不是让模型复述结论")


class SrcHelpers(object):
    """建题 + 写来源。接口用例与渲染用例都用得上，抽出来免得两份实现走偏。"""

    def add_q(self, **kw):
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        payload = {
            "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
            "qtype": "single", "diff": "mid",
            "stem": "UPS 电池组的均衡充电周期一般是（　）。",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": "A", "analysis": "原解析", "tags": "", "allow_dup": True,
        }
        payload.update(kw)
        r = self.post_json("/api/questions", payload)
        self.assertTrue(r.get("ok"), r)
        return r["id"]

    def set_source(self, qid, filename, page):
        conn = self.core.connect()
        conn.execute("UPDATE question SET src_file=?, src_page=? WHERE id=?",
                     (filename, page, qid))
        conn.commit()
        conn.close()


class SourceHttp(SrcHelpers, HttpCase):
    """来源落到题目上、随审校请求发给模型、建议里存下引用。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeAI().start()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.fake.stop()
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.fake.reset()
        cfg = {"enabled": True, "base_url": self.fake.base_url, "api_key": SECRET,
               "model": "fake-k3", "timeout": 60}
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False)

    def test_commit_records_source_file(self):
        """导入入库时记下文件名与页码，之后才有"出处"可看。"""
        st = self.get_json("/api/state")
        cat = st["categories"][0]
        r = self.post_json("/api/import/commit", {
            "filename": "机房运维题库.pdf",
            "items": [{
                "cat_id": cat["id"], "sub_id": cat["subs"][0]["id"],
                "qtype": "single", "diff": "mid",
                "stem": "导入来源测试：蓄电池放电终止电压不得低于额定值的（　）。",
                "options": [{"key": "A", "text": "10%"}, {"key": "B", "text": "20%"}],
                "answer": "A", "src_page": 4,
            }],
        })
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("imported"), 1, r)
        rows = self.core.connect().execute(
            "SELECT src_file, src_page FROM question WHERE stem LIKE '导入来源测试%'").fetchall()
        self.assertEqual(rows[0]["src_file"], "机房运维题库.pdf")
        self.assertEqual(rows[0]["src_page"], 4, "页码要留住，否则出处只剩文件名")

    def test_unknown_source_does_not_block_import(self):
        """没有页码（老数据/手工建的题）也要能入库，写成 0 而不是失败。"""
        qid = self.add_q()
        row = self.core.connect().execute(
            "SELECT src_file, src_page FROM question WHERE id=?", (qid,)).fetchone()
        self.assertEqual(row["src_file"], "")
        self.assertEqual(row["src_page"], 0)

    def test_review_sends_source_to_model(self):
        qid = self.add_q()
        self.set_source(qid, "数据中心值班专员测试题.docx", 3)
        self.fake.queue_json(reply(evidence="题干原句"))
        r = self.post_json("/api/ai/review", {"id": qid})
        self.assertTrue(r.get("ok"), r)
        body = json.dumps(self.fake.last["body"], ensure_ascii=False)
        self.assertIn("数据中心值班专员测试题.docx", body, "模型任务里没带来源")
        self.assertIn("第 3 页", body)

    def test_review_saves_evidence(self):
        qid = self.add_q()
        self.set_source(qid, "卷子.pdf", 2)
        self.fake.queue_json(reply(evidence="【选项】A. 选项A 与题干括号对应"))
        r = self.post_json("/api/ai/review", {"id": qid})
        self.assertTrue(r.get("ok"), r)
        d = self.get_json("/api/questions/%d" % qid)
        self.assertIn("选项A", d["ai_review"]["evidence"], "引用没落库，界面就无从核对")
        self.assertEqual(d["ai_review"]["state"], "pending")

    def test_review_without_source_still_works(self):
        """手工建的题没有来源：审校照常，只是不提来源。"""
        qid = self.add_q()
        self.fake.queue_json(reply())
        r = self.post_json("/api/ai/review", {"id": qid})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["done"][0]["verdict"], "ok")


class SourceMigrate(BaseCase):
    """老库升级：题目表补 src_*、建议表补 evidence。"""

    def test_old_tables_gain_columns(self):
        conn = self.core.connect()
        conn.execute("DROP TABLE IF EXISTS ai_review")
        conn.execute(
            "CREATE TABLE ai_review (qid INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'fill',"
            " verdict TEXT NOT NULL DEFAULT 'filled', suggest_answer TEXT NOT NULL DEFAULT '',"
            " suggest_analysis TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',"
            " confidence REAL NOT NULL DEFAULT 0, model TEXT NOT NULL DEFAULT '',"
            " valid INTEGER NOT NULL DEFAULT 1, problem TEXT NOT NULL DEFAULT '',"
            " state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL)")
        # 题目表也回退成老结构（去掉 P33 新增的三列需要重建表）
        conn.execute(
            "CREATE TABLE q_old AS SELECT id, cat_id, sub_id, qtype, diff, stem, options,"
            " answer, blanks, score, analysis, tags, created_at, updated_at, used_count,"
            " last_used FROM question")
        conn.execute("DROP TABLE question")
        conn.execute("ALTER TABLE q_old RENAME TO question")
        conn.commit()
        conn.close()

        self.core.init_db()

        conn = self.core.connect()
        qcols = {r["name"] for r in conn.execute("PRAGMA table_info(question)")}
        rcols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_review)")}
        conn.close()
        for name in ("src_file", "src_page"):
            self.assertIn(name, qcols, "题目表缺 %s，老库升级后无法记录出处" % name)
        self.assertIn("evidence", rcols, "建议表缺 evidence")
        self.assertIn("old_answer", rcols, "原有的迁移不能被新的覆盖掉")


class SuggestCard(SrcHelpers, RenderCase):
    """界面上要看得见：引用（原文依据）与来源分开显示。"""

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

    def test_card_shows_evidence_and_source(self):
        c = self.nav("bank", "!!window.aiSuggestHtml")
        html = c.eval(
            "aiSuggestHtml({state:'pending', verdict:'filled', suggest_answer:'A',"
            " suggest_analysis:'解析', reason:'运维惯例', evidence:'题干原句：不得低于 20%',"
            " src_file:'机房运维题库.pdf', src_page:3, confidence:0.9, valid:1})")
        self.assertIn("题干原句", html, "引用没显示出来")
        self.assertIn("原文依据", html, "要标明这是原文引用，不是推理")
        self.assertIn("机房运维题库.pdf", html, "来源文件名没显示")
        self.assertIn("第 3 页", html)

    def test_card_hides_source_when_unknown(self):
        c = self.nav("bank", "!!window.aiSuggestHtml")
        html = c.eval(
            "aiSuggestHtml({state:'pending', verdict:'filled', suggest_answer:'A',"
            " suggest_analysis:'解析', reason:'运维惯例', evidence:'', src_file:'',"
            " src_page:0, confidence:0.9, valid:1})")
        self.assertNotIn("第 0 页", html)
        self.assertNotIn("原文依据", html, "没有引用就别留空标签")

    def test_editor_shows_question_source(self):
        """编辑页直接写出来源，核对原卷不用再翻导入记录。"""
        qid = self.add_q()
        self.set_source(qid, "数据中心值班专员测试题.docx", 5)
        c = self.nav("editor?id=%d" % qid, "!!document.getElementById('eStem')")
        hint = c.eval("document.getElementById('srcHint').textContent")
        self.assertIn("数据中心值班专员测试题.docx", hint)
        self.assertIn("第 5 页", hint)

    def test_editor_no_source_no_line(self):
        """没有来源就不显示这行——空标签比不显示更让人困惑。"""
        qid = self.add_q()
        c = self.nav("editor?id=%d" % qid, "!!document.getElementById('eStem')")
        self.assertEqual(c.eval("!!document.getElementById('srcHint')"), False)


if __name__ == "__main__":
    unittest.main()
