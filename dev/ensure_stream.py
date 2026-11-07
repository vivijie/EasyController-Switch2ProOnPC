r"""手柄掉线原因分析与自愈工具

## 实测结论（本机验证）

1. **claim/release 不会打断数据流**
   实验: 建立流(251Hz) → 再次 claim_interface(1) → 流仍 250Hz。
   所以"反复占用接口"不是掉线原因。

2. **Access denied 的触发条件**
   只要有**另一个句柄正在打开同一个 HID 设备**，再对 interface 1 做
   `get_active_configuration()` / `claim_interface()` 就会得到
   `USBError: [Errno 13] Access denied`。实测两种组合都会触发：
     - hidapi 句柄打开 + libusb 激活
     - pygame 已 `Joystick(0)` + 另一个进程 libusb 激活

3. **主程序没有激活能力**
   主程序只用 pygame 读 HID，从不发激活命令。所以只要流在某次
   claim 失败后没建立起来，主程序就一直读到空数据 —— 表现为"断了"。

## 根本对策

**激活必须在"没有任何 HID 句柄打开"的时候做。**
所以正确顺序是：先激活 → 再启动读数据的程序（而不是反过来）。

## 本工具

    python .\ensure_stream.py              # 检查并在必要时激活（读数据前跑）
    python .\ensure_stream.py --watch      # 常驻守护：检测到停流就自动重新激活
    python .\ensure_stream.py --check      # 只检查不激活

设计要点:
    - 激活时**不打开 HID 句柄**，避免 Access denied
    - 激活后用 hidapi 短暂试读确认流真的建立了
    - --watch 模式每 2 秒检查一次，停流就重新激活（守护进程，不占 HID）
"""

import argparse
import os
import sys
import time

import usb.core
import usb.util
from usb.backend import libusb1

NINTENDO_VID = 0x057E
SWITCH2_PRO_PID = 0x2069

TOOL_COMMANDS = [
    bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
           0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
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


def log(message):
    print(message, flush=True)


def load_backend():
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
    candidates.append(os.path.join(here, 'libusb-1.0.dll'))
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
            backend = libusb1.get_backend(find_library=lambda n, p=candidate: p)
            if backend:
                return backend
    return None


def probe_stream(seconds=1.0):
    """短暂打开 HID 试读，返回帧数（-1 表示打不开）。读完立即关闭句柄。"""
    try:
        import hid as hidapi
    except ImportError:
        return -1
    devices = hidapi.enumerate(NINTENDO_VID, SWITCH2_PRO_PID)
    info = None
    for candidate in devices:
        if candidate.get('usage_page') == 0x0001 and candidate.get('usage') == 0x0005:
            info = candidate
            break
    if info is None and devices:
        info = devices[0]
    if info is None:
        return -1
    device = hidapi.device()
    try:
        device.open_path(info['path'])
        device.set_nonblocking(0)
    except Exception:
        return -1
    frames = 0
    started = time.perf_counter()
    try:
        while time.perf_counter() - started < seconds:
            try:
                if device.read(64, 200):
                    frames += 1
            except Exception:
                pass
    finally:
        try:
            device.close()
        except Exception:
            pass
    return frames


def activate():
    """发激活命令。注意: 调用前不能有其它 HID 句柄打开，否则 Access denied。"""
    backend = load_backend()
    if backend is None:
        log('  ❌ 找不到 libusb-1.0.dll')
        return False
    try:
        device = usb.core.find(idVendor=NINTENDO_VID, idProduct=SWITCH2_PRO_PID,
                               backend=backend)
    except Exception as error:
        log(f'  ❌ 打开 USB 设备失败: {type(error).__name__}: {error}')
        return False
    if device is None:
        log('  ❌ 没找到手柄（USB 未连接？）')
        return False

    try:
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
        if interface_number is None:
            log('  ❌ 接口 1 上没有 bulk 端点')
            return False
        usb.util.claim_interface(device, interface_number)
    except Exception as error:
        log(f'  ❌ claim_interface 失败: {type(error).__name__}: {error}')
        if 'Access denied' in str(error):
            log('     → 有其它程序正占用这个手柄的 HID。')
            log('     → 先关掉主程序 / 检测界面，再激活。')
        return False

    sent = 0
    for command in TOOL_COMMANDS:
        try:
            device.write(out_endpoint, command, timeout=500)
            sent += 1
        except Exception:
            pass
        time.sleep(0.004)
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
    log(f'  ✅ 已发送 {sent}/{len(TOOL_COMMANDS)} 条激活命令')
    return sent > 0


def ensure_stream():
    """检查流；没有就激活；再检查一次。返回 (是否成功, 帧数)"""
    log('检查数据流…')
    frames = probe_stream(1.0)
    if frames > 20:
        log(f'  ✅ 数据流正常（1 秒 {frames} 帧，约 {frames} Hz）')
        return True, frames
    if frames < 0:
        log('  ⚠️ 打不开 HID 接口（手柄未连接？）')
    else:
        log(f'  ⚠️ 没有数据流（1 秒 {frames} 帧），尝试激活…')
    if not activate():
        return False, 0
    time.sleep(0.3)
    frames = probe_stream(1.5)
    if frames > 20:
        log(f'  ✅ 激活成功，数据流已恢复（1.5 秒 {frames} 帧）')
        return True, frames
    log(f'  ❌ 激活后仍然没有数据流（{frames} 帧）')
    return False, frames


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--watch', action='store_true', help='常驻守护，停流自动重激活')
    parser.add_argument('--check', action='store_true', help='只检查不激活')
    parser.add_argument('--interval', type=float, default=2.0, help='守护检查间隔(秒)')
    args = parser.parse_args()

    if args.check:
        frames = probe_stream(1.0)
        if frames > 20:
            log(f'✅ 数据流正常（约 {frames} Hz）')
            return 0
        log(f'❌ 没有数据流（{frames} 帧）')
        return 2

    if not args.watch:
        ok, _ = ensure_stream()
        return 0 if ok else 2

    log(f'守护模式启动（每 {args.interval:.0f} 秒检查一次，Ctrl+C 退出）')
    log('注意: 本守护进程只在"没流"时才短暂激活，平时不占用 HID')
    recoveries = 0
    try:
        while True:
            frames = probe_stream(0.8)
            if frames < 20:
                log(f'检测到停流（{frames} 帧），尝试恢复…')
                if activate():
                    time.sleep(0.3)
                    after = probe_stream(1.2)
                    if after > 20:
                        recoveries += 1
                        log(f'  ✅ 已恢复（约 {after} Hz，累计恢复 {recoveries} 次）')
                    else:
                        log(f'  ❌ 恢复失败（{after} 帧）')
            time.sleep(args.interval)
    except KeyboardInterrupt:
        log(f'\n退出（累计恢复 {recoveries} 次）')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
