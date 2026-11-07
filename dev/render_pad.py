"""把 index.html 里的手柄 SVG 按真实绘制顺序渲染成 PNG（Pillow 手绘）

用途：确认网页上肩键/扳机到底被什么遮住了，而不是靠猜。
支持：path(M/L/C/Q/H/V/Z) circle rect polygon text，group translate
"""

import io
import math
import os
import re
import xml.etree.ElementTree as ET

import sys

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = os.path.join(BASE, 'sticklab', 'templates', 'index.html')
OUT = os.path.join(BASE, 'page_render.png')

NS = '{http://www.w3.org/2000/svg}'

# 主题配色（对应 index.html 里的 CSS）
STYLE = {
    'line':   {'fill': (21, 27, 38), 'stroke': (43, 57, 71), 'width': 6.5},
    'key':    {'fill': (36, 46, 61), 'stroke': (56, 71, 92), 'width': 5.5},
    'solid':  {'fill': (47, 59, 79), 'stroke': (56, 71, 92), 'width': 5.5},
    'shoulder': {'fill': (59, 74, 97), 'stroke': (56, 71, 92), 'width': 5.5},
    'open':   {'fill': None, 'stroke': (43, 57, 71), 'width': 6.5},
    'back':   {'fill': (34, 42, 54), 'stroke': (77, 95, 120), 'width': 5.5},
    'symbol': {'fill': (143, 160, 181), 'stroke': None, 'width': 0},
}


def parse_path(d, steps=12):
    tokens = re.findall(r'([MCLHVQZ])|(-?\d+(?:\.\d+)?)', d)
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
            cur = (stream[i], stream[i + 1]); pts.append(cur); i += 2
        elif cmd == 'H':
            cur = (stream[i], cur[1]); pts.append(cur); i += 1
        elif cmd == 'V':
            cur = (cur[0], stream[i]); pts.append(cur); i += 1
        elif cmd == 'C':
            p0 = cur
            p1 = (stream[i], stream[i + 1])
            p2 = (stream[i + 2], stream[i + 3])
            p3 = (stream[i + 4], stream[i + 5])
            for k in range(1, steps + 1):
                t = k / steps
                mt = 1 - t
                pts.append((mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0]
                            + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0],
                            mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1]
                            + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1]))
            cur = p3; i += 6
        elif cmd == 'Q':
            p0 = cur
            p1 = (stream[i], stream[i + 1])
            p2 = (stream[i + 2], stream[i + 3])
            for k in range(1, steps + 1):
                t = k / steps
                mt = 1 - t
                pts.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0],
                            mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
            cur = p2; i += 4
        elif cmd == 'Z':
            pts.append(start); cur = start
        pos = i
    return pts


def classes_of(el):
    return set((el.get('class') or '').split())


def style_for(el):
    cls = classes_of(el)
    if 'pad-line' in cls:
        return STYLE['open'] if 'open' in cls else STYLE['line']
    if 'pad-symbol' in cls:
        return STYLE['symbol']
    if 'pad-key' in cls:
        if 'back' in cls:
            return STYLE['back']
        if 'solid' in cls:
            return STYLE['solid']
        if 'shoulder' in cls:
            return STYLE['shoulder']
        return STYLE['key']
    if 'dir-hit' in cls:
        return None      # 透明热区不画
    return STYLE['key']


PRESSED = set()


def main():
    global PRESSED
    if '--pressed' in sys.argv:
        value = sys.argv[sys.argv.index('--pressed') + 1]
        PRESSED = {int(v) for v in value.split(',') if v.strip()}
    html = io.open(HTML, encoding='utf-8').read()
    block = re.search(r'<svg id="padSvg".*?</svg>', html, re.S)
    if not block:
        raise SystemExit('没找到 padSvg')

    svg = block.group(0)
    # 去掉 role/aria 之类无关属性不影响解析
    root = ET.fromstring(svg)

    width, height = 1296, 1152
    image = Image.new('RGB', (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    try:
        font_big = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 48)
        font_small = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 35)
        font_tiny = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 26)
    except Exception:
        font_big = font_small = font_tiny = ImageFont.load_default()

    counter = {'shape': 0}

    def walk(element, offset, pressed_state=False):
        for child in element:
            tag = child.tag.replace(NS, '')
            pressed_here = pressed_state
            index_attr = child.get('data-index')
            if index_attr is not None:
                try:
                    pressed_here = int(index_attr) in PRESSED
                except ValueError:
                    pressed_here = pressed_state
            if tag == 'g':
                nxt = offset
                transform = child.get('transform') or ''
                found = re.search(r'translate\(\s*([-\d.]+)[ ,]+([-\d.]+)\s*\)',
                                  transform)
                if found:
                    nxt = (offset[0] + float(found.group(1)),
                           offset[1] + float(found.group(2)))
                walk(child, nxt, pressed_here)
                continue

            style = style_for(child)
            if pressed_here and 'pad-key' in classes_of(child):
                style = {'fill': (70, 211, 154),
                         'stroke': (46, 168, 122),
                         'width': style['width']}
            if style is None:
                continue

            if tag == 'path':
                pts = [(x + offset[0], y + offset[1])
                       for x, y in parse_path(child.get('d') or '')]
                if len(pts) < 3:
                    continue
                if style['fill']:
                    draw.polygon(pts, fill=style['fill'])
                if style['stroke']:
                    draw.line(pts + [pts[0]], fill=style['stroke'],
                              width=max(1, int(style['width'])))
                    draw.line(pts, fill=style['stroke'],
                              width=max(1, int(style['width'])))
                counter['shape'] += 1

            elif tag == 'circle':
                cx = float(child.get('cx')) + offset[0]
                cy = float(child.get('cy')) + offset[1]
                r = float(child.get('r'))
                box = [cx - r, cy - r, cx + r, cy + r]
                draw.ellipse(box, fill=style['fill'])
                if style['stroke']:
                    draw.ellipse(box, outline=style['stroke'],
                                 width=max(1, int(style['width'])))
                counter['shape'] += 1

            elif tag == 'rect':
                x = float(child.get('x')) + offset[0]
                y = float(child.get('y')) + offset[1]
                w = float(child.get('width'))
                h = float(child.get('height'))
                radius = float(child.get('rx') or 0)
                box = [x, y, x + w, y + h]
                draw.rounded_rectangle(box, radius=radius, fill=style['fill'],
                                       outline=style['stroke'],
                                       width=max(1, int(style['width'])))
                counter['shape'] += 1

            elif tag == 'polygon':
                nums = [float(v) for v in (child.get('points') or '').split()]
                pts = [(nums[i] + offset[0], nums[i + 1] + offset[1])
                       for i in range(0, len(nums) - 1, 2)]
                if style['fill']:
                    draw.polygon(pts, fill=style['fill'])
                counter['shape'] += 1

            elif tag == 'text':
                text = (child.text or '').strip()
                if not text:
                    continue
                x = float(child.get('x')) + offset[0]
                y = float(child.get('y')) + offset[1]
                cls = classes_of(child)
                use = font_small if ('small' in cls or len(text) > 1) else font_big
                if len(text) > 2:
                    use = font_tiny
                box = draw.textbbox((0, 0), text, font=use)
                draw.text((x - (box[2] - box[0]) / 2, y - (box[3] - box[1]) / 2),
                          text, fill=(201, 212, 224), font=use)

    walk(root, (0.0, 0.0))
    image.save(OUT)
    print('渲染完成: %s  元素数 %d' % (OUT, counter['shape']))


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    main()
