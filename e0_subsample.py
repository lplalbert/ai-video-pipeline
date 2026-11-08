#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e0_subsample.py — E0 质量轴子样本机械选样（预登记规则的代码实现）。

预登记规则（《pilot全量汇总与v1.1修订_20261108.md》§4，E0 开跑前写死）：
  每 (题, 臂) 取 execution_order 最靠前的、产物完整（质量轴口径）的 run，
  共 36 题 × 4 臂 = 144 条；某格 3 run 全部产物不完整则该格无质量轴样本
  （缺即缺，不补不换）；选样纯机械不看内容。

产物完整口径与评委链路同源：judge_panel.load_artifacts 能加载
（文稿.json + 选题.json + 投稿包.json + 裁片映射），失败即不完整。

产物（--out 目录）：
  subsample.json   选样结果 + 审计信息（规则文本、seed 哈希、eo 位置）
  subsample.tsv    逐格明细
  judge_cmd.txt    预生成的 judge_panel 调用行（pro+flash，独立 --out-dir）

用法（项目根）：
    python -X utf8 e0_subsample.py select --root output/_research_audit/e0_v1 \\
        --out output/_research_audit/judge_e0
红线：选样不含任何内容判断；本脚本产出为「自动评审」链路输入，非人评。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from judge_panel import load_artifacts  # 与评委链路同一完整性口径

RULE_TEXT = ("每 (题, 臂) 取 execution_order 最靠前的、产物完整的 run；"
             "3 run 全不完整则该格缺即缺；选样纯机械不看内容")


def quality_complete(wd: Path) -> tuple[bool, str]:
    if not (wd / "文稿.json").exists():
        return False, "无文稿.json（未成型，只进失败率轴）"
    try:
        art = load_artifacts(wd)
    except Exception as exc:
        return False, f"load_artifacts 失败：{exc}"
    if not art.get("sentences"):
        return False, "文稿句列表为空"
    return True, ""


def select(root: Path, out: Path) -> int:
    topics = sorted(root.glob("topic_*/experiment_manifest.json"))
    if not topics:
        print(f"错误：{root} 下无 topic_*/experiment_manifest.json", file=sys.stderr)
        return 2
    rows, missing = [], []
    for mf_path in topics:
        mf = json.loads(mf_path.read_text(encoding="utf-8"))
        topic_dir = mf_path.parent.name
        order = mf.get("execution_order") or list(range(len(mf["workdirs"])))
        eo_pos = {i: pos for pos, i in enumerate(order)}
        for arm in mf["arms"]:
            cands = [w for w in mf["workdirs"] if w["arm"] == arm]
            # workdirs 清单序即 execution_order 的下标序
            cands.sort(key=lambda w: eo_pos.get(mf["workdirs"].index(w), 999))
            picked, rejects = None, []
            for w in cands:
                wd = Path(w["path"])
                if not wd.exists():
                    wd = root / topic_dir / arm / f"run_{w['run_index']:02d}"
                ok, why = quality_complete(wd)
                if ok and picked is None:
                    picked = (w, wd, eo_pos.get(mf["workdirs"].index(w)))
                elif not ok:
                    rejects.append(f"run_{w['run_index']:02d}:{why}")
            if picked is None:
                missing.append({"topic_dir": topic_dir, "arm": arm, "reasons": rejects})
                rows.append({"topic_dir": topic_dir, "topic_title": mf["topic_title"],
                             "arm": arm, "run_index": None, "workdir": None,
                             "eo_pos": None, "status": "missing(缺即缺)"})
            else:
                w, wd, eop = picked
                rows.append({"topic_dir": topic_dir, "topic_title": mf["topic_title"],
                             "arm": arm, "run_index": w["run_index"], "workdir": str(wd),
                             "eo_pos": eop, "status": "selected",
                             "rejected": rejects})
    sel = [r for r in rows if r["status"] == "selected"]
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "rule": RULE_TEXT,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "root": str(root),
        "n_topics": len(topics), "n_cells": len(rows),
        "n_selected": len(sel), "n_missing": len(missing),
        "missing_cells": missing,
        "seed_sha256": {p.parent.name: json.loads(p.read_text(encoding="utf-8")).get("seed_sha256")
                        for p in topics},
        "rows": rows,
    }
    (out / "subsample.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    with (out / "subsample.tsv").open("w", encoding="utf-8") as fh:
        fh.write("topic_dir\tarm\trun_index\teo_pos\tstatus\tworkdir\n")
        for r in rows:
            fh.write("\t".join(str(r.get(k) or "") for k in
                               ("topic_dir", "arm", "run_index", "eo_pos", "status", "workdir")) + "\n")
    wds = " ".join(f'"{r["workdir"]}"' for r in sel)
    (out / "judge_cmd.txt").write_text(
        f"python -X utf8 judge_panel.py {wds} --judges pro,flash --out-dir {out}\n",
        encoding="utf-8")
    print(f"[select] 格 {len(rows)}＝选中 {len(sel)}＋缺 {len(missing)}"
          f"（{report['n_topics']} 题）→ {out}/subsample.json")
    for m in missing:
        print(f"  缺格 {m['topic_dir']}/{m['arm']}：{'; '.join(m['reasons']) or '无候选'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="E0 质量轴子样本机械选样（预登记规则）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("select")
    sp.add_argument("--root", type=Path, required=True, help="实验根目录（含 topic_*/）")
    sp.add_argument("--out", type=Path, required=True, help="输出目录（judge 输出同目录）")
    args = ap.parse_args()
    return select(args.root, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
