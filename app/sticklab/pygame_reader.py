r"""物理手柄读取层 —— 使用 pygame（与用户原程序完全一致的读取方式）

为什么换成 pygame
----------------
之前用 hidapi + 手写 12-bit 解析，出了两个真实 bug：
  1. 按键位域读到了 byte[6..8]，而那正是左摇杆数据 → 大量"幻影按键"
  2. 左摇杆走手写解析，方向/位序不可靠

用户原程序用的是 pygame 的 get_axis()/get_button()，SDL 已经处理好了
字节布局、位序、方向。所以这里直接沿用，只保留"原生激活"这一件
pygame 做不到的事（Switch2 Pro 必须先发 17 条厂商命令才会出数据）。

顺序约束（实测）
-------------
    激活（libusb claim 接口1）→ 然后 pygame 打开手柄
反过来会 Access denied，因为 HID 句柄已占用。

对外接口与原 hardware.DeviceReader 保持一致，webapp 无需改动调用方式。
"""

import os
import sys
import threading
import time

# 复用原 hardware 模块里已验证的激活逻辑
try:
    from . import hardware as _native
except ImportError:
    import hardware as _native

VID = 0x057E
PID = 0x2069
BUTTON_COUNT = 21

# 与用户原程序一致：优先按名字挑 Switch 手柄
PREFERRED_JOYSTICK_NAMES = ('If_Hid', 'Switch')
# 必须排除的：我们自己创建的虚拟 XInput 手柄（只有 11 个按键，抓到会越界）
EXCLUDED_JOYSTICK_NAMES = ('XBOX', 'Xbox', 'xbox', 'XInput', 'vJoy', 'VGAMEPAD')
MAX_BUTTONS = 25


def enumerate_devices():
    """枚举 HID 接口（用于界面显示，不含 bytes 字段）"""
    if _native is None:
        return []
    return [{k: v for k, v in info.items() if k != 'path_bytes'}
            for info in _native.enumerate_devices()]


class PygameReader(object):
    """用 pygame 读取手柄，接口与 hardware.DeviceReader 兼容"""

    def __init__(self):
        self.stage = ''
        self.lock = threading.Lock()
        # 串行化所有 SDL 调用：Flask 线程 rescan 与读取线程不能同时碰 pygame
        self.sdl_lock = threading.RLock()
        self._last_alive_check = 0.0
        self.connected = False
        self.running = False
        self.thread = None
        self.joystick = None
        self.joystick_index = 0
        self.joystick_name = ''
        self.button_limit = 0
        self.num_axes = 0
        self.latest = None
        self.latest_time = 0.0
        self.frames = 0
        self.error = ''
        self.rate_hz = 0.0
        self._rate_mark = time.perf_counter()
        self._rate_frames = 0
        self._pygame = None
        self._activation_note = ''
        self.axis_span = {}

    # ---------- 连接 ----------
    def connect(self):
        with self.sdl_lock:
            return self._connect_locked()

    def _connect_locked(self):
        self.disconnect()

        # 拔插数据线后 SDL 会保留失效的设备列表，进程内不会重新枚举，
        # 所以每次重连都先把 SDL 完全关掉，激活后再重新 init。
        self._sdl_shutdown()

        self.stage = '正在检查设备…'
        devices = enumerate_devices()
        if not devices:
            return {'ok': False, 'message': '未检测到手柄（请用 USB 线连接 Switch2 Pro）'}

        # 1) 先确认数据流：已激活就跳过，避免多余的 claim
        needs_activation = True
        try:
            frames = _native._probe_hid_stream(0.6) if hasattr(
                _native, '_probe_hid_stream') else 0
            if frames > 10:
                needs_activation = False
        except Exception:
            pass

        activation_note = ''
        self.stage = '正在激活手柄…' if needs_activation else '数据流已在运行'
        if needs_activation:
            ok, note = _native.activate(verbose=True)
            activation_note = note
            self._activation_note = note
            # 激活会触发设备复位，等它重新枚举完再让 SDL 去枚举
            time.sleep(1.0)
        else:
            self._activation_note = '数据流已在运行，无需激活'

        # 2) pygame 打开手柄（必须在激活之后）
        self.stage = '正在重新枚举 SDL…'
        try:
            pygame = self._sdl_start()
        except Exception as error:
            return {'ok': False,
                    'message': f'pygame 初始化失败: {type(error).__name__}: {error}',
                    'activation': activation_note}

        joystick, chosen_index = self._pick_joystick(pygame)

        # 拔插后 SDL 靠事件泵更新设备列表：持续泵事件最多等 3 秒
        if joystick is None:
            for _ in range(30):
                try:
                    pygame.event.pump()
                except Exception:
                    pass
                time.sleep(0.1)
                joystick, chosen_index = self._pick_joystick(pygame)
                if joystick is not None:
                    break

        # 还是不行就重建一次 SDL 再试
        if joystick is None:
            self._sdl_shutdown()
            time.sleep(0.4)
            try:
                pygame = self._sdl_start()
                for _ in range(10):
                    pygame.event.pump()
                    time.sleep(0.1)
                joystick, chosen_index = self._pick_joystick(pygame)
            except Exception:
                joystick = None

        if joystick is None:
            try:
                seen = pygame.joystick.get_count()
            except Exception:
                seen = 0
            message = ('无法初始化任何手柄' if seen
                       else 'pygame 没有看到手柄（可能上一轮句柄未释放，请重试）')
            return {'ok': False, 'message': message,
                    'activation': activation_note}
            return {'ok': False, 'message': '无法初始化任何手柄',
                    'activation': activation_note}

        try:
            self.joystick = joystick
            self.joystick_index = chosen_index
            self.joystick_name = joystick.get_name()
            self.button_limit = min(joystick.get_numbuttons(), MAX_BUTTONS)
            self.num_axes = joystick.get_numaxes()
        except Exception as error:
            return {'ok': False, 'message': f'读取手柄信息失败: {error}',
                    'activation': activation_note}

        if self.num_axes < 4:
            return {'ok': False,
                    'message': f'手柄轴数量不足（{self.num_axes} 个，需要 4 个）',
                    'activation': activation_note}

        with self.lock:
            self.connected = True
            self.error = ''
            self.frames = 0
            self.latest = None
            self.latest_time = time.perf_counter()

        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

        self.stage = '等待手柄数据…'
        # 等待读取线程产出数据
        # 这里必须暂时释放 SDL 锁，否则读取线程拿不到锁、永远读不到数据
        # 激活后设备要过一会儿才开始推流，所以轮询最多等 3 秒
        got_frames = False
        for _attempt in range(6):
            self.sdl_lock.release()
            try:
                time.sleep(0.5)
                got_frames = self.frames > 0
            finally:
                self.sdl_lock.acquire()
            if got_frames:
                break

        if not got_frames:
            self.disconnect()
            return {'ok': False,
                    'message': ('已打开手柄但读不到数据'
                                + (f'（{activation_note}）' if activation_note else '')
                                + ' 请确认手柄未休眠'),
                    'activation': activation_note}

        self.stage = ''
        return {
            'ok': True,
            'message': f'已连接：{self.joystick_name}（pygame 读取）',
            'activation': activation_note,
            'device': {
                'joystick_name': self.joystick_name,
                'index': self.joystick_index,
                'axes': self.num_axes,
                'buttons': self.button_limit,
                'rate': round(self.rate_hz, 1),
            },
            'devices': devices,
        }

    def disconnect(self):
        with self.sdl_lock:
            self._disconnect_locked()

    def _disconnect_locked(self):
        self.running = False
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.thread = None
        # 注意：必须先取出对象再置空，否则永远不会调用 joystick.quit()
        joystick = self.joystick
        with self.lock:
            self.connected = False
            self.joystick = None
        if joystick is not None:
            try:
                joystick.quit()
            except Exception:
                pass

    def _sdl_shutdown(self):
        """彻底关闭 SDL（下次 init 会重新枚举设备）"""
        try:
            import pygame as _pg
            self._pygame = _pg
            try:
                _pg.joystick.quit()
            except Exception:
                pass
            try:
                _pg.quit()
            except Exception:
                pass
        except Exception:
            pass

    def _sdl_start(self):
        """重新初始化 SDL 并返回模块对象"""
        import pygame
        self._pygame = pygame
        pygame.init()
        pygame.joystick.init()
        return pygame

    def _pick_joystick(self, pygame):
        """在 SDL 列表里挑物理手柄，返回 (joystick, index)"""
        try:
            count = pygame.joystick.get_count()
        except Exception:
            return None, 0
        chosen = None
        chosen_index = 0
        fallback = None
        for index in range(count):
            try:
                candidate = pygame.joystick.Joystick(index)
                candidate.init()
                name = candidate.get_name()
            except Exception:
                continue
            if any(keyword in name for keyword in EXCLUDED_JOYSTICK_NAMES):
                continue
            if any(keyword in name for keyword in PREFERRED_JOYSTICK_NAMES):
                return candidate, index
            if fallback is None:
                fallback = candidate
                chosen_index = index
        if fallback is not None:
            return fallback, chosen_index
        return None, 0

    def _device_alive(self, pygame):
        """设备是否还在 SDL 的列表里（拔线后这里会返回 False）"""
        try:
            count = pygame.joystick.get_count()
        except Exception:
            return False
        for index in range(count):
            try:
                candidate = pygame.joystick.Joystick(index)
                if candidate.get_name() == self.joystick_name:
                    return True
            except Exception:
                continue
        return False

    # ---------- 读取循环 ----------
    def _loop(self):
        pygame = self._pygame
        while self.running:
            joystick = self.joystick
            if joystick is None:
                break
            # 与 connect/disconnect 互斥，避免 SDL 被并发重初始化
            if not self.sdl_lock.acquire(timeout=0.5):
                continue
            try:
                try:
                    pygame.event.pump()
                    left_x = joystick.get_axis(0)
                    left_y = joystick.get_axis(1)
                    right_x = joystick.get_axis(2)
                    right_y = joystick.get_axis(3)

                    # 采集全部轴：用于诊断"左摇杆到底在哪个轴上"
                    all_axes = []
                    for axis_index in range(self.num_axes):
                        try:
                            all_axes.append(float(joystick.get_axis(axis_index)))
                        except Exception:
                            all_axes.append(0.0)

                    buttons = [False] * BUTTON_COUNT
                    limit = self.button_limit
                    for index in range(min(limit, BUTTON_COUNT)):
                        try:
                            buttons[index] = bool(joystick.get_button(index))
                        except Exception:
                            buttons[index] = False
                except Exception as error:
                    self.error = f'{type(error).__name__}: {error}'
                    time.sleep(0.05)
                    continue

                now = time.perf_counter()
                with self.lock:
                    self.latest = {
                        'left_raw': (float(left_x), float(left_y)),
                        'right_raw': (float(right_x), float(right_y)),
                        'buttons': buttons,
                        'axes': all_axes,
                    }
                    self.latest_time = now
                    self.frames += 1
                    self._rate_frames += 1

                    for axis_index, value in enumerate(all_axes):
                        span = self.axis_span.get(axis_index)
                        if span is None:
                            self.axis_span[axis_index] = [value, value]
                        else:
                            if value < span[0]:
                                span[0] = value
                            if value > span[1]:
                                span[1] = value
                if now - self._rate_mark >= 1.0:
                    self.rate_hz = self._rate_frames / (now - self._rate_mark)
                    self._rate_frames = 0
                    self._rate_mark = now

                # 每 0.5 秒确认设备是否还在 SDL 列表里
                # （拔线后 SDL 不会抛异常，仍返回旧值，只能靠列表判断）
                if now - self._last_alive_check >= 0.5:
                    self._last_alive_check = now
                    if not self._device_alive(pygame):
                        # 主动清理连接状态：
                        # 1) 关掉 joystick 对象（释放 HID 句柄）
                        # 2) 关掉 SDL（否则它会一直缓存失效的设备列表，
                        #    重新插线后在同一个进程里再也枚举不到手柄）
                        self.error = '手柄已断开（数据源消失）'
                        try:
                            joystick.quit()
                        except Exception:
                            pass
                        with self.lock:
                            self.connected = False
                            self.joystick = None
                        self.running = False
                        self._sdl_shutdown()
                        break
            finally:
                self.sdl_lock.release()
            time.sleep(0.001)


    def list_joysticks(self):
        """按 SDL 枚举顺序列出所有手柄（这就是游戏看到的顺序）"""
        pygame = self._pygame
        if pygame is None:
            return []
        try:
            count = pygame.joystick.get_count()
        except Exception:
            return []
        result = []
        with self.sdl_lock:
            for index in range(count):
                opened_here = False
                joystick = None
                try:
                    joystick = pygame.joystick.Joystick(index)
                    if not joystick.get_init():
                        joystick.init()
                        opened_here = True
                    name = joystick.get_name()
                    result.append({
                        'position': index + 1,
                        'name': name,
                        'axes': joystick.get_numaxes(),
                        'buttons': joystick.get_numbuttons(),
                        'virtual': ('Xbox' in name or 'XInput' in name
                                    or 'VGAMEPAD' in name),
                        'selected': (self.joystick is not None
                                     and name == self.joystick_name),
                    })
                except Exception:
                    continue
                finally:
                    # 这里只是探测，必须释放：否则每次轮询都会泄漏一个
                    # HID 句柄，最终导致重连时 SDL 打不开设备
                    if opened_here and joystick is not None:
                        try:
                            joystick.quit()
                        except Exception:
                            pass
        return result

    # ---------- 对外 ----------
    def snapshot(self):
        with self.lock:
            spans = {}
            for axis_index, span in self.axis_span.items():
                spans[axis_index] = [round(span[0], 4), round(span[1], 4),
                                     round(span[1] - span[0], 4)]
            return {
            'stage': self.stage,
                'connected': self.connected,
                'frames': self.frames,
                'rate': round(self.rate_hz, 1),
                'error': self.error,
                'latest': self.latest,
                'age': round(time.perf_counter() - self.latest_time, 3)
                if self.latest_time else None,
                'joystick_name': self.joystick_name,
                'button_limit': self.button_limit,
                'num_axes': self.num_axes,
                'axis_span': spans,
            }


# 全局单例
reader = PygameReader()


def scan_and_connect():
    return reader.connect()


def status():
    return reader.snapshot()


def disconnect():
    reader.disconnect()


def activate(verbose=False):
    return _native.activate(verbose=verbose)


def main():
    """命令行自测：连接并打印实时轴/按键"""
    print('连接中…')
    result = reader.connect()
    print(f'ok={result["ok"]}  {result["message"]}')
    if result.get('activation'):
        print(f'激活: {result["activation"]}')
    if not result['ok']:
        return 1

    print('\n采样 5 秒（推摇杆/按键观察）…')
    deadline = time.perf_counter() + 5
    last = -1
    while time.perf_counter() < deadline:
        snap = reader.snapshot()
        latest = snap['latest']
        if latest and snap['frames'] != last:
            last = snap['frames']
            pressed = [str(i) for i, v in enumerate(latest['buttons']) if v]
            print(f'\r帧{snap["frames"]:<6} 速率={snap["rate"]:>6.1f}Hz  '
                  f'L=({latest["left_raw"][0]:+.3f},{latest["left_raw"][1]:+.3f})  '
                  f'R=({latest["right_raw"][0]:+.3f},{latest["right_raw"][1]:+.3f})  '
                  f'按键={",".join(pressed) or "-"}   ', end='', flush=True)
        time.sleep(0.02)
    print()
    reader.disconnect()
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
