"""Offline smoke check for run attribution and gate event logging."""
import argparse
import json
import tempfile
from pathlib import Path

import autoline
from pipeline import llm


def main():
    short_script = autoline.make_state("script", "failed", error_type="script_gate",
                                       evidence={"bad": ["字数 538 超出目标"]})
    assert autoline._supervise_rules(short_script)["action"] == "restrategy"
    weak_footage = autoline.make_state("footage", "ok",
                                       evidence={"clips": 2, "total": 5,
                                                 "low_conf": [{"index": 1}] * 3})
    assert autoline._supervise_rules(weak_footage)["action"] == "continue"
    titles = {name: {"titles": [f"BLG零封DK：{hook}"]}
              for name, hook in (("bilibili", "上路爆发"),
                                 ("xigua_douyin", "野区压制"),
                                 ("shipinhao", "下路反击"))}
    assert not autoline.title_lint({"topic": "BLG零封DK", "platforms": titles})
    for value in titles.values():
        value["titles"] = ["BLG零封DK：全线碾压"]
    assert autoline.title_lint({"topic": "BLG零封DK", "platforms": titles})
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        original_output = llm.OUTPUT_DIR
        llm.OUTPUT_DIR = workdir
        calls = 0

        def stage(attempt, _actions, _hint):
            nonlocal calls
            calls += 1
            llm._usage_record("synthetic", "input", "", "output",
                              {"input_tokens": 2, "output_tokens": 3})
            if attempt == 1:
                return autoline.make_state("script", "failed", error_type="TimeoutError")
            return autoline.make_state("script", "ok")

        try:
            with llm.usage_context(run_id="offline_audit"), \
                 autoline.experiment_io(workdir=workdir, stats_enabled=False), \
                 autoline.experience_context(enabled=False):
                autoline.run_stage("script", stage, workdir, False, False,
                                   {"fact_basis": "synthetic fact"})
            ledger = [json.loads(line) for line in (workdir / "token_ledger.jsonl").read_text(encoding="utf-8").splitlines()]
            events = [json.loads(line) for line in (workdir / "checkpoint_events.jsonl").read_text(encoding="utf-8").splitlines()]
            assert calls == 2
            assert ledger[0]["run_id"] == "offline_audit"
            assert ledger[0]["stage"] == "script"
            assert ledger[0]["purpose"] == "gen"
            assert ledger[1]["purpose"] == "rework"
            assert events[0]["run_id"] == "offline_audit"
            assert [event["action"] for event in events] == ["retry", "continue"]
            assert events[0]["stage"] == "script"
            assert (workdir / "hindsight_log.jsonl").exists()
            assert not (workdir / "decision_stats.json").exists()
        finally:
            llm.OUTPUT_DIR = original_output
    print("offline attribution audit passed")


if __name__ == "__main__":
    main()
