"""Reproducible automatic checks and blinded review sheets for video runs.

Automatic checks are diagnostic only. Human Q1/Q2/Q4/Q5 and platform Q6
cannot be inferred from filenames or keyword overlap.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import secrets
import shutil
import subprocess
from pathlib import Path

from autoline import title_lint
from pipeline.matcher import FFPROBE
from pipeline.writer import flatten_sentences


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def score(workdir: Path) -> dict:
    script = read_json(workdir / "文稿.json")
    segments = read_json(workdir / "audio" / "segments.json")
    manifest = read_json(workdir / "manifest.json")
    kit = read_json(workdir / "投稿包.json")
    mapping = read_json(workdir / "采集映射.json")
    missing = [name for name, obj in (("文稿.json", script), ("audio/segments.json", segments),
                                      ("manifest.json", manifest), ("投稿包.json", kit)) if obj is None]
    if missing:
        return {"workdir": str(workdir), "status": "incomplete", "missing": missing,
                "q1": None, "q2": None, "q3": None, "q4": None, "q5": None,
                "q6": None}
    sentence_count = len(flatten_sentences(script))
    segment_indices = [s.get("index") for s in segments]
    manifest_indices = [m.get("index") for m in manifest]
    structural = {
        "sentence_count": sentence_count,
        "segment_count": len(segments),
        "manifest_count": len(manifest),
        "indices_match": segment_indices == manifest_indices == list(range(sentence_count)),
        "valid_segment_times": all(isinstance(s.get("start"), (int, float)) and
                                   isinstance(s.get("end"), (int, float)) and
                                   s["end"] > s["start"] for s in segments),
    }
    video = workdir / str(kit.get("video_file") or "")
    media = {"exists": video.is_file(), "probe_ok": False,
             "pixel_format": None, "audio_channels": None, "duration_seconds": None}
    if video.is_file() and segments:
        probe = subprocess.run([FFPROBE, "-v", "error", "-show_streams", "-show_format",
                                "-of", "json", str(video)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        if probe.returncode == 0:
            info = json.loads(probe.stdout)
            streams = info.get("streams", [])
            picture = next((s for s in streams if s.get("codec_type") == "video"), {})
            audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
            media.update({"probe_ok": True, "pixel_format": picture.get("pix_fmt"),
                          "audio_channels": audio.get("channels"),
                          "duration_seconds": float(info.get("format", {}).get("duration") or 0)})
    structural["media"] = media
    media_passed = (bool(segments) and media["probe_ok"] and media["pixel_format"] == "yuv420p"
                    and media["audio_channels"] == 2 and
                    abs(media["duration_seconds"] - segments[-1]["end"]) <= 3.0)
    structural["passed"] = (sentence_count == len(segments) == len(manifest)
                            and structural["indices_match"] and structural["valid_segment_times"]
                            and media_passed)
    title_issues = title_lint(kit)
    descriptions = [p.get("description", "") for p in kit.get("platforms", {}).values()]
    source_map = (mapping or {}).get("sources", {})
    chosen_sources = {c.get("src") for c in (mapping or {}).get("clips", []) if c.get("src")}
    source_gaps = sorted(src for src in chosen_sources if not source_map.get(src, {}).get("url"))
    compliance = {
        "aigc_text_in_all_descriptions": bool(descriptions) and all("AIGC" in d for d in descriptions),
        "source_url_gaps": source_gaps,
        "platform_aigc_checkbox": None,
        "human_review_required": True,
    }
    # ── Q2 分母钉死（2026-11-07 协议修正）────────────────────────────
    # headline Q2 = (N_匹配 + 0.5·N_弱) / N_全部口播句——字卡/辅助素材/缺行句计 0，
    # 防止"少切片=刷高条件匹配率"。Q2a 条件匹配率（仅 collect 句）只作诊断。
    # 人工只需评 collect 片段（字卡行自动计 0），评审工时不变。
    visual_by_index = {m.get("index"): m for m in manifest}
    counts = {"collect": 0, "self_card": 0, "other_footage": 0, "missing_row": 0}
    for i in range(sentence_count):
        src = str((visual_by_index.get(i) or {}).get("source", ""))
        if src.startswith("collect:"):
            counts["collect"] += 1
        elif src == "self-card":
            counts["self_card"] += 1
        elif src:
            counts["other_footage"] += 1
        else:
            counts["missing_row"] += 1
    quoted_seconds = 0.0
    for m in manifest:
        if str(m.get("source", "")).startswith("collect:"):
            seg = next((s for s in segments if s.get("index") == m.get("index")), None)
            if seg:
                quoted_seconds += max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
    total_seconds = float(segments[-1].get("end") or 0) if segments else 0.0
    q2 = {"status": "human_review_required", **counts,
          "sentence_total": sentence_count,
          "sentence_coverage": round(counts["collect"] / max(1, sentence_count), 4),
          "quoted_seconds": round(quoted_seconds, 3),
          "quote_seconds_ratio": round(quoted_seconds / total_seconds, 4) if total_seconds else None,
          "formula": "Q2=(N_匹配+0.5*N_弱)/全部口播句；字卡/辅助素材句计0；Q2a=条件匹配率(仅collect句)作诊断"}
    return {"workdir": str(workdir), "status": "ready_for_review",
            "q1": {"status": "human_review_required", "sentences": sentence_count},
            "q2": q2,
            "q3": structural,
            "q4": {"title_lint_issues": title_issues, "human_preference_required": True},
            "q5": {"status": "human_review_required"}, "q6": compliance}


def prepare(workdirs: list[Path], outdir: Path, reviewers: int = 3) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    mapping = []
    sheets = {"facts": [], "clips": [], "video": [], "title_pairs": []}
    titles_by_topic: dict[str, list[tuple[str, dict]]] = {}
    def anonymous_media(source: Path, target: Path) -> str:
        if not source.is_file():
            raise FileNotFoundError(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(target)
        try:
            target.hardlink_to(source)
        except OSError:
            shutil.copy2(source, target)
        return str(target.relative_to(outdir))

    shuffled = list(workdirs)
    secrets.SystemRandom().shuffle(shuffled)
    for workdir in shuffled:
        result = score(workdir)
        if result["status"] != "ready_for_review":
            raise ValueError(f"incomplete run: {workdir}: {result['missing']}")
        review_id = secrets.token_hex(6)
        script = read_json(workdir / "文稿.json")
        seed = read_json(workdir / "选题.json") or {}
        manifest = read_json(workdir / "manifest.json")
        kit = read_json(workdir / "投稿包.json")
        topic_key = (read_json(workdir / "选题.json") or {}).get("topic_title", "")
        titles_by_topic.setdefault(topic_key, []).append((review_id, kit["platforms"]))
        video_file = anonymous_media(workdir / kit["video_file"],
                                     outdir / "media" / review_id / "video.mp4")
        mapping.append({"review_id": review_id, "workdir": str(workdir),
                        "video_file": kit.get("video_file"), "auto_score": result})
        for i, sentence in enumerate(flatten_sentences(script)):
            sheets["facts"].append({"review_id": review_id, "sentence_index": i,
                                    "sentence": sentence, "atomic_claim": "",
                                    "fact_basis": seed.get("fact_basis", ""),
                                    "rating_support_conflict": "", "evidence": "",
                                    "reviewer_id": ""})
        for item in manifest:
            if str(item.get("source", "")).startswith("collect:"):
                clip_file = anonymous_media(Path(item["clip"]),
                                            outdir / "media" / review_id /
                                            f"clip_{item['index']:03d}.mp4")
                sheets["clips"].append({"review_id": review_id, "sentence_index": item["index"],
                                        "sentence": item.get("text", ""),
                                        "clip_file": clip_file,
                                        "rating_match_weak_wrong": "", "notes": "",
                                        "reviewer_id": ""})
        bilibili_titles = kit.get("platforms", {}).get("bilibili", {}).get("titles", [])
        for reviewer_slot in range(1, reviewers + 1):
            sheets["video"].append({"review_id": review_id,
                                    "reviewer_slot": reviewer_slot,
                                    "video_file": video_file,
                                    "bilibili_title": bilibili_titles[0] if bilibili_titles else "",
                                    "q5_accuracy_1to5": "", "q5_density_1to5": "",
                                    "q5_flow_1to5": "", "q5_visual_1to5": "",
                                    "q5_title_1to5": "", "reviewer_id": ""})
    for topic_runs in titles_by_topic.values():
        for (id_a, platforms_a), (id_b, platforms_b) in itertools.combinations(topic_runs, 2):
            left_a = bool(secrets.randbits(1))
            left_id, right_id = (id_a, id_b) if left_a else (id_b, id_a)
            left, right = (platforms_a, platforms_b) if left_a else (platforms_b, platforms_a)
            for platform in ("bilibili", "xigua_douyin", "shipinhao"):
                left_title = (left.get(platform, {}).get("titles") or [""])[0]
                right_title = (right.get(platform, {}).get("titles") or [""])[0]
                for reviewer_slot in range(1, reviewers + 1):
                    sheets["title_pairs"].append({
                        "left_review_id": left_id, "right_review_id": right_id,
                        "platform": platform, "reviewer_slot": reviewer_slot,
                        "left_title": left_title, "right_title": right_title,
                        "preference_left_right_tie": "", "reason": "", "reviewer_id": ""})
    (outdir / "coordinator_mapping.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, rows in sheets.items():
        with (outdir / f"review_{name}.csv").open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=rows[0].keys() if rows else [])
            writer.writeheader()
            writer.writerows(rows)


# ============================================================
# 评分汇总（人工 CSV → 按 arm 指标 + 一致性 + 缺失值处理）
# 缺失评分一律留缺失（不以 0 代替）；分母随每个指标一起报告。
# ============================================================

Q5_DIMS = ("q5_accuracy_1to5", "q5_density_1to5", "q5_flow_1to5",
           "q5_visual_1to5", "q5_title_1to5")


def cohen_kappa(pairs: list) -> dict:
    """名义尺度两评者一致性。pairs=[(r1, r2)]，空评缺失不计。"""
    pairs = [(a, b) for a, b in pairs if a and b]
    n = len(pairs)
    if n == 0:
        return {"n_pairs": 0, "percent_agreement": None, "kappa": None}
    cats = sorted({c for p in pairs for c in p})
    idx = {c: i for i, c in enumerate(cats)}
    mat = [[0] * len(cats) for _ in cats]
    for a, b in pairs:
        mat[idx[a]][idx[b]] += 1
    po = sum(mat[i][i] for i in range(len(cats))) / n
    pe = sum((sum(mat[i]) / n) * (sum(row[i] for row in mat) / n) for i in range(len(cats)))
    kappa = (po - pe) / (1 - pe) if pe < 1 else None
    return {"n_pairs": n, "percent_agreement": round(po, 4),
            "kappa": round(kappa, 4) if kappa is not None else None}


def _read_csv(path: Path) -> list:
    if not path.exists():
        return []
    import csv as _csv
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(_csv.DictReader(fh))


def _arm_of(workdir_str: str) -> str:
    parts = Path(workdir_str.replace("\\", "/")).parts
    for a in ("A1", "A2", "A3", "A4"):
        if a in parts:
            return a
    return "unknown"


def _mean(vals: list):
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate(review_dir: Path) -> dict:
    mapping = json.loads((review_dir / "coordinator_mapping.json").read_text(encoding="utf-8"))
    info = {m["review_id"]: m for m in mapping}
    per_arm: dict = {}
    def arm_slot(rid: str) -> dict:
        arm = _arm_of((info.get(rid) or {}).get("workdir", ""))
        return per_arm.setdefault(arm, {
            "videos": set(), "q1_support_rates": [], "q1_n_claims": 0,
            "q1_support": 0, "q1_unsupported": 0, "q1_conflict": 0, "q1_nonclaim": 0,
            "q2_headline": [], "q2a_conditional": [], "q2_n_rated": 0,
            "q2_match": 0, "q2_weak": 0, "q2_wrong": 0,
            "q5_dims": {d: [] for d in Q5_DIMS}, "q5_n": 0, "q5_missing": 0,
        })
    missing = {"facts": 0, "clips": 0, "video": 0, "title_pairs": 0}

    # ---- Q1 事实（支持/无支撑/矛盾；非判词从分母排除）----
    facts = _read_csv(review_dir / "review_facts.csv")
    claims_by_video: dict = {}
    for row in facts:
        rid = row.get("review_id", "")
        rating = (row.get("rating_support_conflict") or "").strip()
        if not rating:
            missing["facts"] += 1
            continue
        if rating == "非判词":
            arm_slot(rid)["q1_nonclaim"] += 1
            continue
        key = (rid, row.get("sentence_index", ""), row.get("atomic_claim", ""))
        claims_by_video.setdefault(rid, []).append((key, rating, row.get("reviewer_id", "")))
        slot = arm_slot(rid)
        slot["videos"].add(rid)
        slot["q1_n_claims"] += 1
        if rating == "支持":
            slot["q1_support"] += 1
        elif rating == "无支撑":
            slot["q1_unsupported"] += 1
        elif rating == "矛盾":
            slot["q1_conflict"] += 1
    for rid, items in claims_by_video.items():
        rated = [r for _, r, _ in items if r in ("支持", "无支撑", "矛盾")]
        if rated:
            arm_slot(rid)["q1_support_rates"].append(
                round(sum(1 for r in rated if r == "支持") / len(rated), 4))

    # ---- Q2 素材对位（headline 分母=全部口播句，字卡句计 0）----
    clips = _read_csv(review_dir / "review_clips.csv")
    clip_votes: dict = {}
    for row in clips:
        rid = row.get("review_id", "")
        rating = (row.get("rating_match_weak_wrong") or "").strip()
        if not rating:
            missing["clips"] += 1
            continue
        key = (rid, row.get("sentence_index", ""))
        clip_votes.setdefault(key, []).append((rating, row.get("reviewer_id", "")))
    for (rid, _si), votes in clip_votes.items():
        rating = votes[0][0]  # 多评者分歧以第一票计 headline，分歧看 agreement；定稿由协调员解决
        slot = arm_slot(rid)
        slot["videos"].add(rid)
        slot["q2_n_rated"] += 1
        if rating == "匹配":
            slot["q2_match"] += 1
        elif rating == "弱匹配":
            slot["q2_weak"] += 1
        elif rating == "错位":
            slot["q2_wrong"] += 1
    for rid in {r for r, _ in clip_votes}:
        slot = arm_slot(rid)
        match_n = sum(1 for (r2, _si), v in clip_votes.items() if r2 == rid and v[0][0] == "匹配")
        weak_n = sum(1 for (r2, _si), v in clip_votes.items() if r2 == rid and v[0][0] == "弱匹配")
        total = ((info.get(rid) or {}).get("auto_score") or {}).get("q2", {}).get("sentence_total") or 0
        if total:
            slot["q2_headline"].append(round((match_n + 0.5 * weak_n) / total, 4))
        rated_n = sum(1 for (r2, _si) in clip_votes if r2 == rid)
        if rated_n:
            slot["q2a_conditional"].append(round((match_n + 0.5 * weak_n) / rated_n, 4))

    # ---- Q5 完整视频（1-5 分；缺失留空不补 0）----
    videos = _read_csv(review_dir / "review_video.csv")
    q5_by_key: dict = {}
    for row in videos:
        rid = row.get("review_id", "")
        slot = arm_slot(rid)
        slot["videos"].add(rid)
        any_rating = False
        for dim in Q5_DIMS:
            raw = (row.get(dim) or "").strip()
            if not raw:
                continue
            try:
                val = int(raw)
            except ValueError:
                continue
            if not 1 <= val <= 5:
                continue
            any_rating = True
            slot["q5_dims"][dim].append(val)
            q5_by_key.setdefault((rid, dim), []).append((val, row.get("reviewer_id", "")))
        if any_rating:
            slot["q5_n"] += 1
        else:
            slot["q5_missing"] += 1
            missing["video"] += 1

    # ---- Q4 标题成对偏好（left/right/tie → 胜率）----
    pairs_rows = _read_csv(review_dir / "review_title_pairs.csv")
    q4 = {"by_platform": {}, "missing": 0}
    for row in pairs_rows:
        pref = (row.get("preference_left_right_tie") or "").strip()
        plat = row.get("platform", "")
        if not pref:
            q4["missing"] += 1
            continue
        left_arm = _arm_of((info.get(row.get("left_review_id", ""), {}) or {}).get("workdir", ""))
        right_arm = _arm_of((info.get(row.get("right_review_id", ""), {}) or {}).get("workdir", ""))
        cell = q4["by_platform"].setdefault(plat, {"left_win": 0, "right_win": 0, "tie": 0,
                                                   "pairs": 0, "by_arm_pair": {}})
        cell["pairs"] += 1
        key = f"{left_arm}_vs_{right_arm}"
        ap = cell["by_arm_pair"].setdefault(key, {"left_win": 0, "right_win": 0, "tie": 0})
        if pref == "left":
            cell["left_win"] += 1
            ap["left_win"] += 1
        elif pref == "right":
            cell["right_win"] += 1
            ap["right_win"] += 1
        else:
            cell["tie"] += 1
            ap["tie"] += 1

    # ---- 一致性（只在同项有 ≥2 评者的子集上算）----
    missing["title_pairs"] = q4["missing"]
    agreement = {
        "q1_claims": cohen_kappa([
            (votes[0][0], votes[1][0]) for votes in _group_votes(
                facts, "rating_support_conflict",
                lambda row: (row.get("review_id", ""), row.get("sentence_index", ""),
                             row.get("atomic_claim", "")))
            if len(votes) >= 2]),
        "q2_clips": cohen_kappa([(v[0][0], v[1][0]) for v in clip_votes.values() if len(v) >= 2]),
    }
    q5_pairs = []
    for vals in q5_by_key.values():
        if len(vals) >= 2:
            q5_pairs.append((str(vals[0][0]), str(vals[1][0])))
    agreement["q5_nominal"] = cohen_kappa(q5_pairs)

    summary = {}
    for arm, slot in sorted(per_arm.items()):
        summary[arm] = {
            "videos": len(slot["videos"]),
            "q1_support_rate_mean": _mean(slot["q1_support_rates"]),
            "q1_claims": slot["q1_n_claims"],
            "q1_support_unsupported_conflict": [slot["q1_support"], slot["q1_unsupported"],
                                                slot["q1_conflict"]],
            "q1_nonclaim": slot["q1_nonclaim"],
            "q2_headline_mean": _mean(slot["q2_headline"]),
            "q2a_conditional_mean": _mean(slot["q2a_conditional"]),
            "q2_rated_clips": slot["q2_n_rated"],
            "q2_match_weak_wrong": [slot["q2_match"], slot["q2_weak"], slot["q2_wrong"]],
            "q5_mean_by_dim": {d: _mean(v) for d, v in slot["q5_dims"].items()},
            "q5_rated_rows": slot["q5_n"], "q5_missing_rows": slot["q5_missing"],
        }
    return {"arms": summary, "q4_title_pairs": q4,
            "agreement": agreement, "missing_rows": missing,
            "notes": [
                "headline Q2=(N_匹配+0.5*N_弱)/sentence_total（字卡句计0）；Q2a 仅诊断",
                "缺失评分未以 0 代替；每指标分母=相应计数字段",
                "q1/q2 多评者分歧子集上的 kappa 仅两人可算（第三人用 percent_agreement 目检）",
                "Q5 kappa 按名义近似（1-5 当类别），序数 alpha 留给统计阶段",
            ]}


def _group_votes(rows: list, rating_field: str, key_fn) -> list:
    """按 key 归并多评者评分 → [ [rating, reviewer_id], ... ] 列表（每 key 一个）。"""
    grouped: dict = {}
    for row in rows:
        rating = (row.get(rating_field) or "").strip()
        if not rating or rating == "非判词":
            continue
        grouped.setdefault(key_fn(row), []).append([rating, (row.get("reviewer_id") or "").strip()])
    return list(grouped.values())


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_score = sub.add_parser("score")
    p_score.add_argument("workdir", type=Path)
    p_prepare = sub.add_parser("prepare")
    p_prepare.add_argument("workdirs", nargs="+", type=Path)
    p_prepare.add_argument("--outdir", type=Path, required=True)
    p_prepare.add_argument("--reviewers", type=int, default=3)
    p_agg = sub.add_parser("aggregate", help="汇总已填写评审 CSV：按 arm 指标 + 一致性 + 缺失")
    p_agg.add_argument("review_dir", type=Path)
    p_agg.add_argument("--out", type=Path, help="另存 JSON")
    args = parser.parse_args()
    if args.cmd == "score":
        print(json.dumps(score(args.workdir), ensure_ascii=False, indent=2))
    elif args.cmd == "aggregate":
        result = aggregate(args.review_dir)
        if args.out:
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if args.reviewers < 1:
            parser.error("--reviewers must be positive")
        prepare(args.workdirs, args.outdir, args.reviewers)
        print(f"Review sheets written to {args.outdir}")


if __name__ == "__main__":
    main()
