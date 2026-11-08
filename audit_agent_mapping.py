"""Diagnostic ASR coverage for agent-selected clip windows; never a Q2 score."""
import argparse
import json
from pathlib import Path

from pipeline import matcher
from pipeline.writer import flatten_sentences


def inspect(workdir: Path) -> list[dict]:
    script = json.loads((workdir / "文稿.json").read_text(encoding="utf-8"))
    mapping = json.loads((workdir / "采集映射.json").read_text(encoding="utf-8"))
    sentences = flatten_sentences(script)
    rows = []
    for clip in mapping["clips"]:
        video = matcher._find_local(clip["src"])
        duration = matcher._probe_dur(video)
        start = clip["frac"] * max(1.0, duration - matcher.CLIP_DUR - 2)
        lines = matcher.transcribe_cached(video)
        heard = " ".join(line["content"] for line in lines
                         if line["from"] < start + matcher.CLIP_DUR and line["to"] > start)
        sentence = sentences[clip["index"]]
        grams = matcher._bigrams(sentence)
        coverage = len(grams & matcher._bigrams(heard)) / max(1, len(grams))
        rows.append({"index": clip["index"], "src": clip["src"],
                     "start_seconds": round(start, 1), "coverage": round(coverage, 3),
                     "sentence": sentence, "asr_excerpt": heard[:160],
                     "manual_visual_review_required": True})
    return sorted(rows, key=lambda row: row["coverage"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir", type=Path)
    args = ap.parse_args()
    rows = inspect(args.workdir)
    (args.workdir / "agent_mapping_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"clips": len(rows),
                      "coverage_below_0_12": sum(row["coverage"] < 0.12 for row in rows),
                      "lowest": [{k: row[k] for k in ("index", "src", "start_seconds", "coverage")}
                                 for row in rows[:8]]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
