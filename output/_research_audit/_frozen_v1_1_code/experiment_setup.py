"""Prepare isolated E0 arm directories from one frozen, attributed topic seed."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import secrets
from pathlib import Path


ARMS = ("A1", "A2", "A3", "A4")


def prepare(seed_path: Path, root: Path, runs: int, topic_title: str = "") -> dict:
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    if topic_title:
        if seed.get("topic_title") and seed["topic_title"] != topic_title:
            raise ValueError("--topic conflicts with seed topic_title")
        seed["topic_title"] = topic_title
    if not seed.get("topic_title") or not seed.get("fact_basis"):
        raise ValueError("frozen seed needs topic_title and fact_basis")
    if runs < 1:
        raise ValueError("runs must be positive")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"experiment root is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(seed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {"topic_title": seed["topic_title"], "seed_sha256": digest,
                "runs_per_arm": runs, "arms": list(ARMS),
                "experience_store": str(root / "A4_lessons.jsonl"), "workdirs": []}
    for arm in ARMS:
        for run_index in range(1, runs + 1):
            workdir = root / arm / f"run_{run_index:02d}"
            workdir.mkdir(parents=True)
            (workdir / "选题.json").write_bytes(raw)
            manifest["workdirs"].append({"arm": arm, "run_index": run_index,
                                         "path": str(workdir), "seed_sha256": digest})
    order_seed = secrets.randbits(64)
    rng = random.Random(order_seed)
    order = []
    for run_index in range(runs):
        arm_indices = list(range(len(ARMS)))
        rng.shuffle(arm_indices)
        order.extend(arm_index * runs + run_index for arm_index in arm_indices)
    manifest["execution_order_seed"] = order_seed
    manifest["execution_order"] = order
    manifest["execution_order_rule"] = "shuffle arm order within each run index; preserve A4 learning sequence"
    (root / "experiment_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("seed", type=Path)
    ap.add_argument("root", type=Path)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--topic", default="", help="Add a topic title to a legacy seed")
    args = ap.parse_args()
    result = prepare(args.seed, args.root, args.runs, args.topic)
    print(json.dumps({"topic_title": result["topic_title"],
                      "seed_sha256": result["seed_sha256"],
                      "workdirs": len(result["workdirs"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
