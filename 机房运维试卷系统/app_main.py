#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件夹版 exe 的启动入口（PyInstaller 把本文件当 __main__）。

为什么不能直接用 server.py 当入口：
    ai_parse.py / paper_engine.py 里有 "import server as core"。若 server.py
    自己就是 __main__，进程里会同时存在 __main__ 与 server 两份模块副本，
    DB_PATH、进度状态这些模块级全局量各有一份，出现"读 A 库写 B 库"。
    让 server 只作为普通模块被 import 一次，才能保持源码版行为一致。

--windowed 没有控制台窗口：sys.stdout / sys.stderr 是 None，
server.main() 里的 print 会直接抛 AttributeError；任何启动异常也会静默闪退，
用户只看到"双击图标没反应"。这里把两者都兜住。
"""

import json
import os
import sys
import traceback
import urllib.request
import webbrowser


def _null_streams():
    """无控制台时给 print 一个去处。"""
    class _Sink:
        def write(self, s):
            return len(s) if isinstance(s, str) else 0

        def flush(self):
            pass

        def isatty(self):
            return False

    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _Sink())


def _log_startup_error(msg):
    try:
        d = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "startup_error.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n" + "=" * 60 + "\n")
    except Exception:
        pass


def _alert(msg):
    """出错必须让用户看见，而不是静默退出。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, msg[-1200:], "机房运维试卷系统 · 启动失败", 0x10)
    except Exception:
        pass


def already_running():
    """双击两次图标不能起两个服务：两个端口、两个进程、各写各的库。

    --windowed 启动要一两秒，没看到窗口的人很容易再点一下。发现本机已有
    同一套服务就直接把它的页面打开，本次进程安静退出。只认 /api/version
    的自报身份，端口被别的软件占着不算。
    """
    for port in range(8765, 8785):
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/version" % port, timeout=0.15) as r:
                v = json.loads(r.read())
            if isinstance(v, dict) and str(v.get("version", "")).startswith("V"):
                webbrowser.open("http://127.0.0.1:%d/" % port)
                return True
        except Exception:
            pass
    return False


def run():
    _null_streams()
    if (getattr(sys, "frozen", False)
            and "--port" not in sys.argv
            and already_running()):
        return
    try:
        import server
        server.main()
    except SystemExit:
        raise
    except BaseException:
        detail = traceback.format_exc()
        _log_startup_error(detail)
        _alert("软件启动失败。详细原因已写入 data/startup_error.log\n\n" + detail)
        sys.exit(1)


if __name__ == "__main__":
    run()
