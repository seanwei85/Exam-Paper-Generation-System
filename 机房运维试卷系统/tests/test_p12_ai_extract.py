#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P12 AI 抽取：分块、容错、字段清洗、降级。

两条测试路线分开走：
  - 「会不会按协议说话」用假 AI 服务走真 HTTP（重试、鉴权、返回体解析）；
  - 「编排对不对」把 _ask_text 换成脚本函数，能精确安排"第 2 块必失败"这种局面。
    真正的网络抖动测不了，但"失败了会不会降级"必须测到。
"""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import BaseCase  # noqa: E402
from fake_ai import FakeAI  # noqa: E402

SECRET = "sk-test-abcdefghijklmnop1234"

#: 原始 _ask_text。只能在任何 patch 发生之前抓一次——如果每个测试类各自
#: "第一次见到时保存"，轮到后面的类时抓到的已经是上一个类换上去的假函数，
#: 于是协议类用例根本不发请求，测出来全是假的。
_ORIG_ASK = None


def make_item(stem="题干", qtype="single", **kw):
    item = {"qtype": qtype, "stem": stem,
            "options": [{"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}],
            "answer": "A", "diff": "mid", "cat": "电气", "analysis": ""}
    item.update(kw)
    return item


class AIBase(BaseCase):
    @classmethod
    def setUpClass(cls):
        global _ORIG_ASK
        super().setUpClass()
        cls.fake = FakeAI().start()
        import ai_parse
        if _ORIG_ASK is None:
            _ORIG_ASK = ai_parse._ask_text

    @classmethod
    def tearDownClass(cls):
        try:
            cls.fake.stop()
        finally:
            super().tearDownClass()

    def setUp(self):
        super().setUp()
        import ai_parse
        self.ai = ai_parse
        # 编排类用例会把 _ask_text 换成脚本函数，模块级替换会带到后面的用例里，
        # 导致"协议类用例根本不发请求"。每个用例开始先恢复原函数。
        ai_parse._ask_text = _ORIG_ASK
        self.ai.RETRY_BACKOFF = 0        # 失败用例不该白等几秒
        self.fake.reset()
        path = os.path.join(self.tmp, "ai.json")
        if os.path.exists(path):
            os.remove(path)
        self.ai.save_config({"api_key": SECRET, "base_url": self.fake.base_url,
                             "model": "fake-chat", "vision_model": "fake-vl"})
        self.cats = self.core.categories_tree(self.core.connect())


class P12Chunk(AIBase):
    """分块：切得开，但不许把一道题劈成两半。"""

    def q_block(self, no, n_opt=4):
        lines = ["%d. 第 %d 题题干，这是一段足够长的题干用来撑长度。" % (no, no)]
        for k in "ABCD"[:n_opt]:
            lines.append("%s. 选项 %s 的内容" % (k, k))
        lines.append("答案：A")
        return "\n".join(lines)

    def test_short_text_is_one_chunk(self):
        text = self.q_block(1)
        self.assertEqual(len(self.ai.chunk_text(text, 6000)), 1)

    def test_long_text_is_split(self):
        text = "\n".join(self.q_block(i) for i in range(1, 120))
        chunks = self.ai.chunk_text(text, 1500)
        self.assertGreater(len(chunks), 1, "长文本没有被切开，会一次塞给模型太多")
        for c in chunks:
            self.assertLessEqual(len(c), 1500 * 1.6, "单块远超上限，切分没起作用")

    def test_chunk_never_starts_with_option_line(self):
        """块不能从选项行开始——那说明上一块的题干和这一块的选项被劈开了。"""
        text = "\n".join(self.q_block(i) for i in range(1, 120))
        for c in self.ai.chunk_text(text, 1500):
            first = c.strip().split("\n")[0]
            self.assertFalse(re.match(r"^\s*[A-Ha-h]\s*[\.、．\)）]", first),
                             "块从选项行开始，题目被劈开了：%s" % first[:40])

    def test_every_chunk_has_question(self):
        text = "\n".join(self.q_block(i) for i in range(1, 120))
        for c in self.ai.chunk_text(text, 1500):
            self.assertRegex(c.strip().split("\n")[0], r"^\d{1,3}\s*[\.、．]",
                             "块没有以题号开头")

    def test_empty_text_no_chunk(self):
        self.assertEqual(self.ai.chunk_text("   ", 6000), [])


class P12Json(AIBase):
    """模型输出千奇百怪，抠 JSON 这件事要稳。"""

    def test_plain_array(self):
        got = self.ai.extract_json('[{"stem":"甲"}]')
        self.assertEqual(len(got), 1)

    def test_fenced_array(self):
        raw = '```json\n[{"stem":"甲"}]\n```'
        self.assertEqual(len(self.ai.extract_json(raw)), 1)

    def test_chatter_around_array(self):
        raw = '好的，以下是转换结果：\n[{"stem":"甲"},{"stem":"乙"}]\n希望对你有帮助。'
        self.assertEqual(len(self.ai.extract_json(raw)), 2)

    def test_wrapped_in_object(self):
        raw = '{"items":[{"stem":"甲"}]}'
        self.assertEqual(len(self.ai.extract_json(raw)), 1)

    def test_single_object(self):
        self.assertEqual(len(self.ai.extract_json('{"stem":"甲"}')), 1)

    def test_trailing_comma_tolerated(self):
        raw = '[{"stem":"甲"},]'
        self.assertEqual(len(self.ai.extract_json(raw)), 1)

    def test_garbage_raises(self):
        with self.assertRaises(self.ai.AIError):
            self.ai.extract_json("今天天气不错")


class P12Normalize(AIBase):
    """字段清洗：模型给的中文题干要变成系统认的字段，洗不出就丢。"""

    def test_qtype_alias(self):
        items, _ = self.ai.normalize_items([
            {"qtype": "多项选择题", "stem": "多选一题", "options": [
                {"key": "A", "text": "甲"}, {"key": "B", "text": "乙"}], "answer": "AB"},
            {"qtype": "judge", "stem": "判断题一题", "options": [], "answer": "对"},
            {"qtype": "填空题", "stem": "机房温度应为____℃", "options": [], "answer": "18~27"},
        ], self.cats)
        self.assertEqual([i["qtype"] for i in items], ["multi", "judge", "fill"])
        self.assertEqual(items[1]["answer"], "正确", "「对」应归一成「正确」")
        self.assertEqual(items[0]["answer"], "AB")

    def test_options_normalized(self):
        items, _ = self.ai.normalize_items([{
            "qtype": "single", "stem": "题",
            "options": {"b": "选项B", "a": "A. 选项A"}, "answer": "a"}], self.cats)
        opts = items[0]["options"]
        self.assertEqual([o["key"] for o in opts], ["A", "B"], "选项应按字母排序、key 大写")
        self.assertEqual(opts[0]["text"], "选项A", "选项文字里残留了 A. 前缀")
        self.assertEqual(items[0]["answer"], "A")

    def test_option_keys_filled_when_missing(self):
        items, _ = self.ai.normalize_items([{
            "qtype": "single", "stem": "题",
            "options": ["选项一", "选项二"], "answer": "A"}], self.cats)
        self.assertEqual([o["key"] for o in items[0]["options"]], ["A", "B"])

    def test_multiline_answer_list(self):
        items, _ = self.ai.normalize_items([{
            "qtype": "multi", "stem": "题",
            "options": [{"key": k, "text": "选项" + k} for k in "ABCD"],
            "answer": ["A", "B"]}], self.cats)
        self.assertEqual(items[0]["answer"], "AB")

    def test_cat_mapped_to_id(self):
        items, warns = self.ai.normalize_items([make_item(cat="暖通")], self.cats)
        warm = [c for c in self.cats if c["name"] == "暖通"][0]
        self.assertEqual(items[0]["cat_id"], warm["id"])
        self.assertEqual(warns, [])

    def test_unknown_cat_warns_instead_of_guessing(self):
        items, warns = self.ai.normalize_items([make_item(cat="土建")], self.cats)
        self.assertIsNone(items[0]["cat_id"], "分类认不出就留空，不能瞎猜一个")
        self.assertTrue(any("土建" in w for w in warns), "分类认不出要告诉用户：%s" % warns)

    def test_choice_without_options_downgraded(self):
        items, _ = self.ai.normalize_items([{
            "qtype": "single", "stem": "题", "options": [], "answer": "A"}], self.cats)
        self.assertEqual(items[0]["qtype"], "qa", "选择题却没有选项，应降级为问答")

    def test_empty_stem_dropped(self):
        items, _ = self.ai.normalize_items([{"qtype": "qa", "stem": "  "}], self.cats)
        self.assertEqual(items, [])

    def test_no_answer_kept_but_empty(self):
        """没答案的题要留着（人工能补），但 answer 必须是空——绝不替模型编。"""
        items, _ = self.ai.normalize_items([
            {"qtype": "qa", "stem": "这题没有答案", "options": [], "answer": ""}], self.cats)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["answer"], "")


class P12Protocol(AIBase):
    """真走 HTTP：鉴权、重试、错误透传。"""

    def test_retry_then_success(self):
        self.fake.queue_fail(500, "boom")
        self.fake.queue_json([make_item()])
        items = self.ai.extract_text("1. 题干\nA. 甲\nB. 乙\n答案：A", self.cats)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(self.fake.count(), 2, "5xx 应该重试一次")

    def test_4xx_not_retried(self):
        self.fake.queue_fail(401, "Invalid API key")
        with self.assertRaises(self.ai.AIError) as ctx:
            self.ai.chat([{"role": "user", "content": "hi"}], retries=2)
        self.assertIn("Invalid API key", str(ctx.exception))
        self.assertEqual(self.fake.count(), 1, "密钥错重试没有意义，不该打三次")

    def test_request_shape(self):
        self.fake.queue_json([])
        self.ai.chat([{"role": "user", "content": "hi"}])
        req = self.fake.last
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["body"]["model"], "fake-chat")
        self.assertFalse(req["body"].get("stream"))
        self.assertIn("messages", req["body"])

    def test_system_prompt_lists_categories(self):
        self.fake.queue_json([])
        self.ai.extract_text("1. 题干\n答案：A", self.cats)
        body = self.fake.last["body"]
        sys_text = body["messages"][0]["content"]
        for c in self.cats:
            self.assertIn(c["name"], sys_text, "提示词里没列出分类 %s" % c["name"])
        self.assertIn("不要", sys_text, "提示词缺少「不要编答案」这类约束")

    def test_no_key_raises_before_http(self):
        self.ai.save_config({"clear_key": True})
        with self.assertRaises(self.ai.AIError):
            self.ai.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(self.fake.count(), 0, "没密钥就不该发请求")


class P12Orchestrate(AIBase):
    """编排：多块合并、部分失败、整体降级。"""

    def script(self, mapping):
        """按块内容决定返回什么；mapping 是 关键字 → 应答。

        没命中的块返回空数组（"这段里没有题目"），这样断言只统计命中的块，
        不会把填充段落产生的占位题混进来。
        """
        calls = []

        def fake_ask(chunk, cats, cfg):
            calls.append(chunk)
            for key, resp in mapping.items():
                if key in chunk:
                    if isinstance(resp, Exception):
                        raise resp
                    return resp
            return "[]"
        self.ai._ask_text = fake_ask
        return calls

    def blob(self, head, tail):
        """构造一段足够长的、按行排布的材料（不是超长单行，那样会被硬切）。"""
        filler = "\n".join("填充行 %d，用来把篇幅撑开" % i for i in range(200))
        return "%s\n%s\n%s" % (head, filler, tail)

    def test_merges_all_chunks(self):
        self.script({"第一段": json.dumps([make_item("第一题")], ensure_ascii=False),
                     "第二段": json.dumps([make_item("第二题")], ensure_ascii=False)})
        text = self.blob("第一段", "第二段")
        res = self.ai.extract_text(text, self.cats, cfg={"chunk_chars": 1500})
        self.assertEqual([i["stem"] for i in res["items"]], ["第一题", "第二题"])
        self.assertEqual(res["by"], "ai")
        self.assertGreater(res["chunks"], 1)

    def test_partial_failure_kept_partial(self):
        self.script({"好段": json.dumps([make_item("好题")], ensure_ascii=False),
                     "坏段": self.ai.AIError("blocked")})
        text = self.blob("好段", "坏段")
        res = self.ai.extract_text(text, self.cats, cfg={"chunk_chars": 1500})
        self.assertEqual(len(res["items"]), 1, "成功的那块要保住")
        self.assertEqual(res["by"], "ai+local", "部分失败要标出来，让用户重点核对")
        self.assertTrue(res["failed_chunks"])

    def test_total_failure_falls_back_to_local_rules(self):
        """模型彻底不可用时要退回本地拆题，不能让用户空手而归。"""
        self.ai._ask_text = lambda chunk, cats, cfg: (_ for _ in ()).throw(self.ai.AIError("down"))
        text = "1. 机房温度应保持在多少度？\nA. 18~27℃\nB. 5~10℃\n答案：A\n"
        res = self.ai.extract_text(text, self.cats)
        self.assertEqual(res["by"], "local")
        self.assertEqual(len(res["items"]), 1)
        self.assertTrue(any("本地规则" in w for w in res["warnings"]))

    def test_nothing_recognized(self):
        self.ai._ask_text = lambda chunk, cats, cfg: (_ for _ in ()).throw(self.ai.AIError("down"))
        res = self.ai.extract_text("随便一段没有题目的话。", self.cats)
        self.assertEqual(res["by"], "none")
        self.assertEqual(res["items"], [])

    def test_progress_callback(self):
        seen = []
        self.script({})
        text = self.blob("开头", "结尾")
        self.ai.extract_text(text, self.cats,
                             cfg={"chunk_chars": 1500}, on_chunk=lambda i, n: seen.append((i, n)))
        self.assertGreaterEqual(len(seen), 2, "多块时应逐步回报进度")
        self.assertEqual(seen[0][0], 1)
        self.assertEqual({n for _, n in seen}, {len(seen)}, "每次回报的总块数应一致")


class P12Vision(AIBase):
    """图片：请求体里必须是 image_url + base64，不是把图片当文字塞。"""

    def test_image_sent_as_data_url(self):
        self.fake.queue_json([make_item("图里的题")])
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * 200
        res = self.ai.extract_image(raw, "image/png", self.cats)
        self.assertEqual(res["by"], "ai-vision")
        body = self.fake.last["body"]
        parts = body["messages"][1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        url = parts[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"), url[:40])
        self.assertEqual(body["model"], "fake-vl", "图片应走视觉模型")

    def test_image_failure_message_is_actionable(self):
        self.fake.queue_fail(500, "vision down")
        with self.assertRaises(self.ai.AIError) as ctx:
            self.ai.extract_image(b"x", "image/jpeg", self.cats)
        self.assertIn("图片识别失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
