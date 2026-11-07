r"""Switch 2 Pro 手柄 —— Windows 原生读取（不依赖 SDL/pygame，也不需要网页工具）

设计要点（都是踩过的坑）:
    1. 激活: 用 pyusb 在厂商接口(接口 1, 已绑 WinUSB)原样发送 ProCon2Tool 的 17 条命令
       —— 等价于网页工具的 "Enable HID Output"。不做这一步手柄完全不发数据（实测 0 帧）
    2. 读取: 用 Windows HID API 读 HID 接口(接口 0)的 64 字节报告(报告 ID 0x09)
    3. 结构体安全: Windows 把这些结构当**固定大小**写入(如 HidP_GetCaps 写
       sizeof(HIDP_CAPS) 字节)，用 ctypes.Structure 声明一旦与实际不符就会写出分配范围、
       破坏堆 -> 表现为“读完第一帧就 0xC0000005 崩溃”。
       因此这里全部用**连续字节缓冲区 + 手动偏移**读取。
    4. 句柄安全: 所有涉及 HANDLE 的函数都必须声明 argtypes，否则 64 位句柄被截断成 32 位
       （CancelIo 曾因此触发访问违规）。
    5. 枚举: 用 hidapi（pip 包 hid）枚举接口，比手写 SetupDi 枚举可靠。

报告格式(64 字节, 报告 ID 0x09):
    [0]      报告 ID = 0x09
    [6..11]  左右摇杆, 各 12-bit 打包(低 12 位 = X, 高 12 位 = Y)
    [5..8]   按键位域
    [17..]   陀螺仪/加速度(本项目用不到)

关于频率: 实测约 250 Hz（帧间隔中位数 3.997ms、抖动 0.03ms）。
    这是手柄固件上限，不是本程序限制 —— 改激活命令里的间隔参数(0x00~0x04)频率不变，
    第三方 gamepadla 实测同样是 248.45Hz / 4.03ms。

用法:
    python .\native_reader.py                  # 激活 + 读取 10 秒
    python .\native_reader.py --seconds 30
    python .\native_reader.py --no-activate    # 手柄已处于输出状态时
    python .\native_reader.py --raw-log         # 打印每帧十六进制
    python .\native_reader.py --win32           # 用自写 Win32 HID 读取替代 hidapi
"""

import argparse
import ctypes
import os
import statistics
import sys
import time
from ctypes import wintypes

import usb.core
import usb.util
from usb.backend import libusb1

NINTENDO_VID = 0x057E
SWITCH2_PRO_PID = 0x2069

# Windows 返回码：HidP_GetCaps 成功
HIDP_STATUS_SUCCESS = 0x00110000

# ---- 厂商接口命令（ProCon2Tool 原文，逐字节原样，不要改！）----
# 注意: 这些字节**不能补零、不能改写**。0x08/0x07 之类是数据不是长度字段；
# 早期版本误按 SDL 规则改写了第 6-7 字节，导致命令被破坏、手柄零回执。
TOOL_COMMANDS = [
    bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
           0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),                      # Start HID output
    bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x16, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x15, 0x91, 0x00, 0x01, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x02]
          + [0xFF] * 12),
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

# 按键位域: (字节下标, 位掩码) -> 名称（按报告 ID 0x09 的布局，待精确标定）
BUTTON_BITS = [
    (5, 0x01, 'Y'), (5, 0x02, 'X'), (5, 0x04, 'B'), (5, 0x08, 'A'),
    (5, 0x40, 'R'), (5, 0x80, 'ZR'),
    (6, 0x01, 'Min'), (6, 0x02, 'Plus'), (6, 0x04, 'RStick'), (6, 0x08, 'LStick'),
    (6, 0x10, 'Home'), (6, 0x20, 'Capture'), (6, 0x40, 'C'),
    (7, 0x01, 'Down'), (7, 0x02, 'Up'), (7, 0x04, 'Right'), (7, 0x08, 'Left'),
    (7, 0x40, 'L'), (7, 0x80, 'ZL'),
    (8, 0x01, 'BR'), (8, 0x02, 'BL'),
]

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0x00000000

LOG_FILE = 'native_reader_frames.log'


# ==================== libusb 激活 ====================
def find_libusb_dll():
    """找 libusb-1.0.dll。

    libusb_package 只在某些 Python 环境里装了（例如 3.11 的 site-packages），
    换一个 Python 跑本脚本就会找不到 —— 所以跨环境搜一遍。
    """
    import glob

    candidates = []
    try:
        import libusb_package
        dll = libusb_package.find_library('libusb-1.0')
        if dll:
            candidates.append(dll)
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ('libusb-1.0.dll', 'libusb-1.0.so', 'libusb-1.0.dylib'):
        candidates.append(os.path.join(here, name))

    candidates.extend([r'C:\Windows\System32\libusb-1.0.dll',
                       r'C:\Windows\SysWOW64\libusb-1.0.dll'])

    local_appdata = os.environ.get('LOCALAPPDATA') or ''
    for root in (os.path.join(local_appdata, 'Programs', 'Python'), r'C:\Python*'):
        for pattern in ('**/libusb-1.0.dll', '**/libusb*.dll'):
            try:
                candidates.extend(glob.glob(os.path.join(root, pattern), recursive=True))
            except Exception:
                pass

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def load_backend(verbose=True):
    """返回 pyusb 的 libusb 后端（找不到时给出可操作的提示）"""
    dll = find_libusb_dll()
    if dll is None:
        if verbose:
            print('[libusb] 没找到 libusb-1.0.dll —— 激活这一步会跳过。', flush=True)
            print('[libusb] 解决办法（任选一个）:', flush=True)
            print('   1) 用装了 pyusb 的那个 Python 运行本脚本（例如 Python 3.11）', flush=True)
            print('   2) pip install libusb', flush=True)
            print('   3) 把 libusb-1.0.dll 复制到本脚本所在目录', flush=True)
        return None
    backend = libusb1.get_backend(find_library=lambda name, path=dll: path)
    if backend and verbose:
        print(f'[libusb] 使用: {dll}', flush=True)
    return backend


def activate(verbose=True, drain_seconds=1.0):
    """发送 ProCon2Tool 的 17 条命令，让手柄进入输出状态

    返回 (device, usb_util, interface_number)；失败抛异常。
    """
    backend = load_backend(verbose=verbose)
    if backend is None:
        raise RuntimeError('找不到 libusb-1.0.dll')

    device = usb.core.find(idVendor=NINTENDO_VID, idProduct=SWITCH2_PRO_PID,
                           backend=backend)
    if device is None:
        raise RuntimeError('没找到手柄（VID 057E / PID 2069），请确认用 USB 线连接')

    interface_number = out_endpoint = in_endpoint = None
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
    if interface_number is None or out_endpoint is None:
        raise RuntimeError('接口 1 上没有找到 bulk 端点')

    usb.util.claim_interface(device, interface_number)
    if verbose:
        print(f'[激活] 接口={interface_number} OUT=0x{out_endpoint:02X} '
              f'IN=0x{in_endpoint:02X}', flush=True)
    for index, command in enumerate(TOOL_COMMANDS, start=1):
        try:
            device.write(out_endpoint, command, timeout=500)
        except Exception as error:
            if verbose:
                print(f'[激活] [{index}] 写入失败: {type(error).__name__}: {error}',
                      flush=True)
        time.sleep(0.004)

    # 排空回执，避免它们被误当成输入数据
    drained = 0
    deadline = time.perf_counter() + drain_seconds
    while time.perf_counter() < deadline:
        try:
            if device.read(in_endpoint, 64, timeout=150):
                drained += 1
        except Exception:
            break
    if verbose:
        print(f'[激活] 已发送 {len(TOOL_COMMANDS)} 条命令，排空 {drained} 个回执包', flush=True)
    return device, usb.util, interface_number


# ==================== hidapi 读取 ====================
class HidapiReader(object):
    """用 hidapi（pip 包 hid）枚举并读取，避免手写 SetupDi 枚举的坑"""

    def __init__(self):
        import hid as hidapi
        self.hidapi = hidapi
        self.device = None
        self.report_length = 64

    def open(self):
        # 只按 VID/PID 匹配；若枚举到多个接口，选标准游戏手柄集合
        candidates = self.hidapi.enumerate(NINTENDO_VID, SWITCH2_PRO_PID)
        if not candidates:
            raise RuntimeError('hidapi 没枚举到手柄（VID 057E / PID 2069）')
        info = None
        for candidate in candidates:
            # usage_page 0x0001 / usage 0x0005 = 标准游戏手柄集合
            if candidate.get('usage_page') == 0x0001 and candidate.get('usage') == 0x0005:
                info = candidate
                break
        if info is None:
            info = candidates[0]
        self.device = self.hidapi.device()
        self.device.open_path(info['path'])
        self.device.set_nonblocking(0)
        print(f"[HID] hidapi 打开: {info.get('product_string')} "
              f"usage_page=0x{info.get('usage_page', 0):04X} "
              f"usage=0x{info.get('usage', 0):04X} "
              f"interface={info.get('interface_number')}", flush=True)
        return info['path']

    def read(self, timeout_ms=300):
        try:
            data = self.device.read(64, timeout_ms)
        except Exception:
            return None
        if not data:
            return None
        return bytes(data)

    def close(self):
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
            self.device = None


# ==================== 自写 Win32 HID 读取 ====================
class GUID(ctypes.Structure):
    """HidD_GetHidGuid 需要一个真正的 GUID 结构，不能用裸 bytes 代替"""
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


class Win32Hid(object):
    """用连续字节缓冲区封装 Windows HID 读取，避免 ctypes 结构体尺寸不匹配破坏堆

    注意: HIDP_CAPS 的 Reserved 是 USHORT[16]。若用 ctypes.Structure 声明且写错
    字段数，HidP_GetCaps 会写出分配范围 -> 堆损坏 -> 读完第一帧就崩溃。
    所以这里用 512 字节裸缓冲 + 手动偏移取 InputReportByteLength（偏移 4）。
    """

    def __init__(self):
        self.hid = ctypes.WinDLL('hid.dll')
        self.setupapi = ctypes.WinDLL('setupapi.dll')
        self.kernel32 = ctypes.WinDLL('kernel32.dll')
        self._declare()
        self.handle = None
        self.event = None
        self.report_length = 64
        self.overlapped = ctypes.create_string_buffer(32)  # sizeof(OVERLAPPED) on x64
        self.buffer = ctypes.create_string_buffer(1024)

    def _declare(self):
        hid, setupapi, kernel32 = self.hid, self.setupapi, self.kernel32

        hid.HidD_GetHidGuid.argtypes = [ctypes.c_void_p]
        hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE,
                                              ctypes.POINTER(ctypes.c_void_p)]
        hid.HidD_GetPreparsedData.restype = wintypes.BOOL
        hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
        hid.HidD_FreePreparsedData.restype = wintypes.BOOL
        # PHIDP_CAPS 用裸指针 + 512 字节缓冲，长度自己控制
        hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        hid.HidP_GetCaps.restype = ctypes.c_long

        setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
        setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
        setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p]
        setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
        setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
        setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
        setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                          wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel32.ResetEvent.restype = wintypes.BOOL
        # 关键: 不声明 argtypes 时 64 位句柄会被截断成 32 位，导致 CancelIo 访问违规
        kernel32.CancelIo.argtypes = [wintypes.HANDLE]
        kernel32.CancelIo.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.GetOverlappedResult.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL]
        kernel32.GetOverlappedResult.restype = wintypes.BOOL

    def _find_path(self):
        hid_guid = GUID()
        self.hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
        device_info = self.setupapi.SetupDiGetClassDevsW(
            ctypes.byref(hid_guid), None, None, 0x02 | 0x10)
        if device_info == INVALID_HANDLE_VALUE:
            raise RuntimeError('SetupDiGetClassDevs 失败')
        path = None
        index = 0
        # SP_DEVICE_INTERFACE_DATA 是 28 字节(cbSize 4 + GUID 16 + Flags 4 + Reserved 4)
        interface_data = ctypes.create_string_buffer(32)
        ctypes.cast(interface_data, ctypes.POINTER(ctypes.c_ulong))[0] = 28
        try:
            while self.setupapi.SetupDiEnumDeviceInterfaces(
                    device_info, None, ctypes.byref(hid_guid), index,
                    ctypes.byref(interface_data)):
                index += 1
                needed = wintypes.DWORD(0)
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_info, ctypes.byref(interface_data), None, 0,
                    ctypes.byref(needed), None)
                if needed.value == 0:
                    continue
                detail = ctypes.create_string_buffer(needed.value)
                # SP_DEVICE_INTERFACE_DETAIL_DATA_W: cbSize(4) + 路径; x64 下 cbSize = 8
                ctypes.cast(detail, ctypes.POINTER(ctypes.c_ulong))[0] = 8
                if not self.setupapi.SetupDiGetDeviceInterfaceDetailW(
                        device_info, ctypes.byref(interface_data), ctypes.byref(detail),
                        needed.value, ctypes.byref(needed), None):
                    continue
                candidate = ctypes.wstring_at(ctypes.addressof(detail) + 4)
                upper = candidate.upper()
                if 'VID_057E' in upper and 'PID_2069' in upper:
                    path = candidate
                    break
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(device_info)
        if path is None:
            raise RuntimeError('没找到手柄的 HID 接口')
        return path

    def open(self):
        path = self._find_path()
        self.handle = self.kernel32.CreateFileW(
            path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
            OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if self.handle == INVALID_HANDLE_VALUE:
            raise RuntimeError(f'打开 HID 接口失败 (err={ctypes.get_last_error()})')
        self.event = self.kernel32.CreateEventW(None, True, False, None)
        if not self.event:
            raise RuntimeError('CreateEvent 失败')

        # 用裸缓冲取 caps: HIDP_CAPS 的 InputReportByteLength 在偏移 4
        preparsed = ctypes.c_void_p(0)
        caps_buffer = ctypes.create_string_buffer(512)
        if self.hid.HidD_GetPreparsedData(self.handle, ctypes.byref(preparsed)):
            status = self.hid.HidP_GetCaps(preparsed, ctypes.byref(caps_buffer))
            if status == HIDP_STATUS_SUCCESS:
                raw = caps_buffer.raw
                usage = int.from_bytes(raw[0:2], 'little')
                usage_page = int.from_bytes(raw[2:4], 'little')
                input_len = int.from_bytes(raw[4:6], 'little')
                if input_len:
                    self.report_length = input_len
                print(f'[HID] UsagePage=0x{usage_page:04X} Usage=0x{usage:04X} '
                      f'InputReportByteLength={input_len}', flush=True)
            else:
                print(f'[HID] HidP_GetCaps 返回 0x{status & 0xFFFFFFFF:08X}，按 64 字节读',
                      flush=True)
            self.hid.HidD_FreePreparsedData(preparsed)
        else:
            print('[HID] HidD_GetPreparsedData 失败，按 64 字节读', flush=True)
        return path

    def read(self, timeout_ms=300):
        """读一帧，返回 bytes 或 None"""
        read_bytes = wintypes.DWORD(0)
        ctypes.memset(ctypes.byref(self.overlapped), 0, 32)
        # OVERLAPPED.hEvent 在 x64 的偏移是 24
        ctypes.cast(ctypes.byref(self.overlapped, 24),
                    ctypes.POINTER(wintypes.HANDLE))[0] = self.event
        self.kernel32.ResetEvent(self.event)
        ok = self.kernel32.ReadFile(self.handle, ctypes.byref(self.buffer),
                                    self.report_length, ctypes.byref(read_bytes),
                                    ctypes.byref(self.overlapped))
        if not ok:
            if ctypes.get_last_error() != ERROR_IO_PENDING:
                return None
            if self.kernel32.WaitForSingleObject(self.event, timeout_ms) != WAIT_OBJECT_0:
                self.kernel32.CancelIo(self.handle)
                return None
            if not self.kernel32.GetOverlappedResult(
                    self.handle, ctypes.byref(self.overlapped),
                    ctypes.byref(read_bytes), False):
                return None
        if read_bytes.value == 0:
            return None
        return self.buffer.raw[:read_bytes.value]

    def close(self):
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None
        if self.event:
            self.kernel32.CloseHandle(self.event)
            self.event = None


# ==================== 报告解析 ====================
def parse_report(raw):
    if len(raw) < 17:
        return None
    # 第 0 字节是报告 ID (0x09)；摇杆/按键从 1 开始算，所以整体偏移 1
    body = raw
    lx = body[6] | ((body[7] & 0x0F) << 8)
    ly = (body[7] >> 4) | (body[8] << 4)
    rx = body[9] | ((body[10] & 0x0F) << 8)
    ry = (body[10] >> 4) | (body[11] << 4)
    buttons = [name for index, mask, name in BUTTON_BITS if body[index] & mask]
    return {'lx': lx, 'ly': ly, 'rx': rx, 'ry': ry, 'buttons': buttons}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=10.0)
    parser.add_argument('--no-activate', action='store_true')
    parser.add_argument('--raw-log', action='store_true', help='每帧打印十六进制')
    parser.add_argument('--hz-interval', type=float, default=1.0, help='状态刷新间隔(秒)')
    parser.add_argument('--win32', action='store_true', help='用自写 Win32 HID 读取替代 hidapi')
    args = parser.parse_args()

    usb_handles = None
    if not args.no_activate:
        try:
            usb_handles = activate()
        except Exception as error:
            print(f'[激活] 失败: {type(error).__name__}: {error}', flush=True)
            print('[激活] 继续尝试读取（手柄可能已处于输出状态，或已被其它程序激活）',
                  flush=True)

    reader = Win32Hid() if args.win32 else HidapiReader()
    path = reader.open()
    print(f'[HID] {path}', flush=True)
    print(f'\n开始读取 {args.seconds} 秒 —— 请转动摇杆、按按键\n', flush=True)

    frames = 0
    intervals = []
    last_state = None
    previous_time = None
    start = time.perf_counter()
    next_status = start + args.hz_interval
    log = open(LOG_FILE, 'w', encoding='utf-8')
    try:
        while time.perf_counter() - start < args.seconds:
            raw = reader.read()
            now = time.perf_counter()
            if raw is None:
                continue
            frames += 1
            if previous_time is not None:
                intervals.append((now - previous_time) * 1000.0)
            previous_time = now
            parsed = parse_report(raw)
            log.write(f'{now - start:.4f} '
                      f'{" ".join(f"{b:02X}" for b in raw)}\n')
            if args.raw_log:
                print(f'  帧{frames}: {" ".join(f"{b:02X}" for b in raw)}', flush=True)
            elif parsed and time.perf_counter() >= next_status:
                next_status = time.perf_counter() + args.hz_interval
                state = (parsed['lx'], parsed['ly'], parsed['rx'], parsed['ry'],
                         tuple(parsed['buttons']))
                if state != last_state:
                    last_state = state
                    print(f'  帧{frames:<6} L=({parsed["lx"]:>4},{parsed["ly"]:>4}) '
                          f'R=({parsed["rx"]:>4},{parsed["ry"]:>4}) '
                          f'按键={",".join(parsed["buttons"]) or "-"}', flush=True)
    except KeyboardInterrupt:
        print('\n用户中断', flush=True)
    finally:
        log.close()
        reader.close()
        if usb_handles is not None:
            device, usb_util, interface_number = usb_handles
            try:
                usb_util.release_interface(device, interface_number)
            except Exception:
                pass

    elapsed = time.perf_counter() - start
    print(f'\n总帧数={frames}，耗时{elapsed:.1f}秒，约 {frames / max(elapsed, 0.001):.0f} Hz',
          flush=True)
    if intervals:
        print(f'帧间隔: 中位数 {statistics.median(intervals):.3f} ms，'
              f'抖动 {statistics.pstdev(intervals) if len(intervals) > 1 else 0:.3f} ms',
              flush=True)
    print(f'原始帧已写入 {LOG_FILE}', flush=True)
    return 0 if frames else 2


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
