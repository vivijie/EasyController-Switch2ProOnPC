r"""虚拟 XInput 手柄输出层 —— 移植自用户原程序

原程序的做法（本文件完全照搬）：
    virtual_gamepad = VX360Gamepad()
    virtual_gamepad.left_joystick_float(left['x'], left['y'])
    virtual_gamepad.right_joystick_float(right['x'], right['y'])
    virtual_gamepad.left_trigger_float(trigger_states['left'])
    virtual_gamepad.right_trigger_float(trigger_states['right'])
    virtual_gamepad.update()

按键映射表、特殊按键（键盘）、扳机按键索引都与原程序一致：
    ZR = 按钮 5 → right_trigger
    ZL = 按钮 13 → left_trigger
    特殊按键：16→alt+f1, 17→t, 20→alt+f10
"""

import threading
import time

try:
    from vgamepad import VX360Gamepad, XUSB_BUTTON
    VGAMEPAD_AVAILABLE = True
    VGAMEPAD_ERROR = ''
except Exception as _error:            # 没装或驱动不可用
    VX360Gamepad = None
    XUSB_BUTTON = None
    VGAMEPAD_AVAILABLE = False
    VGAMEPAD_ERROR = '%s: %s' % (type(_error).__name__, _error)

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except Exception:
    PYAUTOGUI_AVAILABLE = False

# 优先用扫描码发送（游戏才认），pyautogui 作兜底
try:
    from . import keys as scancode_keys
    SCANCODE_AVAILABLE = True
except Exception:
    try:
        import keys as scancode_keys
        SCANCODE_AVAILABLE = True
    except Exception:
        scancode_keys = None
        SCANCODE_AVAILABLE = False


def _key_down(name):
    if SCANCODE_AVAILABLE:
        return scancode_keys.key_down(name)
    if PYAUTOGUI_AVAILABLE:
        try:
            pyautogui.keyDown(name)
            return True
        except Exception:
            return False
    return False


def _key_up(name):
    if SCANCODE_AVAILABLE:
        return scancode_keys.key_up(name)
    if PYAUTOGUI_AVAILABLE:
        try:
            pyautogui.keyUp(name)
            return True
        except Exception:
            return False
    return False


def keyboard_backend():
    if SCANCODE_AVAILABLE:
        return 'scancode'
    if PYAUTOGUI_AVAILABLE:
        return 'pyautogui'
    return 'none'


# ==================== 按键映射（原程序原样）====================
PHYSICAL_TO_NAME = {
    0: "A", 1: "B", 2: "X", 3: "Y",
    4: "R", 5: "ZR", 6: "Plus", 7: "RStick",
    8: "Down", 9: "Right", 10: "Left", 11: "Up",
    12: "L", 13: "ZL", 14: "Min", 15: "LStick",
    16: "Home", 17: "Capture",
    18: "BR", 19: "BL",
    20: "C",
}

BUTTON_COUNT = 21


def _name_to_xbox():
    """延迟构建，避免 vgamepad 缺失时导入失败"""
    if not VGAMEPAD_AVAILABLE:
        return {}
    return {
        "BR": XUSB_BUTTON.XUSB_GAMEPAD_X,
        "BL": XUSB_BUTTON.XUSB_GAMEPAD_A,
        "A": XUSB_BUTTON.XUSB_GAMEPAD_A,
        "B": XUSB_BUTTON.XUSB_GAMEPAD_B,
        "X": XUSB_BUTTON.XUSB_GAMEPAD_X,
        "Y": XUSB_BUTTON.XUSB_GAMEPAD_Y,
        "L": XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        "R": XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        "Min": XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        "Plus": XUSB_BUTTON.XUSB_GAMEPAD_START,
        "Home": XUSB_BUTTON.XUSB_GAMEPAD_START,
        "Capture": XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        "LStick": XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        "RStick": XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
        "Up": XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        "Down": XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        "Left": XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        "Right": XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        "ZL": "left_trigger",
        "ZR": "right_trigger",
    }


NAME_TO_XBOX = _name_to_xbox()

# 特殊按键：物理键 → 键盘组合（原程序原样）
# 可配置按键映射：物理索引 -> {'xinput': 目标, 'keys': 键盘组合}
# 目标可以是 A/B/X/Y/LB/RB/START/BACK/LS/RS/DPAD_* / LT / RT / None
DEFAULT_BUTTON_MAP = {
    0: {'xinput': 'A', 'keys': []},
    1: {'xinput': 'B', 'keys': []},
    2: {'xinput': 'X', 'keys': []},
    3: {'xinput': 'Y', 'keys': []},
    4: {'xinput': 'RB', 'keys': []},
    5: {'xinput': 'RT', 'keys': []},
    6: {'xinput': 'START', 'keys': []},
    7: {'xinput': 'RS', 'keys': []},
    8: {'xinput': 'DPAD_DOWN', 'keys': []},
    9: {'xinput': 'DPAD_RIGHT', 'keys': []},
    10: {'xinput': 'DPAD_LEFT', 'keys': []},
    11: {'xinput': 'DPAD_UP', 'keys': []},
    12: {'xinput': 'LB', 'keys': []},
    13: {'xinput': 'LT', 'keys': []},
    14: {'xinput': 'BACK', 'keys': []},
    15: {'xinput': 'LS', 'keys': []},
    16: {'xinput': 'START', 'keys': ['alt', 'f10']},
    17: {'xinput': 'BACK', 'keys': ['t']},
    18: {'xinput': 'X', 'keys': []},
    19: {'xinput': 'A', 'keys': []},
    20: {'xinput': 'None', 'keys': []},
}

# 可选目标（给界面下拉用）
XINPUT_TARGETS = ['None', 'A', 'B', 'X', 'Y', 'LB', 'RB', 'LT', 'RT',
                  'START', 'BACK', 'LS', 'RS',
                  'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT']

BUTTON_MAP = {index: {'xinput': value['xinput'],
                      'keys': list(value['keys'])}
              for index, value in DEFAULT_BUTTON_MAP.items()}


def _xbox_button(target):
    """目标名 -> XUSB_BUTTON（触发器和 None 返回 None）"""
    if not VGAMEPAD_AVAILABLE or not target:
        return None
    mapping = {
        'A': XUSB_BUTTON.XUSB_GAMEPAD_A,
        'B': XUSB_BUTTON.XUSB_GAMEPAD_B,
        'X': XUSB_BUTTON.XUSB_GAMEPAD_X,
        'Y': XUSB_BUTTON.XUSB_GAMEPAD_Y,
        'LB': XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        'RB': XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        'START': XUSB_BUTTON.XUSB_GAMEPAD_START,
        'BACK': XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        'LS': XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        'RS': XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
        'DPAD_UP': XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        'DPAD_DOWN': XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        'DPAD_LEFT': XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        'DPAD_RIGHT': XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
    }
    return mapping.get(target)


def release_all_keys():
    """强制释放所有常见修饰键（防卡死）"""
    if SCANCODE_AVAILABLE:
        released = scancode_keys.release_all()
        return True, '已释放: %s' % ', '.join(released[:4])
    if not PYAUTOGUI_AVAILABLE:
        return False, '没有可用的键盘后端'
    released = []
    for key in ('ctrl', 'ctrlleft', 'ctrlright', 'alt', 'altleft', 'altright',
                'shift', 'shiftleft', 'shiftright', 'win', 'winleft', 'winright'):
        try:
            pyautogui.keyUp(key)
            released.append(key)
        except Exception:
            pass
    return True, '已释放: %s' % ', '.join(released[:4])


def get_button_map():
    return {index: {'xinput': value['xinput'], 'keys': list(value['keys'])}
            for index, value in BUTTON_MAP.items()}


def set_button_map(payload):
    """更新映射，返回 (是否改变, 说明)"""
    changed = []
    if not isinstance(payload, dict):
        return changed, '数据格式不对'
    for key, value in payload.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if not 0 <= index < BUTTON_COUNT or not isinstance(value, dict):
            continue
        current = BUTTON_MAP.setdefault(index, {'xinput': 'None', 'keys': []})
        target = value.get('xinput')
        if isinstance(target, str) and target in XINPUT_TARGETS:
            if current['xinput'] != target:
                current['xinput'] = target
                changed.append('%s→%s' % (PHYSICAL_TO_NAME.get(index, index), target))
        keys = value.get('keys')
        if isinstance(keys, list):
            cleaned = [str(k).strip().lower() for k in keys if str(k).strip()]
            if current['keys'] != cleaned:
                current['keys'] = cleaned
                changed.append('%s键盘' % PHYSICAL_TO_NAME.get(index, index))
    return changed, ''


SPECIAL_BUTTONS = {
    16: {'keys': ['alt', 'f1'], 'description': 'ScreenCapture'},
    17: {'keys': ['t'], 'description': 'Capture 按钮映射为键盘 T 键'},
    20: {'keys': ['alt', 'f10'], 'description': 'Home 按钮映射为 Alt+F10 组合键'},
}


class VirtualXInput(object):
    """虚拟 Xbox 360 手柄，接口设计为可由主循环高频写入"""

    def __init__(self):
        self.lock = threading.Lock()
        self.pad = None
        self.running = False
        self.thread = None
        self.error = ''
        self.enabled = False
        self._state = {
            'left': (0.0, 0.0),
            'right': (0.0, 0.0),
            'trigger_left': 0.0,
            'trigger_right': 0.0,
            'buttons': [False] * BUTTON_COUNT,
        }
        self._pressed = set()
        self._special_down = set()
        self.frames = 0
        self.rate = 0.0
        self._rate_mark = time.perf_counter()
        self._rate_frames = 0
        self.update_rate = 1000

    # ---------- 生命周期 ----------
    def start(self, update_rate=1000):
        if not VGAMEPAD_AVAILABLE:
            self.error = 'vgamepad 不可用: %s' % VGAMEPAD_ERROR
            return False, self.error
        if self.running:
            self.enabled = True
            return True, '已在运行'
        try:
            self.pad = VX360Gamepad()
        except Exception as error:
            self.error = '创建虚拟手柄失败: %s: %s' % (type(error).__name__, error)
            return False, self.error
        self.update_rate = max(60, min(2000, int(update_rate)))
        self.error = ''
        self.frames = 0
        self._pressed = set()
        self._special_down = set()
        self._rate_mark = time.perf_counter()
        self._rate_frames = 0
        self.running = True
        self.enabled = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        note = '虚拟 XInput 手柄已启动'
        if not PYAUTOGUI_AVAILABLE:
            note += '（pyautogui 未安装，特殊按键的键盘模拟不可用）'
        return True, note

    def stop(self):
        self.running = False
        self.enabled = False
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.thread = None
        # 释放所有仍按着的键盘键，避免 Ctrl/Alt 卡死
        for index in list(self._special_down):
            entry = BUTTON_MAP.get(index) or {}
            for key in reversed(entry.get('keys') or []):
                _key_up(key)
        self._special_down.clear()
        pad = self.pad
        if pad is not None:
            try:
                pad.reset()
                pad.update()
            except Exception:
                pass
        self._pressed = set()
        self._special_down = set()
        self.pad = None
        return True, '已停止虚拟手柄'

    # ---------- 写入状态 ----------
    def set_state(self, left, right, trigger_left, trigger_right, buttons):
        with self.lock:
            self._state['left'] = (float(left[0]), float(left[1]))
            self._state['right'] = (float(right[0]), float(right[1]))
            self._state['trigger_left'] = max(0.0, min(1.0, float(trigger_left)))
            self._state['trigger_right'] = max(0.0, min(1.0, float(trigger_right)))
            if buttons is not None:
                count = min(len(buttons), BUTTON_COUNT)
                for index in range(count):
                    self._state['buttons'][index] = bool(buttons[index])
            # 比 21 长的位置按未按下处理（避免越界残留）
            for index in range(count, BUTTON_COUNT):
                self._state['buttons'][index] = False

    # ---------- 输出循环 ----------
    def _apply_special(self, buttons):
        """按键 → 键盘（按下 keyDown，松开按反序 keyUp）"""
        if not SCANCODE_AVAILABLE and not PYAUTOGUI_AVAILABLE:
            return
        for index in range(min(len(buttons), BUTTON_COUNT)):
            entry = BUTTON_MAP.get(index) or {}
            keys = entry.get('keys') or []
            if not keys:
                if index in self._special_down:
                    self._special_down.discard(index)
                continue
            pressed = buttons[index]
            if pressed and index not in self._special_down:
                self._special_down.add(index)
                for key in keys:
                    _key_down(key)
            elif not pressed and index in self._special_down:
                self._special_down.discard(index)
                for key in reversed(keys):
                    _key_up(key)

    def _desired_buttons(self, buttons):
        """把物理按键集合换算成需要按下的 XBOX 按键集合（读 BUTTON_MAP）"""
        desired = set()
        for index, pressed in enumerate(buttons):
            if not pressed:
                continue
            entry = BUTTON_MAP.get(index)
            if not entry:
                continue
            target = entry.get('xinput')
            if target in (None, 'None', 'LT', 'RT'):
                continue                    # 扳机在 set_state 里单独算
            button = _xbox_button(target)
            if button is not None:
                desired.add(button)
        return desired

    def _loop(self):
        while self.running:
            with self.lock:
                left = self._state['left']
                right = self._state['right']
                trigger_left = self._state['trigger_left']
                trigger_right = self._state['trigger_right']
                buttons = list(self._state['buttons'])

            pad = self.pad
            if pad is None:
                break
            try:
                # 特殊按键（键盘）
                self._apply_special(buttons)

                # 普通按键：先算目标集合，再 diff
                desired = self._desired_buttons(buttons)
                for button in desired - self._pressed:
                    try:
                        pad.press_button(button)
                    except Exception:
                        pass
                for button in self._pressed - desired:
                    try:
                        pad.release_button(button)
                    except Exception:
                        pass
                self._pressed = desired

                # 摇杆
                pad.left_joystick_float(left[0], left[1])
                pad.right_joystick_float(right[0], right[1])
                # 扳机
                pad.left_trigger_float(trigger_left)
                pad.right_trigger_float(trigger_right)
                pad.update()

                self.frames += 1
                self._rate_frames += 1
                now = time.perf_counter()
                if now - self._rate_mark >= 1.0:
                    self.rate = self._rate_frames / (now - self._rate_mark)
                    self._rate_frames = 0
                    self._rate_mark = now
            except Exception as error:
                self.error = '输出异常: %s: %s' % (type(error).__name__, error)
                time.sleep(0.05)
            time.sleep(1.0 / self.update_rate)

    # ---------- 状态 ----------
    def snapshot(self):
        return {
            'available': VGAMEPAD_AVAILABLE,
            'available_error': VGAMEPAD_ERROR,
            'keyboard_available': (SCANCODE_AVAILABLE or PYAUTOGUI_AVAILABLE),
            'keyboard_backend': keyboard_backend(),
            'running': self.running,
            'enabled': self.enabled,
            'frames': self.frames,
            'rate': round(self.rate, 1),
            'error': self.error,
            'pressed': sorted(name.name for name in self._pressed
                              if hasattr(name, 'name')),
            'button_map': get_button_map(),
            'xinput_targets': list(XINPUT_TARGETS),
            'mapping': {
                str(index): {
                    'name': PHYSICAL_TO_NAME.get(index, ''),
                    'target': (NAME_TO_XBOX.get(PHYSICAL_TO_NAME.get(index, ''))
                               .name
                               if hasattr(NAME_TO_XBOX.get(
                                   PHYSICAL_TO_NAME.get(index, '')), 'name')
                               else str(NAME_TO_XBOX.get(
                                   PHYSICAL_TO_NAME.get(index, '')) or '')),
                    'special': SPECIAL_BUTTONS.get(index, {}).get('keys'),
                }
                for index in range(BUTTON_COUNT)
            },
        }


# 全局单例
output = VirtualXInput()
