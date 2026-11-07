"""键盘模拟 —— 用硬件扫描码（Scancode）发送

为什么不用 pyautogui：
    pyautogui 走 SendInput 时只填虚拟键码（wVk），wScan 往往为 0。
    记事本这类普通程序认 VK，但很多游戏（尤其用 Raw Input 的）只处理
    带扫描码的按键事件，直接丢弃 wScan == 0 的合成事件 ——
    表现就是"记事本能打出字，游戏里没反应"。

这里改成 KEYEVENTF_SCANCODE，并填上真实扫描码，
让系统把它当成真实硬件按键处理。
"""

import ctypes
import ctypes.wintypes as wt

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

# 常用键的虚拟键码
VK = {
    'backspace': 0x08, 'tab': 0x09, 'enter': 0x0D, 'return': 0x0D,
    'shift': 0x10, 'ctrl': 0x11, 'control': 0x11, 'alt': 0x12,
    'pause': 0x13, 'capslock': 0x14, 'esc': 0x1B, 'escape': 0x1B,
    'space': 0x20, 'pgup': 0x21, 'pageup': 0x21, 'pgdn': 0x22,
    'pagedown': 0x22, 'end': 0x23, 'home': 0x24,
    'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
    'printscreen': 0x2C, 'insert': 0x2D, 'delete': 0x2E, 'del': 0x2E,
    'win': 0x5B, 'winleft': 0x5B, 'winright': 0x5C,
    'numlock': 0x90, 'scrolllock': 0x91,
    'shiftleft': 0xA0, 'shiftright': 0xA1,
    'ctrlleft': 0xA2, 'ctrlright': 0xA3,
    'altleft': 0xA4, 'altright': 0xA5,
    # F1-F24
    **{'f%d' % i: 0x6F + i for i in range(1, 25)},
    # 小键盘
    **{'num%d' % i: 0x60 + i for i in range(0, 10)},
}

# 需要扩展标志的键
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
               0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x90, 0xA3, 0xA5}

KEYBDINPUT_SIZE = 40          # 64 位下 INPUT 结构大小
ULONG_PTR = ctypes.c_ulonglong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wt.WORD), ('wScan', wt.WORD), ('dwFlags', wt.DWORD),
                ('time', wt.DWORD), ('dwExtraInfo', ULONG_PTR)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('padding', ctypes.c_ubyte * 24)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wt.DWORD), ('u', _INPUT_UNION)]


_user32 = ctypes.WinDLL('user32', use_last_error=True)
_user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wt.UINT
_user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
_user32.MapVirtualKeyW.restype = wt.UINT
_user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
_user32.VkKeyScanW.restype = ctypes.c_short


def resolve_key(name):
    """键名 -> (虚拟键码, 扫描码)"""
    if not name:
        return None
    name = name.strip()
    lowered = name.lower()
    if lowered in VK:
        vk = VK[lowered]
    elif len(name) == 1:
        # 单个字符：优先 ASCII 直接映射
        char = name.upper()
        if 'A' <= char <= 'Z' or '0' <= char <= '9':
            vk = ord(char)
        else:
            result = _user32.VkKeyScanW(name)
            if result == -1:
                return None
            vk = result & 0xFF
    else:
        return None
    scan = _user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    return vk, scan


def _send(vk, scan, flags):
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.u.ki = KEYBDINPUT(wVk=0, wScan=scan,
                           dwFlags=flags | KEYEVENTF_SCANCODE,
                           time=0, dwExtraInfo=0)
    return _user32.SendInput(1, ctypes.byref(item), KEYBDINPUT_SIZE) == 1


def key_down(name):
    resolved = resolve_key(name)
    if not resolved:
        return False
    vk, scan = resolved
    flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0
    return _send(vk, scan, flags)


def key_up(name):
    resolved = resolve_key(name)
    if not resolved:
        return False
    vk, scan = resolved
    flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VK else 0
    return _send(vk, scan, flags | KEYEVENTF_KEYUP)


def press(keys, hold=0.03):
    """按顺序按下再反序松开"""
    import time
    done = []
    for key in keys:
        if key_down(key):
            done.append(key)
        time.sleep(0.01)
    time.sleep(hold)
    for key in reversed(done):
        key_up(key)


def release_all():
    """释放常见修饰键"""
    released = []
    for name in ('ctrl', 'ctrlleft', 'ctrlright', 'alt', 'altleft',
                 'altright', 'shift', 'shiftleft', 'shiftright',
                 'win', 'winleft', 'winright'):
        if key_up(name):
            released.append(name)
    return released
