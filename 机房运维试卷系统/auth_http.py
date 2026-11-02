#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""鉴权与授权的 HTTP 无关逻辑（P35）。server.py 只调用本模块。"""
import auth


def is_on(get_setting):
    return str(get_setting("multi_user") or "0") == "1"


def minutes(get_setting):
    try:
        return max(1, int(get_setting("session_minutes") or 30))
    except (TypeError, ValueError):
        return 30


def public_user(conn, user):
    if not user:
        return None
    if user.get("_single"):
        return {"id": 0, "name": user.get("name"), "is_admin": True,
                "cats": [], "single": True}
    cats = [] if user.get("is_admin") else auth.get_user_cats(conn, user.get("id"))
    return {"id": user.get("id"), "name": user.get("name"),
            "is_admin": bool(user.get("is_admin")), "cats": cats,
            "single": False}


def whoami(conn, user, on):
    data = {"ok": True, "multi_user": on,
            "need_setup": bool(on and auth.count_users(conn) == 0),
            "user": public_user(conn, user), "locks": auth.write_locks()}
    if user and not user.get("_single"):
        data["can_write_any"] = bool(user.get("is_admin")) and not auth.online_regular_exists(
            conn, user.get("id"))
    else:
        data["can_write_any"] = True
    return data


def do_login(conn, payload, minutes_value):
    name = (payload.get("name") or "").strip()
    pwd = payload.get("password") or ""
    if not name:
        return {"ok": False, "error": "请输入用户名"}, None
    user = auth.authenticate(conn, name, pwd)
    if not user:
        return {"ok": False, "error": "用户名或密码不对，或被停用"}, None
    token = auth.create_session(conn, user["id"], minutes_value)
    return {"ok": True, "user": public_user(conn, user)}, token


def list_users(conn, actor):
    if not isinstance(actor, dict) or not actor.get("is_admin"):
        return {"ok": False, "error": "只有管理员能管理用户"}
    users = auth.list_users(conn)
    out = []
    for u in users:
        # password_hash 绝不能出门：前端一个字符都用不到，而它一旦被随手截图、
        # 或被浏览器扩展抓走，等于把所有人的口令离线爆破材料交出去。
        out.append({
            "id": u.get("id"),
            "name": u.get("name"),
            "is_admin": bool(u.get("is_admin")),
            "active": bool(u.get("active")),
            "created_at": u.get("created_at"),
            "cats": auth.get_user_cats(conn, u.get("id")),
        })
    return {"ok": True, "users": out}


def write_users(conn, payload, actor=None):
    """用户管理动作。actor 为 None 表示"首次初始化"（FR-MU-08）。

    首次初始化只允许一件事：库里一个账号都没有时，建第一个管理员。
    放开这个口子的前提是 count_users()==0 —— 否则任何人都能凭空给自己开管理员。
    """
    action = payload.get("action") or ""
    if actor is None:
        if action != "setup_admin":
            return {"ok": False, "error": "系统还没初始化管理员，请先创建管理员"}
        if auth.count_users(conn) > 0:
            return {"ok": False, "error": "管理员已存在，请用已有账号登录"}
        try:
            uid = auth.create_user(conn, payload.get("name"), payload.get("password"), 1, [])
            return {"ok": True, "id": uid, "init": True}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    if not isinstance(actor, dict) or not actor.get("is_admin"):
        return {"ok": False, "error": "只有管理员能管理用户"}
    try:
        if action == "create":
            return {"ok": True, "id": auth.create_user(
                conn, payload.get("name"), payload.get("password"),
                1 if payload.get("is_admin") else 0, payload.get("cats") or [])}
        if action == "delete":
            auth.delete_user(conn, int(payload.get("id")))
            return {"ok": True}
        if action == "password":
            auth.set_password(conn, int(payload.get("id")), payload.get("password"))
            return {"ok": True}
        if action == "cats":
            auth.set_user_cats(conn, int(payload.get("id")), payload.get("cats") or [])
            return {"ok": True}
        return {"ok": False, "error": "未知操作"}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def gate(conn, user, token, cat_ids):
    if not isinstance(user, dict) or not user:
        return {"ok": False, "error": "请先登录后再修改题库", "need_login": True}

    ok, why = auth.check_write(conn, user, token, cat_ids)
    if not ok:
        return {"ok": False, "error": why, "blocked": True}
    return None
