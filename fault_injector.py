# -*- coding: utf-8 -*-
"""fault_injector.py —— 受控故障注入器（独立于被测系统 autoline，外部包装）。

用途（实验设计 E3/E4/E7 + A4 条件式监督验证）：
- 瞬态故障（TimeoutError / ConnectionError / JSONDecodeError / llm_bad_json）→ 验证 L1 盲重试/自愈；
- 未知故障（InjectedUnknownFault）→ 验证 A4 监督 LLM 的低置信升级通道（E4/E6）；
- 语义故障（fact_swap / clip_misplace / title_violation）→ 验证下游检测与 Q1/Q2/Q4 发现率。

原则：
- **不为结果挑故障**：注入只由计划文件（seed + rate/pools 或逐条 explicit）决定，与产出好坏无关；
- 确定性可复现：同 (seed, stage, attempt) 决策恒定；
- 全部注入落 `workdir/fault_injections.jsonl`（失败/注入记录保留，不覆盖）；
- 绝不注入 publish 的真实上传路径（实验模式本就禁投稿）。

计划文件示例：
  {"seed": 20261107,
   "explicit": [{"stage": "script", "attempt": 1, "type": "TimeoutError"}],
   "rate": 0.2,
   "pools": {"script": ["TimeoutError", "InjectedUnknownFault"],
             "footage": ["clip_misplace"], "kit": ["title_violation"]}}
rate>0 时按 pools 在 (stage, attempt) 上确定性抽注；explicit 优先且不受 stages/rate 限制。
attempt 从 1 起数；语义故障在阶段成功后**污染产物**（模拟闸门漏过的错）。

用法（开发/实验目录；--arm 必给，A4 须带 --experience-store）：
  python -X utf8 fault_injector.py --plan plan.json --arm A4 \\
      --workdir output/_research_audit/<root>/A4/run_02 --topic "题名" \\
      --experience-store output/_research_audit/<root>/A4_lessons.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRANSIENT = ("TimeoutError", "ConnectionError", "JSONDecodeError", "llm_bad_json")
SEMANTIC = ("fact_swap", "clip_misplace", "title_violation")
UNKNOWN = "InjectedUnknownFault"


class InjectedUnknownFault(Exception):
    """未知错误类型——不在 KNOWN_ERRORS/SELF_HEALING，逼出监督 LLM 低置信通道。"""


class llm_bad_json(Exception):
    """对齐 SELF_HEALING 的 llm_bad_json 错误类型名。"""


def exception_for(name: str, detail: str) -> BaseException:
    detail = f"injected {name}: {detail}"
    if name == "TimeoutError":
        return TimeoutError(detail)
    if name == "ConnectionError":
        return ConnectionError(detail)
    if name == "JSONDecodeError":
        return json.JSONDecodeError(detail, "<injected>", 0)
    if name == "llm_bad_json":
        return llm_bad_json(detail)
    if name == UNKNOWN:
        return InjectedUnknownFault(detail)
    raise ValueError(f"非瞬态/未知故障不能以异常注入：{name}")


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan.setdefault("seed", 0)
    plan.setdefault("rate", 0.0)
    plan.setdefault("pools", {})
    plan.setdefault("explicit", [])
    return plan


def schedule_for(plan: dict, stage: str, attempt: int):
    """确定性注入决策。返回 (kind, type, detail) 或 None。
    kind ∈ exception | semantic。explicit 优先于 rate/pools。"""
    for ex in plan.get("explicit", []):
        if ex.get("stage") == stage and int(ex.get("attempt", 0)) == attempt:
            t = ex["type"]
            kind = "semantic" if t in SEMANTIC else "exception"
            return (kind, t, ex.get("detail", "explicit"))
    pool = (plan.get("pools") or {}).get(stage)
    if not pool or plan.get("rate", 0.0) <= 0:
        return None
    rnd = random.Random(f"{plan['seed']}|{stage}|{attempt}")
    if rnd.random() >= plan["rate"]:
        return None
    t = pool[rnd.randrange(len(pool))]
    kind = "semantic" if t in SEMANTIC else "exception"
    return (kind, t, f"rate={plan['rate']} pool={pool}")


def _log(path: Path, stage: str, attempt: int, kind: str, ftype: str, detail) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage, "attempt": attempt,
           "kind": kind, "type": ftype, "detail": str(detail)[:300]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[fault] {stage}#attempt{attempt} {kind}:{ftype} → {str(detail)[:120]}")


# ---------- 语义故障（阶段成功后污染产物） ----------

def _mutate_fact_swap(workdir: Path) -> str:
    """文稿里第一句含数字的句子，末位数字 +1（事实错置），句数/结构不变。"""
    import re
    p = workdir / "文稿.json"
    script = json.loads(p.read_text(encoding="utf-8"))

    def flip(text: str):
        m = list(re.finditer(r"\d", text))
        if not m:
            return text, ""
        last = m[-1]
        new_digit = str((int(last.group()) + 1) % 10)
        out = text[:last.start()] + new_digit + text[last.end():]
        return out, text

    for key in ("hook", "cta"):
        t = script.get(key) or ""
        if re.search(r"\d", t):
            script[key], old = flip(t)
            p.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            return f"hook/cta 数字错置：{old[:60]}"
    for sec in script.get("sections", []):
        t = sec.get("text") or ""
        if re.search(r"\d", t):
            sec["text"], old = flip(t)
            p.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
            return f"section 数字错置：{old[:60]}"
    return "未找到含数字句子，未改动（记空注）"


def _mutate_clip_misplace(workdir: Path) -> str:
    """把一个引用片段的落点 frac 偏移 +0.3 并重切（模拟定窗错位漏过闸门）。"""
    mp = workdir / "采集映射.json"
    mapping = json.loads(mp.read_text(encoding="utf-8"))
    clips = mapping.get("clips") or []
    if not clips:
        return "无引用片段，未改动（记空注）"
    victim = clips[0]
    old = float(victim.get("frac") or 0.0)
    new = min(1.0, old + 0.3) if old < 0.7 else max(0.0, old - 0.3)
    victim["frac"] = round(new, 4)
    mp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    cp = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "cut_clips.py"), str(workdir)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if cp.returncode != 0:
        return f"frac {old}->{new} 但重切失败：{(cp.stderr or cp.stdout)[-150:]}"
    return f"clip[{victim.get('index')}] frac {old}->{new} 已重切"


def _mutate_title_violation(workdir: Path) -> str:
    """投稿包 B站首标题注入结构词（lint 可检、Q4 可感的违规标题）。"""
    kp = workdir / "投稿包.json"
    kit = json.loads(kp.read_text(encoding="utf-8"))
    tl = kit.get("platforms", {}).get("bilibili", {}).get("titles") or []
    if not tl:
        return "无 B站标题，未改动（记空注）"
    old = tl[0]
    tl[0] = "关键看点一：" + old
    kit["platforms"]["bilibili"]["titles"] = tl
    kp.write_text(json.dumps(kit, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"标题注入结构词：{old[:40]} → {tl[0][:40]}"


SEMANTIC_FN = {"fact_swap": _mutate_fact_swap,
               "clip_misplace": _mutate_clip_misplace,
               "title_violation": _mutate_title_violation}


def apply_semantic(ftype: str, workdir: Path) -> str:
    if ftype not in SEMANTIC_FN:
        raise ValueError(f"未知语义故障类型：{ftype}")
    return SEMANTIC_FN[ftype](workdir)


# ---------- 外部包装：不修改 autoline 源码，运行时替换 run_stage ----------

def install(plan: dict, log_path: Path):
    import autoline
    orig_run_stage = autoline.run_stage

    def run_stage_with_faults(name, fn, workdir, use_llm_supervisor, resume,
                              topic_info=None, policy_override=None):
        def fn_with_faults(attempts, actions, hint=""):
            decision = schedule_for(plan, name, attempts)
            if decision and decision[0] == "exception":
                _log(log_path, name, attempts, "exception", decision[1], decision[2])
                raise exception_for(decision[1], decision[2])
            result = fn(attempts, actions, hint)
            if decision and decision[0] == "semantic":
                try:
                    detail = apply_semantic(decision[1], Path(workdir))
                except Exception as exc:  # 注入器自身失败必须显式留痕，不吞
                    detail = f"语义注入执行失败：{exc}"
                _log(log_path, name, attempts, "semantic", decision[1], detail)
            return result
        return orig_run_stage(name, fn_with_faults, workdir, use_llm_supervisor,
                              resume, topic_info, policy_override)

    autoline.run_stage = run_stage_with_faults
    return orig_run_stage


def main() -> int:
    ap = argparse.ArgumentParser(description="受控故障注入 + autoline 实验运行")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--arm", required=True, choices=["A1", "A2", "A3", "A4"])
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--experience-store", default="")
    ap.add_argument("--minutes", type=float, default=3.5)
    ap.add_argument("--format", default="热点评论")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印注入调度表，不执行 run（校验计划用，零 token）")
    args = ap.parse_args()
    plan = load_plan(Path(args.plan))
    if args.dry_run:
        for stage in ("topic", "script", "footage", "produce", "kit", "publish"):
            for attempt in (1, 2, 3):
                d = schedule_for(plan, stage, attempt)
                if d:
                    print(f"{stage}#attempt{attempt}: {d[0]} {d[1]}")
        return 0
    workdir = Path(args.workdir)
    ns = argparse.Namespace(
        topic=args.topic, arch="program", experiment=True, arm=args.arm,
        experience_store=args.experience_store, ideate=0, mode="hot",
        format=args.format, minutes=args.minutes, no_polish=False,
        engine="mimo", workdir=args.workdir, resume=False, publish=False,
        supervise_llm=(args.arm == "A4"))
    install(plan, workdir / "fault_injections.jsonl")
    import autoline
    return autoline.run_once(ns)


if __name__ == "__main__":
    sys.exit(main())
