"""LLM decision baseline using the same frozen input and media tools as autoline.

The LLM directly writes the script, chooses source windows, and writes titles.
TTS, cutting and rendering remain shared physical tools across arms.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pipeline import matcher, publish, writer
from pipeline.llm import extract_json, pro


def make_stages(args, topic_info: dict, workdir: Path, shared_stages: dict) -> dict:
    topic = topic_info["topic_title"]

    def script_stage(attempts, actions, hint=""):
        lo, hi = writer._word_range(args.minutes, args.format)  # v1.1（R3）分体裁精确窗
        prompt = writer.SCRIPT_PROMPT.format(
            topic=topic, fmt_desc=writer.FORMATS.get(args.format, args.format),
            minutes=args.minutes, lo=lo, hi=hi,
            topic_info="\n冻结事实底座：\n" + (topic_info.get("fact_basis") or ""),
        )
        # v1.1（R3）：长度自检提示——只提示不放水，超窗照败（与程序臂同窗同判）
        prompt += (f"\n字数自查：hook+sections+cta 合计必须落在 [{lo},{hi}] 字内"
                   "（写作中持续估算，宁可压缩句数不得超窗）。")
        script = extract_json(pro(prompt, max_tokens=6000))
        if not isinstance(script, dict) or not isinstance(script.get("sections"), list):
            raise ValueError("agent script has invalid schema")
        words = writer.word_count(script)
        if not (lo <= words <= hi):
            # 输出规格统一（2026-11-07 协议修正）：A1 与程序层同受字数带约束，
            # 防长度不对称污染成本/质量比较（A1 无监督，失败即盲重试，预算同为 MAX_ATTEMPTS）
            # v1.1（R3）：±30% 模糊带取消，改分体裁精确窗（只紧不松）
            raise ValueError(f"agent script words {words} outside genre window [{lo},{hi}]")
        (workdir / "文稿.json").write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"status": "ok", "stage": "script", "evidence": {
            "words": writer.word_count(script), "sentences": len(writer.flatten_sentences(script))}}

    def footage_stage(attempts, actions, hint=""):
        script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
        sentences = writer.flatten_sentences(script)
        sources = {}
        windows = []
        transcripts = {}
        durations = {}
        for bv in topic_info.get("source_bvs") or []:
            video = matcher._find_local(bv)
            if video is None:
                continue
            duration = matcher._probe_dur(video)
            if duration <= 0:
                continue
            durations[bv] = duration
            stem = video.stem
            author, sep, title = stem.partition(" - ")
            sources[bv] = {"author": author if sep else "", "work": title or stem,
                           "url": f"https://www.bilibili.com/video/{bv}"}
            lines = matcher.transcribe_cached(video)
            transcripts[bv] = lines
            # Fixed time bins are an evidence display, not a matching algorithm.
            for start in range(0, int(duration), 45):
                snippet = " ".join(x["content"] for x in lines
                                   if x["from"] < start + 45 and x["to"] > start)
                if snippet:
                    windows.append({"src": bv, "start": start, "text": snippet[:180]})
        if not windows:
            raise RuntimeError("no local source transcripts for agent mapping")
        prompt = (
            "你是视频制作 agent。直接决定每句口播使用哪段本地素材。只根据下列转写窗选择，"
            "必须核对人名、动作和方向；没有可靠窗的句子用字卡。"
            "只输出 JSON：{\"clips\":[{\"index\":0,\"src\":\"BV...\",\"start\":0,\"desc\":\"画面\"}]}。"
            "未选素材的句子不要列入 clips。\n"
            f"选题：{topic}\n事实底座：{topic_info.get('fact_basis', '')}\n"
            f"句子：{json.dumps(list(enumerate(sentences)), ensure_ascii=False)}\n"
            f"可选素材窗：{json.dumps(windows, ensure_ascii=False)}"
        )
        if attempts > 1:
            # v1.1（R5）：盲重试同错复发（F5 越界/无效引用实测）——重试时静态重申硬校验
            # 约束（只加约束不喂旧失败输出，与盲重采样口径一致），提前拦截不带病进 produce
            prompt += ("\n[硬校验约束·重试必读] index 必须是给出的句号之一；src 必须是给出的 BV；"
                       "start 只能落在给出的素材窗内；没有可靠窗的句子宁可不列 clips。")
        chosen = extract_json(pro(prompt, max_tokens=3500))
        if not isinstance(chosen, dict) or not isinstance(chosen.get("clips"), list):
            raise ValueError("agent mapping has invalid schema")
        detail = []
        evid_lo = {}
        for item in chosen["clips"]:
            idx, bv, coarse = int(item["index"]), item["src"], float(item["start"])
            if not 0 <= idx < len(sentences) or bv not in sources or not 0 <= coarse < durations[bv]:
                raise ValueError("agent coarse mapping references invalid sentence or source")
            lines = transcripts[bv]
            excerpt = [{"from": line["from"], "to": line["to"], "text": line["content"]}
                       for line in lines if line["from"] < coarse + 45 and line["to"] > coarse]
            # F10b（2026-11-07 A1 run_02 实测）：证据窗含跨界行（to>coarse 而 from<coarse-1.5），
            # 其 from 是合法证据起点但被旧下限拒掉——下限改为「所给证据行 from 最小值-1.5」，
            # 校验口径与提示词证据严格一致（证据里有的就收，证据里没有的照拒）。
            evid_lo[(idx, bv)] = (min((float(l["from"]) for l in excerpt), default=coarse) - 1.5)
            detail.append({"index": idx, "src": bv, "coarse_start": coarse,
                           "sentence": sentences[idx], "timestamped_asr": excerpt})
        refinement_prompt = (
            "你是视频素材剪辑 agent。以下是你刚选的 45 秒候选窗及逐句时间戳。"
            "每个口播句只能选一个与事实主体、人名和动作相符的 10 秒起点；"
            "找不到足够证据则填 null，使用字卡。"
            "只输出 JSON：{\"clips\":[{\"index\":0,\"src\":\"BV...\",\"start\":12.3}]}。"
            "所有候选都要列出，start 为秒数或 null，不得改 index/src。"
            # F10（2026-11-07 新题四臂 A1 run_01 实测）：模型把别的候选窗时间戳串到本条
            # （index4 coarse=90 证据 [89.44-136.4] 回 68.64），硬校验拦截后盲重试同错复发。
            # 提示词钉死证据来源：start 必须取自本条 timestamped_asr 某行的 from（±1s 内），
            # 不得跨条目引用时间；校验器不动（只紧不松）。
            "start 必须取自该候选自己的 timestamped_asr 某行的 from（允许 ±1 秒微调），"
            "绝不可引用其他候选的时间戳或凭空造数。\n"
            + json.dumps(detail, ensure_ascii=False)
        )
        if attempts > 1:
            # v1.1（R5）：细化阶段同理——start 只取本候选自己的 timestamped_asr
            refinement_prompt += (
                "\n[硬校验约束·重试必读] 不得改 index/src；start 必须取自该候选自己的 "
                "timestamped_asr 某行的 from（允许 ±1 秒微调）；证据不足填 null 用字卡，"
                "绝不可引用其他候选的时间戳、绝不可越界。")
        refined = extract_json(pro(refinement_prompt, max_tokens=2500))
        if not isinstance(refined, dict) or not isinstance(refined.get("clips"), list):
            raise ValueError("agent refined mapping has invalid schema")
        refined_by_key = {(int(item["index"]), item["src"]): item.get("start")
                          for item in refined["clips"]}
        (workdir / "agent_mapping_decisions.json").write_text(json.dumps({
            "coarse": chosen, "refined": refined}, ensure_ascii=False, indent=2), encoding="utf-8")
        clips = []
        used_indices = set()
        for item in chosen["clips"]:
            idx, bv, coarse = int(item["index"]), item["src"], float(item["start"])
            if not 0 <= idx < len(sentences) or idx in used_indices or bv not in sources:
                raise ValueError("agent mapping references invalid sentence or source")
            refined_start = refined_by_key.get((idx, bv))
            if refined_start is None:
                continue
            start = float(refined_start)
            duration = durations[bv]
            lo = evid_lo.get((idx, bv), coarse - 1.5)
            if not lo <= start < min(coarse + 45, duration):
                raise ValueError("agent mapping start is outside source")
            start = max(0.0, start)  # F10b：不再截到 coarse——证据行起点可早于 coarse 粗估
            used_indices.add(idx)
            frac = min(1.0, max(0.0, start / max(1.0, duration - matcher.CLIP_DUR - 2)))
            desc = publish.safe_filename(str(item.get("desc") or "agent selected clip"))[:40]
            clips.append({"index": idx, "src": bv, "frac": round(frac, 4),
                          "desc": desc or "agent selected clip"})
        mapping = {"topic": topic, "sources": sources, "clips": sorted(clips, key=lambda c: c["index"])}
        (workdir / "采集映射.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        if clips:
            cp = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).parent / "cut_clips.py"),
                                 str(workdir)], capture_output=True, text=True, encoding="utf-8", errors="replace")
            if cp.returncode:
                raise RuntimeError(f"cut_clips failed: {(cp.stdout + cp.stderr)[-500:]}")
        return {"status": "ok", "stage": "footage", "evidence": {"clips": len(clips),
                "unmapped": len(sentences) - len(clips)}}

    def kit_stage(attempts, actions, hint=""):
        script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
        prompt = publish.TITLE_PROMPT.format(
            platforms="；".join(f"{p['name']}≤{p['title_limit']}字" for p in publish.PLATFORMS.values()),
            headings="、".join(s.get("heading", "") for s in script.get("sections", [])),
            topic=topic, tags="、".join(script.get("tags", [])),
            summary="\n".join(writer.flatten_sentences(script))[:300],
        )
        titles = extract_json(pro(prompt, max_tokens=1500))
        if not isinstance(titles, dict) or any(not isinstance(titles.get(k), list) for k in publish.PLATFORMS):
            raise ValueError("agent titles have invalid schema")
        publish.build_publish_kit(script, workdir, topic=topic, titles_override=titles)
        return {"status": "ok", "stage": "kit", "evidence": {"title_count": sum(len(v) for v in titles.values())}}

    return {**shared_stages, "script": script_stage, "footage": footage_stage, "kit": kit_stage}
