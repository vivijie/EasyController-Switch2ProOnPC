"""检查进程完整性级别（判断合成按键是否会被 UIPI 拦掉）

如果能输出到记事本但游戏不认，通常是：
  我们的进程 = 中完整性（普通权限）
  游戏进程   = 高完整性（管理员）
Windows 会丢弃低 → 高的 SendInput 合成按键。
"""

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [('Sid', ctypes.c_void_p), ('Attributes', wt.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [('Label', SID_AND_ATTRIBUTES)]


def integrity_of(pid):
    """返回进程完整性级别文字，取不到返回 None"""
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    adv = ctypes.WinDLL('advapi32', use_last_error=True)

    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    adv.OpenProcessToken.argtypes = [ctypes.c_void_p, wt.DWORD,
                                     ctypes.POINTER(ctypes.c_void_p)]
    adv.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                        ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]

    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    token = ctypes.c_void_p()
    try:
        if not adv.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return None
        size = wt.DWORD(0)
        adv.GetTokenInformation(token, TokenIntegrityLevel, None, 0,
                                ctypes.byref(size))
        if size.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, TokenIntegrityLevel, buffer,
                                       size, ctypes.byref(size)):
            return None
        label = ctypes.cast(buffer, ctypes.POINTER(TOKEN_MANDATORY_LABEL))
        sid = label.contents.Label.Sid
        adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        adv.GetSidSubAuthority.restype = ctypes.POINTER(wt.DWORD)
        adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wt.DWORD]
        count = adv.GetSidSubAuthorityCount(sid)
        rid = adv.GetSidSubAuthority(sid, count.contents.value - 1)
        return {
            0x0000: '不受信任 (Untrusted)',
            0x1000: '低 (Low)',
            0x2000: '中 (Medium / 普通用户)',
            0x2100: '中+ (Medium Plus)',
            0x3000: '高 (High / 管理员)',
            0x4000: '系统 (System)',
        }.get(rid.contents.value, '未知 0x%X' % rid.contents.value)
    finally:
        if token:
            try:
                ctypes.WinDLL('kernel32').CloseHandle(token)
            except Exception:
                pass
        k32.CloseHandle(handle)


def list_processes(names):
    try:
        output = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True,
            encoding='mbcs', errors='replace').stdout
    except Exception:
        return []
    found = []
    for line in output.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        process_name = parts[0].strip('"')
        pid_text = parts[1].strip('"')
        if not pid_text.isdigit():
            continue
        lowered = process_name.lower()
        if any(keyword in lowered for keyword in names):
            found.append((process_name, int(pid_text)))
    return found


def main():
    print('=' * 62)
    print('进程完整性级别检查')
    print('=' * 62)

    import os
    print('本进程 (Python): PID %d' % os.getpid())
    print('  完整性级别: %s' % integrity_of(os.getpid()))

    print()
    targets = list_processes(['r5apex', 'easyanticheat', 'eadesktop', 'eaapp',
                              'steam', 'origin'])
    if not targets:
        print('没有检测到 Apex / EA / Steam 进程')
    for name, pid in targets:
        level = integrity_of(pid)
        print('  %-26s PID %-6d %s' % (name, pid, level or '取不到（可能需要管理员权限）'))

    print()
    print('=' * 62)
    print('判读')
    print('=' * 62)
    print('  两边都是「中」  -> 权限不是原因，问题在游戏侧（反作弊/绑定键）')
    print('  游戏是「高」    -> UIPI 拦截，需要以管理员身份运行本工具')


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
