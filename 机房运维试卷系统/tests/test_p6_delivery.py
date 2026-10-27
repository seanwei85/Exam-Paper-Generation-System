#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P6 交付测试：启动器、数据文件、重启后数据一致性、交付物齐全。"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import BaseCase, BASE


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_state(port, data_dir, tries=80):
    for _ in range(tries):
        try:
            d = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/api/state" % port, timeout=0.6).read())
            if os.path.normcase(os.path.abspath(d["data_dir"])) == os.path.normcase(data_dir):
                return d
        except Exception:
            pass
        time.sleep(0.2)
    return None


def start_server(port, data_dir):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE, "server.py"),
         "--port", str(port), "--data-dir", data_dir, "--no-browser"],
        cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, wait_state(port, data_dir)


class P6Deliverables(BaseCase):
    def test_launcher_files(self):
        start = os.path.join(BASE, "启动试卷系统.bat")
        stop = os.path.join(BASE, "停止试卷系统.bat")
        self.assertTrue(os.path.exists(start), "缺少启动脚本")
        self.assertTrue(os.path.exists(stop), "缺少停止脚本")
        stext = open(start, encoding="utf-8", errors="ignore").read().lower()
        self.assertIn("pythonw", stext, "启动脚本应优先用 pythonw 无窗口运行")
        self.assertIn("server.py", stext)
        ttext = open(stop, encoding="utf-8", errors="ignore").read().lower()
        self.assertIn("taskkill", ttext)

    def test_stop_script_covers_ports(self):
        text = open(os.path.join(BASE, "停止试卷系统.bat"), encoding="utf-8", errors="ignore").read()
        self.assertIn("taskkill", text.lower())
        self.assertIn("8765", text)

    def test_source_files_present(self):
        for name in ("server.py", "ui.html", "paper_engine.py", "importer.py", "seed_data.py"):
            self.assertTrue(os.path.exists(os.path.join(BASE, name)), "缺少 %s" % name)

    @staticmethod
    def latest(doc, stem):
        """doc 目录里该文档的最高版本号，没有则 None。"""
        best = None
        for name in os.listdir(doc):
            m = re.match(r"^%s_V(\d+)\.(\d+)\.(?:docx|md)$" % re.escape(stem), name)
            if m:
                v = (int(m.group(1)), int(m.group(2)))
                best = v if best is None or v > best else best
        return best

    def test_docs_present(self):
        """三份文档都要在，且必须停在最新版本上——docx 与 md 同步。

        按目录里的最高版本号判断，不写死具体数字：升版时文档改名不该让这条
        用例失灵；但设了版本底线，防止有人把文档退回旧版还宣称"文档齐全"。
        """
        doc = os.path.join(os.path.dirname(BASE), "doc")
        for stem, floor in (("产品需求文档", (1, 10)),
                            ("开发计划", (1, 4)),
                            ("使用说明", (1, 3))):
            v = self.latest(doc, stem)
            self.assertIsNotNone(v, "doc 目录里没有 %s" % stem)
            self.assertGreaterEqual(
                v, floor, "%s 版本停在 V%d.%d，低于约定底线 V%d.%d" % ((stem,) + v + floor))
            tag = "%s_V%d.%d" % ((stem,) + v)
            for ext in (".docx", ".md"):
                self.assertTrue(os.path.exists(os.path.join(doc, tag + ext)),
                                "缺少 %s%s（docx 与 md 必须同步）" % (tag, ext))
        self.assertTrue(
            os.path.exists(os.path.join(doc, "机房运维题库导入模板.xlsx")),
            "缺少导入模板 机房运维题库导入模板.xlsx")

    def test_docs_match_current_code(self):
        """需求书里承诺的接口，代码里必须真的有路由，避免文档写了、系统没有。"""
        src = open(os.path.join(BASE, "server.py"), encoding="utf-8").read()
        for route in ("/api/import/template", "/api/import/parse", "/api/import/commit",
                      "/api/ai/config", "/api/ai/test"):
            self.assertIn(route, src, "需求书 FR-IM / FR-ST 提到 %s，但服务端没有该路由" % route)

    def test_data_layout(self):
        self.assertTrue(os.path.exists(self.core.DB_PATH))
        self.assertTrue(os.path.isdir(self.core.BACKUP_DIR))


class P6Probe(BaseCase):
    """验收摸底工具：双击 bat → 逐份试读 → 出一份 HTML 报告，且绝不写库。

    这个工具是"用户手上那堆文件到底能不能读"的第一道照妖镜，它自己不能坏。
    之前踩过一次：报告模板里塞了 CSS，printf 式格式化被 CSS 的 `100%` 噎住，
    控制台一切正常、最后一步生成报告直接抛 ValueError——所以这里必须真跑一遍。
    """

    APP = BASE
    PROBE = os.path.join(os.path.dirname(BASE), "tools", "probe_import.py")

    def test_bat_points_at_probe(self):
        bat = os.path.join(BASE, "验收摸底.bat")
        self.assertTrue(os.path.exists(bat), "缺少验收摸底.bat")
        text = open(bat, encoding="utf-8", errors="ignore").read().lower()
        self.assertIn("probe_import.py", text)
        self.assertIn("python", text)
        self.assertIn("--open", text, "跑完要自动打开报告，否则用户不知道去哪看")

    def test_probe_writes_report_and_touches_nothing(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="zjlab_probe_")
        try:
            with open(os.path.join(tmp, "样例.txt"), "w", encoding="utf-8") as f:
                f.write("一、单项选择题\n1. 机房温度应控制在多少度（　）。\n"
                        "A. 18~27℃\nB. 10~35℃\n答案：A\n"
                        "2. UPS 后备时间取决于（　）。\nA. 负载\nB. 面积\n答案：A\n")
            with open(os.path.join(tmp, "假文件.doc"), "wb") as f:
                f.write(b"hello")
            with open(os.path.join(tmp, "先把文件放这里.txt"), "w", encoding="utf-8") as f:
                f.write("说明文件，不该被当题库试读")

            proc = subprocess.run([sys.executable, self.PROBE, tmp],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="ignore", timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("2 题", proc.stdout)

            out = os.path.join(tmp, "识别报告.html")
            self.assertTrue(os.path.exists(out), "没有生成报告")
            with open(out, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("题库识别报告", html)
            self.assertIn("可直接导入", html, "能读的文件没给正面结论")
            self.assertIn("读不出", html, "坏文件没给负面结论")
            self.assertIn("机房温度应控制在多少度", html, "报告里没有样例题干，看不出识别对不对")
            self.assertNotIn("说明文件，不该被当题库试读", html,
                             "目录里的说明文件被当成题库试读了")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_probe_reports_ai_hint_for_images(self):
        """图片没有文字层，必须明确指向"要开 AI"，而不是笼统说读不出。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="zjlab_probe_img_")
        try:
            png = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (40).to_bytes(4, "big")
                   + (40).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
            with open(os.path.join(tmp, "拍的照片.png"), "wb") as f:
                f.write(png)
            proc = subprocess.run([sys.executable, self.PROBE, tmp],
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="ignore", timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            with open(os.path.join(tmp, "识别报告.html"), encoding="utf-8") as f:
                html = f.read()
            self.assertIn("要开 AI", html, "图片没被引导到 AI 通道：%s" % proc.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_garbled_pdf_verdict(self):
        """2009 年那种没带 ToUnicode 映射的 PDF：文字层抽出来是乱码，
        要明确告诉用户"找原始 Word / 走 AI"，而不是笼统的"没识别到题目"。"""
        sys.path.insert(0, os.path.dirname(self.PROBE))
        import probe_import
        normal = "机房精密空调的加湿用水应使用软化水，自来水硬度高容易结垢。" * 3
        self.assertEqual(probe_import.pdf_garbled_msg(normal), "", "正常中文不该报乱码")
        self.assertEqual(probe_import.pdf_garbled_msg(""), "", "空文字层走扫描件判断，不归这里管")
        self.assertEqual(probe_import.pdf_garbled_msg("正常", scanned=True), "")
        garbled = "汉字只有这几个" + "Ê\x01\x1f&ë&ë\x00\x01\x04\x86\n\x00u\x06ý\x04ñ" * 40
        msg = probe_import.pdf_garbled_msg(garbled)
        self.assertTrue(msg, "乱码文字层要给出原因")
        self.assertIn("AI", msg)
        self.assertIn("ToUnicode", msg)


class P6Restart(BaseCase):
    def test_restart_keeps_data(self):
        self.seed(per_cell=2)
        r = self.pe.build(self.base_cfg())
        self.assertTrue(r["ok"], r.get("errors"))
        expect_q = self.core.stats()["total"]
        expect_p = len(self.pe.list_papers({})["items"])

        port = free_port()
        proc, state = start_server(port, self.tmp)
        self.assertIsNotNone(state, "首次启动失败")
        try:
            self.assertEqual(state["stats"]["total"], expect_q)
            papers = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/api/papers" % port, timeout=10).read())
            self.assertEqual(papers["total"], expect_p)
        finally:
            proc.terminate()
            proc.wait(timeout=8)

        port2 = free_port()
        proc2, state2 = start_server(port2, self.tmp)
        self.assertIsNotNone(state2, "重启失败")
        try:
            self.assertEqual(state2["stats"]["total"], expect_q, "重启后题量不一致")
            papers2 = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/api/papers" % port2, timeout=10).read())
            self.assertEqual(papers2["total"], expect_p, "重启后试卷数不一致")
            same = [p for p in papers2["items"] if p["no"] == r["no"]]
            self.assertEqual(len(same), 1, "重启后编号丢失")
            self.assertAlmostEqual(same[0]["total_score"], r["total_score"], places=2)
        finally:
            proc2.terminate()
            proc2.wait(timeout=8)

    def test_second_instance_uses_other_port(self):
        port = free_port()
        proc, state = start_server(port, self.tmp)
        self.assertIsNotNone(state)
        try:
            port2 = free_port()
            proc2, state2 = start_server(port2, self.tmp)
            self.assertIsNotNone(state2, "同一数据目录应允许第二个实例（不同端口）")
            proc2.terminate()
            proc2.wait(timeout=8)
        finally:
            proc.terminate()
            proc.wait(timeout=8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
