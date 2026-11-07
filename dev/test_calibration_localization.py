"""校准本地化机制自动化测试（自包含：不需要 pygame / vgamepad / 真实手柄）

原理:
    1. 注入假的 pygame / vgamepad / pyautogui 模块（因此不需要安装任何第三方库）
    2. 用"模拟时钟"驱动时间：假 sleep 直接把模拟时间往前推，并把
       time.sleep / time.time / time.perf_counter 一起换掉，
       这样校准需要的 3 秒 / 6 秒在真实时间里几乎瞬间完成
    3. 用假手柄模拟"中心静止 + 转动 3 圈"的完整校准过程
    4. 逐个场景运行主程序 Switch2ProControllerWorkOnPC.txt，检查校准文件的读写行为

运行方式:
    python .\\test_calibration_localization.py        # 全部 4 个场景
    python .\\test_calibration_localization.py 1      # 只跑场景 1

结束后会在同目录生成 calibration_test_report.json（详细结果）。

注意（踩过的坑）:
    主程序内部有 `try: ... except Exception` 包住的 sleep，SystemExit 会被一起吞掉，
    导致测试挂死。所以退出时机不能设在那些 sleep 上：
    这里用 EXIT_AFTER_SETUP 次 sleep 之后结束，此时已经进入主循环末尾那个
    “裸露”的 time.sleep(0.001)，那里没有 try 包裹，SystemExit 能正常抛出。
"""

import json
import os
import sys
import time
import types

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_FILE = os.path.join(BASE_DIR, 'Switch2ProControllerWorkOnPC.txt')
CALIBRATION_FILE = os.path.join(BASE_DIR, 'Switch2ProControllerWorkOnPC_calibration.json')
REPORT_FILE = os.path.join(BASE_DIR, 'calibration_test_report.json')
LOG_FILE = os.path.join(BASE_DIR, 'calibration_test_progress.log')

MOVEMENT_START = 5.0        # 校准流程第 5 秒后开始转动摇杆
MOVEMENT_AMPLITUDE = 0.9
# 再过这么多次 sleep 就结束本次运行。取这个较大值是为了确保退出发生在主循环末尾
# 那个“裸露”的 time.sleep(0.001) 处：程序内部用 try/except Exception 包住的 sleep
# 会把 SystemExit 一起吞掉，导致测试挂住。
EXIT_AFTER_SETUP = 5000

CENTER_VALUE = {'left': {'x': 0.02, 'y': -0.03}, 'right': {'x': 0.012, 'y': -0.018}}


# ==================== 时间模拟 ====================
# 主程序里的 time.sleep / time.time / time.perf_counter 全部改由模拟时钟驱动，
# 这样校准需要的 3 秒 / 6 秒在真实时间里瞬间完成，而程序的判断逻辑保持不变。
_REAL_TIME = time.time
_REAL_PERF_COUNTER = time.perf_counter
_REAL_SLEEP = time.sleep
_EPOCH_BASE = _REAL_TIME()
_PERF_BASE = _REAL_PERF_COUNTER()


class SimClock(object):
    """模拟时钟：假 sleep 推进它，程序读到的也是它"""

    def __init__(self):
        self.now = 0.0

    def reset(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds


SIM = SimClock()


def install_sim_time():
    time.sleep = _sim_sleep
    time.time = lambda: _EPOCH_BASE + SIM.now
    time.perf_counter = lambda: SIM.now


def restore_real_time():
    time.sleep = _REAL_SLEEP
    time.time = _REAL_TIME
    time.perf_counter = _REAL_PERF_COUNTER


# 启动阶段收尾用：再过这么多次 sleep 就结束本次运行
_EXIT_STATE = {'after_sleeps': None, 'count': 0}


def _sim_sleep(seconds):
    SIM.advance(seconds)
    if _EXIT_STATE['after_sleeps'] is not None:
        _EXIT_STATE['count'] += 1
        if _EXIT_STATE['count'] >= _EXIT_STATE['after_sleeps']:
            raise SystemExit(0)


# ==================== 假 pygame ====================
class _FakeJoystick(object):
    """模拟手柄：前 5 秒静止（测中心偏移），之后正弦转动（测行程范围）"""

    button_window = None

    def __init__(self, index):
        self.index = index

    def init(self):
        return None

    def get_name(self):
        return 'FAKE Switch2 Pro Controller'

    def get_numbuttons(self):
        return 21

    def get_numaxes(self):
        return 4

    def get_button(self, index):
        window = _FakeJoystick.button_window
        if not window:
            return 0
        start, end = window
        if start <= SIM.now <= end and index in (0, 1, 2, 3, 16, 18, 19, 20):
            return 1
        return 0

    def get_axis(self, index):
        if index == 0:
            base = CENTER_VALUE['left']['x']
        elif index == 1:
            base = CENTER_VALUE['left']['y']
        elif index == 2:
            base = CENTER_VALUE['right']['x']
        else:
            base = CENTER_VALUE['right']['y']

        if SIM.now < MOVEMENT_START:
            return base
        # 正弦往复运动，正负方向都覆盖到
        return base + MOVEMENT_AMPLITUDE * ((SIM.now * 3.0 + index) % 2.0 - 1.0)


def make_fake_pygame():
    pygame = types.ModuleType('pygame')
    pygame.QUIT = 256
    pygame.KEYDOWN = 768
    pygame.KEYUP = 769
    pygame.K_T = 116
    pygame.K_F10 = 121
    pygame.K_ALT = 308

    pygame.init = lambda: None
    pygame.quit = lambda: None

    joystick_module = types.ModuleType('pygame.joystick')
    joystick_module.init = lambda: None
    joystick_module.get_count = lambda: 1
    joystick_module.Joystick = _FakeJoystick

    event_module = types.ModuleType('pygame.event')
    event_module.pump = lambda: None
    event_module.get = lambda *args, **kwargs: []

    pygame.joystick = joystick_module
    pygame.event = event_module
    pygame.time = types.ModuleType('pygame.time')
    return pygame


# ==================== 假 vgamepad ====================
class _FakeXUSBButton(object):
    XUSB_GAMEPAD_A = 'A'
    XUSB_GAMEPAD_B = 'B'
    XUSB_GAMEPAD_X = 'X'
    XUSB_GAMEPAD_Y = 'Y'
    XUSB_GAMEPAD_LEFT_SHOULDER = 'LB'
    XUSB_GAMEPAD_RIGHT_SHOULDER = 'RB'
    XUSB_GAMEPAD_BACK = 'BACK'
    XUSB_GAMEPAD_START = 'START'
    XUSB_GAMEPAD_LEFT_THUMB = 'LS'
    XUSB_GAMEPAD_RIGHT_THUMB = 'RS'
    XUSB_GAMEPAD_DPAD_UP = 'UP'
    XUSB_GAMEPAD_DPAD_DOWN = 'DOWN'
    XUSB_GAMEPAD_DPAD_LEFT = 'LEFT'
    XUSB_GAMEPAD_DPAD_RIGHT = 'RIGHT'


class _FakeVX360Gamepad(object):
    def left_joystick_float(self, *args):
        pass

    def right_joystick_float(self, *args):
        pass

    def left_trigger_float(self, *args):
        pass

    def right_trigger_float(self, *args):
        pass

    def press_button(self, *args):
        pass

    def release_button(self, *args):
        pass

    def reset(self, *args):
        pass

    def update(self, *args):
        pass


def make_fake_vgamepad():
    module = types.ModuleType('vgamepad')
    module.XUSB_BUTTON = _FakeXUSBButton
    module.VX360Gamepad = _FakeVX360Gamepad
    return module


def make_fake_pyautogui():
    """假 pyautogui：避免真实 pyautogui 在导入时卡住（本机实测它会卡在导入阶段）"""
    module = types.ModuleType('pyautogui')
    module.keyDown = lambda *args, **kwargs: None
    module.keyUp = lambda *args, **kwargs: None
    module.press = lambda *args, **kwargs: None
    module.typewrite = lambda *args, **kwargs: None
    return module


# ==================== 进度与看门狗 ====================
def progress(message):
    """把进度同时写到控制台和日志文件，便于定位卡住的位置"""
    line = f'[{time.strftime("%H:%M:%S")}] {message}'
    try:
        print(line, flush=True)
    except Exception:
        pass
    with open(LOG_FILE, 'a', encoding='utf-8') as file_object:
        file_object.write(line + '\n')


def install_stall_watchdog(timeout=25.0):
    """若主线程长时间停在同一条源代码行，把堆栈写进日志，便于定位卡死位置"""
    import threading
    import traceback

    main_thread = threading.main_thread()
    state = {'last': None, 'same_since': time.perf_counter(), 'dumped': False}

    def watch():
        while True:
            time.sleep(1.0)
            frame = sys._current_frames().get(main_thread.ident)
            if frame is None:
                continue
            info = (frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)
            if info != state['last']:
                state['last'] = info
                state['same_since'] = time.perf_counter()
                continue
            if state['dumped'] or (time.perf_counter() - state['same_since']) < timeout:
                continue
            state['dumped'] = True
            lines = [f'{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}']
            walker = frame.f_back
            depth = 0
            while walker is not None and depth < 12:
                lines.append(f'  <- {walker.f_code.co_filename}:{walker.f_lineno} '
                             f'in {walker.f_code.co_name}')
                walker = walker.f_back
                depth += 1
            lines.append('---- 全部线程堆栈 ----')
            for thread_id, thread_frame in sys._current_frames().items():
                lines.append(f'线程 {thread_id}:')
                lines.append(''.join(traceback.format_stack(thread_frame, limit=4)))
            progress('!! 检测到疑似卡死，堆栈如下:\n' + '\n'.join(lines))

    threading.Thread(target=watch, daemon=True).start()


# ==================== 运行器 ====================
class _FakeStdout(object):
    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def reconfigure(self, *args, **kwargs):
        pass


def run_main(button_press_delay=None, exit_after_sleeps=None):
    """在受控环境中运行一次主程序
    exit_after_sleeps: 再过多少次 sleep 就结束运行
    返回 (是否正常跑完, 说明, 主程序输出文本)
    """
    SIM.reset()
    _FakeJoystick.button_window = None
    if button_press_delay is not None:
        _FakeJoystick.button_window = (SIM.now + button_press_delay,
                                       SIM.now + button_press_delay + 5.0)

    fake_pygame = make_fake_pygame()
    sys.modules['pygame'] = fake_pygame
    sys.modules['vgamepad'] = make_fake_vgamepad()
    sys.modules['pyautogui'] = make_fake_pyautogui()

    fake_stdout = _FakeStdout()
    real_stdout = sys.stdout
    _EXIT_STATE['after_sleeps'] = exit_after_sleeps
    _EXIT_STATE['count'] = 0

    # 捕获主程序的 quit 调用（仅用于观察）
    real_quit = fake_pygame.quit

    def fake_quit():
        real_quit()

    fake_pygame.quit = fake_quit

    namespace = {'__name__': '__main__', '__file__': MAIN_FILE}
    with open(MAIN_FILE, 'r', encoding='utf-8') as file_object:
        source = file_object.read()

    install_sim_time()
    sys.stdout = fake_stdout
    try:
        exec(compile(source, MAIN_FILE, 'exec'), namespace)
        ok, note = True, '主程序正常结束'
    except SystemExit:
        ok, note = True, '主循环运行若干帧后按预期结束'
    except BaseException as error:
        ok, note = False, f'{type(error).__name__}: {error}'
    finally:
        restore_real_time()
        sys.stdout = real_stdout
        fake_pygame.quit = real_quit

    return ok, note, ''.join(fake_stdout.chunks)


def read_calibration_file():
    if not os.path.exists(CALIBRATION_FILE):
        return None
    with open(CALIBRATION_FILE, 'r', encoding='utf-8') as file_object:
        return json.load(file_object)


def remove_calibration_file():
    if os.path.exists(CALIBRATION_FILE):
        os.remove(CALIBRATION_FILE)


def seed_calibration_file():
    payload = {
        'version': 1,
        'saved_at': '2024-01-01 00:00:00',
        'joystick_name': 'SEEDED',
        'limits': {
            'left': {'min_x': -0.85, 'max_x': 0.85, 'min_y': -0.80, 'max_y': 0.80},
            'right': {'min_x': -0.82, 'max_x': 0.82, 'min_y': -0.78, 'max_y': 0.78},
        },
        'offsets': {
            'left': {'x': 0.021, 'y': -0.032},
            'right': {'x': 0.011, 'y': -0.013},
        },
    }
    with open(CALIBRATION_FILE, 'w', encoding='utf-8') as file_object:
        json.dump(payload, file_object, ensure_ascii=False, indent=2)
    return payload


def values_close(actual, expected, tolerance):
    for key in expected:
        if abs(float(actual[key]) - expected[key]) > tolerance:
            return False
    return True


def main(only=None):
    results = []
    install_stall_watchdog()

    # ---------- 场景1: 无本地数据 -> 自动校准并写入本地文件 ----------
    if only in (None, 1):
        progress('场景1 开始: 删除本地校准文件，运行主程序')
        remove_calibration_file()
        ok, note, output = run_main(exit_after_sleeps=EXIT_AFTER_SETUP)
        progress(f'场景1 主程序返回: ok={ok} note={note}')
        progress('场景1 主程序输出:\n' + output)
        data = read_calibration_file()
        limits_ok = bool(data) and all(
            values_close(data['limits'][stick], expected, 0.05)
            for stick, expected in (
                ('left', {'min_x': -0.92, 'max_x': 0.92, 'min_y': -0.93, 'max_y': 0.87}),
                ('right', {'min_x': -0.92, 'max_x': 0.92, 'min_y': -0.92, 'max_y': 0.88}),
            ))
        offsets_ok = bool(data) and all(
            values_close({'x': data['offsets'][stick]['x'], 'y': data['offsets'][stick]['y']},
                         expected, 0.01)
            for stick, expected in (
                ('left', {'x': 0.02, 'y': -0.03}),
                ('right', {'x': 0.012, 'y': -0.018}),
            ))
        results.append({
            'scenario': '场景1 无本地校准数据 -> 自动进入校准并写入本地文件',
            'passed': bool(ok and data and limits_ok and offsets_ok),
            'detail': f'运行: {"成功" if ok else "失败"}（{note}）; 生成文件: {bool(data)}; '
                      f'范围正确: {limits_ok}; 中心偏移正确: {offsets_ok}',
            'evidence': {
                'wrote_file': bool(data),
                'saved_limits': (data or {}).get('limits'),
                'saved_offsets': (data or {}).get('offsets'),
            },
        })

    # ---------- 场景2: 有本地数据 + 倒计时内按键 -> 重新校准并覆盖 ----------
    if only in (None, 2):
        progress('场景2 开始: 先清空文件跑一次主程序，再写入种子数据')
        if os.path.exists(CALIBRATION_FILE):
            os.remove(CALIBRATION_FILE)
        run_main(exit_after_sleeps=EXIT_AFTER_SETUP)   # 先生成一份真实校准数据
        progress('场景2 首次主程序结束，写入种子数据')
        seeded = seed_calibration_file()
        progress('场景2 带按键运行主程序（倒计时内按键）')
        ok, note, output = run_main(button_press_delay=0.05, exit_after_sleeps=EXIT_AFTER_SETUP)
        progress(f'场景2 主程序返回: ok={ok} note={note}')
        data = read_calibration_file() or {}
        overwritten = data.get('saved_at') != seeded['saved_at'] and data.get('joystick_name') != 'SEEDED'
        results.append({
            'scenario': '场景2 有本地数据 + 倒计时内按下按键 -> 重新校准并覆盖本地文件',
            'passed': bool(ok and overwritten and '检测到按键' in output),
            'detail': f'运行: {"成功" if ok else "失败"}（{note}）; 检测到按键: {"检测到按键" in output}; '
                      f'文件被重新写入: {overwritten}; 新时间戳: {data.get("saved_at")}',
            'evidence': {'saved_at_after': data.get('saved_at'), 'seeded_saved_at': seeded['saved_at']},
        })

    # ---------- 场景3: 有本地数据 + 不按键 -> 沿用本地数据，文件不重写 ----------
    if only in (None, 3):
        progress('场景3 开始: 写入种子数据后不按键运行主程序')
        seeded = seed_calibration_file()
        ok, note, output = run_main(exit_after_sleeps=EXIT_AFTER_SETUP)
        progress(f'场景3 主程序返回: ok={ok} note={note}')
        data = read_calibration_file() or {}
        unchanged = data.get('saved_at') == seeded['saved_at'] and data.get('joystick_name') == 'SEEDED'
        results.append({
            'scenario': '场景3 有本地数据 + 3秒倒计时内不按键 -> 直接沿用本地数据（文件不重写）',
            'passed': bool(ok and unchanged and '倒计时结束' in output and '校准中' not in output),
            'detail': f'运行: {"成功" if ok else "失败"}（{note}）; 文件保持原样: {unchanged}; '
                      f'未进入校准流程: {"校准中" not in output}; '
                      f'使用本地数据提示: {"倒计时结束，未检测到按键" in output}',
            'evidence': {'file_unchanged': unchanged, 'saved_at': data.get('saved_at')},
        })

    # ---------- 场景4: 本地数据损坏 -> 自动重新校准并修复文件 ----------
    if only in (None, 4):
        progress('场景4 开始: 写入损坏的 JSON 后运行主程序')
        with open(CALIBRATION_FILE, 'w', encoding='utf-8') as file_object:
            file_object.write('{ 这不是合法的 JSON')
        ok, note, output = run_main(exit_after_sleeps=EXIT_AFTER_SETUP)
        progress(f'场景4 主程序返回: ok={ok} note={note}')
        # 这里故意读成文本：损坏的文件如果被成功修复，它现在应该是合法 JSON
        with open(CALIBRATION_FILE, 'r', encoding='utf-8') as file_object:
            raw_text = file_object.read()
        repaired = False
        try:
            repaired = bool(json.loads(raw_text))
        except Exception:
            repaired = False
        results.append({
            'scenario': '场景4 本地数据损坏 -> 自动重新校准并修复文件',
            'passed': bool(ok and repaired and '读取失败' in output and '校准中' in output),
            'detail': f'运行: {"成功" if ok else "失败"}（{note}）; 提示读取失败: '
                      f'{"读取失败" in output}; 进入校准: {"校准中" in output}; '
                      f'文件修复为合法 JSON: {repaired}',
            'evidence': {'repaired': repaired},
        })

    passed_count = sum(1 for item in results if item['passed'])
    all_passed = passed_count == len(results)

    with open(REPORT_FILE, 'w', encoding='utf-8') as file_object:
        json.dump({
            'main_file': MAIN_FILE,
            'time_scale': 'simulated',
            'passed': passed_count,
            'total': len(results),
            'results': results,
        }, file_object, ensure_ascii=False, indent=2)

    progress('=' * 100)
    progress('校准本地化机制测试结果')
    progress('=' * 100)
    for item in results:
        progress(f"[{'通过' if item['passed'] else '失败'}] {item['scenario']}")
        progress(f"        {item['detail']}")
    progress('=' * 100)
    progress(f'总体结果: {passed_count}/{len(results)} ' + ('全部通过' if all_passed else '存在失败项'))
    progress(f'详细报告: {REPORT_FILE}')
    progress('VERDICT: ' + ('PASS' if all_passed else 'FAIL'))
    return 0 if all_passed else 1


if __name__ == '__main__':
    # 控制台按 UTF-8 输出，避免 GBK 终端打不出 ✅ 之类的字符
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    # 用法: python test_calibration_localization.py [1|2|3|4]  只跑指定场景
    only_arg = None
    if len(sys.argv) > 1 and sys.argv[1].strip() in ('1', '2', '3', '4'):
        only_arg = int(sys.argv[1].strip())
    with open(LOG_FILE, 'w', encoding='utf-8') as log_file:
        log_file.write('')
    sys.exit(main(only_arg))
