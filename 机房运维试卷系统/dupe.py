#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重复题判定（FR-IM-07）。

只做纯文本判定，不碰数据库：调用方把库里已有的 (id, 题干) 列表传进来，
返回最像的那一条。这样判定逻辑可以单独测，也方便以后换算法。

判定分两层，先粗后细：
  1) 归一化后比较：全角转半角、去掉所有空白、去掉括号占位与下划线填空。
     用户从不同来源拷来的题，差别往往只是「（　）」还是「( )」；
  2) 粗筛之后才算相似度：先按长度差、字符集重合度排除明显无关的，
     再用 difflib.SequenceMatcher 算编辑相似度。库大时这一步能省掉大部分开销。
"""

import re
import unicodedata
from difflib import SequenceMatcher

# 相似度阈值：超过即标记为疑似重复。0.9 大约对应「20 字的题干改一两个字」
DEFAULT_THRESHOLD = 0.9
# 归一化后短于这个长度就不参与判定，避免「机房温度」这类短语互相误判
MIN_LEN = 6

_SPACE = re.compile(r"\s+")
_BLANK = re.compile(r"[（(]\s*[)）]|_+|＿+")
_PUNCT = re.compile(r"[，,。.；;：:、！!？?\"'“”‘’（）()《》<>【】\[\]]")


def norm(stem):
    """归一化题干，只用于比较，不改动原始数据。"""
    s = unicodedata.normalize("NFKC", stem or "")
    s = _SPACE.sub("", s)
    s = _BLANK.sub("", s)
    s = _PUNCT.sub("", s)
    s = s.lower()
    return s if len(s) >= MIN_LEN else ""


def similarity(a, b):
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _worth(a, b):
    """粗筛：长度差太大或字符集几乎不重合的直接跳过。"""
    if len(a) * 1.7 < len(b) or len(b) * 1.7 < len(a):
        return False
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    return inter >= 0.5 * min(len(sa), len(sb))


def find(stem, pool, threshold=DEFAULT_THRESHOLD):
    """在 pool（[(id, 题干), ...]）里找最相似的一条。

    返回 (id, 相似度, 库内题干) 或 None。相似度为 3 位小数，题干预截断到 60 字，
    便于界面直接告诉用户"题库里已有的是哪一道"。
    """
    target = norm(stem)
    if not target:
        return None
    best_id, best, best_stem = None, 0.0, ""
    for qid, other in pool:
        cand = norm(other)
        if not cand or not _worth(target, cand):
            continue
        score = 1.0 if cand == target else SequenceMatcher(None, target, cand).ratio()
        if score > best:
            best_id, best, best_stem = qid, score, other
            if best >= 0.999:
                break
    if best_id is None or best < threshold:
        return None
    return best_id, round(best, 3), best_stem[:60]


def mark(items, pool, threshold=DEFAULT_THRESHOLD):
    """给解析出的题目打上 dup / dup_of / dup_score / dup_stem 标记，返回标记数。"""
    n = 0
    for it in items:
        hit = find(it.get("stem") or "", pool, threshold)
        if hit:
            it["dup"], it["dup_of"], it["dup_score"] = True, hit[0], hit[1]
            it["dup_stem"] = hit[2]
            n += 1
        else:
            it["dup"], it["dup_of"], it["dup_score"] = False, None, None
            it["dup_stem"] = ""
    return n
