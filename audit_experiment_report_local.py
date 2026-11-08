"""Offline check that E0 reporting retains incomplete and failed runs."""
import json
import tempfile
from pathlib import Path

import experiment_report
from experiment_setup import prepare


def main():
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        seed = base / "seed.json"
        seed.write_text(json.dumps({"topic_title": "审计题", "fact_basis": "已核事实"},
                                   ensure_ascii=False), encoding="utf-8")
        root = base / "study"
        prepare(seed, root, runs=1)
        workdir = root / "A1" / "run_01"
        (workdir / "run_fake.json").write_text(json.dumps({
            "run_id": "fake", "arm": "A1", "exit_code": 2}), encoding="utf-8")
        output = base / "output"
        output.mkdir()
        (output / "token_ledger.jsonl").write_text(json.dumps({
            "run_id": "fake", "layer": "program", "purpose": "gen",
            "tokens_in": 10, "tokens_out": 5}) + "\n", encoding="utf-8")
        original = experiment_report.OUTPUT_DIR
        experiment_report.OUTPUT_DIR = output
        try:
            rows = experiment_report.summarize(root)
        finally:
            experiment_report.OUTPUT_DIR = original
        assert len(rows) == 4
        assert rows[0]["status"] == "failed" and rows[0]["exit_code"] == 2
        assert rows[0]["token_total"] == 15
        assert all(row["status"] == "not_started" for row in rows[1:])
    print("experiment report audit passed")


if __name__ == "__main__":
    main()
