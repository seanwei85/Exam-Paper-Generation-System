#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机房运维试题库与自动组卷系统 —— 本机服务

只用 Python 标准库，无第三方依赖。
启动后浏览器访问 http://127.0.0.1:8765
"""

import base64
import json
import logging
import os
import random
import shutil
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, date
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

BASE = os.path.dirname(os.path.abspath(__file__))
import dupe  # noqa: E402  与本文件同目录，纯标准库
DATA_DIR = os.path.join(BASE, "data")
BACKUP_DIR = os.path.join(DATA_DIR, "backup")
DB_PATH = os.path.join(DATA_DIR, "library.db")
UI_PATH = os.path.join(BASE, "ui.html")
LOG_PATH = os.path.join(DATA_DIR, "app.log")

DEFAULT_PORT = 8765
PORT_RANGE = 20

APP_VERSION = "V1.12"

# 判断"当前进程跑的是不是最新代码"：启动那一刻给源码拍个时间戳快照，
# 之后每次问就把磁盘上的时间跟快照比。比快照新 = 进程里还是老代码。
# 这不是开发者洁癖：软件是单机常驻的，改了代码却忘了重启，
# 界面（磁盘上的 ui.html，刷新即生效）是新的、后端（进程内存里）是旧的，
# 表现就是"按钮点下去报 not found"，从界面上完全看不出原因。
CODE_FILES = ("server.py", "ui.html", "importer.py", "ai_parse.py", "ai_review.py",
              "paper_engine.py", "dupe.py", "pdftext.py", "legacy.py", "ole.py")


def code_mtime(base=None):
    """源码文件里最新的修改时间（秒）。取不到的文件跳过。"""
    latest = 0.0
    for name in CODE_FILES:
        try:
            latest = max(latest, os.path.getmtime(os.path.join(base or BASE, name)))
        except OSError:
            pass
    return latest


_BOOT_MTIME = code_mtime()
_BOOT_AT = time.time()


def code_freshness(boot_mtime=None, base=None):
    """返回运行状态。stale=True 表示磁盘上的代码比本进程新，需要重启。

    容差 1 秒：避免文件系统时间精度造成的误报（刚启动就提示重启最招人烦）。
    """
    boot = _BOOT_MTIME if boot_mtime is None else boot_mtime
    disk = code_mtime(base)
    stale = disk > boot + 1.0
    return {
        "version": APP_VERSION,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_BOOT_AT)),
        "boot_code_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(boot)) if boot else "",
        "disk_code_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(disk)) if disk else "",
        "stale": stale,
    }


def _cli_args():
    """在模块级解析启动参数。

    注意：以 python server.py 方式运行时本文件是 __main__ 模块，而
    paper_engine 里的 import server 会再加载一份模块副本。若在 main()
    里改全局路径，两份副本的 DB_PATH 会不一致（出现"读 A 库、写 B 库"），
    因此必须在模块加载时就定好路径。
    """
    data, port = None, None
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--data-dir" and i + 1 < len(args):
            data = args[i + 1]
        elif a == "--port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                port = None
    return data, port


_CLI_DATA, CLI_PORT = _cli_args()
if _CLI_DATA:
    DATA_DIR = os.path.abspath(_CLI_DATA)
    BACKUP_DIR = os.path.join(DATA_DIR, "backup")
    DB_PATH = os.path.join(DATA_DIR, "library.db")
    LOG_PATH = os.path.join(DATA_DIR, "app.log")

TYPES = ["single", "multi", "judge", "fill", "qa"]
TYPE_NAMES = {
    "single": "单项选择题",
    "multi": "多项选择题",
    "judge": "判断题",
    "fill": "填空题",
    "qa": "问答题",
}
DIFFS = ["easy", "mid", "hard"]
DIFF_NAMES = {"easy": "易", "mid": "中", "hard": "难"}

# 难度积分：客观题/填空题 易0 中1 难2；问答题 易0 中2 难4
DIFF_POINTS = {
    "single": {"easy": 0, "mid": 1, "hard": 2},
    "multi": {"easy": 0, "mid": 1, "hard": 2},
    "judge": {"easy": 0, "mid": 1, "hard": 2},
    "fill": {"easy": 0, "mid": 1, "hard": 2},
    "qa": {"easy": 0, "mid": 2, "hard": 4},
}

DEFAULT_SETTINGS = {
    "unit_name": "XX 数据中心",
    "paper_prefix": "ZJLab",
    "duration": "45",
    "score_single": "1",
    "score_multi": "2",
    "score_judge": "1",
    "score_fill_blank": "1",
    "score_qa": "10",
    "backup_keep": "30",
}

DEFAULT_CATEGORIES = [
    ("电气", ["市电与配电", "UPS", "蓄电池", "柴油发电机", "防雷接地"]),
    ("暖通", ["精密空调", "气流组织", "温湿度", "水系统", "新风排风"]),
    ("弱电", ["综合布线", "网络设备", "动环监控", "门禁", "视频监控"]),
    ("消防", ["气体灭火", "火灾报警", "灭火器材", "防火封堵", "应急疏散"]),
    ("管理", ["巡检制度", "变更与工单", "应急预案", "值班交接", "安全规范"]),
]


# ---------------------------------------------------------------- 基础设施

def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def setup_logging():
    ensure_dirs()
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_ai_review(cur):
    """老库升级：给早期建的 ai_review 表补上 old_* 两列。

    CREATE TABLE IF NOT EXISTS 只保证"表在"，不会给已有表补列。正式库里已经躺着
    几百道题，不能靠删库重建来升级结构，所以这里显式 ALTER 一次。
    """
    cols = {r[1] for r in cur.execute("PRAGMA table_info(ai_review)")}
    if not cols:
        return
    for name in ("old_answer", "old_analysis"):
        if name not in cols:
            cur.execute("ALTER TABLE ai_review ADD COLUMN %s TEXT NOT NULL DEFAULT ''" % name)


def init_db():
    ensure_dirs()
    conn = connect()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS category (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL UNIQUE,
            sort    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subcategory (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            cat_id  INTEGER NOT NULL REFERENCES category(id) ON DELETE CASCADE,
            name    TEXT NOT NULL,
            sort    INTEGER NOT NULL DEFAULT 0,
            UNIQUE(cat_id, name)
        );
        CREATE TABLE IF NOT EXISTS question (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cat_id      INTEGER NOT NULL REFERENCES category(id),
            sub_id      INTEGER NOT NULL REFERENCES subcategory(id),
            qtype       TEXT NOT NULL,
            diff        TEXT NOT NULL,
            stem        TEXT NOT NULL,
            options     TEXT NOT NULL DEFAULT '[]',
            answer      TEXT NOT NULL DEFAULT '',
            blanks      INTEGER NOT NULL DEFAULT 0,
            score       REAL,
            analysis    TEXT NOT NULL DEFAULT '',
            tags        TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            used_count  INTEGER NOT NULL DEFAULT 0,
            last_used   TEXT
        );
        CREATE TABLE IF NOT EXISTS paper (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            no          TEXT NOT NULL UNIQUE,
            title       TEXT NOT NULL,
            unit        TEXT NOT NULL DEFAULT '',
            duration    INTEGER NOT NULL DEFAULT 45,
            total_score REAL NOT NULL DEFAULT 0,
            q_count     INTEGER NOT NULL DEFAULT 0,
            diff_score  INTEGER NOT NULL DEFAULT 0,
            config      TEXT NOT NULL DEFAULT '{}',
            seed        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS paper_item (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id  INTEGER NOT NULL REFERENCES paper(id) ON DELETE CASCADE,
            seq       INTEGER NOT NULL,
            qid       INTEGER,
            qtype     TEXT NOT NULL,
            diff      TEXT NOT NULL,
            stem      TEXT NOT NULL,
            options   TEXT NOT NULL DEFAULT '[]',
            answer    TEXT NOT NULL DEFAULT '',
            analysis  TEXT NOT NULL DEFAULT '',
            blanks    INTEGER NOT NULL DEFAULT 0,
            score     REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS setting (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS import_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            filename   TEXT NOT NULL,
            total      INTEGER NOT NULL DEFAULT 0,
            imported   INTEGER NOT NULL DEFAULT 0,
            failed     INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_review (
            qid              INTEGER PRIMARY KEY REFERENCES question(id) ON DELETE CASCADE,
            kind             TEXT NOT NULL DEFAULT 'fill',
            verdict          TEXT NOT NULL DEFAULT 'filled',
            suggest_answer   TEXT NOT NULL DEFAULT '',
            suggest_analysis TEXT NOT NULL DEFAULT '',
            old_answer       TEXT NOT NULL DEFAULT '',
            old_analysis     TEXT NOT NULL DEFAULT '',
            reason           TEXT NOT NULL DEFAULT '',
            confidence       REAL NOT NULL DEFAULT 0,
            model            TEXT NOT NULL DEFAULT '',
            valid            INTEGER NOT NULL DEFAULT 1,
            problem          TEXT NOT NULL DEFAULT '',
            state            TEXT NOT NULL DEFAULT 'pending',
            created_at       TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_q_cat ON question(cat_id);
        CREATE INDEX IF NOT EXISTS idx_q_type ON question(qtype);
        CREATE INDEX IF NOT EXISTS idx_pi_paper ON paper_item(paper_id);
        CREATE INDEX IF NOT EXISTS idx_rv_state ON ai_review(state, verdict);
        """
    )

    _migrate_ai_review(cur)

    cur.execute("SELECT COUNT(*) AS c FROM category")
    if cur.fetchone()["c"] == 0:
        for i, (name, subs) in enumerate(DEFAULT_CATEGORIES):
            cur.execute("INSERT INTO category(name, sort) VALUES(?,?)", (name, i))
            cid = cur.lastrowid
            for j, sub in enumerate(subs):
                cur.execute(
                    "INSERT INTO subcategory(cat_id, name, sort) VALUES(?,?,?)",
                    (cid, sub, j),
                )

    for k, v in DEFAULT_SETTINGS.items():
        cur.execute("INSERT OR IGNORE INTO setting(key, value) VALUES(?,?)", (k, v))

    conn.commit()

    cur.execute("SELECT COUNT(*) AS c FROM question")
    if cur.fetchone()["c"] == 0:
        import seed_data

        seed_data.load(conn, DIFF_POINTS)
        logging.info("已导入示例题库")

    conn.close()


def daily_backup():
    """每日首次启动自动备份一份，保留最近 N 份。"""
    try:
        conn = connect()
        keep = int(get_setting("backup_keep") or 30)
        conn.close()
        today = date.today().strftime("%Y%m%d")
        target = os.path.join(BACKUP_DIR, f"library_{today}.db")
        if not os.path.exists(target):
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(target)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
            logging.info("自动备份: %s", target)
        files = sorted(
            (f for f in os.listdir(BACKUP_DIR) if f.startswith("library_")),
            reverse=True,
        )
        for old in files[keep:]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception as exc:  # 备份失败不影响启动
        logging.exception("自动备份失败: %s", exc)


def get_setting(key, conn=None):
    own = conn is None
    conn = conn or connect()
    row = conn.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    if own:
        conn.close()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def all_settings(conn=None):
    own = conn is None
    conn = conn or connect()
    rows = conn.execute("SELECT key, value FROM setting").fetchall()
    if own:
        conn.close()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def question_score(row, settings):
    """单题分值：建议分值 > 题型默认（填空按空数×每空分值）。"""
    if row["score"] not in (None, ""):
        return float(row["score"])
    t = row["qtype"]
    if t == "fill":
        blanks = row["blanks"] or 1
        return float(settings.get("score_fill_blank", 1)) * blanks
    return float(settings.get(f"score_{t}", 1))


def count_blanks(stem):
    """统计题干中的空位数量：连续下划线算一个空。"""
    import re

    marks = re.findall(r"_{2,}", stem or "")
    return max(1, len(marks))


def categories_tree(conn=None):
    own = conn is None
    conn = conn or connect()
    cats = conn.execute("SELECT * FROM category ORDER BY sort, id").fetchall()
    subs = conn.execute("SELECT * FROM subcategory ORDER BY sort, id").fetchall()
    if own:
        conn.close()
    result = []
    for c in cats:
        result.append(
            {
                "id": c["id"],
                "name": c["name"],
                "subs": [
                    {"id": s["id"], "name": s["name"]}
                    for s in subs
                    if s["cat_id"] == c["id"]
                ],
            }
        )
    return result


# ---------------------------------------------------------------- 导入日志

def log_import(filename, total, imported, failed):
    """记一条导入记录（FR-IM-10）。空提交由调用方拦掉，不写噪音。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO import_log(filename, total, imported, failed, created_at) "
            "VALUES(?,?,?,?,?)",
            ((filename or "").strip() or "未命名文件", int(total), int(imported),
             int(failed), now_str()))
        conn.commit()
    finally:
        conn.close()


def list_import_logs(limit=None):
    """最近的导入记录，按时间倒序。limit 缺省 20，最多 200。"""
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 20
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, filename, total, imported, failed, created_at "
            "FROM import_log ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 业务

def stats(conn=None):
    own = conn is None
    conn = conn or connect()
    total = conn.execute("SELECT COUNT(*) c FROM question").fetchone()["c"]
    by_cat = {
        r["cat_id"]: r["c"]
        for r in conn.execute(
            "SELECT cat_id, COUNT(*) c FROM question GROUP BY cat_id"
        )
    }
    by_type = {
        r["qtype"]: r["c"]
        for r in conn.execute("SELECT qtype, COUNT(*) c FROM question GROUP BY qtype")
    }
    # 缺答案的题（老题库常常没有答案列）只能算"存着"，不能算"能组卷"：
    # 健康度若按总题数报"够用"，用户一点组卷才发现凑不出，这种自相矛盾最难查。
    by_type_ready = {
        r["qtype"]: r["c"]
        for r in conn.execute(
            "SELECT qtype, COUNT(*) c FROM question "
            "WHERE TRIM(COALESCE(answer,''))<>'' GROUP BY qtype")
    }
    draft = conn.execute(
        "SELECT COUNT(*) c FROM question WHERE TRIM(COALESCE(answer,''))=''"
    ).fetchone()["c"]
    by_diff = {
        r["diff"]: r["c"]
        for r in conn.execute("SELECT diff, COUNT(*) c FROM question GROUP BY diff")
    }
    papers = conn.execute("SELECT COUNT(*) c FROM paper").fetchone()["c"]
    recent = [
        dict(r)
        for r in conn.execute(
            "SELECT id, no, title, created_at, total_score, q_count "
            "FROM paper ORDER BY id DESC LIMIT 5"
        )
    ]
    cats = categories_tree(conn)
    if own:
        conn.close()
    return {
        "total": total,
        "draft": draft,
        "by_cat": [{"id": c["id"], "name": c["name"], "count": by_cat.get(c["id"], 0)} for c in cats],
        "by_type": [{"type": t, "name": TYPE_NAMES[t], "count": by_type.get(t, 0),
                     "ready": by_type_ready.get(t, 0)} for t in TYPES],
        "by_diff": [{"diff": d, "name": DIFF_NAMES[d], "count": by_diff.get(d, 0)} for d in DIFFS],
        "papers": papers,
        "recent": recent,
    }


def _q_where(params):
    """题库筛选条件。列表和导出共用一份，避免"看到的"和"导出的"不一致。"""
    where = ["1=1"]
    args = []
    if params.get("cat"):
        where.append("cat_id=?")
        args.append(int(params["cat"]))
    if params.get("sub"):
        where.append("sub_id=?")
        args.append(int(params["sub"]))
    if params.get("type"):
        where.append("qtype=?")
        args.append(params["type"])
    if params.get("diff"):
        where.append("diff=?")
        args.append(params["diff"])
    kw = (params.get("kw") or "").strip()
    if kw:
        where.append("(stem LIKE ? OR answer LIKE ? OR tags LIKE ?)")
        like = f"%{kw}%"
        args += [like, like, like]
    # 缺答案的题（老题库常常没有答案列）需要单独筛出来补，否则混在几百题里没法找
    ans = params.get("answer")
    if ans == "missing":
        where.append("TRIM(COALESCE(answer,''))=''")
    elif ans == "has":
        where.append("TRIM(COALESCE(answer,''))<>''")
    # AI 审校结果：存疑 = AI 认为答案可能错或拿不准，是补答案/改答案的第一入口
    ai = params.get("ai")
    if ai == "suspect":
        where.append("id IN (SELECT qid FROM ai_review WHERE state='pending' "
                     "AND verdict IN ('wrong','unsure'))")
    elif ai == "pending":
        where.append("id IN (SELECT qid FROM ai_review WHERE state='pending')")
    return " AND ".join(where), args


def export_questions(params):
    """按当前筛选导出 Excel（FR-QB-07），分页不生效——导出就是全部符合条件的题。"""
    import importer
    conn = connect()
    where, args = _q_where(params)
    rows = conn.execute(
        f"SELECT * FROM question WHERE {where} ORDER BY cat_id, qtype, id", args).fetchall()
    cats = {c["id"]: c["name"] for c in categories_tree(conn)}
    subs = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM subcategory")}
    conn.close()

    out = []
    for r in rows:
        opts = json.loads(r["options"] or "[]")
        opt_text = "  ".join("%s. %s" % (o.get("key", ""), o.get("text", ""))
                             for o in opts if (o.get("text") or "").strip())
        out.append([
            TYPE_NAMES.get(r["qtype"], ""), cats.get(r["cat_id"], ""),
            subs.get(r["sub_id"], ""), DIFF_NAMES.get(r["diff"], ""),
            r["stem"], opt_text, r["answer"], r["analysis"],
        ])
    return importer.export_xlsx(out), len(out)


def list_questions(params):
    conn = connect()
    settings = all_settings(conn)
    where, args = _q_where(params)

    sort = params.get("sort") or "id_desc"
    order = {
        "id_desc": "id DESC",
        "id_asc": "id ASC",
        "updated": "updated_at DESC",
        "used": "used_count DESC",
    }.get(sort, "id DESC")

    page = max(1, int(params.get("page") or 1))
    size = min(200, max(10, int(params.get("size") or 20)))

    total = conn.execute(f"SELECT COUNT(*) c FROM question WHERE {where}", args).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM question WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        args + [size, (page - 1) * size],
    ).fetchall()

    cats = {c["id"]: c["name"] for c in categories_tree(conn)}
    subs = {}
    for c in conn.execute("SELECT id, name FROM subcategory").fetchall():
        subs[c["id"]] = c["name"]
    # 待处理建议随行带出：列表上要能标「AI 存疑 / 有建议」，否则用户不知道该点进哪一题
    rv = {}
    for r in conn.execute("SELECT qid, kind, verdict, confidence FROM ai_review "
                          "WHERE state='pending'"):
        rv[r["qid"]] = {"kind": r["kind"], "verdict": r["verdict"],
                        "confidence": r["confidence"]}
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"] or "[]")
        d["cat_name"] = cats.get(r["cat_id"], "")
        d["sub_name"] = subs.get(r["sub_id"], "")
        d["diff_name"] = DIFF_NAMES.get(r["diff"], "")
        d["type_name"] = TYPE_NAMES.get(r["qtype"], "")
        d["points"] = DIFF_POINTS.get(r["qtype"], {}).get(r["diff"], 0)
        d["final_score"] = question_score(r, settings)
        d["ai"] = rv.get(r["id"])
        items.append(d)
    return {"total": total, "page": page, "size": size, "items": items}


def get_question(qid):
    conn = connect()
    row = conn.execute("SELECT * FROM question WHERE id=?", (qid,)).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "题目不存在"}
    d = dict(row)
    d["options"] = json.loads(d["options"] or "[]")
    d["type_name"] = TYPE_NAMES.get(d["qtype"], "")
    d["diff_name"] = DIFF_NAMES.get(d["diff"], "")
    d["points"] = DIFF_POINTS.get(d["qtype"], {}).get(d["diff"], 0)
    d["final_score"] = question_score(row, all_settings(conn))
    d["cat_name"] = (conn.execute("SELECT name FROM category WHERE id=?",
                                  (row["cat_id"],)).fetchone() or {"name": ""})["name"]
    # 编辑页要把「现有答案 / AI 建议」并排显示，所以整条建议一起带上
    rv = conn.execute("SELECT * FROM ai_review WHERE qid=?", (qid,)).fetchone()
    conn.close()
    d["ai_review"] = dict(rv) if rv else None
    return d


def normalize_options(options):
    out = []
    for i, opt in enumerate(options or []):
        text = (opt if isinstance(opt, str) else opt.get("text", "")).strip()
        if text:
            out.append({"key": "ABCDEF"[len(out)], "text": text})
    return out


def validate_question(payload):
    errors = []
    qtype = payload.get("qtype")
    if qtype not in TYPES:
        errors.append("题型不合法")
    if payload.get("diff") not in DIFFS:
        errors.append("难度不合法")
    stem = (payload.get("stem") or "").strip()
    if not stem:
        errors.append("题干不能为空")
    if not payload.get("cat_id"):
        errors.append("未选择一级分类")
    if not payload.get("sub_id"):
        errors.append("未选择二级分类")
    answer = (payload.get("answer") or "").strip()
    options = normalize_options(payload.get("options"))
    # 答案可以为空：老题库（尤其厂商模板导出的面试题库）往往根本没有答案列，
    # 一律拒收等于整份文件白导。空答案的题按"草稿"对待——能存、能改，
    # 但组卷时会被排除（见 paper_engine.load_pool），不会出一份没有答案的卷子。
    if qtype in ("single", "multi"):
        if len(options) < 2:
            errors.append("选择题至少需要 2 个选项")
        keys = {o["key"] for o in options}
        letters = set(answer.replace(",", "").replace("，", "").upper())
        if answer:
            if qtype == "single" and len(letters) != 1:
                errors.append("单选题答案只能是 1 个选项")
            if qtype == "multi" and len(letters) < 2:
                errors.append("多选题答案至少 2 个选项")
            if not letters.issubset(keys):
                errors.append("答案中的选项不在选项范围内")
    if qtype == "judge" and answer and answer not in ("正确", "错误"):
        errors.append("判断题答案只能是“正确”或“错误”")
    return errors


def dup_hint(stem):
    """FR-QE-06 的比对：拿题干跟库内全部题干比一遍，命中就返回库里那道题。

    只做"给提示"这一件事，不决定存不存——是否放过由调用方（界面/接口）决定。
    """
    conn = connect()
    try:
        pool = [(r["id"], r["stem"]) for r in conn.execute("SELECT id, stem FROM question")]
    finally:
        conn.close()
    hit = dupe.find(stem or "", pool)
    if not hit:
        return None
    return {"id": hit[0], "score": hit[1], "stem": hit[2]}


def create_question(payload):
    """单题保存入口（界面走这里）。

    新建时先查重（FR-QE-06）：库里已有高度相似的题就不直接写，先回一个
    need_dup_confirm 让用户看清是哪道题；用户点「仍然保存」时带 allow_dup 再来一次。

    查重放在这一层、而不是 save_question 里，是因为 save_question 是纯粹的
    "校验 + 写库"，导入入库、脚本补题、单元测试都直接用它——在核心写入函数里
    加一道"相似就拒绝"，会让导入无法入库，语义上也不该由存储层决定去重策略。
    """
    if not payload.get("id") and not payload.get("allow_dup"):
        hit = dup_hint(payload.get("stem"))
        if hit:
            return {"ok": False, "need_dup_confirm": True, "dup": hit,
                    "errors": ["题库里已有高度相似的题目"]}
    return save_question(payload)


def save_question(payload):
    errors = validate_question(payload)
    if errors:
        return {"ok": False, "errors": errors}

    conn = connect()
    options = normalize_options(payload.get("options"))
    qtype = payload["qtype"]
    stem = payload["stem"].strip()
    blanks = int(payload.get("blanks") or count_blanks(stem)) if qtype == "fill" else 0
    score = payload.get("score")
    score = float(score) if score not in (None, "",) else None
    answer = payload["answer"].strip()
    if qtype in ("single", "multi"):
        answer = "".join(sorted(set(answer.upper().replace(",", "").replace("，", ""))))

    ts = now_str()
    qid = payload.get("id")
    if qid:
        conn.execute(
            """UPDATE question SET cat_id=?, sub_id=?, qtype=?, diff=?, stem=?, options=?,
               answer=?, blanks=?, score=?, analysis=?, tags=?, updated_at=? WHERE id=?""",
            (
                payload["cat_id"], payload["sub_id"], qtype, payload["diff"], stem,
                json.dumps(options, ensure_ascii=False), answer, blanks, score,
                payload.get("analysis", ""), payload.get("tags", ""), ts, qid,
            ),
        )
    else:
        cur = conn.execute(
            """INSERT INTO question(cat_id, sub_id, qtype, diff, stem, options, answer,
               blanks, score, analysis, tags, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payload["cat_id"], payload["sub_id"], qtype, payload["diff"], stem,
                json.dumps(options, ensure_ascii=False), answer, blanks, score,
                payload.get("analysis", ""), payload.get("tags", ""), ts, ts,
            ),
        )
        qid = cur.lastrowid
    conn.commit()
    conn.close()
    return {"ok": True, "id": qid}


def delete_questions(ids):
    conn = connect()
    conn.executemany("DELETE FROM question WHERE id=?", [(i,) for i in ids])
    # 题目没了，挂在它上面的 AI 建议不能留着——否则统计里会多出永远处理不了的一条
    conn.executemany("DELETE FROM ai_review WHERE qid=?", [(i,) for i in ids])
    conn.commit()
    conn.close()
    return {"ok": True, "deleted": len(ids)}


# ---------------------------------------------------------------- AI 审校
#
# 分工：ai_review 负责「问模型」（不碰库），这一层负责「存建议、等采纳、再写库」。
# 建议自成一个状态机（pending / adopted / dismissed），题目表不动——
# 模型看走眼时最坏结果只是多了一条待处理建议，而不是题库被改坏。


def save_review(rec):
    """写一条建议。一题只留最新一条：重跑就覆盖并回到待处理。"""
    conn = connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO ai_review(qid, kind, verdict, suggest_answer,
               suggest_analysis, reason, confidence, model, valid, problem, state, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)""",
            (rec.get("qid"), rec.get("kind") or "fill", rec.get("verdict") or "filled",
             rec.get("suggest_answer") or "", rec.get("suggest_analysis") or "",
             rec.get("reason") or "", float(rec.get("confidence") or 0),
             rec.get("model") or "", 1 if rec.get("valid", 1) else 0,
             rec.get("problem") or "", now_str()))
        conn.commit()
    finally:
        conn.close()


def get_review(qid):
    conn = connect()
    row = conn.execute("SELECT * FROM ai_review WHERE qid=?", (qid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_reviews(state="pending", limit=200):
    """建议清单（带题干），用于「AI 审校结果」弹窗与导出报告。"""
    try:
        n = max(1, min(500, int(limit)))
    except (TypeError, ValueError):
        n = 200
    conn = connect()
    rows = conn.execute(
        "SELECT r.*, q.stem, q.qtype, q.answer AS cur_answer, q.analysis AS cur_analysis "
        "FROM ai_review r JOIN question q ON q.id = r.qid "
        "WHERE r.state=? ORDER BY r.confidence DESC, r.qid DESC LIMIT ?",
        (state, n)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def review_summary():
    conn = connect()
    by = {r["verdict"]: r["c"] for r in conn.execute(
        "SELECT verdict, COUNT(*) c FROM ai_review WHERE state='pending' GROUP BY verdict")}
    draft = conn.execute(
        "SELECT COUNT(*) c FROM question WHERE TRIM(COALESCE(answer,''))=''").fetchone()["c"]
    adopted = conn.execute(
        "SELECT COUNT(*) c FROM ai_review WHERE state='adopted'").fetchone()["c"]
    conn.close()
    return {"pending": sum(by.values()), "by_verdict": by, "draft": draft,
            "adopted": adopted}


def adopt_reviews(ids, fields=None):
    """采纳建议 → 写进题目。**只有这一条路能改题**，且只改用户勾的那几项。

    校验不过的建议（答案不在选项里、空数不对）在这里再拦一次：界面上可能被绕过，
    真正写库的地方必须自己判断。
    """
    fields = [f for f in (fields or ["answer", "analysis"])
              if f in ("answer", "analysis")] or ["answer"]
    ids = [int(i) for i in ids]
    adopted, errors = [], []
    conn = connect()
    try:
        for qid in ids:
            row = conn.execute("SELECT * FROM ai_review WHERE qid=?", (qid,)).fetchone()
            if not row:
                errors.append({"id": qid, "error": "这道题没有 AI 建议"})
                continue
            if row["state"] != "pending":
                errors.append({"id": qid, "error": "这条建议已处理过"})
                continue
            if not row["valid"]:
                errors.append({"id": qid, "error": "建议不可采纳：%s"
                               % (row["problem"] or "格式不符")})
                continue
            qrow = conn.execute("SELECT answer, analysis FROM question WHERE id=?",
                                (qid,)).fetchone()
            if not qrow:
                errors.append({"id": qid, "error": "题目不存在"})
                continue
            sets, args = [], []
            if "answer" in fields and (row["suggest_answer"] or "").strip():
                sets.append("answer=?")
                args.append(row["suggest_answer"])
            if "analysis" in fields and (row["suggest_analysis"] or "").strip():
                sets.append("analysis=?")
                args.append(row["suggest_analysis"])
            if not sets:
                errors.append({"id": qid, "error": "这条建议里没有可写入的内容"})
                continue
            args += [now_str(), qid]
            conn.execute("UPDATE question SET " + ", ".join(sets)
                         + ", updated_at=? WHERE id=?", args)
            # 采纳前把原值留一份：人工确认也会点错，没有回退路径的改库操作不该存在
            conn.execute("UPDATE ai_review SET state='adopted', old_answer=?, old_analysis=? "
                         "WHERE qid=?", (qrow["answer"] or "", qrow["analysis"] or "", qid))
            adopted.append({"id": qid, "was_draft": not (qrow["answer"] or "").strip()})
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "adopted": adopted, "errors": errors}


def dismiss_reviews(ids):
    ids = [int(i) for i in ids]
    conn = connect()
    before = conn.total_changes
    conn.executemany("UPDATE ai_review SET state='dismissed' WHERE qid=? AND state='pending'",
                     [(i,) for i in ids])
    conn.commit()
    n = conn.total_changes - before
    conn.close()
    return {"ok": True, "dismissed": n}


def undo_reviews(ids):
    """撤销采纳：把答案与解析改回采纳前的样子，建议回到待处理。

    只回滚 answer / analysis 两项——采纳只会改这两项，撤销多改一个字段就是新的破坏。
    """
    ids = [int(i) for i in ids]
    restored, errors = [], []
    conn = connect()
    try:
        for qid in ids:
            row = conn.execute("SELECT * FROM ai_review WHERE qid=?", (qid,)).fetchone()
            if not row or row["state"] != "adopted":
                errors.append({"id": qid, "error": "这道题没有可撤销的采纳记录"})
                continue
            conn.execute("UPDATE question SET answer=?, analysis=?, updated_at=? WHERE id=?",
                         (row["old_answer"], row["old_analysis"], now_str(), qid))
            conn.execute("UPDATE ai_review SET state='pending' WHERE qid=?", (qid,))
            restored.append(qid)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "restored": restored, "errors": errors}


def review_batch(ids, job=None):
    """逐题审校。一题一次请求：题目短、失败只丢一题，也方便暂停。

    连续失败 3 次且一次没成，说明是通道问题（密钥、模型名、网关），
    继续跑只是把同一个错误重复 60 遍——直接停，把第一个错误交给界面。
    """
    import ai_parse
    import ai_review

    ids = [int(i) for i in (ids or [])]
    if not ids:
        return {"ok": False, "error": "没有选中题目"}
    if len(ids) > ai_review.MAX_BATCH:
        return {"ok": False,
                "error": "一次最多审校 %d 题，先按筛选条件缩小范围" % ai_review.MAX_BATCH}
    cfg = ai_parse.load_config()
    if not ai_parse.ready(cfg):
        return {"ok": False, "error": "还没配 AI 模型：到「系统设置 · AI 识别」"
                                     "填好接口地址与 API Key 再用 AI 审校"}

    done, failed = [], []
    for i, qid in enumerate(ids, 1):
        progress_update(job, stage="AI 审校第 %d/%d 题" % (i, len(ids)),
                        page=i, pages=len(ids))
        q = get_question(qid)
        if q.get("ok") is False:
            failed.append({"id": qid, "error": "题目不存在"})
            continue
        try:
            rec = ai_review.review_one(q, cfg=cfg)
        except (ai_parse.AIError, ai_review.ReviewError) as exc:
            failed.append({"id": qid, "error": str(exc)})
            if not done and len(failed) >= 3:
                break
            continue
        rec["qid"] = qid
        save_review(rec)
        done.append({"id": qid, "verdict": rec["verdict"], "valid": rec["valid"],
                     "problem": rec["problem"]})
    return {"ok": True, "done": done, "failed": failed}


# ---------------------------------------------------------------- 导入调度

#: 能直接交给视觉模型的图片类型（主流多模态接口都吃这几种）
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
              "webp": "image/webp"}
#: 常见但模型不吃的图片类型，要明确让用户转格式
IMAGE_CONVERT = ("bmp", "gif", "tif", "tiff", "heic", "heif", "emf", "wmf")


#: 导入进度登记表（job id -> 最近状态）。导入请求是同步阻塞的：正则在跑、模型在想，
#: 这段时间里 HTTP 连接上没有新字节，界面无从知道"在干活还是卡死"。所以由解析侧
#: 往这张表里写、界面用另一个请求轮询——ThreadingHTTPServer 天然支持并发请求。
PROGRESS = {}
PROGRESS_LOCK = threading.Lock()
PROGRESS_TTL = 900


def progress_start(job):
    if not job:
        return
    _progress_sweep()
    with PROGRESS_LOCK:
        PROGRESS[job] = {"state": "running", "stage": "读取文件",
                         "page": 0, "pages": 0, "ts": time.time()}


def progress_update(job, **kw):
    if not job:
        return
    with PROGRESS_LOCK:
        cur = PROGRESS.get(job)
        if not cur:
            return
        cur.update(kw)
        cur["ts"] = time.time()


def progress_finish(job, error=None):
    if not job:
        return
    with PROGRESS_LOCK:
        cur = PROGRESS.get(job)
        if not cur:
            return
        cur.update({"state": "error" if error else "done",
                    "stage": (error or "已完成")[:200], "ts": time.time()})


def progress_get(job):
    """给界面查的视图。未知/过期任务老实回 idle，不让页面自己猜。"""
    if not job:
        return {"ok": True, "state": "idle", "stage": "", "page": 0, "pages": 0}
    with PROGRESS_LOCK:
        cur = PROGRESS.get(job)
        if not cur:
            return {"ok": True, "state": "idle", "stage": "", "page": 0, "pages": 0}
        if time.time() - cur.get("ts", 0) > PROGRESS_TTL:
            PROGRESS.pop(job, None)
            return {"ok": True, "state": "idle", "stage": "", "page": 0, "pages": 0}
        out = dict(cur)
    out["ok"] = True
    return out


def _progress_sweep():
    """顺手清理僵尸任务：客户端关页面就不会再来查，不扫会一直攒着。"""
    cut = time.time() - PROGRESS_TTL
    with PROGRESS_LOCK:
        for k in [k for k, v in PROGRESS.items() if v.get("ts", 0) < cut]:
            PROGRESS.pop(k, None)


def _try_ocr_parse(raw, cats=None, default_type=None):
    """图片型 PDF 的本地兜底（P30，FR-IM-28）：Windows 自带 OCR 认字，
    认出的文字交给现有规则解析。返回 None 表示 OCR 不可用或没认出题目，
    调用方走原来的视觉模型流程；两道失败原因不同，报错文案要分得开。"""
    import importer, ocr_win, pdftext
    if not ocr_win.engine_ready():
        return None
    try:
        pages = pdftext.page_images(raw)
    except Exception:
        return None
    texts = []
    for pg in pages:
        png = pg.get("png")
        if not png:
            continue
        try:
            t = ocr_win.ocr_png_bytes(png)
        except ocr_win.OcrError:
            continue
        if t.strip():
            texts.append(t.strip())
    if not texts:
        return None
    try:
        res = importer.parse_text("\n".join(texts), cats=cats, default_type=default_type)
    except importer.ImportError_:
        return None
    res["by"] = "local-ocr"
    return res


def import_parse(payload):
    """导入解析的统一入口（本地规则 / AI 识别 由 engine 决定）。

    engine 三档：
      auto  —— 先本地规则，拆不出来才动 AI。默认档，标准格式的材料不花 token；
      ai    —— 全部交给模型，适合版式乱的卷子；
      local —— 只用本地规则，一次网络请求都不发（内容不能出网时选它）。

    这里刻意不把 AI 做成默认：用户手上大部分文件其实是规整的，全走模型既慢又贵。
    真正需要 AI 的只有"规则拆不出来"那一类，让它自动兜底即可。
    """
    import ai_parse
    import importer

    try:
        raw = base64.b64decode(payload.get("content") or "")
    except Exception:
        raise importer.ImportError_("文件内容传输不完整，请重新选择文件")
    filename = payload.get("filename") or ""
    engine = (payload.get("engine") or "auto").lower()
    if engine not in ("auto", "ai", "local"):
        engine = "auto"
    job = payload.get("job") or ""
    default_type = payload.get("default_type")
    cats = categories_tree()
    ext = filename.lower().rsplit(".", 1)[-1]

    def on_page(idx, total):
        # 图片型 PDF 逐页送模型，一页可能几分钟：不报页码，界面上就是"卡死"
        progress_update(job, stage="AI 识别第 %d/%d 页" % (idx, total),
                        page=idx, pages=total)

    def on_chunk(idx, total):
        progress_update(job, stage="AI 识别第 %d/%d 段" % (idx, total),
                        page=idx, pages=total)

    progress_update(job, stage="本地规则试读")

    # ---- 图片：只有视觉模型能读，本地规则无从下手
    if ext in IMAGE_MIME:
        if engine == "local":
            raise importer.ImportError_(
                "图片没有文字层，本地规则读不出来。请把「识别方式」改成 AI 识别，"
                "或改用 txt / docx / xlsx 这类带文字的格式。")
        if not ai_parse.ready():
            raise importer.ImportError_(
                "识别图片需要先开启 AI：到「系统设置 · AI 识别」填好接口地址和密钥，"
                "回来再传这张图。")
        try:
            return ai_parse.extract_image(raw, IMAGE_MIME[ext], cats=cats)
        except ai_parse.AIError as exc:
            raise importer.ImportError_(str(exc))

    if ext in IMAGE_CONVERT:
        raise importer.ImportError_(
            ".%s 这种图片格式多数模型不支持，请先另存为 PNG 或 JPG 再导入" % ext)

    # ---- 带文字的格式：本地优先
    local_err = None
    if engine in ("auto", "local"):
        try:
            res = importer.parse(raw, filename, cats=cats, default_type=default_type)
            if res["items"] or engine == "local":
                return res
        except importer.ScannedPdfError as exc:
            # 图片型 PDF：先试 Windows 自带 OCR（本地、免费、不挑模型，P30），
            # 认不出再退回视觉模型。OCR 认出的字与文字版 PDF 走同一套规则。
            ocr_res = _try_ocr_parse(raw, cats, default_type)
            if ocr_res:
                return ocr_res
            if engine == "local":
                raise
            if not ai_parse.ready():
                raise exc if engine == "auto" else importer.ImportError_(
                    "%s\n（选「只用 AI」时需要先在系统设置里配好接口和密钥）" % exc)
            try:
                return ai_parse.extract_pdf_images(raw, cats=cats, on_page=on_page)
            except ai_parse.AIError as ai_exc:
                raise importer.ImportError_(str(ai_exc))
        except importer.ImportError_ as exc:
            if engine == "local":
                raise
            local_err = exc

    if not ai_parse.ready():
        if local_err:
            raise local_err
        raise importer.ImportError_(
            "本地规则没能从这份材料里拆出题目。可以开 AI 识别试试：到「系统设置 · "
            "AI 识别」填好接口地址和密钥，再回来导入。")

    # 图片型 PDF（每页一张图）：本地抽不出文字，但页面图我们自己能抽出来，
    # 逐页转 PNG 交给视觉模型即可。放在这里统一接住，是因为"只用 AI"这条分支
    # 根本不走上面的本地解析，异常会从 read_file 直接冒出来。
    try:
        text = importer.read_file(raw, filename)
    except importer.ScannedPdfError as exc:
        ocr_res = _try_ocr_parse(raw, cats, default_type)
        if ocr_res:
            return ocr_res
        if engine == "local":
            raise
        try:
            return ai_parse.extract_pdf_images(raw, cats=cats, on_page=on_page)
        except ai_parse.AIError as ai_exc:
            raise importer.ImportError_("%s\n%s" % (exc, ai_exc))
    if not text.strip():
        raise importer.ImportError_("文件内容为空，或无法提取到文字")
    try:
        res = ai_parse.extract_text(text, cats, on_chunk=on_chunk)
    except ai_parse.AIError as exc:
        raise importer.ImportError_(str(exc))
    res["chars"] = len(text)
    return res


def bulk_update(ids, field, value):
    """批量改分类/二级/难度/题型（FR-QB-06）。"""
    allowed = {"cat_id", "sub_id", "diff", "qtype"}
    if field not in allowed:
        return {"ok": False, "errors": ["不支持的字段"]}
    if not ids:
        return {"ok": False, "errors": ["没有选中题目"]}

    conn = connect()
    try:
        ts = now_str()
        if field == "cat_id":
            cat_id = int(value)
            if not conn.execute("SELECT 1 FROM category WHERE id=?", (cat_id,)).fetchone():
                return {"ok": False, "errors": ["分类不存在"]}
            sub = conn.execute(
                "SELECT id FROM subcategory WHERE cat_id=? ORDER BY sort,id LIMIT 1",
                (cat_id,)).fetchone()
            if not sub:
                return {"ok": False,
                        "errors": ["该分类下还没有二级分类，请先在系统设置里添加"]}
            # 换一级分类必须同时换二级，否则会留下"消防 / 空调水系统"这种脏数据
            rows = [(cat_id, sub["id"], ts, int(i)) for i in ids]
            conn.executemany(
                "UPDATE question SET cat_id=?, sub_id=?, updated_at=? WHERE id=?", rows)
        elif field == "sub_id":
            sub = conn.execute("SELECT id, cat_id FROM subcategory WHERE id=?",
                               (int(value),)).fetchone()
            if not sub:
                return {"ok": False, "errors": ["二级分类不存在"]}
            rows = [(sub["cat_id"], sub["id"], ts, int(i)) for i in ids]
            conn.executemany(
                "UPDATE question SET cat_id=?, sub_id=?, updated_at=? WHERE id=?", rows)
        elif field == "diff":
            if value not in DIFFS:
                return {"ok": False, "errors": ["难度只能是 易 / 中 / 难"]}
            rows = [(value, ts, int(i)) for i in ids]
            conn.executemany(
                "UPDATE question SET diff=?, updated_at=? WHERE id=?", rows)
        else:  # qtype
            if value not in TYPES:
                return {"ok": False, "errors": ["题型不存在"]}
            rows = [(value, ts, int(i)) for i in ids]
            conn.executemany(
                "UPDATE question SET qtype=?, updated_at=? WHERE id=?", rows)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "updated": len(ids)}


# ---------------------------------------------------------------- 维护

def category_action(payload):
    action = payload.get("action")
    conn = connect()
    try:
        if action == "add_cat":
            name = (payload.get("name") or "").strip()
            if not name:
                return {"ok": False, "error": "分类名称不能为空"}
            if conn.execute("SELECT 1 FROM category WHERE name=?", (name,)).fetchone():
                return {"ok": False, "error": "分类「%s」已存在" % name}
            sort = conn.execute("SELECT COALESCE(MAX(sort),0)+1 s FROM category").fetchone()["s"]
            cur = conn.execute("INSERT INTO category(name, sort) VALUES(?,?)", (name, sort))
            conn.commit()
            return {"ok": True, "id": cur.lastrowid}

        if action in ("rename_cat", "del_cat", "add_sub", "rename_sub", "del_sub"):
            if action.endswith("_cat"):
                cid = int(payload.get("id") or 0)
                row = conn.execute("SELECT * FROM category WHERE id=?", (cid,)).fetchone()
                if not row:
                    return {"ok": False, "error": "分类不存在"}
                used = conn.execute(
                    "SELECT COUNT(*) c FROM question WHERE cat_id=?", (cid,)).fetchone()["c"]
                if action == "rename_cat":
                    name = (payload.get("name") or "").strip()
                    if not name:
                        return {"ok": False, "error": "分类名称不能为空"}
                    conn.execute("UPDATE category SET name=? WHERE id=?", (name, cid))
                    conn.commit()
                    return {"ok": True}
                if used:
                    return {"ok": False, "error": "该分类下还有 %d 道题，请先移动或删除题目" % used}
                conn.execute("DELETE FROM subcategory WHERE cat_id=?", (cid,))
                conn.execute("DELETE FROM category WHERE id=?", (cid,))
                conn.commit()
                return {"ok": True}

            sid = int(payload.get("id") or 0)
            row = conn.execute("SELECT * FROM subcategory WHERE id=?", (sid,)).fetchone()
            if action == "add_sub":
                cid = int(payload.get("cat_id") or 0)
                if not conn.execute("SELECT 1 FROM category WHERE id=?", (cid,)).fetchone():
                    return {"ok": False, "error": "一级分类不存在"}
                name = (payload.get("name") or "").strip()
                if not name:
                    return {"ok": False, "error": "二级分类名称不能为空"}
                sort = conn.execute(
                    "SELECT COALESCE(MAX(sort),0)+1 s FROM subcategory WHERE cat_id=?", (cid,)
                ).fetchone()["s"]
                cur = conn.execute(
                    "INSERT INTO subcategory(cat_id, name, sort) VALUES(?,?,?)", (cid, name, sort))
                conn.commit()
                return {"ok": True, "id": cur.lastrowid}
            if not row:
                return {"ok": False, "error": "二级分类不存在"}
            if action == "rename_sub":
                name = (payload.get("name") or "").strip()
                if not name:
                    return {"ok": False, "error": "二级分类名称不能为空"}
                conn.execute("UPDATE subcategory SET name=? WHERE id=?", (name, sid))
                conn.commit()
                return {"ok": True}
            used = conn.execute(
                "SELECT COUNT(*) c FROM question WHERE sub_id=?", (sid,)).fetchone()["c"]
            if used:
                return {"ok": False, "error": "该二级分类下还有 %d 道题，不能删除" % used}
            conn.execute("DELETE FROM subcategory WHERE id=?", (sid,))
            conn.commit()
            return {"ok": True}

        return {"ok": False, "error": "未知操作"}
    finally:
        conn.close()


def make_backup(tag="manual"):
    name = "%s_%s.db" % (tag, datetime.now().strftime("%Y%m%d_%H%M%S"))
    target = os.path.join(BACKUP_DIR, name)
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()
    return target


def save_settings(payload):
    conn = connect()
    try:
        for k, v in (payload or {}).items():
            if k in DEFAULT_SETTINGS:
                conn.execute(
                    "INSERT INTO setting(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=?",
                    (k, str(v), str(v)),
                )
        conn.commit()
        return {"ok": True, "settings": all_settings(conn)}
    finally:
        conn.close()


def list_backups():
    items = []
    if os.path.isdir(BACKUP_DIR):
        for name in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if not name.lower().endswith(".db"):
                continue
            path = os.path.join(BACKUP_DIR, name)
            st = os.stat(path)
            items.append({
                "name": name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    return {"ok": True, "dir": BACKUP_DIR, "data_dir": DATA_DIR,
            "db_size": size, "items": items}


def restore_backup(name):
    safe = os.path.basename(name or "")
    path = os.path.join(BACKUP_DIR, safe)
    if not safe or not os.path.isfile(path):
        return {"ok": False, "error": "备份文件不存在"}
    stamp = "before_restore_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".db"
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(os.path.join(BACKUP_DIR, stamp))
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        shutil.copy2(path, DB_PATH)
        return {"ok": True, "safety": stamp}
    except Exception as exc:
        logging.exception("restore")
        return {"ok": False, "error": "恢复失败：%s" % exc}


def maintenance(action):
    conn = connect()
    try:
        if action == "clear_questions":
            conn.execute("DELETE FROM question")
        elif action == "clear_papers":
            conn.execute("DELETE FROM paper_item")
            conn.execute("DELETE FROM paper")
        elif action == "vacuum":
            pass
        else:
            return {"ok": False, "error": "未知操作"}
        conn.commit()
        conn.execute("VACUUM")
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ZJLabExam/1.0"

    def log_message(self, fmt, *args):
        logging.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html", "/ui.html"):
                with open(UI_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            elif path == "/api/ping":
                self._send(200, {"ok": True, "app": "zjlab-exam"})
            elif path == "/api/state":
                conn = connect()
                data = {
                    "categories": categories_tree(conn),
                    "settings": all_settings(conn),
                    "stats": stats(conn),
                    "types": [{"key": t, "name": TYPE_NAMES[t], "points": DIFF_POINTS[t]} for t in TYPES],
                    "diffs": [{"key": d, "name": DIFF_NAMES[d]} for d in DIFFS],
                    "data_dir": DATA_DIR,
                }
                conn.close()
                self._send(200, data)
            elif path == "/api/questions":
                self._send(200, list_questions(params))
            elif path == "/api/questions/export":
                data, n = export_questions(params)
                name = "题库导出_%s.xlsx" % now_str()[:10].replace("-", "")
                self._send(
                    200, data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition":
                     "attachment; filename=\"questions.xlsx\"; filename*=UTF-8''%s"
                     % quote(name),
                     "X-Exported-Count": str(n)},
                )
            elif path.startswith("/api/questions/"):
                qid = int(path.rsplit("/", 1)[-1])
                self._send(200, get_question(qid))
            elif path == "/api/categories":
                self._send(200, {"categories": categories_tree()})
            elif path == "/api/import/logs":
                self._send(200, {"logs": list_import_logs(params.get("limit"))})
            elif path == "/api/import/progress":
                self._send(200, progress_get(params.get("job")))
            elif path == "/api/import/template":
                import importer
                name = "机房运维题库导入模板.xlsx"
                self._send(
                    200, importer.template_xlsx(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    {"Content-Disposition":
                     "attachment; filename=\"template.xlsx\"; filename*=UTF-8''%s"
                     % quote(name)},
                )
            elif path == "/api/backups":
                self._send(200, list_backups())
            elif path == "/api/version":
                st = code_freshness()
                # plain=1 给启动脚本用：脚本里解析 JSON 太麻烦，
                # 只要一个词就能判断"要不要先杀掉旧进程再起新的"。
                if params.get("plain"):
                    self._send(200, "stale" if st["stale"] else "fresh",
                               "text/plain; charset=utf-8")
                else:
                    self._send(200, st)
            elif path == "/api/ai/config":
                import ai_parse
                self._send(200, {"config": ai_parse.public_config()})
            elif path == "/api/ai/review/summary":
                self._send(200, review_summary())
            elif path == "/api/ai/review/progress":
                self._send(200, progress_get(params.get("job")))
            elif path == "/api/ai/review":
                qid = params.get("qid")
                if qid:
                    rec = get_review(int(qid))
                    self._send(200, {"ok": True, "review": rec})
                else:
                    self._send(200, {"reviews": list_reviews(
                        params.get("state") or "pending", params.get("limit"))})
            elif path == "/api/paper/default":
                import paper_engine
                self._send(200, paper_engine.default_config())
            elif path == "/api/papers":
                import paper_engine
                self._send(200, paper_engine.list_papers(params))
            elif path.startswith("/api/papers/"):
                import paper_engine
                self._send(200, paper_engine.get_paper(int(path.rsplit("/", 1)[-1])))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            logging.exception("GET %s", path)
            self._send(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._body()
        try:
            if path == "/api/questions":
                self._send(200, create_question(payload))
            elif path == "/api/questions/bulk":
                action = payload.get("action")
                ids = payload.get("ids") or []
                if action == "delete":
                    self._send(200, delete_questions(ids))
                elif action == "update":
                    self._send(200, bulk_update(ids, payload.get("field"), payload.get("value")))
                else:
                    self._send(400, {"ok": False, "error": "未知操作"})
            elif path == "/api/papers":
                import paper_engine
                self._send(200, paper_engine.build(payload))
            elif path == "/api/papers/precheck":
                import paper_engine
                self._send(200, paper_engine.precheck(payload))
            elif path.startswith("/api/papers/"):
                import paper_engine
                parts = [p for p in path.split("/") if p]
                pid = int(parts[2])
                if len(parts) == 4 and parts[3] == "score":
                    self._send(200, paper_engine.set_item_score(pid, payload.get("item_id"), payload.get("score")))
                elif len(parts) == 4 and parts[3] == "balance":
                    self._send(200, paper_engine.balance(pid, payload.get("target", 100)))
                elif len(parts) == 3:
                    self._send(200, paper_engine.update_paper(pid, payload))
                else:
                    self._send(404, {"ok": False, "error": "not found"})
            elif path == "/api/categories":
                self._send(200, category_action(payload))
            elif path == "/api/backups/restore":
                self._send(200, restore_backup(payload.get("name")))
            elif path == "/api/maintenance":
                self._send(200, maintenance(payload.get("action")))
            elif path == "/api/import/parse":
                import importer
                job = payload.get("job") or ""
                progress_start(job)
                failure = None
                try:
                    res = import_parse(payload)
                    # 重复题判定放在服务端：要跟库内全部题干比，解析器本身不碰库
                    conn = connect()
                    try:
                        pool = [(r["id"], r["stem"]) for r in
                                conn.execute("SELECT id, stem FROM question")]
                    finally:
                        conn.close()
                    dup_n = dupe.mark(res["items"], pool)
                    self._send(200, {"ok": True, "items": res["items"],
                                     "warnings": res.get("warnings") or [],
                                     "chars": res.get("chars") or 0,
                                     "by": res.get("by") or "text",
                                     "chunks": res.get("chunks") or 1,
                                     "failed_chunks": res.get("failed_chunks") or [],
                                     "dup_count": dup_n})
                except importer.ImportError_ as exc:
                    failure = str(exc)
                    self._send(200, {"ok": False, "error": str(exc)})
                except Exception as exc:
                    failure = str(exc)
                    raise
                finally:
                    # 成功失败都要收尾：留一个永远 running 的任务，界面就一直转圈
                    progress_finish(job, failure)
            elif path == "/api/import/commit":
                items = payload.get("items") or []
                conn = connect()
                imported, failed, errors = 0, 0, []
                for it in items:
                    sub = it.get("sub_id")
                    if not sub and it.get("cat_id"):
                        row = conn.execute(
                            "SELECT id FROM subcategory WHERE cat_id=? ORDER BY sort,id LIMIT 1",
                            (int(it["cat_id"]),)).fetchone()
                        sub = row["id"] if row else None
                    res = save_question({
                        "cat_id": it.get("cat_id"), "sub_id": sub,
                        "qtype": it.get("qtype"), "diff": it.get("diff") or "mid",
                        "stem": it.get("stem"), "options": it.get("options") or [],
                        "answer": it.get("answer"), "analysis": it.get("analysis") or "",
                        "tags": it.get("tags") or "",
                    })
                    if res.get("ok"):
                        imported += 1
                    else:
                        failed += 1
                        errors.append({"stem": (it.get("stem") or "")[:20],
                                       "errors": res.get("errors")})
                conn.close()
                # 全部失败也要记一条：否则用户会以为"这次根本没导过"
                if items:
                    log_import(payload.get("filename"), len(items), imported, failed)
                self._send(200, {"ok": True, "imported": imported,
                                 "failed": failed, "errors": errors[:20]})
            elif path == "/api/settings":
                self._send(200, save_settings(payload))
            elif path == "/api/ai/config":
                import ai_parse
                ai_parse.save_config(payload)
                # 一律回脱敏视图：密钥刚填进来，也不能原样吐回页面
                self._send(200, {"ok": True, "config": ai_parse.public_config()})
            elif path == "/api/ai/test":
                import ai_parse
                self._send(200, ai_parse.test_connection())
            elif path == "/api/ai/review":
                job = payload.get("job") or ""
                progress_start(job)
                failure = None
                try:
                    ids = payload.get("ids")
                    if not ids and payload.get("id"):
                        ids = [payload["id"]]
                    res = review_batch(ids, job=job)
                    failure = None if res.get("ok") else res.get("error")
                    self._send(200, res)
                except Exception as exc:
                    failure = str(exc)
                    raise
                finally:
                    progress_finish(job, failure)
            elif path == "/api/ai/review/adopt":
                self._send(200, adopt_reviews(payload.get("ids") or [],
                                              payload.get("fields")))
            elif path == "/api/ai/review/dismiss":
                self._send(200, dismiss_reviews(payload.get("ids") or []))
            elif path == "/api/ai/review/undo":
                self._send(200, undo_reviews(payload.get("ids") or []))
            elif path == "/api/backup":
                self._send(200, {"ok": True, "file": make_backup()})
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            logging.exception("POST %s", path)
            self._send(500, {"ok": False, "error": str(exc)})

    def do_DELETE(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if len(parts) == 3 and parts[:2] == ["api", "questions"]:
                self._send(200, delete_questions([int(parts[2])]))
            elif len(parts) == 3 and parts[:2] == ["api", "papers"]:
                import paper_engine
                self._send(200, paper_engine.delete_paper(int(parts[2])))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except Exception as exc:
            logging.exception("DELETE %s", parsed.path)
            self._send(500, {"ok": False, "error": str(exc)})


def port_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    forced_port = CLI_PORT

    setup_logging()
    init_db()
    daily_backup()

    if forced_port:
        port = forced_port
    else:
        port = DEFAULT_PORT
        for p in range(DEFAULT_PORT, DEFAULT_PORT + PORT_RANGE):
            if not port_busy(p):
                port = p
                break

    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    logging.info("服务启动 %s 版本 %s 代码快照 %s", url, APP_VERSION,
                 time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_BOOT_MTIME)))
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    print(f"机房运维试卷系统已启动（{APP_VERSION}）：{url}")
    print("关闭此窗口即停止服务。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
