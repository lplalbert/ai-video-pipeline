"""Record human intervention time for experiment runs.

Usage: python -X utf8 human_time.py WORKDIR STAGE start|stop --run-id ID --note TEXT
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def main():
    ap = argparse.ArgumentParser(description="Record manual intervention time")
    ap.add_argument("workdir", type=Path)
    ap.add_argument("stage", choices=("topic", "script", "footage", "produce", "kit", "publish", "eval"))
    ap.add_argument("action", choices=("start", "stop"))
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--note", default="")
    args = ap.parse_args()
    if not args.workdir.is_dir():
        ap.error("workdir must already exist")
    path = args.workdir / "human_interventions.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    now = datetime.now(timezone.utc)
    if args.action == "start":
        if any(row["stage"] == args.stage and row.get("run_id") == args.run_id and row["action"] == "start" and
               not any(end.get("start_id") == row["id"] for end in rows) for row in rows):
            ap.error(f"an intervention is already open for {args.stage}")
        row = {"id": uuid4().hex, "action": "start", "stage": args.stage,
               "run_id": args.run_id,
               "ts": now.isoformat(), "note": args.note}
    else:
        closed = {row.get("start_id") for row in rows if row["action"] == "stop"}
        starts = [row for row in rows if row["action"] == "start" and row["stage"] == args.stage
                  and row.get("run_id") == args.run_id
                  and row["id"] not in closed]
        if not starts:
            ap.error(f"no open intervention for {args.stage}")
        start = starts[-1]
        seconds = (now - datetime.fromisoformat(start["ts"])).total_seconds()
        row = {"id": uuid4().hex, "action": "stop", "stage": args.stage,
               "run_id": args.run_id,
               "ts": now.isoformat(), "start_id": start["id"],
               "elapsed_seconds": round(seconds, 3), "note": args.note}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{args.stage} {args.action}: {row['ts']}")
    if args.action == "stop":
        print(f"human_minutes={seconds / 60:.2f}")


if __name__ == "__main__":
    main()
