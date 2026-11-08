"""字幕：由逐句时间段生成 SRT（无需 ASR，与配音天然同步）。"""
from pathlib import Path


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list, out: Path) -> Path:
    """segments: [{start, end, text}] → SRT（带 BOM，播放器兼容中文）。"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i), f"{_ts(seg['start'])} --> {_ts(seg['end'])}", seg["text"], ""]
    out.write_text("\n".join(lines), encoding="utf-8-sig")
    return out
