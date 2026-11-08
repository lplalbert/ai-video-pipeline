# -*- coding: utf-8 -*-
"""按 采集映射.json 从本地下载源切引用片段 → 采集/NNN_主题.mp4 + 采集/来源.json + 采集清单.md。

选段工单分工的「拍板与取材」本地版：对批准的候选视频按份额窗口切片，
署名写进 采集/来源.json（投稿包逐条署名从这里取），并生成人读的 采集清单.md。
用法: python -X utf8 cut_clips.py <workdir>
"""
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

FFMPEG = r"C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin\ffmpeg.exe"
FFPROBE = r"C:\ffmpeg-2025-11-02-git-f5eb11a71d-essentials_build\bin\ffprobe.exe"
DL_ROOT = Path("downloads")
CLIP_DUR = 10.0  # 每条引用片段切 10s（成片阶段按句长自动收紧）


def probe_dur(p: Path) -> float:
    out = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


def find_src(bv: str) -> Path:
    # 只认合成后的 `[BVxxx].mp4`，排除 yt-dlp 中间流 `.f100024.mp4`（无音轨）；
    # 注意不能把 [BV] 当 glob 字符类（坑⑯），用普通字符串 endswith。
    hits = [p for p in DL_ROOT.rglob("*.mp4") if p.name.endswith(f"{bv}].mp4")]
    if not hits:
        raise FileNotFoundError(f"下载目录里找不到 {bv} 的合成视频文件")
    return hits[0]


def main() -> int:
    workdir = Path(sys.argv[1])
    mapping = json.loads((workdir / "采集映射.json").read_text(encoding="utf-8"))
    sources_meta = mapping["sources"]      # {bv: {author, work, url}}
    clips = mapping["clips"]               # [{index, desc, src, frac}]
    outdir = workdir / "采集"
    outdir.mkdir(exist_ok=True)
    # 坑㉛：旧批次 NNN_*.mp4 不清理会与新批次共存，collect_search 按 index glob 撞错文件
    # （实测人工覆掉的错位切片经旧文件泄漏回成片）——批次以采集映射.json 为准，切前清空旧切片
    for old in outdir.glob("*.mp4"):
        old.unlink()

    dur_cache, src_map, lines = {}, {}, []
    for c in clips:
        bv = c["src"]
        if bv not in dur_cache:
            src = find_src(bv)
            dur_cache[bv] = (src, probe_dur(src))
        src, dur = dur_cache[bv]
        start = max(0.0, min(c["frac"] * (dur - CLIP_DUR - 2), dur - CLIP_DUR - 0.5))
        out = outdir / f"{c['index']:03d}_{c['desc']}.mp4"
        print(f"[{c['index']:02d}] {src.name[:40]} @{start:7.1f}s -> {out.name}")
        r = subprocess.run(
            [FFMPEG, "-y", "-ss", f"{start:.2f}", "-t", str(CLIP_DUR), "-i", str(src),
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  !! ffmpeg 失败：{r.stderr[-300:]}")
            return 1
        meta = sources_meta[bv]
        src_map[out.name] = meta
        lines.append(f"句{c['index']}：{out.name} —— 画面来源：@{meta['author']}《{meta['work']}》"
                     f"（{meta['url']}）（引用用途：评论/介绍，适当引用）")

    (outdir / "来源.json").write_text(
        json.dumps(src_map, ensure_ascii=False, indent=2), encoding="utf-8")
    (workdir / "采集清单.md").write_text(
        f"# 采集清单 — {mapping.get('topic', '')}\n\n" + "\n".join(f"- {ln}" for ln in lines)
        + "\n", encoding="utf-8")
    print(f"完成：{len(clips)} 条 → {outdir}；署名 → 采集/来源.json + 采集清单.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
