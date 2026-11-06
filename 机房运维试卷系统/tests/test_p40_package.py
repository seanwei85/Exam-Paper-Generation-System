#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P40：文件夹版 exe（PyInstaller onedir）打包与"退出软件"。

用户："每次点启动 bat 和停止 bat 不如点软件图标快捷，什么时候封装成一套软件。"

打包最容易出事的不是"打不出来"，而是打出来之后数据与程序混在一起：
  * PyInstaller 冻结后 __file__ 指向临时解包目录，若拿它当数据目录，
    每次启动题库都是空的，重启一次丢一次；
  * 把现有 data/（含 ai.json 真实密钥）顺手复制进发布包，等于把密钥送人；
  * 入口直接用 server.py，则 __main__ 与 "import server as core" 会各加载一份
    模块副本，DB_PATH 可能不一致（源码注释里已经踩过这个坑）。

所以这一阶段的验收重点：
  1) 冻结时 BASE 必须是 exe 所在目录，data/ 与 ui.html 外置可升级；
  2) 发布包里绝不能出现 library.db / ai.json；
  3) --windowed 没有控制台窗口，必须有软件内的「退出软件」入口，且只有管理员能点；
  4) 退出要先把响应发回浏览器，再停服务，否则界面只会看到"连接被重置"。
"""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
ROOT = os.path.dirname(APP)
sys.path.insert(0, HERE)
sys.path.insert(0, APP)
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402
from cdp import Chrome  # noqa: E402


def read_app(name):
    with open(os.path.join(APP, name), encoding="utf-8") as f:
        return f.read()


def read_root(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class AuthResetMixin:
    """类共享临时库，每个用例都要清账号/会话/开关。"""

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

    def login(self, op, name, pwd="secret123"):
        r = self.call(op, "/api/login", {"name": name, "password": pwd})
        self.assertTrue(r.get("ok"), r)

    def make_regular(self, name="tec", cats=(1,)):
        boss = self.opener()
        self.login(boss, "boss")
        r = self.call(boss, "/api/users", {"action": "create", "name": name,
                                           "password": "secret123",
                                           "cats": list(cats)})
        self.assertTrue(r.get("ok"), r)
        op = self.opener()
        self.login(op, name)
        return op


class P40PackageStatic(unittest.TestCase):
    """静态结构：不启服务也能确认打包接线没接错。"""

    def setUp(self):
        self.server = read_app("server.py")
        self.ui = read_app("ui.html")
        self.ocr = read_app("ocr_win.py")

    def test_entry_script_imports_server_instead_of_running_it(self):
        """入口必须是薄薄一层 import，不能把 server.py 当 __main__ 直接跑。"""
        entry = read_app("app_main.py")
        self.assertIn("import server", entry)
        self.assertIn("server.main()", entry)
        self.assertNotIn("runpy", entry)

    def test_frozen_base_points_to_exe_directory(self):
        """冻结时数据目录跟着 exe 走，不能跟着 _MEIPASS 临时目录走。"""
        self.assertIn('getattr(sys, "frozen", False)', self.server)
        self.assertIn("sys.executable", self.server)
        self.assertIn('os.path.join(BASE, "data")', self.server)

    def test_ui_and_ocr_script_stay_external(self):
        """ui.html / ocr_png.ps1 外置，升级时替换单个文件即可，不必重打包。"""
        self.assertIn('os.path.join(BASE, "ui.html")', self.server)
        self.assertIn('getattr(sys, "frozen", False)', self.ocr)
        self.assertIn("sys.executable", self.ocr)

    def test_shutdown_route_exists_and_is_admin_only(self):
        self.assertIn('"/api/shutdown"', self.server)
        i = self.server.find('"/api/shutdown"')
        seg = self.server[i:i + 600]
        self.assertIn("_admin_identity_only", seg,
                      "退出服务影响所有在线用户，普通用户不能点")

    def test_shutdown_sends_response_before_stopping(self):
        """先回响应再停服务，否则浏览器只看到连接被重置。"""
        self.assertIn("def request_shutdown", self.server)
        i = self.server.find("def request_shutdown")
        body = self.server[i:i + 900]
        self.assertIn("time.sleep", body)
        self.assertIn("Thread", body)

    def test_build_script_uses_onedir_and_windowed(self):
        b = read_root("tools/build_exe.py")
        self.assertIn("--onedir", b)
        self.assertIn("--windowed", b)
        self.assertIn("logo.ico", b)

    def test_build_script_never_copies_data_or_secrets(self):
        """发布包里绝不能带真实题库与 AI 密钥。"""
        b = read_root("tools/build_exe.py")
        self.assertIn("EXCLUDE", b)
        self.assertIn("ai.json", b)
        self.assertIn("library.db", b)

    def test_build_script_lists_all_dynamic_imports(self):
        """函数内 import 容易被 PyInstaller 漏收，逐个显式声明。"""
        b = read_root("tools/build_exe.py")
        for mod in ("ai_parse", "ai_review", "importer", "paper_engine",
                    "pdftext", "legacy", "ole", "gb1map", "ocr_win",
                    "seed_data", "auth", "auth_http", "dupe"):
            self.assertIn(mod, b, "隐藏导入清单缺少 %s" % mod)

    def test_windowed_entry_guards_stdout_and_startup_errors(self):
        """--windowed 没有控制台，print 和启动异常都要兜住，不能静默闪退。"""
        entry = read_app("app_main.py")
        self.assertIn("sys.stdout", entry)
        self.assertIn("MessageBoxW", entry)

    def test_double_click_does_not_start_two_servers(self):
        """无窗口启动慢，连点两下会起两个进程各写各的库，必须挡住。"""
        entry = read_app("app_main.py")
        self.assertIn("def already_running", entry)
        self.assertIn("/api/version", entry)
        self.assertIn("webbrowser.open", entry)
        self.assertIn('"--port" not in sys.argv', entry,
                      "测试用固定端口启动，守卫不能把它也挡了")

    def test_ui_has_quit_button_with_confirmation(self):
        self.assertIn("quitBtn", self.ui)
        self.assertIn("doQuit", self.ui)
        self.assertIn("/api/shutdown", self.ui)
        self.assertIn("confirm(", self.ui)

    def test_exe_icon_uses_dib_frames(self):
        """PNG 帧 ico 在部分 Explorer 图标缓存下会退化成默认图标。

        exe 的 RT_ICON 必须写标准 DIB(BMP) 帧；BITMAPINFOHEADER 的 biSize
        恒为 40，而 PNG 帧开头是 0x474e5089，可据此逐帧判定。
        """
        import struct
        self.assertIn("logo_exe.ico", read_root("tools/build_exe.py"),
                      "打包必须用 DIB 帧 ico，不能用 PNG 帧的 logo.ico")
        maker = read_app("tools/make_icons.py")
        self.assertIn("def build_ico_dib", maker)
        self.assertIn("def dib_frame", maker)
        ico = os.path.join(APP, "assets", "logo_exe.ico")
        self.assertTrue(os.path.isfile(ico), "缺少 assets/logo_exe.ico")
        with open(ico, "rb") as f:
            data = f.read()
        cnt = struct.unpack_from("<H", data, 4)[0]
        self.assertEqual(cnt, 7, "应含 16/24/32/48/64/128/256 七帧")
        for i in range(cnt):
            off = struct.unpack_from("<I", data, 6 + 16 * i + 12)[0]
            self.assertEqual(struct.unpack_from("<I", data, off)[0], 40,
                             "第 %d 帧不是 DIB" % (i + 1))


class P40ShutdownHttp(AuthResetMixin, HttpCase):
    """接口：普通用户点不动；真退出用独立进程验证，不碰类共享服务。"""

    def setUp(self):
        super().setUp()
        self.reset_auth()

    def test_regular_user_cannot_shutdown(self):
        self.set_multi()
        self.make_admin("boss")
        op = self.make_regular()
        r = self.call(op, "/api/shutdown", {})
        self.assertFalse(r.get("ok"), r)
        self.assertIn("管理员", r.get("error", ""))
        # 服务必须还活着，否则说明守卫没挡住（/api/state 需登录态，用带 cookie 的 opener）
        self.assertIn("data_dir", self.call(op, "/api/state"))

    def test_shutdown_really_stops_a_dedicated_process(self):
        """单独起一个服务进程，点退出后它必须真的结束。"""
        import tempfile
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        tmp = tempfile.mkdtemp(prefix="p40_shutdown_")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(APP, "server.py"),
             "--port", str(port), "--data-dir", tmp, "--no-browser"],
            cwd=APP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = "http://127.0.0.1:%d" % port
            for _ in range(60):
                try:
                    urllib.request.urlopen(url + "/api/state", timeout=0.5).read()
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                self.fail("专用服务没起来")
            req = urllib.request.Request(
                url + "/api/shutdown", data=b"{}",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                r = json.loads(resp.read())
            self.assertTrue(r.get("ok"), r)
            proc.wait(timeout=8)
            self.assertIsNotNone(proc.poll(), "点退出后进程应当结束")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class P40QuitRender(AuthResetMixin, RenderCase):
    """真实浏览器：退出入口按身份显隐，且始终能找到。"""

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

    def _quit_visible(self, ch):
        return ch.eval("!!(document.getElementById('quitBtn') "
                       "&& !document.getElementById('quitBtn').hidden)")

    def test_single_mode_shows_quit_button(self):
        """单机模式没有账号，退出入口必须常驻侧栏。"""
        ch = self.nav("dashboard", "!!document.getElementById('quitBtn')")
        # 等 loadState 真的把数据目录写进侧栏，否则断言打在 syncQuitBtn 跑之前
        ch.wait("document.getElementById('dataPath') && "
                "document.getElementById('dataPath').textContent !== 'data/library.db'",
                timeout=25)
        self.assertTrue(self._quit_visible(ch))

    def test_admin_sees_quit_button(self):
        self.set_multi()
        self.make_admin("boss")
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        ch.eval("document.getElementById('lgName').value='boss';"
                "document.getElementById('lgPwd').value='secret123';doLogin()")
        ch.wait("AUTH && AUTH.user && AUTH.user.name==='boss'", timeout=25)
        self.assertTrue(self._quit_visible(ch))

    def test_regular_user_has_no_quit_button(self):
        self.set_multi()
        self.make_admin("boss")
        self.make_regular()
        ch = self.nav("dashboard",
                      "document.getElementById('loginRoot') && !document.getElementById('loginRoot').hidden")
        ch.eval("document.getElementById('lgName').value='tec';"
                "document.getElementById('lgPwd').value='secret123';doLogin()")
        ch.wait("AUTH && AUTH.user && AUTH.user.name==='tec'", timeout=25)
        self.assertFalse(self._quit_visible(ch),
                         "普通用户不该能关掉所有同事正在用的服务")


if __name__ == "__main__":
    unittest.main()
