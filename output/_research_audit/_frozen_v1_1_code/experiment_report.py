"""Aggregate all planned E0 runs without silently dropping failed or missing runs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

from config import OUTPUT_DIR
from eval_harness import score


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def summarize(root: Path) -> list[dict]:
    manifest = json.loads((root / "experiment_manifest.json").read_text(encoding="utf-8"))
    ledger_by_run: dict[str, list[dict]] = {}
    for entry in jsonl(OUTPUT_DIR / "token_ledger.jsonl"):
        if entry.get("run_id"):
            ledger_by_run.setdefault(entry["run_id"], []).append(entry)
    result = []
    for planned in manifest["workdirs"]:
        workdir = Path(planned["path"])
        seed_digest = hashlib.sha256((workdir / "选题.json").read_bytes()).hexdigest()
        if seed_digest != manifest["seed_sha256"]:
            raise ValueError(f"seed changed in {workdir}")
        meta_files = sorted(workdir.glob("run_*.json"))
        if len(meta_files) > 1:
            raise ValueError(f"multiple runs in one experimental directory: {workdir}")
        meta = json.loads(meta_files[0].read_text(encoding="utf-8")) if meta_files else {}
        if meta and meta.get("arm") != planned["arm"]:
            raise ValueError(f"arm mismatch in {workdir}")
        run_id = meta.get("run_id")
        calls = ledger_by_run.get(run_id, [])
        events = [e for e in jsonl(workdir / "checkpoint_events.jsonl") if e.get("run_id") == run_id]
        intervention_events = jsonl(workdir / "human_interventions.jsonl")
        human_seconds = sum(e.get("elapsed_seconds", 0) for e in intervention_events
                            if e.get("run_id") == run_id and e.get("action") == "stop")
        evaluation = score(workdir) if meta else {"status": "not_started", "q3": None}
        tokens = lambda rows: sum((e.get("tokens_in") or 0) + (e.get("tokens_out") or 0) for e in rows)
        exit_code = meta.get("exit_code")
        status = ("failed" if exit_code not in (None, 0) else evaluation["status"])
        token_total_val = tokens(calls)
        # 长度公平性（2026-11-07 协议修正）：token 随句数/时长伸缩，跨组比较必须带归一化列
        q3 = evaluation.get("q3") or {}
        sentences = q3.get("sentence_count") or ""
        duration = ((q3.get("media") or {}).get("duration_seconds")) or ""
        result.append({"arm": planned["arm"], "run_index": planned["run_index"],
                       "workdir": str(workdir), "run_id": run_id or "",
                       "status": status, "exit_code": exit_code if exit_code is not None else "",
                       "event_count": len(events),
                       "last_action": events[-1]["action"] if events else "",
                       "token_total": token_total_val,
                       "token_program": tokens(e for e in calls if e.get("layer") == "program"),
                       "token_supervisor": tokens(e for e in calls if e.get("layer") == "supervisor"),
                       "token_rework": tokens(e for e in calls if e.get("purpose") == "rework"),
                       "human_minutes": round(human_seconds / 60, 3),
                       "sentences": sentences,
                       "duration_seconds": duration,
                       "token_per_sentence": round(token_total_val / sentences, 1) if sentences else "",
                       "token_per_second": round(token_total_val / duration, 1) if duration else "",
                       "q3_passed": q3.get("passed") if q3 else "",
                       "q2_sentence_coverage": (evaluation.get("q2") or {}).get("sentence_coverage", ""),
                       "q2_quote_seconds_ratio": (evaluation.get("q2") or {}).get("quote_seconds_ratio", "")})
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()
    rows = summarize(args.root)
    if args.csv:
        with args.csv.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
