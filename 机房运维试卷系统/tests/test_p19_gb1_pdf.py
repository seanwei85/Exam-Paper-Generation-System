#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P19 测试：无 ToUnicode 的老中文 PDF（Adobe-GB1 CID 编码）本地直接解析。

背景：方正等老排版软件导出的中文 PDF，字体不内嵌、没有 /ToUnicode，文字层
抽出来全是 2 字节 CID 乱码。这类字体的 /CIDSystemInfo 标明 Adobe-GB1 字符集，
CID→Unicode 用官方映射表（gb1map，内嵌压缩表）硬解即可，不需要 AI、不需要网络。

金标准比对：这份 PDF 的人工核对结果存在 tests/fixtures/gb1_golden.txt（67 题）。
软件解析结果必须与金标准大体一致——这是用户要求的「手工成果与软件结果比对」，
也是防止映射表或拆题规则将来退化的锚。
"""

import difflib
import os
import re
import sys
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import pdftext  # noqa: E402
import importer  # noqa: E402
import gb1map  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "gb1_forced_code_paper.pdf")
GOLDEN = os.path.join(HERE, "fixtures", "gb1_golden.txt")


def _minimal_gb1_pdf(pairs):
    """构造一份最小的 Adobe-GB1 CID PDF：Type0 + Identity-H、字体不内嵌、
    无 ToUnicode，正文用 <十六进制 CID>Tj 书写。pairs 为 [(x, y, 文本)]。"""
    ops = []
    for x, y, text in pairs:
        cids = []
        for ch in text:
            cid = gb1map.UNI2CID.get(ord(ch))
            if cid is None:
                raise AssertionError("夹具字符 %r 不在 GB1 表里" % ch)
            cids.append("%04x" % cid)
        ops.append("BT /F1 12 Tf 1 0 0 1 %g %g Tm <%s> Tj ET" % (x, y, "".join(cids)))
    content = "\n".join(ops).encode("latin-1")
    comp = zlib.compress(content)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type0 /BaseFont /SimSun /Encoding /Identity-H "
        b"/DescendantFonts [6 0 R] >>",                       # 故意不给 /ToUnicode
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(comp), comp),
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimSun "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
        b"/DW 1000 >>",
    ]
    out = [b"%PDF-1.4"]
    for i, body in enumerate(objs, 1):
        out.append(b"%d 0 obj\n%s\nendobj" % (i, body))
    pdf = b"\n".join(out) + b"\n%%EOF"
    return pdf


class P19Gb1Map(unittest.TestCase):
    def test_table_basics(self):
        """官方表的几个锚点：来自 PDF 实测（工=1789、程=1265、建=2169）。"""
        self.assertEqual(gb1map.get(1789), "工")
        self.assertEqual(gb1map.get(1265), "程")
        self.assertEqual(gb1map.get(2169), "建")
        self.assertEqual(gb1map.get(9963), "▁")
        self.assertEqual(gb1map.get(117), "《")
        self.assertEqual(gb1map.get(99999999), "", "越界 CID 返回空串")

    def test_uni2cid_complete(self):
        """双向表自洽：UNI2CID 与 get 互为反函数（抽样 100 个）。"""
        import random
        random.seed(7)
        cids = random.sample(sorted(gb1map.MAP), 100)
        for cid in cids:
            ch = gb1map.get(cid)
            self.assertEqual(gb1map.UNI2CID.get(ord(ch)), cid)


class P19Gb1Pdf(unittest.TestCase):
    def test_synthetic_gb1_pdf_decodes(self):
        """构造的最小 GB1 PDF：无 ToUnicode 也能解出中文。"""
        pdf = _minimal_gb1_pdf([(50, 800, "机房温度应控制在18~27℃"),
                                (50, 780, "UPS后备时间取决于负载")])
        info = pdftext.read_pdf(pdf)
        self.assertFalse(info["scanned"])
        self.assertIn("机房温度应控制在", info["text"])
        self.assertIn("UPS后备时间取决于负载", info["text"])

    def test_real_fixture_decodes(self):
        """真实存量文件（方正导出，字体无 ToUnicode）：直接解出可读文本。"""
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        info = pdftext.read_pdf(raw)
        t = info["text"]
        self.assertFalse(info["scanned"], "这文件有文字层，不该判成扫描件")
        self.assertIn("工程建设强制性条文", t)
        self.assertIn("机房内严禁存放易燃", t)
        # 之前是乱码，现在汉字占绝对多数
        printable = [c for c in t if c.isprintable() and not c.isspace()]
        cjk = sum(1 for c in printable if "\u4e00" <= c <= "\u9fff")
        self.assertGreater(cjk / max(1, len(printable)), 0.5)

    def test_importer_splits_standard_items(self):
        """「标准号: 《规范》◆题干」逐条开题、出处进题干、挖空识别为填空。"""
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        res = importer.parse(raw, "试题C.pdf", cats=None)
        items = res["items"]
        self.assertGreaterEqual(len(items), 60, "应拆出 60+ 题")
        self.assertTrue(all(i["qtype"] == "fill" for i in items),
                        [i["qtype"] for i in items[:5]])
        first = items[0]["stem"]
        self.assertIn("【《固定电话交换设备安装工程验收规范》", first)
        self.assertIn("机房内严禁存放易燃", first)
        self.assertIn("▁", first, "挖空符号应保留在题干里")
        self.assertTrue(all(not (i.get("answer") or "").strip() for i in items),
                        "原卷没有答案，全部应为草稿")

    def test_golden_comparison(self):
        """金标准比对：人工核对结果 vs 软件解析结果，匹配率必须 ≥ 90%。

        金标准每行一题（人工整理时的格式），比对前把两边的出处前缀、空白、
        挖空符号归一，再用 difflib 相似度配对。
        """
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        parsed = [i["stem"] for i in importer.parse(raw, "试题C.pdf", cats=None)["items"]]

        def norm(s):
            s = s.strip()
            s = re.sub(r"^【[^】]*】", "", s)          # 金标准的人工出处前缀
            s = re.sub(r"^标准号\s*[:：]\s*《[^》]+》[^◆]*◆", "", s)
            s = re.sub(r"[▁_]+", "", s)                # 挖空不计入相似度
            return re.sub(r"\s+", "", s)

        with open(GOLDEN, encoding="utf-8") as f:
            gold = [l.split(".", 1)[1] for l in f.read().split("\n")
                    if re.match(r"^\d+\.", l)]
        gold = [norm(g) for g in gold]
        parsed_n = [norm(s) for s in parsed]

        matched = 0
        misses = []
        for g in gold:
            best = max((difflib.SequenceMatcher(None, g, p).ratio() for p in parsed_n),
                       default=0.0)
            if best >= 0.80:
                matched += 1
            else:
                misses.append((g[:24], round(best, 2)))
        rate = matched / len(gold)
        self.assertGreaterEqual(rate, 0.90,
                                "金标准匹配率 %.0f%%（%d/%d），未匹配：%s"
                                % (rate * 100, matched, len(gold), misses[:6]))


if __name__ == "__main__":
    unittest.main()
