"""预览新版校准画布：瞄准镜（中心）+ 覆盖度环（边缘）"""

import math
import os
import random

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, 'calib_preview.png')

W = H = 360
BG = (11, 15, 20)
PANEL = (23, 28, 36)
GREEN = (70, 211, 154)
BLUE = (76, 154, 255)
GHOST = (190, 205, 225)
FONT13 = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 13)
FONT14 = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 14)


def base():
    image = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(image)
    cx, cy = W / 2, H / 2
    R = min(W, H) / 2 - 28
    return image, draw, cx, cy, R


def scope(samples=180, locked=False):
    image, draw, cx, cy, R = base()
    zoom = 8
    # 极坐标网格
    for k in (0.35, 0.7, 1.0):
        draw.ellipse([cx - R * k, cy - R * k, cx + R * k, cy + R * k],
                     outline=(32, 44, 58))
    for i in range(8):
        a = math.pi * 2 * i / 8
        draw.line([cx, cy, cx + math.cos(a) * R, cy + math.sin(a) * R],
                  fill=(26, 36, 48))

    random.seed(9)
    pts = []
    for _ in range(samples):
        pts.append((random.gauss(0.002, 0.008), random.gauss(-0.001, 0.008)))
    total = len(pts)
    for i, (x, y) in enumerate(pts):
        px, py = cx + x * R * zoom, cy - y * R * zoom
        if (px - cx) ** 2 + (py - cy) ** 2 > R * R:
            continue
        age = i / max(1, total - 1)
        v = 0.18 + 0.72 * age
        draw.ellipse([px - 2.2, py - 2.2, px + 2.2, py + 2.2],
                     fill=(int(GREEN[0] * v), int(GREEN[1] * v), int(GREEN[2] * v)))

    lock = min(1.0, total / 180)
    ring_r = R * (1 - 0.82 * lock)
    colour = GREEN if lock >= 1 else BLUE
    for i in range(0, 360, 14):
        draw.arc([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                 i, i + 7, fill=colour, width=2)

    ax = sum(p[0] for p in pts) / total
    ay = sum(p[1] for p in pts) / total
    mx, my = cx + ax * R * zoom, cy - ay * R * zoom
    draw.line([mx - 18, my, mx + 18, my], fill=colour, width=2)
    draw.line([mx, my - 18, mx, my + 18], fill=colour, width=2)
    draw.ellipse([mx - 9, my - 9, mx + 9, my + 9], outline=colour, width=2)
    if lock >= 1:
        draw.ellipse([mx - 3, my - 3, mx + 3, my + 3], fill=colour)

    draw.text((cx, 20), "中心捕捉 · 放大 8 倍", fill=BLUE, font=FONT13, anchor="mm")
    draw.text((cx, H - 18), "✔ 中心已锁定" if lock >= 1 else "保持静止",
              fill=GREEN if lock >= 1 else (139, 152, 168), font=FONT14,
              anchor="mm")
    return image


def coverage(progress=0.72):
    image, draw, cx, cy, R = base()
    for i in range(0, 360, 12):
        draw.arc([cx - R, cy - R, cx + R, cy + R], i, i + 6,
                 fill=(110, 122, 140), width=2)

    sectors = 32
    seg = 360 / sectors
    ring_r = R + 14
    lit_count = int(sectors * progress)
    for i in range(sectors):
        lit = i < lit_count
        colour = GREEN if lit else (60, 74, 92)
        width = 7 if lit else 5
        draw.arc([cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r],
                 i * seg + 1.5, (i + 1) * seg - 1.5, fill=colour, width=width)

    trail = []
    total = 220
    for i in range(total):
        a = math.radians(i * 2.4)
        r = R * (0.93 + 0.04 * math.sin(i * 0.33))
        trail.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    for i in range(1, total):
        age = i / total
        v = 0.05 + 0.55 * age
        draw.line([trail[i - 1], trail[i]],
                  fill=(int(GREEN[0] * v), int(GREEN[1] * v), int(GREEN[2] * v)),
                  width=2)
    px, py = trail[-1]
    draw.ellipse([px - 9, py - 9, px + 9, py + 9], fill=(24, 66, 50))
    draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=GREEN)

    draw.text((cx, H - 34), "边缘覆盖 %d%%" % int(progress * 100), fill=GREEN,
              font=FONT14, anchor="mm")
    draw.text((cx, H - 12), "沿边缘转圈，把外环点亮", fill=(139, 152, 168),
              font=FONT13, anchor="mm")
    return image


def main():
    cards = [
        ('① 中心捕捉（放大 8 倍 · 采样收敛）', scope(90, False)),
        ('② 中心锁定', scope(200, True)),
        ('③ 边缘覆盖环', coverage(0.72)),
    ]
    gap, label_h = 16, 26
    width = 3 * W + 4 * gap
    height = H + 2 * gap + label_h
    sheet = Image.new('RGB', (width, height), PANEL)
    draw = ImageDraw.Draw(sheet)
    for index, (text, image) in enumerate(cards):
        x = gap + index * (W + gap)
        sheet.paste(image, (x, gap))
        draw.text((x + 2, gap + H + 5), text, fill=(200, 210, 225), font=FONT14)
    sheet.save(OUT)
    print('预览:', OUT, sheet.size)


if __name__ == '__main__':
    main()
