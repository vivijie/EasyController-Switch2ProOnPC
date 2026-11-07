"""校准可视化预览图（复刻 canvas 画法，用于确认效果）"""

import math
import os

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
W, H = 620, 200
SCALE = 0.9

BG = '#0a0e13'
LINE = '#2a3340'
DIM = '#8b98a8'
GREEN = '#46d39a'
BLUE = '#4c9aff'


def font(size, bold=False):
    name = 'msyhbd.ttc' if bold else 'msyh.ttc'
    for path in (os.path.join('C:\\Windows\\Fonts', name),
                 os.path.join('C:\\Windows\\Fonts', 'msyh.ttc')):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


F12 = font(12, True)


def draw_panel(phase, pair, live, running=True):
    img = Image.new('RGB', (W, H), BG)
    d = ImageDraw.Draw(img)

    steps = [
        ('中心', phase in ('prepare', 'center'), phase in ('ready', 'edge', 'compute', 'done')),
        ('边缘', phase in ('ready', 'edge'), phase in ('compute', 'done')),
        ('完成', phase == 'compute', phase == 'done'),
    ]
    px0, gap, py = 372, 74, 62
    d.line([(px0, py), (px0 + gap * 2, py)], fill='#26303d', width=2)

    for i, (label, active, done) in enumerate(steps):
        cx = px0 + gap * i
        if done:
            d.ellipse([cx - 15, py - 15, cx + 15, py + 15], fill=GREEN)
            d.line([(cx - 6, py), (cx - 1, py + 5)], fill=BG, width=3)
            d.line([(cx - 1, py + 5), (cx + 7, py - 5)], fill=BG, width=3)
        elif active:
            d.ellipse([cx - 15, py - 15, cx + 15, py + 15], fill='#16283f', outline=BLUE, width=2)
        else:
            d.ellipse([cx - 15, py - 15, cx + 15, py + 15], fill='#151b26', outline=LINE, width=2)
        color = BLUE if active else (GREEN if done else '#67737f')
        tw = d.textlength(label, font=F12)
        d.text((cx - tw / 2, py + 26), label, fill=color, font=F12)

    gauges = [(96, 100, '左', 'left'), (232, 100, '右', 'right')]
    R = 62
    zoom = phase in ('prepare', 'center')

    for gx, gy, label, side in gauges:
        d.ellipse([gx - R, gy - R, gx + R, gy + R], fill='#0e131a',
                  outline='#3d4a5a' if zoom else LINE, width=2)
        d.line([(gx - R, gy), (gx + R, gy)], fill='#1d2530', width=1)
        d.line([(gx, gy - R), (gx, gy + R)], fill='#1d2530', width=1)

        dx, dy = pair[side] if pair and side in pair else (0.0, 0.0)
        nx = max(-1, min(1, dx / SCALE))
        ny = max(-1, min(1, -dy / SCALE))

        if zoom:
            rr = R * 0.34
            d.ellipse([gx - rr, gy - rr, gx + rr, gy + rr],
                      outline='#2b4a70', width=1)
            kx, ky = gx + nx * rr, gy + ny * rr
            d.ellipse([kx - 6, ky - 6, kx + 6, ky + 6], fill=BLUE)
        else:
            reach = 0.0
            if live and side in live:
                box = live[side]
                reach = max(abs(box[0]), abs(box[1]), abs(box[2]), abs(box[3])) / SCALE
                reach = max(0.0, min(1.0, reach))
            if reach > 0.01:
                d.arc([gx - (R - 5), gy - (R - 5), gx + (R - 5), gy + (R - 5)],
                      start=-90, end=-90 + 360 * reach, fill=GREEN, width=5)
            kx, ky = gx + nx * (R - 12), gy + ny * (R - 12)
            d.ellipse([kx - 7, ky - 7, kx + 7, ky + 7], fill=GREEN)

        tw = d.textlength(label, font=F12)
        d.text((gx - tw / 2, gy + R + 8), label, fill=DIM, font=F12)

    if not running and phase != 'done':
        d.text((440, 132), '待开始', fill='#67737f', font=F12)
    return img


# 中心阶段
p1 = draw_panel('center', {'left': (0.008, 0.012), 'right': (0.020, -0.015)}, None)
# 边缘阶段（转了约 70%）
p2 = draw_panel('edge', {'left': (0.62, 0.35), 'right': (-0.41, 0.66)},
                {'left': (-0.78, 0.65, -0.72, 0.58), 'right': (-0.52, 0.70, -0.61, 0.66)})
# 完成
p3 = draw_panel('done', {'left': (0.02, 0.01), 'right': (0.01, -0.02)},
                {'left': (-0.78, 0.79, -0.75, 0.76), 'right': (-0.70, 0.68, -0.66, 0.72)})

out = Image.new('RGB', (W, H * 3 + 70), '#0e1116')
d = ImageDraw.Draw(out)
labels = ['① 中心阶段（放大中心，蓝点 = 摇杆位置）',
          '② 边缘阶段（绿弧 = 已到达的行程，绿点 = 实时位置）',
          '③ 完成（三个阶段 ✓）']
for i, (img, text) in enumerate(zip([p1, p2, p3], labels)):
    y = i * (H + 23)
    out.paste(img, (0, y + 22))
    d.text((10, y + 4), text, fill='#e6edf3', font=F12)

path = os.path.join(BASE, 'calib_viz_preview.png')
out.save(path)
print('预览:', path, out.size)
