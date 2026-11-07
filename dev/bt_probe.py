"""Switch 2 Pro 手柄 —— 蓝牙连接探测工具（独立模块）

⚠️ 本文件完全独立，不导入、不修改主程序任何代码。

背景
----
USB 连接时，手柄必须先通过厂商接口(libusb 接口1)发 17 条激活命令，
HID 接口才会输出 0x09 报告。
蓝牙连接是另一套传输（HID 输出报告 0x01/0x11 走子命令），
激活方式完全不同，所以需要单独验证。

用法
----
    python .\\bt_probe.py --list            列出所有 Nintendo HID 设备（USB/蓝牙）
    python .\\bt_probe.py --dump 60         原样抓 60 帧原始报告（不发送任何东西）
    python .\\bt_probe.py --watch           持续显示报告ID、速率与摇杆值
    python .\\bt_probe.py --handshake switch1 --seconds 8
                                            尝试经典 Switch 蓝牙握手（子命令 0x03 等）
    python .\\bt_probe.py --handshake switch2 --seconds 8
                                            尝试用 USB 那套 0x91 命令当 HID 输出报告发

蓝牙连接前请先：拔掉 USB 线 → 手柄长按配对键 → Windows 蓝牙里配对。
配对后先跑 --list，确认能看到设备，再 --dump 看有没有数据。
"""

import argparse
import os
import sys
import time

NINTENDO_VID = 0x057E

# 经典 Switch 手柄蓝牙用手的中性震动数据
NEUTRAL_RUMBLE = bytes([0x00, 0x01, 0x40, 0x40, 0x00, 0x01, 0x40, 0x40])

# 经典 Switch 子命令（仅用于蓝牙 HID 输出报告）
SUBCOMMAND = {
    'set_input_report_mode': (0x03, bytes([0x30])),   # 标准全量报告
    'enable_imu': (0x40, bytes([0x01])),
    'enable_vibration': (0x48, bytes([0x01])),
    'set_player_lights': (0x30, bytes([0x01])),
}


def _text(value):
    if isinstance(value, bytes):
        try:
            return value.decode('utf-8', 'replace')
        except Exception:
            return repr(value)
    return str(value or '')


def enumerate_nintendo():
    """列出所有 Nintendo HID 接口，并判断 USB / 蓝牙"""
    try:
        import hid as hidapi
    except ImportError:
        print('缺少 hid 模块：pip install hid')
        return []

    devices = []
    for info in hidapi.enumerate(NINTENDO_VID):
        path = info.get('path')
        path_text = _text(path)
        lowered = path_text.lower()
        bus = '蓝牙' if ('bth' in lowered or 'bthenum' in lowered) else 'USB'
        devices.append({
            'path': path,
            'path_text': path_text,
            'bus': bus,
            'product': _text(info.get('product_string')),
            'manufacturer': _text(info.get('manufacturer_string')),
            'serial': _text(info.get('serial_number')),
            'interface': info.get('interface_number'),
            'usage_page': info.get('usage_page'),
            'usage': info.get('usage'),
        })
    return devices


def pick_device(devices, prefer_bus=None):
    """优先挑游戏手柄用法(0x01/0x05)，其次第一个"""
    candidates = devices
    if prefer_bus:
        filtered = [d for d in devices if d['bus'] == prefer_bus]
        if filtered:
            candidates = filtered
    for info in candidates:
        if info['usage_page'] == 0x0001 and info['usage'] == 0x0005:
            return info
    return candidates[0] if candidates else None


def open_device(info):
    try:
        import hid as hidapi
    except ImportError:
        print('缺少 hid 模块')
        return None
    try:
        device = hidapi.device()
        device.open_path(info['path'])
        device.set_nonblocking(1)
        return device
    except Exception as error:
        print('打开失败: %s: %s' % (type(error).__name__, error))
        print('  提示：如果主程序正在运行，它可能占用着设备；关掉主程序再试。')
        return None


def describe_report(raw):
    """粗略解读一帧报告"""
    report_id = raw[0] if raw else -1
    text = 'ID=0x%02X len=%d' % (report_id, len(raw))
    if len(raw) >= 12 and report_id in (0x09, 0x30):
        lx = raw[6] | ((raw[7] & 0x0F) << 8)
        ly = (raw[7] >> 4) | (raw[8] << 4)
        rx = raw[9] | ((raw[10] & 0x0F) << 8)
        ry = (raw[10] >> 4) | (raw[11] << 4)
        text += ('  L=(%d,%d) R=(%d,%d)'
                 % (lx - 2048, ly - 2048, rx - 2048, ry - 2048))
    return text


def cmd_list():
    devices = enumerate_nintendo()
    print('=' * 70)
    print('Nintendo HID 设备（VID 0x%04X）' % NINTENDO_VID)
    print('=' * 70)
    if not devices:
        print('  没有检测到任何 Nintendo HID 设备。')
        print('  - USB 连接：请插好数据线')
        print('  - 蓝牙连接：先长按配对键，在 Windows 蓝牙里配对')
        return 1
    for index, info in enumerate(devices, 1):
        print('  [%d] %s  (%s)' % (index, info['bus'],
                                   info['product'] or '(无名称)'))
        print('      PID: 0x%04X' % _pid_from_path(info['path_text']))
        print('      usage_page=0x%04X usage=0x%04X interface=%s'
              % (info['usage_page'] or 0, info['usage'] or 0,
                 info['interface']))
        print('      path: %s' % info['path_text'][:88])
    print()
    buses = sorted({d['bus'] for d in devices})
    print('  传输类型: %s' % ' / '.join(buses))
    if '蓝牙' not in buses:
        print('  ⚠️ 当前只有 USB。蓝牙需要：拔掉数据线 → 长按配对键 → 系统蓝牙配对')
    return 0


def _pid_from_path(path_text):
    marker = 'pid_'
    lowered = path_text.lower()
    position = lowered.find(marker)
    if position < 0:
        return 0
    try:
        return int(lowered[position + len(marker):position + len(marker) + 4], 16)
    except ValueError:
        return 0


def cmd_dump(count, bus=None):
    devices = enumerate_nintendo()
    info = pick_device(devices, bus)
    if info is None:
        print('没有设备，先跑 --list')
        return 1
    print('目标: %s  %s' % (info['bus'], info['product']))
    device = open_device(info)
    if device is None:
        return 1

    seen = {}
    rows = []
    deadline = time.perf_counter() + max(3.0, count * 0.05)
    try:
        while len(rows) < count and time.perf_counter() < deadline:
            try:
                raw = device.read(64, 100)
            except Exception as error:
                print('读取异常: %s' % error)
                break
            if raw:
                data = bytes(raw)
                rows.append(data)
                seen[data[0]] = seen.get(data[0], 0) + 1
                if len(rows) <= 8:
                    print('  %s' % describe_report(data))
                    print('     %s' % ' '.join('%02X' % b for b in data[:20]))
    finally:
        try:
            device.close()
        except Exception:
            pass

    print()
    print('共收到 %d 帧' % len(rows))
    if seen:
        print('报告ID 分布: %s' % ', '.join(
            '0x%02X×%d' % (k, v) for k, v in sorted(seen.items())))
    else:
        print('⚠️ 一帧都没有。蓝牙下通常需要先做握手（--handshake switch1）')
    if rows:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'bt_dump.txt')
        with open(path, 'w', encoding='utf-8') as file_object:
            for data in rows:
                file_object.write(' '.join('%02X' % b for b in data) + '\n')
        print('原始数据已保存: %s' % path)
    return 0


def build_classic_handshake():
    """经典 Switch 蓝牙握手包（输出报告 0x01 + 子命令）"""
    packets = []
    for name in ('set_input_report_mode', 'enable_imu', 'enable_vibration',
                 'set_player_lights'):
        subcommand_id, data = SUBCOMMAND[name]
        packets.append(bytes([0x01]) + NEUTRAL_RUMBLE
                       + bytes([subcommand_id]) + data)
    # 再来一份 0x11（仅震动）保持连接活跃
    packets.append(bytes([0x11]) + NEUTRAL_RUMBLE)
    return packets


def build_switch2_handshake():
    """把 USB 那套 0x91 命令当 HID 输出报告发（实验性）"""
    usb_commands = [
        bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
               0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
        bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x09, 0x00,
               0x00, 0x00]),
        bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00,
               0x00]),
    ]
    return usb_commands


def cmd_handshake(mode, seconds, bus=None):
    devices = enumerate_nintendo()
    info = pick_device(devices, bus)
    if info is None:
        print('没有设备，先跑 --list')
        return 1
    print('目标: %s  %s' % (info['bus'], info['product']))
    if info['bus'] != '蓝牙':
        print('  ⚠️ 当前是 USB 连接，蓝牙握手对它不适用（仅作参考）')

    device = open_device(info)
    if device is None:
        return 1

    packets = (build_classic_handshake() if mode == 'switch1'
               else build_switch2_handshake())
    print('发送 %d 个握手包…' % len(packets))
    for index, packet in enumerate(packets, 1):
        try:
            written = device.write(packet)
            print('  [%d] 写入 %d 字节 -> 返回 %s' % (index, len(packet), written))
        except Exception as error:
            print('  [%d] 写入失败: %s: %s'
                  % (index, type(error).__name__, error))
        time.sleep(0.05)

    print()
    print('握手后监听 %.0f 秒…' % seconds)
    seen = {}
    frames = 0
    axes = []
    deadline = time.perf_counter() + seconds
    try:
        while time.perf_counter() < deadline:
            try:
                raw = device.read(64, 100)
            except Exception:
                raw = None
            if raw:
                data = bytes(raw)
                frames += 1
                seen[data[0]] = seen.get(data[0], 0) + 1
                if len(data) >= 12 and data[0] in (0x09, 0x30):
                    lx = data[6] | ((data[7] & 0x0F) << 8)
                    ly = (data[7] >> 4) | (data[8] << 4)
                    rx = data[9] | ((data[10] & 0x0F) << 8)
                    ry = (data[10] >> 4) | (data[11] << 4)
                    axes.append((lx - 2048, ly - 2048, rx - 2048, ry - 2048))
                if frames <= 3:
                    print('  %s' % describe_report(data))
    finally:
        try:
            device.close()
        except Exception:
            pass

    print()
    print('收到 %d 帧（%.1f Hz）' % (frames, frames / max(seconds, 0.1)))
    if seen:
        print('报告ID: %s' % ', '.join(
            '0x%02X×%d' % (k, v) for k, v in sorted(seen.items())))
    if axes:
        xs = [a[0] for a in axes]
        ys = [a[1] for a in axes]
        print('左摇杆范围: X[%d, %d]  Y[%d, %d]' % (min(xs), max(xs),
                                                    min(ys), max(ys)))
    print()
    if frames > 10:
        print('✅ 有数据流。下一步：把这段逻辑接进主程序的读取层')
    elif frames > 0:
        print('⚠️ 有少量数据，可能需要更多握手或不同子命令')
    else:
        print('❌ 握手后仍无数据。可试：--handshake switch2，或先 --dump 看原始状态')
    return 0


def cmd_watch(bus=None):
    devices = enumerate_nintendo()
    info = pick_device(devices, bus)
    if info is None:
        print('没有设备，先跑 --list')
        return 1
    print('监听 %s（Ctrl+C 退出）' % info['bus'])
    device = open_device(info)
    if device is None:
        return 1
    frames = 0
    started = time.perf_counter()
    last_id = None
    try:
        while True:
            try:
                raw = device.read(64, 100)
            except KeyboardInterrupt:
                break
            if raw:
                data = bytes(raw)
                frames += 1
                if data[0] != last_id or frames % 60 == 0:
                    last_id = data[0]
                    elapsed = time.perf_counter() - started
                    rate = frames / elapsed if elapsed > 0 else 0
                    print('\r%6.1fHz  %s      ' % (rate, describe_report(data)),
                          end='', flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            device.close()
        except Exception:
            pass
    print()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Switch 2 Pro 蓝牙探测（独立工具）')
    parser.add_argument('--list', action='store_true', help='列出 Nintendo HID 设备')
    parser.add_argument('--dump', type=int, metavar='N', help='抓 N 帧原始报告')
    parser.add_argument('--watch', action='store_true', help='持续显示报告')
    parser.add_argument('--handshake', choices=['switch1', 'switch2'],
                        help='握手模式')
    parser.add_argument('--seconds', type=float, default=6.0, help='握手后监听秒数')
    parser.add_argument('--bus', choices=['USB', '蓝牙'], help='只挑这种传输')
    args = parser.parse_args()

    if args.list:
        return cmd_list()
    if args.dump:
        return cmd_dump(args.dump, args.bus)
    if args.handshake:
        return cmd_handshake(args.handshake, args.seconds, args.bus)
    if args.watch:
        return cmd_watch(args.bus)
    parser.print_help()
    print()
    return cmd_list()


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
