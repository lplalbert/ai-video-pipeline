"""yt-dlp CC/自有内容取材器 —— 单条下载 + 强制来源审查 + 授权留痕。

这是全项目红线「抓平台视频流(yt-dlp 类绕防盗链)仍是我方红线」的**唯一受控例外**：
仅当来源**明确可商用再利用**时才可下载——
  ① 你自己的视频；② 明确标注 CC 授权（CC0/CC-BY 等允许商用的）；③ 公有领域。
判断标准是「授权允许再利用」，**不是「能搜到」**。

硬约束（改代码也别绕过）：
- **单条**下载：一次一个 URL，不做关键词批量扒库；
- **强制来源审查**：下载前必须交互确认来源/授权，否则拒绝执行；
- **授权留痕**：每条写进 素材库/cc_来源台账.json，含 URL、授权类型、审查时间、署名要求；
- **不自动进产线**：默认落到 素材库/_cc_reviewed/（隔离区），须人工把复核过的片段
  手动挪进 采集/ 或 素材库/ 才会被 footage 用到——录制/取材的拍板权留给人。

绝不做：给关键词批量下载 YouTube/B站随手搜到的视频 = 洗稿搬运 = 红线。
      YouTube/B站 的一般视频绝大多数是他人版权，未经授权下载再拼进成片即侵权。

用法：
  python collect_cc.py <视频URL> --license cc0 --reason "作者明确标 CC0"
  python collect_cc.py <视频URL> --license own --reason "我自己频道的视频"
  python collect_cc.py --list          # 查看已审查留痕
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from config import LIB_DIR  # noqa: E402

LEDGER = LIB_DIR / "cc_来源台账.json"
REVIEW_DIR = LIB_DIR / "_cc_reviewed"
OUT_DIR = LIB_DIR / "cc_downloads"

# 允许下载的授权类型（只有这些过审；unknown/other 一律拒绝）
ALLOWED_LICENSES = {
    "own": "自有内容（作者本人上传/持有版权）",
    "cc0": "CC0 公有领域贡献（可商用、无需署名）",
    "cc-by": "CC-BY（可商用，须署名）",
    "pd": "公有领域（Public Domain，可商用）",
}

# 红线清单：这些描述一旦出现，直接拒绝（大概率是搬运/侵权）
BLOCK_HINTS = ("搬运", "切片", "录播", "搬运号", "盗", "去水印", "防搬运", "二次上传")


def _load_ledger() -> list:
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_ledger(entries: list) -> None:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _already(url: str) -> dict | None:
    for e in _load_ledger():
        if e.get("url") == url:
            return e
    return None


def yt_dlp_meta(url: str) -> dict:
    """只取元数据（标题/作者/时长/描述），不下载。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist",
             "--no-warnings", "--socket-timeout", "20", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
        )
        if r.returncode != 0:
            return {"error": (r.stderr or "yt-dlp 取元数据失败").strip()[:300]}
        return json.loads(r.stdout)
    except Exception as exc:
        return {"error": str(exc)[:300]}


def review(meta: dict, reason: str) -> dict:
    """交互式来源审查。返回 {"pass": bool, "note": str}。"""
    print("\n" + "=" * 60)
    print("来源审查（下载前必过；不过即拒绝）")
    print("=" * 60)
    print(f"  标题：{meta.get('title', '（未知）')}")
    print(f"  作者：{meta.get('uploader', '（未知）')}")
    print(f"  平台：{meta.get('extractor', '（未知）')}")
    dur = meta.get("duration")
    print(f"  时长：{int(dur) // 60}:{int(dur) % 60:02d}" if dur else "  时长：（未知）")
    print(f"  你填的理由：{reason}")
    print("-" * 60)
    print("授权类型只能是：")
    for k, v in ALLOWED_LICENSES.items():
        print(f"  {k:8s} = {v}")
    print("-" * 60)

    # 红线自检：描述/标题里出现搬运类字眼 → 直接拒
    blob = f"{meta.get('title', '')}{meta.get('description', '')}".lower()
    for bad in BLOCK_HINTS:
        if bad.lower() in blob:
            return {"pass": False,
                    "note": f"命中红线词「{bad}」——疑似搬运/去水印/防搬运素材，拒绝下载。"}

    print("请逐条确认（这关系到版权责任，务必如实）：")
    ans = input("  ① 该视频授权确实允许商用再利用吗？(yes/no): ").strip().lower()
    if ans not in ("yes", "y"):
        return {"pass": False, "note": "未确认可商用——按红线拒绝。"}
    ans = input("  ② 你填的授权类型与实际相符吗？(yes/no): ").strip().lower()
    if ans not in ("yes", "y"):
        return {"pass": False, "note": "授权类型存疑——按红线拒绝。"}
    ans = input("  ③ 用途是「原创贡献」而非「搬运+轻解说」吗？(yes/no): ").strip().lower()
    if ans not in ("yes", "y"):
        return {"pass": False, "note": "用途涉嫌搬运——按红线拒绝。"}
    return {"pass": True, "note": "来源审查通过"}


def download(url: str, license_type: str, reason: str, force: bool = False) -> int:
    if license_type not in ALLOWED_LICENSES:
        print(f"拒绝：授权类型「{license_type}」不在白名单。\n"
              f"      只允许：{', '.join(ALLOWED_LICENSES)}", file=sys.stderr)
        return 2

    prior = _already(url)
    if prior:
        print(f"[跳过] 该 URL 已审查留痕（{prior['reviewed_at']}，{prior['license_type']}）。")
        print(f"       文件：{prior['file']}")
        return 0

    meta = yt_dlp_meta(url)
    if meta.get("error"):
        print(f"拒绝：取元数据失败——{meta['error']}", file=sys.stderr)
        return 3

    if force:
        print("[--force] 跳过交互审查（仅限你自己刚下过、URL 未变的场景）。")
        verdict = {"pass": True, "note": "force 跳过交互"}
    else:
        verdict = review(meta, reason)
    if not verdict["pass"]:
        print(f"\n✗ 未通过审查：{verdict['note']}", file=sys.stderr)
        return 4

    # 单条下载（绝不批量）
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(REVIEW_DIR / "%(id)s.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings",
           "--socket-timeout", "20", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
           "-o", out_tmpl, url]
    print(f"\n[下载] 单条 → {REVIEW_DIR}/（隔离区，不自动进产线）")
    r = subprocess.run(cmd, encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0:
        print("✗ 下载失败。", file=sys.stderr)
        return 5

    vid = meta.get("id", "")
    files = sorted(REVIEW_DIR.glob(f"{vid}.*"))
    fname = files[0].name if files else f"{vid}.mp4"

    entry = {
        "url": url, "id": vid, "title": meta.get("title", ""),
        "uploader": meta.get("uploader", ""), "platform": meta.get("extractor", ""),
        "license_type": license_type, "license_desc": ALLOWED_LICENSES[license_type],
        "reason": reason, "note": verdict["note"],
        "attribution_required": license_type in ("cc-by",),
        "file": fname, "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "status": "待人工复核（挪入 采集/ 或 素材库/ 才会进产线）",
    }
    led = _load_ledger()
    led.append(entry)
    _save_ledger(led)

    print("\n✓ 已下载并留痕。")
    print(f"  文件：{REVIEW_DIR / fname}")
    print(f"  台账：{LEDGER}")
    if entry["attribution_required"]:
        print("  ⚠ CC-BY：成片必须署名原作者（投稿包来源声明里写明）。")
    print("\n下一步（人工）：复核片段确属可商用后，把它挪进 采集/ 或 素材库/，")
    print("            再跑 run_pipeline.py 采用。取材拍板权在你手上。")
    return 0


def list_ledger() -> int:
    led = _load_ledger()
    if not led:
        print("台账为空。")
        return 0
    print(f"共 {len(led)} 条来源留痕：\n")
    for e in led:
        mark = "⚠须署名" if e.get("attribution_required") else "  可商用"
        print(f"[{mark}] {e['license_type']:7s} | {e['title'][:34]}")
        print(f"           {e['platform']} @{e.get('uploader', '?')}  → {e['file']}")
        print(f"           {e['url']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="yt-dlp CC/自有内容取材器（单条+来源审查+留痕，红线受控例外）")
    ap.add_argument("url", nargs="?", default="", help="视频 URL（一次一个）")
    ap.add_argument("--license", default="", dest="license_type",
                    choices=sorted(ALLOWED_LICENSES), help="授权类型（白名单）")
    ap.add_argument("--reason", default="", help="为何判定可商用（写入留痕）")
    ap.add_argument("--force", action="store_true",
                    help="跳过交互审查（慎用，仅你自己确认过的场景）")
    ap.add_argument("--list", action="store_true", help="查看已审查台账")
    args = ap.parse_args()

    if args.list:
        return list_ledger()
    if not args.url:
        ap.error("请给一个视频 URL（或用 --list 查看台账）")
    if not args.license_type:
        ap.error(f"请用 --license 指定授权类型：{', '.join(ALLOWED_LICENSES)}")
    if not args.reason:
        ap.error("请用 --reason 说明为何判定可商用（会写入留痕台账）")
    return download(args.url, args.license_type, args.reason, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
