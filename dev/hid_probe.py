r"""Switch 2 Pro 手柄 HID 接口探测脚本（只读，不需要安装任何第三方库）

作用:
    列出系统里所有 HID 接口，并标出当前是否被占用，用来判断:
      1. 手柄在 Windows 上到底暴露了几个 HID 接口
      2. 每个接口能否直接用 HID 读取（原生读取路线是否可行）
      3. 手柄的 VID/PID，供后续代码按 ID 精确打开

用法（不需要管理员权限，但用管理员权限能看到更完整的占用情况）:
    python .\hid_probe.py

判读方法:
    [可读]  -> 该接口可以用 CreateFile(HID) 打开，原生 HID 读取可行
    [占用]  -> 打不开，通常是被 SDL/Steam/浏览器 等独占，或被换成了 WinUSB 驱动
               （如果是 WinUSB，HID 路线就走不通，需要 libusb/WinUSB 路线）
    Usage Page 0x0001 Usage 0x0005 -> 标准游戏手柄集合（HID 输入报告从这里来）
    Usage Page 0xFF00 及以上        -> 厂商自定义集合（专属报告/vendor 通道常见）
    ★ 标记的是 Nintendo(VID 0x057E) 的设备
"""

import ctypes
import sys
from ctypes import wintypes

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# 只用于展示，方便你按名字定位;更全的对照表可查 USB-IF HID Usage Tables
USAGE_PAGE_NAMES = {
    0x0001: 'Generic Desktop (摇杆/按键/鼠标)',
    0x0002: 'Simulation',
    0x0005: 'Game Controls',
    0x0006: 'Generic Device',
    0x000C: 'Consumer',
    0x000D: 'Digitizer',
    0x0020: 'Sensor',
    0xFF00: 'Vendor-defined (厂商自定义)',
}


class GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', wintypes.DWORD),
        ('Data2', wintypes.WORD),
        ('Data3', wintypes.WORD),
        ('Data4', ctypes.c_ubyte * 8),
    ]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('InterfaceClassGuid', GUID),
        ('Flags', wintypes.DWORD),
        ('Reserved', ctypes.POINTER(ctypes.c_ulong)),
    ]


class HIDD_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ('Size', ctypes.c_ulong),
        ('VendorID', ctypes.c_ushort),
        ('ProductID', ctypes.c_ushort),
        ('VersionNumber', ctypes.c_ushort),
    ]


def load_apis():
    hid = ctypes.WinDLL('hid.dll')
    setupapi = ctypes.WinDLL('setupapi.dll')
    kernel32 = ctypes.WinDLL('kernel32.dll')

    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.POINTER(HIDD_ATTRIBUTES)]
    hid.HidD_GetAttributes.restype = wintypes.BOOL
    hid.HidD_GetManufacturerString.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                               ctypes.c_ulong]
    hid.HidD_GetManufacturerString.restype = wintypes.BOOL
    hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                          ctypes.c_ulong]
    hid.HidD_GetProductString.restype = wintypes.BOOL

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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    return hid, setupapi, kernel32


def get_string(hid_function, handle, buffer_size=256):
    buffer = ctypes.create_unicode_buffer(buffer_size)
    if hid_function(handle, buffer, ctypes.sizeof(buffer)):
        return buffer.value
    return ''


def main():
    if not sys.platform.startswith('win'):
        print('这个脚本只能在 Windows 上运行')
        return 1

    hid, setupapi, kernel32 = load_apis()

    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    device_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if device_info == INVALID_HANDLE_VALUE:
        print('SetupDiGetClassDevs 失败')
        return 1

    interfaces = []
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
            # cbSize 在 64 位下是 8，前 4 字节为 cbSize，路径紧跟其后
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ulong))[0] = 8
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(
                    device_info, ctypes.byref(interface_data), buffer,
                    required_size.value, ctypes.byref(required_size), None):
                continue

            path = ctypes.wstring_at(ctypes.addressof(buffer) + 4)
            interfaces.append(path)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info)

    print(f'HID 接口总数: {len(interfaces)}')
    print('=' * 100)

    gamepad_rows = []
    for path in interfaces:
        # 只读打开: 不用 GENERIC_WRITE，避免误申请写权限
        handle = kernel32.CreateFileW(
            path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
            None, OPEN_EXISTING, 0, None)
        readable = handle != INVALID_HANDLE_VALUE
        vendor_id = product_id = version = None
        manufacturer = product = ''

        if readable:
            attributes = HIDD_ATTRIBUTES()
            attributes.Size = ctypes.sizeof(HIDD_ATTRIBUTES)
            if hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
                vendor_id = attributes.VendorID
                product_id = attributes.ProductID
                version = attributes.VersionNumber
            manufacturer = get_string(hid.HidD_GetManufacturerString, handle)
            product = get_string(hid.HidD_GetProductString, handle)
        else:
            # 打不开时用路径里的 VID/PID 信息兜底
            upper = path.upper()
            if 'VID_' in upper and 'PID_' in upper:
                vendor_id = int(upper.split('VID_')[1][:4], 16)
                product_id = int(upper.split('PID_')[1][:4], 16)

        # 从路径里取集合信息（如果有）
        collection = ''
        lower = path.lower()
        if 'col' in lower:
            collection = path[lower.index('col'):]

        is_nintendo = vendor_id == 0x057E  # Nintendo 的 USB 厂商 ID
        mark = '★' if is_nintendo else ' '
        status = '可读' if readable else '占用/打不开'
        vendor_text = f'0x{vendor_id:04X}' if vendor_id is not None else '未知'
        product_text = f'0x{product_id:04X}' if product_id is not None else '未知'
        revision_text = f'0x{version:04X}' if version is not None else '未知'
        print(f'{mark} [{status}] '
              f'VID={vendor_text} PID={product_text} rev={revision_text} '
              f'厂商="{manufacturer}" 产品="{product}"')
        print(f'      {collection}')
        print(f'      {path}')
        if is_nintendo and readable:
            gamepad_rows.append(path)
        if readable:
            kernel32.CloseHandle(handle)
        print('-' * 100)

    print()
    print('=' * 100)
    if gamepad_rows:
        print(f'结论: 找到 {len(gamepad_rows)} 个可原生读取的 Nintendo(0x057E) HID 接口。')
        print('      → 原生 HID 读取路线可行，不需要网页工具的“输出模式”。')
        print('      → 下一步可以按 VID/PID 精确打开这些接口，用 ReadFile 收 0x05/0x09 报告。')
        print('      → 注意: 接口可读 != 有数据。手柄还需先被激活（发 17 条厂商命令），')
        print('        否则读取会一直超时（实测未激活时 0 帧）。')
    else:
        print('结论: 没有发现可读的 Nintendo(0x057E) HID 接口。可能原因:')
        print('      1. 手柄没插/没连（先连上再跑一次）')
        print('      2. 被别的程序独占（关掉 Steam / 浏览器 / 你的主程序再试）')
        print('      3. 接口被换成了 WinUSB/libusb 驱动 → 需要走 WinUSB 路线而不是原生 HID')
    print('=' * 100)
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
