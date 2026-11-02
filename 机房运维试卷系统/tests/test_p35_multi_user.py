"""P35: 多用户 / 岗位授权 / 分类级互斥（FR-MU-01~13）。"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import BaseCase
from httpbase import HttpCase

class P35Account(BaseCase):
    """账号与授权（FR-MU-01/02/04）。"""

    def setUp(self):
        super().setUp()
        import auth
        self.auth = auth
        auth.WRITE.clear()
        self.conn = self.core.connect()
        self.conn.execute("DELETE FROM user_category")
        self.conn.execute("DELETE FROM session")
        self.conn.execute("DELETE FROM user")
        self.conn.commit()

    def tearDown(self):
        self.auth.WRITE.clear()
        self.conn.close()
        super().tearDown()

    def test_password_roundtrip(self):
        """密码哈希自描述，正确密码过、错密码不过（FR-MU-01）。"""
        h = self.auth.hash_password("abc123")
        self.assertTrue(h.startswith("scrypt$"))
        self.assertTrue(self.auth.verify_password("abc123", h))
        self.assertFalse(self.auth.verify_password("abc124", h))

    def test_grant_and_session(self):
        """建号勾分类、登录换 token、token 反查用户（FR-MU-02）。"""
        conn = self.conn
        uid = self.auth.create_user(conn, "zhang", "pw", 0, [1, 2])
        self.assertGreater(uid, 0)
        self.assertEqual(sorted(self.auth.get_user_cats(conn, uid)), [1, 2])
        u = self.auth.authenticate(conn, "zhang", "pw")
        self.assertTrue(u is not None)
        self.assertEqual(u["name"], "zhang")
        tok = self.auth.create_session(conn, uid, 30)
        self.assertTrue(tok)
        who = self.auth.session_user(conn, tok, 30)
        self.assertEqual(who["name"], "zhang")
        self.assertTrue(self.auth.session_user(conn, "bad-token", 30) is None)

    def test_write_mutex_same_category(self):
        """同一分类同时只允许一个会话写（FR-MU-07）。"""
        conn = self.conn
        a = self.auth.create_user(conn, "a", "p", 0, [1])
        b = self.auth.create_user(conn, "b", "p", 0, [1])
        tok_a = self.auth.create_session(conn, a, 30)
        tok_b = self.auth.create_session(conn, b, 30)
        ua = self.auth.get_user(conn, a)
        ub = self.auth.get_user(conn, b)
        ok1, why1 = self.auth.check_write(conn, ua, tok_a, [1])
        self.assertTrue(ok1)
        ok2, why2 = self.auth.check_write(conn, ub, tok_b, [1])
        self.assertFalse(ok2)
        self.assertIn("编辑", why2)

    def test_parallel_across_categories(self):
        """跨分类可以并行写：电气和暖通互不阻塞（FR-MU-07）。"""
        conn = self.conn
        a = self.auth.create_user(conn, "a2", "p", 0, [1])
        b = self.auth.create_user(conn, "b2", "p", 0, [2])
        ta = self.auth.create_session(conn, a, 30)
        tb = self.auth.create_session(conn, b, 30)
        ua = self.auth.get_user(conn, a)
        ub = self.auth.get_user(conn, b)
        ok1, _ = self.auth.check_write(conn, ua, ta, [1])
        ok2, _ = self.auth.check_write(conn, ub, tb, [2])
        self.assertTrue(ok1)
        self.assertTrue(ok2)

    def test_admin_readonly_when_regular_online(self):
        """有普通用户在线时，管理员不能写题库（FR-MU-12）。"""
        conn = self.conn
        adm = self.auth.create_user(conn, "boss", "p", 1, [])
        reg = self.auth.create_user(conn, "worker", "p", 0, [1])
        t_adm = self.auth.create_session(conn, adm, 30)
        t_reg = self.auth.create_session(conn, reg, 30)
        self.auth.session_user(conn, t_reg, 30)
        u_adm = self.auth.get_user(conn, adm)
        ok, why = self.auth.check_write(conn, u_adm, t_adm, [1])
        self.assertFalse(ok)
        self.assertIn("只读", why)

    def test_admin_readonly_follows_session_minutes_setting(self):
        """管理员什么时候解锁，要跟设置页那一项同源，不能写死 30 分钟。

        原先 online_regular_exists 用常量 30，而会话本身按设置过期。
        用户把"自动退出"改成 5 分钟后，普通用户 8 分钟没动就该算离线、
        管理员恢复可写；若仍按 30 分钟算，就会一直卡在只读，
        同事明明已经走了，管理员却改不了题。
        """
        conn = self.conn
        adm = self.auth.create_user(conn, "boss2", "p", 1, [])
        reg = self.auth.create_user(conn, "worker2", "p", 0, [1])
        t_adm = self.auth.create_session(conn, adm, 5)
        t_reg = self.auth.create_session(conn, reg, 5)
        # 普通用户最后活动时间拨到 8 分钟前
        conn.execute("UPDATE session SET last_seen=? WHERE token=?",
                     (time.time() - 8 * 60, t_reg))
        conn.execute("INSERT OR REPLACE INTO setting(key,value)"
                     " VALUES('session_minutes','5')")
        conn.commit()
        try:
            self.assertFalse(self.auth.online_regular_exists(conn),
                             "设成 5 分钟后，8 分钟没动的人不该再算在线")
            u_adm = self.auth.get_user(conn, adm)
            ok, why = self.auth.check_write(conn, u_adm, t_adm, [1])
            self.assertTrue(ok, why)

            # 同一个 8 分钟，把设置改回 30 分钟就该重新算"在线"
            conn.execute("UPDATE setting SET value='30' WHERE key='session_minutes'")
            conn.commit()
            self.assertTrue(self.auth.online_regular_exists(conn))
            ok2, why2 = self.auth.check_write(conn, u_adm, t_adm, [1])
            self.assertFalse(ok2)
            self.assertIn("只读", why2)
        finally:
            conn.execute("INSERT OR REPLACE INTO setting(key,value)"
                         " VALUES('session_minutes','30')")
            conn.commit()

    def test_write_denied_outside_grant(self):
        """只授权电气，改暖通要在鉴权层就被拒（FR-MU-05）。"""
        conn = self.conn
        u = self.auth.create_user(conn, "dz", "p", 0, [1])
        tk = self.auth.create_session(conn, u, 30)
        user = self.auth.get_user(conn, u)
        ok, why = self.auth.check_write(conn, user, tk, [2])
        self.assertFalse(ok)
        self.assertIn("权限", why)

    def test_expired_session_releases_lock(self):
        """会话过期要顺手释放分类占用，不能让别人等一个已掉线的人。"""
        conn = self.conn
        u = self.auth.create_user(conn, "old", "p", 0, [1])
        tk = self.auth.create_session(conn, u, 30)
        user = self.auth.get_user(conn, u)
        ok, _ = self.auth.check_write(conn, user, tk, [1])
        self.assertTrue(ok)
        self.assertTrue(self.auth.write_locks())
        conn.execute("UPDATE session SET last_seen=0 WHERE token=?", (tk,))
        conn.commit()
        self.assertTrue(self.auth.session_user(conn, tk, 30) is None)
        self.assertEqual(self.auth.write_locks(), [])


class P35PaperScope(BaseCase):
    """组卷范围：普通用户只能组本岗位的卷（FR-MU-06）。"""

    def test_precheck_rejects_other_category(self):
        """配额里点了未授权分类，预检要直接报错讲清原因。"""
        self.seed_electrical_only()
        cfg = self.base_cfg(mode="quota")
        cfg["quotas"] = {"1": 50, "2": 50}
        cfg["allowed_cats"] = [1]
        res = self.pe.precheck(cfg)
        self.assertFalse(res["ok"])
        joined = " ".join(res["errors"])
        self.assertIn("不在你的岗位授权内", joined)

    def seed_electrical_only(self):
        """只给电气分类铺题，够组一份小卷。"""
        return self.seed(per_cell=3, cats=["电气"])

    def test_build_only_picks_granted(self):
        """真正组卷时，抽到的题必须全部落在授权分类内（FR-MU-06）。"""
        self.seed()
        cfg = self.base_cfg()
        cfg["rows"] = [
            {"qtype": "single", "enabled": True, "count": 2, "score": 1},
            {"qtype": "judge", "enabled": True, "count": 2, "score": 1},
        ]
        cfg["diff_target"] = None
        cfg["allowed_cats"] = [1]
        res = self.pe.build(cfg)
        self.assertTrue(res.get("ok"), res.get("errors"))
        pid = res["id"]
        conn = self.core.connect()
        rows = conn.execute(
            "SELECT q.cat_id FROM paper_item i JOIN question q ON q.id=i.qid "
            "WHERE i.paper_id=?", (pid,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 4)
        for r in rows:
            self.assertEqual(int(r["cat_id"]), 1)


class P35Http(HttpCase):
    """HTTP 接线：只读要登录、首次初始化、管理员专属操作（FR-MU-04/08/10）。"""

    def set_multi(self, on=True):
        """直接改库里的开关，避免依赖设置页。"""
        conn = self.core.connect()
        conn.execute("INSERT OR REPLACE INTO setting(key,value) VALUES('multi_user',?)",
                     ("1" if on else "0",))
        conn.commit()
        conn.close()

    def test_state_requires_login(self):
        """开了多用户，未登录读分类树要被拒（FR-MU-04）。"""
        self.set_multi(True)
        res = self.get_json("/api/state")
        self.assertFalse(res.get("ok", False))
        self.assertTrue(res.get("need_login"))

    def test_setup_admin_once(self):
        """库里没有账号时可以建第一个管理员，建完就不许匿名再来（FR-MU-08）。"""
        self.set_multi(True)
        r1 = self.post_json("/api/users", {"action": "setup_admin", "name": "root", "password": "pw"})
        self.assertTrue(r1.get("ok"), r1)
        r2 = self.post_json("/api/users", {"action": "setup_admin", "name": "x", "password": "pw"})
        self.assertFalse(r2.get("ok"))
        self.assertIn("已存在", r2.get("error", ""))

    def login(self, name, pwd):
        req = urllib.request if 0 else None
        return None
