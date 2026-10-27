# -*- coding: utf-8 -*-
"""用软件自身的 import_parse 链路试跑一份 PDF（不走代理手工能力）。

用法：python tools/try_one_pdf.py <pdf路径> [engine]
engine 缺省 auto，与界面默认一致。
"""
import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "机房运维试卷系统")
sys.path.insert(0, APP)
os.chdir(APP)

import server  # noqa: E402


def main():
    path = sys.argv[1]
    engine = sys.argv[2] if len(sys.argv) > 2 else "auto"
    with open(path, "rb") as fh:
        raw = fh.read()
    payload = {
        "content": base64.b64encode(raw).decode("ascii"),
        "filename": os.path.basename(path),
        "engine": engine,
    }
    res = server.import_parse(payload)
    items = res.get("items") or []
    out = {
        "by": res.get("by"),
        "chars": res.get("chars"),
        "count": len(items),
        "qtypes": {},
        "sample": [],
    }
    for it in items:
        out["qtypes"][it.get("qtype")] = out["qtypes"].get(it.get("qtype"), 0) + 1
    for it in items[:3]:
        out["sample"].append({
            "qtype": it.get("qtype"),
            "stem": (it.get("stem") or "")[:80],
            "options": it.get("options"),
            "answer": (it.get("answer") or "")[:40],
        })
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
