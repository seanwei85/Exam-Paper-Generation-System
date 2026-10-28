#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""题库文件解析：txt / csv / docx / doc / xlsx / xls / pdf 自动拆题。

只依赖标准库。三类"看着像读不出来"的情况分别对待：
  * PDF 有文字层的（Word/WPS 导出、电子定稿）—— `pdftext` 抽出带换行的文字再拆题；
  * PDF 扫描件/拍照转的（没有文字层）—— 明确说"这是图片版"并指向 AI 视觉识别，
    不能笼统报"无法提取文字"，否则用户只能反复换文件格式试；
  * .doc / .xls 旧格式 —— `legacy` 按复合文档结构取正文（Word 走片段表、Excel 走
    BIFF8 记录），并按内容判断真实格式：现实里 .doc 常是 RTF 或 HTML，.xls 常是
    HTML 表格，只看扩展名会把这些判成"损坏"。
"""

import csv
import io
import os
import re
import zipfile

import legacy
import pdftext

MAX_FILE = 20 * 1024 * 1024      # 单个文件 20MB
MAX_UNZIP = 60 * 1024 * 1024     # 解压总量 60MB，防压缩炸弹

TYPES = ["single", "multi", "judge", "fill", "qa"]
TYPE_NAMES = {
    "single": "单项选择题", "multi": "多项选择题", "judge": "判断题",
    "fill": "填空题", "qa": "问答题",
}
DIFFS = ["easy", "mid", "hard"]


class ImportError_(Exception):
    pass


class ScannedPdfError(ImportError_):
    """PDF 是图片版（扫描件/拍照转存）。

    单独一个类型，是因为这种"读不出"和"格式不支持"的下一步完全不同：
    前者要改走视觉识别或导出图片，后者要换文件格式。调用方要能分开处理，
    不能靠匹配错误文案（文案一改逻辑就静默失效）。
    """


# ---------------------------------------------------------------- 文本抽取

def _decode(raw):
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "ignore")


def _zip_guard(zf):
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNZIP:
        raise ImportError_("文件解压后过大（%.1f MB），请拆分后再导入" % (total / 1048576.0))


def _docx_text(raw):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        raise ImportError_("不是有效的 .docx 文件")
    with zf:
        _zip_guard(zf)
        try:
            xml = zf.read("word/document.xml").decode("utf-8", "ignore")
        except KeyError:
            raise ImportError_("docx 缺少正文，可能不是 Word 文档")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    lines = []
    for p in paras:
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)
        seg = "".join(texts)
        seg = (seg.replace("&amp;", "&").replace("&lt;", "<")
                  .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
        if "<w:br" in p and not seg:
            lines.append("")
        lines.append(seg)
    return "\n".join(lines)


def _col_index(ref):
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _shared_strings(zf):
    """读取共享字符串表；没有该部件时返回空表。"""
    try:
        sx = zf.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
    except KeyError:
        return []
    out = []
    for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
        out.append(_unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
    return out


CELL_RE = re.compile(r"<c([^>]*?)(?:/>|>(.*?)</c>)", re.S)


def _cell_value(attrs, body, shared):
    """取单元格文本：优先内联字符串，其次共享字符串，最后原始数值。"""
    m = re.search(r"<is>.*?<t[^>]*>(.*?)</t>", body or "", re.S)
    if m:
        return _unescape(m.group(1)).strip()
    m = re.search(r"<v>(.*?)</v>", body or "", re.S)
    if not m:
        return ""
    val = _unescape(m.group(1)).strip()
    if 't="s"' in attrs:
        try:
            return shared[int(val)]
        except Exception:
            return ""
    return val


def _xlsx_rows(raw):
    """返回 [[单元格,...], ...]，**保留列位置**（空单元格以空串占位）。

    按表头导入时必须知道每个值在第几列，所以不能像纯文本路径那样丢掉空单元格。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except Exception:
        raise ImportError_("不是有效的 .xlsx 文件")
    with zf:
        _zip_guard(zf)
        shared = _shared_strings(zf)
        names = [n for n in zf.namelist()
                 if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]
        if not names:
            raise ImportError_("xlsx 中没有工作表")
        names.sort()
        rows = []
        for name in names[:3]:
            sx = zf.read(name).decode("utf-8", "ignore")
            for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                cells = {}
                for m in CELL_RE.finditer(row):
                    attrs, body = m.group(1), m.group(2) or ""
                    ref = re.search(r'r="([A-Z]+\d+)"', attrs)
                    idx = _col_index(ref.group(1)) if ref else len(cells)
                    cells[idx] = _cell_value(attrs, body, shared)
                width = max(cells) + 1 if cells else 0
                rows.append([cells.get(i, "") for i in range(width)])
        return rows


def _xlsx_text(raw):
    """表格转纯文本：一行一题、单元格用换行连接，交给通用拆题逻辑。"""
    blocks = []
    for r in _xlsx_rows(raw):
        cells = [(c or "").strip() for c in r if (c or "").strip()]
        if cells:
            blocks.append("\n".join(cells))
    return "\n\n".join(blocks)


def _unescape(s):
    return (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'"))


def _csv_rows(text):
    try:
        return [[(c or "").strip() for c in r] for r in csv.reader(io.StringIO(text))]
    except Exception:
        return []


def _csv_text(text):
    """表格型 CSV 转成「一行一题、单元格换行」，纯文本 CSV 原样返回。"""
    rows = _csv_rows(text)
    if not rows:
        return text
    widths = [len([c for c in r if (c or "").strip()]) for r in rows]
    multi = sum(1 for w in widths if w >= 2)
    if multi < len(rows) * 0.6:
        return text
    blocks = []
    for r in rows:
        cells = [(c or "").strip() for c in r if (c or "").strip()]
        if not cells:
            continue
        blocks.append("\n".join(cells))
    return "\n\n".join(blocks)


def _legacy(fn, raw):
    """把 legacy 的异常翻成导入层的异常，保证界面拿到的是一句人话。"""
    try:
        return fn(raw)
    except legacy.LegacyError as exc:
        raise ImportError_(str(exc))
    except Exception as exc:                      # 二进制格式，什么都可能遇到
        raise ImportError_("读取旧版 Office 文件时出错：%s" % exc)


def read_file(raw, filename):
    """返回纯文本。filename 用于判断格式。"""
    if len(raw) > MAX_FILE:
        raise ImportError_("文件超过 20MB，请拆分后再导入")
    ext = (filename or "").lower().rsplit(".", 1)[-1]
    if ext in ("txt", "md", "text", ""):
        return _decode(raw)
    if ext == "csv":
        return _csv_text(_decode(raw))
    if ext == "docx":
        # 扩展名可能骗人：有些系统导出的"docx"其实是 zip，有些 .docx 改成了
        # 旧版复合文档。内容说了算，否则用户只会在"文件损坏"上反复打转。
        if legacy.sniff(raw) == "ole":
            return _legacy(legacy.doc_text, raw)
        return _docx_text(raw)
    if ext == "doc":
        return _legacy(legacy.doc_text, raw)
    if ext == "xlsx":
        if legacy.sniff(raw) == "ole":
            return _legacy(legacy.xls_text, raw)
        return _xlsx_text(raw)
    if ext == "xls":
        return _legacy(legacy.xls_text, raw)
    if ext == "pdf":
        return _pdf_text(raw)
    raise ImportError_("不支持的文件类型 .%s，可用 doc / xls / docx / xlsx / pdf / txt / csv" % ext)


def _pdf_text(raw):
    """PDF 抽文字。抽不出文字的扫描件要单独说明，别混进"格式不支持"。"""
    try:
        info = pdftext.read_pdf(raw)
    except pdftext.PdfError as exc:
        raise ImportError_(str(exc))
    if info["scanned"]:
        pages = 0
        try:
            pages = pdftext.page_image_count(raw)
        except pdftext.PdfError:
            pass
        if pages:
            raise ScannedPdfError(
                "这份 PDF 是图片版（每页一张图，没有文字层），共抽出 %d 页页面图。"
                "识别它需要一个能看图的模型：到「系统设置 · AI 识别」填「视觉模型」，"
                "再把识别方式选成 AI。也可以把每页另存为 PNG 后直接上传。" % pages)
        raise ScannedPdfError(
            "这份 PDF 是图片版（扫描件或拍照转存），没有文字层，也没抽出整页图片。"
            "请把每页导出成图片（PNG/JPG）后在导入页上传、识别方式选 AI；"
            "或用带文字层的原始文件。")
    return info["text"]


# ---------------------------------------------------------------- 拆题规则

SECTION_RE = re.compile(r"^\s*(?:第\s*)?([一二三四五六七八九十]+)\s*[、\.．]\s*(.{0,40})$")
#: 「标准号: 《规范名》YDxxxx-2005 ◆题干」——某些单位的标准条文测试卷一条一行，
#: 没有题号。规范名有《…》和 <<…>> 两种写法；行首可能有粘连的页码（如「2 标准号:」）。
STD_RE = re.compile(
    r"^\s*\d{0,3}\s*标准号\s*[:：]\s*(?:《([^》]+)》|<<([^>]+)>>)\s*([^◆]*?)◆\s*(.*)$")
NUM_RE = re.compile(r"^\s*(\d{1,3})\s*[\.、．\)）]\s*(.*)$")
#: 选项行。行首可能带「答题解析」导出的圆点标记：● 是正确答案、○ 是错项（见 P24）。
#: 分隔符含逗号（P32）：答题解析导出有「●A, 电池失效;」这种写法。
OPT_RE = re.compile(r"^\s*([○●◯〇◦]?)\s*[（\(【\[]?\s*([A-Ha-h])\s*[）\)】\]\.、．:：,，]\s*(.+)$")
#: 无字母的圆点选项（P32）：「○12」「●36」——答题人答卷导出时选项连字母都没带，
#: 只剩圆点标记。● 仍是正确答案。字母按出现顺序自动编 A/B/C…。
CIRC_OPT_RE = re.compile(r"^\s*([○●◯〇◦])\s*(\S.*)$")
#: 一行里挤着多条选项（`A. 5500*1400   B、5500*1600  C、6000*1600`）。只在空白之后、
#: 且字母紧接序号时才算——句子中间的「A、B、C 三类负荷」不许切。
INLINE_OPT_RE = re.compile(r"(?:^|[\s　┋│|\t]+)[（(]?([A-H])[）\)】\]\.、．:：]\s*")
#: 「回答正确（+1分）」「回答错误（+0分）」——平台的评分反馈，不是选项内容。
FEEDBACK_RE = re.compile(r"^\s*回答(?:正确|错误|不完全)\s*(?:[（(][^)）]*[)）])?\s*$")
#: 题干行尾挤着答案（`……副边三相电压为    。答案：B`）。只认字母/判断标记，
#: 「答案：塑壳开关」这种填空答案不在题干里摘。
STEM_ANS_RE = re.compile(r"答案\s*[:：]\s*([A-Ha-h]{1,6}|√|×|正确|错误)\s*$")
#: 题干的「分值1分」——平台导出时附在题干尾巴上。
FEN_RE = re.compile(r"\s*分值\s*\d+(?:\.\d+)?\s*分?")
#: 实心圆点 = 正确答案（答题解析导出）。
CORRECT_MARK = "●"
ANS_RE = re.compile(r"^\s*(?:【答案】|答案(?!解析)|正确答案|参考答案|答(?!案)|"
                    r"answer|answers|ans|key)\s*[:：]?\s*(.*)$", re.I)
ANA_RE = re.compile(r"^\s*(?:【解析】|解析|试题解析|答案解析|"
                    r"analysis|explanation|note)\s*[:：]?\s*(.*)$", re.I)
#: 题干括号里直接写了答案的写法，如「……密码（   C  ）」「蓄冷池（  B  ）m³」「（ABD）」。
#: 括号内必须是纯选项字母或判断标记，避免把「（2018 版）」「（A区）」这类内容误吞。
#: 位置不限——厂商模板的空常挖在句中。多于一处时不好判断哪个是答案，宁可不猜。
INLINE_ANS_RE = re.compile(
    r"([（(])\s*((?:[A-Ha-h][、,，]?\s*){1,6}|√|×|[TFYN]|正确|错误|[对错]{1,2})\s*([）)])")
#: 独占一行的题号（厂商模板把题号单独放一个单元格）。要接续序号才认，
#: 否则正文中间孤立的页码「3」会把一道好题拦腰截断。
SEQ_RE = re.compile(r"^\s*(\d{1,3})\s*$")
DIFF_RE = re.compile(r"^[【\[（(]\s*(易|简单|中|中等|较难|难|困难)\s*[】\]）)]\s*")
#: 难度写法。真实题库里「低 / 中 / 高」比「易 / 中 / 难」更常见（设备厂商的题库
#: 模板就是这么定的），只认前者会把整批题都按"中等"入库，组卷时难度配比全废。
DIFF_MAP = {"易": "easy", "简单": "easy", "低": "easy", "容易": "easy",
            "中": "mid", "中等": "mid", "一般": "mid",
            "较难": "hard", "难": "hard", "困难": "hard", "高": "hard"}

#: 页眉页脚：页码、「共 N 页」、「第 N 页 共 M 页」、「Page N of M」、「- 3 -」。
#: 「页」要同时认兼容字「⻚」（U+2EDA，CJK RADICAL C-SIMPLIFIED LEAF）——有些字体的
#: 字形表把正常汉字映射到部首区，打印出来肉眼看不出差别，正则只写「页」就会漏。
_PAGE_CHAR = "页\u2eda"
FURNITURE_RE = re.compile(
    r"^\s*(?:第\s*\d+\s*[" + _PAGE_CHAR + r"]\s*(?:[,，/]?\s*共\s*\d+\s*[" + _PAGE_CHAR + r"])?"
    r"|共\s*\d+\s*[" + _PAGE_CHAR + r"]"
    r"|page\s*\d+\s*(?:of|/)\s*\d+"
    r"|[-—]\s*\d{1,3}\s*[-—]"
    r"|姓名\s*[:：]\s*[▁＿_]*\s*(?:部门\s*[:：]\s*[▁＿_]*)?\s*)\s*$", re.I)


def strip_furniture(text):
    """删掉整行都是页码/页脚的行。

    必须在拆题之前做：页脚常常正好卡在一道题的选项中间（题目跨页时），
    留着它就会被"普通行追加到最后一个选项"的逻辑拼进选项文本里，
    变成「按蒸发量的 3% 第 2 页 共 3 页」这种脏数据。
    """
    out = []
    for line in (text or "").split("\n"):
        if FURNITURE_RE.match(line):
            continue
        out.append(line)
    return "\n".join(out)


def _type_from_text(text):
    t = text.replace(" ", "")
    if "多项选择" in t or "多选" in t or "不定项" in t:
        return "multi"
    if "单项选择" in t or "单选" in t:
        return "single"
    if "判断" in t:
        return "judge"
    if "填空" in t:
        return "fill"
    if "问答" in t or "简答" in t or "论述" in t or "计算" in t or "案例" in t:
        return "qa"
    # 光写「选择题」不说是单选还是多选：先按单选，后面答案里出现多个字母时
    # _guess_type 会自己改成多选，不会把多选题判错。
    if "选择" in t:
        return "single"
    return None


def _clean_answer(qtype, raw):
    raw = (raw or "").strip()
    if qtype == "single":
        m = re.search(r"[A-Ha-h]", raw)
        return m.group(0).upper() if m else ""
    if qtype == "multi":
        letters = sorted(set(re.findall(r"[A-Ha-h]", raw)))
        return "".join(letters)
    if qtype == "judge":
        t = raw.replace(" ", "")
        if t in ("正确", "对", "√", "是", "T", "True", "true"):
            return "正确"
        if t in ("错误", "错", "×", "否", "F", "False", "false", "不正确"):
            return "错误"
        if "正确" in t or "对" in t:
            return "正确"
        if "错误" in t or "错" in t:
            return "错误"
        return raw
    if qtype == "fill":
        parts = [p.strip() for p in re.split(r"[|｜；;]", raw) if p.strip()]
        return "|".join(parts) if parts else raw
    return raw


def _guess_type(stem, options, answer):
    if options:
        if re.search(r"[A-Ha-h]", answer or "") and len(set(re.findall(r"[A-Ha-h]", answer or ""))) > 1:
            return "multi"
        return "single"
    a = (answer or "").strip()
    if a in ("正确", "错误", "对", "错", "√", "×"):
        return "judge"
    # ▁（U+2581）是标准条文卷里挖空的写法，与下划线等价
    if len(re.findall(r"[▁_]{2,}", stem or "")) >= 1:
        return "fill"
    # 表单类卷子用一长串空格当横线（`含义是            。`）。只在下划线缺席时兜底，
    # 且答案不能太长——长答案更像问答题。
    if len(a) <= 40 and re.search(r"(?:[ ]{4,}|[\u3000]{2,})", stem or ""):
        return "fill"
    return "qa"


def _split_tail_options(text, prev_key):
    """把「5500*1400   B、5500*1600  C、6000*1600」拆成 [(B,…), (C,…)]。

    只认**紧接** prev_key 的字母（A→B→C…），中间断号就停：正文里偶尔会冒出
    「（B、C 两类）」这种字样，按顺序对齐才不会被它带偏。
    返回 (第一段正文, 后续选项列表)。
    """
    marks = []
    expect = chr(ord(prev_key) + 1)
    for m in INLINE_OPT_RE.finditer(text):
        if expect > "H":
            break
        if m.start() == 0 or m.group(1) != expect:
            continue
        marks.append((expect, m.start(), m.end()))
        expect = chr(ord(expect) + 1)
    if not marks:
        return text.strip(), []
    head = text[:marks[0][1]].strip()
    tails = []
    for i, (key, _start, end) in enumerate(marks):
        stop = marks[i + 1][1] if i + 1 < len(marks) else len(text)
        tails.append((key, text[end:stop].strip()))
    return head, tails


def split_questions(text, default_type=None, default_diff="mid", cats=None):
    """把文本拆成题目列表。

    cats: [{"id":1,"name":"电气",...}] 用于识别分类；可为 None。
    """
    lines = [l.rstrip() for l in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    items = []
    section_type = default_type
    cur = None
    pending_answer = None
    last_seq = 0  # 最近一次开始的题号（两种题号写法共用一个计数）
    want_answer = False  # 「正确答案:」后面那行才是答案值

    def flush():
        nonlocal cur, want_answer
        want_answer = False
        if not cur:
            return
        stem = "\n".join([s for s in cur["stem_lines"] if s.strip()]).strip()
        stem = FEN_RE.sub("", stem).strip()
        opts = [{"key": o["key"], "text": o["text"].strip()}
                for o in cur["options"] if o["text"].strip()]
        if not stem and not opts:
            cur = None
            return
        # 题干末尾括号里写了答案（厂商模板版式）：只在没有任何答案行时提取，
        # 提取后把括号挖空——答案进字段，卷面上还是留空的括号。
        # 必须在推断题型之前做，否则 √/ABD 这类答案参与不了题型判断。
        # 答题解析导出：正确答案就是那个实心圆点。这比任何猜测都可靠，
        # 所以放在最前面——有它就不用再看题干括号里的残字。
        if not cur["answer"]:
            marked = sorted({o["key"] for o in cur["options"] if o.get("correct")})
            if marked:
                cur["answer"] = "".join(marked)
        # 答案挤在题干行尾（`……副边三相电压为    。答案：B`）
        if not cur["answer"]:
            m = STEM_ANS_RE.search(stem)
            if m:
                cur["answer"] = m.group(1)
                stem = stem[:m.start()].rstrip(" 　。．.，,；;")
        if not cur["answer"]:
            hits = list(INLINE_ANS_RE.finditer(stem))
            if len(hits) == 1:
                m = hits[0]
                cur["answer"] = m.group(2)
                blank = "（　　）" if m.group(1) == "（" else "(    )"
                stem = stem[:m.start()] + blank + stem[m.end():]
        qtype = cur["qtype"] or section_type or _guess_type(stem, opts, cur["answer"])
        # 段落标题说「问答题」、题目却带着选项——以数据为准（P32，真实文件里
        # 这批圆点选项题就是这么被标题按成 qa、选项全挤进题干的）。
        if len(opts) >= 2 and qtype not in ("single", "multi"):
            qtype = "multi" if len((cur["answer"] or "").strip()) > 1 else "single"
        answer = _clean_answer(qtype, cur["answer"])
        diff = cur["diff"] or default_diff
        items.append({
            "qtype": qtype,
            "stem": stem,
            "options": opts,
            "answer": answer,
            "diff": diff,
            "analysis": cur["analysis"].strip(),
            # 文本路径原来完全不做归类，界面却写着"题干里出现电气/暖通…会自动归类"。
            # 这里补齐；仍归不出来的留给界面按第一分类兜底。
            "cat_id": cur["cat_id"] or _guess_cat(stem, cats),
            "sub_id": None,
            "tags": cur["tags"],
            "raw_answer": cur["answer"].strip(),
        })
        cur = None

    def feed(line):
        """喂入一行（已 strip）。行内可能粘连多条（「…。 标准号: …」），
        由调用方先按「标准号:」切开逐段喂入。"""
        nonlocal cur, section_type, last_seq, want_answer

        # 题型分节，如「一、单项选择题」
        m = SECTION_RE.match(line)
        if m and len(line) <= 46:
            t = _type_from_text(m.group(2))
            if t:
                flush()
                section_type = t
                return

        # 标准条文卷：「标准号: 《规范》YDxx ◆题干」直接开新题，出处收进题干。
        # 规范名有《…》和 <<…>> 两种写法。
        m = STD_RE.match(line)
        if m:
            flush()
            std = m.group(1) if m.group(1) is not None else m.group(2)
            body = "【《%s》%s】%s" % (std, m.group(3).strip(), m.group(4).strip())
            cur = {"stem_lines": [body], "options": [], "answer": "",
                   "analysis": "", "qtype": None, "diff": default_diff,
                   "cat_id": _match_cat(body, cats), "tags": ""}
            return

        # 新题
        m = NUM_RE.match(line)
        if m:
            body = m.group(2)
            # 排除「1.5 米」这类数字开头的题干内容
            if body and not re.match(r"^\d", body):
                flush()
                last_seq = int(m.group(1))
                diff = default_diff
                dm = DIFF_RE.match(body)
                if dm:
                    diff = DIFF_MAP.get(dm.group(1), default_diff)
                    body = body[dm.end():]
                cat_id = _match_cat(body, cats)
                cur = {"stem_lines": [body], "options": [], "answer": "",
                       "analysis": "", "qtype": None, "diff": diff,
                       "cat_id": cat_id, "tags": ""}
                return

        # 题号独占一行（表格里题号一个单元格、题干另一个单元格）：接续序号才当
        # 新题，否则正文中间孤立的页码会把好题截断。唯一例外是「1」——多张工作表
        # 各自从 1 编号，而上一题已经完整（有选项或答案）时，按新表开头重新计数。
        m = SEQ_RE.match(line)
        n = int(m.group(1)) if m else None
        if n is not None and (
                n == last_seq + 1
                or (n == 1 and (cur is None or cur["options"] or cur["answer"]))):
            flush()
            last_seq = n
            cat_id = None
            cur = {"stem_lines": [], "options": [], "answer": "",
                   "analysis": "", "qtype": None, "diff": default_diff,
                   "cat_id": cat_id, "tags": ""}
            return

        if cur is None:
            return

        # 解析行先判：「答案解析：…」以「答案」开头，先按答案判会把真答案顶掉，
        # 结果单选的答案被写成「解析：冷机直接带负荷…」，清洗后变成空。
        m = ANA_RE.match(line)
        if m:
            cur["analysis"] = m.group(1)
            return
        # 答案行。有的导出把「正确答案:」和值拆成两行，中间还隔着空行——
        # 这时只置一个标志，下一行整行都是答案（哪怕它长得像选项）。
        if want_answer:
            cur["answer"] = line.lstrip("○●◯〇◦ ").strip()
            want_answer = False
            return
        m = ANS_RE.match(line)
        if m:
            val = m.group(1).strip()
            if val:
                cur["answer"] = val
            else:
                want_answer = True
            return
        # 评分反馈（答题解析导出每道题后面都有）
        if FEEDBACK_RE.match(line):
            return
        # 选项行
        m = OPT_RE.match(line)
        if m and not cur["answer"]:
            key = m.group(2).upper()
            if not any(o["key"] == key for o in cur["options"]):
                mark = m.group(1)
                rest = m.group(3).strip()
                head, tails = _split_tail_options(rest, key)
                cur["options"].append({"key": key, "text": head,
                                       "correct": mark == CORRECT_MARK})
                for k, t in tails:
                    cur["options"].append({"key": k, "text": t, "correct": False})
                return
        # 无字母的圆点选项（P32）：○/● 后面直接跟内容。「●36」是正确项。
        # 只在有题干之后才认（题干还一行没有时，行首圆点更像正文标记）；
        # 选项已满 8 个也不再吃，防止整页圆点符号的正文被吞成一道题。
        m = CIRC_OPT_RE.match(line)
        if m and cur is not None and not cur["answer"] \
                and (cur["stem_lines"] or cur["options"]) and len(cur["options"]) < 8:
            used = {o["key"] for o in cur["options"]}
            key = next((k for k in "ABCDEFGH" if k not in used), None)
            if key:
                cur["options"].append({"key": key, "text": m.group(2).strip(),
                                       "correct": m.group(1) == CORRECT_MARK})
                return
        # 普通行：追加到最后一个选项或题干
        if cur["options"]:
            cur["options"][-1]["text"] += " " + line
        else:
            cur["stem_lines"].append(line)

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        # 一行物理文本里可能粘着多条题（「…。 标准号: 《…》…◆…」）——
        # 按「标准号:」出现的位置切开，逐段喂入。
        segs = re.split(r"(?=标准号\s*[:：])", line)
        if len(segs) > 1:
            for seg in segs:
                seg = seg.strip()
                if seg:
                    feed(seg)
        else:
            feed(line)

    flush()
    return items


def _match_cat(text, cats):
    if not cats:
        return None
    for c in cats:
        if c["name"] and c["name"] in (text or ""):
            return c["id"]
    return None


#: 分类关键词。真实卷子的题干很少直接写"电气""暖通"这些分类名，写的是设备与现象：
#: 「冷水机组冷凝器结垢」属于暖通，「UPS 旁路」属于电气。只按分类名匹配，等于没匹配。
#: 按命中次数取最多的那类；一类都不沾就留空，交给界面按第一分类兜底（宁可不猜）。
CAT_HINTS = {
    "电气": ("UPS", "配电", "变压器", "断路器", "母线", "电缆", "发电机", "开关柜",
             "电容", "整流", "逆变", "电压", "电流", "低压", "高压", "接地", "蓄电池"),
    "暖通": ("空调", "冷机", "冷水机组", "冷冻", "冷却", "新风", "风机", "冷塔",
             "冷却塔", "湿度", "送回风", "末端", "水系统", "氟"),
    "弱电": ("门禁", "监控", "摄像", "网络", "综合布线", "动环", "交换机", "录像",
             "对讲", "BA系统", "DDC", "弱电"),
    "消防": ("消防", "火灾", "喷淋", "灭火", "烟感", "温感", "报警阀", "防火"),
    "管理": ("制度", "规程", "交接班", "值班", "巡检", "应急", "演练", "培训", "台账"),
}


def _guess_cat(text, cats):
    """按分类名 → 关键词猜分类。分类是用户自己维护的，只映射名字里含上述词的那一类。"""
    if not cats:
        return None
    text = text or ""
    hit = _match_cat(text, cats)
    if hit:
        return hit
    best, best_score = None, 0
    for c in cats:
        name = c.get("name") or ""
        words = []
        for key, ws in CAT_HINTS.items():
            if key in name:
                words.extend(ws)
        if not words:
            continue
        score = sum(1 for w in words if w in text)
        if score > best_score:
            best, best_score = c["id"], score
    return best


# ---------------------------------------------------------------- 按表头导入

# 表头别名：用户单位表格的列名五花八门，常见写法都认
HEADER_ALIASES = {
    "qtype": ("题型", "题目类型", "题类型", "试题类型", "类型"),
    "stem": ("题干", "题目", "试题正文", "题目正文", "试题内容", "题干内容",
             "题目内容", "题面", "试题", "内容"),
    "answer": ("答案", "正确答案", "参考答案", "标准答案"),
    "analysis": ("解析", "答案解析", "试题解析", "题目解析", "说明"),
    "diff": ("难度", "难易", "难度等级", "难易程度"),
    "cat": ("分类", "专业", "大类", "专业大类", "一级分类", "所属分类", "类型"),
    "sub": ("二级", "二级分类", "子类", "子分类", "专业方向",
            "知识点二级", "二级知识点", "知识模块"),
    "tags": ("标签", "知识点", "考点", "知识点三级", "三级知识点"),
}
OPT_HEADER_RE = re.compile(r"^(?:选项)?([A-Ha-h])(?:项|选项)?$")


def _norm_header(cell):
    """表头归一：「试题正文（项目组）」这种带注解的，去掉括号注解再比对。"""
    s = (cell or "").replace(" ", "").replace("\u3000", "").replace("\n", "").strip()
    s = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]\s*$", "", s)
    return s.strip("*＊:：")


def _header_map(row):
    """识别表头，返回 {字段: 列号}；不是表头返回 None。

    只要认出了题干或答案列就当表头处理 —— 没有这两列的表不可能是按列组织的题库。
    匹配**按别名的先后**而不是按列的先后：一行里同时有「类型」和「题型」时，
    「题型」才是题型，而「类型」往往是专业大类（真实题库里就是这样写的）。
    """
    if not row:
        return None
    norm = [_norm_header(c) for c in row]
    hmap = {}
    for field, names in HEADER_ALIASES.items():
        for name in names:
            hit = None
            for i, t in enumerate(norm):
                if t == name:
                    hit = i
                    break
            if hit is not None:
                hmap[field] = hit
                break
    for i, t in enumerate(norm):
        if not t:
            continue
        m = OPT_HEADER_RE.match(t)
        if m:
            hmap.setdefault("opt:" + m.group(1).upper(), i)
    if "stem" not in hmap and "answer" not in hmap:
        return None
    return hmap


def _cell(row, hmap, field):
    idx = hmap.get(field)
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _diff_from_text(text, default="mid"):
    t = _norm_header(text)
    if not t:
        return default
    if t in DIFF_MAP:
        return DIFF_MAP[t]
    for k, v in DIFF_MAP.items():
        if k in t:
            return v
    return default


#: 题干单元格内嵌选项的标号：`A、xxx`、`B.xxx`、`C）xxx`。真实题库（厂商模板）
#: 常把选项跟题干放在同一格里，用换行分隔；也有挤在一行用空格隔开的。
_INLINE_OPT = re.compile(r"(?:^|(?<=[\s\n、，,；;]))\s*[（(\[]?\s*([A-Ha-h])\s*[、．.)）:：]\s*")


def _split_inline_options(text):
    """把内嵌在题干里的选项拆出来，返回 (题干, 选项)。

    只认**从 A 开始、连续递增**的一组标号：题干里偶尔出现"按 A、B 两级分类"
    这种写法，不能见到字母加顿号就当选项，否则题干会被腰斩。
    """
    s = text or ""
    marks = list(_INLINE_OPT.finditer(s))
    start = None
    for i, m in enumerate(marks):
        if m.group(1).upper() == "A":
            start = i
            break
    if start is None:
        return s, []
    picked = []
    want = ord("A")
    for m in marks[start:]:
        if ord(m.group(1).upper()) == want:
            picked.append(m)
            want += 1
    if len(picked) < 2:
        return s, []
    stem = s[:picked[0].start()].strip()
    if not stem:
        return s, []
    opts = []
    for i, m in enumerate(picked):
        end = picked[i + 1].start() if i + 1 < len(picked) else len(s)
        opts.append({"key": m.group(1).upper(),
                     "text": s[m.end():end].strip(" \t\n、，,；;")})
    return stem, opts


def _rows_to_items(rows, cats=None, default_type=None, default_diff="mid"):
    """有表头就按列建题；没有表头返回 None，交给通用拆题逻辑。"""
    if not rows:
        return None
    hmap = _header_map(rows[0])
    if not hmap:
        return None
    # 按选项标号排序，而不是按列位置：列顺序被打乱时 A/B/C 的语义不能跟着乱
    opt_cols = sorted((k.split(":", 1)[1], v) for k, v in hmap.items()
                      if k.startswith("opt:"))
    items = []
    for row in rows[1:]:
        stem = _cell(row, hmap, "stem")
        answer_raw = _cell(row, hmap, "answer")
        if not stem and not answer_raw:
            continue
        options = []
        for key, idx in opt_cols:
            text = (row[idx] or "").strip() if idx < len(row) else ""
            if text:
                options.append({"key": key, "text": text})
        qtype = _type_from_text(_cell(row, hmap, "qtype")) or default_type
        qtype = qtype or _guess_type(stem, options, answer_raw)
        # 选择题的选项经常写在题干单元格里（每行一个 A、B、C…）而不是单独的列。
        # 不拆出来就变成"选择题没有选项"，题干里还留着一串 A、B、C。
        if not options and qtype in ("single", "multi"):
            stem, options = _split_inline_options(stem)
        answer = _clean_answer(qtype, answer_raw)
        cat_id = None
        sub_id = None
        cat_name = _cell(row, hmap, "cat")
        sub_name = _cell(row, hmap, "sub")
        for c in (cats or []):
            if cat_name and (cat_name == c["name"] or cat_name in c["name"] or c["name"] in cat_name):
                cat_id = c["id"]
                if sub_name:
                    for s in (c.get("subs") or []):
                        if sub_name == s["name"] or sub_name in s["name"] or s["name"] in sub_name:
                            sub_id = s["id"]
                            break
                break
        if cat_id is None:
            # 没填分类列时，仍按题干关键词兜底
            cat_id = _guess_cat(stem, cats)
        items.append({
            "qtype": qtype,
            "stem": stem,
            "options": options,
            "answer": answer,
            "diff": _diff_from_text(_cell(row, hmap, "diff"), default_diff),
            "analysis": _cell(row, hmap, "analysis"),
            "cat_id": cat_id,
            "sub_id": sub_id,
            "tags": _cell(row, hmap, "tags"),
            "raw_answer": answer_raw,
        })
    return items or None


# ---------------------------------------------------------------- 导入模板

TEMPLATE_HEADER = ["题型", "分类", "二级分类", "难度", "题干",
                   "A", "B", "C", "D", "答案", "解析"]
TEMPLATE_ROWS = [
    ["单选题", "电气", "供配电", "易",
     "机房市电中断后，UPS 应能维持负载供电不少于（　）分钟。",
     "5", "10", "15", "30", "C", "后备时间按机房等级要求确定，一般不低于 15 分钟。"],
    ["多选题", "消防", "气体灭火", "中",
     "机房气体灭火系统喷放前应完成的动作包括（　）。",
     "关闭空调新风", "关闭门窗", "启动声光报警", "切断非消防电源",
     "ABCD", "灭火剂喷放前必须形成封闭空间并疏散人员。"],
    ["判断题", "暖通", "精密空调", "中",
     "精密空调可以频繁启停。", "", "", "", "",
     "错误", "频繁启停会缩短压缩机寿命。"],
    ["填空题", "电气", "UPS", "中",
     "机房温度应控制在 18~____ ℃，湿度 40%~____。", "", "", "", "",
     "27|60", "多个空用竖线分隔。"],
    ["问答题", "管理", "应急演练", "难",
     "简述市电中断后的应急处置流程。", "", "", "", "",
     "确认告警→启动柴油发电机→切换负荷→持续监控",
     "按要点给分。"],
]


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _col_letter(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xlsx_bytes(rows, widths, sheet_name="题库"):
    """把二维表写成 xlsx（纯标准库，内联字符串，不依赖 openpyxl）。"""
    body = []
    for r, values in enumerate(rows, 1):
        cells = []
        for c, val in enumerate(values):
            if val == "" or val is None:
                continue
            ref = "%s%d" % (_col_letter(c), r)
            cells.append('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                         % (ref, _xml_escape(val)))
        body.append('<row r="%d">%s</row>' % (r, "".join(cells)))
    cols = "".join('<col min="%d" max="%d" width="%d" customWidth="1"/>'
                   % (i + 1, i + 1, w) for i, w in enumerate(widths))
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<cols>%s</cols><sheetData>%s</sheetData></worksheet>' % (cols, "".join(body))
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
            ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets></workbook>'
            % _xml_escape(sheet_name[:31])
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>'
        ),
        "xl/worksheets/sheet1.xml": sheet,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content.encode("utf-8"))
    return buf.getvalue()


def template_xlsx():
    """生成导入模板 .xlsx。

    模板本身必须能被本系统原样读回——这是"下载模板→填写→导回"闭环的前提，
    因此列名严格取自 HEADER_ALIASES 的规范写法，且示例行覆盖五种题型。
    """
    return _xlsx_bytes([TEMPLATE_HEADER] + TEMPLATE_ROWS,
                       [10, 10, 12, 8, 52, 16, 16, 16, 16, 22, 36], "题库模板")


EXPORT_HEADER = ["题型", "分类", "二级分类", "难度", "题干", "选项", "答案", "解析"]


def export_xlsx(rows, sheet_name="题库"):
    """按当前筛选导出为 Excel（FR-QB-07）。

    列与模板保持一致，导出的文件可以直接改完再导回来。
    """
    return _xlsx_bytes([EXPORT_HEADER] + rows, [12, 10, 16, 8, 56, 44, 16, 40], sheet_name)


def _looks_like_header(row):
    """整行像不像表头：至少三个单元格能对上已知列名（含选项列）。"""
    hit = 0
    for cell in row:
        t = _norm_header(cell)
        if not t:
            continue
        if any(t in names for names in HEADER_ALIASES.values()) or OPT_HEADER_RE.match(t):
            hit += 1
    return hit >= 3


def _drop_repeated_headers(rows):
    """多工作表拼在一起后，第二张表以后的表头行会被当成一道题。

    真实题库常把「通用专业知识」「通用维护知识」分两张工作表。两张表的表头还可能
    不完全一样（有的表有「参考答案」列、有的没有），所以不能只比"整行相同"，
    要按"这一行看起来是不是表头"来判断。
    """
    if not rows or len(rows) < 2:
        return rows
    out = [rows[0]]
    for row in rows[1:]:
        if _looks_like_header(row):
            continue
        out.append(row)
    return out


def postprocess(items):
    """补题型、清空非法项、生成提示。"""
    out = []
    warn_ans = []
    warn_opt = []
    for i, q in enumerate(items, 1):
        stem = (q.get("stem") or "").strip()
        if not stem:
            continue
        qtype = q.get("qtype") if q.get("qtype") in TYPES else "qa"
        opts = [o for o in (q.get("options") or []) if (o.get("text") or "").strip()]
        if qtype in ("single", "multi") and len(opts) < 2:
            qtype = "qa" if not opts else qtype
        answer = (q.get("answer") or "").strip()
        if not answer:
            warn_ans.append("第 %d 题没有识别到答案：%s" % (i, stem[:18]))
        if qtype in ("single", "multi") and not opts:
            warn_opt.append("第 %d 题识别为选择题但没有选项：%s" % (i, stem[:18]))
        q["qtype"] = qtype
        q["options"] = opts
        q["answer"] = answer
        out.append(q)

    # 整份文件都没有答案列时，逐题提示会刷出几百条，把人吓退，也看不见别的提示。
    # 这种情况一句话说清就够了——它是"文件缺一列"，不是"这几题有问题"。
    # 只有几条题时不合并：那时逐条说更清楚。
    if out and len(warn_ans) == len(out) and len(out) >= 4:
        warn_ans = ["这份文件里没有「答案」列（%d 题全部没有答案）。可以先入库，"
                    "再逐题补答案；也可以把答案列加进表格后重新导入" % len(out)]
    return out, warn_ans + warn_opt


def parse_text(text, cats=None, default_type=None):
    """已抽出文字的材料走同一套规则解析（OCR 通道复用，P30）。"""
    text = strip_furniture(text or "")
    items = split_questions(text, default_type=default_type, cats=cats)
    items, warns = postprocess(items)
    if not items:
        raise ImportError_("OCR 认出了文字但没拆出题目：页面版式可能太乱，请改用视觉模型识别")
    return {"items": items, "warnings": warns, "chars": len(text), "by": "text"}


def parse(raw, filename, cats=None, default_type=None):
    if len(raw) > MAX_FILE:
        raise ImportError_("文件超过 20MB，请拆分后再导入")
    ext = (filename or "").lower().rsplit(".", 1)[-1]

    # 表格文件优先按表头取列：列顺序怎么换、多了无关列都不影响导入
    if ext in ("xlsx", "csv", "xls"):
        if ext == "xlsx":
            rows = _xlsx_rows(raw)
        elif ext == "xls":
            rows = _legacy(legacy.xls_rows, raw)
        else:
            rows = _csv_rows(_decode(raw))
        rows = _drop_repeated_headers(rows)
        items = _rows_to_items(rows, cats, default_type)
        if items:
            items, warns = postprocess(items)
            if not items:
                raise ImportError_("表格里有表头但没有可用的题目行")
            return {"items": items, "warnings": warns,
                    "chars": sum(len(str(c)) for r in rows for c in r),
                    "by": "header"}

    text = read_file(raw, filename)
    if not text.strip():
        raise ImportError_("文件内容为空，或无法提取到文字")
    text = strip_furniture(text)
    items = split_questions(text, default_type=default_type, cats=cats)
    items, warns = postprocess(items)
    if not items:
        raise ImportError_("没有识别到题目。请确认题库是「1. 题干 / A. 选项 / 答案：X」这类结构，"
                           "或先用 Excel 模板整理")
    return {"items": items, "warnings": warns, "chars": len(text), "by": "text"}
