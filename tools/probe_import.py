#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收摸底：把一堆存量题库文件先"试读"一遍，出一份报告，但不写库。

为什么要有这个：单测里的 doc/xls/PDF 样本都是自己构造的，只能证明"格式解析没写错"，
证明不了"你手上这份文件能读出来"。把文件丢进 待验收样例 目录，双击 验收摸底.bat，
就能在动正式题库之前看清每份文件的结果：能拆几题、几题缺答案、要不要走 AI。

只读，不改数据库；报告写到 待验收样例/识别报告.html。
"""

import html
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
APP = os.path.join(BASE, "机房运维试卷系统")
sys.path.insert(0, APP)

import importer  # noqa: E402
import pdftext  # noqa: E402

SUPPORTED = ("xlsx", "xls", "csv", "docx", "doc", "txt", "rtf", "htm", "html",
             "pdf", "png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "heic")
#: 目录里自带的说明文件与本工具自己生成的报告，不要当成题库去试读
SKIP = ("识别报告.html", "先把文件放这里.txt", "说明.txt", "readme.txt")
DB = os.path.join(APP, "data", "library.db")


def load_cats():
    """只读地拿一份分类树，让报告里的分类名跟系统一致；拿不到就按无分类试读。"""
    if not os.path.exists(DB):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        conn.row_factory = sqlite3.Row
        cats = []
        for c in conn.execute("SELECT id,name FROM category ORDER BY sort,id"):
            subs = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
                "SELECT id,name FROM subcategory WHERE cat_id=? ORDER BY sort,id", (c["id"],))]
            cats.append({"id": c["id"], "name": c["name"], "subs": subs})
        conn.close()
        return cats or None
    except Exception:
        return None


def probe_one(path, cats):
    name = os.path.basename(path)
    t0 = time.time()
    rec = {"name": name, "size": os.path.getsize(path), "n": 0, "noans": 0,
           "noopt": 0, "warns": [], "by": "", "err": "", "samples": [], "sec": 0.0}
    try:
        with open(path, "rb") as f:
            raw = f.read()
        res = importer.parse(raw, name, cats=cats)
        items = res["items"]
        rec["n"] = len(items)
        rec["by"] = res.get("by", "")
        rec["warns"] = res.get("warnings") or []
        for it in items:
            if not (it.get("answer") or "").strip():
                rec["noans"] += 1
            if it.get("qtype") in ("single", "multi") and len(it.get("options") or []) < 2:
                rec["noopt"] += 1
        rec["samples"] = [(it.get("stem") or "")[:46] for it in items[:2]]
    except importer.ImportError_ as e:
        rec["err"] = str(e)
    except Exception as e:                      # 兜底：不让一个坏文件把整轮摸底打断
        rec["err"] = "%s: %s" % (type(e).__name__, e)
    # PDF 额外过一遍乱码检测：老排版软件导出的字体没带 ToUnicode 映射，
    # 抽出来是乱码——不仅读不出题，还会从乱码里"捡"出几道垃圾题，必须拦下。
    if name.lower().endswith(".pdf") and not rec["err"]:
        try:
            info = pdftext.read_pdf(raw)
            msg = pdf_garbled_msg(info.get("text", ""), info.get("scanned", False))
            if msg:
                rec["err"] = msg
                rec["n"] = 0
                rec["noans"] = rec["noopt"] = 0
                rec["warns"] = []
                rec["samples"] = []
        except Exception:
            pass
    rec["sec"] = time.time() - t0
    return rec


def pdf_garbled_msg(text, scanned=False):
    """文字层几乎不含汉字 = 乱码。返回给用户看的原因，正常返回空串。"""
    if scanned or not text:
        return ""
    printable = [ch for ch in text if ch.isprintable() and not ch.isspace()]
    if len(printable) < 50:
        return ""
    cjk = sum(1 for ch in printable if "\u4e00" <= ch <= "\u9fff")
    if cjk / len(printable) >= 0.30:
        return ""
    return ("这份 PDF 有文字层，但抽出来几乎不含汉字（%d 个可见字符里只有 %d 个汉字）——"
            "多半是老排版软件导出的、字体没带 ToUnicode 映射，本地规则读不了。"
            "请找原始 Word 文件，或把每页导成图片后走 AI 视觉识别。"
            % (len(printable), cjk))


def human_size(n):
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def verdict(rec):
    if rec["err"]:
        msg = rec["err"]
        if any(k in msg for k in ("图片", "扫描", "AI", "视觉")):
            # 乱码 PDF、图片型 PDF 的具体原因直接透给用户，别只说"要开 AI"
            if "ToUnicode" in msg or "视觉" in msg or "页面图" in msg:
                return ("要开 AI", msg)
            return ("要开 AI", "这样是图片，本地规则读不了；到设置里配好 AI 再导")
        return ("读不出", msg)
    if rec["noans"] == rec["n"]:
        return ("能读，但整份没答案", "可先入成草稿再补答案（使用说明 4.11）")
    if rec["noans"]:
        return ("能读，%d 题缺答案" % rec["noans"], "可先入库，再逐题补")
    if rec["noopt"]:
        return ("能读，%d 题选项没拆开" % rec["noopt"], "导入页里手工补选项")
    return ("可直接导入", "识别完整，无缺项")


COLOR = {"可直接导入": "#1a7f37", "能读，但整份没答案": "#9a6700",
         "要开 AI": "#0b5cad", "读不出": "#b42318"}


def color_of(v):
    if v.startswith("能读，") and "缺答案" in v:
        return "#9a6700"
    if v.startswith("能读") and "选项" in v:
        return "#9a6700"
    return COLOR.get(v, "#b42318")


CSS = """
body{font-family:"Microsoft YaHei",system-ui,sans-serif;margin:28px;color:#1f2328;background:#fff}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
.kpi{display:flex;gap:14px;margin-bottom:18px}
.kpi div{background:#f6f7f9;border:1px solid #e5e7eb;border-radius:8px;padding:10px 16px}
.kpi b{display:block;font-size:20px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #e8eaed;padding:8px 10px;text-align:left;vertical-align:top}
th{background:#f6f7f9;font-weight:600;font-size:12.5px;color:#4b5563}
td.f{max-width:230px;word-break:break-all}
td.n{text-align:center;width:64px;font-variant-numeric:tabular-nums}
td.s{color:#4b5563;max-width:330px}
.meta{color:#8b949e;font-size:11.5px;font-weight:400;margin-top:2px}
.note{margin-top:20px;padding:12px 14px;background:#f6f7f9;border-left:3px solid #d0d7de;
      font-size:12.5px;color:#4b5563;line-height:1.7}
code{background:#f0f1f3;padding:1px 4px;border-radius:3px}
"""

HEAD = '<!doctype html><meta charset="utf-8"><title>题库识别报告</title><style>%s</style>'

BODY = """<h1>题库识别报告</h1>
<div class="sub">目录：%s · 共 %d 份文件 · 能读出 %d 份 · 合计 %d 道题</div>
<div class="kpi">
  <div><b>%d</b>份文件</div><div><b>%d</b>道题已识别</div>
  <div><b>%d</b>题缺答案</div><div><b>%d</b>份需要开 AI</div>
</div>
<table><thead><tr><th>文件</th><th>结论</th><th>题数</th><th>缺答案</th><th>提示</th>
<th>前两题题干（看一眼像不像）</th></tr></thead>
<tbody>%s</tbody></table>
<div class="note">
这只是一次<strong>试读</strong>，没有写进题库——数据库一个字都没动。<br>
可以直接用的，到「批量导入」里正常导一次即可；结论写「要开 AI」的，先把图片/扫描件拖进导入页
（设置里要填好密钥与视觉模型）；<br>
「读不出」的把那句原因发出来就行，多半是版式特殊，加一条规则就能认。<br>
报告文件：<code>%s</code>
</div>"""


def render(recs, out_path, folder):
    total = len(recs)
    ok = [r for r in recs if not r["err"]]
    qs = sum(r["n"] for r in ok)
    rows = []
    for r in recs:
        v, tip = verdict(r)
        c = color_of(v)
        samples = "<br>".join(html.escape(s) for s in r["samples"]) or "—"
        warns = len(r["warns"])
        rows.append(
            "<tr><td class='f'>%s<div class='meta'>%s · %.2fs</div></td>"
            "<td style='color:%s;font-weight:600'>%s<div class='meta'>%s</div></td>"
            "<td class='n'>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
            "<td class='s'>%s</td></tr>" % (
                html.escape(r["name"]), human_size(r["size"]), r["sec"], c,
                html.escape(v), html.escape(tip),
                r["n"] or "—", r["noans"] if not r["err"] else "—",
                warns if not r["err"] else "—", samples))
    body = BODY % (html.escape(folder), total, len(ok), qs, total, qs,
                   sum(r["noans"] for r in ok),
                   sum(1 for r in recs if r["err"] and "要开 AI" in verdict(r)[0]),
                   "".join(rows), html.escape(out_path))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write((HEAD % CSS) + body)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_open = "--open" in sys.argv[1:]
    folder = args[0] if args else os.path.join(BASE, "待验收样例")
    if not os.path.isdir(folder):
        print("找不到目录：%s" % folder)
        print("建一个文件夹把题库文件放进去，再把它拖到 验收摸底.bat 上。")
        return 1
    cats = load_cats()
    files = sorted(f for f in os.listdir(folder)
                   if os.path.isfile(os.path.join(folder, f))
                   and f.rsplit(".", 1)[-1].lower() in SUPPORTED
                   and f not in SKIP and not f.startswith("~$"))
    if not files:
        print("这个目录里没有可识别的题库文件：%s" % folder)
        print("把 .doc/.xls/.docx/.xlsx/.pdf/.txt 或图片丢进去再跑一次。")
        return 2

    recs = []
    print("=" * 78)
    print("试读目录：%s（%d 份文件）" % (folder, len(files)))
    print("=" * 78)
    for f in files:
        r = probe_one(os.path.join(folder, f), cats)
        recs.append(r)
        v, tip = verdict(r)
        print("%-28s %-22s %s" % (r["name"][:28], v,
                                  ("%d 题" % r["n"]) if not r["err"] else tip[:40]))
    out = os.path.join(folder, "识别报告.html")
    render(recs, out, folder)
    ok = [r for r in recs if not r["err"]]
    print("-" * 78)
    print("能读出 %d/%d 份，合计 %d 题，其中缺答案 %d 题" % (
        len(ok), len(recs), sum(r["n"] for r in ok), sum(r["noans"] for r in ok)))
    print("报告：%s" % out)
    if want_open:
        try:
            import webbrowser
            webbrowser.open("file:///" + os.path.abspath(out).replace("\\", "/"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
