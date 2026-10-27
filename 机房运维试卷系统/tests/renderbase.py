#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端渲染测试基类：起独立服务（随机端口 + 独立数据目录）+ 无头浏览器抓真实 DOM。

要点：
  1) 端口用 socket.bind(('127.0.0.1', 0)) 动态取，避免连到开发时残留的旧服务；
  2) 启动后校验 /api/state 里的 data_dir 与本次临时目录一致，防止"看着起了新服务、
     其实连的是正式库"这种静默错配；
  3) 找不到浏览器直接 fail，不 skip —— 渲染断言不允许被静默放过。
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from httpbase import HttpCase  # noqa: E402

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


class RenderCase(HttpCase):
    """带本地服务与无头浏览器的测试基类。子类在 setUp 里自行铺数据。"""

    budget = 6000

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.browser = find_browser()

    def setUp(self):
        if not self.browser:
            self.fail("未找到 Chrome 或 Edge，端到端渲染测试无法执行（不允许跳过）")
        super().setUp()

    #: 导航序号。必须是"类级"计数器，不能放在实例上——每个测试方法都会新建一个
    #: 用例实例，实例级计数器每次都从 1 开始，第二个用例的首次导航 URL 与第一个
    #: 用例完全相同，Chrome 视作同文档导航（只换 hash）不重新加载，页面里的
    #: STATE 还是上一个用例的，断言就会打在旧数据上。
    _nav_seq = 0

    def nav(self, hash_path, wait, timeout=25):
        """导航到 ?t=<自增序号>#<hash_path>，并等到新文档里的条件成立。

        wait 里不用自己带序号，这里会自动加上 location.search 的前置判断。
        """
        cls = type(self)
        cls._nav_seq += 1
        n = cls._nav_seq
        self.chrome.goto("http://127.0.0.1:%d/?t=%d#%s" % (self.port, n, hash_path))
        self.chrome.wait("location.search.indexOf('t=%d')>=0 && (%s)" % (n, wait),
                         timeout=timeout)
        return self.chrome

    def dom(self, hash_path, budget=None):
        url = "http://127.0.0.1:%d/#%s" % (self.port, hash_path)
        out = subprocess.check_output(
            [self.browser, "--headless=new", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--virtual-time-budget=%d" % (budget or self.budget),
             "--dump-dom", url],
            timeout=90, stderr=subprocess.DEVNULL)
        return out.decode("utf-8", "ignore")

    # 内联 <script>/<style> 整块
    _ASSET = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)

    def dom_text(self, hash_path, budget=None):
        """去掉内联 <script>/<style> 之后的真实渲染 DOM。

        整份 dump 直接做字符串计数会被页面自身的 JS 源码干扰——比如 viewImport /
        renderImportResult 里的模板字符串会被当成"已经渲染了一次"。凡是"出现几次/
        有没有这个元素"这类断言，一律用这个方法取文本，否则断言的是源码而不是页面。
        """
        return self._ASSET.sub("", self.dom(hash_path, budget))
