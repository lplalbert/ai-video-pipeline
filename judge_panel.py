#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge_panel.py — 自动评审团（2026-01-07 无外部人类评者方案）。

背景：3 名独立人类评者不可得（用户裁定），评测改双模型 LLM 盲评替代全量人评。
红线：本工具产出一律标「自动评审」，绝不冒充独立人类评分；论文主张里
质量轴=「自动评审分」，主结论压在全自动硬轴（token/失败率/E1/E2）。

设计：
- 评委 = mimo-v2.6-pro + mimo-v2.6-flash（--judges 可选），量表沿用盲评协议
  Q1 判词支持度 / Q2 素材落点 / Q4 标题 / Q5 文稿整体（文本层，不看画面——
  局限写进威胁效度）；
- 盲评：提示词只含内容（文稿/事实底座/片段文本/标题），绝不含 arm、run、
  路径；产物按匿名 sid 落盘，sid→workdir 映射单独存放（仅协调员持有）；
- 不动任何冻结文件（eval_harness.py / experiment_report.py / pipeline/*），
  只读导入 pipeline.writer.flatten_sentences 与 pipeline.llm。

用法（项目根）：
    python -X utf8 judge_panel.py <workdir> [<workdir> ...]
        [--judges pro,flash] [--out-dir output/_research_audit/judge_v1]
        [--q 1,2,4,5] [--temperature 0.2]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from config import MODEL_FLASH, MODEL_PRO
from pipeline import llm
from pipeline.writer import flatten_sentences

JUDGE_MODELS = {"pro": MODEL_PRO, "flash": MODEL_FLASH}


def sid_of(workdir: Path) -> str:
    return hashlib.sha1(str(workdir.resolve()).encode("utf-8")).hexdigest()[:12]


def chat_json(model: str, prompt: str, temperature: float, tries: int = 3):
    last = None
    for _ in range(max(1, tries)):
        raw = llm.chat(model, prompt, system="", temperature=temperature)
        try:
            return llm.extract_json(raw)
        except ValueError as exc:
            last = exc
            time.sleep(1)
    raise RuntimeError(f"评审 JSON 解析失败（{model}）：{last}")


def blind_artifacts(art: dict) -> dict:
    """盲化加固（2026-11-08）：冻结 fact_basis 等文本内的内部题号 slug（topic_*）
    →「参考题A/B/…」，同一文档内同 slug 同标签（保留跨题引用可区分性）。
    对所有评委统一生效（pro/flash/GPT/DeepSeek 同料才有一致率可比性）。"""
    labels: dict[str, str] = {}

    def rep(m: "re.Match[str]") -> str:
        slug = m.group(0)
        if slug not in labels:
            labels[slug] = "参考题" + chr(ord("A") + len(labels) % 26)
        return labels[slug]

    def scrub(x):
        if isinstance(x, str):
            return re.sub(r"topic_[a-z0-9_]+", rep, x)
        if isinstance(x, list):
            return [scrub(i) for i in x]
        if isinstance(x, dict):
            return {k: scrub(v) for k, v in x.items()}
        return x

    return scrub(art)


def load_artifacts(workdir: Path) -> dict:
    script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
    seed = json.loads((workdir / "选题.json").read_text(encoding="utf-8"))
    kit = json.loads((workdir / "投稿包.json").read_text(encoding="utf-8"))
    # 裁片映射两臂产物不同：program 臂=落点报告.json(rows)；full-agent 臂=agent_mapping_decisions.json(coarse.clips+desc)
    lp = workdir / "落点报告.json"
    if lp.exists():
        rows = [r for r in json.loads(lp.read_text(encoding="utf-8")).get("rows", [])
                if r.get("status") == "ok"]
    else:
        ad = workdir / "agent_mapping_decisions.json"
        clips = ((json.loads(ad.read_text(encoding="utf-8")).get("coarse") or {}).get("clips")
                 if ad.exists() else []) or []
        rows = [{"index": c.get("index"), "status": "ok", "text": c.get("desc") or ""} for c in clips]
    sentences = flatten_sentences(script)
    body = "\n".join([script.get("hook") or ""] +
                     [f"{s.get('heading','')}\n{s.get('text','')}" for s in script.get("sections") or []] +
                     [script.get("cta") or ""])
    titles = []
    for plat, cfg in (kit.get("platforms") or {}).items():
        ts = cfg.get("titles") or []
        if ts:
            titles.append({"platform": cfg.get("name") or plat, "title": ts[0]})
    return blind_artifacts({"sentences": sentences, "fact_basis": seed.get("fact_basis") or "",
                            "clip_rows": [r for r in rows if r.get("status") == "ok"],
                            "titles": titles, "body": body})


# ---------------------------------------------------------------- Q1

def judge_q1(model: str, art: dict, temperature: float) -> dict:
    items = []
    sents = art["sentences"]
    for start in range(0, len(sents), 8):
        chunk = sents[start:start + 8]
        lines = "\n".join(f"[{start + i}] {s}" for i, s in enumerate(chunk))
        prompt = f"""你是事实核对评审。对下列每一句口播，拆出其中的原子判词（可证伪的事实断言），
逐条对照【事实底座】判：支持 / 无支撑 / 矛盾 / 非判词（观点修辞无事实断言）。
判「支持/无支撑/矛盾」时 evidence 写底座中的对应条目编号或「无」。只输出 JSON。

【事实底座】
{art['fact_basis'][:6000]}

【口播句】
{lines}

输出 JSON 格式：{{"items": [{{"index": 句号, "claims": [{{"claim": "判词", "verdict": "支持|无支撑|矛盾|非判词", "evidence": "底座定位或无"}}]}}]}}"""
        data = chat_json(model, prompt, temperature)
        items.extend(data.get("items") or [])
    n = {"支持": 0, "无支撑": 0, "矛盾": 0, "非判词": 0}
    for it in items:
        for c in it.get("claims") or []:
            v = c.get("verdict")
            if v in n:
                n[v] += 1
    denom = n["支持"] + n["无支撑"] + n["矛盾"]
    return {"items": items, "verdict_counts": n,
            "support_rate": round(n["支持"] / denom, 3) if denom else None}


# ---------------------------------------------------------------- Q2

def judge_q2(model: str, art: dict, temperature: float) -> dict:
    items = []
    pairs = []
    for r in art["clip_rows"]:
        idx = r.get("index")
        if isinstance(idx, int) and 0 <= idx < len(art["sentences"]):
            pairs.append((idx, art["sentences"][idx], r.get("text") or ""))
    for start in range(0, len(pairs), 6):
        chunk = pairs[start:start + 6]
        lines = "\n".join(f"[{i}] 解说句：{s}\n    片段文本：{t}" for i, s, t in chunk)
        prompt = f"""你是素材落点评审。逐条判断「片段文本」与「解说句」是否指同一事件/主体/时点：
匹配（主体、动作、事件一致）/ 弱匹配（相关但细节无法确认）/ 错位（主体、人物、动作或时点不符）。
只有解说音频对上而画面内容无法确认时不得判「匹配」。只输出 JSON。

{lines}

输出 JSON 格式：{{"items": [{{"index": 句号, "verdict": "匹配|弱匹配|错位", "why": "一句话理由"}}]}}"""
        data = chat_json(model, prompt, temperature)
        items.extend(data.get("items") or [])
    n = {"匹配": 0, "弱匹配": 0, "错位": 0}
    for it in items:
        v = it.get("verdict")
        if v in n:
            n[v] += 1
    total = len(art["sentences"])
    headline = (n["匹配"] + 0.5 * n["弱匹配"]) / total if total else None
    return {"items": items, "verdict_counts": n, "sentence_total": total,
            "headline_q2": round(headline, 3) if headline is not None else None}


# ---------------------------------------------------------------- Q4

def judge_q4(model: str, art: dict, temperature: float) -> dict:
    lines = "\n".join(f"[{t['platform']}] {t['title']}" for t in art["titles"])
    prompt = f"""你是标题评审。对每个平台标题按 1–5 分评三个维度：
信息量（是否说清看点）、准确（是否与内容相符、无夸大）、吸引（点击欲）。
1=明显不可用，3=基本可用，5=可直接发布。只输出 JSON。

{lines}

输出 JSON 格式：{{"titles": [{{"platform": "平台", "information": n, "accuracy": n, "appeal": n, "why": "一句话"}}]}}"""
    data = chat_json(model, prompt, temperature)
    titles = data.get("titles") or []
    return {"titles": titles}


# ---------------------------------------------------------------- Q5

def judge_q5(model: str, art: dict, temperature: float) -> dict:
    prompt = f"""你是文稿整体评审（只看文本，不看画面）。按 1–5 分评三个维度：
叙事流畅、信息密度（干货与注水之比）、事实边界（断言是否克制、有据）。
1=明显不可用，3=基本可用但需编辑，5=可直接发布。只输出 JSON。

【文稿】
{art['body'][:5000]}

输出 JSON 格式：{{"flow": n, "density": n, "fact_boundary": n, "why": "一句话"}}"""
    data = chat_json(model, prompt, temperature)
    return {k: data.get(k) for k in ("flow", "density", "fact_boundary", "why")}


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="自动评审团（LLM 盲评，非人评）")
    ap.add_argument("workdirs", nargs="+", type=Path)
    ap.add_argument("--judges", default="pro,flash", help="pro,flash 任选")
    ap.add_argument("--out-dir", type=Path, default=Path("output/_research_audit/judge_v1"))
    ap.add_argument("--q", default="1,2,4,5", help="评哪些维度，如 1,2,4,5")
    ap.add_argument("--temperature", type=float, default=0.2)
    args = ap.parse_args()

    judges = [j.strip() for j in args.judges.split(",") if j.strip()]
    for j in judges:
        if j not in JUDGE_MODELS:
            print(f"未知评委 {j}，可选：{list(JUDGE_MODELS)}", file=sys.stderr)
            return 2
    dims = {int(x) for x in args.q.split(",")}

    (args.out_dir / "scores").mkdir(parents=True, exist_ok=True)
    mapping_file = args.out_dir / "mapping.json"
    mapping = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}

    for wd in args.workdirs:
        if not (wd / "文稿.json").exists():
            print(f"跳过 {wd}：无 文稿.json（未成片）")
            continue
        sid = sid_of(wd)
        mapping[sid] = str(wd)
        try:
            art = load_artifacts(wd)
        except Exception as exc:
            print(f"跳过 {sid}（{wd}）：产物加载失败 {exc}")
            continue
        for j in judges:
            out_file = args.out_dir / "scores" / f"{sid}.{j}.json"
            if out_file.exists():
                print(f"跳过 {sid}/{j}：已有评分")
                continue
            model = JUDGE_MODELS[j]
            result = {"sid": sid, "judge": j, "judge_model": model,
                      "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                      "n_sentences": len(art["sentences"])}
            with llm.usage_context(run_id=f"judge_{sid}", stage="judge", purpose="judge"):
                t0 = time.time()
                try:
                    if 1 in dims:
                        result["q1"] = judge_q1(model, art, args.temperature)
                    if 2 in dims:
                        result["q2"] = judge_q2(model, art, args.temperature)
                    if 4 in dims:
                        result["q4"] = judge_q4(model, art, args.temperature)
                    if 5 in dims:
                        result["q5"] = judge_q5(model, art, args.temperature)
                except Exception as exc:
                    result["error"] = str(exc)
                    print(f"评审失败 {sid}/{j}：{exc}（记档继续）")
            result["elapsed_s"] = round(time.time() - t0, 1)
            if "error" in result:
                err_file = args.out_dir / "scores" / f"{sid}.{j}.error.json"
                err_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                continue  # 不落正常评分文件，续跑可重试
            out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            q1 = (result.get("q1") or {}).get("support_rate")
            q2 = (result.get("q2") or {}).get("headline_q2")
            print(f"评完 {sid}/{j}：Q1支持率={q1} Q2={q2} elapsed={result['elapsed_s']}s")

    mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[judge_panel] 完成；评分 → {args.out_dir}/scores/，映射 → mapping.json（仅协调员持有）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
