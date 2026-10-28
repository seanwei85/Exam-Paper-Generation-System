# -*- coding: utf-8 -*-
"""Windows 自带 OCR 通道（P30，FR-IM-28）。

设计动机：图片型 PDF 此前只有"视觉模型"一条路——模型不支持看图就全卡死。
其实"把像素认成文字"这一步 Windows 10/11 系统自带（Windows.Media.Ocr，
装了中文语言包就能用），认出来的文字交给现有的本地规则解析即可：
零联网、零安装、不挑模型。视觉模型保留为兜底，负责 OCR 认不动的页面。

实现方式：本模块只负责起进程、传图、收文本；真正的识别在 ocr_png.ps1 里
（WinRT 异步 API 在批处理脚本里写最省事）。OCR 是"尽力而为"的通道：
任何一环出问题都抛 OcrError，由调用方决定退回视觉模型，绝不阻塞导入。
"""
import os
import re
import subprocess
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
# 环境变量可换脚本（测试注入假 OCR 用）；不设就用随程序发布的这份
PS_SCRIPT = os.environ.get("ZJLAB_OCR_PS1") or os.path.join(_HERE, "ocr_png.ps1")
TIMEOUT = 60          # 单页 OCR 上限（秒）；正常几百毫秒到两三秒
_cache = {"ready": None}


# Windows OCR 会在汉字之间插空格（"机 房 温 度"），中文之间一律去掉。
# 半角字符之间的空格保留——"A. 10.8V" 不能被粘死。
_CJK = r"\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"
# 两侧都是半角字符才保留空格（"A. 10.8V"），否则视为 OCR 假空格
_SPURIOUS = re.compile(r"(?<=[%s])[ \t]+(?=.)|(?<=.)[ \t]+(?=[%s])" % (_CJK, _CJK))


def _despace(text):
    """去掉 CJK 字符之间的假空格（含全角标点），标点也要算进"字"里。"""
    return "\n".join(_SPURIOUS.sub("", ln) for ln in text.splitlines())


class OcrError(Exception):
    """OCR 通道自己的失败。与导入失败、AI 失败分开，便于分岔提示。"""


def _run(args):
    """起一次 powershell。绝对路径优先，找不到再靠 PATH。"""
    exe = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                       "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(exe):
        exe = "powershell.exe"
    try:
        return subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", PS_SCRIPT] + list(args),
            capture_output=True, timeout=TIMEOUT)
    except FileNotFoundError:
        raise OcrError("这台电脑上找不到 PowerShell，OCR 通道不可用")
    except subprocess.TimeoutExpired:
        raise OcrError("OCR 超时（超过 %d 秒），这页可能太大或太乱" % TIMEOUT)


def engine_ready():
    """Windows OCR 中文引擎是否可用。结果缓存，进程内只探测一次。"""
    if _cache["ready"] is None:
        try:
            p = _run(["-Check"])
            out = (p.stdout or b"").decode("utf-8", "replace").strip()
            ok = p.returncode == 0 and out and "NOOCR" not in out
            _cache["ready"] = out.splitlines()[0].strip() if ok else ""
        except Exception:
            _cache["ready"] = ""
    return _cache["ready"]


def ocr_png_bytes(png):
    """PNG 字节 -> 识别文本（一行一条）。失败抛 OcrError，调用方走兜底。"""
    if not os.path.exists(PS_SCRIPT):
        raise OcrError("找不到 OCR 脚本 ocr_png.ps1，程序可能没拷全")
    fd, tmp = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        p = _run(["-Path", tmp])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0 or "NOOCR" in out:
        err = (p.stderr or b"").decode("utf-8", "replace").strip()
        if "NOOCR" in out or "NOOCR" in err:
            raise OcrError(hint_when_missing())
        raise OcrError("OCR 运行失败：%s" % (err or out or ("退出码 %d" % p.returncode))[:200])
    # 假空格必须在出口就清掉：下游按"行首题号 + 选项字母"拆题，
    # 汉字之间夹着空格时题号和选项都认不出来（"机 房 温 度" 拆不出题）。
    return _despace(out)


def hint_when_missing():
    return ("本机 Windows 没有可用的中文 OCR：请在 系统设置 → 时间和语言 → 语言和区域 "
            "里添加「中文（简体，中国）」，图片型 PDF 将退回视觉模型识别。")
