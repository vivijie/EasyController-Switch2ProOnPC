r"""Switch 2 Pro 手柄 HID 激活 + 原始读取（纯原生，不需要网页工具）

原理（照搬 ProCon2Tool 的握手命令，见 https://handheldlegend.github.io/procon2tool/ ）:
    手柄在 USB 下“枚举正常但不发数据”，必须先通过厂商接口(MI_01, 已绑定 WinUSB)
    发送一串厂商 bulk 命令，其中第一条就是:
        03 91 00 0d 00 08 00 00 01 00 ff ff ff ff ff ff
    发完之后，HID 接口(MI_00) 才会开始按 4ms 间隔输出 64 字节输入报告（报告 ID 0x09）。
    这正是那个网页工具 "Enable HID Output" 所做的事（它走 WebUSB）。

    实测结论:
      - 不发这 17 条命令 -> HID 读取 0 帧
      - 发了之后        -> 稳定约 250 Hz
      - 命令字节必须**原样发送**，不能补零/改写第 6-7 字节（那是数据不是长度字段）

依赖:
    pyusb（需要 libusb-1.0.dll，脚本会自动跨环境查找）

用法:
    python .\switch2_activate.py                 # 激活 + 读 8 秒
    python .\switch2_activate.py --seconds 20 --verbose
    python .\switch2_activate.py --no-read       # 只激活，不读
    python .\switch2_activate.py --no-keepalive  # 读取期间不重发 Start output
    python .\switch2_activate.py --no-sdl-extra  # 只发工具原始序列

输出:
    原始帧写入 switch2_frames.log，并在终端显示按键/摇杆的实时解析。
"""

import argparse
import ctypes
import os
import statistics
import sys
import time
from ctypes import wintypes

NINTENDO_VID = 0x057E
SWITCH2_PRO_PID = 0x2069
BULK_OUT_SIZE = 64
LOG_FILE = 'switch2_frames.log'

# ==================== 初始化时序 ====================
# 与 ProCon2Tool（handheldlegend.github.io/procon2tool）完全一致的初始化时序，
# 字节原样发送——不要补零、不要改写任何字节
# （它把 0x08/0x07 这类值当成数据，不是长度字段）。
INIT_SEQUENCE = [
    (bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
            0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
     '★ INIT 0x03 Start output（让手柄开始按 4ms 输出 HID）'),
    (bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
     'UNKNOWN 0x07'),
    (bytes([0x16, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
     'UNKNOWN 0x16'),
    (bytes([0x15, 0x91, 0x00, 0x01, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x02,
            0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
            0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
     'REQUEST_CONTROLLER_MAC 0x15 arg 0x01'),
    (bytes([0x15, 0x91, 0x00, 0x02, 0x00, 0x11, 0x00, 0x00, 0x00,
            0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
            0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
     'LTK_REQUEST 0x15 arg 0x02'),
    (bytes([0x15, 0x91, 0x00, 0x03, 0x00, 0x01, 0x00, 0x00, 0x00]),
     'UNKNOWN 0x15 arg 0x03'),
    (bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
     'UNKNOWN 0x09'),
    (bytes([0x0c, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
     'IMU 0x0C arg 0x02（无需回执）'),
    (bytes([0x11, 0x91, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00]),
     'OUT_UNKNOWN 0x11'),
    (bytes([0x0a, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01,
            0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
            0x35, 0x00, 0x46,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
     'UNKNOWN 0x0A（震动参数）'),
    (bytes([0x0c, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
     'IMU 0x0C arg 0x04'),
    (bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x09, 0x00, 0x00, 0x00]),
     'ENABLE_HAPTICS 0x03'),
    (bytes([0x10, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
     'OUT_UNKNOWN 0x10（无需回执）'),
    (bytes([0x01, 0x91, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00]),
     'OUT_UNKNOWN 0x01'),
    (bytes([0x03, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00]),
     'OUT_UNKNOWN 0x03（注意只有 7 字节）'),
    (bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0x00]),
     'OUT_UNKNOWN 0x0A alt'),
    (bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00, 0x01,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
     'SET_PLAYER_LED 0x09'),
]

# SDL 驱动里有、而工具里没有的命令（用于开启/选择输入报告）
SDL_EXTRA_COMMANDS = [
    (bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x05, 0x00, 0x00, 0x00]),
     'Set report format（选输入报告 0x05）'),
    (bytes([0x08, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00]),
     'Enable grip buttons on charging grip'),
]

# 激活后用来保活的命令（就是 Start output，工具里那条）
KEEPALIVE_COMMAND = INIT_SEQUENCE[0][0]

# 工具里被注释掉的 SPI 读校准命令（默认不发，这里保留备用）
SPI_READ_COMMANDS = {
    0x00013080: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0x80, 0x30, 0x01, 0x00]),
    0x000130C0: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0xc0, 0x30, 0x01, 0x00]),
    0x00013040: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0x40, 0x30, 0x01, 0x00]),
    0x00013100: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0x00, 0x31, 0x01, 0x00]),
    0x00013060: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0x60, 0x30, 0x01, 0x00]),
    0x001FC040: bytes([0x02, 0x91, 0x00, 0x04, 0x00, 0x08, 0x00, 0x00, 0x40, 0x7e,
                       0x00, 0x00, 0x40, 0xc0, 0x1f, 0x00]),
}

BUTTON_NAMES = {
    # (字节下标, 位掩码): 名称 —— 来自 SDL 的 HandleSwitchProState
    (5, 0x01): 'Y', (5, 0x02): 'X', (5, 0x04): 'B', (5, 0x08): 'A',
    (5, 0x40): 'R', (5, 0x80): 'ZR',
    (6, 0x01): 'Min', (6, 0x02): 'Plus', (6, 0x04): 'RStick', (6, 0x08): 'LStick',
    (6, 0x10): 'Home', (6, 0x20): 'Capture', (6, 0x40): 'C',
    (7, 0x40): 'L', (7, 0x80): 'ZL',
    (8, 0x01): 'BR(右背键)', (8, 0x02): 'BL(左背键)',
}


def pack_bulk_command(command):
    """补零到 64 字节；仅用于少量场景（正常发送请用原始字节！）"""
    buffer = bytearray(BULK_OUT_SIZE)
    buffer[0:len(command)] = command
    payload_length = command[5]
    buffer[6] = payload_length & 0xFF
    buffer[7] = (payload_length >> 8) & 0xFF
    return bytes(buffer)


def hexdump(data, limit=64):
    return ' '.join(f'{b:02X}' for b in data[:limit])


# ==================== 第一步: 用 pyusb 做激活握手 ====================
def find_libusb_dll():
    """跨 Python 环境找 libusb-1.0.dll"""
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


def load_libusb_backend():
    """定位 libusb-1.0.dll 并返回 pyusb 后端"""
    from usb.backend import libusb1

    dll = find_libusb_dll()
    if dll is None:
        return None
    backend = libusb1.get_backend(find_library=lambda name, path=dll: path)
    if backend:
        print(f'[libusb] 使用: {dll}', flush=True)
    return backend


def find_usb_device(usb_core, backend=None):
    return usb_core.find(idVendor=NINTENDO_VID, idProduct=SWITCH2_PRO_PID,
                         backend=backend)


def find_bulk_endpoints(device):
    """找接口 1 上的 bulk OUT/IN 端点（与 SDL 的 FindBulkEndpoints 一致）"""
    for interface in device.get_active_configuration():
        if interface.bInterfaceNumber != 1:
            continue
        out_endpoint = in_endpoint = None
        for endpoint in interface:
            if (endpoint.bmAttributes & 0x03) != 0x02:  # 0x02 = bulk
                continue
            if endpoint.bEndpointAddress & 0x80:
                in_endpoint = endpoint.bEndpointAddress
            else:
                out_endpoint = endpoint.bEndpointAddress
        if out_endpoint is not None and in_endpoint is not None:
            return interface.bInterfaceNumber, out_endpoint, in_endpoint
    return None, None, None


def activate(usb_core, usb_util, backend=None, verbose=True, sdl_compat=True, burst=True):
    """发送初始化序列，返回 (device, out_endpoint, in_endpoint, 校准数据字典)"""
    device = find_usb_device(usb_core, backend)
    if device is None:
        raise RuntimeError('没找到 Switch 2 Pro 手柄（VID 0x057E / PID 0x2069），'
                           '请确认已用 USB 线连接')

    print(f'已找到 USB 设备: bus={device.bus} address={device.address}', flush=True)
    # 读取字符串描述符可能失败（设备状态/权限问题），不能让它中断激活
    try:
        manufacturer = device.manufacturer or ''
    except Exception as error:
        manufacturer = f'<读取失败 {type(error).__name__}>'
    try:
        product = device.product or ''
    except Exception as error:
        product = f'<读取失败 {type(error).__name__}>'
    print(f'  厂商="{manufacturer}" 产品="{product}"', flush=True)

    interface_number, out_endpoint, in_endpoint = find_bulk_endpoints(device)
    if interface_number is None:
        raise RuntimeError('没找到接口 1 上的 bulk 端点（手柄可能不是 USB 连接，或被其它程序占用）')
    print(f'  接口={interface_number} bulk OUT=0x{out_endpoint:02X} '
          f'bulk IN=0x{in_endpoint:02X}', flush=True)

    # 与 SDL 一样：允许自动解绑内核驱动后占用接口
    try:
        device.set_auto_detach_kernel_driver(True)
    except Exception:
        pass
    try:
        if device.is_kernel_driver_active(interface_number):
            device.detach_kernel_driver(interface_number)
            print('  已解绑内核驱动', flush=True)
    except Exception:
        pass
    usb_util.claim_interface(device, interface_number)
    print(f'  已占用接口 {interface_number}', flush=True)

    calibration = {}

    def send(command, description, expect_reply=True):
        """原样发送字节（不做任何补零或改写），延迟后尝试读 32 字节回执"""
        try:
            written = device.write(out_endpoint, command, timeout=1000)
            write_note = f'写入{written}字节'
        except Exception as error:
            write_note = f'❌写入异常 {type(error).__name__}: {error}'
        if verbose:
            print(f'  -> {" ".join(f"{b:02X}" for b in command)}  [{description}] '
                  f'{write_note}', flush=True)
        time.sleep(0.01)
        if not expect_reply:
            return None
        try:
            reply = bytes(device.read(in_endpoint, 32, timeout=500))
            if verbose:
                print(f'     <- 回执 {len(reply)} 字节: {hexdump(reply[:16])}', flush=True)
            return reply
        except Exception:
            if verbose:
                print('     <- 无回执', flush=True)
            return None

    def read_flash_block(address):
        command = SPI_READ_COMMANDS.get(address)
        if command is None:
            return None
        reply = send(command, f'SPI Read 0x{address:06X}', expect_reply=True)
        if reply and len(reply) >= 0x50:
            return reply[0x10:0x50]
        return None

    print('\n[1/3] 读取手柄工厂校准数据（可选，失败不影响激活）', flush=True)
    block = read_flash_block(0x00013080)
    if block:
        calibration['left'] = parse_stick_calibration(block[0x28:0x28 + 9])
        print(f'  左摇杆校准: {calibration["left"]}', flush=True)
    block = read_flash_block(0x000130C0)
    if block:
        calibration['right'] = parse_stick_calibration(block[0x28:0x28 + 9])
        print(f'  右摇杆校准: {calibration["right"]}', flush=True)
    if not calibration:
        print('  （没读到校准数据，后面可以把你的自校准结果接上）', flush=True)

    print('\n[2/3] 发送初始化序列', flush=True)
    commands = list(INIT_SEQUENCE)
    if sdl_compat:
        commands = commands + list(SDL_EXTRA_COMMANDS)

    if burst:
        # 工具的 sendUsbData 是高频连发：写入后只等 10ms，回执读不到就继续。
        # 这里连着写，避免“发一条等一条”把节奏拖慢。
        print('  模式: 连发（不等待回执）', flush=True)
        for index, (command, description) in enumerate(commands, start=1):
            try:
                written = device.write(out_endpoint, command, timeout=1000)
                status = f'写入{written}字节'
            except Exception as error:
                status = f'❌{type(error).__name__}: {error}'
            if verbose:
                print(f'  -> [{index}/{len(commands)}] '
                      f'{" ".join(f"{b:02X}" for b in command)}  {description}  {status}',
                      flush=True)
            # 只尝试非阻塞地收一下回执，绝不等待
            try:
                device.read(in_endpoint, 32, timeout=1)
            except Exception:
                pass
        time.sleep(0.05)
    else:
        print(f'  来自工具: {len(INIT_SEQUENCE)} 条'
              f'{"，追加 SDL: " + str(len(SDL_EXTRA_COMMANDS)) + " 条" if sdl_compat else ""}',
              flush=True)
        for index, (command, description) in enumerate(commands, start=1):
            send(command, f'{index}/{len(commands)} {description}')

    print('\n[3/3] 激活完成——手柄现在应该开始输出 HID 报告了', flush=True)
    return device, out_endpoint, in_endpoint, calibration


def parse_stick_calibration(data):
    """SDL 的 ParseStickCalibration: 3 个 12-bit 值 = neutral / max / min"""
    if len(data) < 9:
        return None
    x_neutral = data[0] | ((data[1] & 0x0F) << 8)
    y_neutral = (data[1] >> 4) | (data[2] << 4)
    x_max = data[3] | ((data[4] & 0x0F) << 8)
    y_max = (data[4] >> 4) | (data[5] << 4)
    x_min = data[6] | ((data[7] & 0x0F) << 8)
    y_min = (data[7] >> 4) | (data[8] << 4)
    return {'x': {'neutral': x_neutral, 'max': x_max, 'min': x_min},
            'y': {'neutral': y_neutral, 'max': y_max, 'min': y_min}}


# ==================== 第二步: 从 HID 接口读输入报告 ====================
class GUID(ctypes.Structure):
    """HidD_GetHidGuid 需要真正的 GUID 结构"""
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('InterfaceClassGuid', GUID),
                ('Flags', wintypes.DWORD), ('Reserved', ctypes.POINTER(ctypes.c_ulong))]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [('Internal', ctypes.POINTER(ctypes.c_ulong)),
                ('InternalHigh', ctypes.POINTER(ctypes.c_ulong)),
                ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD),
                ('hEvent', wintypes.HANDLE)]


def open_hid_interface():
    """按 Windows HID 接口路径打开 MI_00"""
    hid = ctypes.WinDLL('hid.dll')
    setupapi = ctypes.WinDLL('setupapi.dll')
    kernel32 = ctypes.WinDLL('kernel32.dll')

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wintypes.DWORD,
        ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel32.ReadFile.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    # 重叠 I/O 相关（用于带超时读取，避免设备不发数据时永久阻塞）
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                      wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.CancelIo.argtypes = [wintypes.HANDLE]
    kernel32.GetOverlappedResult.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    kernel32.GetOverlappedResult.restype = wintypes.BOOL

    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    device_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid), None, None, 0x02 | 0x10)
    if device_info == INVALID_HANDLE_VALUE:
        raise RuntimeError('SetupDiGetClassDevs 失败')

    path = None
    index = 0
    try:
        while True:
            interface_data = SP_DEVICE_INTERFACE_DATA()
            interface_data.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(
                    device_info, None, ctypes.byref(hid_guid), index,
                    ctypes.byref(interface_data)):
                break
            index += 1
            required_size = wintypes.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(
                device_info, ctypes.byref(interface_data), None, 0,
                ctypes.byref(required_size), None)
            if required_size.value == 0:
                continue
            buffer = ctypes.create_string_buffer(required_size.value)
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong))[0] = 8
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_info, ctypes.byref(interface_data), buffer,
                    required_size.value, ctypes.byref(required_size), None):
                continue
            candidate = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
            if 'VID_057E' in candidate.upper() and 'PID_2069' in candidate.upper():
                path = candidate
                break
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info)

    if path is None:
        raise RuntimeError('没找到手柄的 HID 接口（VID_057E/PID_2069）')

    handle = kernel32.CreateFileW(path, 0x80000000, 0x01 | 0x02,
                                  None, 3, 0x40000000, None)  # FILE_FLAG_OVERLAPPED
    if handle == INVALID_HANDLE_VALUE:
        raise RuntimeError(f'打开 HID 接口失败: {path}')
    event = kernel32.CreateEventW(None, True, False, None)
    if not event:
        kernel32.CloseHandle(handle)
        raise RuntimeError('创建事件对象失败')
    return kernel32, handle, path, event


def parse_report(report):
    """按 SDL 的 HandleSwitchProState 解析 64 字节报告"""
    if len(report) < 17:
        return None
    # 若首字节是报告 ID(0x05/0x09)，正文从 data[1] 开始
    data = report[1:] if report[0] in (0x05, 0x09) and len(report) >= 17 else report
    if len(data) < 17:
        return None

    buttons = [name for (byte_index, mask), name in BUTTON_NAMES.items()
               if data[byte_index] & mask]

    lx = data[11] | ((data[12] & 0x0F) << 8)
    ly = (data[12] >> 4) | (data[13] << 4)
    rx = data[14] | ((data[15] & 0x0F) << 8)
    ry = (data[15] >> 4) | (data[16] << 4)

    return {'buttons': buttons, 'lx': lx, 'ly': ly, 'rx': rx, 'ry': ry}


def read_report_timeout(kernel32, handle, event, timeout_ms=500, size=128):
    """带超时地读一帧，返回 bytes 或 None（超时/无数据）"""
    ERROR_IO_PENDING = 997
    WAIT_OBJECT_0 = 0x00000000
    overlapped = OVERLAPPED()
    overlapped.hEvent = event
    buffer = ctypes.create_string_buffer(size)
    read_bytes = wintypes.DWORD(0)
    kernel32.ResetEvent(event)
    if not kernel32.ReadFile(handle, buffer, size, ctypes.byref(read_bytes),
                             ctypes.byref(overlapped)):
        if ctypes.get_last_error() != ERROR_IO_PENDING:
            return None
        if kernel32.WaitForSingleObject(event, timeout_ms) != WAIT_OBJECT_0:
            kernel32.CancelIo(handle)
            return None
        if not kernel32.GetOverlappedResult(handle, ctypes.byref(overlapped),
                                            ctypes.byref(read_bytes), False):
            return None
    if read_bytes.value == 0:
        return None
    return buffer.raw[:read_bytes.value]


def install_watchdog(seconds=6.0):
    """若主线程卡住不动，把堆栈打印出来（用于定位静默退出/卡死）"""
    import threading
    import traceback

    main_thread = threading.main_thread()
    state = {'last': None, 'since': time.perf_counter(), 'dumped': False}

    def watch():
        while True:
            time.sleep(1.0)
            frame = sys._current_frames().get(main_thread.ident)
            if frame is None:
                continue
            info = (frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name)
            if info != state['last']:
                state['last'] = info
                state['since'] = time.perf_counter()
                continue
            if state['dumped'] or (time.perf_counter() - state['since']) < seconds:
                continue
            state['dumped'] = True
            print('\n!! 主线程疑似卡住，堆栈如下:', flush=True)
            print(f'   位置: {info[0]}:{info[1]} in {info[2]}', flush=True)
            walker = frame.f_back
            depth = 0
            while walker is not None and depth < 10:
                print(f'   <- {walker.f_code.co_filename}:{walker.f_lineno} '
                      f'in {walker.f_code.co_name}', flush=True)
                walker = walker.f_back
                depth += 1
            for thread_id, thread_frame in sys._current_frames().items():
                print(f'-- 线程 {thread_id}:', flush=True)
                print(''.join(traceback.format_stack(thread_frame, limit=4)), flush=True)

    threading.Thread(target=watch, daemon=True).start()


def read_loop(kernel32, handle, seconds, verbose, event, keepalive=None):
    """keepalive: (device, out_endpoint) —— 每 0.5 秒重发一次 Start output"""
    print(f'\n开始读取 HID 输入报告，共 {seconds} 秒 —— 请转动摇杆、按按键\n', flush=True)
    start = time.perf_counter()
    frames = 0
    timeouts = 0
    keepalive_count = 0
    next_keepalive = 0.0
    last_print = 0.0
    last_print_verbose = 0.0
    log = open(LOG_FILE, 'w', encoding='utf-8')
    log.write('# Switch2 Pro raw HID frames (after activation)\n')
    sample = None
    try:
        while time.perf_counter() - start < seconds:
            elapsed = time.perf_counter() - start
            if elapsed - last_print > 1.0:
                last_print = elapsed
                print(f'  ...已运行 {elapsed:.1f}s，帧={frames}，超时={timeouts}，'
                      f'保活={keepalive_count}', flush=True)
            if keepalive is not None and elapsed >= next_keepalive:
                device, out_endpoint = keepalive
                try:
                    device.write(out_endpoint, KEEPALIVE_COMMAND, timeout=200)
                    keepalive_count += 1
                except Exception:
                    pass
                next_keepalive = elapsed + 0.5
            raw = read_report_timeout(kernel32, handle, event, timeout_ms=300)
            if raw is None:
                timeouts += 1
                continue
            frames += 1
            parsed = parse_report(raw)
            log.write(f'{time.perf_counter() - start:.4f} '
                      f'{" ".join(f"{b:02X}" for b in raw)} {parsed}\n')
            if parsed and sample is None:
                sample = (raw, parsed)
            now = time.perf_counter()
            if verbose and now - last_print_verbose > 0.25:
                last_print_verbose = now
                if parsed:
                    print(f'\r帧={frames:<6} L=({parsed["lx"]:>4},{parsed["ly"]:>4}) '
                          f'R=({parsed["rx"]:>4},{parsed["ry"]:>4}) '
                          f'按键={",".join(parsed["buttons"]) or "-"}          ',
                          end='', flush=True)
    except KeyboardInterrupt:
        print('\n用户中断', flush=True)
    except BaseException as error:
        import traceback
        print(f'\n读取循环异常: {type(error).__name__}: {error}', flush=True)
        traceback.print_exc()
    finally:
        log.close()

    print()
    if sample is not None:
        raw, parsed = sample
        print('\n首个可用帧:', flush=True)
        print('  十六进制:', ' '.join(f'{b:02X}' for b in raw), flush=True)
        print(f'  报告ID=0x{raw[0]:02X} 长度={len(raw)}', flush=True)
        print(f'  左摇杆 raw=({parsed["lx"]}, {parsed["ly"]})  '
              f'右摇杆 raw=({parsed["rx"]}, {parsed["ry"]})  '
              f'（12-bit，中心约 2048）', flush=True)
        print(f'  按键={parsed["buttons"]}', flush=True)
    print(f'\n总帧数={frames}，读取超时次数={timeouts}，保活发送={keepalive_count} 次，'
          f'耗时{seconds}秒，约 {frames / max(seconds, 0.001):.0f} Hz', flush=True)
    print(f'原始帧已写入: {LOG_FILE}', flush=True)
    return frames


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds', type=float, default=8.0, help='激活后读取时长')
    parser.add_argument('--verbose', action='store_true', help='实时显示解析结果')
    parser.add_argument('--no-read', action='store_true', help='只激活，不读取')
    parser.add_argument('--no-keepalive', action='store_true', help='读取期间不重发 Start output')
    parser.add_argument('--no-sdl-extra', action='store_true', help='只发工具原始序列，不追加 SDL 命令')
    args = parser.parse_args()

    if not sys.platform.startswith('win'):
        print('这个脚本是为 Windows 写的')
        return 1

    try:
        import usb.core
        import usb.util
    except ImportError:
        print('缺少 pyusb，请先安装: pip install pyusb')
        return 1

    backend = load_libusb_backend()
    if backend is None:
        print('找不到 libusb-1.0.dll。可以选一种解决:')
        print('  pip install libusb          （自带 DLL，最简单）')
        print('  或把 libusb-1.0.dll 放到主程序同目录')
        return 1

    try:
        device, out_endpoint, in_endpoint, calibration = activate(
            usb.core, usb.util, backend=backend, sdl_compat=not args.no_sdl_extra)
    except Exception as error:
        print(f'\n激活失败: {type(error).__name__}: {error}')
        print('常见原因:')
        print('  1. 手柄没插好 / 不是 USB 连接')
        print('  2. 接口 1 被别的程序占用（关掉 Steam、浏览器里的 ProCon2Tool 页面再试）')
        print('  3. 需要管理员权限占用该接口（用管理员身份重开终端再试）')
        return 1

    if args.no_read:
        return 0

    frames = 0
    try:
        kernel32, handle, path, event = open_hid_interface()
        print(f'HID 接口: {path}', flush=True)
        frames = read_loop(kernel32, handle, args.seconds, args.verbose, event,
                           keepalive=None if args.no_keepalive else (device, out_endpoint))
        kernel32.CloseHandle(handle)
    except Exception as error:
        print(f'\n读取 HID 失败: {type(error).__name__}: {error}')
    finally:
        try:
            usb.util.release_interface(device, 1)
        except Exception:
            pass

    if frames == 0:
        print('\n⚠️ 激活后仍然 0 帧。说明这套初始化序列对你的手柄不够，')
        print('   下一步需要抓取那个网页工具实际发送的 USB 数据来对齐。')
        return 2
    print('\n✅ 原生激活成功，手柄已经能输出 HID 报告了。')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
