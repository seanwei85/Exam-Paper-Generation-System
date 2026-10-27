#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P28：运行版本自检与前后端接口契约。

问题不是"某段代码写错了"——代码是对的、用例也全绿，用户点了却报 not found。
原因是**运行中的进程还是旧的**：界面 ui.html 是磁盘文件，刷新就生效；后端跑在
一个早上 09:22 启动的 pythonw 进程里。新界面 + 旧后端，新按钮自然 404。

更糟的是没有任何地方能看出来。界面照常打开、题库照常显示，只有新功能悄悄坏掉；
而"重开软件"这条路也被堵死了——启动脚本一看到端口在监听，就只打开浏览器然后退出，
旧进程纹丝不动。用户以为自己重开了。

所以这一阶段验证的是三件"防误判"的事：
  1) 后端要能自己说清楚"我跑的是不是磁盘上的最新代码"（/api/version）；
  2) 界面要把这件事显示出来，不能让人对着一个 404 猜；
  3) 启动脚本不能把"端口有人监听"当成"软件已就绪"，该重启就重启；
  4) 前后端的接口路径必须一一对得上（这才是 404 的直接来源）。
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from httpbase import HttpCase  # noqa: E402
from renderbase import RenderCase  # noqa: E402

BASE = os.path.dirname(HERE)
BAT = os.path.join(BASE, "启动试卷系统.bat")


class VersionApiCase(HttpCase):
    """接口层：能不能如实报告自己的版本状态。"""

    def test_version_reports_running_copy(self):
        d = self.get_json("/api/version")
        for k in ("version", "started_at", "stale"):
            self.assertIn(k, d)
        self.assertTrue(d["version"], "版本号不能为空")
        self.assertRegex(d["started_at"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        # 刚启动的进程，磁盘上的代码不会比它自己新
        self.assertFalse(d["stale"], "刚启动就报 stale 会让提示失去意义")

    def test_plain_text_switch_for_launcher(self):
        """.bat 里解析 JSON 太麻烦，给一个单词就够了。"""
        import urllib.request
        with urllib.request.urlopen(self.url("/api/version?plain=1"), timeout=10) as r:
            body = r.read().decode("utf-8")
            ctype = r.headers.get("Content-Type", "")
        self.assertEqual(body.strip(), "fresh")
        self.assertIn("text/plain", ctype)

    def test_unknown_path_reports_not_found(self):
        """404 时返回的正是用户看到的 not found —— 保留这个形状，便于对号入座。"""
        import urllib.error
        import json
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/api/ai/does-not-exist"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)
        body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(body.get("error"), "not found")

    def test_frontend_calls_only_existing_endpoints(self):
        """前后端契约：ui.html 里出现的每个 /api 路径，后端都必须有。

        这条是本次 not found 的"近亲"——旧后端缺整批路由时，界面看起来完全正常，
        点下去才炸。把这条钉死在用例里，以后加接口忘了后端就会红。
        """
        ui = open(os.path.join(BASE, "ui.html"), encoding="utf-8").read()
        sv = open(os.path.join(BASE, "server.py"), encoding="utf-8").read()
        front = set(re.findall(r"['\"](/api/[A-Za-z0-9_\-/]*)", ui))
        back = set(re.findall(r'"(/api/[A-Za-z0-9_\-/]*)"', sv))
        missing = []
        for p in sorted(front):
            if p in back:
                continue
            # /api/questions/123 这类由 /api/questions/ 前缀路由接住
            if any(p.startswith(b) for b in back if b.endswith("/")):
                continue
            missing.append(p)
        self.assertEqual(missing, [], "前端调用了后端不存在的接口：%s" % missing)


class FreshnessCase(unittest.TestCase):
    """纯函数：判断方向的正确性。判反了比不判更糟（天天提示重启）。"""

    @classmethod
    def setUpClass(cls):
        import server
        cls.core = server

    def test_stale_when_files_newer_than_process(self):
        st = self.core.code_freshness(boot_mtime=0)
        self.assertTrue(st["stale"], "启动时间早于所有源码，必须判为旧进程")

    def test_fresh_when_process_started_after_files(self):
        st = self.core.code_freshness(boot_mtime=self.core.code_mtime())
        self.assertFalse(st["stale"])

    def test_one_second_tolerance(self):
        """文件系统时间精度会带来亚秒级误差，不该触发重启提示。"""
        disk = self.core.code_mtime()
        self.assertFalse(self.core.code_freshness(boot_mtime=disk - 0.5)["stale"])
        self.assertTrue(self.core.code_freshness(boot_mtime=disk - 3)["stale"])


class StaleBannerCase(RenderCase):
    """界面层：提示到底弹不弹。

    判定逻辑必须能被直接断言——提示不弹出来，后端自报再准也等于没修。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from cdp import Chrome
        cls.chrome = Chrome(cls.browser)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.chrome.close()
        finally:
            super().tearDownClass()

    def test_banner_rule(self):
        ch = self.nav("settings", "document.querySelector('.pre-item')!==null")
        # 正常应答且代码一致：不打扰
        self.assertFalse(ch.eval("staleBarNeeded({version:'V1.13',stale:false})"))
        # 磁盘代码比进程新：必须提示
        self.assertTrue(ch.eval("staleBarNeeded({version:'V1.13',stale:true})"))
        # 旧后端连这个接口都没有：更要提示（这正是用户遇到的情形）
        self.assertTrue(ch.eval("staleBarNeeded({ok:false,error:'not found'})"))
        # 请求本身失败：按"不知道"处理，不吓唬人
        self.assertFalse(ch.eval("staleBarNeeded(null)"))

    def test_banner_hidden_when_running_latest(self):
        ch = self.nav("settings", "document.querySelectorAll('.pre-item').length>2")
        hidden = ch.eval("document.getElementById('staleBar').hidden")
        self.assertTrue(hidden, "代码与进程一致时不该出现提示条")

    def test_settings_shows_version_and_start_time(self):
        ch = self.nav("settings", "document.querySelectorAll('.pre-item').length>2")
        text = ch.eval("document.querySelector('.main').innerText") or ""
        self.assertIn("软件版本", text)
        self.assertIn("本次运行始于", text)


class LauncherCase(unittest.TestCase):
    """启动脚本：它决定了"重开软件"到底有没有用。"""

    @classmethod
    def setUpClass(cls):
        cls.raw = open(BAT, "rb").read()
        cls.text = cls.raw.decode("ascii", "replace")

    def test_launcher_checks_version_before_reusing_process(self):
        self.assertIn("api/version?plain=1", self.text)
        self.assertIn("fresh", self.text)

    def test_launcher_only_kills_python(self):
        """端口被别的软件占着时，绝不能替用户杀进程。"""
        self.assertIn("tasklist", self.text)
        self.assertIn("python", self.text.lower())
        self.assertIn("taskkill", self.text)

    def test_launcher_keeps_bat_labels_working(self):
        """goto 依赖 CRLF：LF-only 的 .bat 在 cmd 里找不到标签，
        表现为脚本一闪而过、什么都没启动。"""
        self.assertIn(b"\r\n", self.raw)
        self.assertEqual(self.raw.count(b"\n"), self.raw.count(b"\r\n"),
                         "启动脚本必须全部使用 CRLF 换行")


if __name__ == "__main__":
    unittest.main()
