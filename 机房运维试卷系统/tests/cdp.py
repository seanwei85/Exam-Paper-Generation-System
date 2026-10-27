#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小 Chrome DevTools Protocol 客户端（纯标准库）。

为什么需要：--dump-dom 只能取某一时刻的静态 DOM，点不了按钮、输不进文字。
"删掉一行后剩几行""清空答案后该行会不会标黄"这类行为，只有真点了才知道对不对。

只实现测试需要的最小子集：导航、执行 JS（含返回值）、等页面就绪。
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


class _WS:
    """够用的 WebSocket 客户端：只处理文本帧、分片、ping。"""

    def __init__(self, url, timeout=20):
        assert url.startswith("ws://"), url
        hostport, _, path = url[5:].partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /%s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, hostport, key)
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP 握手失败：连接被关闭")
            buf += chunk
        status = buf.split(b"\r\n")[0]
        if b"101" not in status:
            raise RuntimeError("CDP 握手失败：%s" % status.decode("utf-8", "ignore"))

    def send(self, text):
        data = text.encode("utf-8")
        head = bytearray([0x81])          # FIN + text
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack(">H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack(">Q", n)
        head += mask
        self.sock.sendall(bytes(head) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("CDP 连接已断开")
            out += chunk
        return out

    def recv(self):
        payload = b""
        while True:
            b0, b1 = self._read(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            data = self._read(ln) if ln else b""
            if op == 0x8:
                raise RuntimeError("CDP 服务端关闭连接")
            if op == 0x9:                      # ping，忽略
                continue
            payload += data
            if fin:
                return payload.decode("utf-8", "ignore")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class Chrome:
    """启动一个带远程调试端口的无头 Chrome，并暴露 call/eval/goto。"""

    def __init__(self, binary, extra_args=None):
        self.profile = tempfile.mkdtemp(prefix="zjlab_cdp_")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        args = [
            binary, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--hide-scrollbars",
            "--remote-allow-origins=*",
            "--user-data-dir=" + self.profile,
            "--remote-debugging-port=%d" % self.port,
            "about:blank",
        ] + list(extra_args or [])
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.ws = _WS(self._page_target())
        self._id = 0

    def _http(self, path):
        for _ in range(80):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d%s" % (self.port, path), timeout=1) as r:
                    return json.loads(r.read())
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("无法连接 Chrome 调试端口 %d" % self.port)

    def _page_target(self):
        for _ in range(80):
            targets = self._http("/json/list")
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
            time.sleep(0.15)
        raise RuntimeError("Chrome 没有可用的页面目标")

    def call(self, method, **params):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s 失败：%s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True, userGesture=True)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError("页面脚本异常：%s" % r["exceptionDetails"].get("text") or
                               str(r["exceptionDetails"]))
        return res.get("value")

    def goto(self, url, ready="document.readyState==='complete'"):
        self.call("Page.navigate", url=url)
        self.wait(ready)

    def wait(self, expr, timeout=15.0):
        """轮询直到表达式为真；超时把当前页面状态带出来，便于定位。"""
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.eval(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.12)
        raise RuntimeError("等待超时：%s" % expr)

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
