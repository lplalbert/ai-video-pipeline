"""Offline smoke check for fault_injector: deterministic schedule, exception
injection through run_stage, semantic mutations. No model or network calls."""
import argparse
import json
import tempfile
from pathlib import Path

import autoline
import fault_injector as fi


def main():
    plan = {"seed": 20261107, "rate": 0.0,
            "explicit": [{"stage": "script", "attempt": 1, "type": "TimeoutError"},
                         {"stage": "kit", "attempt": 1, "type": "title_violation"}]}
    # 1) 确定性 + explicit 优先
    d1 = fi.schedule_for(plan, "script", 1)
    d2 = fi.schedule_for(plan, "script", 1)
    assert d1 == d2 and d1[0] == "exception" and d1[1] == "TimeoutError", d1
    assert fi.schedule_for(plan, "script", 2) is None
    rate_plan = {"seed": 7, "rate": 1.0, "pools": {"script": ["JSONDecodeError", "InjectedUnknownFault"]}}
    s1 = fi.schedule_for(rate_plan, "script", 1)
    s2 = fi.schedule_for(rate_plan, "script", 1)
    assert s1 == s2 and s1[1] in ("JSONDecodeError", "InjectedUnknownFault")
    assert fi.schedule_for(rate_plan, "footage", 1) is None

    # 2) 异常注入走 run_stage 重试链（attempt1 注入 → attempt2 成功）
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        orig = fi.install(plan, workdir / "fault_injections.jsonl")
        calls = 0

        def stage(attempt, _actions, _hint):
            nonlocal calls
            calls += 1
            return autoline.make_state("script", "ok")

        try:
            with autoline.experiment_io(workdir=workdir, stats_enabled=False), \
                 autoline.experience_context(enabled=False):
                autoline.run_stage("script", stage, workdir, False, False,
                                   {"fact_basis": "synthetic"}, policy_override="auto")
        finally:
            autoline.run_stage = orig
        # attempt1 被注入异常整体顶替（真实瞬态失败也没有产物），fn 只在 attempt2 真跑
        assert calls == 1, calls
        log = [json.loads(x) for x in
               (workdir / "fault_injections.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(log) == 1 and log[0]["type"] == "TimeoutError", log

    # 3) 语义注入：fact_swap 改数字不改句数
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        (workdir / "文稿.json").write_text(json.dumps({
            "hook": "BLG 3比0 DK，Bin 8分钟4头。", "sections": [], "cta": "怎么看？",
            "titles": ["t"], "tags": [], "fact_check_notes": []}, ensure_ascii=False),
            encoding="utf-8")
        detail = fi.apply_semantic("fact_swap", workdir)
        script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
        assert script["hook"] == "BLG 3比0 DK，Bin 8分钟5头。", (script["hook"], detail)
        assert len(script["hook"]) == len("BLG 3比0 DK，Bin 8分钟4头。")

    # 4) 语义注入：title_violation 加结构词
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        (workdir / "投稿包.json").write_text(json.dumps({
            "video_file": "x.mp4",
            "platforms": {"bilibili": {"titles": ["三比零零封！BLG全线碾压DK"]}}}),
            encoding="utf-8")
        fi.apply_semantic("title_violation", workdir)
        kit = json.loads((workdir / "投稿包.json").read_text(encoding="utf-8"))
        assert kit["platforms"]["bilibili"]["titles"][0].startswith("关键看点一：")

    # 5) 未知故障类型名对齐（逼监督 LLM 低置信通道）
    exc = fi.exception_for("InjectedUnknownFault", "t")
    assert type(exc).__name__ == "InjectedUnknownFault"
    exc2 = fi.exception_for("llm_bad_json", "t")
    assert type(exc2).__name__ == "llm_bad_json"

    # 6) F7 回归（2026-11-07）：gate 路径未知故障重试预算耗尽必须升级给人，
    #    不能被低置信 retry 回退无限重试（非监督 arm 的潜在死循环）
    with tempfile.TemporaryDirectory() as temporary:
        workdir = Path(temporary)
        calls = 0

        def stage_always_fail(attempt, _actions, _hint=""):
            nonlocal calls
            calls += 1
            raise fi.exception_for("InjectedUnknownFault", "always")

        with autoline.experiment_io(workdir=workdir, stats_enabled=False), \
             autoline.experience_context(enabled=False):
            try:
                autoline.run_stage("script", stage_always_fail, workdir, False, False,
                                   {"fact_basis": "synthetic"})
                raise AssertionError("预算耗尽应升级（RuntimeError），未抛出")
            except RuntimeError as exc:
                assert "升级给人" in str(exc), exc
        assert calls == 2, calls  # 恰好 MAX_ATTEMPTS 次，不多烧
        rows = [json.loads(x) for x in
                (workdir / "hindsight_log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["action"] == "escalate" and rows[-1]["final"] == "escalated", rows[-1]
    print("fault injector offline audit passed")


if __name__ == "__main__":
    main()
