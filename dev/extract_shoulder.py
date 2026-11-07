"""从截图提取肩键(红)/扳机(蓝)轮廓，并叠加当前 SVG 路径做对照

校准：两个绿色背键圆（SVG cx=427/861, cy=1046, r=59, 描边 5.5）
  → 绿色填充半径 = 59 - 2.75 = 56.25，直径 112.5
"""

import io
import json
import math
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(BASE, 'ref_ui_screenshot.png')
HTML = os.path.join(BASE, 'sticklab', 'templates', 'index.html')
OUT = os.path.join(BASE, 'shoulder_landmarks.png')

GREEN_SVG = [(427.0, 1046.0), (861.0, 1046.0)]
GREEN_FILL_DIAMETER = 2 * (59 - 2.75)


# ---------------------------------------------------------------- 路径采样
def sample_path(d, steps=16):
    tokens = re.findall(r'([MCLHVQZ])|(-?\d+(?:\.\d+)?)', d)
    stream = [t if t else float(n) for t, n in tokens]
    points = []
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    pos = 0

    def bezier(p0, p1, p2, p3):
        out = []
        for i in range(1, steps + 1):
            t = i / steps
            mt = 1 - t
            out.append((
                mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0]
                + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0],
                mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1]
                + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1]))
        return out

    while pos < len(stream):
        while pos < len(stream) and not isinstance(stream[pos], str):
            pos += 1
        if pos >= len(stream):
            break
        cmd = stream[pos]
        i = pos + 1
        if cmd == 'M':
            current = (stream[i], stream[i + 1]); start = current
            points.append(current); i += 2
        elif cmd == 'L':
            current = (stream[i], stream[i + 1])
            points.append(current); i += 2
        elif cmd == 'H':
            current = (stream[i], current[1]); points.append(current); i += 1
        elif cmd == 'V':
            current = (current[0], stream[i]); points.append(current); i += 1
        elif cmd == 'C':
            p1 = (stream[i], stream[i + 1])
            p2 = (stream[i + 2], stream[i + 3])
            p3 = (stream[i + 4], stream[i + 5])
            points.extend(bezier(current, p1, p2, p3))
            current = p3; i += 6
        elif cmd == 'Q':
            p1 = (stream[i], stream[i + 1])
            p2 = (stream[i + 2], stream[i + 3])
            for k in range(1, steps + 1):
                t = k / steps
                mt = 1 - t
                points.append((mt * mt * current[0] + 2 * mt * t * p1[0]
                               + t * t * p2[0],
                               mt * mt * current[1] + 2 * mt * t * p1[1]
                               + t * t * p2[1]))
            current = p2; i += 4
        elif cmd == 'Z':
            points.append(start); current = start
        pos = i
    return points


def svg_paths(html, ids):
    """取出指定 id 的 path d 属性"""
    found = {}
    for name in ids:
        block = re.search(
            r'id="%s".*?<path[^>]*?d="([^"]+)"' % re.escape(name), html, re.S)
        if block:
            found[name] = ' '.join(block.group(1).split())
    return found


# ---------------------------------------------------------------- 图像处理
def load_masks(path):
    image = Image.open(path).convert('RGB')
    px = np.asarray(image).astype(np.int16)
    r, g, b = px[:, :, 0], px[:, :, 1], px[:, :, 2]
    return image, {
        'blue': (b > 150) & (r < 130) & (g < 130),
        'red': (r > 170) & (g < 90) & (b < 90),
        'green': (g > 170) & (r < 130) & (b < 130),
    }


def blob(mask, x_from, x_to):
    sub = mask[:, x_from:x_to]
    ys, xs = np.nonzero(sub)
    if len(xs) == 0:
        return None
    return {'cx': xs.mean() + x_from, 'cy': ys.mean(),
            'x0': xs.min() + x_from, 'x1': xs.max() + x_from,
            'y0': ys.min(), 'y1': ys.max(), 'n': len(xs),
            'x_from': x_from, 'mask': sub}


def radial(mask_full, info, angles=48):
    cx, cy = info['cx'], info['cy']
    out = []
    for i in range(angles):
        angle = 2 * math.pi * i / angles
        dx, dy = math.cos(angle), math.sin(angle)
        best = None
        dist = 0.0
        while dist < 420:
            x = int(round(cx + dx * dist))
            y = int(round(cy + dy * dist))
            if y < 0 or y >= mask_full.shape[0] or x < 0 or x >= mask_full.shape[1]:
                break
            if mask_full[y, x]:
                best = (cx + dx * dist, cy + dy * dist)
            dist += 0.5
        if best:
            out.append(best)
    return out


def main():
    image, masks = load_masks(REF)
    height, width = masks['green'].shape
    middle = width // 2

    gl = blob(masks['green'], 0, middle)
    gr = blob(masks['green'], middle, width)
    if not gl or not gr:
        raise SystemExit('没有找到两个绿色背键')

    # 截图正好是 viewBox 的等比放大：scale = 图像尺寸 / viewBox
    sx = width / 1296.0
    sy = height / 1152.0
    ox = 0.0
    oy = 0.0

    print('=' * 66)
    print('校准')
    print('=' * 66)
    print('  绿键中心(图像): 左 (%.1f, %.1f)  右 (%.1f, %.1f)'
          % (gl['cx'], gl['cy'], gr['cx'], gr['cy']))
    print('  绿键直径(图像): 水平 %d  垂直 %d'
          % (gl['x1'] - gl['x0'], gl['y1'] - gl['y0']))
    print('  换算: sx=%.4f sy=%.4f  ox=%.2f oy=%.2f' % (sx, sy, ox, oy))

    def to_svg(px_, py_):
        return ((px_ - ox) / sx, (py_ - oy) / sy)

    def to_img(sx_, sy_):
        return (sx_ * sx + ox, sy_ * sy + oy)

    # ---------------- 当前 SVG 路径 → 画到图上 ----------------
    html = io.open(HTML, encoding='utf-8').read()
    paths = svg_paths(html, ['left-trigger', 'left-shoulder',
                             'right-trigger', 'right-shoulder'])
    draw = ImageDraw.Draw(image)
    for name, d in paths.items():
        pts = [to_img(*p) for p in sample_path(d)]
        draw.line(pts + [pts[0]], fill=(255, 0, 255), width=3)

    # ---------------- 提取红/蓝轮廓 ----------------
    results = {}
    palette = {'blue': (0, 170, 255), 'red': (255, 200, 0)}
    for name in ('blue', 'red'):
        for side, x_from, x_to in (('left', 0, middle),
                                   ('right', middle, width)):
            info = blob(masks[name], x_from, x_to)
            if not info:
                continue
            outline = radial(masks[name], info)
            draw.line(outline + [outline[0]], fill=(0, 200, 120), width=4)
            results['%s_%s' % (name, side)] = {
                'points_svg': [to_svg(x, y) for x, y in outline],
                'centre_svg': to_svg(info['cx'], info['cy']),
            }

    # ---------------- 编号标注 ----------------
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 19)
    except Exception:
        font = ImageFont.load_default()

    def mark(svg_xy, text, colour):
        px_, py_ = to_img(*svg_xy)
        draw.ellipse([px_ - 6, py_ - 6, px_ + 6, py_ + 6],
                     fill=colour, outline=(255, 255, 255))
        draw.text((px_ + 9, py_ - 24), text, fill=colour, font=font)

    print()
    print('=' * 66)
    print('提取到的轮廓（SVG 坐标）')
    print('=' * 66)
    for key in ('blue_left', 'red_left', 'blue_right', 'red_right'):
        data = results.get(key)
        if not data:
            continue
        pts = data['points_svg']
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        print('  %-11s 中心(%6.1f,%6.1f)  x %6.1f..%6.1f  y %6.1f..%6.1f'
              % (key, data['centre_svg'][0], data['centre_svg'][1],
                 min(xs), max(xs), min(ys), max(ys)))

    # 左扳机四角 + 左肩键左右两端
    bl = results.get('blue_left')
    if bl:
        xs = [p[0] for p in bl['points_svg']]
        ys = [p[1] for p in bl['points_svg']]
        corners = [(min(xs), min(ys)), (max(xs), min(ys)),
                   (max(xs), max(ys)), (min(xs), max(ys))]
        for idx, corner in enumerate(corners, 1):
            mark(corner, 'B%d' % idx, (255, 0, 180))

    rl = results.get('red_left')
    if rl:
        pts = rl['points_svg']
        leftmost = min(pts, key=lambda p: p[0])
        rightmost = max(pts, key=lambda p: p[0])
        topmost = min(pts, key=lambda p: p[1])
        bottommost = max(pts, key=lambda p: p[1])
        mark(leftmost, 'R1', (255, 120, 0))
        mark(rightmost, 'R2', (255, 120, 0))
        mark(topmost, 'R3', (255, 120, 0))
        mark(bottommost, 'R4', (255, 120, 0))

    image.save(OUT)
    with io.open(os.path.join(BASE, 'landmarks.json'), 'w',
                 encoding='utf-8') as file_object:
        json.dump(results, file_object, ensure_ascii=False, indent=1)
    print()
    print('确认图: %s' % OUT)


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
