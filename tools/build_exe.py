#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包「文件夹版 exe」（PyInstaller onedir + --windowed）。

用法（用系统 Python 3.13，已装 pyinstaller）：
    python tools/build_exe.py

产物：output/MolabExam/  —— 整个文件夹拷到别的电脑即可用。

三条硬规矩：
  1) 数据与程序分离。题库 library.db、AI 密钥 ai.json、备份 backup/ 一律
     不进发布包；包里只有一个空的 data/ 目录，首次启动自动建库。
  2) ui.html / ocr_png.ps1 外置在 exe 旁边，不塞进 _internal。
     以后升级界面或 OCR 脚本，替换单个文件即可，不必重新打包。
  3) 打完包逐文件扫一遍，发现 .db / ai.json 直接判失败——不靠"应该没复制"。
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "机房运维试卷系统")
OUT = os.path.join(ROOT, "output", "MolabExam")
BUILD = os.path.join(ROOT, "output", "_build_exe")

# 复制外部资源时的排除名单。宁可少拷，绝不能把真实数据带出去。
EXCLUDE_DIRS = {"data", "backup", "tests", "__pycache__", "tools", "_shot",
                "dist", "build", ".git", ".workbuddy"}
EXCLUDE_FILES = {"ai.json", "library.db", "app.log", "startup_error.log"}
EXCLUDE_EXTS = {".db", ".db-journal", ".pyc", ".log", ".spec"}

# 函数内 import 与 "import server as core" 这类间接引用，PyInstaller 静态
# 分析可能漏收，逐个显式声明。少一个就会在用户机器上报 ModuleNotFoundError。
HIDDEN = [
    "server", "auth", "auth_http", "dupe", "importer", "ai_parse",
    "ai_review", "paper_engine", "pdftext", "legacy", "ole", "gb1map",
    "ocr_win", "seed_data",
]

# 随 exe 外置、可单独升级的文件
EXTERNAL = ["ui.html", "ocr_png.ps1"]

README = """机房运维试卷系统 · 文件夹版
================================

【怎么用】
双击「机房运维试卷系统.exe」。软件没有黑色控制台窗口，稍等两三秒会自动
打开浏览器。用完在网页左侧栏最下方点「退出软件」。

不要只把 exe 单独拷走：整个文件夹（含 _internal 目录、ui.html）要一起拷。

【数据在哪】
题库、账号、设置、试卷、AI 密钥都在本文件夹的 data\\ 目录里。
首次启动会自动建一个带示例题的新库。

【从旧电脑搬数据】
1. 两边都先退出软件；
2. 把旧电脑上 data\\ 里的 library.db、ai.json 复制到新电脑的 data\\；
3. 想连备份一起搬，把 data\\backup\\ 整个复制过来。
注意：ai.json 里是你的 AI 接口密钥，别把整个文件夹发给无关的人。

【以后软件升级怎么办】
只替换程序文件，data\\ 目录一个字都不要动：
  * 常规升级：覆盖「机房运维试卷系统.exe」和整个 _internal\\ 目录；
  * 只改了界面：只覆盖 ui.html 就行，连 exe 都不用换。
升级前顺手把 data\\library.db 复制一份留底，更稳妥。

【多人使用】
管理员进「系统设置」勾选「允许局域网访问」，重启软件后，同网段同事
在浏览器输入 http://本机IP:8765 即可（无需在他们电脑上安装任何东西）。

【出问题了】
启动失败会弹窗提示，详细原因写在 data\\startup_error.log。
运行日志在 data\\app.log。
"""


def fail(msg):
    raise SystemExit("打包失败：" + msg)


def check_source():
    for f in ["app_main.py", "server.py", "ui.html"] + EXTERNAL:
        if not os.path.isfile(os.path.join(APP, f)):
            fail("缺少源文件 " + f)
    ico = os.path.join(APP, "assets", "logo.ico")
    if not os.path.isfile(ico):
        fail("缺少图标 " + ico)
    return ico


def clean():
    for d in (OUT, BUILD):
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    os.makedirs(BUILD, exist_ok=True)


def build(ico):
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean", "--onedir", "--windowed",
           "--name", "机房运维试卷系统",
           "--icon", ico,
           "--distpath", os.path.dirname(OUT),
           "--workpath", BUILD,
           "--specpath", BUILD]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    cmd.append(os.path.join(APP, "app_main.py"))
    print("执行：", " ".join(cmd[:12]), "...")
    r = subprocess.run(cmd, cwd=APP)
    if r.returncode != 0:
        fail("PyInstaller 退出码 %d" % r.returncode)
    # --name 决定的是子目录名；统一成 OUT，避免中文名/空格带来的路径麻烦
    produced = os.path.join(os.path.dirname(OUT), "机房运维试卷系统")
    if os.path.isdir(produced):
        if os.path.isdir(OUT):
            shutil.rmtree(OUT)
        os.rename(produced, OUT)
    if not os.path.isdir(OUT):
        fail("没找到产物目录 " + OUT)


def copy_external():
    for name in EXTERNAL:
        shutil.copy2(os.path.join(APP, name), os.path.join(OUT, name))
    # 只放空目录，绝不复制现有 data 内容
    os.makedirs(os.path.join(OUT, "data"), exist_ok=True)
    with open(os.path.join(OUT, "使用说明.txt"), "w", encoding="utf-8-sig") as f:
        f.write(README)


def verify():
    exe = os.path.join(OUT, "机房运维试卷系统.exe")
    if not os.path.isfile(exe):
        fail("产物里没有 exe")
    for name in EXTERNAL:
        if not os.path.isfile(os.path.join(OUT, name)):
            fail("外置资源没拷进去：" + name)
    leaks = []
    for base, dirs, files in os.walk(OUT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            low = fn.lower()
            if low in ("ai.json", "library.db") or low.endswith(".db"):
                leaks.append(os.path.relpath(os.path.join(base, fn), OUT))
    if leaks:
        fail("发布包里混入了数据文件（含真实密钥或题库）：\n  " + "\n  ".join(leaks))
    print("\n校验通过：发布包内无 ai.json / library.db / .db 文件。")


def main():
    ico = check_source()
    clean()
    build(ico)
    copy_external()
    verify()
    size = sum(os.path.getsize(os.path.join(b, f))
               for b, _, fs in os.walk(OUT) for f in fs)
    print("\n产物：%s" % OUT)
    print("大小：%.1f MB" % (size / 1024 / 1024))
    print("把整个 MolabExam 文件夹拷到别的电脑，双击 exe 即可。")


if __name__ == "__main__":
    main()
