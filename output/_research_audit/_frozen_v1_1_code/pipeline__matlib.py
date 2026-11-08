"""素材库积攒工具：给定关键词 → Pexels/Pixabay（免费可商用）→ 本地素材库 素材库/。

用途：批量把「可商用」视频片段攒进本地素材库，供流水线 footage.local_search 复用。
      产线取材时本地命中即免联网、免版权风险。

合规红线（与全项目一致）：
- 只取明确**免费可商用**授权的来源（Pexels License / Pixabay Content License），逐条留痕；
- **不做系统性批量扒库**（Pixabay 条款明令禁止）——增量攒池 OK，爬全库不行。
  故内置：单关键词上限 MATLIB_PER_KW_CAP、单次总量上限 MATLIB_TOTAL_CAP、
  请求间隔 MATLIB_SLEEP_S、搜索结果 24h 磁盘缓存（Pixabay 要求请求缓存 24h）；
- 素材本身授权宽松，但**用在哪**仍受项目红线约束（不洗稿/不搬运/逐条署名/AIGC 标注）。

存放：素材库/{来源}_{id}.mp4 + 素材库/_index.json（逐条来源+授权+下载时间留痕）。

用法：
  python -m pipeline.matlib --kw "太空站" "宇宙" "航天器" --per-kw 6
  python -m pipeline.matlib --kw-file keywords.txt --per-kw 8 --min-width 1280
  python -m pipeline.matlib --from-topic        # 从选题库最高分选题的 keywords 取词
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from config import (LIB_DIR, MATLIB_PER_KW_CAP, MATLIB_SLEEP_S, MATLIB_TOTAL_CAP,
                    PEXELS_KEY, PIXABAY_KEY)

USER_AGENT = "ai-video-matlib/1.0 (personal stock-footage collector)"
CACHE_DIR = LIB_DIR / "_cache"      # 搜索结果 24h 缓存（Pixabay 条款要求）
INDEX_NAME = "_index.json"
CACHE_TTL_S = 24 * 3600

# 每条素材的固定留痕字段
LICENSE_PEXELS = "Pexels License（免费可商用，无需署名）"
LICENSE_PIXABAY = "Pixabay Content License（免费可商用；展示处宜标注来源）"


# ---------- 24h 搜索缓存（避免重复请求 + 守 Pixabay「请求应缓存24h」） ----------

def _cache_key(url: str, params: dict) -> str:
    raw = url + "?" + json.dumps(params, sort_keys=True, ensure_ascii=False)
    return re.sub(r"[^0-9A-Za-z]+", "_", raw)[:120]


def _cache_get(url: str, params: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (_cache_key(url, params) + ".json")
    if f.exists() and (time.time() - f.stat().st_mtime) < CACHE_TTL_S:
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _cache_put(url: str, params: dict, payload) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (_cache_key(url, params) + ".json")
    try:
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------- 检索 ----------

def _norm(item: dict) -> dict:
    """统一成 {url,width,height,duration,source,license,attribution,provider}。"""
    return {
        "url": item["url"], "width": item.get("width"), "height": item.get("height"),
        "duration": item.get("duration"), "source": item["source"],
        "license": item["license"], "attribution": item.get("attribution", ""),
        "provider": item["provider"],
    }


def pexels_search(query: str, per_page: int = 5, min_width: int = 1280) -> list:
    """Pexels 视频检索（免费可商用）。key 缺失返回空。"""
    if not PEXELS_KEY:
        return []
    url = "https://api.pexels.com/videos/search"
    params = {"query": query, "per_page": per_page, "orientation": "landscape"}
    cached = _cache_get(url, params)
    if cached is None:
        try:
            r = requests.get(url, headers={"Authorization": PEXELS_KEY,
                                           "User-Agent": USER_AGENT},
                             params=params, timeout=30)
            r.raise_for_status()
            cached = r.json()
            _cache_put(url, params, cached)
        except requests.RequestException:
            return []
    out = []
    for v in cached.get("videos", []):
        files = [f for f in v.get("video_files", []) if (f.get("width") or 0) >= min_width]
        if not files:
            continue
        best = min(files, key=lambda f: f.get("width", 99999))
        user = v.get("user") or {}
        out.append(_norm({
            "url": best["link"], "width": best.get("width"), "height": best.get("height"),
            "duration": v.get("duration"), "source": f"pexels:{v.get('id')}",
            "license": LICENSE_PEXELS, "provider": "pexels",
            "attribution": user.get("name") or "",
        }))
    return out


def pixabay_search(query: str, per_page: int = 5, min_width: int = 1280) -> list:
    """Pixabay 视频检索（免费可商用）。key 缺失返回空。

    注意：Pixabay 条款「请求须缓存24h、禁止系统性批量下载」——已由缓存+限速+上限兜住。
    """
    if not PIXABAY_KEY:
        return []
    url = "https://pixabay.com/api/videos/"
    params = {"key": PIXABAY_KEY, "q": query, "per_page": per_page, "safesearch": "true"}
    cached = _cache_get(url, params)
    if cached is None:
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT},
                             params=params, timeout=30)
            r.raise_for_status()
            cached = r.json()
            _cache_put(url, params, cached)
        except requests.RequestException:
            return []
    out = []
    for h in cached.get("hits", []):
        vids = h.get("videos", {})
        # 取满足宽度的最小档（够用即可，省流量）
        cand = [v for v in (vids.get("large"), vids.get("medium"), vids.get("small"))
                if v and (v.get("width") or 0) >= min_width]
        if not cand:
            cand = [v for v in vids.values() if v and v.get("url")]
        if not cand:
            continue
        best = min(cand, key=lambda v: v.get("width", 99999))
        out.append(_norm({
            "url": best["url"], "width": best.get("width"), "height": best.get("height"),
            "duration": h.get("duration"), "source": f"pixabay:{h.get('id')}",
            "license": LICENSE_PIXABAY, "provider": "pixabay",
            "attribution": h.get("user") or "",
        }))
    return out


def search_all(query: str, per_page: int, min_width: int) -> list:
    """Pexels + Pixabay 合并去重（按 URL）。"""
    seen, out = set(), []
    for fn in (pexels_search, pixabay_search):
        for it in fn(query, per_page=per_page, min_width=min_width):
            if it["url"] not in seen:
                seen.add(it["url"])
                out.append(it)
        time.sleep(MATLIB_SLEEP_S)  # 限速：两源之间也留间隔
    return out


# ---------- 下载 + 留痕 ----------

def _load_index() -> list:
    f = LIB_DIR / INDEX_NAME
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_index(entries: list) -> None:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    (LIB_DIR / INDEX_NAME).write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _already(entry_url: str, index: list) -> bool:
    return any(e.get("url") == entry_url for e in index)


def _download(url: str, dest: Path) -> bool:
    if dest.exists() and dest.stat().st_size > 10000:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(url, stream=True, timeout=120,
                          headers={"User-Agent": USER_AGENT}) as r:
            r.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        return dest.stat().st_size > 10000
    except requests.RequestException:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return False


def _slug(text: str, n: int = 20) -> str:
    s = re.sub(r"[^\w一-鿿]+", "", text)[:n]
    return s or "clip"


def collect(keywords: list, per_kw: int, min_width: int,
            use_pexels: bool = True, use_pixabay: bool = True) -> list:
    """给定关键词批量攒素材入 素材库/，返回新增留痕条目。"""
    per_kw = min(per_kw, MATLIB_PER_KW_CAP)
    index = _load_index()
    added = []
    total = 0
    for kw in keywords:
        kw = kw.strip()
        if not kw or total >= MATLIB_TOTAL_CAP:
            continue
        print(f"[matlib] 检索：{kw}", flush=True)
        pool = []
        if use_pexels:
            pool += pexels_search(kw, per_page=per_kw, min_width=min_width)
        if use_pixabay:
            pool += pixabay_search(kw, per_page=per_kw, min_width=min_width)
        time.sleep(MATLIB_SLEEP_S)

        got_this_kw = 0
        for item in pool:
            if got_this_kw >= per_kw or total >= MATLIB_TOTAL_CAP:
                break
            if _already(item["url"], index):
                continue  # 同一 URL 不重复存
            dest = LIB_DIR / f"{item['provider']}_{_slug(kw)}_{abs(hash(item['url'])) % 10**8}.mp4"
            if not _download(item["url"], dest):
                continue
            entry = {
                "file": dest.name, "keyword": kw, "url": item["url"],
                "source": item["source"], "provider": item["provider"],
                "license": item["license"], "attribution": item["attribution"],
                "width": item["width"], "height": item["height"],
                "duration": item["duration"], "downloaded_at": datetime.now().isoformat(timespec="seconds"),
            }
            index.append(entry)
            added.append(entry)
            got_this_kw += 1
            total += 1
            print(f"  + {dest.name}  [{item['source']}] {item['license']}", flush=True)
        print(f"[matlib] {kw}：新增 {got_this_kw} 条", flush=True)
    _save_index(index)
    return added


def _keywords_from_topic() -> list:
    """从选题库最高分选题取 keywords。"""
    try:
        from pipeline import topics
        conn = topics.db()
        row = conn.execute(
            "SELECT keywords FROM topics ORDER BY score_total DESC LIMIT 1").fetchone()
        if row and row["keywords"]:
            return [k for k in json.loads(row["keywords"]) if isinstance(k, str)]
    except Exception as exc:
        print(f"[matlib] 读取选题库失败：{exc}", file=sys.stderr)
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="素材库积攒：关键词 → Pexels/Pixabay → 本地素材库")
    ap.add_argument("--kw", nargs="*", default=[], help="一个或多个搜索关键词")
    ap.add_argument("--kw-file", default="", help="关键词文件（每行一个）")
    ap.add_argument("--from-topic", action="store_true", help="从选题库最高分选题取 keywords")
    ap.add_argument("--per-kw", type=int, default=6, help=f"每关键词最多存几条（≤{MATLIB_PER_KW_CAP}）")
    ap.add_argument("--min-width", type=int, default=1280, help="最小分辨率宽")
    ap.add_argument("--no-pexels", action="store_true", help="不用 Pexels 源")
    ap.add_argument("--no-pixabay", action="store_true", help="不用 Pixabay 源")
    args = ap.parse_args()

    kws = list(args.kw)
    if args.kw_file:
        p = Path(args.kw_file)
        if p.exists():
            kws += [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
                    if ln.strip()]
    if args.from_topic:
        kws += _keywords_from_topic()
    # 去重保序
    seen, uniq = set(), []
    for k in kws:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    if not uniq:
        ap.error("没给关键词：用 --kw / --kw-file / --from-topic 之一")

    if not (PEXELS_KEY or PIXABAY_KEY):
        print("[matlib] 警告：PEXELS_API_KEY / PIXABAY_API_KEY 都没配——无源可检索。\n"
              "         填进 .env 后重试（两个都是免费 key）。", file=sys.stderr)
        return 2

    added = collect(uniq, per_kw=args.per_kw, min_width=args.min_width,
                    use_pexels=not args.no_pexels, use_pixabay=not args.no_pixabay)
    print(f"\n[matlib] 完成：新增 {len(added)} 条入 素材库/（{_load_index().__len__()} 条总留痕）")
    if added:
        print("[matlib] 提示：素材授权宽松，但成片仍须过「逐条署名 + AIGC 标注 + 引用比例」自查。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
