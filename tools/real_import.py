#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实入库：把待验收样例里的文件按软件自身链路解析后写进正式题库。

只做两件事：解析（importer / ai_parse）、入库（server.save_question），
判重与界面一致（dupe 模块，阈值 0.90）。**不动既有题目**，只追加。

用法：
    python tools/real_import.py 文件1 文件2 ...        # 默认 auto 档
    python tools/real_import.py --engine local 文件... # 不发网络请求
"""

import base64
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "机房运维试卷系统")
sys.path.insert(0, APP)
os.chdir(APP)

import server  # noqa: E402
import dupe  # noqa: E402
import importer  # noqa: E402


def pool():
    conn = server.connect()
    try:
        return [(r["id"], r["stem"]) for r in conn.execute("SELECT id, stem FROM question")]
    finally:
        conn.close()


CATS = server.categories_tree()


def fill_cat(it):
    """按界面 impNormalize 的做法补齐分类：没猜出来就用第一分类、二级取该分类第一个。

    不补就直接提交会被校验拦下（"未选择一级分类/二级分类"）——界面上看着有下拉框
    默认值，实际提交的是空值，这正是 P22 记录过的那个坑。
    """
    cat_id = it.get("cat_id")
    if not any(c["id"] == cat_id for c in CATS):
        cat_id = CATS[0]["id"] if CATS else None
    subs = next((c.get("subs") or [] for c in CATS if c["id"] == cat_id), [])
    sub_id = it.get("sub_id")
    if not any(x["id"] == sub_id for x in subs):
        sub_id = subs[0]["id"] if subs else None
    return cat_id, sub_id


def commit(items, pool_rows):
    """按界面「开始导入」的规则入库：题干重复的跳过，其余照常写（缺答案=草稿）。"""
    added, dup, bad = 0, 0, []
    kept = []
    for it in items:
        if dupe.find(it.get("stem") or "", pool_rows):
            dup += 1
            continue
        cat_id, sub_id = fill_cat(it)
        res = server.save_question({
            "cat_id": cat_id, "sub_id": sub_id,
            "qtype": it.get("qtype"), "diff": it.get("diff") or "mid",
            "stem": it.get("stem"), "options": it.get("options") or [],
            "answer": it.get("answer") or "", "analysis": it.get("analysis") or "",
            "tags": it.get("tags") or "",
        })
        if res.get("ok"):
            added += 1
            kept.append(it)
            # 新题要参与后续判重，否则同一份文件里的重复题会全写进去
            pool_rows.append((res.get("id"), it.get("stem") or ""))
        else:
            bad.append({"stem": (it.get("stem") or "")[:24], "errors": res.get("errors")})
    return added, dup, bad, kept


def main(argv):
    engine = "auto"
    if "--engine" in argv:
        i = argv.index("--engine")
        engine = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    files = argv
    if not files:
        print("用法: python tools/real_import.py [--engine auto|ai|local] 文件...")
        return 2
    # 上面 chdir 到了程序目录，相对路径要按项目根解析
    files = [p if os.path.isabs(p) else os.path.join(ROOT, p) for p in files]

    rows = pool()
    print("入库前题库：%d 题" % len(rows))
    report = []
    for path in files:
        name = os.path.basename(path)
        raw = open(path, "rb").read()
        t0 = time.time()
        rec = {"file": name, "size": len(raw), "engine": engine}
        try:
            res = server.import_parse({"filename": name, "engine": engine,
                                       "content": base64.b64encode(raw).decode("ascii")})
        except importer.ImportError_ as exc:
            rec.update(ok=False, error=str(exc)[:160])
            report.append(rec)
            print("[读不出] %-34s %s" % (name, str(exc)[:80]))
            continue
        except Exception as exc:  # noqa: BLE001 真实文件五花八门，异常也要落报告
            rec.update(ok=False, error="%s: %s" % (type(exc).__name__, str(exc)[:120]))
            report.append(rec)
            print("[异常  ] %-34s %s" % (name, rec["error"][:80]))
            continue

        items = res["items"]
        added, dup, bad, kept = commit(items, rows)
        drafts = sum(1 for it in kept if not (it.get("answer") or "").strip())
        by_type = {}
        for it in kept:
            by_type[it.get("qtype")] = by_type.get(it.get("qtype"), 0) + 1
        rec.update(ok=True, parsed=len(items), added=added, dup=dup,
                   drafts=drafts, bad=bad[:5], by=res.get("by"), by_type=by_type,
                   seconds=round(time.time() - t0, 1))
        report.append(rec)
        print("[%s] %-34s 解析%3d 入库%3d 重复%3d 草稿%3d %.1fs (%s)"
              % ("AI  " if res.get("by", "").startswith("ai") else "本地",
                 name, len(items), added, dup, drafts, rec["seconds"], res.get("by")))

    out = os.path.join(ROOT, "output", "import_real_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"engine": engine, "before": len(rows), "files": report},
                  fh, ensure_ascii=False, indent=2)
    total_added = sum(r.get("added", 0) for r in report)
    print("\n合计新增 %d 题；入库后题库 %d 题" % (total_added, len(pool())))
    print("报告：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
