#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组卷引擎

组卷方式（mode）
  auto   系统自动   —— 只按题型题量与难度积分抽题，分类不限量
  quota  按分类配额 —— 一级分类按比例或题数分配题量
  custom 自定义组合 —— 配额 + 附加规则（排除近期已用 / 必考锁定 / 标签范围）

约束层级
  硬约束：题型题量、难度分解、必考锁定、范围过滤
  软约束：分类配额（近似到 ±1，结果里给出实际分布）

只用标准库。
"""

import json
import random
from collections import defaultdict
from datetime import date, datetime

import server as core

TYPES = core.TYPES
TYPE_NAMES = core.TYPE_NAMES
DIFFS = core.DIFFS
DIFF_NAMES = core.DIFF_NAMES
DIFF_POINTS = core.DIFF_POINTS

# 一键配平时的调整顺序：先动分值大的题，少动几道
BALANCE_ORDER = {"qa": 0, "fill": 1, "multi": 2, "single": 3, "judge": 4}


# ---------------------------------------------------------------- 默认配置

def default_config(conn=None):
    cats = core.categories_tree(conn)
    share = round(100 / len(cats)) if cats else 0
    quotas = {str(c["id"]): share for c in cats}
    if cats and share * len(cats) != 100:
        quotas[str(cats[0]["id"])] = 100 - share * (len(cats) - 1)
    return {
        "mode": "quota",
        "title": "机房运维上岗考试",
        "unit": core.get_setting("unit_name", conn),
        "duration": int(core.get_setting("duration", conn) or 45),
        "rows": [
            {"qtype": "single", "enabled": True, "count": 25, "score": 1},
            {"qtype": "multi", "enabled": True, "count": 8, "score": 2},
            {"qtype": "judge", "enabled": True, "count": 15, "score": 1},
            {"qtype": "fill", "enabled": True, "count": 10, "score": 1},
            {"qtype": "qa", "enabled": True, "count": 2, "score": 12},
        ],
        "diff_mode": "points",
        "diff_target": 40,
        "diff_ratio": {"easy": 50, "mid": 30, "hard": 20},
        "quota_mode": "percent",
        "quotas": quotas,
        "rules": {
            "avoid_recent": {"enabled": True, "papers": 3},
            "must": {"enabled": False, "ids": []},
            "scope": {"enabled": False, "tags": "", "new_days": 0, "new_ratio": 0},
        },
        "target_score": 100,
    }


def _diff_cap(rows):
    """整卷难度积分上限 = 各题型题量 × 该题型单题最高积分。"""
    cap = 0
    for r in rows:
        pts = DIFF_POINTS.get(r["qtype"]) or {}
        if pts:
            cap += r["count"] * max(pts.values())
    return cap


def diff_recommend(rows):
    """FR-AG-04：目标难度留空时的推荐值 —— 总上限的 30%（中等偏易）。"""
    return int(round(_diff_cap(rows) * 0.3))


def _norm(cfg):
    """补全缺失字段，容忍前端只传部分参数。"""
    base = default_config()
    cfg = dict(cfg or {})
    for k, v in base.items():
        cfg.setdefault(k, v)
    rules = dict(base["rules"])
    rules.update(cfg.get("rules") or {})
    cfg["rules"] = rules
    rows = []
    given = {r.get("qtype"): r for r in (cfg.get("rows") or [])}
    for t in TYPES:
        r = given.get(t) or {}
        rows.append({
            "qtype": t,
            "enabled": bool(r.get("enabled", False)),
            "count": int(r.get("count") or 0),
            "score": float(r.get("score") or 1),
        })
    cfg["rows"] = rows

    # 目标难度留空（None）时按推荐值走；明确填 0 表示"全易"，必须尊重
    rec = diff_recommend(active_rows(cfg))
    cfg["diff_recommend"] = rec
    if cfg.get("diff_target") is None:
        cfg["diff_target"] = rec
        cfg["diff_target_auto"] = True
    else:
        cfg["diff_target_auto"] = bool(cfg.get("diff_target_auto"))
    return cfg


def active_rows(cfg):
    return [r for r in cfg["rows"] if r["enabled"] and r["count"] > 0]


# ---------------------------------------------------------------- 难度分解

def _combos(qtype, n, ratio, avail=None):
    """枚举某题型 n 道题的难度组合；avail 限制各难度实际可用的题量。"""
    p = DIFF_POINTS[qtype]
    if avail is None:
        lim = {"easy": n, "mid": n, "hard": n}
    else:
        # 缺失的键表示该难度一道题都没有，不是"不限制"
        lim = {k: min(n, avail.get(k, 0)) for k in ("easy", "mid", "hard")}
    seen = {}
    for h in range(lim["hard"] + 1):
        for m in range(min(n - h, lim["mid"]) + 1):
            e = n - h - m
            if e > lim["easy"]:
                continue
            s = e * p["easy"] + m * p["mid"] + h * p["hard"]
            dist = (
                abs(e - n * ratio["easy"] / 100.0)
                + abs(m - n * ratio["mid"] / 100.0)
                + abs(h - n * ratio["hard"] / 100.0)
            )
            if s not in seen or dist < seen[s][0]:
                seen[s] = (dist, (e, m, h))
    return {s: v[1] for s, v in seen.items()}


def _solve_points(rows, target, ratio, avail_by=None):
    """把整卷目标积分拆到各题型。返回 (方案或 None, 最近可达值, 无法分解的题型)。"""
    avail_by = avail_by or {}
    combo_map = {}
    for r in rows:
        cm = _combos(r["qtype"], r["count"], ratio, avail_by.get(r["qtype"]))
        if not cm:
            return None, 0, r["qtype"]
        combo_map[r["qtype"]] = cm
    dp = {0: []}
    for r in rows:
        nxt = {}
        for s0, path in dp.items():
            for s1, combo in combo_map[r["qtype"]].items():
                s = s0 + s1
                if s not in nxt:
                    nxt[s] = path + [(r["qtype"], combo)]
        if not nxt:
            return None, 0, r["qtype"]
        dp = nxt
    if target in dp:
        return {t: c for t, c in dp[target]}, target, None
    nearest = min(dp.keys(), key=lambda s: abs(s - target))
    return None, nearest, None


def diff_plan(rows, cfg, avail_by=None):
    """返回 (各题型易/中/难数量, 整卷积分, 原始目标, 无法分解的题型)。"""
    ratio = cfg.get("diff_ratio") or {"easy": 50, "mid": 30, "hard": 20}
    if cfg.get("diff_mode") == "ratio":
        plan = {}
        for r in rows:
            n = r["count"]
            e = min(n, int(round(n * ratio["easy"] / 100.0)))
            m = min(n - e, int(round(n * ratio["mid"] / 100.0)))
            h = n - e - m
            if avail_by is not None:
                av = avail_by.get(r["qtype"], {})
                if e > av.get("easy", 0) or m > av.get("mid", 0) or h > av.get("hard", 0):
                    return None, 0, None, r["qtype"]
            plan[r["qtype"]] = (e, m, h)
        points = sum(
            c[0] * DIFF_POINTS[t]["easy"] + c[1] * DIFF_POINTS[t]["mid"] + c[2] * DIFF_POINTS[t]["hard"]
            for t, c in plan.items()
        )
        return plan, points, None, None

    target = int(cfg.get("diff_target") or 0)
    solved, real, bad = _solve_points(rows, target, ratio, avail_by)
    if solved is None and bad is None:
        # 目标不可达，退到最近可行值；预检会提示
        solved, _, bad = _solve_points(rows, real, ratio, avail_by)
        if solved is None:
            return None, real, target, bad
    if solved is None:
        return None, 0, target, bad
    return solved, real, target, None



# ---------------------------------------------------------------- 分类配额

def resolve_quota(cfg, cats, total_n):
    """把配额配置换算成每个分类的题量。留空或 0 的分类自动均分剩余。"""
    mode = cfg.get("quota_mode") or "percent"
    raw = cfg.get("quotas") or {}
    values = {}
    for c in cats:
        v = raw.get(str(c["id"]), raw.get(c["id"]))
        values[c["id"]] = None if v in (None, "") else float(v)

    filled = {k: v for k, v in values.items() if v and v > 0}
    empty = [k for k, v in values.items() if not v or v <= 0]

    if mode == "percent":
        used = sum(filled.values())
        if used > 100.5:
            return None, "分类占比合计超过 100%"
        rest = max(0.0, 100.0 - used)
        for k in empty:
            values[k] = rest / len(empty) if empty else 0
        counts = {k: total_n * v / 100.0 for k, v in values.items()}
    else:
        fixed = sum(filled.values())
        if fixed > total_n:
            return None, f"分类题数合计 {int(fixed)} 超过总题量 {total_n}"
        rest = total_n - fixed
        for k in empty:
            values[k] = rest / len(empty) if empty else 0
        counts = dict(values)

    # 最大余数法取整，保证合计等于总题量
    base = {k: int(v) for k, v in counts.items()}
    left = total_n - sum(base.values())
    if left > 0:
        order = sorted(counts.keys(), key=lambda k: counts[k] - int(counts[k]), reverse=True)
        i = 0
        while left > 0 and order:
            base[order[i % len(order)]] += 1
            left -= 1
            i += 1
    elif left < 0:
        order = sorted(counts.keys(), key=lambda k: int(counts[k]) - counts[k])
        i = 0
        while left < 0 and order:
            k = order[i % len(order)]
            if base[k] > 0:
                base[k] -= 1
                left += 1
            i += 1
            if i > 500:
                break
    return base, None


# ---------------------------------------------------------------- 题库池

def load_pool(cfg, conn):
    rules = cfg["rules"]
    where = ["1=1"]
    args = []
    # 没有答案的题不进候选池：卷子要附标准答案，抽到空答案等于出一份无法评分的卷。
    # 这类题是"导进来了但还没补答案"的草稿，在题库页能筛出来逐题补。
    where.append("TRIM(COALESCE(answer,''))<>''")

    avoid = rules.get("avoid_recent") or {}
    if avoid.get("enabled"):
        n = int(avoid.get("papers") or 0)
        if n > 0:
            recent = conn.execute(
                "SELECT id FROM paper ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            ids = [r["id"] for r in recent]
            if ids:
                marks = ",".join("?" * len(ids))
                where.append(f"id NOT IN (SELECT qid FROM paper_item WHERE paper_id IN ({marks}))")
                args.extend(ids)

    scope = rules.get("scope") or {}
    if scope.get("enabled") and (scope.get("tags") or "").strip():
        tags = [t.strip() for t in scope["tags"].replace("，", ",").split(",") if t.strip()]
        if tags:
            sub = " OR ".join(["tags LIKE ?"] * len(tags))
            where.append(f"({sub})")
            args.extend(["%" + t + "%" for t in tags])

    sql = "SELECT * FROM question WHERE " + " AND ".join(where)
    rows = conn.execute(sql, args).fetchall()
    pool = []
    settings = core.all_settings(conn)
    for r in rows:
        d = dict(r)
        d["options"] = json.loads(d["options"] or "[]")
        d["points"] = DIFF_POINTS.get(d["qtype"], {}).get(d["diff"], 0)
        d["final_score"] = core.question_score(r, settings)
        pool.append(d)
    return pool


def _is_new(q, days):
    if not days:
        return False
    try:
        created = datetime.strptime(q["created_at"][:10], "%Y-%m-%d").date()
    except Exception:
        return False
    return (date.today() - created).days <= days


# ---------------------------------------------------------------- 预检

def precheck(cfg, conn=None):
    own = conn is None
    conn = conn or core.connect()
    cfg = _norm(cfg)
    cats = core.categories_tree(conn)
    rows = active_rows(cfg)
    errors, warnings, gaps = [], [], []

    if not rows:
        errors.append("至少启用一个题型并填写题量")

    total_n = sum(r["count"] for r in rows)
    settings = core.all_settings(conn)
    pool = load_pool(cfg, conn)
    avail_by_type = defaultdict(int)
    avail_by = defaultdict(dict)
    for q in pool:
        avail_by_type[q["qtype"]] += 1
        avail_by[q["qtype"]][q["diff"]] = avail_by[q["qtype"]].get(q["diff"], 0) + 1

    # 填空题按题库平均空数估算小计
    fill_blanks = [q["blanks"] for q in pool if q["qtype"] == "fill" and q["blanks"]]
    avg_blank = (sum(fill_blanks) / len(fill_blanks)) if fill_blanks else 2

    by_type = []
    total_score = 0.0
    for r in rows:
        t = r["qtype"]
        if t == "fill":
            subtotal = r["count"] * r["score"] * avg_blank
            rule = "每空 %g 分（按平均 %.1f 空估算）" % (r["score"], avg_blank)
        else:
            subtotal = r["count"] * r["score"]
            rule = "每题 %g 分" % r["score"]
        total_score += subtotal
        avail = avail_by_type.get(t, 0)
        p = DIFF_POINTS[t]
        if avail < r["count"]:
            gaps.append({
                "qtype": t, "name": TYPE_NAMES[t], "need": r["count"],
                "avail": avail, "lack": r["count"] - avail,
                "reason": "题库题量不足",
            })
        by_type.append({
            "qtype": t, "name": TYPE_NAMES[t], "count": r["count"],
            "score": r["score"], "rule": rule, "subtotal": round(subtotal, 2),
            "avail": avail, "enough": avail >= r["count"],
            "avail_diff": dict(avail_by.get(t, {})),
            "points_min": r["count"] * p["easy"],
            "points_max": r["count"] * p["hard"],
        })

    # 难度（分解时必须受各难度实际可用量约束，否则会算出库里凑不出的组合）
    if rows:
        plan, points, target, bad = diff_plan(rows, cfg, avail_by)
    else:
        plan, points, target, bad = {}, 0, None, None
    pmin = sum(b["points_min"] for b in by_type)
    pmax = sum(b["points_max"] for b in by_type)
    feasible = True
    if bad:
        feasible = False
        row = next((r for r in rows if r["qtype"] == bad), None)
        av = avail_by.get(bad, {})
        errors.append(
            "%s 的难度构成在题库中凑不出：需 %d 题，可用 易%d / 中%d / 难%d，请减少题量或补充该难度题目"
            % (TYPE_NAMES.get(bad, bad), row["count"] if row else 0,
               av.get("easy", 0), av.get("mid", 0), av.get("hard", 0))
        )
    elif rows and cfg.get("diff_mode") != "ratio":
        feasible = pmin <= int(cfg.get("diff_target") or 0) <= pmax
        if not feasible:
            errors.append(
                "难度积分 %s 超出可达范围 %d~%d，请调整目标值或题量"
                % (cfg.get("diff_target"), pmin, pmax)
            )
    diff_items = []
    for b in by_type:
        c = (plan or {}).get(b["qtype"], (0, 0, 0))
        diff_items.append({
            "qtype": b["qtype"], "name": b["name"],
            "easy": c[0], "mid": c[1], "hard": c[2],
        })

    # 分类配额
    quota_items = []
    quota = None
    if cfg["mode"] in ("quota", "custom"):
        quota, err = resolve_quota(cfg, cats, total_n)
        if err:
            errors.append(err)
        else:
            pool_by_cat = defaultdict(int)
            for q in pool:
                pool_by_cat[q["cat_id"]] += 1
            short, short_detail = [], []
            for c in cats:
                quota_items.append({
                    "cat_id": c["id"], "name": c["name"], "count": quota[c["id"]],
                    "avail": pool_by_cat.get(c["id"], 0),
                })
                if quota[c["id"]] > pool_by_cat.get(c["id"], 0):
                    short.append(c["name"])
                    short_detail.append("%s 配 %d / 可用 %d"
                                        % (c["name"], quota[c["id"]], pool_by_cat.get(c["id"], 0)))
            if short:
                warnings.append("以下分类配额超过题库可用量（多出的会向其他分类借位）："
                                + "，".join(short_detail))

    # 必考锁定
    must = (cfg["rules"].get("must") or {})
    must_ids = []
    if must.get("enabled"):
        must_ids = [int(i) for i in (must.get("ids") or [])]
        found = {q["id"]: q for q in pool}
        miss = [i for i in must_ids if i not in found]
        if miss:
            warnings.append("有 %d 道锁定题目不在可用范围内（可能被排除规则过滤）" % len(miss))
        for t in TYPES:
            need = sum(1 for i in must_ids if i in found and found[i]["qtype"] == t)
            row = next((b for b in by_type if b["qtype"] == t), None)
            if need and not row:
                errors.append("锁定了%s，但该题型未启用" % TYPE_NAMES[t])
            elif row and need > row["count"]:
                errors.append(
                    "锁定的%s有 %d 道，超过该题型题量 %d" % (TYPE_NAMES[t], need, row["count"])
                )

    if total_score and abs(total_score - 100) > 0.01:
        warnings.append("卷面总分 %.1f 分，与标准 100 分不一致" % total_score)

    if total_n and cfg.get("duration"):
        per = float(cfg["duration"]) * 60 / total_n
        if per < 40:
            warnings.append(
                "%d 题 / %s 分钟，平均每题 %.0f 秒，问答题可能写不完" % (total_n, cfg["duration"], per)
            )

    if gaps:
        errors.append("题库题量不足，共缺 %d 题" % sum(g["lack"] for g in gaps))

    data = {
        "ok": not errors,
        "mode": cfg["mode"],
        "title": cfg.get("title"),
        "total_count": total_n,
        "total_score": round(total_score, 2),
        "target_score": cfg.get("target_score", 100),
        "by_type": by_type,
        "diff": {
            "mode": cfg.get("diff_mode"),
            "target": target,
            "points": points,
            "min": pmin,
            "max": pmax,
            "feasible": feasible,
            "items": diff_items,
            "ratio": cfg.get("diff_ratio"),
        },
        "quota": {"mode": cfg.get("quota_mode"), "items": quota_items},
        "diff_recommend": cfg.get("diff_recommend"),
        "diff_auto": bool(cfg.get("diff_target_auto")),
        "errors": errors,
        "warnings": warnings,
        "gaps": gaps,
    }
    if own:
        conn.close()
    return data


# ---------------------------------------------------------------- 抽题

def _draw(rows, plan, quota, pool, must_ids, cfg):
    """按题型→难度抽题，分类配额做软约束。"""
    cell = defaultdict(list)
    by_id = {}
    for q in pool:
        by_id[q["id"]] = q
        cell[(q["qtype"], q["cat_id"], q["diff"])].append(q)

    picked = []
    cat_taken = defaultdict(int)
    plan_left = {t: list(c) for t, c in plan.items()}
    new_days = (cfg["rules"].get("scope") or {}).get("new_days") or 0
    new_ratio = (cfg["rules"].get("scope") or {}).get("new_ratio") or 0
    total_n = sum(r["count"] for r in rows)
    new_quota = int(round(total_n * new_ratio / 100.0)) if (new_days and new_ratio) else 0
    new_taken = 0
    used = set()

    def take(q):
        nonlocal new_taken
        picked.append(q)
        used.add(q["id"])
        cat_taken[q["cat_id"]] += 1
        if _is_new(q, new_days):
            new_taken += 1

    # 1) 必考锁定优先占位；该难度没余量时占用同题型其他难度的名额
    for q in pool:
        if q["id"] in must_ids and q["id"] not in used:
            t = q["qtype"]
            if t not in plan_left:
                continue
            idx = {"easy": 0, "mid": 1, "hard": 2}.get(q["diff"], 1)
            if plan_left[t][idx] > 0:
                plan_left[t][idx] -= 1
            else:
                spare = [i for i in (0, 1, 2) if i != idx and plan_left[t][i] > 0]
                if not spare:
                    continue
                plan_left[t][spare[0]] -= 1
            take(q)

    # 2) 逐题型、逐难度抽取
    for r in rows:
        t = r["qtype"]
        for di, d in enumerate(DIFFS):
            k = plan_left[t][di]
            guard = 0
            while k > 0 and guard < 10000:
                guard += 1
                cands = []
                for cid in (quota or {}):
                    bucket = [q for q in cell[(t, cid, d)] if q["id"] not in used]
                    if not bucket:
                        continue
                    cap = quota[cid] if quota else None
                    full = cap is not None and cat_taken[cid] >= cap
                    cands.append((full, cat_taken[cid] / max(1.0, (cap or 1)), cid, bucket))
                if not cands and not quota:
                    all_bucket = [q for key, lst in cell.items()
                                  if key[0] == t and key[2] == d for q in lst if q["id"] not in used]
                    if not all_bucket:
                        break
                    q = random.choice(all_bucket)
                    take(q)
                    k -= 1
                    continue
                if not cands:
                    break
                cands.sort(key=lambda x: (x[0], x[1]))
                _, _, cid, bucket = cands[0]
                if new_quota and new_taken < new_quota:
                    fresh = [q for q in bucket if _is_new(q, new_days)]
                    q = random.choice(fresh or bucket)
                else:
                    q = random.choice(bucket)
                take(q)
                k -= 1
    return picked, cat_taken


def _paper_no(conn, prefix):
    today = date.today().strftime("%Y%m%d")
    pat = "%s-%s-%%" % (prefix, today)
    row = conn.execute(
        "SELECT no FROM paper WHERE no LIKE ? ORDER BY no DESC LIMIT 1", (pat,)
    ).fetchone()
    seq = 1
    if row:
        try:
            seq = int(row["no"].rsplit("-", 1)[1]) + 1
        except Exception:
            seq = 1
    return "%s-%s-%03d" % (prefix, today, seq)


def build(cfg):
    conn = core.connect()
    try:
        cfg = _norm(cfg)
        pre = precheck(cfg, conn)
        if not pre["ok"]:
            return {"ok": False, "errors": pre["errors"], "precheck": pre}

        rows = active_rows(cfg)
        cats = core.categories_tree(conn)
        total_n = sum(r["count"] for r in rows)
        quota = None
        if cfg["mode"] in ("quota", "custom"):
            quota, err = resolve_quota(cfg, cats, total_n)
            if err:
                return {"ok": False, "errors": [err], "precheck": pre}

        pool = load_pool(cfg, conn)
        avail_by = defaultdict(dict)
        for q in pool:
            avail_by[q["qtype"]][q["diff"]] = avail_by[q["qtype"]].get(q["diff"], 0) + 1
        plan, points, target, bad = diff_plan(rows, cfg, avail_by)
        if plan is None:
            return {
                "ok": False,
                "errors": ["%s 的难度构成在题库中凑不出，请调整题量或补充题目"
                           % TYPE_NAMES.get(bad or "", bad or "")],
                "precheck": pre,
            }

        must_ids = set()
        if (cfg["rules"].get("must") or {}).get("enabled"):
            must_ids = {int(i) for i in (cfg["rules"]["must"].get("ids") or [])}
        picked, cat_taken = _draw(rows, plan, quota, pool, must_ids, cfg)

        if len(picked) < total_n:
            got = defaultdict(int)
            for q in picked:
                got[q["qtype"]] += 1
            missing = [
                "%s 缺 %d 题" % (TYPE_NAMES[r["qtype"]], r["count"] - got.get(r["qtype"], 0))
                for r in rows if got.get(r["qtype"], 0) < r["count"]
            ]
            return {
                "ok": False,
                "errors": ["抽题未完成：" + "；".join(missing) + "（可能被分类配额或难度分布限制）"],
                "precheck": pre,
            }

        # 按题型分组、卷内排序：先题型，再易→难
        order = {t: i for i, t in enumerate(TYPES)}
        picked.sort(key=lambda q: (order[q["qtype"]], DIFFS.index(q["diff"]), q["id"]))

        settings = core.all_settings(conn)
        prefix = core.get_setting("paper_prefix", conn) or "ZJLab"
        no = _paper_no(conn, prefix)
        ts = core.now_str()

        items = []
        total_score = 0.0
        for q in picked:
            score = core.question_score(q, settings)
            # 题型统一分值覆盖题目建议分值
            row = next((r for r in rows if r["qtype"] == q["qtype"]), None)
            if row:
                score = row["score"] * (q["blanks"] or 1) if q["qtype"] == "fill" else row["score"]
            total_score += score
            items.append({
                "qid": q["id"], "qtype": q["qtype"], "diff": q["diff"],
                "stem": q["stem"], "options": q["options"], "answer": q["answer"],
                "analysis": q["analysis"], "blanks": q["blanks"], "score": score,
            })

        diff_score = sum(DIFF_POINTS[i["qtype"]][i["diff"]] for i in items)

        # 把配额执行情况写进快照（FR-AG-20）：配置是"打算抽多少"（留空的分类均分后的
        # 结果也算配置），实际是"真抽到多少"，两者都要留痕，否则过几天再翻这套卷，
        # 说不清当时配额执行得怎么样。
        if quota:
            cfg["cat_quota"] = {str(k): int(v) for k, v in quota.items()}
            cfg["cat_actual"] = {str(k): int(v) for k, v in cat_taken.items()}

        cur = conn.execute(
            """INSERT INTO paper(no, title, unit, duration, total_score, q_count,
               diff_score, config, seed, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (no, cfg.get("title") or "机房运维考试", cfg.get("unit") or "",
             int(cfg.get("duration") or 45), round(total_score, 2), len(items),
             diff_score, json.dumps(cfg, ensure_ascii=False), random.randint(1, 10 ** 9), ts),
        )
        pid = cur.lastrowid
        for seq, it in enumerate(items, 1):
            conn.execute(
                """INSERT INTO paper_item(paper_id, seq, qid, qtype, diff, stem, options,
                   answer, analysis, blanks, score) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, seq, it["qid"], it["qtype"], it["diff"], it["stem"],
                 json.dumps(it["options"], ensure_ascii=False), it["answer"],
                 it["analysis"], it["blanks"], it["score"]),
            )
        conn.executemany(
            "UPDATE question SET used_count=used_count+1, last_used=? WHERE id=?",
            [(ts, it["qid"]) for it in items],
        )
        conn.commit()
        warns = list(pre["warnings"])
        if quota:
            names = {c["id"]: c["name"] for c in cats}
            off = [
                "%s %+d" % (names.get(k, k), cat_taken.get(k, 0) - v)
                for k, v in quota.items() if cat_taken.get(k, 0) != v
            ]
            if off:
                warns.append("分类配额实际分布有偏差：" + "，".join(off))
        if target is not None and diff_score != target:
            warns.append("难度积分实际 %d，与目标 %s 相差 %+d（锁定题或可用题限制所致）"
                         % (diff_score, target, diff_score - target))
        return {
            "ok": True, "id": pid, "no": no,
            "total_score": round(total_score, 2), "q_count": len(items),
            "diff_score": diff_score,
            "cat_taken": dict(cat_taken),
            "quota": quota or {},
            "warnings": warns,
            "diff_recommended": cfg.get("diff_recommend") if cfg.get("diff_target_auto") else None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------- 试卷读写

def get_paper(pid, conn=None):
    own = conn is None
    conn = conn or core.connect()
    row = conn.execute("SELECT * FROM paper WHERE id=?", (pid,)).fetchone()
    if not row:
        if own:
            conn.close()
        return {"ok": False, "error": "试卷不存在"}
    items = []
    for r in conn.execute(
        "SELECT * FROM paper_item WHERE paper_id=? ORDER BY seq", (pid,)
    ):
        d = dict(r)
        d["options"] = json.loads(d["options"] or "[]")
        d["diff_name"] = DIFF_NAMES.get(d["diff"], "")
        d["type_name"] = TYPE_NAMES.get(d["qtype"], "")
        d["answer_list"] = [x for x in (d["answer"] or "").split("|")] if d["qtype"] == "fill" else []
        items.append(d)
    data = dict(row)
    data["config"] = json.loads(data["config"] or "{}")
    data["items"] = items
    data["sections"] = _sections(items)
    if own:
        conn.close()
    return {"ok": True, "paper": data}


def _sections(items):
    secs = []
    for t in TYPES:
        group = [i for i in items if i["qtype"] == t]
        if not group:
            continue
        for idx, i in enumerate(group, 1):
            i["no_in_type"] = idx
        secs.append({
            "qtype": t, "name": TYPE_NAMES[t], "count": len(group),
            "subtotal": round(sum(i["score"] for i in group), 2),
            "items": group,
        })
    return secs


def list_papers(params):
    conn = core.connect()
    page = max(1, int(params.get("page") or 1))
    size = min(100, int(params.get("size") or 20))
    kw = (params.get("kw") or "").strip()
    where, args = ["1=1"], []
    if kw:
        where.append("(no LIKE ? OR title LIKE ?)")
        args += ["%" + kw + "%", "%" + kw + "%"]
    w = " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) c FROM paper WHERE " + w, args).fetchone()["c"]
    rows = conn.execute(
        "SELECT id, no, title, unit, duration, total_score, q_count, diff_score, created_at "
        "FROM paper WHERE " + w + " ORDER BY id DESC LIMIT ? OFFSET ?",
        args + [size, (page - 1) * size],
    ).fetchall()
    conn.close()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


def delete_paper(pid):
    conn = core.connect()
    conn.execute("DELETE FROM paper_item WHERE paper_id=?", (pid,))
    conn.execute("DELETE FROM paper WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return {"ok": True}


def _recalc(conn, pid):
    total = conn.execute(
        "SELECT COALESCE(SUM(score),0) s FROM paper_item WHERE paper_id=?", (pid,)
    ).fetchone()["s"]
    conn.execute("UPDATE paper SET total_score=? WHERE id=?", (round(total, 2), pid))
    return round(total, 2)


def set_item_score(pid, item_id, score):
    conn = core.connect()
    row = conn.execute(
        "SELECT * FROM paper_item WHERE id=? AND paper_id=?", (item_id, pid)
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "题目不存在"}
    score = float(score)
    if score < 0:
        conn.close()
        return {"ok": False, "error": "分值不能为负"}
    conn.execute("UPDATE paper_item SET score=? WHERE id=?", (score, item_id))
    total = _recalc(conn, pid)
    conn.commit()
    conn.close()
    return {"ok": True, "total_score": total}


def balance(pid, target=100):
    """一键配平：优先调问答题、填空，单题不低于 1 分。"""
    conn = core.connect()
    items = conn.execute(
        "SELECT id, qtype, score FROM paper_item WHERE paper_id=? ORDER BY seq", (pid,)
    ).fetchall()
    if not items:
        conn.close()
        return {"ok": False, "error": "试卷为空"}
    total = sum(i["score"] for i in items)
    diff = round(float(target) - total, 2)
    changes = []
    order = sorted(items, key=lambda i: BALANCE_ORDER.get(i["qtype"], 9))
    # 优先把差额集中到分值高的题上：同等类型内均摊，一次到位
    rank = {}
    for it in order:
        rank.setdefault(it["qtype"], []).append(it)
    for qtype in sorted(rank, key=lambda t: BALANCE_ORDER.get(t, 9)):
        group = rank[qtype]
        for pos, it in enumerate(group):
            if abs(diff) <= 0.01:
                break
            cur = conn.execute("SELECT score FROM paper_item WHERE id=?", (it["id"],)).fetchone()["score"]
            share = round(diff / (len(group) - pos), 1)
            if abs(share) < 0.5:
                share = 0.5 if diff > 0 else -0.5
            newv = cur + share
            if newv < 1:
                newv = 1
            newv = round(newv, 1)
            if abs(newv - cur) < 0.01:
                continue
            conn.execute("UPDATE paper_item SET score=? WHERE id=?", (newv, it["id"]))
            changes.append({"id": it["id"], "from": cur, "to": newv})
            diff = round(diff - (newv - cur), 2)
    total = _recalc(conn, pid)
    conn.commit()
    conn.close()
    return {"ok": abs(diff) < 0.01, "total_score": total, "changes": changes[:40],
            "left": round(diff, 2)}


def update_paper(pid, payload):
    conn = core.connect()
    fields, args = [], []
    for k in ("title", "unit"):
        if k in payload:
            fields.append(f"{k}=?")
            args.append(payload[k])
    if "duration" in payload:
        fields.append("duration=?")
        args.append(int(payload["duration"]))
    if fields:
        args.append(pid)
        conn.execute("UPDATE paper SET " + ",".join(fields) + " WHERE id=?", args)
        conn.commit()
    conn.close()
    return {"ok": True}
