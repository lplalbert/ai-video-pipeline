#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge_agreement.py — 自动评委间一致率分析（2026-11-08）。

输入 judge_v1/scores/<sid>.<judge>.json（pro/flash/gpt/deepseek* 同格式），
输出评委两两对照：Q2 三分类逐条一致率+Cohen κ、Q1 句级「有问题判词」二分类
一致率+κ（各评委拆判词粒度不同，句级坍缩才可比）、Q4/Q5 评分均差与全同率、
各 sid 汇总分相关（Spearman）。产物：agreement_report.json + 控制台表。

红线口径不变：以上均为「自动评审」间一致性，不冒充人评、不代表人类信度。
用法（项目根）：
    python -X utf8 judge_agreement.py [--judges deepseek,deepseek-flash]
        [--dir output/_research_audit/judge_v1]
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
from collections import Counter, defaultdict
from pathlib import Path


def kappa(pairs: list[tuple]) -> tuple[float, float]:
    """pairs=[(a,b),...]；返回 (percent_agreement, cohen_kappa)。"""
    n = len(pairs)
    if not n:
        return float("nan"), float("nan")
    po = sum(1 for a, b in pairs if a == b) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] / n * cb[c] / n for c in set(ca) | set(cb))
    k = (po - pe) / (1 - pe) if pe < 1 else float("nan")
    return round(po, 3), round(k, 3)


def spearman(xs: list[float], ys: list[float]) -> float | None:
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for t in range(i, j + 1):
                r[order[t]] = avg
            i = j + 1
        return r
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def load_scores(d: Path, judges: list[str]) -> dict:
    out = defaultdict(dict)  # sid -> judge -> data
    for f in sorted(d.glob("*.json")):
        if "error" in f.name or f.name.startswith("_"):
            continue
        sid, _, judge = f.name[:-5].rpartition(".")
        if judge in judges:
            out[sid][judge] = json.loads(f.read_text(encoding="utf-8"))
    return out


def q1_sentence_flags(data: dict) -> dict[int, bool]:
    """句级坍缩：该句是否存在「无支撑/矛盾」判词（各评委粒度不同的可比口径）。"""
    flags = {}
    for it in (data.get("q1") or {}).get("items") or []:
        idx = it.get("index")
        if not isinstance(idx, int):
            continue
        bad = any(c.get("verdict") in ("无支撑", "矛盾") for c in it.get("claims") or [])
        flags[idx] = flags.get(idx, False) or bad
    return flags


def pair_analysis(scores: dict, ja: str, jb: str) -> dict:
    q2_pairs, q1_pairs, q4_diffs, q5_diffs = [], [], [], []
    q1_sup_a, q1_sup_b, q2_h_a, q2_h_b = [], [], [], []
    n_common = 0
    for sid, byj in scores.items():
        if ja not in byj or jb not in byj:
            continue
        n_common += 1
        da, db = byj[ja], byj[jb]
        # Q2 逐条三分类
        ia = {it.get("index"): it.get("verdict") for it in (da.get("q2") or {}).get("items") or []}
        ib = {it.get("index"): it.get("verdict") for it in (db.get("q2") or {}).get("items") or []}
        for idx in set(ia) & set(ib):
            if ia[idx] in ("匹配", "弱匹配", "错位") and ib[idx] in ("匹配", "弱匹配", "错位"):
                q2_pairs.append((ia[idx], ib[idx]))
        # Q1 句级二分类
        fa, fb = q1_sentence_flags(da), q1_sentence_flags(db)
        for idx in set(fa) & set(fb):
            q1_pairs.append((fa[idx], fb[idx]))
        # 汇总分
        for src, dst in ((da, q1_sup_a), (db, q1_sup_b)):
            r = (src.get("q1") or {}).get("support_rate")
            if r is not None:
                dst.append(r)
        for src, dst in ((da, q2_h_a), (db, q2_h_b)):
            r = (src.get("q2") or {}).get("headline_q2")
            if r is not None:
                dst.append(r)
        # Q4 按 (platform) 对齐 3 维
        pa = {t.get("platform"): t for t in (da.get("q4") or {}).get("titles") or []}
        pb = {t.get("platform"): t for t in (db.get("q4") or {}).get("titles") or []}
        for plat in set(pa) & set(pb):
            for dim in ("information", "accuracy", "appeal"):
                va, vb = pa[plat].get(dim), pb[plat].get(dim)
                if isinstance(va, int) and isinstance(vb, int):
                    q4_diffs.append((va, vb))
        # Q5 3 维
        for dim in ("flow", "density", "fact_boundary"):
            va, vb = (da.get("q5") or {}).get(dim), (db.get("q5") or {}).get(dim)
            if isinstance(va, int) and isinstance(vb, int):
                q5_diffs.append((va, vb))

    def dim_stats(diffs):
        if not diffs:
            return {}
        mad = sum(abs(a - b) for a, b in diffs) / len(diffs)
        exact = sum(1 for a, b in diffs if a == b) / len(diffs)
        within1 = sum(1 for a, b in diffs if abs(a - b) <= 1) / len(diffs)
        return {"n": len(diffs), "mean_abs_diff": round(mad, 3),
                "exact_rate": round(exact, 3), "within1_rate": round(within1, 3)}

    po_q2, k_q2 = kappa(q2_pairs)
    po_q1, k_q1 = kappa(q1_pairs)
    return {
        "judges": [ja, jb], "n_common_sids": n_common,
        "q2_clip_verdicts": {"n": len(q2_pairs), "percent_agreement": po_q2, "cohen_kappa": k_q2},
        "q1_sentence_anyissue": {"n": len(q1_pairs), "percent_agreement": po_q1, "cohen_kappa": k_q1},
        "q1_support_rate_spearman": spearman(q1_sup_a, q1_sup_b),
        "q2_headline_spearman": spearman(q2_h_a, q2_h_b),
        "q4_title_scores": dim_stats(q4_diffs),
        "q5_script_scores": dim_stats(q5_diffs),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="自动评委间一致率分析（非人评）")
    ap.add_argument("--dir", type=Path, default=Path("output/_research_audit/judge_v1"))
    ap.add_argument("--judges", default="auto", help="auto=用 scores/ 里实际出现的评委")
    args = ap.parse_args()
    sdir = args.dir / "scores"
    if args.judges == "auto":
        judges = sorted({f.name[:-5].rpartition(".")[2] for f in sdir.glob("*.json")
                         if "error" not in f.name and not f.name.startswith("_")})
    else:
        judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    scores = load_scores(sdir, judges)
    print(f"评委: {judges}；有评分的 sid: {len(scores)}")
    report = []
    for ja, jb in itertools.combinations(judges, 2):
        r = pair_analysis(scores, ja, jb)
        report.append(r)
        print(f"\n== {ja} vs {jb}（共同 sid {r['n_common_sids']}）==")
        for key in ("q2_clip_verdicts", "q1_sentence_anyissue"):
            m = r[key]
            print(f"  {key}: n={m['n']} 一致率={m['percent_agreement']} κ={m['cohen_kappa']}")
        print(f"  Q1支持率 Spearman={r['q1_support_rate_spearman']}  Q2落点 Spearman={r['q2_headline_spearman']}")
        for key in ("q4_title_scores", "q5_script_scores"):
            print(f"  {key}: {r[key]}")
    out = args.dir / "agreement_report.json"
    out.write_text(json.dumps({"judges": judges, "pairs": report}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"\n[agreement] 报告 → {out}（口径：自动评审间一致性，非人类信度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
