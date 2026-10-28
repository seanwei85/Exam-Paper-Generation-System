# -*- coding: utf-8 -*-
"""视觉通道 vs 本地 OCR 的对照实验。

用法（在仓库根目录跑）：
    python tools/vision_bench.py probe  --page 1 --models Kimi-K3,GLM-5.3,...
    python tools/vision_bench.py extract --page 1 --models Kimi-K3,DeepSeek-V4.1-Flash
    python tools/vision_bench.py local  --page 1 2

设计要点：
- 用产品自己的提示词与归一化，测出来的题数才是"真能入库的题数"，不是模型嘴上的题数。
- 单页一次请求，计时和 token 都按页算，避免多页混在一起没法归因。
- usage 原样落盘：prompt_tokens 里含图片 token，completion_tokens 里含思维链 token，
  推理模型的"贵"主要体现在后者，不记下来就看不清账。
"""
import base64
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SYS_DIR = os.path.join(ROOT, "机房运维试卷系统")
sys.path.insert(0, SYS_DIR)

import ai_parse          # noqa: E402
import importer          # noqa: E402
import ocr_win           # noqa: E402
import pdftext           # noqa: E402

FIXTURE = os.path.join(SYS_DIR, "tests", "fixtures", "image_paper.pdf")
OUT = os.path.join(ROOT, "_bench_result.json")


def pages():
    raw = open(FIXTURE, "rb").read()
    return pdftext.page_images(raw)


def _usage_of(obj):
    u = (obj or {}).get("usage") or {}
    return {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _call(model, messages, max_tokens, cfg, timeout=900):
    """不走 ai_parse.chat，因为它把 usage 丢了。协议与 chat() 完全一致。"""
    import urllib.request
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": messages, "temperature": 0.1,
            "stream": False, "max_tokens": max_tokens}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + cfg["api_key"]})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        obj = json.loads(resp.read().decode("utf-8", "replace"))
    dt = round(time.time() - t0, 1)
    msg = obj["choices"][0]["message"]
    return {"content": msg.get("content") or "", "reasoning": msg.get("reasoning_content") or "",
            "finish": obj["choices"][0].get("finish_reason"),
            "usage": _usage_of(obj), "sec": dt}


def _img_b64(pg):
    return "data:image/png;base64," + base64.b64encode(pg["png"]).decode()


def probe(page, models):
    cfg = ai_parse.load_config()
    pg = pages()[page - 1]
    out = []
    for m in models:
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "把这张图片里的文字原样抄出来，只输出文字，不要解释。"},
            {"type": "image_url", "image_url": {"url": _img_b64(pg)}}]}]
        try:
            r = _call(m, msgs, 1200, cfg)
            txt = r["content"].strip()
            hit = [k for k in ("机房温度", "MySQL", "答案", "选择题") if k in txt]
            out.append({"model": m, "ok": True, "sec": r["sec"], "chars": len(txt),
                        "hits": hit, "usage": r["usage"], "finish": r["finish"],
                        "head": txt[:120]})
            print(f"[看图 OK] {m}  {r['sec']}s  {len(txt)}字  命中{hit}  {r['usage']}", flush=True)
        except Exception as exc:
            out.append({"model": m, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            print(f"[看图 失败] {m}  {type(exc).__name__}: {exc}", flush=True)
    return out


def extract(page, models):
    cfg = ai_parse.load_config()
    pg = pages()[page - 1]
    out = []
    for m in models:
        msgs = [
            {"role": "system", "content": ai_parse._system_prompt(None)},
            {"role": "user", "content": [
                {"type": "text", "text": "这是一张试卷或题库的照片，请把上面的题目全部转成 JSON 数组。"},
                {"type": "image_url", "image_url": {"url": _img_b64(pg)}}]}]
        try:
            r = _call(m, msgs, 6000, cfg)
            try:
                raw_items = ai_parse.extract_json(r["content"])
            except Exception as exc:
                raw_items = []
                err = f"JSON 解析失败: {exc}"
            items, warns = ai_parse.normalize_items(raw_items, None)
            with_ans = sum(1 for it in items if (it.get("answer") or "").strip())
            with_opt = sum(1 for it in items if it.get("options"))
            out.append({"model": m, "ok": True, "page": page, "sec": r["sec"],
                        "items": len(items), "with_answer": with_ans,
                        "with_options": with_opt, "usage": r["usage"],
                        "finish": r["finish"],
                        "types": _type_count(items),
                        "sample": [{"stem": it["stem"][:40], "answer": it.get("answer"),
                                    "opts": len(it.get("options") or [])} for it in items[:3]]})
            print(f"[提取] {m} 第{page}页  {r['sec']}s  题数={len(items)} 有答案={with_ans} "
                  f"有选项={with_opt}  {r['usage']}", flush=True)
        except Exception as exc:
            out.append({"model": m, "ok": False, "page": page,
                        "error": f"{type(exc).__name__}: {exc}"})
            print(f"[提取 失败] {m} 第{page}页  {type(exc).__name__}: {exc}", flush=True)
    return out


def _type_count(items):
    d = {}
    for it in items:
        k = it.get("qtype") or it.get("type") or "?"
        d[k] = d.get(k, 0) + 1
    return d


def local(pages_):
    cfg = ai_parse.load_config()
    cat = None
    out = []
    for p in pages_:
        pg = pages()[p - 1]
        t0 = time.time()
        try:
            txt = ocr_win.ocr_png_bytes(pg["png"])
            dt_ocr = round(time.time() - t0, 1)
            res = importer.parse_text(txt, default_type=None)
            dt = round(time.time() - t0, 1)
            items = res.get("items") or []
            with_ans = sum(1 for it in items if (it.get("answer") or "").strip())
            out.append({"mode": "local-ocr", "page": p, "sec": dt, "ocr_sec": dt_ocr,
                        "chars": len(txt), "items": len(items), "with_answer": with_ans,
                        "types": _type_count(items),
                        "sample": [{"stem": it["stem"][:40], "answer": it.get("answer"),
                                    "opts": len(it.get("options") or [])} for it in items[:3]]})
            print(f"[本地] 第{p}页 {dt}s（OCR {dt_ocr}s）{len(txt)}字 题数={len(items)} "
                  f"有答案={with_ans} 类型={_type_count(items)}", flush=True)
        except Exception as exc:
            out.append({"mode": "local-ocr", "page": p, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"})
            print(f"[本地] 第{p}页 失败 {type(exc).__name__}: {exc}", flush=True)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    mode = sys.argv[1]
    page, models = 1, []
    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] in ("--page", "--pages"):
            vals = []
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                vals.append(int(args[i])); i += 1
            page = vals if len(vals) > 1 else vals[0]
        elif args[i] == "--models":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                models.extend(args[i].split(",")); i += 1
        else:
            i += 1

    if mode == "local":
        res = local(page if isinstance(page, list) else [page])
    elif mode == "probe":
        res = probe(page if isinstance(page, int) else page[0], models)
    else:
        res = extract(page if isinstance(page, int) else page[0], models)

    old = []
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            old = []
    old.extend(res if isinstance(res, list) else [res])
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(old, fh, ensure_ascii=False, indent=1)
    print("\n已追加到", OUT)


if __name__ == "__main__":
    main()
