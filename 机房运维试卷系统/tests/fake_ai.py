#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地假 AI 服务：模拟 OpenAI 兼容的 /chat/completions。

为什么不用真接口跑测试：真接口要花钱、要联网、返回内容每次不同，断言无从下手，
一条网络抖动就能让整轮测试变红。这里用一个能"编程"的假服务——想让第几次请求
返回 500、想让模型吐一段带 ``` 包裹的 JSON，都能精确安排，测的才是解析与降级逻辑。
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def completion(content, model="fake-chat"):
    """拼一个最小的 chat.completion 响应体。"""
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeAI/1.0"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        state = self.server.state
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_raw": raw.decode("utf-8", "ignore")}
        with state["lock"]:
            state["requests"].append({
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            })
            plan = state["plan"].pop(0) if state["plan"] else None
        if plan is None:
            plan = state["default"]

        status = plan.get("status", 200)
        payload = plan.get("body")
        if payload is None:
            payload = completion(plan.get("content", ""))
        # 慢应答：模拟推理模型/带图请求的真实耗时，用来验证进度上报
        delay = plan.get("delay") or 0
        if delay:
            time.sleep(delay)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeAI:
    """起一个假模型服务。plan 是"逐次应答脚本"，用完后回落到 default。"""

    def __init__(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
        self.httpd.state = {"plan": [], "default": {"content": "好"},
                            "requests": [], "lock": threading.Lock()}
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # ---------------------------------------------------------- 控制
    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/v1" % self.port

    def reset(self):
        st = self.httpd.state
        with st["lock"]:
            st["plan"] = []
            st["requests"] = []
            st["default"] = {"content": "好"}

    def queue(self, **plan):
        """压一条应答：status / content / body 三选一。"""
        with self.httpd.state["lock"]:
            self.httpd.state["plan"].append(plan)

    def queue_json(self, obj, status=200):
        self.queue(status=status, body=completion(json.dumps(obj, ensure_ascii=False)))

    def queue_fail(self, status=500, message="upstream boom"):
        self.queue(status=status, body={"error": {"message": message, "type": "server_error"}})

    def set_default(self, **plan):
        with self.httpd.state["lock"]:
            self.httpd.state["default"] = plan

    @property
    def requests(self):
        with self.httpd.state["lock"]:
            return list(self.httpd.state["requests"])

    @property
    def last(self):
        reqs = self.requests
        return reqs[-1] if reqs else None

    def count(self):
        return len(self.requests)
