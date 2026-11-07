"""把提取到的肩键/扳机真实轮廓简化为控制点，输出确认图

输出：
  shoulder_proposal.png   —— 编号控制点 + 拟合曲线
  控制点坐标打印到控制台，供下一步写入 SVG
"""

import io
import json
import math
import os

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(BASE, 'ref_ui_screenshot.png')
DATA = os.path.join(BASE, 'landmarks.json')
OUT = os.path.join(BASE, 'shoulder_proposal.png')

SX = 1313.0 / 1296.0
SY = 1198.0 / 1152.0


def to_img(p):
    return (p[0] * SX, p[1] * SY)


def perpendicular(p, a, b):
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    x0, y0 = p
    x1, y1 = a
    x2, y2 = b
    num = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
    den = math.hypot(y2 - y1, x2 - x1)
    return num / den


def douglas_peucker(points, epsilon):
    if len(points) < 3:
        return list(points)
    start, end = points[0], points[-1]
    index, best = 0, -1.0
    for i in range(1, len(points) - 1):
        d = perpendicular(points[i], start, end)
        if d > best:
            index, best = i, d
    if best > epsilon:
        left = douglas_peucker(points[:index + 1], epsilon)
        right = douglas_peucker(points[index:], epsilon)
        return left[:-1] + right
    return [start, end]


def smooth_path(points, radius=18):
    """把折线转成带圆角的路径（每段用二次贝塞尔过渡）"""
    parts = ['M %.1f %.1f' % points[0]]
    count = len(points)
    for i in range(count):
        prev = points[(i - 1) % count]
        cur = points[i]
        nxt = points[(i + 1) % count]

        def trimmed(a, b, dist):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy) or 1.0
            t = min(dist / length, 0.5)
            return (a[0] + dx * t, a[1] + dy * t)

        p1 = trimmed(cur, prev, radius)
        p2 = trimmed(cur, nxt, radius)
        parts.append('L %.1f %.1f' % p1)
        parts.append('Q %.1f %.1f %.1f %.1f' % (cur[0], cur[1], p2[0], p2[1]))
    parts.append('Z')
    return ' '.join(parts)


# 手工微调：{(原 x, 原 y): (新 x, 新 y)}
MANUAL_FIX = {
    (448.6, 234.1): (448.6, 238.1),   # 肩键 R8 下移 4px
}


def apply_fixes(points):
    out = []
    for point in points:
        fixed = point
        for src, dst in MANUAL_FIX.items():
            if abs(point[0] - src[0]) < 1.5 and abs(point[1] - src[1]) < 1.5:
                fixed = dst
        out.append(fixed)
    return out


def main():
    data = json.load(open(DATA, encoding='utf-8'))
    image = Image.open(REF).convert('RGB')
    draw = ImageDraw.Draw(image)

    try:
        font = ImageFont.truetype('C:/Windows/Fonts/consola.ttf', 18)
    except Exception:
        font = ImageFont.load_default()

    proposals = {}
    for key, label, colour in (('blue_left', '扳机 ZL', (0, 200, 80)),
                               ('red_left', '肩键 L', (255, 90, 200))):
        raw = [tuple(pt) for pt in data[key]['points_svg']]
        # 闭合曲线：复制首点便于 DP
        closed = raw + [raw[0]]
        simplified = apply_fixes(douglas_peucker(closed, 5.0)[:-1])
        path = smooth_path(simplified)
        proposals[key] = {'points': simplified, 'path': path}

        # 画拟合曲线
        drawn = [to_img(p) for p in simplified]
        draw.line(drawn + [drawn[0]], fill=colour, width=4)

        # 编号每个控制点
        for index, point in enumerate(simplified, 1):
            x, y = to_img(point)
            draw.ellipse([x - 7, y - 7, x + 7, y + 7],
                         fill=colour, outline=(255, 255, 255), width=2)
            draw.text((x + 10, y - 22), '%s%d' % (label[0], index),
                      fill=colour, font=font)

        print()
        print('=' * 58)
        print('%s  控制点（SVG 坐标，顺时针）' % label)
        print('=' * 58)
        for index, point in enumerate(simplified, 1):
            print('  %2d  (%7.1f, %7.1f)' % (index, point[0], point[1]))
        print()
        print('  路径:')
        print('  ' + path)

    image.save(OUT)
    print()
    print('确认图: %s' % OUT)

    with io.open(os.path.join(BASE, 'proposal.json'), 'w',
                 encoding='utf-8') as file_object:
        json.dump(proposals, file_object, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
