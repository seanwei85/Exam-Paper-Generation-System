#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 审校：给缺答案的题补答案/解析，或复核已有答案是否正确。

与 ai_parse 的分工要分清，否则两件事都会做坏：

  * ai_parse 负责「把文件变成题目」——材料里没写答案就必须留空，绝不编；
  * ai_review 负责「题已经进了库，答案该是什么」——这时编答案是本分，
    但编出来的东西必须能被机器校验、必须由人确认。

三条硬约束：
  1) **只出建议，不写库**。写库是用户逐题点「采纳」的事（server.adopt_review）。
  2) **建议必须可校验**：选择题答案要落在选项里、判断题只能是正确/错误、
     填空题空数要和题干下划线数对得上。校验不过只展示、不许采纳——
     一个落在选项范围外的答案，比没有答案更坑人。
  3) **自相矛盾的建议降级为存疑**：模型说「错了」却给出和原答案一样的答案、
     说「对了」却给了另一个答案，这不是结论，是噪音，一律按 unsure 处理。

只依赖标准库与 ai_parse / importer，不引入新依赖。
"""

import json
import re

import ai_parse
import importer

#: 建议的类型：fill = 补全（原答案为空），check = 复核（原答案已有）
KIND_FILL = "fill"
KIND_CHECK = "check"

#: 复核结论。filled 只可能出现在补全场景。
VERDICTS = ("filled", "ok", "wrong", "unsure")
VERDICT_NAMES = {"filled": "建议补全", "ok": "一致", "wrong": "不一致", "unsure": "存疑"}

#: 一次最多审校多少题。再多就该按筛选分批——模型一题几秒到几十秒，
#: 200 题一个请求跑十几分钟，中间断了全白跑。
MAX_BATCH = 60

TYPE_LABEL = {"single": "单项选择题", "multi": "多项选择题", "judge": "判断题",
              "fill": "填空题", "qa": "问答题"}

SYSTEM_PROMPT = """你是机房运维（数据中心电气、暖通、弱电、消防）题库的审校员。你面对的是已经成型的题目，任务是判定标准答案并写出解析。

**只输出一个 JSON 对象**，不要 markdown 代码块、不要任何解释文字。

字段与格式：
- answer：标准答案。单选题给单个字母如 "B"；多选题给连续字母如 "ABD"；判断题给 "正确" 或 "错误"；填空题多个空按顺序用竖线分隔如 "27|60"；问答题给分点陈述的参考答案。
- analysis：解析。说明为什么是这个答案，讲清原理或依据，不要复述题干。
- verdict：ok（现有答案正确）/ wrong（现有答案错误）/ unsure（拿不准）。用户没给现有答案时给 "filled"。
- reason：判断依据，一到两句，写规范编号与条款、或电气/暖通原理。**记不清规范号就不要写编号**，只写原理，绝不编造条款号。
- confidence：0 到 1 的小数，表示你对答案的把握。

硬性规则：
1. 题干本身有歧义、选项互相排斥不清、答案依赖缺失的图或表时，verdict 给 "unsure"，并在 reason 里说明缺什么——不要硬猜。
2. 填空题的答案个数必须与题干中下划线空位的个数一致。
3. 数值类答案（温度、时间、电压、电流、间距）按现行国家标准取值；若标准存在多个版本口径，在 reason 里点明按哪一版。
4. 现有解析若与答案矛盾，要在 analysis 里给出正确解析，并在 reason 里指出矛盾点。
5. 输出必须是合法 JSON 对象，字符串内部不要换行符，需要分行用 \n。"""


class ReviewError(Exception):
    """审校失败。与 ai_parse.AIError 区分：这里更多是"模型答得不合格"。"""


# ---------------------------------------------------------------- 应答解析

_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```")


def load_obj(content):
    """从模型回复里抠出一个 JSON 对象。

    容忍 ``` 包裹、前后寒暄、单元素数组；抠不出来就报错，不猜内容。
    """
    text = (content or "").strip()
    if not text:
        raise ReviewError("模型没有返回内容")
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    cands = [text]
    lb, rb = text.find("{"), text.rfind("}")
    if lb >= 0 and rb > lb:
        cands.append(text[lb:rb + 1])
    for cand in cands:
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                obj = json.loads(attempt)
            except Exception:
                continue
            if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                return obj[0]
            if isinstance(obj, dict):
                return obj
    raise ReviewError("模型输出不是 JSON 对象：%s" % text[:120])


def _option_keys(options):
    return {str(o.get("key") or "").strip().upper() for o in (options or [])
            if str(o.get("key") or "").strip()}


def shape_answer(qtype, options, raw, blanks=0):
    """把模型给的答案洗成系统能存的形态，顺便做合法性校验。

    返回 (answer, problem)。problem 非空 = 这条建议不可采纳，但仍会展示出来，
    让用户看到"AI 想说 D，可这题没有 D"——比直接吞掉更有用。
    """
    raw = (raw or "").strip()
    if qtype in ("single", "multi"):
        ans = importer._clean_answer(qtype, raw)
        if not ans:
            return "", "模型没给出答案"
        keys = _option_keys(options)
        if keys and not set(ans).issubset(keys):
            return ans, "答案 %s 不在选项范围（%s）内" % (ans, "".join(sorted(keys)))
        if qtype == "single" and len(ans) != 1:
            return ans, "单选题给了 %d 个答案" % len(ans)
        if qtype == "multi" and len(ans) < 2:
            return ans, "多选题只给了 1 个答案"
        return ans, ""
    if qtype == "judge":
        ans = importer._clean_answer("judge", raw)
        if ans not in ("正确", "错误"):
            return ans, "判断题只能是“正确”或“错误”"
        return ans, ""
    if qtype == "fill":
        parts = [p.strip() for p in re.split(r"[|｜；;]", raw) if p.strip()]
        ans = "|".join(parts)
        if not parts:
            return "", "模型没给出答案"
        if blanks and len(parts) != int(blanks):
            return ans, "题干有 %d 个空，模型给了 %d 个答案" % (int(blanks), len(parts))
        return ans, ""
    ans = raw.strip()
    if len(ans) < 2:
        return ans, "模型没给出参考答案"
    return ans, ""


def _same_answer(qtype, a, b):
    """两条答案是否等价。选择题比字母集合，其余比去空白后的文本。"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    if qtype in ("single", "multi"):
        return set(a.upper()) == set(b.upper())
    return re.sub(r"\s+", "", a) == re.sub(r"\s+", "", b)


_VERDICT_ALIAS = {
    "ok": "ok", "一致": "ok", "correct": "ok", "right": "ok", "正确": "ok",
    "wrong": "wrong", "不一致": "wrong", "mismatch": "wrong", "incorrect": "wrong",
    "错误": "wrong", "不对": "wrong",
    "unsure": "unsure", "存疑": "unsure", "uncertain": "unsure", "unknown": "unsure",
    "不确定": "unsure", "?": "unsure",
    "filled": "filled", "补全": "filled", "": "filled",
}


def _with_note(reason, note):
    """在原有依据后面追一句说明；原本没有依据时就用说明本身，别留一对空括号。"""
    return (reason + "（" + note + "）").strip() if reason else note


def normalize_verdict(value, kind):
    key = str(value or "").strip().lower()
    got = _VERDICT_ALIAS.get(key, "unsure")
    # 补全场景没有"现有答案"，一切结论都只能是 filled
    return "filled" if kind == KIND_FILL else got


def parse_reply(content, q, kind=None):
    """模型回复 → 一条可入库的建议记录（纯函数，便于用例直接喂字符串）。

    q 需要 qid / qtype / options / answer / blanks / stem。
    """
    obj = load_obj(content)
    qtype = q.get("qtype") or "qa"
    options = q.get("options") or []
    cur = (q.get("answer") or "").strip()
    kind = kind or (KIND_FILL if not cur else KIND_CHECK)

    raw_ans = obj.get("answer")
    if isinstance(raw_ans, list):
        raw_ans = "".join(str(x) for x in raw_ans)
    answer, problem = shape_answer(qtype, options, str(raw_ans if raw_ans is not None else ""),
                                   q.get("blanks") or 0)

    analysis = str(obj.get("analysis") or "").strip()[:2000]
    reason = str(obj.get("reason") or obj.get("why") or "").strip()[:400]
    verdict = normalize_verdict(obj.get("verdict"), kind)

    if kind == KIND_CHECK:
        if verdict == "ok" and answer and not _same_answer(qtype, answer, cur):
            verdict = "unsure"
            reason = _with_note(reason, "模型判“一致”，但给出的答案与原答案不同，按存疑处理")
        elif verdict == "wrong" and _same_answer(qtype, answer, cur):
            verdict = "unsure"
            reason = _with_note(reason, "模型判“不一致”，但给出的答案与原答案相同，按存疑处理")

    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    return {
        "qid": q.get("id"),
        "kind": kind,
        "verdict": verdict,
        "suggest_answer": answer,
        "suggest_analysis": analysis,
        "reason": reason,
        "confidence": conf,
        "valid": 0 if problem else 1,
        "problem": problem,
    }


# ---------------------------------------------------------------- 提问

def build_messages(q, kind):
    """拼一次性提问。一题一个请求：题目短，分开问比批量问更准、失败也只丢一题。"""
    qtype = q.get("qtype") or "qa"
    lines = ["【题型】" + TYPE_LABEL.get(qtype, qtype),
             "【一级分类】" + (q.get("cat_name") or "未知"),
             "【题干】" + (q.get("stem") or "").strip()]
    opts = [o for o in (q.get("options") or []) if (o.get("text") or "").strip()]
    if opts:
        lines.append("【选项】" + "；".join(
            "%s. %s" % (o.get("key"), o.get("text")) for o in opts))
    cur = (q.get("answer") or "").strip()
    lines.append("【现有答案】" + (cur if cur else "（空，这题没有答案）"))
    ana = (q.get("analysis") or "").strip()
    if ana:
        lines.append("【现有解析】" + ana)
    if kind == KIND_FILL:
        lines.append("这题缺答案，请给出标准答案与解析，verdict 给 filled。")
    else:
        lines.append("请复核【现有答案】是否正确，判断现有解析是否与答案矛盾，"
                     "并给出你认为的标准答案与解析。")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def review_one(q, cfg=None, kind=None):
    """调一次模型，返回建议记录（不写库）。失败抛 AIError / ReviewError。"""
    cfg = cfg or ai_parse.load_config()
    if not ai_parse.ready(cfg):
        raise ai_parse.AIError(
            "还没配 AI 模型：到「系统设置 · AI 识别」填接口地址和 API Key 后再用 AI 审校")
    cur = (q.get("answer") or "").strip()
    kind = kind or (KIND_FILL if not cur else KIND_CHECK)
    content = ai_parse.chat(build_messages(q, kind), cfg=cfg, max_tokens=1200)
    rec = parse_reply(content, q, kind)
    rec["model"] = cfg.get("model") or ""
    return rec
