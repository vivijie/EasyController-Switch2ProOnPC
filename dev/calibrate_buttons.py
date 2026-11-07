"""按键位域标定工具（物理手柄）

目的：确认真实 0x09 报告里，每个按键对应哪个字节的哪一位。
之前沿用 SDL 的偏移导致大量"幻影按键"，必须实测。

流程：对每个按键采集两轮——松开若干帧、按住若干帧，比较差异位。

用法:
    python .\calibrate_buttons.py                # 标定全部 21 个键
    python .\calibrate_buttons.py --quick        # 只标 A/B/X/Y/ZL/ZR
    python .\calibrate_buttons.py --key A        # 只标一个键
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticklab import hardware as H

# 要标定的按键（按 Switch2 Pro 物理布局命名）
BUTTON_TARGETS = [
    ('A', 'A 键'), ('B', 'B 键'), ('X', 'X 键'), ('Y', 'Y 键'),
    ('R', 'R 键（右肩）'), ('L', 'L 键（左肩）'),
    ('ZR', 'ZR 扳机'), ('ZL', 'ZL 扳机'),
    ('Plus', 'Plus (+)'), ('Min', 'Minus (-)'),
    ('LStick', '左摇杆按下'), ('RStick', '右摇杆按下'),
    ('Home', 'Home 键'), ('Capture', 'Capture 键'), ('C', 'C 键'),
    ('BL', '左背键'), ('BR', '右背键'),
    ('Up', '十字键 上'), ('Down', '十字键 下'),
    ('Left', '十字键 左'), ('Right', '十字键 右'),
]
QUICK = ['A', 'B', 'X', 'Y', 'ZL', 'ZR']


def open_device():
    """激活并打开读取句柄（先激活、再打开，避免 Access denied）"""
    devices = H.enumerate_devices()
    if not devices:
        print('❌ 没检测到手柄，请用 USB 线连接')
        return None
    ok, note = H.activate()
    print(f'激活: {"成功" if ok else "失败"} — {note}')
    time.sleep(0.15)
    chosen = None
    for info in devices:
        if info.get('usage_page') == 0x0001 and info.get('usage') == 0x0005:
            chosen = info
            break
    if chosen is None:
        chosen = devices[0]
    try:
        import hid
        device = hid.device()
        device.open_path(chosen['path'])
        device.set_nonblocking(0)
        return device
    except Exception as error:
        print(f'❌ 打开 HID 失败: {error}')
        return None


def collect(device, seconds):
    frames = []
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        try:
            raw = device.read(64, 120)
        except Exception:
            raw = None
        if raw:
            frames.append(bytes(raw))
    return frames


def diff_bits(released, pressed):
    """找出按住时必然与松开不同的位（只在前 12 字节找，后面是传感器）"""
    hits = []
    if not released or not pressed:
        return hits
    for byte_index in range(12):
        for bit in range(8):
            mask = 1 << bit
            released_values = {bool(f[byte_index] & mask) for f in released}
            pressed_values = {bool(f[byte_index] & mask) for f in pressed}
            if len(released_values) != 1 or len(pressed_values) != 1:
                continue                    # 有抖动，不是按键
            if released_values == pressed_values:
                continue
            hits.append((byte_index, mask, not released_values.pop()))
    return hits


def main():
    parser = argparse.ArgumentParser(description='物理手柄按键位域标定')
    parser.add_argument('--quick', action='store_true', help='只标 6 个常用键')
    parser.add_argument('--key', default=None, help='只标一个键（如 A）')
    parser.add_argument('--seconds', type=float, default=1.2, help='每轮采集秒数')
    args = parser.parse_args()

    if args.key:
        targets = [(name, label) for name, label in BUTTON_TARGETS
                   if name.lower() == args.key.lower()]
        if not targets:
            print(f'未知按键: {args.key}')
            print('可选: ' + ', '.join(name for name, _ in BUTTON_TARGETS))
            return 1
    elif args.quick:
        targets = [(name, label) for name, label in BUTTON_TARGETS if name in QUICK]
    else:
        targets = BUTTON_TARGETS

    device = open_device()
    if device is None:
        return 1

    print('\n开始标定。每个键两轮：先【松开】，再【按住】。')
    print('（提示出现后请立刻照做，每轮约 %.1f 秒）\n' % args.seconds)

    results = {}
    try:
        for name, label in targets:
            input(f'>>> 准备好标定 [{label}]，按回车开始…')
            print(f'    第 1 轮：请【不要碰】{label}', flush=True)
            for countdown in range(2, 0, -1):
                print(f'      {countdown}…', end='', flush=True)
                time.sleep(1.0)
            released = collect(device, args.seconds)
            print(f' 采集 {len(released)} 帧', flush=True)

            print(f'    第 2 轮：请【按住 {label}】不要松开', flush=True)
            for countdown in range(2, 0, -1):
                print(f'      {countdown}…', end='', flush=True)
                time.sleep(1.0)
            pressed = collect(device, args.seconds)
            print(f' 采集 {len(pressed)} 帧', flush=True)

            hits = diff_bits(released, pressed)
            results[name] = hits
            if hits:
                text = ', '.join(f'byte[{b}]:0x{m:02X}{"(低有效)" if low else ""}'
                                 for b, m, low in hits)
                print(f'    ✅ [{label}] -> {text}\n', flush=True)
            else:
                print(f'    ⚠️ [{label}] 没找到差异位（可能没按到）\n', flush=True)
    except KeyboardInterrupt:
        print('\n\n已中断，输出已完成部分的结果')
    finally:
        try:
            device.close()
        except Exception:
            pass

    print('\n' + '=' * 72)
    print('标定结果（可直接粘贴给开发者）')
    print('=' * 72)
    for name, hits in results.items():
        if hits:
            text = ', '.join(f'byte[{b}]:0x{m:02X}{"(低有效)" if low else ""}'
                             for b, m, low in hits)
            print(f'  {name:<10} -> {text}')
        else:
            print(f'  {name:<10} -> (未标定)')
    print('=' * 72)

    try:
        if sys.stdin and sys.stdin.isatty():
            input('\n按回车退出...')
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
