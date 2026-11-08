"""选段辅助：批量发现候选视频 + 定位重要片段，人来最终拍板。

分工（合规红线不变）：
- 本模块做「发现与定位」：搜公开元数据、分析公开字幕，输出带时间戳的引用候选时间窗；
- 「拍板与取材」由人完成：对批准的时间窗录屏（或平台官方下载），丢进 workdir/采集/；
- 不做自动下载/录制工具；批量抓取平台视频、模拟鼠标绕反爬虫 = 红线，不做。

能力：
- bili_search / bili_subtitles：B站公开检索与 AI/CC 字幕（定位引用片段用）；
- locate_in_subs：按事实关键词在字幕里定位引用窗（长度以覆盖语境为准，不设合规配额）；
- discover_for_plan：对采集工单逐项出「选段工单.md + quotes.json」，含署名格式。

用法：python -m pipeline.sources <workdir>   （对已有 采集计划.json 重新选段）
"""
import json
import re
import time
import urllib.parse
from pathlib import Path

import requests

from config import BILI_SESSDATA

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BILI_API = "https://api.bilibili.com"
_QUOTE_WIN_S = 45.0  # 候选建议窗长度（工具参数，便于人工浏览定位；非合规配额）

_STOP = set("的了是在和与及这那有就都不一我也你们个为对上中到说等而其但")


def _get(url: str, params: dict = None, cookies: dict = None, timeout: int = 15):
    ck = dict(cookies or {})
    if BILI_SESSDATA:
        ck.setdefault("SESSDATA", BILI_SESSDATA)  # 字幕接口 need_login_subtitle
    try:
        r = requests.get(url, params=params, headers={"User-Agent": UA,
                                                      "Referer": "https://www.bilibili.com/"},
                         cookies=ck, timeout=timeout)
        return r.json()
    except Exception:
        return {}


def _bigrams(text: str) -> set:
    t = re.sub(r"[^\w一-鿿]", "", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)
            if t[i] not in _STOP and t[i + 1] not in _STOP}


def bili_search(keyword: str, n: int = 5) -> list:
    """B站公开视频检索；失败返回空（外层会退化为手动检索链接）。"""
    import uuid

    cookies = {"buvid3": f"{uuid.uuid4()}infoc"}
    d = _get(f"{BILI_API}/x/web-interface/search/type",
             {"search_type": "video", "keyword": keyword, "page": 1, "page_size": 20},
             cookies=cookies)
    out = []
    for it in (d.get("data") or {}).get("result") or []:
        bvid = it.get("bvid", "")
        if not bvid:
            continue
        dur = 0
        for part in str(it.get("duration", "0:0")).split(":"):
            dur = dur * 60 + (int(part) if part.isdigit() else 0)
        title = re.sub(r"<[^>]+>", "", it.get("title", ""))  # 去高亮标签
        out.append({"bvid": bvid, "title": title, "author": it.get("author", ""),
                    "play": it.get("play", 0), "duration": dur,
                    "url": f"https://www.bilibili.com/video/{bvid}"})
        if len(out) >= n:
            break
    return out


def bili_subtitles(bvid: str) -> list:
    """取该视频公开字幕（AI/CC）文本行 [{from,to,content}]；无字幕返回空。"""
    v = _get(f"{BILI_API}/x/web-interface/view", {"bvid": bvid}).get("data") or {}
    cid = v.get("cid")
    if not cid:
        return []
    p = _get(f"{BILI_API}/x/player/v2", {"bvid": bvid, "cid": cid}).get("data") or {}
    subs = (p.get("subtitle") or {}).get("subtitles") or []
    for s in subs:
        url = s.get("subtitle_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            continue
        d = _get(url)
        body = d.get("body") or []
        if body:
            return [{"from": b.get("from", 0), "to": b.get("to", 0),
                     "content": b.get("content", "")} for b in body]
    return []


def locate_in_subs(lines: list, fact: str, max_s: float = _QUOTE_WIN_S) -> list:
    """按事实关键词打分定位引用窗，返回 top 候选 [{start,end,text,score}]。"""
    grams = _bigrams(fact)
    if not grams or not lines:
        return []
    scored = []
    for ln in lines:
        s = len(grams & _bigrams(ln["content"]))
        if s > 0:
            scored.append((s, ln))
    scored.sort(key=lambda x: -x[0])
    out, used = [], []
    for s, ln in scored[:12]:
        start = max(0.0, ln["from"] - 1.0)
        if any(abs(start - u) < 6 for u in used):
            continue
        used.append(start)
        # 引用窗=锚定行起 max_s 秒（曾 min(start+max_s, line.to+2) 把窗钳成一行字，坑㉗同簇）
        end = min(start + max_s, lines[-1]["to"] + 2.0)
        text = " ".join(x["content"] for x in lines
                        if x["from"] < end and x["to"] > start)[:80]
        out.append({"start": round(start, 1), "end": round(end, 1),
                    "text": text, "score": s})
        if len(out) >= 2:
            break
    return out


def _fmt_ts(sec: float) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}"


def _match_ok(title: str, topic: str) -> bool:
    """错场次过滤：选题带两支以上拉丁队名（如 IG 3-1 TES）时，
    对阵类标题（含 vs）必须同时提到全部队名——防止 LYON vs IG 混入 TES vs IG 的工单。"""
    teams = {t.lower() for t in re.findall(r"[A-Za-z]{2,}", topic or "")}
    if len(teams) < 2:
        return True
    tl = title.lower()
    if "vs" not in tl:
        return True
    return all(t in tl for t in teams)


def discover_for_plan(plan: dict, workdir: Path, n_videos: int = 2) -> Path:
    """对采集计划逐项发现候选视频并定位引用窗 → 选段工单.md + quotes.json。"""
    workdir.mkdir(parents=True, exist_ok=True)
    topic = plan.get("topic", "")
    rows = []
    for it in plan.get("items", []):
        if it.get("visual") in ("无", "自制字卡", "数据图表"):
            continue
        query = (it.get("entity") or it.get("fact") or "")[:16]
        if not query:
            continue
        cands = [c for c in bili_search(query, n=n_videos * 2)
                 if _match_ok(c["title"], topic)][:n_videos]
        for c in cands:
            time.sleep(1.5)  # 频控
            subs = bili_subtitles(c["bvid"])
            wins = locate_in_subs(subs, it.get("fact", "")) if subs else []
            rows.append({"item": it, "video": c, "windows": wins,
                         "signature": f"画面来源：@{c['author']}《{c['title']}》（{c['url']}）"})
        time.sleep(1.5)

    (workdir / "quotes.json").write_text(
        json.dumps({"topic": topic, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    md = [f"# 选段工单 — {topic}", "",
          "> 分工：本单自动**发现＋定位**；**拍板＋取材**由你完成——",
          "> 批准某时间窗后对该段录屏（或平台官方下载），命名 `序号_描述.mp4` 丢进 `采集/`；",
          "> 署名格式已给好，复制进投稿包来源声明。引用量以支撑评论为限（法律不设秒数/比例上限）。", ""]
    for row in rows:
        it, v = row["item"], row["video"]
        md += [f"## 句{it.get('index')} · {str(it.get('fact', ''))[:30]}",
               "",
               f"**候选视频**：[{v['title']}]({v['url']})　@{v['author']}　"
               f"播放 {v['play']}　时长 {_fmt_ts(v['duration'])}", ""]
        if row["windows"]:
            md += ["| 采用 | 建议时间窗 | 引用文本（字幕摘录） |",
                   "|---|---|---|"]
            for w in row["windows"]:
                md.append(f"| [ ] | {_fmt_ts(w['start'])}-{_fmt_ts(w['end'])} | {w['text']} |")
            md += ["", f"取材：录屏建议时间窗 → `采集/{it.get('index', 0):03d}_片段描述.mp4`",
                   f"署名：{row['signature']}", ""]
        else:
            md += ["（该视频无公开字幕或未命中关键词——人工浏览定位后取材）",
                   f"署名：{row['signature']}", ""]
    md += ["## 手动补充渠道（无字幕/未命中时）",
           "",
           f"- YouTube 检索：<https://www.youtube.com/results?search_query="
           f"{urllib.parse.quote((plan.get('topic') or '')[:20])}>",
           f"- 抖音/B站站内检索：关键词见各项「事实点」", ""]
    out = workdir / "选段工单.md"
    out.write_text("\n".join(md), encoding="utf-8")
    return out


def main() -> int:
    import sys

    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    wd = Path(sys.argv[1])
    plan = json.loads((wd / "采集计划.json").read_text(encoding="utf-8"))
    out = discover_for_plan(plan, wd)
    print("done:", out)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
