#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P39：设置页按身份裁剪 —— 普通用户看不到管理的那些东西。

起因是用户的一句话："系统设置里面的一些内容普通用户不需要看到，可以隐藏起来。"
之前 P36 接完前端后，普通用户进设置页看到的东西和管理员一模一样：
多人协作开关、单位与默认值、分类维护、AI 接口与密钥、备份记录、清库按钮……
点下去才弹一句"只有管理员能…"。看得见点不动，比看不见更让人困惑。

规则（与后端 server._admin_only / _admin_identity_only 一一对应）：
  * 管理员：照旧，全部可见；
  * 普通用户：只留「我的账号」（姓名 / 身份 / 岗位 / 会话时长 / 退出），
    其余卡片整块不渲染，并且不再请求 /api/backups；
  * 单机模式（AUTH.on=false）：没有账号体系，一切照旧，老用法不受影响。

后端口径同步收紧（不改就无法自洽：隐藏得再好，控制台里一条请求仍能拿到）：
  * GET  /api/backups       普通用户不给看（数据目录、备份文件名）
  * GET  /api/ai/config     普通用户只给 enabled/model/vision_model/ready
  * POST /api/ai/test       会真的用管理员密钥花钱，只给管理员
  * POST /api/ai/vision-probe 同上（发一次视觉请求）
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


def func_body(src, name):
    """取一个顶层函数的函数体（从 function 名到下一个顶层 "}\\n" 为止）。"""
    i = src.find(name)
    if i < 0:
        return ""
    j = src.find("\n}\n", i)
    return src[i:j if j > 0 else len(src)]


class P39ScopeStatic(unittest.TestCase):
    """静态结构：不启服务，先确认接线在。"""

    def setUp(self):
        self.ui = read_ui()

    def test_settings_splits_by_identity(self):
        self.assertIn("function isAdmin(", self.ui, "缺少统一的管理员判定")
        self.assertIn("function settingsForRegular(", self.ui, "缺少普通用户版设置页")
        self.assertIn("if(!isAdmin()) return settingsForRegular();", self.ui,
                      "viewSettings 没有按身份分开渲染")

    def test_regular_settings_has_no_admin_cards(self):
        """普通用户版里不能出现任何管理员卡片的渲染调用。

        断言"函数体里没有"而不是"某处没写"——防止以后有人复制一段卡片进去。
        """
        body = func_body(self.ui, "function settingsForRegular(")
        self.assertTrue(body, "找不到 settingsForRegular 的函数体")
        for bad in ("multiUserCard()", "aiConfigCard()", "catAction(",
                    "doMaintenance(", "restoreBackup(", "doBackup(",
                    "saveSettings(", "data_dir"):
            self.assertNotIn(bad, body, "普通用户版设置页里不该出现 %s" % bad)

    def test_regular_settings_keeps_self_and_logout(self):
        body = func_body(self.ui, "function settingsForRegular(")
        self.assertIn("我的账号", body, "普通用户至少要知道自己是谁")
        self.assertIn("doLogout()", body, "普通用户必须能从这里退出登录")
        self.assertIn("岗位分类", body, "要能看到自己被分配到哪一科")

    def test_users_page_hidden_from_regular_user(self):
        """用户管理页对普通用户同样没意义：进去只会看到一行"只有管理员能…"。"""
        self.assertIn("p.key !== 'users' || (AUTH.on && isAdmin())", self.ui,
                      "用户管理入口应按身份显隐")

    def test_single_mode_still_sees_everything(self):
        """单机模式没有账号，isAdmin() 必须返回 true，否则老用户会丢掉整个设置页。"""
        body = func_body(self.ui, "function isAdmin(")
        self.assertIn("if(!AUTH.on) return true", body, "单机模式应视为管理员")

    def test_position_names_refreshed_after_categories_load(self):
        """岗位名要靠分类树翻译，必须在 loadState 之后重刷一遍。

        首次登录时 applyRoBar / renderWhoBox 跑在 loadState 之前，那一刻
        STATE.cats 还是空的，页面上会留下「岗位：#1 · #2」。截图核对时一眼看见，
        代码读起来却毫无问题——所以固化成用例。
        """
        body = func_body(self.ui, "async function loadState(")
        self.assertIn("applyRoBar()", body, "loadState 之后没有重刷只读条")
        self.assertIn("renderWhoBox()", body, "loadState 之后没有重刷侧栏用户块")


class AuthResetMixin:
    """把临时库恢复成"单机、无账号、无会话"。类共享库，每个用例都要清。"""

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

    def write_ai(self):
        """铺一份带密钥的 ai.json，用来判断"哪些字段被露出去了"。"""
        with open(os.path.join(self.tmp, "ai.json"), "w", encoding="utf-8") as f:
            json.dump({"enabled": True,
                       "base_url": "http://127.0.0.1:9/v1",
                       "api_key": "sk-test-abcd1234",
                       "model": "test-model",
                       "vision_model": "test-vision",
                       "timeout": 30}, f)

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

    def login(self, op, name, pwd="secret123"):
        r = self.call(op, "/api/login", {"name": name, "password": pwd})
        self.assertTrue(r.get("ok"), r)

    def make_regular(self, name="tec", cats=(1,)):
        """管理员建一个普通账号，然后返回它自己的 opener（已登录）。"""
        boss = self.opener()
        self.login(boss, "boss")
        r = self.call(boss, "/api/users",
                      {"action": "create", "name": name,
                       "password": "secret123", "cats": list(cats)})
        self.assertTrue(r.get("ok"), r)
        op = self.opener()
        self.login(op, name)
        return op


class P39ScopeHttp(AuthResetMixin, HttpCase):
    """接口层：隐藏了界面不等于关上了门。"""

    def setUp(self):
        super().setUp()
        self.reset_auth()
        self.write_ai()

    def test_ai_config_brief_for_regular_user(self):
        """接口地址与密钥尾号不给普通用户：这是管理员的配置，不是他的。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        cfg = self.call(op, "/api/ai/config")["config"]
        for secret in ("base_url", "api_key_masked", "timeout", "chunk_chars"):
            self.assertNotIn(secret, cfg, "普通用户不该拿到 %s：%s" % (secret, cfg))
        # 但"能不能用、哪个模型"要留着，导入页的引擎说明靠它
        self.assertIn("ready", cfg)
        self.assertTrue(cfg["ready"], cfg)
        self.assertIn("model", cfg)

    def test_ai_config_full_for_admin(self):
        self.set_multi()
        self.make_admin("boss")
        op = self.opener()
        self.login(op, "boss")
        cfg = self.call(op, "/api/ai/config")["config"]
        self.assertIn("base_url", cfg, "管理员要能看到接口地址")
        self.assertEqual(cfg["api_key_masked"], "****1234", "密钥只露末 4 位")
        self.assertNotIn("sk-test-abcd1234", json.dumps(cfg), "明文密钥绝不能出现在接口里")

    def test_backups_hidden_from_regular_user(self):
        """备份列表里是数据目录与文件名，普通用户拿它没用。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        r = self.call(op, "/api/backups")
        self.assertFalse(r.get("ok"), "普通用户不该看到备份列表：%s" % r)
        self.assertNotIn("data_dir", r, "被拒时也不该带出数据目录")

    def test_admin_can_still_read_backups_while_regular_online(self):
        """查看不是改动：管理员被普通用户挡着时（被动只读）也必须看得见备份列表。

        这就是 _admin_identity_only 与 _admin_only 分开的原因。若这里用了
        _admin_only，管理员一有人在线设置页就会报错。
        """
        self.set_multi()
        self.make_admin("boss")
        self.make_regular()          # 普通用户保持在线
        boss = self.opener()
        self.login(boss, "boss")
        r = self.call(boss, "/api/backups")
        self.assertIn("items", r, "管理员应能查看备份列表：%s" % r)
        self.assertIn("data_dir", r)

    def test_ai_test_blocked_for_regular_user(self):
        """连通测试会拿真实密钥发一次请求，等于花管理员的钱。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        r = self.call(op, "/api/ai/test", {})
        self.assertFalse(r.get("ok"), "普通用户不该能触发接口测试：%s" % r)
        self.assertIn("只有管理员", r.get("error", ""))

    def test_vision_probe_blocked_for_regular_user(self):
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        r = self.call(op, "/api/ai/vision-probe", {})
        self.assertFalse(r.get("ok"), "普通用户不该能触发视觉检测：%s" % r)

    def test_settings_blocked_for_regular_user(self):
        """老规矩再确认一遍：设置页藏起来了，接口也不能被绕过去。"""
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        r = self.call(op, "/api/settings", {"unit_name": "改个名"})
        self.assertFalse(r.get("ok"), r)

    def test_single_mode_unchanged(self):
        """单机模式没有任何账号，必须一切照旧——包括备份列表与完整 AI 配置。"""
        self.assertFalse(self.get_json("/api/whoami").get("multi_user"))
        bk = self.get_json("/api/backups")
        self.assertIn("items", bk, "单机模式应能看备份")
        cfg = self.get_json("/api/ai/config")["config"]
        self.assertIn("base_url", cfg, "单机模式应看到完整 AI 配置")
        self.assertIn("timeout", cfg)


class P39ScopeRender(AuthResetMixin, RenderCase):
    """真实浏览器：普通用户点开"系统设置"，看到的应当只有自己那张卡。"""

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
        self.write_ai()

    def login_ui(self, ch, name):
        ch.eval("document.getElementById('lgName').value=%s;"
                "document.getElementById('lgPwd').value='secret123';doLogin()"
                % json.dumps(name))
        ch.wait("AUTH && AUTH.user && AUTH.user.name===%s" % json.dumps(name), timeout=25)
        ch.wait("document.getElementById('loginRoot').hidden", timeout=25)

    def goto_settings(self, ch, wait):
        ch.eval("location.hash='settings'")
        ch.wait(wait, timeout=25)

    def test_regular_user_sees_only_own_account(self):
        self.set_multi()
        self.make_admin("boss")
        self.make_regular()
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        self.login_ui(ch, "tec")
        # 管理员版才有 sUnit；普通用户版要等自己的卡渲染出来
        self.goto_settings(ch, "!!document.querySelector('#main .pre-item')")
        txt = ch.eval("document.querySelector('#main').textContent")
        self.assertIn("我的账号", txt, "普通用户应看到自己的账号信息")
        self.assertIn("退出登录", txt, "普通用户必须能从这里退出")
        for bad in ("分类维护", "数据与备份", "备份记录", "多人协作", "清空题库", "默认分值"):
            self.assertNotIn(bad, txt, "普通用户不该看到「%s」" % bad)
        # 也不该再为备份信息发请求
        self.assertIsNone(ch.eval("document.getElementById('sUnit')"),
                          "普通用户版不该渲染单位与默认值")

    def test_admin_sees_full_settings(self):
        self.set_multi()
        self.make_admin("boss")
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        self.login_ui(ch, "boss")
        self.goto_settings(ch, "!!document.getElementById('sUnit')")
        txt = ch.eval("document.querySelector('#main').textContent")
        for good in ("多人协作", "单位与默认值", "分类维护", "数据与备份", "清理"):
            self.assertIn(good, txt, "管理员应保留「%s」" % good)
        self.assertNotIn("我的账号", txt, "管理员版不需要「我的账号」这张卡")
        self.assertIn("用户管理", ch.eval("document.getElementById('nav').textContent"))

    def test_position_shows_category_name_not_id(self):
        """首屏的岗位不能显示成「岗位：#1」。

        与静态那条互补：静态只证明调用在，这条证明"刷新后确实翻译成了中文名"。
        """
        self.set_multi()
        self.make_admin("boss")
        self.make_regular(cats=[1])
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        self.login_ui(ch, "tec")
        ch.wait("!!document.getElementById('roText')", timeout=25)
        ro = ch.eval("document.getElementById('roText').textContent")
        self.assertIn("电气", ro, "只读条里的岗位应是分类名：%s" % ro)
        self.assertNotIn("#1", ro, "岗位名退化成了分类 ID：%s" % ro)
        who = ch.eval("document.getElementById('whoBox').textContent")
        self.assertNotIn("#1", who, "侧栏用户块里的岗位也应是分类名：%s" % who)

    def test_nav_hides_user_admin_from_regular(self):
        self.set_multi()
        self.make_admin("boss")
        self.make_regular()
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        self.login_ui(ch, "tec")
        nav = ch.eval("document.getElementById('nav').textContent")
        self.assertNotIn("用户管理", nav, "普通用户不该看到用户管理入口")
        self.assertIn("系统设置", nav, "但设置页要留着——里面有「我的账号」与退出")


if __name__ == "__main__":
    unittest.main()
