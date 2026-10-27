#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P13：PDF 文字层抽取与 PDF 导入。

原来 PDF 是直接拒收的（"排版差异大、易出错"），实际用起来不成立——
现场定稿的卷子八成是 PDF。这里验收的是"能读"而不是"读得完美"：

  1) 文字版 PDF 必须能抽出带换行的中文（换行靠文本矩阵纵坐标，不能靠空格猜）；
  2) 扫描件必须被识别出来并给明确提示，不能笼统报"无法提取文字"；
  3) 页序必须按 /Pages 树的 /Kids，不能按对象号；
  4) 对象流（ObjStm）里压着的对象也要能找到；
  5) 加密与假 PDF 要报可读的错，不能抛栈。
"""

import base64
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from common import BaseCase  # noqa: E402
from httpbase import HttpCase  # noqa: E402
import pdftext  # noqa: E402


# ---------------------------------------------------------------- 造 PDF

def make_pdf(objects):
    """把 {对象号: 对象体} 拼成一份带 xref 的最小 PDF。"""
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"
    size = max(objects) + 1
    xref_at = len(out)
    out += b"xref\n0 %d\n" % size
    out += b"0000000000 65535 f \n"
    for num in range(1, size):
        if num in offsets:
            out += b"%010d 00000 n \n" % offsets[num]
        else:
            out += b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref_at)
    return bytes(out)


def stream(body, extra=b""):
    return b"<< /Length %d %s >>\nstream\n" % (len(body), extra) + body + b"\nendstream"


def simple_pdf(content, font_body=None):
    """单页 + 简单字体（WinAnsi），内容是原样的内容流。"""
    font = font_body or (b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                         b"/Encoding /WinAnsiEncoding >>")
    return make_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        4: stream(content),
        5: font,
    })


CMAP_CN = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Test def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
3 beginbfchar
<0001> <4E2D>
<0002> <6587>
<0003> <9898>
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def cid_pdf(content, cmap=CMAP_CN):
    """单页 + Type0（Identity-H）+ ToUnicode。"""
    return make_pdf({
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
        4: stream(content),
        5: (b"<< /Type /Font /Subtype /Type0 /BaseFont /TestCN "
            b"/Encoding /Identity-H /ToUnicode 6 0 R >>"),
        6: stream(cmap),
    })


class PdfCore(BaseCase):
    """抽取器本身的单元验收（不启服务，直接喂字节）。"""

    def test_literal_tj_simple_font(self):
        """简单字体用 `(text) Tj` 字面量，必须能读出来（不能用 ToUnicode 才能解）。"""
        raw = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Power supply unit) Tj ET")
        r = pdftext.read_pdf(raw)
        self.assertIn("Power supply unit", r["text"])
        self.assertEqual(r["page_count"], 1)
        self.assertFalse(r["scanned"], "有文字的 PDF 不该被判成扫描件")

    def test_hex_tj_with_tounicode(self):
        """Type0 + ToUnicode：中文靠查表还原，双字节码。"""
        raw = cid_pdf(b"BT /F1 12 Tf 72 720 Td <000100020003> Tj ET")
        self.assertEqual(pdftext.read_pdf(raw)["text"].strip(), "中文题")

    def test_escaped_parens_in_literal(self):
        """字面量里的转义括号不能把字符串截断。"""
        raw = simple_pdf(b"BT /F1 12 Tf 72 720 Td (Flow \\(approx\\) 30) Tj ET")
        self.assertIn("(approx)", pdftext.read_pdf(raw)["text"])

    def test_newline_follows_text_matrix(self):
        """两行不同纵坐标必须断成两行：题号在行首是拆题的前提。"""
        content = (b"BT /F1 12 Tf 72 720 Td (1. First question) Tj ET\n"
                   b"BT /F1 12 Tf 72 700 Td (A. yes) Tj ET")
        text = pdftext.read_pdf(simple_pdf(content))["text"]
        self.assertIn("1. First question\nA. yes", text,
                      "纵坐标变了却没断行，拆题会把选项并进题干：%r" % text)

    def test_same_line_pieces_not_split(self):
        """同一行上的多段（TJ 数组）不能因为多次 Tj 被拆成多行。"""
        content = (b"BT /F1 12 Tf 72 720 Td (1. Question) Tj 40 0 Td (continued) Tj ET")
        text = pdftext.read_pdf(simple_pdf(content))["text"]
        self.assertEqual(len([ln for ln in text.split("\n") if ln.strip()]), 1,
                         "同一行被拆开了：%r" % text)

    def test_page_order_follows_kids(self):
        """页序按 /Kids，不按对象号。"""
        raw = make_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            # Kids 先 6 后 5，与对象号相反
            2: b"<< /Type /Pages /Kids [6 0 R 5 0 R] /Count 2 >>",
            5: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 7 0 R >> >> /Contents 3 0 R >>"),
            6: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>"),
            3: stream(b"BT /F1 12 Tf 72 720 Td (SECOND) Tj ET"),
            4: stream(b"BT /F1 12 Tf 72 720 Td (FIRST) Tj ET"),
            7: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        })
        pages = pdftext.read_pdf(raw)["pages"]
        self.assertEqual([p.strip() for p in pages], ["FIRST", "SECOND"],
                         "页序按对象号排了，导进来题序就乱了")

    def test_objects_inside_object_stream(self):
        """对象流里压着的对象也要能找到（新版 Word / WPS 会这么存）。"""
        # 把 5 号字体对象塞进 ObjStm
        inner = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        head = b"5 0 "
        data = head + inner
        raw = make_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"),
            4: stream(b"BT /F1 12 Tf 72 720 Td (In object stream) Tj ET"),
            8: stream(data, b"/Type /ObjStm /N 1 /First %d " % len(head)),
        })
        self.assertIn("In object stream", pdftext.read_pdf(raw)["text"])

    def test_image_only_pdf_is_scanned(self):
        """只有图像、没有文字的 PDF（扫描件）要判成 scanned，而不是"读出来是空的"。"""
        content = b"q 595 0 0 842 0 0 cm /Im0 Do Q"
        raw = make_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"),
            4: stream(content),
            5: (b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 >>"),
        })
        r = pdftext.read_pdf(raw)
        self.assertTrue(r["scanned"], "没有文字层却报 scanned=False，前台会误导用户")
        self.assertEqual(r["chars"], 0)

    def test_rejects_non_pdf(self):
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.read_pdf(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        self.assertIn("PDF", str(ctx.exception))

    def test_rejects_encrypted(self):
        raw = simple_pdf(b"BT /F1 12 Tf 72 720 Td (secret) Tj ET")
        raw = raw.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R")
        with self.assertRaises(pdftext.PdfError) as ctx:
            pdftext.read_pdf(raw)
        self.assertIn("加密", str(ctx.exception))

    def test_broken_stream_does_not_raise(self):
        """流解压失败时不能抛异常，最多是这一页读不出字。"""
        raw = make_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << >> /Contents 4 0 R >>"),
            4: b"<< /Length 20 /Filter /FlateDecode >>\nstream\n" + b"x" * 20 + b"\nendstream",
        })
        self.assertEqual(pdftext.read_pdf(raw)["pages"], [""])


class PdfImport(HttpCase):
    """导入链路：PDF 进 /api/import/parse，要能拆成题目。"""

    def upload(self, raw, filename):
        return self.post_json("/api/import/parse", {
            "filename": filename,
            "content": base64.b64encode(raw).decode("ascii"),
        })

    def test_imports_text_pdf(self):
        """文字版 PDF 要拆出结构化题目（题型、选项、答案都得对）。"""
        content = (
            b"BT /F1 12 Tf 72 760 Td (1. Generator should be warmed up before load.) Tj ET\n"
            b"BT /F1 12 Tf 72 740 Td (A. Correct) Tj ET\n"
            b"BT /F1 12 Tf 72 720 Td (B. Wrong) Tj ET\n"
            b"BT /F1 12 Tf 72 700 Td (Answer: A) Tj ET\n"
            b"BT /F1 12 Tf 72 660 Td (2. Chilled water makeup is about 1 percent.) Tj ET\n"
            b"BT /F1 12 Tf 72 640 Td (A. Correct) Tj ET\n"
            b"BT /F1 12 Tf 72 620 Td (B. Wrong) Tj ET\n"
            b"BT /F1 12 Tf 72 600 Td (Answer: B) Tj ET"
        )
        r = self.upload(simple_pdf(content), "旧卷子.pdf")
        self.assertTrue(r.get("ok"), r.get("error"))
        self.assertEqual(len(r["items"]), 2, "应拆出 2 道题：%s" % r["items"])
        self.assertIn("Generator", r["items"][0]["stem"])
        self.assertEqual(r["items"][0]["answer"], "A")
        self.assertEqual(len(r["items"][0]["options"]), 2)
        self.assertEqual(r["items"][1]["answer"], "B")

    def test_scanned_pdf_gets_actionable_hint(self):
        """扫描件不能只说"无法提取"，要告诉用户下一步怎么做。"""
        raw = make_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>"),
            4: stream(b"q 595 0 0 842 0 0 cm /Im0 Do Q"),
            5: (b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 "
                b"/ColorSpace /DeviceGray /BitsPerComponent 8 >>"),
        })
        r = self.upload(raw, "扫描件.pdf")
        self.assertFalse(r.get("ok"))
        msg = r.get("error") or ""
        self.assertIn("图片", msg, "要说明这是图片版而不是格式不支持")
        self.assertTrue("视觉" in msg or "OCR" in msg or "拍照" in msg,
                        "要给出可行下一步，实际提示：%s" % msg)

    def test_encrypted_pdf_reports_clearly(self):
        raw = simple_pdf(b"BT /F1 12 Tf 72 720 Td (x) Tj ET")
        raw = raw.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 9 0 R")
        r = self.upload(raw, "加密.pdf")
        self.assertFalse(r.get("ok"))
        self.assertIn("加密", r.get("error") or "")


EXAM_HTML = """<html><head><meta charset="utf-8"><style>
body{font-family:'Microsoft YaHei',sans-serif;font-size:14px;line-height:1.9}
p{margin:0}</style></head><body>
<p>一、单项选择题（每题 2 分，共 4 分）</p>
<p>1. 柴油发电机启动后应先空载暖机再带负荷，这种说法（　）。</p>
<p>A. 正确</p><p>B. 错误</p><p>C. 视机型而定</p><p>D. 无所谓</p>
<p>答案：A</p>
<p>答案解析：冷机直接带负荷会加剧缸体磨损。</p>
<p>2. 冷冻水系统补水量一般按蒸发量的百分之几估算？</p>
<p>A. 0.5%</p><p>B. 1%</p><p>C. 3%</p><p>D. 5%</p>
<p>答案：B</p>
<p>二、判断题（每题 2 分，共 4 分）</p>
<p>1. 消防水泵每月应做一次手动启泵试验。（　）</p>
<p>答案：正确</p>
<p>2. UPS 电池可以在满载状态下长时间放电。（　）</p>
<p>答案：错误</p>
</body></html>"""


class PdfRealExam(BaseCase):
    """现场拿到的基本是 Word/WPS 导出的 PDF，用真浏览器打一份再导入。

    这条链路覆盖的是"中文 + Type0 字体 + ToUnicode + 每行独立文本矩阵"，
    与手写 PDF 的简单字体完全不同；只测手写 PDF 会漏掉真问题
    （比如「答案解析」被当成答案行，把单选的答案顶成空）。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from renderbase import find_browser
        from cdp import Chrome

        cls.browser = find_browser()
        cls.chrome = Chrome(cls.browser) if cls.browser else None

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.chrome:
                cls.chrome.close()
        finally:
            super().tearDownClass()

    def setUp(self):
        if not self.chrome:
            self.fail("未找到 Chrome 或 Edge，真实 PDF 导入测试无法执行（不允许跳过）")

    def print_pdf(self, html):
        import tempfile

        d = tempfile.mkdtemp(prefix="zjlab_pdf_")
        path = os.path.join(d, "exam.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        c = self.chrome
        c.goto("file:///" + path.replace("\\", "/"))
        c.wait("document.body && document.body.innerText.length>100", timeout=25)
        return base64.b64decode(c.call("Page.printToPDF", printBackground=True)["data"])

    def parse(self, raw, name="旧卷子.pdf"):
        import importer
        return importer.parse(raw, name)

    def test_real_exam_pdf_imports(self):
        """中文真卷子打成 PDF 再导入：题数、题型、选项、答案都要对。"""
        res = self.parse(self.print_pdf(EXAM_HTML))
        items = res["items"]
        self.assertEqual(len(items), 4, "应拆出 4 道题，实际 %d：%s"
                         % (len(items), [i["stem"][:20] for i in items]))
        self.assertEqual([i["qtype"] for i in items],
                         ["single", "single", "judge", "judge"],
                         "题型串了：%s" % [i["qtype"] for i in items])
        self.assertEqual([i["answer"] for i in items], ["A", "B", "正确", "错误"],
                         "答案不对：%s" % [i["answer"] for i in items])
        self.assertEqual(len(items[0]["options"]), 4, "四个选项没拆出来")
        self.assertEqual(items[1]["options"][2]["text"], "3%")
        self.assertTrue(items[0]["stem"].startswith("柴油发电机"),
                        "题干被行首的题号或页眉污染了：%r" % items[0]["stem"])

    def test_analysis_line_not_taken_as_answer(self):
        """「答案解析：…」不能把前面的真答案顶掉。"""
        items = self.parse(self.print_pdf(EXAM_HTML))["items"]
        first = items[0]
        self.assertEqual(first["answer"], "A",
                         "答案解析被当成答案行，单选答案变成空/说明文字")
        self.assertIn("缸体磨损", first["analysis"], "解析没进解析字段：%r" % first["analysis"])


#: 20 题、必然跨页、带页脚页码——最接近"手上那份旧卷子"的样子
LONG_EXAM_HTML = """<html><head><meta charset="utf-8"><style>
body{font-family:'Microsoft YaHei',sans-serif;font-size:13px;line-height:1.8}
p{margin:0}
@page{size:A4;margin:15mm;
  @bottom-center{content:"第 " counter(page) " 页  共 " counter(pages) " 页";font-size:9pt}}
</style></head><body>
<p style="text-align:center;font-size:17px">机房运维月度考核试卷</p>
<p style="text-align:center">单位：某某数据中心　编号：ZJLab-20261025-001</p>
<p>一、单项选择题（每题 2 分）</p>
%s
</body></html>""" % "".join(
    "<p>%d. 第%d题：柴油发电机启动前应检查的项目不包括以下哪一项？</p>"
    "<p>A. 机油液位</p><p>B. 冷却液液位</p><p>C. 蓄电池电压</p><p>D. 机房温度</p>"
    "<p>答案：%s</p>" % (i, i, "ABCD"[(i - 1) % 4])
    for i in range(1, 21))


class PdfLongExam(PdfRealExam):
    """多页真卷子：跨页的题不能被页脚打断，页脚也不能混进题干或选项。"""

    def test_multipage_exam_imports_completely(self):
        res = self.parse(self.print_pdf(LONG_EXAM_HTML), "月度考核.pdf")
        items = res["items"]
        self.assertEqual(len(items), 20, "跨页后题数不对，实际 %d：%s"
                         % (len(items), [i["stem"][:16] for i in items]))
        self.assertEqual([i["answer"] for i in items],
                         ["ABCD"[(i - 1) % 4] for i in range(1, 21)],
                         "跨页后答案串了")
        self.assertTrue(all(len(i["options"]) == 4 for i in items),
                        "跨页时选项丢了：%s" % [(i["stem"][:12], len(i["options"])) for i in items])
        self.assertEqual(res["warnings"], [], "干净的多页卷子不该有警告")

    def test_page_footer_never_leaks_into_content(self):
        """页脚「第 N 页 共 M 页」必须整行丢掉，不能粘到题干或选项上。

        注意「页」可能被字体映射成兼容字「⻚」，所以这里按正则判，不写死字面量。
        """
        items = self.parse(self.print_pdf(LONG_EXAM_HTML), "月度考核.pdf")["items"]
        pattern = re.compile(r"(?:第|共)\s*\d+\s*[页\u2ee7]")
        for it in items:
            blob = it["stem"] + "".join(o["text"] for o in it["options"])
            self.assertIsNone(pattern.search(blob),
                              "页脚混进了内容：%r" % blob[:60])

    def test_title_and_number_not_taken_as_question(self):
        """卷首标题、单位/编号行不能变成一道题。"""
        items = self.parse(self.print_pdf(LONG_EXAM_HTML), "月度考核.pdf")["items"]
        for it in items[:2]:
            self.assertNotIn("机房运维月度考核试卷", it["stem"])
            self.assertNotIn("ZJLab-20261025-001", it["stem"])


class PdfFurniture(BaseCase):
    """页脚清理本身的单元验收（不经过浏览器，用纯文本直接喂）。"""

    def parse_text(self, text):
        import importer
        return importer.parse(text.encode("utf-8"), "卷子.txt")

    def test_footer_line_dropped(self):
        text = ("1. 柴油发电机启动后应先空载暖机，这种说法（　）。\n"
                "A. 正确\nB. 错误\n答案：A\n"
                "第 1 页 共 2 页\n"
                "2. 冷却塔填料结垢会造成什么后果？\n"
                "A. 换热效率下降\nB. 风量增大\n答案：A\n")
        items = self.parse_text(text)["items"]
        self.assertEqual(len(items), 2, "页脚行不该被当成内容：[%s]"
                         % [i["stem"][:20] for i in items])
        self.assertNotIn("共 2 页", items[0]["stem"] + items[1]["stem"])

    def test_footer_between_options_does_not_pollute(self):
        """页脚插在选项中间时，选项文本里不能多出页码。"""
        text = ("1. 冷冻水系统补水量应如何估算？\n"
                "A. 按蒸发量的 1%\n"
                "B. 按蒸发量的 3%\n"
                "第 2 页 共 3 页\n"
                "C. 按循环量的 1%\n"
                "D. 按补水次数估算\n"
                "答案：A\n")
        it = self.parse_text(text)["items"][0]
        texts = "".join(o["text"] for o in it["options"])
        self.assertNotIn("共 3 页", texts, "页脚被拼进了选项：%r" % texts)
        self.assertEqual(len(it["options"]), 4, "页脚把选项拆断了：%r" % texts)

    def test_compat_page_glyph(self):
        """有些字体把「页」映射成兼容字「⻚」（U+2EE7），同样要认出来。"""
        text = ("1. 消防水泵每月应做一次手动启泵试验。（　）\n"
                "答案：正确\n"
                "第 1 ⻚ 共 2 ⻚\n"
                "2. UPS 电池可以长时间满载放电。（　）\n答案：错误\n")
        items = self.parse_text(text)["items"]
        self.assertEqual(len(items), 2)
        self.assertNotIn("⻚", items[0]["stem"] + items[1]["stem"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
