"""Run one isolated script-stage audit using an existing frozen topic seed.

This is a development audit, never a paper sample. It does not publish media.
"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

import autoline
from pipeline.llm import usage_context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path, help="Existing workdir with 选题.json")
    parser.add_argument("--arch", choices=("program", "full-agent"), default="program")
    parser.add_argument("--stage", choices=("script", "footage", "kit"), default="script")
    parser.add_argument("--audit-workdir", type=Path,
                        help="Continue an existing isolated audit directory")
    args = parser.parse_args()
    seed_path = args.source / "选题.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    topic = seed.get("topic_title") or args.source.name.split("_", 1)[-1]
    workdir = args.audit_workdir or autoline.OUTPUT_DIR / "_research_audit" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.audit_workdir:
        if not (workdir / "audit_meta.json").exists():
            parser.error("--audit-workdir must point to a prior audit")
    else:
        workdir.mkdir(parents=True)
        shutil.copy2(seed_path, workdir / "选题.json")
    topic_info = {"topic_title": topic, **seed}
    options = argparse.Namespace(format="热点评论", minutes=3.5, no_polish=False,
                                 engine="mimo", publish=False)
    run_id = "audit_" + args.arch.replace("-", "_") + "_" + workdir.name
    if not args.audit_workdir:
        (workdir / "audit_meta.json").write_text(json.dumps({
            "run_id": run_id, "source": str(args.source), "arch": args.arch,
            "purpose": "development_audit_not_paper_sample",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        meta = json.loads((workdir / "audit_meta.json").read_text(encoding="utf-8"))
        if meta["run_id"] != run_id or meta["arch"] != args.arch:
            parser.error("audit workdir architecture does not match")
    stages = autoline.make_stages(options, topic_info, workdir)
    if args.arch == "full-agent":
        from full_agent import make_stages as make_full_agent_stages
        stages = make_full_agent_stages(options, topic_info, workdir, stages)
    with usage_context(run_id=run_id):
        autoline.run_stage(args.stage, stages[args.stage], workdir, False, False, topic_info,
                           policy_override="auto" if args.arch == "full-agent" else None)
    print(f"AUDIT_RUN_ID={run_id}")
    print(f"AUDIT_WORKDIR={workdir}")


if __name__ == "__main__":
    main()
