# -*- coding: utf-8 -*-
"""freeze_seed.py —— 新题逐题冻结工具（步骤 3，pilot/主实验种子）。

每题冻结：事实底座（手写）+ 来源视频（BV/av、URL、上传日期、本地文件 SHA-256）
+ 代码快照（核心 .py 逐文件 SHA-256 + git commit）+ 模型配置（引擎/模型名/音色/采样）。
产出：
  <out>/选题.json       —— canonical 序列化（与 experiment_setup 同口径，seed_sha256 可复算）
  <out>/seed_freeze.json —— 冻结清单（manifest）

原则：
- 只记模型名/音色等公开配置，**绝不写入 key/cookie/SESSDATA**；
- 排除校验：题名不得与既有开发/实验 选题.json 的 topic_title 重复（未用于开发的新题）；
- 冻结即不可改：改动必须换 out 目录重冻并记录版本。

用法：
  python -X utf8 freeze_seed.py --topic-title "题名" --fact-basis-file fact.txt \
      --sources sources.json --out output/_research_audit/seeds/<topic_id> \
      --check-against output/_research_audit

sources.json 示例（local_file 可缺省=尚未下载，须冻结前补齐）：
  [{"id": "BV1xxxx", "url": "https://www.bilibili.com/video/BV1xxxx",
    "upload_date": "20261106", "local_file": "downloads/xxx.mp4"}]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 代码快照范围：产线 + 实验/评测核心（按审计口径）。
# 2026-11-07 F9：原清单漏 pipeline/publish.py 等模块——F8 缺陷正住在未快照的 publish.py 里，
# 「代码冻结」声明覆盖不了实际执行路径。现改为：根目录核心脚本 + pipeline/ 全模块 + config.py。
CORE_CODE = ["autoline.py", "full_agent.py", "eval_harness.py", "experiment_setup.py",
             "experiment_report.py", "fault_injector.py", "cut_clips.py",
             "run_pipeline.py", "bili_publish.py", "config.py",
             ] + [f"pipeline/{p.name}" for p in sorted((ROOT / "pipeline").glob("*.py"))]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_seed_bytes(seed: dict) -> bytes:
    """与 experiment_setup.prepare 完全同口径：sort_keys + ensure_ascii=False + indent=2 + 换行。"""
    return (json.dumps(seed, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def git_commit() -> str:
    try:
        cp = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=ROOT)
        return cp.stdout.strip() if cp.returncode == 0 else "no-commit-yet"
    except Exception:
        return "no-commit-yet"


def model_config() -> dict:
    """只读公开配置；不导入 config 里任何秘密值（config.py 不打印、本函数不触碰 *_KEY/TOKEN/SESSDATA）。"""
    import config
    return {"engine": "mimo", "model_flash": config.MODEL_FLASH, "model_pro": config.MODEL_PRO,
            "model_tts": config.MODEL_TTS, "tts_voice": config.TTS_VOICE,
            "tts_style": config.TTS_STYLE, "tts_atempo": config.TTS_ATEMPO,
            "chat_temperature_default": 0.7, "thinking": "disabled",
            "max_attempts_gate": 2, "supervisor_llm_budget": 3}


def existing_topic_titles(check_roots: list[Path]) -> set[str]:
    titles = set()
    for root in check_roots:
        if root.is_file() and root.name == "选题.json":
            roots = [root]
        else:
            roots = root.rglob("选题.json")
        for p in roots:
            try:
                t = (json.loads(p.read_text(encoding="utf-8")) or {}).get("topic_title")
                if t:
                    titles.add(t)
            except Exception:
                pass
    return titles


def main() -> int:
    ap = argparse.ArgumentParser(description="逐题冻结 pilot/主实验种子")
    ap.add_argument("--topic-title", required=True)
    ap.add_argument("--fact-basis-file", required=True, type=Path,
                    help="手写事实底座（来源定位+口径禁区），冻结后不得改")
    ap.add_argument("--sources", required=True, type=Path,
                    help="来源列表 JSON：[{id,url,upload_date,local_file}]")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--angle", default="")
    ap.add_argument("--hook-line", default="")
    ap.add_argument("--keywords", default="", help="逗号分隔")
    ap.add_argument("--check-against", nargs="*", default=[], type=Path,
                    help="扫描这些目录/文件里的 选题.json，topic_title 不得重复（未用于开发）")
    ap.add_argument("--allow-missing-source-file", action="store_true",
                    help="允许 local_file 缺省/不存在（**未完全冻结**，manifest 会标 frozen=false）")
    args = ap.parse_args()

    fact = args.fact_basis_file.read_text(encoding="utf-8").strip()
    if not fact:
        print("事实底座为空——拒绝冻结（文稿必出编造）")
        return 2
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    if not sources:
        print("来源列表为空——至少一条源视频")
        return 2

    # 排除校验：未用于开发的新题
    seen = existing_topic_titles(args.check_against)
    if args.topic_title in seen:
        print(f"题名已存在于既有种子（开发/实验用过）：{args.topic_title!r}——拒绝冻结")
        return 2

    # 来源哈希（local_file 必须存在才算完全冻结，除非显式放行）
    src_rows, frozen = [], True
    for s in sources:
        row = {"id": s.get("id", ""), "url": s.get("url", ""), "upload_date": s.get("upload_date", "")}
        lf = s.get("local_file") or ""
        if lf and Path(lf).is_file():
            row["local_file"] = str(Path(lf))
            row["sha256"] = sha256_file(Path(lf))
        else:
            row["local_file"] = lf or None
            row["sha256"] = None
            frozen = False
            if not args.allow_missing_source_file:
                print(f"来源缺本地文件或不存在：{lf!r}（用 --allow-missing-source-file 可暂冻但标 frozen=false）")
                return 2
        src_rows.append(row)

    seed = {"topic_title": args.topic_title, "fact_basis": fact}
    if args.angle:
        seed["angle"] = args.angle
    if args.hook_line:
        seed["hook_line"] = args.hook_line
    if args.keywords:
        seed["keywords"] = [k.strip() for k in args.keywords.split(",") if k.strip()]
    seed["source_bvs"] = [s["id"] for s in sources if s.get("id")]

    raw = canonical_seed_bytes(seed)
    seed_digest = hashlib.sha256(raw).hexdigest()

    code_snap = {}
    for rel in CORE_CODE:
        p = ROOT / rel
        code_snap[rel] = sha256_file(p) if p.is_file() else "missing"

    manifest = {
        "topic_title": args.topic_title,
        "seed_sha256": seed_digest,
        "frozen": frozen,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sources": src_rows,
        "code_snapshot_sha256": code_snap,
        "git_commit": git_commit(),
        "model_config": model_config(),
        "note": "开发/主实验种子冻结清单；secrets 不入清单；改内容须换目录重冻",
    }

    args.out.mkdir(parents=True, exist_ok=True)
    if any(args.out.iterdir()):
        print(f"输出目录非空（冻结即不可改，换新目录）：{args.out}")
        return 2
    (args.out / "选题.json").write_bytes(raw)
    (args.out / "seed_freeze.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(args.out), "seed_sha256": seed_digest,
                      "frozen": frozen, "sources": len(src_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
