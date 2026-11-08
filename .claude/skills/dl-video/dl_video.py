#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yt-dlp 批量下载器 —— 关键词 → 搜索 → 下载 N 条到本地目录。

本脚本是 dl-video skill 的执行体，只做「搜 + 下 + 归档去重」，不碰剪辑/投稿。

用法（Windows 下 Python 一律带 -X utf8）：
  python -X utf8 dl_video.py "IG vs TES" --n 5
  python -X utf8 dl_video.py "IG vs TES" "lol s15 总决赛" --n 3 --source both
  python -X utf8 dl_video.py "IG vs TES" --n 8 --list
  python -X utf8 dl_video.py "IG vs TES" --n 5 --out 素材库/yt --max-height 720

参数：
  关键词           一个或多个搜索关键词（位置参数）；也可直接给视频 URL
  --n N            每个关键词下载几条（默认 5）
  --source S       youtube / bilibili / both（默认 youtube）
  --out DIR        输出根目录（默认 downloads/），按关键词建子目录
  --max-height H   分辨率上限（默认 1080）
  --min-dur S      时长下限（秒，0=不限）
  --max-dur S      时长上限（秒，0=不限）
  --list           只搜索列出候选，不下载
  --archive FILE   归档文件路径（默认 <out>/.dl_archive.txt，跨次去重）
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from yt_dlp import YoutubeDL
except ImportError:
    sys.exit("缺 yt-dlp：先 `pip install yt-dlp`")

SEARCH_FMT = {"youtube": "ytsearch{n}:{q}", "bilibili": "bilisearch{n}:{q}"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def slug(text: str, n: int = 32) -> str:
    t = re.sub(r"[^\w一-鿿]+", "-", text).strip("-")
    return (t[:n] or "dl").lower()


def looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "www."))


def targets_for(keywords: list, n: int, source: str) -> list:
    """每个 (关键词, 来源) 生成一个 yt-dlp 目标串；URL 原样透传。"""
    out = []
    srcs = [source] if source != "both" else ["youtube", "bilibili"]
    for kw in keywords:
        if looks_like_url(kw):
            out.append((kw, "url", kw))
        else:
            for s in srcs:
                out.append((kw, s, SEARCH_FMT[s].format(n=n, q=kw)))
    return out


def make_opts(out_dir: Path, max_height: int, archive: Path,
              min_dur: int, max_dur: int, cookies_from_browser: str = "",
              cookies_file: str = "") -> dict:
    def duration_filter(info, *, incomplete):
        d = info.get("duration")
        if d is not None:
            if min_dur and d < min_dur:
                return f"时长 {d}s < {min_dur}s"
            if max_dur and d > max_dur:
                return f"时长 {d}s > {max_dur}s"
        return None

    fmt = (f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]"
           f"/bv*[height<={max_height}]+ba"
           f"/b[height<={max_height}]/b")
    opts = {
        "outtmpl": str(out_dir / "%(uploader)s - %(title)s [%(id)s].%(ext)s"),
        "format": fmt,
        "merge_output_format": "mp4",
        "writeinfojson": True,
        "download_archive": str(archive),
        "match_filter": duration_filter,
        "ignoreerrors": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "sleep_interval": 1.0,
        "max_sleep_interval": 5.0,
        "sleep_requests": 0.8,
        "quiet": True,
        "noprogress": True,
        "no_warnings": False,
        # YouTube 需 JS 运行时解播放器响应，否则只暴露图片流。
        # 本机有 node 无 deno，故显式启用 node（yt-dlp 默认只认 deno）。
        "js_runtimes": {"node": {}},
    }
    # 登录 cookie（解 YouTube 人机验证墙）——须用户明确授权后开启
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    return opts


def list_only(target: str, n: int) -> None:
    """只搜不下载：列出候选视频。"""
    with YoutubeDL({"extract_flat": "in_playlist", "quiet": True}) as ydl:
        info = ydl.extract_info(target, download=False)
    entries = (info or {}).get("entries") or []
    print(f"\n候选 {len(entries)} 条：")
    for i, e in enumerate(entries, 1):
        dur = e.get("duration")
        ds = f"{int(dur) // 60}:{int(dur) % 60:02d}" if isinstance(dur, (int, float)) else "--"
        print(f"  {i:2d}. [{ds:>5}] {e.get('uploader') or '?' } — "
              f"{(e.get('title') or '')[:48]}")
        print(f"       {e.get('url') or e.get('id') or ''}")


def download_one(kw: str, src: str, target: str, out_root: Path,
                 max_height: int, archive: Path, min_dur: int, max_dur: int,
                 cookies_from_browser: str = "", cookies_file: str = "") -> list:
    out_dir = out_root / slug(kw)
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS}

    def hook(d):
        if d.get("status") == "finished":
            print(f"  ✓ 已下载：{Path(d.get('filename', '')).name}")

    opts = make_opts(out_dir, max_height, archive, min_dur, max_dur,
                     cookies_from_browser, cookies_file)
    opts["progress_hooks"] = [hook]
    print(f"\n>>> [{src}] 「{kw}」 → {out_dir}")
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([target])
    except Exception as exc:
        print(f"  ✗ 出错：{exc}", file=sys.stderr)

    after = {p.name for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS}
    return [str(out_dir / n) for n in sorted(after - before)]


def main() -> int:
    ap = argparse.ArgumentParser(description="yt-dlp 关键词批量下载器")
    ap.add_argument("keywords", nargs="+", help="搜索关键词（可多个）或视频 URL")
    ap.add_argument("--n", type=int, default=5, help="每关键词下载条数（默认 5）")
    ap.add_argument("--source", choices=["youtube", "bilibili", "both"],
                    default="bilibili", help="来源站（默认 bilibili；YouTube 有 PO-Token 反爬，2026-11-04 起弃用）")
    ap.add_argument("--out", default="downloads", help="输出根目录（默认 downloads）")
    ap.add_argument("--max-height", type=int, default=1080, help="分辨率上限（默认 1080）")
    ap.add_argument("--min-dur", type=int, default=0, help="时长下限秒（0=不限）")
    ap.add_argument("--max-dur", type=int, default=0, help="时长上限秒（0=不限）")
    ap.add_argument("--list", action="store_true", help="只搜不下载")
    ap.add_argument("--archive", default="", help="归档文件（默认 <out>/.dl_archive.txt）")
    ap.add_argument("--cookies-from-browser", default="", dest="cookies_from_browser",
                    metavar="BROWSER",
                    help="用浏览器登录 cookie 解验证墙（chrome/edge/firefox…）")
    ap.add_argument("--cookies", default="", dest="cookies_file",
                    help="cookies.txt 路径（Netscape 格式）")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    archive = Path(args.archive) if args.archive else out_root / ".dl_archive.txt"
    tgts = targets_for(args.keywords, args.n, args.source)

    if args.list:
        for _, _, t in tgts:
            print(f"\n=== {t} ===")
            list_only(t, args.n)
        return 0

    print(f"yt-dlp {__import__('yt_dlp').version.__version__} | 源={args.source} | "
          f"每关键词 {args.n} 条 | 上限 {args.max_height}p | 归档 {archive}")
    if args.cookies_from_browser:
        print(f"cookie 来源：{args.cookies_from_browser} 浏览器登录态")
    elif args.cookies_file:
        print(f"cookie 文件：{args.cookies_file}")
    else:
        print("未启用 cookie（下载可能撞人机验证墙）")
    all_new = []
    for kw, src, t in tgts:
        all_new += download_one(kw, src, t, out_root, args.max_height,
                                archive, args.min_dur, args.max_dur,
                                args.cookies_from_browser, args.cookies_file)

    total = sum(Path(f).stat().st_size for f in all_new)
    print(f"\n=== 完成：新下载 {len(all_new)} 个，共 {total / 1e6:.1f} MB ===")
    for f in all_new:
        print(f"  {f}")
    if not all_new:
        print("  （本批无新增——可能都已在归档里，或搜索无命中）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
