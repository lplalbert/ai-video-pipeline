#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge_packets.py — 外部评委（GPT / DeepSeek）手动评审资料包与回收（2026-11-08）。

背景：用户批准 GPT 与 DeepSeek 作为额外评委模型参与评估（资料由用户手动提交给
模型；若提供 API 也可走 judge_panel 同链路自动跑）。异族评委补强同族自评效度。

本工具与 judge_panel.py 量表逐字一致（Q1 判词支持度 / Q2 素材落点 / Q4 标题 /
Q5 文稿整体），只做两件事：
  emit    —— 成片产物盲化成「一视频一份」自足资料包 → manual_packets/<sid>.md；
  collect —— 回收模型返回的 JSON，补算聚合计数后按 <sid>.<judge>.json 落入
              scores/，与 pro/flash 评分同格式，一致率分析一视同仁。

盲评原则：包内只有内容（事实底座/口播句/片段对照/标题/文稿）与匿名 sid，
绝不含 arm、run、路径；评分口径与红线不变——一律标「自动评审」，绝不冒充人评。
不动任何冻结文件（eval_harness.py / experiment_report.py / pipeline/*）。

用法（项目根）：
    python -X utf8 judge_packets.py emit [root]     # root 默认 output/_research_audit/pilot_v1
    python -X utf8 judge_packets.py run-api         # 用外部 API 自动评审（2026-11-08 用户批准）
    python -X utf8 judge_packets.py collect <返回文件...>
返回文件命名：<sid>.<judge>.json（如 3f2a9c01b4d5.gpt.json），
可直接粘贴模型原始回复（含 ```json 围栏亦可）。

API 自动链路（run-api）：key 只从 gitignore 覆盖的本地文件读取（默认
<out-dir>/.api_keys/deepseek.key），不回显、不进日志、错误信息自动打码。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from judge_panel import load_artifacts, sid_of

JUDGE_DIR = Path("output/_research_audit/judge_v1")
SID_RE = re.compile(r"^[0-9a-f]{12}$")

# 与 judge_panel.py 各量表措辞逐字一致（cross-judge 可比性靠同一份量表）
Q1_RUBRIC = """你是事实核对评审。对下列每一句口播，拆出其中的原子判词（可证伪的事实断言），
逐条对照【事实底座】判：支持 / 无支撑 / 矛盾 / 非判词（观点修辞无事实断言）。
判「支持/无支撑/矛盾」时 evidence 写底座中的对应条目编号或「无」。"""
Q2_RUBRIC = """你是素材落点评审。逐条判断「片段文本」与「解说句」是否指同一事件/主体/时点：
匹配（主体、动作、事件一致）/ 弱匹配（相关但细节无法确认）/ 错位（主体、人物、动作或时点不符）。
只有解说音频对上而画面内容无法确认时不得判「匹配」。"""
Q4_RUBRIC = """你是标题评审。对每个平台标题按 1–5 分评三个维度：
信息量（是否说清看点）、准确（是否与内容相符、无夸大）、吸引（点击欲）。
1=明显不可用，3=基本可用，5=可直接发布。"""
Q5_RUBRIC = """你是文稿整体评审（只看文本，不看画面）。按 1–5 分评三个维度：
叙事流畅、信息密度（干货与注水之比）、事实边界（断言是否克制、有据）。
1=明显不可用，3=基本可用但需编辑，5=可直接发布。"""

OUTPUT_SPEC = """{
  "q1": {"items": [{"index": 句号, "claims": [{"claim": "判词", "verdict": "支持|无支撑|矛盾|非判词", "evidence": "底座定位或无"}]}]},
  "q2": {"items": [{"index": 句号, "verdict": "匹配|弱匹配|错位", "why": "一句话理由"}]},
  "q4": {"titles": [{"platform": "平台", "information": 1-5, "accuracy": 1-5, "appeal": 1-5, "why": "一句话"}]},
  "q5": {"flow": 1-5, "density": 1-5, "fact_boundary": 1-5, "why": "一句话"}
}"""


# ---------------------------------------------------------------- emit

def build_packet(sid: str, art: dict) -> str:
    sents = art["sentences"]
    pairs = []
    for r in art["clip_rows"]:
        idx = r.get("index")
        if isinstance(idx, int) and 0 <= idx < len(sents):
            pairs.append((idx, sents[idx], r.get("text") or ""))
    sent_lines = "\n".join(f"[{i}] {s}" for i, s in enumerate(sents))
    pair_lines = "\n".join(f"[{i}] 解说句：{s}\n    片段文本：{t}" for i, s, t in pairs)
    title_lines = "\n".join(f"[{t['platform']}] {t['title']}" for t in art["titles"])
    return f"""# 盲评任务包 {sid}

你是独立评审模型。下面是一条短视频的全部评审材料，请**只按内容**完成 4 个维度的评审，
不要猜测或评论材料来源、生成系统、实验分组——这些信息不存在也不需要。
评分标准：1=明显不可用，3=基本可用但需编辑，5=可直接发布（各维度另有细则见对应节）。

## Q1 · 判词支持度（对照事实底座逐句核对）
{Q1_RUBRIC}
句数较多时可按 [0]–[7]、[8]–[15] 分段处理，最后合并进同一个 items 数组，不要跳句。

【事实底座】
{art["fact_basis"][:6000]}

【口播句】
{sent_lines}

## Q2 · 素材落点（片段与解说句对照）
{Q2_RUBRIC}

{pair_lines if pair_lines else "（本片无引用片段对照项，q2.items 留空数组 []）"}

## Q4 · 标题（按平台逐个评）
{Q4_RUBRIC}

{title_lines}

## Q5 · 文稿整体（只看文本）
{Q5_RUBRIC}

【文稿】
{art["body"][:5000]}

## 输出格式（只输出这一个 JSON，不要任何其他文字；四个维度都要有）

{OUTPUT_SPEC}
"""


def cmd_emit(root: Path, out_dir: Path) -> int:
    packs = out_dir / "manual_packets"
    packs.mkdir(parents=True, exist_ok=True)
    mapping_file = out_dir / "mapping.json"
    mapping = json.loads(mapping_file.read_text(encoding="utf-8")) if mapping_file.exists() else {}
    n_ok = n_skip = 0
    for wd in sorted(root.glob("topic_*/A*/run_*")):
        if not (wd / "文稿.json").exists():
            continue  # 未成片（失败在成片前），不进质量轴
        sid = sid_of(wd)
        try:
            art = load_artifacts(wd)
        except Exception as exc:
            print(f"跳过 {sid}（{wd.name} 父级 {wd.parent.name}）：产物不完整 {exc}")
            n_skip += 1
            continue
        mapping[sid] = str(wd)
        (packs / f"{sid}.md").write_text(build_packet(sid, art), encoding="utf-8")
        print(f"包 {sid} ← {wd.parent.parent.name}/{wd.parent.name}/{wd.name}（{len(art['sentences'])} 句）")
        n_ok += 1
    mapping_file.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (packs / "使用说明.md").write_text(usage_doc(n_ok), encoding="utf-8")
    print(f"[emit] 资料包 {n_ok} 份 → {packs}/（另跳过 {n_skip} 份不完整产物）；映射 → mapping.json（仅协调员持有）")
    return 0


def usage_doc(n_ok: int) -> str:
    return f"""# 外部评委手动评审 · 使用说明（GPT / DeepSeek）

本目录共 **{n_ok} 份盲评任务包**（一文件 = 一条视频），文件名即匿名编号（sid）。
每份包自足：任务指令、评分细则、全部材料、输出格式都在文件里，直接整包粘贴即可。

## 操作步骤（每个模型各做一遍，互相独立、不看对方结果）

1. 在 GPT（或 DeepSeek）**新开一个对话**，把某个 `<sid>.md` 的**全部内容**粘贴进去发送。
2. 模型只应回复一个 JSON（允许被 ```json 围栏包住）。把回复**原样**保存为：
   `returns/<sid>.gpt.json`（GPT）或 `returns/<sid>.deepseek.json`（DeepSeek）。
3. 建议**每份包新开一个对话**，避免前一份材料影响后一份评分。
4. 模型拒答/输出不全时：重开对话重发一次即可，不必改包。
5. 全部评完后执行回收（项目根）：

   ```
   python -X utf8 judge_packets.py collect output/_research_audit/judge_v1/returns/*.json
   ```

   回收会校验 JSON、按逐条判定重算支持率/落点口径，产出 `../scores/<sid>.<judge>.json`
   ——与 pro/flash 自动评委**同一格式**，可直接进后续一致率与维度对比分析。

## 纪律（与盲评协议一致）

- **独立作答**：两个模型分开评；GPT 与 DeepSeek 互不参考。
- **只按内容判**：不要按文件名、编号推测分组。
- **原样回收**：不要手工修改模型返回的判定值；有疑问的项在分析阶段标记，不改原始评分。
- 评分一律记「自动评审」，不冒充人类评分。
"""


# ---------------------------------------------------------------- collect

def extract_json(text: str) -> dict:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("未找到 JSON 对象")


def agg_q1(items: list) -> dict:
    n = {"支持": 0, "无支撑": 0, "矛盾": 0, "非判词": 0}
    for it in items or []:
        for c in it.get("claims") or []:
            v = c.get("verdict")
            if v in n:
                n[v] += 1
    denom = n["支持"] + n["无支撑"] + n["矛盾"]
    return {"items": items or [], "verdict_counts": n,
            "support_rate": round(n["支持"] / denom, 3) if denom else None}


def agg_q2(items: list, sentence_total: int | None) -> dict:
    n = {"匹配": 0, "弱匹配": 0, "错位": 0}
    for it in items or []:
        v = it.get("verdict")
        if v in n:
            n[v] += 1
    headline = (n["匹配"] + 0.5 * n["弱匹配"]) / sentence_total if sentence_total else None
    return {"items": items or [], "verdict_counts": n, "sentence_total": sentence_total,
            "headline_q2": round(headline, 3) if headline is not None else None}


def sentence_total_of(sid: str, out_dir: Path, data: dict) -> int | None:
    mapping_file = out_dir / "mapping.json"
    if mapping_file.exists():
        mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
        wd = mapping.get(sid)
        if wd:
            try:
                return len(load_artifacts(Path(wd))["sentences"])
            except Exception as exc:
                print(f"  警告：{sid} 映射产物加载失败（{exc}），退回包内口径", file=sys.stderr)
    return (data.get("q2") or {}).get("sentence_total")  # 包内自报兜底


def build_result(sid: str, judge: str, data: dict, out_dir: Path, source: str) -> dict | None:
    result = {"sid": sid, "judge": judge, "judge_model": f"external:{judge}",
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source}
    if "q1" in data:
        result["q1"] = agg_q1((data.get("q1") or {}).get("items"))
    if "q2" in data:
        total = sentence_total_of(sid, out_dir, data)
        if total is None:
            print(f"  警告：{sid}/{judge} 无法确定句数，headline_q2 记 null", file=sys.stderr)
        result["q2"] = agg_q2((data.get("q2") or {}).get("items"), total)
    if "q4" in data:
        result["q4"] = {"titles": (data.get("q4") or {}).get("titles") or []}
    if "q5" in data:
        q5 = data.get("q5") or {}
        result["q5"] = {k: q5.get(k) for k in ("flow", "density", "fact_boundary", "why")}
    if not any(k in result for k in ("q1", "q2", "q4", "q5")):
        return None
    result["n_sentences"] = result.get("q2", {}).get("sentence_total")
    return result


def cmd_collect(files: list[Path], out_dir: Path) -> int:
    scores = out_dir / "scores"
    scores.mkdir(parents=True, exist_ok=True)
    n_ok = n_skip = 0
    for f in files:
        m = re.match(r"^([0-9a-f]{12})\.([A-Za-z0-9_-]+)\.json$", f.name)
        if not m:
            print(f"跳过 {f.name}：文件名须为 <sid>.<judge>.json")
            n_skip += 1
            continue
        sid, judge = m.group(1), m.group(2)
        try:
            data = extract_json(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"跳过 {f.name}：JSON 解析失败 {exc}")
            n_skip += 1
            continue
        result = build_result(sid, judge, data, out_dir, source="manual_packet")
        if result is None:
            print(f"跳过 {f.name}：未识别出任何维度")
            n_skip += 1
            continue
        out_file = scores / f"{sid}.{judge}.json"
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        q1 = (result.get("q1") or {}).get("support_rate")
        q2 = (result.get("q2") or {}).get("headline_q2")
        print(f"收 {sid}/{judge}：Q1支持率={q1} Q2={q2} → {out_file}")
        n_ok += 1
    print(f"[collect] 回收 {n_ok} 份 → {scores}/（跳过 {n_skip}）")
    return 0


# ---------------------------------------------------------------- run-api

def redact(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9]{8,}", "sk-***", text or "")


def api_chat(base_url: str, key: str, model: str, prompt: str,
             temperature: float, max_tokens: int, timeout: int = 600) -> tuple[str, dict]:
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "temperature": temperature, "max_tokens": max_tokens,
                       "response_format": {"type": "json_object"}}).encode("utf-8")
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = (payload["choices"][0]["message"].get("content") or "")
    return content, payload.get("usage") or {}


def api_chat_retry(base_url: str, key: str, model: str, prompt: str,
                   temperature: float, max_tokens: int, tries: int = 3) -> tuple[str, dict]:
    last = None
    for i in range(max(1, tries)):
        try:
            return api_chat(base_url, key, model, prompt, temperature, max_tokens)
        except urllib.error.HTTPError as exc:
            detail = redact(exc.read().decode("utf-8", "replace")[:300])
            last = f"HTTP {exc.code}: {detail}"
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except Exception as exc:  # 网络/解析类
            last = redact(str(exc))
        time.sleep(3 * (i + 1))
    raise RuntimeError(last or "未知错误")


def cmd_run_api(args) -> int:
    out_dir: Path = args.out_dir
    packets: Path = args.packets
    key_file = args.key_file or (out_dir / ".api_keys" / "deepseek.key")
    if not Path(key_file).exists():
        print(f"缺 key 文件：{key_file}（不回显 key，只读本地文件）", file=sys.stderr)
        return 2
    api_key = Path(key_file).read_text(encoding="utf-8").strip()
    judge_specs = []
    for pair in args.judges.split(","):
        pair = pair.strip()
        if not pair:
            continue
        jid, _, model = pair.partition(":")
        judge_specs.append((jid, model or jid))
    scores = out_dir / "scores"
    scores.mkdir(parents=True, exist_ok=True)

    jobs = []
    for p in sorted(packets.glob("*.md")):
        if p.name == "使用说明.md":
            continue
        for judge, model in judge_specs:
            out_file = scores / f"{p.stem}.{judge}.json"
            if out_file.exists() and not args.force:
                continue
            jobs.append((p.stem, judge, model, p, out_file))
    print(f"[run-api] 待跑 {len(jobs)} 个评审调用：{judge_specs}（已有评分跳过，--force 可重跑）")
    if not jobs:
        return 0

    def run_one(job):
        sid, judge, model, p, out_file = job
        t0 = time.time()
        try:
            content, usage = api_chat_retry(args.base_url, api_key, model,
                                            p.read_text(encoding="utf-8"),
                                            args.temperature, args.max_tokens)
            data = extract_json(content)
            result = build_result(sid, judge, data, out_dir, source="api")
            if result is None:
                raise RuntimeError("未识别出任何维度")
        except Exception as exc:
            err = {"sid": sid, "judge": judge, "judge_model": f"external:{judge}",
                   "api_model": model, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "source": "api", "error": redact(str(exc)),
                   "elapsed_s": round(time.time() - t0, 1)}
            err_file = scores / f"{sid}.{judge}.error.json"
            err_file.write_text(json.dumps(err, ensure_ascii=False, indent=2), encoding="utf-8")
            return f"失败 {sid}/{judge}：{redact(str(exc))}（记档，续跑可重试）"
        result["api_model"] = model
        result["api_usage"] = usage
        result["elapsed_s"] = round(time.time() - t0, 1)
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        q1 = (result.get("q1") or {}).get("support_rate")
        q2 = (result.get("q2") or {}).get("headline_q2")
        return (f"评完 {sid}/{judge}：Q1支持率={q1} Q2={q2} "
                f"elapsed={result['elapsed_s']}s tok_in={usage.get('prompt_tokens')} "
                f"tok_out={usage.get('completion_tokens')}")

    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futs = [pool.submit(run_one, job) for job in jobs]
        for fut in as_completed(futs):
            line = fut.result()
            print(line, flush=True)
            n_err += line.startswith("失败")
            n_ok += line.startswith("评完")
    print(f"[run-api] 完成：成功 {n_ok} / 失败 {n_err} → {scores}/")
    return 0


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="外部评委手动评审资料包与回收（自动评审，非人评）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("emit", help="生成盲评资料包")
    pe.add_argument("root", nargs="?", type=Path, default=Path("output/_research_audit/pilot_v1"))
    pe.add_argument("--out-dir", type=Path, default=JUDGE_DIR)
    pc = sub.add_parser("collect", help="回收模型返回评分")
    pc.add_argument("files", nargs="+", type=Path)
    pc.add_argument("--out-dir", type=Path, default=JUDGE_DIR)
    pr = sub.add_parser("run-api", help="用外部 API 自动评审（自动评审，非人评）")
    pr.add_argument("--packets", type=Path, default=JUDGE_DIR / "manual_packets")
    pr.add_argument("--out-dir", type=Path, default=JUDGE_DIR)
    pr.add_argument("--judges", default="deepseek:deepseek-v4-pro,deepseek-flash:deepseek-flash",
                    help="judge_id:model 逗号分隔")
    pr.add_argument("--base-url", default="https://api.deepseek.com/v1")
    pr.add_argument("--key-file", type=Path, default=None)
    pr.add_argument("--workers", type=int, default=3)
    pr.add_argument("--temperature", type=float, default=0.2)
    pr.add_argument("--max-tokens", type=int, default=16384)
    pr.add_argument("--force", action="store_true", help="已有评分也重跑")
    args = ap.parse_args()
    if args.cmd == "emit":
        return cmd_emit(args.root, args.out_dir)
    if args.cmd == "run-api":
        return cmd_run_api(args)
    return cmd_collect(args.files, args.out_dir)


if __name__ == "__main__":
    sys.exit(main())
