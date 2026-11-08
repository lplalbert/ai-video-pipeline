"""AI视频量产流水线 CLI —— 选题库 → 文稿 → 配音 → 素材 → 成片 → 投稿包。

用法示例：
  python run_pipeline.py --ideate 10        # 先批量生成10个选题入SQLite选题库，取最高分制作
  python run_pipeline.py                    # 直接从选题库取最高分 queued 选题制作
  python run_pipeline.py --topic "古人夏天怎么降温"
  python run_pipeline.py --mode hot --ideate 5      # 热点模式出题
  python run_pipeline.py --skip-footage              # 纯色背景快速出样片
  python run_pipeline.py --no-polish                 # 不用 pro 精修，省 token

选题引擎单独使用（collect/ideate/angles/review/list）：
  python -m pipeline.topics ideate --mode evergreen --n 20
"""
import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from config import OUTPUT_DIR
from pipeline import assemble, footage, newswire, publish, sources, subtitles, topics, tts, writer


def slugify(text: str, n: int = 18) -> str:
    s = re.sub(r"[^\w一-鿿]+", "", text)[:n] or "video"
    return f"{date.today().isoformat()}_{s}"


def log(msg: str) -> None:
    print(f"[流水线] {msg}", flush=True)


def pick_topic_from_db() -> dict | None:
    """从选题库取最高分的 queued 选题，并标记为 in_production。"""
    conn = topics.db()
    row = conn.execute(
        "SELECT * FROM topics WHERE status='queued' ORDER BY score_total DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM topics WHERE status='candidate' ORDER BY score_total DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    t = dict(row)
    t["keywords"] = json.loads(t.get("keywords") or "[]")
    conn.execute("UPDATE topics SET status='in_production' WHERE id=?", (t["id"],))
    conn.commit()
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description="AI视频量产流水线")
    ap.add_argument("--topic", default="", help="直接指定选题（跳过选题库）")
    ap.add_argument("--ideate", type=int, default=0, metavar="N",
                    help="先生成 N 个选题入選题库再取最高分")
    ap.add_argument("--mode", default="evergreen", choices=["evergreen", "hot"],
                    help="选题生成模式：常青 / 热点")
    ap.add_argument("--format", default="科普解说", choices=list(writer.FORMATS),
                    help="文稿体裁")
    ap.add_argument("--minutes", type=float, default=4.0, help="目标时长（分钟）")
    ap.add_argument("--no-polish", action="store_true", help="跳过 pro 精修")
    ap.add_argument("--engine", default="mimo", choices=["mimo", "edge", "sapi"],
                    help="TTS 引擎（默认 mimo，限时免费；失败自动逐级回退）")
    ap.add_argument("--skip-footage", action="store_true", help="不找素材，用纯色背景")
    ap.add_argument("--skip-sources", action="store_true", help="跳过选段辅助（候选视频+时间窗）")
    ap.add_argument("--workdir", default="", help="指定工作目录（配合 --resume 复用已有产出）")
    ap.add_argument("--resume", action="store_true",
                    help="复用工作目录里已有文稿+配音，只重跑 素材→成片→投稿包"
                         "（人工把录屏放进 采集/ 后用）")
    ap.add_argument("--bgm", default="", help="背景音乐文件路径（可选）")
    ap.add_argument("--no-assemble", action="store_true", help="只出文稿/配音/投稿包，不合成视频")
    args = ap.parse_args()

    topic_info: dict
    if args.resume and args.workdir:
        workdir = Path(args.workdir)
        if not (workdir / "文稿.json").exists():
            ap.error(f"--resume 需要 {workdir} 里已有 文稿.json")
        script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
        topic = script.get("topic_title") or workdir.name.split("_", 1)[-1]
        topic_info = {"topic_title": topic, "keywords": []}
        log(f"复用产出：{workdir}")
    else:
        if args.topic:
            topic_info = {"topic_title": args.topic}
            log(f"指定选题：{args.topic}")
        else:
            if args.ideate:
                log(f"生成选题（mode={args.mode}, n={args.ideate}）→ 选题库…")
                topics.run_pipeline(args.mode, args.ideate)
            topic_info = pick_topic_from_db()
            if not topic_info:
                ap.error("选题库为空，请先 `python run_pipeline.py --ideate 10` 或指定 --topic")
            log(f"从选题库取题：[{topic_info.get('score_total')}] {topic_info['topic_title']}"
                f"（id={topic_info['id']}）")

        topic = topic_info["topic_title"]
        workdir = Path(args.workdir) if args.workdir else OUTPUT_DIR / slugify(topic)
        workdir.mkdir(parents=True, exist_ok=True)
        log(f"工作目录：{workdir}")

        # 1. 文稿
        log(f"生成文稿（{args.format}，目标 {args.minutes} 分钟）…")
        script = writer.generate_script(topic, fmt=args.format, minutes=args.minutes,
                                        polish=not args.no_polish, topic_info=topic_info)
        (workdir / "文稿.json").write_text(json.dumps(script, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        sents = writer.flatten_sentences(script)
        log(f"文稿完成：{writer.word_count(script)} 字 / {len(sents)} 句")
        for note in script.get("fact_check_notes", []):
            log(f"  [待核实] {note}")

        # 1.5 采集工单（时事素材：人工按单取材丢进 workdir/采集/，流水线优先使用）
        plan_md = newswire.build_collection_plan(script, workdir, topic=topic)
        log(f"采集工单：{plan_md.name}（引用素材按单取回放入 采集/ 子目录）")

        # 1.6 选段辅助：批量发现候选视频 + 字幕定位引用时间窗，人工拍板后录屏取材
        if not args.skip_sources:
            try:
                plan = json.loads((workdir / "采集计划.json").read_text(encoding="utf-8"))
                pick_md = sources.discover_for_plan(plan, workdir)
                log(f"选段工单：{pick_md.name}（候选视频+时间窗已定位，拍板后录屏取材）")
            except Exception as exc:
                log(f"选段辅助跳过（{exc}）")

    # 2. 配音 + 字幕（--resume 时复用；文稿改过则自动重配）
    audio_ready = (workdir / "audio/segments.json").exists()
    if audio_ready and (workdir / "文稿.json").stat().st_mtime > (
            workdir / "audio/segments.json").stat().st_mtime:
        log("检测到文稿已更新，重新配音…")
        audio_ready = False
    if audio_ready and not tts.cache_fresh(workdir / "audio", tts.get_engine(args.engine)):
        log("检测到音色/风格/语速配置已更新，重新配音…")
        audio_ready = False
    if args.resume and audio_ready:
        segs = json.loads((workdir / "audio/segments.json").read_text(encoding="utf-8"))
        srt = workdir / "字幕.srt"
        log(f"复用配音：audio/narration.wav，总长 {segs[-1]['end']:.1f}s")
    else:
        log(f"合成配音（engine={args.engine}）…")
        sents = writer.flatten_sentences(script)
        narration = tts.synthesize_narration(sents, workdir / "audio",
                                             engine=tts.get_engine(args.engine))
        segs = narration["segments"]
        srt = subtitles.write_srt(segs, workdir / "字幕.srt")
        log(f"配音完成：{narration['audio'].name}，总长 {segs[-1]['end']:.1f}s")

    # 3. 素材
    if args.skip_footage:
        manifest = [{"index": i, "text": s["text"], "query": "", "clip": "color",
                     "source": "color-fallback", "license": "纯色背景（自动生成）"}
                    for i, s in enumerate(segs)]
        (workdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
        log("跳过素材检索（纯色背景模式）")
    else:
        log("检索素材（Pexels/本地素材库）…")
        manifest = footage.build_manifest(segs, workdir,
                                          keyword_pool=topic_info.get("keywords"))
        real = sum(1 for m in manifest if m.get("source", "").startswith(("pexels", "local")))
        cards = sum(1 for m in manifest if m.get("source") == "self-card")
        log(f"素材：实拍 {real} 句 / 自制字卡 {cards} 句 / 纯色兜底 {len(manifest) - real - cards} 句")

    # 3.5 引用统计（不设阈值——适当引用是著作权法 24 条的实质判断，不是时长配额）
    total = segs[-1]["end"] or 1.0
    quoted = 0.0
    for m in manifest:
        if str(m.get("source", "")).startswith("collect:"):
            seg = segs[m["index"]]
            quoted += max(0.0, seg["end"] - seg["start"])
    ratio = quoted / total
    log(f"引用统计：{quoted:.1f}s / {total:.1f}s = {ratio:.0%}（仅统计，不设配额）")
    (workdir / "引用预算.txt").write_text(
        f"引用累计 {quoted:.1f}s / 成片 {total:.1f}s = {ratio:.0%}\n"
        f"规范（著作权法 24 条适当引用）：为评论/介绍而引用、引用适当部分、逐条署名"
        f"（作者姓名/名称+作品名称）、不影响原作正常使用；法律不设秒数/比例上限\n",
        encoding="utf-8")

    # 4. 成片
    if not args.no_assemble:
        log("FFmpeg 合成成片…")
        final = assemble.assemble_video(segs, manifest, workdir / "audio/narration.wav", workdir,
                                        srt=srt, bgm=Path(args.bgm) if args.bgm else None,
                                        title=(script.get("titles") or [topic])[0])
        log(f"成片：{final}")

    # 5. 投稿包
    publish.build_publish_kit(script, workdir, topic=topic)
    log("投稿包：投稿包.md / 发布自查清单.md")
    log("完成。发布前请逐项过一遍自查清单。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
