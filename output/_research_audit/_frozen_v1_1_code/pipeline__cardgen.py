"""自制素材：字卡渲染（Pillow）——零版权风险的画面。

用途：
1. 素材检索无命中时的兜底画面（替代纯色，规避低质判定）；
2. 自建"字卡素材库"：同一套模板批量出图，攒出完全自有版权的画面资产。
设计：深色渐变底 + 装饰几何 + 大字关键词，横屏 1920x1080，配合 assemble 的
Ken Burns 缓推动镜（zoompan）产生画面呼吸感。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_REG = "C:/Windows/Fonts/msyh.ttc"

C_TOP = (13, 27, 42)      # 深海军蓝
C_BOT = (27, 58, 75)      # 深青
ACCENT = (125, 211, 252)  # 亮青（关键词/装饰）


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FONT_REG, size)  # 粗体缺失退回常规


def _gradient(img: Image.Image) -> None:
    draw = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        col = tuple(int(a + (b - a) * t) for a, b in zip(C_TOP, C_BOT))
        draw.line([(0, y), (W, y)], fill=col)


def _decor(img: Image.Image) -> None:
    """半透明几何装饰：右上大圆环、左下光斑、四角细节。"""
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.ellipse([W - 560, -320, W + 260, 500], fill=(255, 255, 255, 14))
    d.ellipse([W - 430, -190, W + 130, 370], outline=(255, 255, 255, 34), width=3)
    d.ellipse([-260, H - 420, 300, H + 220], fill=(125, 211, 252, 18))
    for i in range(4):
        x = 96 + i * 26
        d.ellipse([x, H - 96, x + 10, H - 86], fill=(*ACCENT, 150 - i * 28))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def _wrap(text: str, per_line: int, max_lines: int) -> list:
    text = text.strip()
    lines = [text[i:i + per_line] for i in range(0, len(text), per_line)]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines or [""]


def render_card(text: str, out: Path, keyword: str = "") -> Path:
    """渲染一张 1920x1080 字卡：keyword 小标签 + text 大字主视觉。"""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (W, H))
    _gradient(img)
    _decor(img)
    draw = ImageDraw.Draw(img)

    main = text.strip()
    if len(main) <= 14:
        size, per_line, max_lines = 128, 8, 2
    elif len(main) <= 30:
        size, per_line, max_lines = 100, 11, 3
    elif len(main) <= 60:
        size, per_line, max_lines = 76, 14, 4
    else:
        size, per_line, max_lines = 60, 18, 4

    lines = _wrap(main, per_line, max_lines)
    f_main = _font(FONT_BOLD, size)
    line_h = int(size * 1.42)

    y = H // 2 - (len(lines) * line_h) // 2
    if keyword:
        f_tag = _font(FONT_REG, 42)
        tag_y = y - 96
        draw.rectangle([W // 2 - 300, tag_y - 14, W // 2 - 292, tag_y + 52], fill=ACCENT)
        draw.text((W // 2 - 262, tag_y), keyword[:12], font=f_tag, fill=ACCENT)
        y = tag_y + 110

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=f_main)
        x = (W - (bbox[2] - bbox[0])) // 2
        draw.text((x + 3, y + 4), line, font=f_main, fill=(0, 0, 0))   # 投影
        draw.text((x, y), line, font=f_main, fill=(245, 250, 252))
        y += line_h

    img.save(out, "PNG")
    return out


if __name__ == "__main__":
    import sys

    render_card(sys.argv[1] if len(sys.argv) > 1 else "测试字卡：自制素材零版权风险",
                Path(sys.argv[2]) if len(sys.argv) > 2 else Path("卡片测试.png"))
