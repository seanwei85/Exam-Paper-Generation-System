#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""接口测试基类：起独立服务（随机端口 + 独立数据目录），走真实 HTTP。

与 renderbase.RenderCase 的区别：不需要浏览器，纯接口断言用它更快。
服务启动的三条硬要求与渲染基类一致：
  1) 端口动态分配，避免连到开发时残留的旧服务；
  2) 启动后校验 /api/state 的 data_dir 与本次临时目录一致；
  3) 起不来直接报错，不静默降级。
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import BaseCase, BASE  # noqa: E402


class HttpCase(BaseCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(BASE, "server.py"),
             "--port", str(cls.port), "--data-dir", cls.tmp, "--no-browser"],
            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not cls._wait_ready():
            cls.proc.kill()
            raise RuntimeError("测试服务未能以独立数据目录启动在端口 %d" % cls.port)

    @classmethod
    def _wait_ready(cls):
        for _ in range(80):
            try:
                d = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/state" % cls.port, timeout=0.6).read())
                if os.path.normcase(os.path.abspath(d["data_dir"])) == os.path.normcase(cls.tmp):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()
        super().tearDownClass()

    # ------------------------------------------------------------ HTTP

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get_json(self, path, timeout=20):
        with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
            return json.loads(r.read())

    def post_json(self, path, obj, timeout=40):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return json.loads(exc.read().decode("utf-8", "ignore"))
