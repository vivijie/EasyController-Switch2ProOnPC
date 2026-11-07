"""把确认过的扳机/肩键轮廓写入 SVG（右侧按 x=1296-x 镜像）

同时输出一张叠加校验图 verify_paths.png
"""

import io
import json
import math
import os
import re

from PIL import Image, ImageDraw

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'sticklab', 'templates', 'index.html')
DATA = os.path.join(BASE, 'proposal.json')
REF = os.path.join(BASE, 'ref_ui_screenshot.png')
VERIFY = os.path.join(BASE, 'verify_paths.png')

SX = 1313.0 / 1296.0
SY = 1198.0 / 1152.0


def mirror(points):
    """沿 x=1296 镜像，并反序保持绕向一致"""
    return [(1296.0 - x, y) for x, y in reversed(points)]


def smooth_path(points, radius=16):
    parts = ['M %.1f %.1f' % tuple(points[0])]
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


def sample(path, steps=14):
    """把路径采样成折线，用于画校验图"""
    tokens = re.findall(r'([MLQZ])|(-?\d+(?:\.\d+)?)', path)
    stream = [t if t else float(n) for t, n in tokens]
    pts = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    pos = 0
    while pos < len(stream):
        while pos < len(stream) and not isinstance(stream[pos], str):
            pos += 1
        if pos >= len(stream):
            break
        cmd = stream[pos]
        i = pos + 1
        if cmd == 'M':
            cur = (stream[i], stream[i + 1]); start = cur
            pts.append(cur); i += 2
        elif cmd == 'L':
            cur = (stream[i], stream[i + 1])
            pts.append(cur); i += 2
        elif cmd == 'Q':
            p1 = (stream[i], stream[i + 1])
            p2 = (stream[i + 2], stream[i + 3])
            for k in range(1, steps + 1):
                t = k / steps
                mt = 1 - t
                pts.append((mt * mt * cur[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                            mt * mt * cur[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
            cur = p2; i += 4
        elif cmd == 'Z':
            pts.append(start); cur = start
        pos = i
    return pts


def main():
    data = json.load(open(DATA, encoding='utf-8'))
    trigger_left = [tuple(p) for p in data['blue_left']['points']]
    shoulder_left = [tuple(p) for p in data['red_left']['points']]

    paths = {
        'left-trigger': smooth_path(trigger_left),
        'left-shoulder': smooth_path(shoulder_left),
        'right-trigger': smooth_path(mirror(trigger_left)),
        'right-shoulder': smooth_path(mirror(shoulder_left)),
    }

    # ---------- 校验图 ----------
    image = Image.open(REF).convert('RGB')
    draw = ImageDraw.Draw(image)
    colours = {'left-trigger': (0, 220, 90), 'right-trigger': (0, 220, 90),
               'left-shoulder': (255, 0, 200), 'right-shoulder': (255, 0, 200)}
    for name, path in paths.items():
        pts = [(x * SX, y * SY) for x, y in sample(path)]
        draw.line(pts, fill=colours[name], width=4)
    image.save(VERIFY)
    print('校验图:', VERIFY)

    # ---------- 写入 HTML ----------
    html = io.open(HTML, encoding='utf-8').read()
    for name, path in paths.items():
        pattern = re.compile(
            r'(id="%s".*?<path[^>]*?d=")([^"]+)(")' % re.escape(name), re.S)
        assert len(pattern.findall(html)) == 1, '%s 匹配异常' % name
        html = pattern.sub(lambda m: m.group(1) + path + m.group(3), html, count=1)

    io.open(HTML, 'w', encoding='utf-8', newline='\n').write(html)
    print('已写入 4 条路径')
    for name, path in paths.items():
        print('  %-15s %d 字符' % (name, len(path)))


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
