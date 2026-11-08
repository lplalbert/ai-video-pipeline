# -*- coding: utf-8 -*-
"""B站自动投稿（biliupR 封装）——读 workdir/投稿包.json + 成片（文件名=B站主标题）→ biliupR 投稿。

安全阀：默认 dry-run 只打印将提交内容，不碰线上；
        --publish 才真提交；--private 首跑用（稿件仅自己可见）。

登录（一次性；交互式需真终端，由用户自己执行，QR 扫码）：
    <BILIUP> -u <COOKIES> login
凭据存 tools/biliup/cookies.json（含 SESSDATA——永不打印、永不进日志）。

用法:
    python -X utf8 bili_publish.py <workdir>                        # dry-run
    python -X utf8 bili_publish.py <workdir> --publish --private    # 首跑：仅自己可见
    python -X utf8 bili_publish.py <workdir> --publish              # 正式发布
可选: --title N（第 N 个标题候选，默认 1）--tid/--tid-v2 覆盖分区（默认从引用源视频解析）
      --dtime 时间戳 --mission-id 活动ID --submit app|web
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
BILIUP = ROOT / "tools" / "biliup" / "v1.3.4" / "biliupR-v1.3.4-x86_64-windows" / "biliup.exe"
COOKIES = ROOT / "tools" / "biliup" / "cookies.json"


def find_cookie_pairs(node, out):
    if isinstance(node, dict):
        if "name" in node and "value" in node and isinstance(node["name"], str):
            out.append((node["name"], node["value"]))
        for v in node.values():
            find_cookie_pairs(v, out)
    elif isinstance(node, list):
        for v in node:
            find_cookie_pairs(v, out)


def cookie_header() -> str:
    data = json.loads(COOKIES.read_text(encoding="utf-8"))
    pairs, seen = [], set()
    find_cookie_pairs(data, pairs)
    return "; ".join(f"{k}={v}" for k, v in pairs if k not in seen and not seen.add(k))


def resolve_tid_from_sources(workdir: Path):
    """从引用源视频（采集映射.json 的 BV）取真实分区——同类目，比猜分区表稳。
    member.bilibili.com 老接口已 404（2026-11-05），走公开 view API。"""
    mp = workdir / "采集映射.json"
    bvs = list((json.loads(mp.read_text(encoding="utf-8")).get("sources") or {}).keys()) if mp.exists() else []
    tids, tid2s = [], {}
    for bv in bvs:
        try:
            req = urllib.request.Request(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bv}",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read().decode("utf-8"))["data"]
            tids.append(d.get("tid"))
            if d.get("tid_v2"):
                tid2s[d["tid_v2"]] = d["tid_v2"]
            print(f"源 {bv}：tid={d.get('tid')} tid_v2={d.get('tid_v2')}")
        except Exception as e:
            print(f"!! 源 {bv} 分区查询失败：{e}")
    if tids:
        tid = max(set(tids), key=tids.count)
        tid_v2 = max(tid2s, key=list(tid2s).count) if tid2s else ""
        return str(tid), str(tid_v2)
    return "", ""


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        print(__doc__)
        return 1
    workdir = Path(args[0])
    opt = {"title": 1, "publish": False, "private": False, "tid": "", "tid_v2": "",
           "dtime": "", "mission": "", "submit": "app", "extra": ""}
    i = 1
    while i < len(args):
        a = args[i]
        if a == "--publish": opt["publish"] = True
        elif a == "--private": opt["private"] = True
        elif a == "--title": opt["title"] = int(args[i + 1]); i += 1
        elif a == "--tid": opt["tid"] = args[i + 1]; i += 1
        elif a == "--tid-v2": opt["tid_v2"] = args[i + 1]; i += 1
        elif a == "--dtime": opt["dtime"] = args[i + 1]; i += 1
        elif a == "--mission-id": opt["mission"] = args[i + 1]; i += 1
        elif a == "--submit": opt["submit"] = args[i + 1]; i += 1
        elif a == "--extra-fields": opt["extra"] = args[i + 1]; i += 1
        i += 1

    kit = json.loads((workdir / "投稿包.json").read_text(encoding="utf-8"))
    p = kit["platforms"]["bilibili"]
    title = p["titles"][opt["title"] - 1]
    desc = p["description"]
    tags = ",".join(kit.get("tags") or [])
    # 成片文件名=终稿 B站主标题（用户 2026-11-05 约定）：video_file → 成片.mp4 → 最新 mp4 兜底
    video = None
    vf = kit.get("video_file")
    if vf and (workdir / vf).is_file():
        video = workdir / vf
    elif (workdir / "成片.mp4").is_file():
        video = workdir / "成片.mp4"
    else:
        mp4s = sorted((p for p in workdir.glob("*.mp4") if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if mp4s:
            video = mp4s[0]
    if video is None:
        print(f"!! 找不到成片（查过 video_file/成片.mp4/*.mp4）：{workdir}")
        return 1

    tid, tid_v2 = resolve_tid_from_sources(workdir)
    if not tid:
        tid, tid_v2 = "235", "2141"
        print("!! 未从源视频解析到分区，用篮球默认 tid=235 tid_v2=2141（可用 --tid/--tid-v2 覆盖）")
    if opt["tid"]: tid = opt["tid"]
    if opt["tid_v2"]: tid_v2 = opt["tid_v2"]

    cmd = [str(BILIUP), "-u", str(COOKIES), "upload", str(video),
           "--submit", opt["submit"], "--copyright", "1", "--no-reprint", "1",
           "--title", title, "--desc", desc, "--tag", tags, "--tid", tid]
    if tid_v2:
        cmd += ["--tid-v2", tid_v2]
    if opt["private"]:
        cmd += ["--is-only-self", "1"]
    if opt["dtime"]:
        cmd += ["--dtime", opt["dtime"]]
    if opt["mission"]:
        cmd += ["--mission-id", opt["mission"]]
    if opt["extra"]:
        cmd += ["--extra-fields", opt["extra"]]

    print("=" * 60)
    print(f"视频：{video}  ({video.stat().st_size / 1e6:.1f} MB)")
    print(f"标题：{title}")
    print(f"标签：{tags}")
    print(f"分区：tid={tid or '-'} tid_v2={tid_v2 or '-'}  提交口：{opt['submit']}"
          f"  仅自己可见：{'是' if opt['private'] else '否'}")
    print(f"简介（{len(desc)} 字）：")
    print(desc)
    print("=" * 60)

    if not opt["publish"]:
        print("[dry-run] 未提交。确认无误后加 --publish；首跑建议加 --private（仅自己可见）。")
        return 0

    print("[提交] biliup upload …")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout[-3000:])
    if r.returncode != 0:
        print(r.stderr[-1500:])
        print("!! 提交失败")
        return 1
    print("完成。到创作中心核对稿件；--private 的稿件改「所有人可见」即发布。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
