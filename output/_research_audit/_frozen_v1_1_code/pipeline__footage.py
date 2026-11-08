"""素材引擎：本地素材库 + Pexels（免费可商用）→ 分镜 manifest。

只用可商用授权素材；每条素材记录来源与授权，写入 manifest 留痕。
找不到素材时用自制字卡（pipeline/cardgen.py）兜底——零版权风险且是有效信息画面，
保证流水线永远能出片。
"""
import json
from pathlib import Path

import requests

from config import LIB_DIR, PEXELS_KEY
from pipeline.cardgen import render_card
from pipeline.llm import extract_json, flash

KEYWORD_PROMPT = """把下面每句口播文案转换成一个视频素材搜索关键词。
要求：选最能代表这句话画面的具象名词/场景（2-4个实义词），英文小写。
只输出严格JSON数组：
[{{"index":0,"en":"english search keywords","zh":"中文关键词"}}]

句子列表：
{sentences}"""

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def collect_search(workdir: Path, index: int) -> list:
    """每视频人工采集目录（workdir/采集/）：按采集清单取回的素材优先使用。

    命名约定：{index:03d}_描述.mp4 / .png（如 005_BP环节.mp4），
    index = 句子段号（segments.json 的 index，000 起），一句一段号。
    """
    cdir = workdir / "采集"
    if not cdir.exists():
        return []
    out = []
    for f in sorted(cdir.glob(f"{index:03d}_*")):
        if f.suffix.lower() in VIDEO_EXTS | IMAGE_EXTS:
            out.append({"url": str(f), "width": None, "height": None, "duration": None,
                        "source": f"collect:{f.name}",
                        "license": "引用/自采素材（来源与署名见采集清单，投稿包生成声明）"})
    return out


def extract_keywords(sentences: list) -> list:
    """每句 → 一组英文搜索词（用 flash，成本忽略不计）。"""
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(sentences))
    raw = flash(KEYWORD_PROMPT.format(sentences=numbered), max_tokens=3000)
    kws = extract_json(raw)
    by_index = {k["index"]: k for k in kws if isinstance(k, dict)}
    result = []
    for i, s in enumerate(sentences):
        k = by_index.get(i, {})
        result.append({"index": i, "text": s,
                       "en": (k.get("en") or "abstract background").strip(),
                       "zh": (k.get("zh") or "").strip()})
    return result


def pexels_search(query: str, min_width: int = 1280, per_page: int = 5) -> list:
    """Pexels 视频检索，返回 {url, width, height, duration, source, license}。"""
    if not PEXELS_KEY:
        return []
    try:
        resp = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_KEY},
            params={"query": query, "per_page": per_page, "orientation": "landscape"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []
    out = []
    for video in resp.json().get("videos", []):
        files = [f for f in video.get("video_files", []) if (f.get("width") or 0) >= min_width]
        if not files:
            continue
        best = min(files, key=lambda f: f.get("width", 99999))
        out.append({
            "url": best["link"], "width": best.get("width"), "height": best.get("height"),
            "duration": video.get("duration"), "source": f"pexels:{video.get('id')}",
            "license": "Pexels License（可商用，无需署名）",
        })
    return out


def local_search(keyword_zh: str) -> list:
    """本地素材库模糊匹配：文件名包含关键词任一 2 字片段即命中。"""
    tokens = [keyword_zh[i:i + 2] for i in range(len(keyword_zh) - 1)] if keyword_zh else []
    out = []
    for f in LIB_DIR.rglob("*"):
        if f.suffix.lower() in VIDEO_EXTS:
            name = f.stem
            if not tokens or any(t in name for t in tokens):
                out.append({"url": str(f), "width": None, "height": None,
                            "duration": None, "source": f"local:{f.name}",
                            "license": "本地素材库（使用者自证授权）"})
    return out


def _download(url: str, dest_dir: Path, idx: int) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"clip_{idx:03d}.mp4"
    if dest.exists() and dest.stat().st_size > 10000:
        return dest
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    return dest


def build_manifest(segments: list, workdir: Path, keyword_pool: list = None) -> list:
    """为每个句子段落选一条素材 → manifest.json（含授权留痕）。

    keyword_pool：选题阶段产出的英文 B-roll 检索词（来自 topics.keywords），
    句级检索无命中时按池轮换兜底，最后才落到纯色背景。
    """
    kws = extract_keywords([s["text"] for s in segments])
    pool = list(keyword_pool or [])
    manifest = []
    for kw in kws:
        # 优先级：本视频人工采集 > 本地素材库/Pexels > 池兜底 > 自制字卡
        candidates = collect_search(workdir, kw["index"]) \
            or pexels_search(kw["en"]) or local_search(kw["zh"])
        if not candidates and pool:
            fallback_q = pool[kw["index"] % len(pool)]
            candidates = pexels_search(fallback_q) or local_search(fallback_q)
            if candidates:
                kw["en"] = fallback_q
        if candidates:
            pick = candidates[kw["index"] % len(candidates)]
            clip = _download(pick["url"], workdir / "clips", kw["index"]) \
                if pick["url"].startswith("http") else Path(pick["url"])
            entry = {"index": kw["index"], "text": kw["text"], "query": kw["en"],
                     "clip": str(clip), "source": pick["source"], "license": pick["license"]}
        else:
            # 自制字卡兜底（零版权风险；比纯色更抗低质判定）
            card = workdir / "cards" / f"card_{kw['index']:03d}.png"
            render_card(kw["text"], card, keyword=kw.get("zh", ""))
            entry = {"index": kw["index"], "text": kw["text"], "query": kw["en"],
                     "clip": str(card), "source": "self-card",
                     "license": "自制字卡（自动生成，零版权风险）"}
        manifest.append(entry)
    (workdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
