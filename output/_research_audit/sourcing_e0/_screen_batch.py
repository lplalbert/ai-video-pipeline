# E0 新题 26 题候选筛查批（2026-11-08）：搜索→全量元数据→candidates.tsv
# 坑⑬口径：flat 元数据不作数，逐条全量提取；412 冷却重试，仍败记 FAIL 行继续（FAIL 行留痕，续跑自动重试该词）。
# 修 bug 记录（2026-11-08）：flat 条目 id 为纯数字 av 号，av/BV 前缀在 url/webpage_url_basename 里——
# v1 按 id.startswith("av"/"BV") 过滤导致 20 词静默零行；v2 改从 url basename 提取（回落 av+id）。
import json
import subprocess
import sys
import time
from pathlib import Path

OUT = Path(__file__).parent / "candidates.tsv"
KEYWORDS = [
    # 格1 赛事×高 +4
    ("格1", "SKT ROX 复盘"), ("格1", "T1 DRX 决赛 复盘"),
    ("格1", "EDG 决赛 复盘"), ("格1", "Wings TI6 夺冠"),
    # 格2 赛事×低 +4
    ("格2", "EDG 夺冠 采访"), ("格2", "Deft 采访 夺冠"),
    ("格2", "Faker 采访 语录"), ("格2", "Uzi 退役"),
    # 格3 科普×高 +4
    ("格3", "中国空间站 建造"), ("格3", "mRNA 疫苗 原理"),
    ("格3", "韦伯望远镜 成果"), ("格3", "CRISPR 基因编辑"),
    # 格4 科普×低 +5
    ("格4", "等效原理 电梯"), ("格4", "熵增 时间之箭"),
    ("格4", "混沌 蝴蝶效应"), ("格4", "囚徒困境 博弈论"), ("格4", "傅里叶变换 直觉"),
    # 格5 盘点×高 +4
    ("格5", "太阳系 行星之最"), ("格5", "元素周期表 之最"),
    ("格5", "诺贝尔物理奖 里程碑"), ("格5", "深潜 纪录 挑战者深渊"),
    # 格6 盘点×低 +5
    ("格6", "电影 十大反转结局"), ("格6", "游戏 十大名场面"),
    ("格6", "体操 满分时刻"), ("格6", "世界奇趣 纪录 盘点"), ("格6", "古代工程奇迹"),
]


def ytdlp(args, tries=3):
    for i in range(tries):
        cp = subprocess.run([sys.executable, "-X", "utf8", "-m", "yt_dlp", "--no-warnings", *args],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if cp.returncode == 0:
            return cp.stdout
        time.sleep(60)  # 风控/网络抖动统一冷却后再试（含空 stderr 情形）
    return None


def entry_vid(e: dict) -> str:
    """flat 条目取视频号：url basename（自带 av/BV 前缀）→ id 补 av 前缀。"""
    for key in ("webpage_url_basename", "url", "webpage_url"):
        v = e.get(key) or ""
        v = v.rstrip("/").rsplit("/", 1)[-1]
        if v.startswith("av") or v.startswith("BV"):
            return v
    vid = str(e.get("id") or "")
    return f"av{vid}" if vid.isdigit() else ""


def main():
    if not OUT.exists():
        OUT.write_text("cell\tkeyword\tbvid\tduration_s\ttitle\tuploader\tupload_date\n",
                       encoding="utf-8")
    lines = OUT.read_text(encoding="utf-8").splitlines()
    done = {line.split("\t")[1] for line in lines[1:]
            if line.split("\t")[2:3] and not line.split("\t")[2].startswith("FAIL")}
    for cell, kw in KEYWORDS:
        if kw in done:
            continue
        # 该词历史 FAIL 行留痕另存，主文件去重后重试（防重复累积）
        keep = [lines[0]] + [l for l in OUT.read_text(encoding="utf-8").splitlines()[1:]
                             if not (l.split("\t")[1] == kw and l.split("\t")[2].startswith("FAIL"))]
        OUT.write_text("\n".join(keep) + "\n", encoding="utf-8")
        print(f"== {cell} {kw}", flush=True)
        raw = ytdlp(["--dump-json", "--flat-playlist", f"bilisearch8:{kw}"])
        if not raw:
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(f"{cell}\t{kw}\tFAIL_SEARCH\t\t\t\t\n")
            print(f"   FAIL_SEARCH {kw}", flush=True)
            continue
        ids = []
        for line in raw.splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            vid = entry_vid(e)
            if vid and vid not in ids:
                ids.append(vid)
        ids = ids[:8]
        print(f"   hits={len(ids)}", flush=True)
        for vid in ids:
            j = ytdlp(["--skip-download", "-J", f"https://www.bilibili.com/video/{vid}"])
            if not j:
                with OUT.open("a", encoding="utf-8") as fh:
                    fh.write(f"{cell}\t{kw}\tFAIL_META:{vid}\t\t\t\t\n")
                continue
            try:
                d = json.loads(j)
            except Exception:
                continue
            row = "\t".join(str(x).replace("\t", " ") for x in (
                cell, kw, d.get("id", vid), round(d.get("duration") or 0),
                d.get("title", ""), d.get("uploader", ""), d.get("upload_date", "")))
            with OUT.open("a", encoding="utf-8") as fh:
                fh.write(row + "\n")
            print(f"   {row[:110]}", flush=True)
            time.sleep(1.5)
        time.sleep(3)
    print("SCREEN DONE", flush=True)


if __name__ == "__main__":
    main()
