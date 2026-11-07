r"""测试：能否只用一个 HID 句柄完成"激活 + 读取"（完全不用 libusb）

目的:
    libusb 的 claim_interface(1) 与 HID 句柄冲突，是断链的根源。
    如果激活命令能通过 HID 接口的 output report 发出去，就只需要一个句柄，
    彻底避开冲突。

本脚本只做"能不能发出去"的验证，不判断手柄是否真的响应。
"""

import ctypes
import sys
import time
from ctypes import wintypes

NINTENDO_VID = 0x057E
SWITCH2_PRO_PID = 0x2069

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

START_OUTPUT = bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00,
                      0x01, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])


class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]


def declare(kernel32, hid, setupapi):
    hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_SetOutputReport.restype = wintypes.BOOL
    hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_SetFeature.restype = wintypes.BOOL

    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.POINTER(GUID), wintypes.LPCWSTR, wintypes.HWND, wintypes.DWORD]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID),
        wintypes.DWORD, ctypes.c_void_p]
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


def find_hid_path(hid, setupapi):
    """用与 hid_probe.py 完全相同的写法枚举（那份代码已实测可用）"""
    hid_guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(hid_guid))

    DIGCF_PRESENT = 0x02
    DIGCF_DEVICEINTERFACE = 0x10
    device_info = setupapi.SetupDiGetClassDevsW(
        ctypes.byref(hid_guid), None, None, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if device_info == INVALID_HANDLE_VALUE:
        raise RuntimeError('SetupDiGetClassDevs 失败')

    paths = []
    index = 0
    try:
        while True:
            # 每次循环都要新建，和 hid_probe.py 一致
            class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
                _fields_ = [('cbSize', wintypes.DWORD), ('InterfaceClassGuid', GUID),
                            ('Flags', wintypes.DWORD),
                            ('Reserved', ctypes.POINTER(ctypes.c_ulong))]
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
            paths.append(ctypes.wstring_at(ctypes.addressof(buffer) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(device_info)

    print(f'  枚举到 {len(paths)} 个 HID 接口')
    for candidate in paths:
        upper = candidate.upper()
        if 'VID_057E' in upper and 'PID_2069' in upper:
            return candidate
    return None


def try_write(path, access, label, kernel32, hid):
    handle = kernel32.CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                  None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        print(f'  [{label}] 打开失败 err={ctypes.get_last_error()}')
        return False
    try:
        buffer = ctypes.create_string_buffer(START_OUTPUT, len(START_OUTPUT))
        ok = hid.HidD_SetOutputReport(handle, ctypes.byref(buffer), len(START_OUTPUT))
        first_error = 0 if ok else ctypes.get_last_error()
        buffer2 = ctypes.create_string_buffer(START_OUTPUT, len(START_OUTPUT))
        ok2 = hid.HidD_SetFeature(handle, ctypes.byref(buffer2), len(START_OUTPUT))
        second_error = 0 if ok2 else ctypes.get_last_error()
        print(f'  [{label}] HidD_SetOutputReport: {"成功" if ok else f"失败 err={first_error}"}     '
              f'HidD_SetFeature: {"成功" if ok2 else f"失败 err={second_error}"}')
        return bool(ok or ok2)
    finally:
        kernel32.CloseHandle(handle)


def main():
    kernel32 = ctypes.WinDLL('kernel32.dll')
    hid = ctypes.WinDLL('hid.dll')
    setupapi = ctypes.WinDLL('setupapi.dll')
    declare(kernel32, hid, setupapi)

    path = find_hid_path(hid, setupapi)
    print('HID 接口路径:', path)
    if not path:
        print('没找到手柄 HID 接口')
        return 1

    print('\n尝试用 HID 接口发送激活命令（Start output）:')
    readable = try_write(path, GENERIC_READ, '只读权限', kernel32, hid)
    writable = try_write(path, GENERIC_READ | GENERIC_WRITE, '读写权限', kernel32, hid)

    print('\n结论:')
    if writable:
        print('  ✅ 可以用 HID 句柄发命令 —— 有望彻底摆脱 libusb 冲突')
    elif readable:
        print('  ⚠️ 只读句柄能发、读写句柄不行（少见，需进一步确认）')
    else:
        print('  ❌ HID 接口不接受这些命令，激活只能走 libusb（接口1）')
        print('     那么防断链只能靠：激活后保持流、不要反复 claim/release')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
