#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""youtube-dl YouTube 下载器 —— 关键词→按上传日期搜→精选 N 条→下载合并 mp4。

本脚本是 dl-video skill 的 **YouTube 引擎**（B站/其他站点用同目录 dl_video.py + yt-dlp）。
执行体是 ytdl-org/youtube-dl，**必须 GitHub master 版**（PyPI 的 2021.12.17 解析不了
现在的 YouTube）：pip install git+https://github.com/ytdl-org/youtube-dl.git

只做「搜 + 精选 + 下 + 归档去重」，不碰剪辑/投稿；下载物不自动进产线。

用法（Windows 下 Python 一律带 -X utf8）：
  python -X utf8 dl_ytdl.py "Golden State Warriors Full Game Highlights" --n 3 --per-day 1
  python -X utf8 dl_ytdl.py "IG vs TES" --n 8 --list
  python -X utf8 dl_ytdl.py "https://www.youtube.com/watch?v=XXXX"
  python -X utf8 dl_ytdl.py "NBA highlights" --n 5 --min-dur 300 --max-dur 1800

参数：
  关键词           一个或多个搜索关键词（位置参数）；也可直接给视频 URL
  --n N            每个关键词下载几条（默认 5）
  --sort S         date=按上传日期新→旧（默认，取“最新”）；rel=按相关度
  --per-day K      同一上传日期最多取 K 条（0=不限，默认 0）。
                   同一场比赛/事件常有多条重复上传，取 1 即“每场一条”
  --out DIR        输出根目录（默认 downloads/），按关键词建子目录；URL 归入 url/
  --max-height H   分辨率上限（默认 1080）
  --min-dur S      时长下限（秒，0=不限）
  --max-dur S      时长上限（秒，0=不限）
  --list           只列候选不下载（标注本次会被选中哪几条）
  --archive FILE   归档文件路径（默认 <out>/.dl_archive.txt，跨次去重）
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

if importlib.util.find_spec("youtube_dl") is None:
    sys.exit("缺 youtube-dl（须 GitHub master 版）："
             "pip install git+https://github.com/ytdl-org/youtube-dl.git")

# 一律子进程调 `python -X utf8 -m youtube_dl`：不依赖 PATH 上的 youtube-dl.exe
YTDL = [sys.executable, "-X", "utf8", "-m", "youtube_dl"]
# 日期#时长#ID#标题 —— 标题里的 # 不影响（只从左切 3 刀）
META_FMT = "%(upload_date)s#%(duration)s#%(id)s#%(title)s"
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
PROGRESS_RE = re.compile(r"% of .*at ")  # 进度行（--newline 输出），转发时滤掉


def slug(text: str, n: int = 32) -> str:
    t = re.sub(r"[^\w一-鿿]+", "-", text).strip("-")
    return (t[:n] or "dl").lower()


def looks_like_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "www."))


def load_archived_ids(archive: Path) -> set:
    """归档文件形如 `youtube <id>`，取已下过的 ID 用于精选时跳过。"""
    ids = set()
    if archive.exists():
        for line in archive.read_text(encoding="utf-8", errors="replace").split():
            if len(line) == 11 and " " not in line:
                ids.add(line)
    return ids


def search_candidates(kw: str, pool: int, sort: str) -> list:
    """搜 pool 条候选，返回 [{date, dur, id, title}]（按 sort 策略排序）。"""
    prefix = "ytsearchdate" if sort == "date" else "ytsearch"
    target = f"{prefix}{pool}:{kw}"
    proc = subprocess.run(
        YTDL + ["--get-filename", "-o", META_FMT, target],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split("#", 3)
        if len(parts) != 4:
            continue
        date, dur, vid, title = parts
        rows.append({
            "date": date if date.isdigit() else "",
            "dur": int(dur) if dur.isdigit() else None,
            "id": vid,
            "title": title,
        })
    if sort == "date":
        # YouTube 的日期排序偶发不严格，落盘前按日期新→旧兜底
        rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def select(rows: list, n: int, min_dur: int, max_dur: int, per_day: int,
           done_ids: set) -> tuple:
    """按 每日限额 → 已下跳过（但占当日名额）→ 时长过滤 选前 n 条。

    已下条目必须占当日名额，否则会去下载同场次的另一条重复上传。
    返回 (选中, 被滤掉原因计数)。
    """
    picked, per, skipped = [], {}, {"时长不符": 0, "已在归档": 0, "超出每日限额": 0}
    for r in rows:
        if per_day and per.get(r["date"], 0) >= per_day:
            skipped["超出每日限额"] += 1
            continue
        if r["id"] in done_ids:
            per[r["date"]] = per.get(r["date"], 0) + 1
            skipped["已在归档"] += 1
            continue
        if r["dur"] is not None:
            if min_dur and r["dur"] < min_dur:
                skipped["时长不符"] += 1
                continue
            if max_dur and r["dur"] > max_dur:
                skipped["时长不符"] += 1
                continue
        per[r["date"]] = per.get(r["date"], 0) + 1
        picked.append(r)
        if len(picked) >= n:
            break
    return picked, skipped


def mmss(sec) -> str:
    return f"{int(sec) // 60}:{int(sec) % 60:02d}" if isinstance(sec, int) else "--:--"


def download(urls: list, out_dir: Path, max_height: int, archive: Path) -> list:
    out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS}
    fmt = (f"bestvideo[height<={max_height}]+bestaudio"
           f"/best[height<={max_height}]/best")
    cmd = YTDL + [
        "-f", fmt, "--merge-output-format", "mp4", "--newline",
        "--sleep-interval", "2", "--retries", "5", "--fragment-retries", "5",
        "--no-playlist", "--write-info-json",
        "--download-archive", str(archive),
        "-o", str(out_dir / "%(upload_date)s - %(title)s.%(ext)s"),
    ] + urls
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        if not PROGRESS_RE.search(line):
            print("  " + line.rstrip())
    proc.wait()
    if proc.returncode not in (0, None):
        print(f"  ✗ youtube-dl 退出码 {proc.returncode}（部分失败不影响已成文件）",
              file=sys.stderr)
    after = {p.name for p in out_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS}
    return [str(out_dir / n) for n in sorted(after - before)]


def main() -> int:
    ap = argparse.ArgumentParser(description="youtube-dl YouTube 精选下载器")
    ap.add_argument("keywords", nargs="+", help="搜索关键词（可多个）或视频 URL")
    ap.add_argument("--n", type=int, default=5, help="每关键词下载条数（默认 5）")
    ap.add_argument("--sort", choices=["date", "rel"], default="date",
                    help="date=按上传日期新→旧（默认）；rel=按相关度")
    ap.add_argument("--per-day", type=int, default=0, dest="per_day",
                    help="同一上传日期最多取几条（0=不限；同场多条重复上传时取 1）")
    ap.add_argument("--out", default="downloads", help="输出根目录（默认 downloads）")
    ap.add_argument("--max-height", type=int, default=1080, help="分辨率上限（默认 1080）")
    ap.add_argument("--min-dur", type=int, default=0, help="时长下限秒（0=不限）")
    ap.add_argument("--max-dur", type=int, default=0, help="时长上限秒（0=不限）")
    ap.add_argument("--list", action="store_true", help="只列候选不下载")
    ap.add_argument("--archive", default="", help="归档文件（默认 <out>/.dl_archive.txt）")
    args = ap.parse_args()

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    archive = Path(args.archive) if args.archive else out_root / ".dl_archive.txt"
    done_ids = load_archived_ids(archive)

    ver = subprocess.run(YTDL + ["--version"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    print(f"youtube-dl {ver.stdout.strip() or '?'} | 排序={args.sort} | "
          f"每关键词 {args.n} 条 | 上限 {args.max_height}p | 归档 {archive}")

    all_new = []
    for kw in args.keywords:
        if looks_like_url(kw):
            if args.list:
                print(f"\n=== {kw} ===（URL 直下，不走搜索）")
                continue
            print(f"\n>>> [url] → {out_root / 'url'}")
            all_new += download([kw], out_root / "url", args.max_height, archive)
            continue

        rows = search_candidates(kw, pool=max(args.n * 3, 12), sort=args.sort)
        if not rows:
            print(f"\n=== 「{kw}」无搜索结果 ===")
            continue
        picked, skipped = select(rows, args.n, args.min_dur, args.max_dur,
                                 args.per_day, done_ids)

        if args.list:
            print(f"\n=== 「{kw}」候选 {len(rows)} 条（✓=本次将下载，共 {len(picked)}）===")
            pick_ids = {r["id"] for r in picked}
            for r in rows:
                mark = "✓" if r["id"] in pick_ids else (
                    "已下" if r["id"] in done_ids else " ")
                print(f"  [{mark:>2}] {r['date']} | {mmss(r['dur'])} | "
                      f"{r['id']} | {r['title'][:60]}")
            continue

        if not picked:
            print(f"\n=== 「{kw}」无可下载条目（过滤后剩 0）：{skipped} ===")
            continue
        print(f"\n>>> 「{kw}」选中 {len(picked)} 条：")
        for r in picked:
            print(f"  - {r['date']} | {mmss(r['dur'])} | {r['title'][:56]}")
        if any(skipped.values()):
            print(f"  （跳过：{skipped}）")
        all_new += download([f"https://www.youtube.com/watch?v={r['id']}"
                             for r in picked],
                            out_root / slug(kw), args.max_height, archive)

    if not args.list:
        total = sum(Path(f).stat().st_size for f in all_new)
        print(f"\n=== 完成：新下载 {len(all_new)} 个，共 {total / 1e6:.1f} MB ===")
        for f in all_new:
            print(f"  {f}")
        if not all_new:
            print("  （本批无新增——可能都已在归档里，或候选被过滤）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
