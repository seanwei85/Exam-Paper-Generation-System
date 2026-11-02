#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""多用户、岗位授权与分类级写互斥（P35）。

设计依据：doc/多用户管理需求草案_v0.4_定稿版.md

三条核心规则
------------
1. 岗位授权 = 一级分类，授权粒度直接绑 cat_id。
2. 分类级写互斥：同一分类同时只允许一个会话写题库，跨分类可并行；
   组卷是独立通道，不走互斥（FR-MU-13）。
3. 管理员被动只读：有普通用户在线时，管理员可读可组卷但不能写题库。

单用户模式：setting.multi_user 为 0（默认）时本模块不参与，
行为与历史版本一致。
"""

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time

DEFAULT_SESSION_MINUTES = 30
WRITE_IDLE_SECONDS = 600

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32

WRITE = {}
LOCK = threading.Lock()


T_USER_DDL = (
    "CREATE TABLE IF NOT EXISTS user ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "name TEXT NOT NULL UNIQUE,"
    "password_hash TEXT NOT NULL,"
    "is_admin INTEGER NOT NULL DEFAULT 0,"
    "active INTEGER NOT NULL DEFAULT 1,"
    "created_at TEXT NOT NULL DEFAULT '')"
)

T_UC = (
    "CREATE TABLE IF NOT EXISTS user_category ("
    "uid INTEGER NOT NULL,"
    "cat_id INTEGER NOT NULL,"
    "PRIMARY KEY (uid, cat_id))"
)

T_SESS = (
    "CREATE TABLE IF NOT EXISTS session ("
    "token TEXT PRIMARY KEY,"
    "uid INTEGER NOT NULL,"
    "created REAL NOT NULL DEFAULT 0,"
    "last_seen REAL NOT NULL DEFAULT 0)"
)

DDL = [T_USER_DDL, T_UC, T_SESS]


def _scrypt(password, salt, n, r, p, dklen):
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=dklen)


def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = _scrypt((password or "").encode("utf-8"), salt,
                 SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P,
                                      salt.hex(), dk.hex())


def verify_password(password, stored):
    if not stored or not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        want = bytes.fromhex(parts[5])
    except Exception:
        return False
    try:
        got = _scrypt((password or "").encode("utf-8"), salt, n, r, p, len(want))
    except Exception:
        return False
    return hmac.compare_digest(got, want)


def init_schema(conn):
    cur = conn.cursor()
    for sql in DDL:
        cur.execute(sql)
    conn.commit()


def count_users(conn):
    return conn.execute("SELECT COUNT(*) FROM user").fetchone()[0]



USER_COLS = ("id", "name", "password_hash", "is_admin", "active", "created_at")


def _row_dict(row, cols):
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip(cols, row))


def list_users(conn):
    rows = conn.execute(
        "SELECT id, name, password_hash, is_admin, active, created_at"
        " FROM user ORDER BY name"
    ).fetchall()
    return [_row_dict(r, USER_COLS) for r in rows]


def get_user(conn, uid):
    row = conn.execute(
        "SELECT id, name, password_hash, is_admin, active, created_at"
        " FROM user WHERE id=?", (uid,)
    ).fetchone()
    return _row_dict(row, USER_COLS)


def get_user_by_name(conn, name):
    row = conn.execute(
        "SELECT id, name, password_hash, is_admin, active, created_at"
        " FROM user WHERE name=?", (name,)
    ).fetchone()
    return _row_dict(row, USER_COLS)


def create_user(conn, name, password, is_admin=0, cats=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("用户名不能为空")
    if get_user_by_name(conn, name):
        raise ValueError("用户名已存在")
    if not (password or "").strip():
        raise ValueError("密码不能为空")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO user (name, password_hash, is_admin, created_at)"
        " VALUES (?,?,?,?)",
        (name, hash_password(password), 1 if is_admin else 0, now),
    )
    uid = cur.lastrowid
    set_user_cats(conn, uid, cats or [])
    conn.commit()
    return uid


def delete_user(conn, uid):
    conn.execute("DELETE FROM user_category WHERE uid=?", (uid,))
    conn.execute("DELETE FROM user WHERE id=?", (uid,))
    conn.commit()


def set_user_cats(conn, uid, cats):
    conn.execute("DELETE FROM user_category WHERE uid=?", (uid,))
    for c in cats or []:
        conn.execute(
            "INSERT OR IGNORE INTO user_category (uid, cat_id) VALUES (?,?)",
            (uid, int(c)),
        )
    conn.commit()


def get_user_cats(conn, uid):
    rows = conn.execute(
        "SELECT cat_id FROM user_category WHERE uid=?", (uid,)
    ).fetchall()
    return [r[0] for r in rows]


def setting_minutes(conn):
    """会话有效期与"谁算在线"必须同源。

    原先 online_regular_exists 写死 DEFAULT_SESSION_MINUTES，而会话本身
    按设置页那一项过期：用户把"自动退出"改成 60 分钟后，30~60 分钟之间
    管理员会以为没人在线而恢复可写，可对方其实还能操作，两边就错位了。
    """
    try:
        row = conn.execute(
            "SELECT value FROM setting WHERE key='session_minutes'").fetchone()
        if row and str(row[0]).strip():
            return max(1, int(row[0]))
    except Exception:
        pass
    return DEFAULT_SESSION_MINUTES


def create_session(conn, uid, minutes=None):
    now = time.time()
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO session (token, uid, created, last_seen) VALUES (?,?,?,?)",
        (token, uid, now, now),
    )
    conn.commit()
    return token


SESS_COLS = ("token", "uid", "created", "last_seen")


def get_session(conn, token):
    if not token:
        return None
    row = conn.execute(
        "SELECT token, uid, created, last_seen FROM session WHERE token=?",
        (token,),
    ).fetchone()
    return _row_dict(row, SESS_COLS)


def session_user(conn, token, minutes=None):
    s = get_session(conn, token)
    if not s:
        return None
    minutes = setting_minutes(conn) if minutes is None else minutes
    if time.time() - float(s.get("last_seen") or 0) > minutes * 60:
        # 过期即释放写锁：会话没了却还占着分类，别人会一直看到"正被某人编辑"，
        # 而那个人其实早就掉线了，只能等 10 分钟空闲超时才能接手。
        release_all_for_token(token)
        conn.execute("DELETE FROM session WHERE token=?", (token,))
        conn.commit()
        return None
    conn.execute("UPDATE session SET last_seen=? WHERE token=?", (time.time(), token))
    conn.commit()
    u = get_user(conn, s["uid"])
    if not u or not u.get("active"):
        return None
    return u


def authenticate(conn, name, password):
    u = get_user_by_name(conn, (name or "").strip())
    if not u or not u.get("active"):
        return None
    if not verify_password(password, u.get("password_hash")):
        return None
    return u


def logout(conn, token):
    release_all_for_token(token)
    conn.execute("DELETE FROM session WHERE token=?", (token,))
    conn.commit()


def set_password(conn, uid, password):
    if not (password or "").strip():
        raise ValueError("密码不能为空")
    conn.execute(
        "UPDATE user SET password_hash=? WHERE id=?",
        (hash_password(password), uid),
    )
    conn.commit()


def purge_sessions(conn, minutes=None):
    minutes = setting_minutes(conn) if minutes is None else minutes
    cutoff = time.time() - minutes * 60
    conn.execute("DELETE FROM session WHERE last_seen < ?", (cutoff,))
    conn.commit()


def acquire_write(cat_id, uid, name, token):
    now = time.time()
    with LOCK:
        cur = WRITE.get(cat_id)
        if cur and not (cur.get("uid") == uid or now - cur.get("at", 0) > WRITE_IDLE_SECONDS):
            return {"ok": False, "holder": cur.get("name") or ""}
        WRITE[cat_id] = {"token": token, "uid": uid, "name": name, "at": now}
        return {"ok": True, "holder": name}


def release_write(cat_id, token):
    with LOCK:
        cur = WRITE.get(cat_id)
        if cur and (not token or cur.get("token") == token):
            WRITE.pop(cat_id, None)
            return True
    return False


def can_write_category(conn, user, cat_id, token=None):
    if not user:
        return False
    if user.get("is_admin"):
        return not online_regular_exists(conn, user.get("id"))
    return int(cat_id) in get_user_cats(conn, user.get("id"))


def online_regular_exists(conn, exclude_uid=None):
    minutes = setting_minutes(conn)
    cutoff = time.time() - minutes * 60
    rows = conn.execute(
        "SELECT s.uid, u.is_admin FROM session s JOIN user u ON u.id=s.uid"
        " WHERE s.last_seen >= ? AND u.active=1",
        (cutoff,),
    ).fetchall()
    for r in rows:
        if r[1]:
            continue
        if exclude_uid is not None and r[0] == exclude_uid:
            continue
        return True
    return False


def release_all_for_token(token):
    """登出/会话过期时把该会话占用的分类全部释放。"""
    if not token:
        return 0
    freed = 0
    with LOCK:
        for cid in list(WRITE.keys()):
            if WRITE[cid].get("token") == token:
                WRITE.pop(cid, None)
                freed += 1
    return freed


def check_write(conn, user, token, cat_ids):
    """写题库前的统一闸门。cat_ids 是本次要动的分类集合。

    返回 (True, None) 表示放行并把这些分类占住；否则 (False, 原因)。
    """
    if not user:
        return False, "请先登录"
    if user.get("is_admin") and online_regular_exists(conn, user.get("id")):
        return False, "有普通用户在线，管理员当前为只读（可查看、可组卷）"
    if not user.get("is_admin"):
        allowed = set(get_user_cats(conn, user.get("id")))
        for cid in cat_ids or []:
            if int(cid) not in allowed:
                return False, "没有该分类的操作权限"
    for cid in cat_ids or []:
        res = acquire_write(int(cid), user.get("id"), user.get("name"), token)
        if not res.get("ok"):
            return False, "该分类正被 %s 编辑，请稍候" % (res.get("holder") or "其他用户")
    return True, None


def cats_of(conn, qids):
    """按题目 id 反查一级分类，供批量操作的互斥判断。"""
    ids = [int(i) for i in (qids or [])]
    if not ids:
        return []
    rows = conn.execute(
        "SELECT DISTINCT cat_id FROM question WHERE id IN (%s)"
        % ",".join("?" * len(ids)), tuple(ids)).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def write_locks():
    """当前分类占用快照，给界面显示谁在写哪一科。"""
    now = time.time()
    out = []
    with LOCK:
        for cid, rec in WRITE.items():
            idle = now - float(rec.get("at") or 0)
            if idle > WRITE_IDLE_SECONDS:
                continue
            out.append({"cat_id": cid, "name": rec.get("name") or "",
                        "idle_sec": int(idle)})
    return out
