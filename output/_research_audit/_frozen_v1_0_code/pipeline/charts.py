"""图表动画：公开数据 → 自制动态图表（零版权，时评画面主力）。

图表类型（spec.json 的 type 字段）：
- bar    柱状对比动画（柱子生长 + 数字滚动）
- line   折线趋势动画（线条生长 + 端点游标）
- timeline 时间线推进（事件逐个亮起）
输出 1920x1080 mp4，风格与 cardgen 一致；图上强制带「数据来源」标注——
图表必须注明数据出处（公开发布数据），这是引用规范的一部分。

用法：python -m pipeline.charts spec.json out.mp4
spec 示例（bar）：
{"type":"bar","title":"历年双十一成交额","source":"数据来源：平台公开财报",
 "items":[["2019",2684],["2020",4982],["2021",5403]]}
"""
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from pipeline.cardgen import ACCENT, C_BOT, C_TOP, H, W, _font
from pipeline.tts import FFMPEG

FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"
FPS = 30
ANIM_S, HOLD_S = 2.2, 1.0  # 动画时长 / 结尾停留


def _bg() -> tuple:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        col = tuple(int(a + (b - a) * t) for a, b in zip(C_TOP, C_BOT))
        draw.line([(0, y), (W, y)], fill=col)
    return img, draw


def _frame_head(draw: ImageDraw.ImageDraw, title: str, source: str) -> None:
    f_t = _font(FONT_BOLD, 62)
    draw.text((110, 84), title, font=f_t, fill=(245, 250, 252))
    if source:
        draw.text((112, H - 72), source[:60], font=_font(FONT_REG, 30), fill=(150, 175, 190))


def _ease(t: float) -> float:
    return 1 - (1 - t) ** 3  # ease-out cubic


def _render(frames_dir: Path, out: Path, render_fn, n_frames: int) -> Path:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_frames):
        t = _ease(min(1.0, i / max(1, ANIM_S * FPS)))
        img, draw = render_fn(t)
        img.save(frames_dir / f"f{i:04d}.png")
    proc = subprocess.run(
        [FFMPEG, "-y", "-framerate", str(FPS), "-i", str(frames_dir / "f%04d.png"),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "19", str(out)],
        capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"图表视频合成失败: {proc.stderr[-300:]}")
    return out


def bar_chart(spec: dict, out: Path) -> Path:
    items = [(str(a), float(b)) for a, b in spec.get("items", [])]
    vmax = max((v for _, v in items), default=1) or 1

    def render(t: float):
        img, draw = _bg()
        _frame_head(draw, spec.get("title", ""), spec.get("source", ""))
        x0, x1, base = 220, W - 220, H - 190
        top = 260
        n = max(1, len(items))
        slot = (x1 - x0) / n
        for i, (label, val) in enumerate(items):
            grow = max(0.0, min(1.0, t * n - i * 0.6))  # 逐根错峰生长
            h = (base - top) * (val / vmax) * grow
            cx = x0 + slot * (i + 0.5)
            bw = slot * 0.42
            draw.rectangle([cx - bw / 2, base - h, cx + bw / 2, base], fill=ACCENT)
            shown = int(val * grow)
            f_v = _font(FONT_BOLD, 46)
            txt = f"{shown:,}"
            bb = draw.textbbox((0, 0), txt, font=f_v)
            draw.text((cx - (bb[2] - bb[0]) / 2, base - h - 62), txt, font=f_v, fill=(245, 250, 252))
            f_l = _font(FONT_REG, 38)
            bb2 = draw.textbbox((0, 0), label, font=f_l)
            draw.text((cx - (bb2[2] - bb2[0]) / 2, base + 18), label, font=f_l, fill=(180, 200, 212))
        draw.line([(x0, base), (x1, base)], fill=(90, 120, 140), width=3)
        return img, draw

    return _render(out.parent / (out.stem + "_frames"), out, render,
                   int((ANIM_S + HOLD_S) * FPS))


def line_chart(spec: dict, out: Path) -> Path:
    pts = [(str(a), float(b)) for a, b in spec.get("items", [])]
    vmax = max((v for _, v in pts), default=1) or 1
    vmin = min((v for _, v in pts), default=0)

    def render(t: float):
        img, draw = _bg()
        _frame_head(draw, spec.get("title", ""), spec.get("source", ""))
        x0, x1, base, top = 240, W - 160, H - 190, 280
        n = max(1, len(pts) - 1)
        coords = []
        for i, (_, val) in enumerate(pts):
            x = x0 + (x1 - x0) * i / n
            y = base - (base - top) * ((val - vmin) / (vmax - vmin or 1))
            coords.append((x, y))
        visible = min(len(coords), max(2, int(len(coords) * t) + 1))
        draw.line(coords[:visible], fill=ACCENT, width=7, joint="curve")
        hx, hy = coords[visible - 1]
        draw.ellipse([hx - 12, hy - 12, hx + 12, hy + 12], fill=(245, 250, 252))
        f_l = _font(FONT_REG, 36)
        for i, (label, val) in enumerate(pts):
            if i < visible:
                x, _ = coords[i]
                bb = draw.textbbox((0, 0), label, font=f_l)
                draw.text((x - (bb[2] - bb[0]) / 2, base + 18), label, font=f_l, fill=(180, 200, 212))
        f_v = _font(FONT_BOLD, 48)
        cur = int(pts[visible - 1][1])
        draw.text((hx + 18, hy - 70), f"{cur:,}", font=f_v, fill=ACCENT)
        draw.line([(x0, base), (x1, base)], fill=(90, 120, 140), width=3)
        return img, draw

    return _render(out.parent / (out.stem + "_frames"), out, render,
                   int((ANIM_S + HOLD_S) * FPS))


def timeline_chart(spec: dict, out: Path) -> Path:
    events = [(str(a), str(b)) for a, b in spec.get("items", [])]

    def render(t: float):
        img, draw = _bg()
        _frame_head(draw, spec.get("title", ""), spec.get("source", ""))
        y = H // 2
        draw.line([(140, y), (W - 140, y)], fill=(90, 120, 140), width=5)
        n = max(1, len(events))
        for i, (date, text) in enumerate(events):
            x = 180 + (W - 360) * i / max(1, n - 1) if n > 1 else W // 2
            on = t * n - i * 0.7
            if on <= 0:
                continue
            glow = min(1.0, on)
            r = int(10 + 8 * glow)
            draw.ellipse([x - r, y - r, x + r, y + r], fill=ACCENT)
            f_d = _font(FONT_BOLD, 36)
            f_t = _font(FONT_REG, 28)
            bb = draw.textbbox((0, 0), date, font=f_d)
            draw.text((x - (bb[2] - bb[0]) / 2, y - 96), date, font=f_d, fill=(245, 250, 252))
            # 事件文字上下交替、手动折行
            words = text[:14] + ("…" if len(text) > 14 else "")
            bb2 = draw.textbbox((0, 0), words, font=f_t)
            ty = y + 46 if i % 2 else y - 62
            draw.text((x - (bb2[2] - bb2[0]) / 2, ty), words, font=f_t, fill=(180, 200, 212))
        return img, draw

    return _render(out.parent / (out.stem + "_frames"), out, render,
                   int((ANIM_S + HOLD_S) * FPS))


RENDERERS = {"bar": bar_chart, "line": line_chart, "timeline": timeline_chart}


def render_chart(spec: dict, out: Path) -> Path:
    fn = RENDERERS.get(spec.get("type", "bar"))
    if fn is None:
        raise ValueError(f"未知图表类型: {spec.get('type')}")
    return fn(spec, Path(out))


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = render_chart(spec, Path(sys.argv[2]))
    print("done:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
