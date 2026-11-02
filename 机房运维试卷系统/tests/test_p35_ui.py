#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P35 前端：登录门 / 用户管理页 / 多人协作开关 / 只读提示。

后端能力再全，界面没有入口也等于没做。这里断言的正是"入口"：
  * 多用户开着且没登录 -> 整页登录卡，侧栏都不该露出来（否则能绕过登录读分类树）；
  * 库里有账号之后，匿名初始化入口必须关掉（FR-MU-08 只放行第一次）；
  * 单机模式 -> 不该出现"用户管理"，也不该弹登录，老用法不能被这次改动打扰；
  * 管理员在有普通用户在线时 -> 顶部要有明确只读提示，而不是点了没反应。

用例注意：HttpCase / RenderCase 的临时库是"按类共享"的，一个用例把
multi_user 打开、建了账号，后面的用例就会接着用。所以每个用例开头必须
reset_auth() 把账号与会话清干净，否则失败原因会指向"上一个用例"，极难定位。
"""

import json
import os
import sqlite3
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


def read_ui():
    with open(os.path.join(os.path.dirname(HERE), "ui.html"), encoding="utf-8") as f:
        return f.read()


class P35UiStatic(unittest.TestCase):
    """静态结构：不启动服务，直接检查页面源码里的接线是否存在。"""

    def setUp(self):
        self.ui = read_ui()

    def test_login_root_and_ro_bar_exist(self):
        self.assertIn('id="loginRoot"', self.ui, "缺少登录页容器")
        self.assertIn('id="roBar"', self.ui, "缺少只读状态条")

    def test_route_handles_users_page(self):
        self.assertIn("key==='users'", self.ui, "路由里没有用户管理页")
        self.assertIn("function viewUsers(", self.ui, "缺少用户管理页渲染函数")

    def test_boot_waits_for_auth(self):
        """启动顺序不能反：先 loadState 再判登录，未登录时会先闪一屏"加载失败"。"""
        body = self.ui[self.ui.find("(async function(){"):]
        i_auth = body.find("await bootAuth()")
        i_state = body.find("await loadState()")
        self.assertTrue(i_auth > 0, "启动流程没有调用 bootAuth")
        self.assertTrue(i_state > i_auth, "应先判登录态再加载题库数据")

    def test_nav_hides_users_in_single_mode(self):
        self.assertIn("p.key !== 'users' || AUTH.on", self.ui,
                      "用户管理应按多人协作开关显隐")

    def test_settings_has_multi_user_card(self):
        self.assertIn("multiUserCard()", self.ui, "设置页没有多人协作卡片")
        self.assertIn("saveMultiUser(", self.ui, "多人协作开关没有保存函数")

    def test_logout_entry_is_always_reachable(self):
        """退出登录必须常驻在侧栏。

        真踩过：退出按钮只写在只读条里，而只读条只在"管理员被普通用户挡住"
        或"普通用户"时出现。管理员正常使用时整条隐藏，界面上找不到任何退出入口，
        于是"我建了账号怎么从来没要我登录"——因为一直在登录态里出不来。
        """
        self.assertIn('id="whoBox"', self.ui, "侧栏缺少当前用户块")
        self.assertIn("function renderWhoBox(", self.ui, "缺少用户块渲染函数")
        # 登录成功后必须渲染一次，否则块是空的
        seg = self.ui[self.ui.find("async function bootAuth("):]
        seg = seg[:seg.find("\n}\n")]
        self.assertIn("renderWhoBox()", seg, "bootAuth 成功后没有渲染当前用户")
        # 未登录/初始化页要收起来，避免登录页背后透出用户名
        for fn in ("function showLogin(", "function showSetupAdmin("):
            body = self.ui[self.ui.find(fn):]
            body = body[:body.find("root.innerHTML")]
            self.assertIn("renderWhoBox()", body, "%s 没有收起用户块" % fn)

    def test_stale_check_ignores_ui_html(self):
        """ui.html 不该算进"代码过期需重启"。

        页面走 no-store 返回，刷新一定拿到新版；把它算进来会让每次改界面
        都弹一条"请双击停止/启动.bat"，而重启对界面毫无作用。
        """
        with open(os.path.join(os.path.dirname(HERE), "server.py"), encoding="utf-8") as f:
            src = f.read()
        start = src.find("CODE_FILES = (")
        block = src[start:src.find(")", start) + 1]
        self.assertNotIn("ui.html", block, "ui.html 不应列入重启检测")
        self.assertIn("server.py", block, "后端文件必须在检测范围内")

    def test_multi_user_fields_have_unique_ids(self):
        """多人协作的三个控件不能和"单位与默认值"撞 id。

        真踩过一次：使用方式用了 id="sMulti"，而"多选默认分值"也叫 sMulti，
        getElementById 拿到的是分数输入框，保存出来的"使用方式"永远是空值，
        开关点了等于没点，界面上却看不出任何异常。
        所以这里查的是"重名"，不是"不许出现"。
        """
        import re
        from collections import Counter
        ids = Counter(re.findall(r'id="([A-Za-z0-9_]+)"', self.ui))
        # eAnswer 是编辑器里按题型分支渲染的三份，天生互斥，不算重名
        dups = {k: v for k, v in ids.items() if v > 1 and k != "eAnswer"}
        self.assertEqual(dups, {}, "界面里有重复 id，getElementById 会取错元素：%s" % dups)
        for good in ("muMode", "muLan", "muMins"):
            self.assertEqual(ids.get(good), 1, "缺少多人协作控件 %s" % good)

    def test_bank_marks_busy_category(self):
        """分类被别人编辑时，列表行要有标记，否则用户只会觉得"保存没反应"。"""
        self.assertIn("lockTag(q.cat_id)", self.ui, "题库列表没有占用标记")
        self.assertIn("lockTag(c.id)", self.ui, "组卷分类配额没有占用标记")


class AuthResetMixin:
    """把临时库恢复成"单机、无账号、无会话"。"""

    def reset_auth(self):
        db = os.path.join(self.tmp, "library.db")
        conn = sqlite3.connect(db, timeout=10)
        try:
            for sql in ("DELETE FROM session", "DELETE FROM user_category",
                        "DELETE FROM user",
                        "DELETE FROM setting WHERE key='multi_user'"):
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def set_multi(self, on=True):
        r = self.post_json("/api/settings", {"multi_user": "1" if on else "0"})
        self.assertTrue(r.get("ok"), r)

    def make_admin(self, name="boss", pwd="secret123"):
        r = self.post_json("/api/users",
                           {"action": "setup_admin", "name": name, "password": pwd})
        self.assertTrue(r.get("ok"), r)

    def opener(self):
        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(self, op, path, obj=None):
        data = None if obj is None else json.dumps(obj).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=data,
                                     headers={"Content-Type": "application/json"})
        with op.open(req, timeout=20) as resp:
            return json.loads(resp.read())


class P35UiHttp(AuthResetMixin, HttpCase):
    """接口层：登录门、初始化只放行一次、设置不能被普通用户改。"""

    def setUp(self):
        super().setUp()
        self.reset_auth()

    def test_state_denied_when_not_logged_in(self):
        self.set_multi()
        d = self.get_json("/api/state")
        self.assertFalse(d.get("ok"), "多用户开启后，未登录不该拿到题库数据")
        self.assertTrue(d.get("need_login"), "应明确告诉前端去登录")

    def test_setup_admin_only_once(self):
        self.set_multi()
        self.make_admin("boss")
        r2 = self.post_json("/api/users",
                            {"action": "setup_admin", "name": "hacker", "password": "secret123"})
        self.assertFalse(r2.get("ok"), "已经有账号了，不能再匿名建管理员")

    def test_settings_cannot_be_changed_by_regular_user(self):
        """普通用户若能改设置，就能把 multi_user 关掉、绕开全部岗位限制。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.opener()
        self.assertTrue(self.call(op, "/api/login",
                                  {"name": "boss", "password": "secret123"}).get("ok"))
        r = self.call(op, "/api/users",
                      {"action": "create", "name": "tec", "password": "secret123", "cats": [1]})
        self.assertTrue(r.get("ok"), r)
        self.call(op, "/api/logout", {})
        self.assertTrue(self.call(op, "/api/login",
                                  {"name": "tec", "password": "secret123"}).get("ok"))
        bad = self.call(op, "/api/settings", {"multi_user": "0"})
        self.assertFalse(bad.get("ok"), "普通用户不该能关掉多人协作：%s" % bad)
        me = self.call(op, "/api/whoami")
        self.assertTrue(me.get("multi_user"), "multi_user 被偷偷改掉了")

    def test_user_list_never_leaks_password_hash(self):
        """列表里带密码哈希等于把口令的离线爆破材料交给任何截图的人。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.opener()
        self.call(op, "/api/login", {"name": "boss", "password": "secret123"})
        r = self.call(op, "/api/users")
        self.assertTrue(r.get("ok"), r)
        for u in r["users"]:
            self.assertNotIn("password_hash", u, "接口不该返回密码哈希：%s" % u)
        self.assertIn("cats", r["users"][0], "岗位授权仍要带上，界面靠它渲染勾选")

    def test_admin_can_toggle_multi_user_back_on_and_setup_stays_closed(self):
        """管理员关掉再打开多人协作后，初始化入口不能重新打开 —— 否则谁都能重建管理员。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.opener()
        self.call(op, "/api/login", {"name": "boss", "password": "secret123"})
        self.assertTrue(self.call(op, "/api/settings", {"multi_user": "0"}).get("ok"))
        self.assertTrue(self.call(op, "/api/settings", {"multi_user": "1"}).get("ok"))
        r = self.post_json("/api/users",
                           {"action": "setup_admin", "name": "hacker", "password": "secret123"})
        self.assertFalse(r.get("ok"))


class P35UiRender(AuthResetMixin, RenderCase):
    """真实浏览器：登录卡、单机模式无感、只读提示。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.chrome = Chrome(cls.browser) if cls.browser else None

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "chrome", None):
            cls.chrome.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.reset_auth()

    def login_admin(self, ch, name="boss", pwd="secret123"):
        ch.eval("document.getElementById('lgName').value=%s;"
                "document.getElementById('lgPwd').value=%s;"
                "doLogin()" % (json.dumps(name), json.dumps(pwd)))
        ch.wait("AUTH && AUTH.user && AUTH.user.is_admin", timeout=20)

    def test_login_page_covers_ui_when_not_logged_in(self):
        self.set_multi()
        self.make_admin("boss")
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        # 登录卡必须顶掉整个系统界面，否则侧栏/导航会把分类结构暴露出来
        self.assertEqual(ch.eval("document.querySelector('.layout').style.display"), "none")
        self.assertIn("登录", ch.eval("document.getElementById('loginRoot').textContent"))

    def test_setup_page_when_no_account(self):
        """多人协作刚打开、一个账号都没有时，应该进"创建管理员"页，而不是登录页。"""
        self.set_multi()
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        txt = ch.eval("document.getElementById('loginRoot').textContent")
        self.assertIn("创建管理员", txt)
        self.assertIsNotNone(ch.eval("document.getElementById('suName')"))

    def test_single_mode_has_no_login_and_no_user_page(self):
        ch = self.nav("dashboard", "typeof STATE!=='undefined' && STATE.cats && STATE.cats.length>0")
        self.assertEqual(ch.eval("AUTH.on"), False)
        self.assertTrue(ch.eval("document.getElementById('loginRoot').hidden"))
        self.assertNotIn("用户管理", ch.eval("document.getElementById('nav').textContent"))

    def test_login_then_users_page_lists_accounts(self):
        self.set_multi()
        self.make_admin("boss")
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        self.login_admin(ch)
        ch.wait("document.getElementById('loginRoot').hidden")
        ch.eval("location.hash='users'")
        # 等真实 DOM 节点，不能等 body.textContent —— 它会把 <script> 里的模板字符串
        # 也算进来，页面还没渲染就"匹配成功"，断言等于没写。
        ch.wait("!!document.querySelector('.ucard-user')", timeout=20)
        txt = ch.eval("document.querySelector('.card:last-child').textContent")
        self.assertIn("boss", txt)

    def test_readonly_bar_shows_for_admin_with_regular_online(self):
        self.set_multi()
        self.make_admin("boss")
        op = self.opener()
        self.call(op, "/api/login", {"name": "boss", "password": "secret123"})
        self.call(op, "/api/users",
                  {"action": "create", "name": "tec", "password": "secret123", "cats": [1]})
        self.call(op, "/api/logout", {})
        # 普通用户在线（会话写进库里）之后，管理员就该是只读
        self.assertTrue(self.call(op, "/api/login",
                                  {"name": "tec", "password": "secret123"}).get("ok"))
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        # 用管理员另一个浏览器会话登录：先登出普通用户所在的 opener 不够，
        # 因为"普通用户在线"看的是库里会话，这里让管理员直接用页面登录
        ch.eval("document.getElementById('lgName').value='boss';"
                "document.getElementById('lgPwd').value='secret123';doLogin()")
        ch.wait("AUTH && AUTH.user && AUTH.user.is_admin", timeout=20)
        ch.eval("pollAuth()")
        ch.wait("!document.getElementById('roBar').hidden", timeout=15)
        self.assertIn("只读", ch.eval("document.getElementById('roText').textContent"))


if __name__ == "__main__":
    unittest.main()
