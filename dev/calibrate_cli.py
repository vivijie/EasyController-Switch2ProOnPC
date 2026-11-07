"""摇杆校准（命令行版）

把原程序 Switch2ProControllerWorkOnPC 的文本交互原样搬过来：
    1. 启动时读取本地校准数据，有数据就 3 秒倒计时（按键 = 重新校准）
    2. 第一步：中心点采样（100 个样本）
    3. 第二步：6 秒范围采样
    4. 结果对比 + 行程体检 + 自动保存

用法:
    python calibrate_cli.py            # 正常流程（有数据走倒计时）
    python calibrate_cli.py --force    # 强制重新校准
    python calibrate_cli.py --show     # 只看已保存的校准数据

注意：Web 服务运行时独占手柄，请先关闭服务再运行本工具。
"""

import json
import math
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

CALIBRATION_FILE = os.path.join(BASE, 'data', 'calibration.json')
CONFIG_FILE = os.path.join(BASE, 'data', 'sticklab_config.json')

RECALIBRATE_COUNTDOWN_SECONDS = 3.0
CENTER_SAMPLE_COUNT = 100
CENTER_SAMPLE_INTERVAL = 0.01
CENTER_PREPARE_SECONDS = 3.0
RANGE_PREPARE_SECONDS = 2.0
RANGE_CALIBRATION_SECONDS = 6.0

TRAVEL_WARN_RATIO = 0.75
TRAVEL_WARN_ABSOLUTE = 0.72
MIN_VALID_CALIBRATION_RANGE = 0.4

DEFAULT_LIMITS = {
    'left': {'min_x': -0.85, 'max_x': 0.85, 'min_y': -0.85, 'max_y': 0.85},
    'right': {'min_x': -0.85, 'max_x': 0.85, 'min_y': -0.85, 'max_y': 0.85},
}
DEFAULT_OFFSETS = {
    'left': {'x': 0.0, 'y': 0.0},
    'right': {'x': 0.0, 'y': 0.0},
}

LINE = '=' * 80
DASH = '-' * 80


# ==================== 手柄接入 ====================
def connect_controller():
    """连接物理手柄，返回 (ok, note)"""
    from sticklab import pygame_reader as hardware

    # 服务在跑就独占设备，先提醒
    try:
        import socket
        probe = socket.socket()
        probe.settimeout(0.3)
        busy = probe.connect_ex(('127.0.0.1', 8899)) == 0
        probe.close()
        if busy:
            print('⚠️  Web 服务正在运行（8899 端口被占用），手柄被它独占。')
            print('    请先关闭服务窗口，再运行本工具。')
            return False, '服务占用'
    except Exception:
        pass

    result = hardware.scan_and_connect()
    if not result.get('ok'):
        return False, result.get('message', '连接失败')

    return True, result.get('message', '手柄')


def read_stick_pair():
    """读取左右摇杆原始值，返回 (lx, ly, rx, ry) 或 None"""
    from sticklab import pygame_reader as hardware

    try:
        snapshot = hardware.status()
        latest = snapshot.get('latest')
        if latest is None:
            return None
        left = latest.get('left_raw') or [0.0, 0.0]
        right = latest.get('right_raw') or [0.0, 0.0]
        return float(left[0]), float(left[1]), float(right[0]), float(right[1])
    except Exception:
        return None


def pressed_button_name():
    """返回当前被按下的按键名，没有按下返回 None"""
    from sticklab import pygame_reader as hardware

    try:
        snapshot = hardware.status()
        latest = snapshot.get('latest')
        if latest is None:
            return None
        buttons = latest.get('buttons') or []
    except Exception:
        return None

    try:
        from sticklab.pygame_reader import PHYS_NAMES
    except Exception:
        PHYS_NAMES = {}

    for index, value in enumerate(buttons):
        if value:
            return PHYS_NAMES.get(index, '按钮%d' % index)
    return None


# ==================== 文本辅助 ====================
def print_travel_warning(weak_directions, source_hint=True):
    """打印行程体检结论"""
    if not weak_directions:
        print('\n✅ 行程体检: 四个方向行程均衡，无需重新校准')
        return
    print('\n' + LINE)
    print('⚠️  行程体检：有方向明显偏小，建议重新校准'.center(80))
    print(LINE)
    for stick_name, direction, weakest_value, strongest_value, travels in weak_directions:
        print('%s: %s 只量到 %.3f，最强方向是 %.3f'
              % (stick_name, direction, weakest_value, strongest_value))
        print('    各方向: ' + '  '.join('%s=%.3f' % (k, v) for k, v in travels.items()))
    print(DASH)
    print('这会带来两个后果:')
    print('  1. 偏差方向推不到满偏（例如只到 0.96 而不是 1.0）')
    print('  2. 统一缩放系数被拉大，其他方向的外部死区随之变大')
    if source_hint:
        print('这份是上次保存的数据，重新校准即可改善：')
        print('  启动后按任意手柄按键，或删掉上面的 json 文件')
    else:
        print('建议: 重新校准时把摇杆【贴着边缘慢慢转 3~5 圈】，覆盖每个方向到极限')
    print(LINE)


def analyze_stick_travel(limits):
    """检查四个方向行程是否均衡"""
    weak = []
    for stick in ('left', 'right'):
        stick_name = '左摇杆' if stick == 'left' else '右摇杆'
        limit = limits[stick]
        travels = {
            '向右': abs(limit['max_x']),
            '向左': abs(limit['min_x']),
            '向上': abs(limit['max_y']),
            '向下': abs(limit['min_y']),
        }
        weakest_direction = min(travels, key=travels.get)
        weakest_value = travels[weakest_direction]
        strongest_value = max(travels.values())
        if (strongest_value > 0.01 and
                (weakest_value < strongest_value * TRAVEL_WARN_RATIO or
                 weakest_value < TRAVEL_WARN_ABSOLUTE)):
            weak.append((stick_name, weakest_direction, weakest_value,
                         strongest_value, travels))
    return weak


# ==================== 存取 ====================
def _load_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except Exception:
        return None


def load_saved_calibration():
    """读取本地校准数据，没有或损坏时返回 None"""
    data = _load_json(CALIBRATION_FILE)
    if not data:
        config = _load_json(CONFIG_FILE) or {}
        data = config.get('calibration')
    if not data:
        return None

    try:
        limits = {'left': {}, 'right': {}}
        for stick in ('left', 'right'):
            for boundary in ('min_x', 'max_x', 'min_y', 'max_y'):
                limits[stick][boundary] = float(data['limits'][stick][boundary])
            if (abs(limits[stick]['min_x']) < 0.01 and abs(limits[stick]['max_x']) < 0.01) or \
               (abs(limits[stick]['min_y']) < 0.01 and abs(limits[stick]['max_y']) < 0.01):
                print('⚠️  本地校准数据范围异常（将重新校准）')
                return None

        offsets = {
            'left': {'x': float(data['offsets']['left']['x']),
                     'y': float(data['offsets']['left']['y'])},
            'right': {'x': float(data['offsets']['right']['x']),
                      'y': float(data['offsets']['right']['y'])},
        }
    except (KeyError, TypeError, ValueError) as error:
        print('⚠️  本地校准数据格式不正确（将重新校准）: %s' % error)
        return None

    return limits, offsets, data.get('saved_at', '未知时间'), data.get('joystick_name', '')


def save_calibration(limits, offsets, joystick_name=''):
    """写入 data/calibration.json，同时同步进应用配置"""
    payload = {
        'version': 1,
        'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'joystick_name': joystick_name,
        'limits': {
            stick: {boundary: float(limits[stick][boundary])
                    for boundary in ('min_x', 'max_x', 'min_y', 'max_y')}
            for stick in ('left', 'right')
        },
        'offsets': {
            stick: {'x': float(offsets[stick]['x']),
                    'y': float(offsets[stick]['y'])}
            for stick in ('left', 'right')
        },
    }

    try:
        os.makedirs(os.path.dirname(CALIBRATION_FILE), exist_ok=True)
        with open(CALIBRATION_FILE, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print('💾 校准数据已保存到本地: %s' % CALIBRATION_FILE)
    except Exception as error:
        print('⚠️  校准数据保存失败: %s' % error)
        return False

    # 同步进应用配置，让网页端直接生效
    try:
        config = _load_json(CONFIG_FILE) or {}
        config['calibration'] = payload
        with open(CONFIG_FILE, 'w', encoding='utf-8') as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
        print('   同时已同步到: %s' % CONFIG_FILE)
    except Exception as error:
        print('⚠️  同步应用配置失败: %s' % error)

    return True


def print_saved_calibration(limits, offsets, saved_at):
    print('\n' + LINE)
    print('📂 已读取本地校准数据（无需重新校准）'.center(80))
    print(LINE)
    print('保存时间: %s' % saved_at)
    for stick in ('left', 'right'):
        stick_name = '左摇杆' if stick == 'left' else '右摇杆'
        limit = limits[stick]
        offset = offsets[stick]
        print('%s: X[%+.3f↔%+.3f] Y[%+.3f↔%+.3f] 中心偏移(X=%+.4f, Y=%+.4f)'
              % (stick_name, limit['min_x'], limit['max_x'],
                 limit['min_y'], limit['max_y'], offset['x'], offset['y']))
    print(LINE)


# ==================== 倒计时 ====================
def wait_and_check_recalibrate_request():
    """3 秒倒计时：期间按下任意手柄按键则返回 True"""
    total = RECALIBRATE_COUNTDOWN_SECONDS
    print('\n本地校准数据: 已找到 ✅')
    print('⏳ %.0f秒倒计时内按下手柄任意按键 = 重新校准' % total)
    print('   不按键则直接使用本地校准数据启动（无需触碰手柄）')

    start_time = time.perf_counter()
    pressed = None
    last_shown = None

    while True:
        remaining = total - (time.perf_counter() - start_time)
        if remaining <= 0:
            break

        pressed = pressed_button_name()
        if pressed is not None:
            break

        display_value = math.ceil(remaining * 10) / 10.0
        if display_value != last_shown:
            print('\r倒计时: %.1f 秒 ...' % display_value, end='', flush=True)
            last_shown = display_value

        time.sleep(0.02)

    if pressed is not None:
        print('\r✅ 检测到按键 [%s]，进入重新校准流程' % pressed + ' ' * 20)
        return True

    print('\r⏱️  倒计时结束，未检测到按键，使用本地校准数据启动' + ' ' * 20)
    return False


# ==================== 核心：校准 ====================
def calibrate_joystick_range_and_center():
    """修正的摇杆校准：先校准中心点，再计算校准后的实际范围
    返回值: (校准后范围, 中心偏移, 本次校准是否有效)
    """
    print('\n' + LINE)
    print('🎮 修正版摇杆范围与中心偏移自动识别'.center(80))
    print(LINE)
    print('第一步: 请将左右摇杆保持在中心位置，不要触碰')
    print('第二步: 10秒后开始范围校准，请同时转动左右摇杆3圈，覆盖所有方向')
    print(LINE)

    center_offsets = {
        'left': {'x': 0.0, 'y': 0.0},
        'right': {'x': 0.0, 'y': 0.0},
    }

    # ---------- 第一步: 检测中心偏移 ----------
    print('\n📊 正在检测摇杆中心偏移...')
    print('请确保左右摇杆都保持在中心位置，不要触碰')
    time.sleep(CENTER_PREPARE_SECONDS)

    sums = {'left': [0.0, 0.0], 'right': [0.0, 0.0]}
    collected = 0

    for i in range(CENTER_SAMPLE_COUNT):
        values = read_stick_pair()
        if values is not None:
            lx, ly, rx, ry = values
            sums['left'][0] += lx
            sums['left'][1] += ly
            sums['right'][0] += rx
            sums['right'][1] += ry
            collected += 1

        progress = (i + 1) / CENTER_SAMPLE_COUNT * 100
        print('\r采集中心样本: %.0f%%' % progress, end='', flush=True)
        time.sleep(CENTER_SAMPLE_INTERVAL)

    print()
    if collected == 0:
        print('❌ 没有读到任何数据，请确认手柄已连接')
        return ({s: DEFAULT_LIMITS[s].copy() for s in ('left', 'right')},
                center_offsets, False)

    for stick in ('left', 'right'):
        center_offsets[stick]['x'] = sums[stick][0] / collected
        center_offsets[stick]['y'] = sums[stick][1] / collected

    print('\n✅ 中心偏移检测完成!')
    print('左摇杆中心偏移: X=%+.4f, Y=%+.4f'
          % (center_offsets['left']['x'], center_offsets['left']['y']))
    print('右摇杆中心偏移: X=%+.4f, Y=%+.4f'
          % (center_offsets['right']['x'], center_offsets['right']['y']))

    # ---------- 第二步: 检测物理范围 ----------
    print('\n🔄 正在校准摇杆物理范围...')
    print('请同时转动左右摇杆3圈，覆盖所有方向')
    time.sleep(RANGE_PREPARE_SECONDS)

    first = read_stick_pair()
    if first is None:
        print('初始化读取失败，使用默认范围')
        return ({s: DEFAULT_LIMITS[s].copy() for s in ('left', 'right')},
                center_offsets, False)

    raw = {}
    calibrated = {}
    lx, ly, rx, ry = first
    raw['left'] = {'min_x': lx, 'max_x': lx, 'min_y': ly, 'max_y': ly}
    raw['right'] = {'min_x': rx, 'max_x': rx, 'min_y': ry, 'max_y': ry}

    clx, cly = lx - center_offsets['left']['x'], ly - center_offsets['left']['y']
    crx, cry = rx - center_offsets['right']['x'], ry - center_offsets['right']['y']
    calibrated['left'] = {'min_x': clx, 'max_x': clx, 'min_y': cly, 'max_y': cly}
    calibrated['right'] = {'min_x': crx, 'max_x': crx, 'min_y': cry, 'max_y': cry}

    start_time = time.time()
    while time.time() - start_time < RANGE_CALIBRATION_SECONDS:
        values = read_stick_pair()
        if values is None:
            continue
        lx, ly, rx, ry = values

        for stick, x, y in (('left', lx, ly), ('right', rx, ry)):
            raw[stick]['min_x'] = min(raw[stick]['min_x'], x)
            raw[stick]['max_x'] = max(raw[stick]['max_x'], x)
            raw[stick]['min_y'] = min(raw[stick]['min_y'], y)
            raw[stick]['max_y'] = max(raw[stick]['max_y'], y)

            cx = x - center_offsets[stick]['x']
            cy = y - center_offsets[stick]['y']
            calibrated[stick]['min_x'] = min(calibrated[stick]['min_x'], cx)
            calibrated[stick]['max_x'] = max(calibrated[stick]['max_x'], cx)
            calibrated[stick]['min_y'] = min(calibrated[stick]['min_y'], cy)
            calibrated[stick]['max_y'] = max(calibrated[stick]['max_y'], cy)

        remaining_time = RANGE_CALIBRATION_SECONDS - (time.time() - start_time)
        print('\r校准中... %.1f秒 左:[X%+.3f↔%+.3f Y%+.3f↔%+.3f] 右:[X%+.3f↔%+.3f Y%+.3f↔%+.3f]'
              % (remaining_time,
                 calibrated['left']['min_x'], calibrated['left']['max_x'],
                 calibrated['left']['min_y'], calibrated['left']['max_y'],
                 calibrated['right']['min_x'], calibrated['right']['max_x'],
                 calibrated['right']['min_y'], calibrated['right']['max_y']),
              end='', flush=True)
        time.sleep(0.01)

    print()

    # ---------- 有效性校验 ----------
    calibration_valid = True
    for stick in ('left', 'right'):
        limits = calibrated[stick]
        if (abs(limits['min_x']) < 0.01 and abs(limits['max_x']) < 0.01 and
                abs(limits['min_y']) < 0.01 and abs(limits['max_y']) < 0.01):
            print('\n⚠️  %s摇杆未检测到有效移动，使用默认范围' % stick)
            calibrated[stick] = DEFAULT_LIMITS[stick].copy()
            calibration_valid = False
        else:
            if limits['min_x'] > limits['max_x']:
                limits['min_x'], limits['max_x'] = limits['max_x'], limits['min_x']
            if limits['min_y'] > limits['max_y']:
                limits['min_y'], limits['max_y'] = limits['max_y'], limits['min_y']

            range_x = limits['max_x'] - limits['min_x']
            range_y = limits['max_y'] - limits['min_y']
            if range_x < MIN_VALID_CALIBRATION_RANGE or range_y < MIN_VALID_CALIBRATION_RANGE:
                print('\n⚠️  %s摇杆行程过小(X=%.3f, Y=%.3f)，本次校准结果不保存'
                      % (stick, range_x, range_y))
                calibrated[stick] = DEFAULT_LIMITS[stick].copy()
                calibration_valid = False

    print('\n✅ 摇杆校准完成!')

    # ---------- 结果对比 ----------
    print('\n' + LINE)
    print('🎯 校准结果对比'.center(80))
    print(LINE)
    for stick in ('left', 'right'):
        stick_name = '左摇杆' if stick == 'left' else '右摇杆'
        raw_limits = raw[stick]
        limits = calibrated[stick]
        offset = center_offsets[stick]
        print('%s:' % stick_name)
        print('  原始范围:   X[%+.3f ↔ %+.3f] Y[%+.3f ↔ %+.3f]'
              % (raw_limits['min_x'], raw_limits['max_x'],
                 raw_limits['min_y'], raw_limits['max_y']))
        print('  校准后范围: X[%+.3f ↔ %+.3f] Y[%+.3f ↔ %+.3f]'
              % (limits['min_x'], limits['max_x'],
                 limits['min_y'], limits['max_y']))
        print('  中心偏移:   X=%+.4f, Y=%+.4f' % (offset['x'], offset['y']))
    print(LINE)

    if calibration_valid:
        print_travel_warning(analyze_stick_travel(calibrated), source_hint=False)
    else:
        print('\n⚠️  本次校准无效（摇杆没有转到），行程体检跳过，'
              '已在上面提示需要重新校准')

    return calibrated, center_offsets, calibration_valid


# ==================== 调度 ====================
def resolve_calibration(force=False):
    """校准调度：有本地数据走倒计时判断，无本地数据直接校准"""
    if force:
        print('\n（--force 强制重新校准）')
        limits, offsets, valid = calibrate_joystick_range_and_center()
        if valid:
            save_calibration(limits, offsets, _current_name())
        return limits, offsets

    saved = load_saved_calibration()

    if saved is None:
        print('\n未找到本地校准数据，进入首次校准流程...')
        limits, offsets, valid = calibrate_joystick_range_and_center()
        if valid:
            save_calibration(limits, offsets, _current_name())
        else:
            print('⚠️  本次校准数据无效，不写入本地文件（下次启动仍会进入校准）')
        return limits, offsets

    saved_limits, saved_offsets, saved_at, saved_name = saved
    print_saved_calibration(saved_limits, saved_offsets, saved_at)
    print_travel_warning(analyze_stick_travel(saved_limits), source_hint=True)

    if wait_and_check_recalibrate_request():
        limits, offsets, valid = calibrate_joystick_range_and_center()
        if valid:
            save_calibration(limits, offsets, _current_name())
            return limits, offsets
        print('⚠️  本次校准数据无效，继续使用原有本地校准数据')
        return saved_limits, saved_offsets

    return saved_limits, saved_offsets


def _current_name():
    try:
        from sticklab import pygame_reader as hardware
        return hardware.reader.joystick_name or ''
    except Exception:
        return ''


def main():
    force = '--force' in sys.argv
    show_only = '--show' in sys.argv

    print(LINE)
    print('EasyController 摇杆校准（命令行）'.center(80))
    print(LINE)

    if show_only:
        saved = load_saved_calibration()
        if saved is None:
            print('没有找到本地校准数据。')
            return 1
        print_saved_calibration(*saved[:3])
        return 0

    ok, note = connect_controller()
    if not ok:
        print('❌ 连接手柄失败: %s' % note)
        return 1
    print('✅ ' + note)

    try:
        resolve_calibration(force=force)
    finally:
        try:
            from sticklab import pygame_reader as hardware
            hardware.disconnect()
        except Exception:
            pass

    print('\n完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
