#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络 AI 识别通道：把"排版五花八门"的材料直接转成结构化题目。

设计原则：
  1) **只做识别，不做入库**。结果一律回到原有的核对表，人工确认后才写库。
  2) **一切外发内容都可关**。没配密钥时本模块整体不可用，导入退回本地规则。
  3) **失败必须能降级**。整块调用失败 → 二分重试 → 仍失败 → 回退本地正则拆题，
     宁可少识别几题，也不让用户"看着像成功、实际全是错题"。
  4) 只用标准库（urllib / json），不引入 openai 之类依赖。
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

import server as core
import importer

# 重试退避基数（秒）。测试里会把它调成 0，免得一条失败用例白等 3 秒。
RETRY_BACKOFF = 1.2
MAX_RETRIES = 2

DEFAULT_CONFIG = {
    "enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "vision_model": "",
    "timeout": 300,
    "chunk_chars": 6000,
    # 视觉能力探测结果（P31，FR-IM-29）：vision_checked 记录判定针对的模型名，
    # 换模型自动失效。None = 还没测过（网络失败也不缓存，下次再试）。
    "vision_ok": None,
    "vision_checked": "",
}

CONFIG_KEYS = ("enabled", "base_url", "model", "vision_model",
               "timeout", "chunk_chars", "vision_ok", "vision_checked")


class AIError(Exception):
    pass


# ---------------------------------------------------------------- 配置

def config_path():
    """配置文件与数据库同目录：备份、迁移时一起带走，不会漏。"""
    return os.path.join(core.DATA_DIR, "ai.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if not os.path.exists(path):
        return cfg
    try:
        with open(path, encoding="utf-8") as fh:
            stored = json.load(fh)
    except Exception:
        return cfg
    if isinstance(stored, dict):
        for k in CONFIG_KEYS:
            if k in stored:
                cfg[k] = stored[k]
        if stored.get("api_key"):
            cfg["api_key"] = stored["api_key"]
    return cfg


def save_config(payload):
    """保存配置。

    密钥留空表示"不改"——界面上是密码框，回显为空，只改模型名时不能把密钥抹掉。
    真要清空得明确传 clear_key。
    """
    cfg = load_config()
    for k in CONFIG_KEYS:
        if k in payload and payload[k] is not None:
            cfg[k] = payload[k]
    if payload.get("clear_key"):
        cfg["api_key"] = ""
    elif payload.get("api_key"):
        cfg["api_key"] = str(payload["api_key"]).strip()
    try:
        cfg["timeout"] = int(cfg.get("timeout") or 300)
        cfg["chunk_chars"] = int(cfg.get("chunk_chars") or 6000)
    except (TypeError, ValueError):
        cfg["timeout"], cfg["chunk_chars"] = 300, 6000
    # 0 或负数等于一个请求都发不出去，按默认值兜住
    if cfg["timeout"] < 30:
        cfg["timeout"] = 300
    os.makedirs(os.path.dirname(config_path()), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def public_config(cfg=None):
    """给界面看的版本：密钥只留末 4 位。"""
    cfg = cfg or load_config()
    key = cfg.get("api_key") or ""
    out = {k: cfg.get(k) for k in CONFIG_KEYS}
    out["api_key_masked"] = ("****" + key[-4:]) if key else ""
    out["has_key"] = bool(key)
    out["ready"] = ready(cfg)
    return out


def ready(cfg=None):
    cfg = cfg or load_config()
    return bool((cfg.get("api_key") or "").strip() and (cfg.get("base_url") or "").strip())


# ---------------------------------------------------------------- 调用

def _post(url, body, key, timeout):
    """返回 (status, obj, err)。status=0 表示连接层就失败了。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8")), ""
            except Exception:
                return resp.status, None, "返回内容不是 JSON：%s" % raw[:160]
    except urllib.error.HTTPError as exc:
        try:
            obj = json.loads(exc.read().decode("utf-8", "ignore"))
        except Exception:
            obj = None
        return exc.code, obj, "HTTP %s" % exc.code
    except Exception as exc:                      # 超时、DNS、连接被拒
        return 0, None, str(exc)


def _err_text(status, obj, err):
    if isinstance(obj, dict):
        e = obj.get("error")
        if isinstance(e, dict) and e.get("message"):
            return "%s（%s）" % (e["message"], status or err)
        if isinstance(e, str) and e:
            return "%s（%s）" % (e, status or err)
        if obj.get("message"):
            return "%s（%s）" % (obj["message"], status or err)
    return "调用失败：%s" % (err or ("HTTP %s" % status))


def chat(messages, cfg=None, model=None, max_tokens=None, retries=None, timeout=None):
    cfg = cfg or load_config()
    if not (cfg.get("api_key") or "").strip():
        raise AIError("还没填 API Key")
    if not (cfg.get("base_url") or "").strip():
        raise AIError("还没填接口地址")
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {
        "model": model or cfg.get("model") or "gpt-4o-mini",
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
    }
    if max_tokens:
        body["max_tokens"] = max_tokens

    retries = MAX_RETRIES if retries is None else retries
    timeout = timeout or cfg.get("timeout") or 300
    last = ""
    for attempt in range(retries + 1):
        status, obj, err = _post(url, body, cfg["api_key"], timeout)
        if status == 200 and isinstance(obj, dict):
            try:
                return obj["choices"][0]["message"]["content"]
            except Exception:
                last = "返回格式不像 chat/completions：%s" % json.dumps(obj, ensure_ascii=False)[:200]
        else:
            last = _err_text(status, obj, err)
            # 4xx 里除了限流，重试也没用（密钥错、模型名错、余额不足）
            if status and 400 <= status < 500 and status != 429:
                raise AIError(last)
        if attempt < retries:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise AIError(last or "调用失败")


def test_connection(cfg=None):
    cfg = cfg or load_config()
    started = time.time()
    try:
        content = chat([{"role": "user", "content": "回一个字：好"}],
                       cfg=cfg, max_tokens=8, retries=0, timeout=25)
    except AIError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "model": cfg.get("model"), "sample": (content or "")[:20],
            "ms": int((time.time() - started) * 1000)}


# ---------------------------------------------------------------- 视觉探测

#: 探测图上写的字符。模型要"抄出来"才算看图，防止只回一句客套话就误判。
PROBE_TEXT = "AB12"

#: 5x7 点阵，纯标准库画字，不依赖任何绘图库
_PROBE_FONT = {
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
}

_PROBE_CACHE = {}


def _png_chunk(tag, data):
    import struct
    import zlib
    c = tag + data
    return (struct.pack(">I", len(data)) + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))


def _probe_png_bytes():
    """一张白底黑字写着 AB12 的小 PNG（约 260x88）。进程内只画一次。"""
    import struct
    import zlib
    if _PROBE_CACHE.get("png"):
        return _PROBE_CACHE["png"]
    scale, gap, margin = 8, 3, 2
    gw = 5 * len(PROBE_TEXT) + gap * (len(PROBE_TEXT) - 1)
    w, h = (gw + 2 * margin) * scale, (7 + 2 * margin) * scale
    grid = [[0] * gw for _ in range(7)]
    x = 0
    for ch in PROBE_TEXT:
        for ry, row in enumerate(_PROBE_FONT[ch]):
            for rx, v in enumerate(row):
                if v == "1":
                    grid[ry][x + rx] = 1
        x += 5 + gap
    raw = bytearray()
    for y in range(h):
        gy = y // scale - margin
        raw.append(0)                                   # filter type 0
        for px in range(w):
            gx = px // scale - margin
            on = 0 <= gy < 7 and 0 <= gx < gw and grid[gy][gx]
            raw += b"\x00\x00\x00" if on else b"\xff\xff\xff"
    png = (b"\x89PNG\r\n\x1a\n"
           + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + _png_chunk(b"IEND", b""))
    _PROBE_CACHE["png"] = png
    return png


def vision_model_name(cfg=None):
    """看图用哪个模型：单独配了视觉模型用它，否则用主模型。"""
    cfg = cfg or load_config()
    return (cfg.get("vision_model") or "").strip() or (cfg.get("model") or "").strip()


def vision_verdict(cfg=None):
    """当前缓存里的视觉能力判定：True/False；没测过或换了模型返回 None。"""
    cfg = cfg or load_config()
    if not ready(cfg):
        return None
    if (cfg.get("vision_checked") or "") != vision_model_name(cfg):
        return None
    ok = cfg.get("vision_ok")
    return None if ok is None else bool(ok)


def _probe_answer_hit(content):
    """回答里真抄出了 AB12 才算看图（NFKC 归一，防全角/空格干扰）。"""
    import unicodedata
    norm = unicodedata.normalize("NFKC", re.sub(r"\s+", "", content or "")).upper()
    return PROBE_TEXT in norm


def probe_vision(cfg=None):
    """探测当前模型能不能真的看图（FR-IM-29）。

    为什么发一张带字的图、还要检查回答：有些网关把图静默丢弃照样回 200
    （实测 DeepSeek-V4-Flash 回"请上传图片"），只看状态码必然误判。
    返回 True/False（结果写进配置缓存，键带模型名）；
    网络级失败返回 None（**不缓存**——断网不代表模型不支持，下次导入再试）；
    没配置 AI 抛 AIError。
    """
    cfg = cfg or load_config()
    if not ready(cfg):
        raise AIError("还没配置 AI：先到「系统设置 · AI 识别」填好接口地址和密钥")
    b64 = base64.b64encode(_probe_png_bytes()).decode()
    messages = [{"role": "user", "content": [
        {"type": "text", "text": "这张图片里写了什么？只输出图中那几个字符，不要解释。"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
    ]}]
    try:
        content = chat(messages, cfg=cfg, model=vision_model_name(cfg),
                       max_tokens=200, retries=0,
                       timeout=min(int(cfg.get("timeout") or 300), 120))
    except AIError:
        return None
    ok = _probe_answer_hit(content)
    save_config({"vision_ok": ok, "vision_checked": vision_model_name(cfg)})
    return ok


# ---------------------------------------------------------------- 提示词

def _cat_names(cats):
    return "、".join(c["name"] for c in (cats or []) if c.get("name")) or "电气、暖通、弱电、消防、管理"


SYSTEM_PROMPT = """你是机房运维题库的录入助手。把用户给的材料转成结构化题目，**只输出一个 JSON 数组**，不要任何解释、不要 markdown 代码块以外的文字。

每个元素固定这些字段：
- qtype：只能是 single（单选）/ multi（多选）/ judge（判断）/ fill（填空）/ qa（问答）之一
- stem：题干本身。不要包含题号、不要包含选项文字、不要包含答案。填空处保留下划线
- options：选择题的选项数组，形如 [{"key":"A","text":"选项文字"}]；非选择题给 []
- answer：单选题给单个字母如 "A"；多选题给连续字母如 "ABD"；判断题给 "正确" 或 "错误"；填空题多个空按顺序用竖线分隔，如 "27|60"；问答题给参考答案要点
- diff：easy / mid / hard 三选一，判断不出来就给 mid
- cat：一级分类，必须从这些里面选一个：{cats}
- analysis：解析，没有就给空字符串

硬性规则：
1. 材料里没有给出答案的题，answer 给空字符串，**绝对不要自己编答案**。
2. 题干里的题型标题（如"一、单项选择题"）不是题目，不要输出。
3. 一道题一条记录，不要把两道题合并，也不要把一道题拆成两条。
4. 分不清题型的按 qa 处理。
5. 输出必须是合法 JSON 数组，不要带注释。"""


def _system_prompt(cats):
    return SYSTEM_PROMPT.replace("{cats}", _cat_names(cats))


# ---------------------------------------------------------------- 分块

_NUM_LINE = re.compile(r"^\s*\d{1,3}\s*[\.、．\)）]")


def chunk_text(text, limit=6000):
    """按行累加切块，尽量切在题号行上。

    不按字符硬切：硬切会把一道题的题干和它的选项劈到两个块里，模型只能各猜一半，
    拼回来就是错题。但单行本身就超上限（整段没有换行、或表格被压成一行）时没有
    选择，只能把那一行按上限硬切——否则一个块能塞进几万字，直接超出模型上下文。
    """
    limit = max(1500, int(limit or 6000))
    chunks, buf, size = [], [], 0

    def flush():
        nonlocal buf, size
        if buf:
            chunks.append("\n".join(buf))
        buf, size = [], 0

    for line in (text or "").split("\n"):
        if len(line) + 1 > limit:
            # 超长单行：先把已攒的收掉，再把这行硬切成几段
            flush()
            step = limit
            for i in range(0, len(line), step):
                chunks.append(line[i:i + step])
            continue
        n = len(line) + 1
        if buf and size + n > limit:
            # 到了题号行就切开；否则再忍一忍，直到超过 1.5 倍上限
            if _NUM_LINE.match(line) or size > limit * 1.5:
                flush()
        buf.append(line)
        size += n
    flush()
    return [c for c in chunks if c.strip()]


# ---------------------------------------------------------------- 解析与校验

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(content):
    """从模型输出里抠出 JSON 数组。

    模型很爱加"好的，以下是结果："和 ``` 包裹，或者把数组包在 {"items":[...]} 里，
    这些都容忍——但绝不能"猜"内容，抠不出来就报错，让上层降级。
    """
    text = (content or "").strip()
    if not text:
        raise AIError("模型没有返回内容")

    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()

    candidates = [text]
    lb, rb = text.find("["), text.rfind("]")
    if lb >= 0 and rb > lb:
        candidates.append(text[lb:rb + 1])
    lb, rb = text.find("{"), text.rfind("}")
    if lb >= 0 and rb > lb:
        candidates.append(text[lb:rb + 1])

    for cand in candidates:
        for attempt in (cand, re.sub(r",\s*([\]}])", r"\1", cand)):
            try:
                obj = json.loads(attempt)
            except Exception:
                continue
            if isinstance(obj, list):
                return obj
            if isinstance(obj, dict):
                for key in ("items", "questions", "data", "list", "result"):
                    if isinstance(obj.get(key), list):
                        return obj[key]
                # 单个对象也当一条
                if obj.get("stem"):
                    return [obj]
    raise AIError("模型输出不是可解析的 JSON 数组：%s" % (content or "")[:120])


_QTYPE_ALIAS = {}
for _k, _names in {
    "single": ("single", "单选", "单项选择", "单项选择题", "单选题"),
    "multi": ("multi", "多选", "多项选择", "多项选择题", "多选题", "不定项"),
    "judge": ("judge", "判断", "判断题", "是非题"),
    "fill": ("fill", "填空", "填空题"),
    "qa": ("qa", "问答", "问答题", "简答", "简答题", "论述", "论述题", "计算", "案例"),
}.items():
    for _n in _names:
        _QTYPE_ALIAS[_n] = _k
for _n in list(_QTYPE_ALIAS):
    _QTYPE_ALIAS[_n.lower()] = _QTYPE_ALIAS[_n]

_DIFF_ALIAS = {
    "easy": "easy", "易": "easy", "简单": "easy", "容易": "easy",
    "mid": "mid", "中": "mid", "中等": "mid", "一般": "mid",
    "hard": "hard", "难": "hard", "困难": "hard", "较难": "hard",
}


def _qtype_of(value, stem, options, answer):
    raw = str(value or "").strip()
    hit = _QTYPE_ALIAS.get(raw) or _QTYPE_ALIAS.get(raw.lower())
    if hit:
        return hit
    text = raw.replace(" ", "")
    for name, key in (("多项选择", "multi"), ("多选", "multi"), ("单项选择", "single"),
                      ("单选", "single"), ("判断", "judge"), ("填空", "fill"),
                      ("问答", "qa"), ("简答", "qa"), ("论述", "qa")):
        if name in text:
            return key
    return importer._guess_type(stem, options, answer)


def _options_of(raw):
    """统一成 [{"key":"A","text":"..."}]，key 大写、按字母序。"""
    out = []
    if isinstance(raw, dict):
        raw = [{"key": k, "text": v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return out
    for i, opt in enumerate(raw):
        if isinstance(opt, dict):
            key = str(opt.get("key") or opt.get("label") or "").strip()
            text = str(opt.get("text") or opt.get("value") or opt.get("content") or "").strip()
        else:
            key, text = "", str(opt).strip()
        if not text:
            continue
        if not key:
            key = chr(ord("A") + i)
        key = key.strip(" ()（）.、．:：")[:1].upper() or chr(ord("A") + i)
        # 模型有时把 "A. xxx" 整串塞进 text
        text = re.sub(r"^\s*[A-Ha-h]\s*[\.、．\)）:：]\s*", "", text)
        out.append({"key": key, "text": text})
    out.sort(key=lambda o: o["key"])
    seen, uniq = set(), []
    for o in out:
        if o["key"] in seen:
            continue
        seen.add(o["key"])
        uniq.append(o)
    return uniq


def normalize_items(raw_items, cats=None, default_diff="mid"):
    """把模型给的字段洗成系统认的字段。有一项不合格就整条丢弃，不做"猜"。"""
    items, warns = [], []
    for i, q in enumerate(raw_items or [], 1):
        if not isinstance(q, dict):
            continue
        stem = str(q.get("stem") or q.get("question") or "").strip()
        if not stem:
            continue
        opts = _options_of(q.get("options") or q.get("choices") or [])
        answer_raw = q.get("answer")
        if isinstance(answer_raw, list):
            answer_raw = "".join(str(a) for a in answer_raw)
        answer = str(answer_raw or "").strip()

        qtype = _qtype_of(q.get("qtype"), stem, opts, answer)
        answer = importer._clean_answer(qtype, answer)
        if qtype in ("single", "multi") and len(opts) < 2:
            qtype = "qa" if not opts else qtype

        diff = _DIFF_ALIAS.get(str(q.get("diff") or "").strip().lower(),
                               _DIFF_ALIAS.get(str(q.get("diff") or "").strip(), default_diff))

        cat_id = None
        cat_text = str(q.get("cat") or q.get("category") or "").strip()
        if cat_text and cats:
            cat_id = importer._match_cat(cat_text, cats)
            if cat_id is None:
                warns.append("第 %d 题的分类「%s」不在现有分类里，已留空待你选" % (i, cat_text))
        if cat_id is None and cats:
            cat_id = importer._match_cat(stem, cats)

        items.append({
            "qtype": qtype,
            "stem": stem,
            "options": opts,
            "answer": answer,
            "diff": diff,
            "analysis": str(q.get("analysis") or "").strip(),
            "cat_id": cat_id,
            "sub_id": None,
            "tags": str(q.get("tags") or "").strip(),
            "raw_answer": answer,
        })
    return items, warns


# ---------------------------------------------------------------- 抽取

def _ask_text(chunk, cats, cfg):
    messages = [
        {"role": "system", "content": _system_prompt(cats)},
        {"role": "user", "content": "材料如下，请转换：\n\n" + chunk},
    ]
    return chat(messages, cfg=cfg)


def _split_half(text):
    lines = text.split("\n")
    if len(lines) < 4:
        return None
    mid = len(lines) // 2
    for i in range(mid, len(lines)):
        if _NUM_LINE.match(lines[i]):
            mid = i
            break
    return "\n".join(lines[:mid]), "\n".join(lines[mid:])


def extract_text(text, cats=None, cfg=None, on_chunk=None):
    """整段文本 → 题目列表。

    返回 {"items", "warnings", "chunks", "by"}；by 说明这批题是怎么来的：
    ai（模型识别）/ ai+local（部分块失败、回退本地）/ local（模型完全不可用）。
    """
    cfg = cfg or load_config()
    chunks = chunk_text(text, (cfg or {}).get("chunk_chars") or 6000)
    items, warns, ok_chunks, failed = [], [], 0, []

    for idx, chunk in enumerate(chunks, 1):
        if on_chunk:
            on_chunk(idx, len(chunks))
        try:
            raw = _ask_text(chunk, cats, cfg)
            got, w = normalize_items(extract_json(raw), cats)
        except AIError as exc:
            half = _split_half(chunk)
            if half:
                got, w = [], []
                for sub in half:
                    try:
                        raw = _ask_text(sub, cats, cfg)
                        g, ww = normalize_items(extract_json(raw), cats)
                        got.extend(g)
                        w.extend(ww)
                        ok_chunks += 1
                    except AIError:
                        failed.append(idx)
                if not got:
                    failed.append(idx)
            else:
                got, w = [], []
                failed.append(idx)
                warns.append("第 %d 段识别失败：%s" % (idx, exc))
        items.extend(got)
        warns.extend(w)
        if got:
            ok_chunks += 1

    by = "ai"
    if not items:
        # 模型完全用不上：退回本地正则拆题，至少别让用户白跑一趟
        local = importer.split_questions(text, cats=cats)
        local = [q for q in local if q.get("stem")]
        if local:
            items = local
            by = "local"
            warns.append("AI 识别没有拿到结果，已改用本地规则拆出 %d 题，请重点核对" % len(local))
        else:
            by = "none"
    elif failed:
        by = "ai+local"

    return {"items": items, "warnings": warns, "chunks": len(chunks), "by": by,
            "failed_chunks": failed}


def extract_image(raw, mime, cats=None, cfg=None):
    """图片（拍照/扫描）走多模态。"""
    cfg = cfg or load_config()
    model = cfg.get("vision_model") or cfg.get("model")
    b64 = base64.b64encode(raw).decode()
    messages = [
        {"role": "system", "content": _system_prompt(cats)},
        {"role": "user", "content": [
            {"type": "text", "text": "这是一张试卷或题库的照片，请把上面的题目全部转成 JSON 数组。"},
            {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (mime, b64)}},
        ]},
    ]
    try:
        answer = chat(messages, cfg=cfg, model=model)
        items, warns = normalize_items(extract_json(answer), cats)
    except AIError as exc:
        raise AIError("图片识别失败：%s" % exc)
    if not items:
        raise AIError("这张图里没有识别到题目。如果是模型看不懂图，到「系统设置 · AI 识别」"
                      "单独填一个支持看图的「视觉模型」；否则换一张更清晰的图。")
    return {"items": items, "warnings": warns, "chunks": 1, "by": "ai-vision",
            "failed_chunks": []}


def vision_error_hint(err):
    """视觉通道失败时给下一步：**超时和"看不了图"是两码事**。

    混成一句"可能不支持看图"，用户会去换一个根本不必要换的模型，而真正该调的
    超时（推理模型一页三五分钟）没人提。所以这里按错误内容分岔。
    """
    low = (err or "").lower()
    if "timed out" in low or "timeout" in low or "超时" in (err or ""):
        return ("模型比设置的超时还慢：到「系统设置 · AI 识别」把「超时（秒）」调大再导一次"
                "（带思维链的推理模型一页可能要三五分钟）。")
    return ("如果模型本身不支持看图，到「系统设置 · AI 识别」单独填一个"
            "支持看图的「视觉模型」；否则检查这几页是否太模糊。")


def extract_pdf_images(raw, cats=None, cfg=None, on_page=None):
    """图片型 PDF：自己把每页的内嵌图抽出来，逐页交给视觉模型。

    为什么不让用户先导成图片：老卷子往往几十页，手工导出再逐张上传没人愿意干。
    PDF 里每页本来就是一张完整位图，`pdftext.page_images` 用 zlib 就能解出来、
    按页面矩阵摆正、连 PNG 都是现写的——不需要渲染器，也不需要第三方库。
    """
    import pdftext
    cfg = cfg or load_config()
    try:
        pages = pdftext.page_images(raw)
    except pdftext.PdfError as exc:
        raise AIError(str(exc))
    if not pages:
        raise AIError("这份 PDF 是图片型，但里面抽不出整页图片（可能是矢量绘制，"
                      "或用了 JPEG2000/传真压缩）。请用 PDF 阅读器把每页另存为 PNG 再上传。")

    items, warns, failed = [], [], []
    for idx, pg in enumerate(pages, 1):
        if on_page:
            on_page(idx, len(pages))
        try:
            res = extract_image(pg["png"], "image/png", cats=cats, cfg=cfg)
        except AIError as exc:
            failed.append({"page": pg["page"], "error": str(exc)})
            warns.append("第 %d 页没识别出来：%s" % (pg["page"], exc))
            continue
        items.extend(res["items"])
        for w in res["warnings"]:
            warns.append("第 %d 页：%s" % (pg["page"], w))
    if not items:
        first = (failed[0]["error"] if failed else "")
        raise AIError("这份 PDF 的 %d 页图片都没识别出题目。%s\n%s"
                      % (len(pages), first, vision_error_hint(first)))
    return {"items": items, "warnings": warns, "chunks": len(pages),
            "by": "ai-vision" if not failed else "ai-vision+partial",
            "failed_chunks": failed}
