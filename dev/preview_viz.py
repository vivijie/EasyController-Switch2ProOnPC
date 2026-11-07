"""预览网页里"方圆 / 死区"图形的实际效果（与前端同一套公式）"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'viz_preview.png')

SIZE = 190
RATIO = 0.62
BG = (11, 15, 20)
PANEL = (23, 28, 36)
GRID = (36, 48, 64)
ACCENT = (76, 154, 255)
GREEN = (70, 211, 154)
RED = (242, 85, 90)
AMBER = (240, 180, 41)
DIM = (139, 152, 168)
GHOST = (190, 205, 225)


def make_canvas():
    image = Image.new('RGB', (SIZE, SIZE), BG)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=10,
                           outline=GRID, width=1)
    return image, draw


def dashed_circle(draw, cx, cy, r, colour, width=1, dash=6):
    steps = 120
    for i in range(steps):
        if i % 2:
            continue
        a0 = 2 * math.pi * i / steps
        a1 = 2 * math.pi * (i + 0.8) / steps
        draw.line([cx + r * math.cos(a0), cy + r * math.sin(a0),
                   cx + r * math.cos(a1), cy + r * math.sin(a1)],
                  fill=colour, width=width)


def shape_viz(strength):
    image, draw = make_canvas()
    cx = cy = SIZE / 2
    R = SIZE / 2 * RATIO

    for i in range(0, 360, 6):
        if (i // 6) % 2:
            continue
        a0 = math.radians(i)
        a1 = math.radians(i + 5)
        draw.line([cx + R * math.cos(a0), cy + R * math.sin(a0),
                   cx + R * math.cos(a1), cy + R * math.sin(a1)], fill=GRID)
    draw.rectangle([cx - R, cy - R, cx + R, cy + R], outline=GRID)

    pts = []
    for i in range(200):
        a = 2 * math.pi * i / 200
        ux, uy = math.cos(a), math.sin(a)
        m = max(abs(ux), abs(uy)) or 1
        pts.append((cx + (ux * (1 - strength) + ux / m * strength) * R,
                    cy + (uy * (1 - strength) + uy / m * strength) * R))
    draw.polygon(pts, fill=(30, 60, 100), outline=ACCENT)
    draw.line(pts + [pts[0]], fill=ACCENT, width=2)

    draw.line([cx - R * 1.22, cy, cx + R * 1.22, cy], fill=(60, 74, 92))
    draw.line([cx, cy - R * 1.22, cx, cy + R * 1.22], fill=(60, 74, 92))
    dashed_circle(draw, cx, cy, R, GHOST, 2)
    return image


def deadzone_viz(inner, outer):
    image, draw = make_canvas()
    cx = cy = SIZE / 2
    R = SIZE / 2 * RATIO
    r_in = max(0.0, inner) * R
    r_out = min(1.35, max(0.05, outer)) * R

    dashed_circle(draw, cx, cy, R, GHOST, 2)

    draw.ellipse([cx - R * 1.35, cy - R * 1.35, cx + R * 1.35, cy + R * 1.35],
                 fill=(24, 44, 38))
    if r_in > 1:
        draw.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in],
                     fill=(74, 34, 38), outline=RED, width=2)
    if inner < 0:
        boost = min(R * 1.1, -inner * R * 3.2)
        steps = 18
        for k in range(steps, 0, -1):
            r = boost * k / steps
            alpha = int(150 * (1 - k / steps) ** 1.4) + 20
            draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                         fill=(int(24 + 46 * (1 - k / steps)),
                               int(44 + 167 * (1 - k / steps)),
                               int(38 + 116 * (1 - k / steps))))
        dashed_circle(draw, cx, cy, boost, GREEN, 2, dash=4)
    draw.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out],
                 outline=AMBER, width=3)

    draw.line([cx - R * 1.22, cy, cx + R * 1.22, cy], fill=(60, 74, 92))
    draw.line([cx, cy - R * 1.22, cx, cy + R * 1.22], fill=(60, 74, 92))
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=DIM)
    dashed_circle(draw, cx, cy, R, GHOST, 2)
    return image


def main():
    cards = [
        ('方圆 0.00', shape_viz(0.0)),
        ('方圆 0.50', shape_viz(0.5)),
        ('方圆 1.00', shape_viz(1.0)),
        ('死区 0（默认）', deadzone_viz(0.03, 1.0)),
        ('中心 0.25 / 边缘 0.15', deadzone_viz(0.25, 0.85)),
        ('边缘 -0.15（提前满偏）', deadzone_viz(0.0, 1.15)),
        ('中心 -0.08（放大信号）', deadzone_viz(-0.08, 1.0)),
        ('中心 0.2 / 边缘 0.1', deadzone_viz(0.2, 0.9)),
    ]
    gap = 16
    label_h = 26
    cols = 3
    rows = 3
    width = cols * SIZE + (cols + 1) * gap
    height = rows * (SIZE + label_h) + (rows + 1) * gap
    sheet = Image.new('RGB', (width, height), PANEL)
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 15)
    except Exception:
        font = ImageFont.load_default()

    for index, (label, image) in enumerate(cards):
        col = index % cols
        row = index // cols
        x = gap + col * (SIZE + gap)
        y = gap + row * (SIZE + label_h + gap)
        sheet.paste(image, (x, y))
        draw.text((x + 2, y + SIZE + 5), label, fill=(200, 210, 225), font=font)

    sheet.save(OUT)
    print('预览图:', OUT, sheet.size)


if __name__ == '__main__':
    main()
