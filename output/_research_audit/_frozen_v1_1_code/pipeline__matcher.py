# -*- coding: utf-8 -*-
"""matcher.py —— 素材落点自动化（ASR 关键词定位 → 采集映射.json）。

无人值守产线「素材落点」环节的脚本化替代：agent 不在场时，
按事实关键词给每句自动定引用窗，产物直接喂 `cut_clips.py`。

三级取窗（成本收益递减，逐级兜底）：
  1) quotes.json 已有字幕窗（免费）→ 直接用；
  2) 本地源视频 CPU ASR（坑⑮：ffmpeg 16k mono s16le → numpy 喂 transcribe，
     PyAV 19 的 decode_audio 会崩）→ bigram 打分定位；
  3) 都拿不到 → 该句落低置信清单，进检查点给监督层/人。

置信度 = 命中 bigram 数 / 事实 bigram 总数；低于阈值的句子列进
落点报告（错队错人核对的降级替代——纯脚本给不出视觉判断，只给低置信清单）。

GPU 禁用（用户 2026-11-05）：WhisperModel(device="cpu", compute_type="int8")。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

FFMPEG = r"C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin\ffprobe.exe"
CLIP_DUR = 10.0  # 与 cut_clips.py 保持一致
QUOTE_WIN_S = 45.0

_WHISPER = None  # 懒加载单例（CPU）


# ---------- ASR（CPU-only，逐条落盘缓存） ----------

def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        # GPU 禁用（负载即重启）——只走 CPU int8
        _WHISPER = WhisperModel("small", device="cpu", compute_type="int8")
    return _WHISPER


def transcribe_cached(video: Path) -> list:
    """ASR 转写（带缓存 <video>.transcript.json）→ [{from,to,content}]。"""
    # 文件名带 [BVxxx]（坑⑯），with_suffix 对多后缀不友好——统一手工拼
    cache = Path(str(video) + ".transcript.json")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 坑⑮：PyAV 19 decode_audio 崩 → ffmpeg 抽 16k mono s16le → numpy float32
    import numpy as np
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "s16le", "-"],
        capture_output=True)
    if raw.returncode != 0 or not raw.stdout:
        raise RuntimeError(f"ffmpeg 抽音频失败：{video.name}: {raw.stderr[-200:]}")
    audio = np.frombuffer(raw.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    model = _get_whisper()
    segments, _info = model.transcribe(audio, language="zh", beam_size=1)
    lines = [{"from": round(s.start, 2), "to": round(s.end, 2), "content": s.text.strip()}
             for s in segments if s.text.strip()]
    cache.write_text(json.dumps(lines, ensure_ascii=False, indent=1), encoding="utf-8")
    return lines


# ---------- 定窗打分（与 sources.locate_in_subs 同算法） ----------

_STOP = set("的了是在和与及这那有就都不一我也你们个为对上中到说等而其但")

# ---- 文本归一（坑㉗配套）：ASR 出繁体+音译+阿拉伯数字，事实侧是简体+英文ID，
# 不归一 bigram 全被吃掉（实测整篇繁体转写对简体事实命中≈0）----
import unicodedata

ALIASES = {  # 电竞别名/音译 → 规范 token；两边同规则归一（按题补充，误伤就删）
    r"滔搏|韬博|韜博": "tes",
    r"奈特|纳特": "knight",
    r"秋比|乔维|喬維|卓比|抽比|仇比": "chovy",
    r"奇因|奇恩": "kiin",
    r"尺帝": "ruler",
    r"妹扣|美可|梅可": "meiko",
    r"三星|杰尼|傑尼": "gen",
    r"伊泽瑞尔|伊澤瑞爾": "ezreal",
    r"奇亚娜|奇亞娜": "qiyana",
    r"梦魇|夢魘": "nocturne",
}

_CN_DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def _cn2num(s: str) -> str:
    """中文数字→阿拉伯（两边同规则）：三比一↔3比1、四十三↔43、两千五百六十五↔2565。"""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        is_run = c in _CN_DIG or (c in _CN_UNIT and i + 1 < n
                                  and (s[i + 1] in _CN_DIG or s[i + 1] in _CN_UNIT))
        if not is_run:
            out.append(c)
            i += 1
            continue
        total, tmp = 0, 0
        while i < n and (s[i] in _CN_DIG or s[i] in _CN_UNIT):
            c = s[i]
            if c in _CN_DIG:
                tmp = _CN_DIG[c]
                if i + 1 >= n or s[i + 1] not in _CN_UNIT:
                    total += tmp
                    tmp = 0
            else:
                total += (tmp or 1) * _CN_UNIT[c]
                tmp = 0
            i += 1
        out.append(str(total + tmp))
    return "".join(out)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")  # 全角→半角
    try:
        import zhconv
        t = zhconv.convert(t, "zh-cn")  # 繁→简（Whisper zh 常出繁体）
    except Exception:
        pass
    t = _cn2num(t)
    t = re.sub(r"(\d)\s*[-—–:：]\s*(\d)", r"\1比\2", t)  # 3-1/3:1/3比1/三比一 归一
    for pat, rep in ALIASES.items():
        t = re.sub(pat, rep, t)
    return t.lower()


def _bigrams(text: str) -> set:
    t = re.sub(r"[^\w一-鿿]", "", _norm(text))
    return {t[i:i + 2] for i in range(len(t) - 1)
            if t[i] not in _STOP and t[i + 1] not in _STOP}


def _entity_ratio(entities_raw: str, text: str) -> float:
    """实体命中率（归一后 token 子串匹配）：错队错人的第二信号。"""
    toks = [t for t in re.split(r"[/、，,\s]+", _norm(entities_raw or "")) if len(t) >= 2]
    if not toks:
        return 0.0
    tn = _norm(text)
    return sum(1 for t in toks if t in tn) / len(toks)


def locate_windows(lines: list, fact: str, max_s: float = QUOTE_WIN_S) -> list:
    """按事实关键词打分定位引用窗 → [{start,end,text,score,conf}]。"""
    grams = _bigrams(fact)
    if not grams or not lines:
        return []
    scored = [(len(grams & _bigrams(ln["content"])), ln) for ln in lines]
    scored = [(s, ln) for s, ln in scored if s > 0]
    scored.sort(key=lambda x: -x[0])
    out, used = [], []
    for s, ln in scored[:12]:
        start = max(0.0, ln["from"] - 1.0)
        if any(abs(start - u) < 6 for u in used):
            continue
        used.append(start)
        # 引用窗=锚定行起 max_s 秒（早前 min(start+max_s, line.to+2) 把窗钳成一行字，
        # conf 结构上永远 <0.2——覆盖度要按整窗文本算，不是单行）
        end = min(start + max_s, lines[-1]["to"] + 2.0)
        text = " ".join(x["content"] for x in lines
                        if x["from"] < end and x["to"] > start)
        conf = len(grams & _bigrams(text)) / max(1, len(grams))
        out.append({"start": round(start, 1), "end": round(end, 1), "text": text,
                    "score": s, "conf": round(conf, 3)})
        if len(out) >= 2:
            break
    return out


# ---------- 采集映射自动生成 ----------

SKIP_VISUALS = ("无", "自制字卡", "数据图表")  # 这些画面类型走字卡/图表，不进实拍映射


def _assign_to_sentences(items: list, sentences: list):
    """plan 事实 → 句子序号（bigram argmax）。

    坑㉗：采集计划.json 的 index 是小节号（实测 33 条事实只给 0~5 重复值），
    不能当句号用——采集映射/切片/manifest 全按「句号=audio segments index」对位。
    这里改按事实文本↔句子文本打分锚定，conf<0.12 的事实进孤儿清单。
    """
    sent_grams = [_bigrams(s) for s in sentences]
    by_sent: dict = {}
    orphans: list = []
    for it in items:
        fg = _bigrams(it.get("fact") or "")
        best_i, best_s = -1, 0
        for i, g in enumerate(sent_grams):
            s = len(fg & g)
            if s > best_s:
                best_i, best_s = i, s
        if best_i < 0 or not fg or best_s / len(fg) < 0.12:
            orphans.append(it)
        else:
            by_sent.setdefault(best_i, []).append(it)
    return by_sent, orphans

def _find_local(bv: str, dl_root: Path = Path("downloads")) -> Path | None:
    # 坑⑯⑳：只认合成后的 `[BVxxx].mp4`，[...] 不能进 glob
    if not dl_root.exists():
        return None
    hits = [p for p in dl_root.rglob("*.mp4")
            if p.name.endswith(f"{bv}].mp4") and ".f" not in p.stem.split("[")[0][-6:]]
    # 过滤 yt-dlp 中间流 .fNNNN.mp4（坑⑳）：中间流名字不含 [BVxxx] 后缀模式，上面 endswith 已挡
    return hits[0] if hits else None


def _probe_dur(p: Path) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else 0.0


def _desc(fact: str) -> str:
    d = re.sub(r"[^\w一-鿿]+", "", fact or "")[:8] or "片段"
    return d


def auto_mapping(workdir: Path, min_conf: float = 0.20, dl_root: Path = Path("downloads")) -> dict:
    """采集计划.json + quotes.json + 文稿.json → 采集映射.json + 落点报告.json。

    按**句子**对位（clip index = 句号 = audio segments index，坑㉗）：
    事实先锚定到句子（文本打分，不信采集计划的 index），再按句查窗。
    返回 {"clips": n, "low_conf": [...], "mapping": {...}}。
    低置信句不进映射留给人工/监督层：双闸=文本覆盖≥min_conf，
    或 ≥0.12 且实体命中（entity/视频题名在窗内=右素材，措辞差异不误杀）；
    字卡/图表句、无事实句落 fallback（manifest 自会补字卡/实拍），不算低置信。
    """
    from pipeline.writer import flatten_sentences

    plan = json.loads((workdir / "采集计划.json").read_text(encoding="utf-8"))
    script_path = workdir / "文稿.json"
    if not script_path.exists():
        raise RuntimeError("缺 文稿.json——素材落点按句对位，须先过文稿阶段")
    sentences = flatten_sentences(json.loads(script_path.read_text(encoding="utf-8")))

    by_fact: dict = {}
    qfile = workdir / "quotes.json"
    if qfile.exists():
        try:
            for row in json.loads(qfile.read_text(encoding="utf-8")).get("rows", []):
                by_fact.setdefault((row.get("item") or {}).get("fact") or "", []).append(row)
        except Exception:
            pass

    items = plan.get("items", [])
    by_sent, orphans = _assign_to_sentences(items, sentences)

    # 手选源视频池（选题.json source_bvs）——本地 ASR 直查的兜底候选（坑㉙）
    seed_bvs: list = []
    seed_file = workdir / "选题.json"
    if seed_file.exists():
        try:
            seed_bvs = json.loads(seed_file.read_text(encoding="utf-8")).get("source_bvs") or []
        except Exception:
            pass

    sources: dict = {}
    clips: list = []
    low_conf: list = []
    report_rows: list = []
    used: dict = {}  # bv -> [start]，同窗不复用给相邻句

    for si, sent in enumerate(sentences):
        sent_items = by_sent.get(si, [])
        want = [it for it in sent_items if it.get("visual") not in SKIP_VISUALS]
        if not want:
            report_rows.append({"index": si, "status": "fallback",
                                "why": "无实拍需求（字卡/图表/无）或无对应事实"})
            continue

        query = sent  # 用整句查窗（事实≤30字太薄，召回低）
        grams = _bigrams(query)
        cands: list = []  # (conf, bv, start, dur, meta, text)
        # 1) quotes.json 字幕窗（免费；无字幕轨时 windows 为空，实测常见）
        for it in want:
            fact = it.get("fact") or ""
            for row in by_fact.get(fact, []):
                v = row.get("video") or {}
                bv = v.get("bvid", "")
                for w in row.get("windows") or []:
                    conf = w.get("conf")
                    if conf is None:
                        conf = round(w.get("score", 0) / max(1, len(grams)), 3)
                    cands.append((conf, bv, w.get("start", 0.0), v.get("duration", 0.0), v,
                                  w.get("text", "")))
        # 2) 本地源视频 ASR（缓存逐视频落盘，重复查窗不重转写）。
        #    坑㉙：候选池必须含选题 source_bvs 全体直查——只沿 quotes 行的 video 找
        #    会整链查空（行挂的 bvid 常不在本地/事实无行，实测 7 句全 0 置信）。
        pool_bvs: list = []
        for it in want:
            for row in by_fact.get(it.get("fact") or "", []):
                bv = (row.get("video") or {}).get("bvid", "")
                if bv and bv not in pool_bvs:
                    pool_bvs.append(bv)
        for bv in seed_bvs:
            if bv not in pool_bvs:
                pool_bvs.append(bv)
        for bv in pool_bvs:
            local = _find_local(bv, dl_root)
            if not local:
                continue
            try:
                lines = transcribe_cached(local)
            except Exception as exc:
                report_rows.append({"index": si, "status": "asr_fail", "why": str(exc)[:120]})
                continue
            stem = re.sub(r"\s*\[BV\w+\]$", "", local.stem)
            author, _, title = stem.partition(" - ")
            meta = {"bvid": bv, "title": title or stem, "author": author if title else "",
                    "url": f"https://www.bilibili.com/video/{bv}"}
            dur = _probe_dur(local)
            for w in locate_windows(lines, query):
                cands.append((w["conf"], bv, w["start"], dur, meta, w["text"]))

        # 双闸验收 + 实体排序：窗文本实体命中才算验收证据（clip 拍的是窗不是题名——
        # 题名命中只加排序权重，曾放行「题名对、窗内容是别人采访」的错位）；
        # 闸=文本覆盖≥min_conf，或 ≥0.12 且窗文本实体命中；同源同窗不复用
        ents_raw = " ".join(it.get("entity") or "" for it in want)
        ranked = []
        for c in cands:
            e_w = _entity_ratio(ents_raw, c[5] or "")                    # 窗文本——验收+排序
            e_t = _entity_ratio(ents_raw, (c[4] or {}).get("title") or "")  # 题名——只排序
            if c[0] >= min_conf or (c[0] >= 0.12 and e_w > 0):
                ranked.append((c[0] + 0.3 * e_w + 0.2 * e_t, c, e_w))
        ranked.sort(key=lambda x: -x[0])
        best = None
        for _key, c, e_w in ranked:
            if any(abs(c[2] - u) < 8 for u in used.get(c[1], [])):
                continue
            best = (c, e_w)
            break

        if best is None:
            top = max(cands, key=lambda c: c[0], default=None)
            conf = top[0] if top else 0.0
            ent = _entity_ratio(ents_raw, (top[5] or "")) if top else 0.0
            why = ("好窗已用于相邻句（同窗不复用）——需人工另选落点"
                   if ranked else "无窗/文本覆盖不足且实体未命中——需人工取材或复核")
            low_conf.append({"index": si, "fact": want[0].get("fact", "")[:40],
                             "conf": conf, "conf_ent": ent, "why": why})
            report_rows.append({"index": si, "status": "low_conf",
                                "conf": conf, "conf_ent": ent})
            continue
        (conf, bv, start, dur, meta, text), ent = best
        if not dur:
            local = _find_local(bv, dl_root)
            dur = _probe_dur(local) if local else 0.0
        if not dur:
            low_conf.append({"index": si, "fact": want[0].get("fact", "")[:40], "conf": conf,
                             "why": "源视频不在本地、时长未知，无法定 frac"})
            continue
        frac = min(1.0, max(0.0, start / max(1.0, dur - CLIP_DUR - 2)))
        used.setdefault(bv, []).append(start)
        sources.setdefault(bv, {"author": meta.get("author", ""),
                                "work": meta.get("title", ""),
                                "url": meta.get("url") or f"https://www.bilibili.com/video/{bv}"})
        clips.append({"index": si, "desc": _desc(want[0].get("fact") or sent),
                      "src": bv, "frac": round(frac, 4)})
        report_rows.append({"index": si, "status": "ok", "conf": conf, "conf_ent": ent,
                            "window": f"{start:.1f}s", "text": text[:60]})

    for it in orphans:
        report_rows.append({"index": it.get("index"), "status": "orphan",
                            "why": f"事实锚不上句子：{(it.get('fact') or '')[:30]}"})

    # 人工覆核落点（检查点决策落成数据，重跑不丢）：
    # 采集映射.manual.json = {"clips": {"<句号>": {"src": "BVxxx", "start": 秒, "desc": "…", "why": "…"}}}
    manual_file = workdir / "采集映射.manual.json"
    if manual_file.exists():
        try:
            manual = json.loads(manual_file.read_text(encoding="utf-8")).get("clips", {})
        except Exception:
            manual = {}
        for k in sorted(manual, key=lambda x: int(x)):
            idx = int(k)
            mv = manual[k]
            clips = [c for c in clips if c["index"] != idx]
            low_conf[:] = [lc for lc in low_conf if lc.get("index") != idx]
            report_rows[:] = [r for r in report_rows if r.get("index") != idx]
            bv = mv.get("src", "")
            local = _find_local(bv, dl_root)
            dur = _probe_dur(local) if local else 0.0
            if not dur:
                report_rows.append({"index": idx, "status": "manual_missing",
                                    "why": f"{bv} 不在本地，人工落点无法应用"})
                continue
            start = float(mv.get("start", 0.0))
            frac = min(1.0, max(0.0, start / max(1.0, dur - CLIP_DUR - 2)))
            if bv not in sources and local:
                stem = re.sub(r"\s*\[BV\w+\]$", "", local.stem)
                author, _, title = stem.partition(" - ")
                sources[bv] = {"author": author if title else "", "work": title or stem,
                               "url": f"https://www.bilibili.com/video/{bv}"}
            clips.append({"index": idx, "desc": mv.get("desc") or _desc(mv.get("why", "")),
                          "src": bv, "frac": round(frac, 4)})
            report_rows.append({"index": idx, "status": "manual", "why": mv.get("why", ""),
                                "window": f"{start:.1f}s"})
        clips.sort(key=lambda c: c["index"])

    mapping = {"topic": plan.get("topic", ""), "sources": sources, "clips": clips}
    (workdir / "采集映射.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"min_conf": min_conf, "sentences": len(sentences),
              "total_items": len(items), "clips": len(clips),
              "low_conf": low_conf, "rows": report_rows}
    (workdir / "落点报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"clips": len(clips), "low_conf": low_conf, "mapping": mapping}


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    wd = Path(sys.argv[1])
    r = auto_mapping(wd)
    print(f"落点：{r['clips']} 条进映射，{len(r['low_conf'])} 条低置信 → 落点报告.json")
