"""Generate the home-screen icons (app/static/icon-*.png).

Ivory paper, a navy serif "T", and a brass rule: the day-sheet look of the app.
iOS rounds the corners itself, so the art fills the full square.

Usage: python scripts/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PAPER = (246, 242, 233)
NAVY = (34, 54, 90)
BRASS = (168, 123, 47)
OUT = Path(__file__).resolve().parents[1] / "app" / "static"
FONT_CANDIDATES = ["C:/Windows/Fonts/georgiab.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"]


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), PAPER)
    draw = ImageDraw.Draw(img)
    letter = font(int(size * 0.58))
    box = draw.textbbox((0, 0), "T", font=letter)
    w, h = box[2] - box[0], box[3] - box[1]
    x = (size - w) / 2 - box[0]
    y = (size - h) / 2 - box[1] - size * 0.04
    draw.text((x, y), "T", font=letter, fill=NAVY)
    rule_w, rule_h = size * 0.34, max(2, size * 0.035)
    top = y + box[3] + size * 0.07
    draw.rectangle([(size - rule_w) / 2, top, (size + rule_w) / 2, top + rule_h], fill=BRASS)
    return img


if __name__ == "__main__":
    for size in (180, 192, 512):
        icon(size).save(OUT / f"icon-{size}.png")
        print(f"wrote icon-{size}.png")
