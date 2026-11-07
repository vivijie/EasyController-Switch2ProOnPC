r"""Switch 2 Pro 物理手柄接入层

把之前验证过的三件事封装成模块：
  1. **原生激活**：向厂商接口(接口1)发送 17 条命令，让手柄开始输出 HID 报告
     （等价于网页工具的 "Enable HID Output"，不需要浏览器）
  2. **读取**：用 hidapi 打开 HID 接口，读 64 字节输入报告（报告 ID 0x09，约 250Hz）
  3. **解析**：从报告里解出两根摇杆（12-bit）与按键位域

关键约束（实测结论）
------------------
- 手柄未激活时**完全不出数据**，必须先发激活命令
- 激活需要 claim 接口 1；此时若已有其它 HID 句柄打开 → Access denied
  所以顺序必须是：**先激活，再打开读取句柄**
- 激活后如果程序退出，流通常会保持；但重新插拔手柄后需要重新激活

设备信息（本机实测）
------------------
    VID 0x057E / PID 0x2069
    接口0 = HID（读取）；接口1 = 厂商 bulk（激活）；接口2 = 音频
"""

import ctypes
import os
import sys
import threading
import time

VID = 0x057E
PID = 0x2069
REPORT_ID = 0x09
REPORT_LENGTH = 64

# ==================== 激活命令（ProCon2Tool 原文，逐字节原样）====================
ACTIVATE_COMMANDS = [
    bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
           0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),                    # Start HID output
    bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x16, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x15, 0x91, 0x00, 0x01, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x02] + [0xFF] * 12),
    bytes([0x15, 0x91, 0x00, 0x02, 0x00, 0x11, 0x00, 0x00, 0x00] + [0xFF] * 16),
    bytes([0x15, 0x91, 0x00, 0x03, 0x00, 0x01, 0x00, 0x00, 0x00]),
    bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08] + [0x00] * 10),
    bytes([0x0c, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
    bytes([0x11, 0x91, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x0a, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01] + [0xff] * 8
          + [0x35, 0x00, 0x46] + [0x00] * 8),
    bytes([0x0c, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
    bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x09, 0x00, 0x00, 0x00]),
    bytes([0x10, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x01, 0x91, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x03, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00]),
    bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0x00]),
    bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00, 0x01] + [0x00] * 7),
]

# 按键位域（按 0x09 报告布局；实测可用，若有偏差可在 UI 里用"按键查询"校准）
BUTTON_BITS = [
    (0x05, 0x01, 0, 'B'), (0x05, 0x02, 1, 'A'), (0x05, 0x04, 2, 'Y'), (0x05, 0x08, 3, 'X'),
    (0x05, 0x40, 4, 'R'), (0x05, 0x80, 5, 'ZR'),
    (0x06, 0x01, 14, 'Min'), (0x06, 0x02, 6, 'Plus'),
    (0x06, 0x04, 7, 'RStick'), (0x06, 0x08, 15, 'LStick'),
    (0x06, 0x10, 16, 'Home'), (0x06, 0x20, 17, 'Capture'), (0x06, 0x40, 20, 'C'),
    (0x07, 0x01, 8, 'Down'), (0x07, 0x02, 11, 'Up'),
    (0x07, 0x04, 9, 'Right'), (0x07, 0x08, 10, 'Left'),
    (0x07, 0x40, 12, 'L'), (0x07, 0x80, 13, 'ZL'),
    (0x08, 0x01, 18, 'BR'), (0x08, 0x02, 19, 'BL'),
]
BUTTON_COUNT = 21


def _load_libusb():
    """跨环境找 libusb-1.0.dll"""
    import glob
    try:
        import usb.backend.libusb1 as libusb1
    except ImportError:
        return None
    candidates = []
    try:
        import libusb_package
        dll = libusb_package.find_library('libusb-1.0')
        if dll:
            candidates.append(dll)
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, 'libusb-1.0.dll'))
    candidates.append(os.path.join(os.path.dirname(here), 'libusb-1.0.dll'))
    candidates.append(r'C:\Windows\System32\libusb-1.0.dll')
    local = os.environ.get('LOCALAPPDATA') or ''
    for root in (os.path.join(local, 'Programs', 'Python'), r'C:\Python*'):
        for pattern in ('**/libusb-1.0.dll', '**/libusb*.dll'):
            try:
                candidates.extend(glob.glob(os.path.join(root, pattern), recursive=True))
            except Exception:
                pass
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            backend = libusb1.get_backend(find_library=lambda name, path=candidate: path)
            if backend:
                return backend, candidate
    return None, None


def _text(value):
    """hidapi 的字符串字段可能是 bytes，统一转成 str（JSON 序列化需要）"""
    if value is None:
        return ''
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', 'replace')
        except Exception:
            return value.decode('latin-1', 'replace')
    return str(value)


def enumerate_devices():
    """枚举手柄 HID 接口（不需要激活）

    注意：hidapi 的 open_path() 需要 **bytes** 类型的 path，
    但 Web 层要把它 JSON 序列化（bytes 不行）。
    所以这里同时返回：
      - 'path'      : str（给 JSON/显示用）
      - 'path_bytes': bytes（给 open_path 用，内部使用）
    """
    try:
        import hid as hidapi
    except ImportError:
        return []
    result = []
    try:
        for info in hidapi.enumerate(VID, PID):
            raw_path = info.get('path')
            if isinstance(raw_path, bytes):
                path_bytes = raw_path
            else:
                path_bytes = str(raw_path).encode('utf-8', 'replace')
            result.append({
                'path': _text(raw_path),
                'path_bytes': path_bytes,
                'product': _text(info.get('product_string')),
                'manufacturer': _text(info.get('manufacturer_string')),
                'interface': info.get('interface_number'),
                'usage_page': info.get('usage_page'),
                'usage': info.get('usage'),
                'serial': _text(info.get('serial_number')),
            })
    except Exception:
        pass
    return result


def activate(verbose=False):
    """发送激活命令。返回 (成功, 说明)"""
    try:
        import usb.core
        import usb.util
    except ImportError:
        return False, '没装 pyusb，无法激活手柄（pip install pyusb）'

    backend, dll_path = _load_libusb()
    if backend is None:
        return False, '找不到 libusb-1.0.dll（pip install libusb 或放到程序目录）'

    try:
        device = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    except Exception as error:
        return False, f'打开 USB 设备失败: {type(error).__name__}: {error}'
    if device is None:
        return False, 'USB 设备不存在（手柄未用线连接？）'

    interface_number = out_endpoint = in_endpoint = None
    try:
        try:
            for interface in device.get_active_configuration():
                if interface.bInterfaceNumber == 1:
                    interface_number = 1
                    for endpoint in interface:
                        if (endpoint.bmAttributes & 0x03) != 0x02:
                            continue
                        if endpoint.bEndpointAddress & 0x80:
                            in_endpoint = endpoint.bEndpointAddress
                        else:
                            out_endpoint = endpoint.bEndpointAddress
        except Exception as error:
            return False, f'读取 USB 配置失败: {type(error).__name__}: {error}'

        if interface_number is None or out_endpoint is None:
            return False, '厂商接口(接口1)不可用'

        try:
            usb.util.claim_interface(device, interface_number)
        except Exception as error:
            message = str(error)
            if 'Access denied' in message or 'insufficient permissions' in message:
                return False, ('激活被拒绝（Access denied）：'
                               '有其它程序正占用手柄（如旧的主程序窗口、Steam 输入）')
            return False, f'占用接口失败: {type(error).__name__}: {error}'

        sent = 0
        for command in ACTIVATE_COMMANDS:
            try:
                device.write(out_endpoint, command, timeout=500)
                sent += 1
            except Exception:
                pass
            time.sleep(0.004)
        # 排空回执，避免被当成输入数据
        deadline = time.perf_counter() + 0.4
        while time.perf_counter() < deadline:
            try:
                if not device.read(in_endpoint, 64, timeout=120):
                    break
            except Exception:
                break
        try:
            usb.util.release_interface(device, interface_number)
        except Exception:
            pass

        if sent == 0:
            return False, '激活命令一条都没能发出'
        return True, f'已发送 {sent}/{len(ACTIVATE_COMMANDS)} 条激活命令' + (
            f'（libusb: {dll_path}）' if verbose else '')
    finally:
        # 必须释放设备句柄，否则下一次激活会报 Access denied
        try:
            usb.util.dispose_resources(device)
        except Exception:
            pass
        try:
            device.close()
        except Exception:
            pass



def parse_report(raw):
    """解析 64 字节 0x09 报告 → 摇杆原始值 + 按键状态

    摇杆 12-bit 打包（按 SDL Switch2 驱动的字节偏移）：
        LX = data[6] | ((data[7] & 0x0F) << 8)
        LY = (data[7] >> 4) | (data[8] << 4)
        RX = data[9] | ((data[10] & 0x0F) << 8)
        RY = (data[10] >> 4) | (data[11] << 4)

    **Y 轴方向**：与用户原算法保持一致，**不做取反**。
    原算法里的方向约定是 "Y 正 = 上"，见 analyze_stick_travel:
        '向上': abs(limit['max_y']),  '向下': abs(limit['min_y'])
    所以这里必须保持报告原始方向，交给算法处理。
    （曾加过取反导致"右摇杆上下颠倒"，已撤销。）

    中心值约 2048，物理满偏约 ±1500，换算成 -1~1。
    """
    if len(raw) < 17:
        return None

    def axis(value):
        result = (value - 2048.0) / 1500.0
        return max(-1.0, min(1.0, result))

    lx = raw[6] | ((raw[7] & 0x0F) << 8)
    ly = (raw[7] >> 4) | (raw[8] << 4)
    rx = raw[9] | ((raw[10] & 0x0F) << 8)
    ry = (raw[10] >> 4) | (raw[11] << 4)

    buttons = [False] * BUTTON_COUNT
    for byte_index, mask, button_index, _name in BUTTON_BITS:
        if byte_index < len(raw) and raw[byte_index] & mask:
            buttons[button_index] = True

    return {
        'left_raw': (axis(lx), axis(ly)),
        'right_raw': (axis(rx), axis(ry)),
        'buttons': buttons,
        'report_id': raw[0] if raw else None,
        'stick_12bit': {'lx': lx, 'ly': ly, 'rx': rx, 'ry': ry},
    }


class DeviceReader(object):
    """后台持续读取物理手柄，供 Web 层取最新帧

    设计：
      - 独立线程以 250Hz 读取（设备原生速率）
      - 帧存入 latest，Web 轮询时直接取
      - 断流检测：超过 1 秒没有新帧 → 标记 disconnected
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None
        self.latest_time = 0.0
        self.frames = 0
        self.error = ''
        self.connected = False
        self.running = False
        self.thread = None
        self.device = None
        self.rate_hz = 0.0
        self._rate_mark = time.perf_counter()
        self._rate_frames = 0

    # ---- 生命周期 ----
    def connect(self):
        """扫描 → 激活（必要时）→ 打开读取句柄 → 启动线程

        返回 {'ok': bool, 'message': str, 'devices': [...]}
        """
        self.disconnect()
        devices = enumerate_devices()
        if not devices:
            return {'ok': False, 'message': '未检测到手柄（请用 USB 线连接 Switch2 Pro）',
                    'devices': []}

        chosen = None
        for info in devices:
            if info.get('usage_page') == 0x0001 and info.get('usage') == 0x0005:
                chosen = info
                break
        if chosen is None:
            chosen = devices[0]

        # 先试探：如果流已经在跑就不用激活（避免多占一次接口）
        needs_activation = True
        probe = self._open(chosen.get('path_bytes') or chosen['path'])
        if probe is not None:
            frames = self._probe_frames(probe, 0.6)
            try:
                probe.close()
            except Exception:
                pass
            if frames > 10:
                needs_activation = False

        activation_note = ''
        if needs_activation:
            ok, note = activate(verbose=True)
            activation_note = note
            if not ok:
                # 激活失败也可能流已经在（被别的程序激活过），继续尝试读取
                pass
            time.sleep(0.15)

        device = self._open(chosen.get('path_bytes') or chosen['path'])
        if device is None:
            return {'ok': False,
                    'message': '打开 HID 接口失败（可能被其它程序独占）',
                    'devices': devices, 'activation': activation_note}

        with self.lock:
            self.device = device
            self.connected = True
            self.error = ''
            self.frames = 0
            self.latest = None
            self.latest_time = time.perf_counter()
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

        # 等一下确认真的有数据
        time.sleep(0.8)
        if self.frames == 0:
            self.disconnect()
            return {'ok': False,
                    'message': ('已连接接口但收不到数据。'
                                + (f'（激活: {activation_note}）' if activation_note else '')
                                + ' 请确认手柄未休眠、未被其它程序占用'),
                    'devices': devices, 'activation': activation_note}

        return {'ok': True,
                'message': f'已连接：{chosen.get("product") or chosen["path"]}',
                'devices': devices,
                'path': chosen['path'],
                'activation': activation_note,
                'rate': round(self.rate_hz, 1)}

    def disconnect(self):
        self.running = False
        thread = self.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self.thread = None
        with self.lock:
            device = self.device
            self.device = None
            self.connected = False
        if device is not None:
            try:
                device.close()
            except Exception:
                pass

    # ---- 内部 ----
    def _open(self, path):
        try:
            import hid as hidapi
            device = hidapi.device()
            device.open_path(path)
            device.set_nonblocking(0)
            return device
        except Exception as error:
            self.error = f'{type(error).__name__}: {error}'
            return None

    def _probe_frames(self, device, seconds):
        frames = 0
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            try:
                if device.read(REPORT_LENGTH, 150):
                    frames += 1
            except Exception:
                break
        return frames

    def _loop(self):
        while self.running:
            with self.lock:
                device = self.device
            if device is None:
                break
            try:
                raw = device.read(REPORT_LENGTH, 200)
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                raw = None
            now = time.perf_counter()
            if raw:
                parsed = parse_report(bytes(raw))
                with self.lock:
                    self.latest = parsed
                    self.latest_time = now
                    self.frames += 1
                    self._rate_frames += 1
                if now - self._rate_mark >= 1.0:
                    self.rate_hz = self._rate_frames / (now - self._rate_mark)
                    self._rate_frames = 0
                    self._rate_mark = now
            else:
                if now - self.latest_time > 1.5:
                    self.error = '超过 1.5 秒没有收到数据（可能掉流/休眠）'
                    with self.lock:
                        self.connected = False

    # ---- 对外 ----
    def snapshot(self):
        with self.lock:
            return {
                'connected': self.connected,
                'frames': self.frames,
                'rate': round(self.rate_hz, 1),
                'error': self.error,
                'latest': self.latest,
                'age': round(time.perf_counter() - self.latest_time, 3)
                if self.latest_time else None,
            }


# 全局单例（Web 层共用）
reader = DeviceReader()


def scan_and_connect():
    return reader.connect()


def status():
    return reader.snapshot()


def disconnect():
    reader.disconnect()


def main():
    """命令行自测：连接 → 读 3 秒 → 打印帧率与摇杆值"""
    import argparse
    parser = argparse.ArgumentParser(description='物理手柄接入自测')
    parser.add_argument('--seconds', type=float, default=3.0)
    args = parser.parse_args()

    print('扫描手柄…')
    devices = enumerate_devices()
    print(f'  枚举到 {len(devices)} 个 HID 接口')
    for info in devices:
        print(f'    if={info["interface"]} product={info["product"]!r} '
              f'usage_page=0x{(info["usage_page"] or 0):04X}')

    print('\n连接…')
    result = reader.connect()
    print(f'  ok={result["ok"]}  {result["message"]}')
    if result.get('activation'):
        print(f'  激活: {result["activation"]}')
    if not result['ok']:
        return 1

    print(f'\n采样 {args.seconds:.0f} 秒…')
    deadline = time.perf_counter() + args.seconds
    last_frames = 0
    while time.perf_counter() < deadline:
        snapshot = reader.snapshot()
        latest = snapshot['latest']
        if latest and snapshot['frames'] != last_frames:
            last_frames = snapshot['frames']
            pressed = [name for _b, _m, index, name in BUTTON_BITS
                       if latest['buttons'][index]]
            print(f'\r帧{snapshot["frames"]:<6} 速率={snapshot["rate"]:>6.1f}Hz  '
                  f'L={latest["left_raw"][0]:+.3f},{latest["left_raw"][1]:+.3f}  '
                  f'R={latest["right_raw"][0]:+.3f},{latest["right_raw"][1]:+.3f}  '
                  f'按键={",".join(pressed) or "-"}', end='', flush=True)
        time.sleep(0.02)
    print()
    reader.disconnect()
    print('已断开')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
