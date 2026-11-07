"""硬件接入自检：枚举 → 打开 → 激活 → 读取（每步都报告）"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sticklab import hardware as H


def main():
    print('=' * 64)
    print('硬件接入自检')
    print('=' * 64)

    print('\n[1] 枚举 HID 接口')
    devices = H.enumerate_devices()
    print(f'    找到 {len(devices)} 个')
    for info in devices:
        print(f'      product={info["product"]!r} interface={info["interface"]} '
              f'usage_page=0x{(info["usage_page"] or 0):04X}')
        print(f'      path_bytes 类型: {type(info["path_bytes"]).__name__}')
    public = [{k: v for k, v in d.items() if k != 'path_bytes'} for d in devices]
    try:
        json.dumps(public)
        print('    ✅ 可 JSON 序列化')
    except TypeError as error:
        print(f'    ❌ 不可序列化: {error}')

    if not devices:
        return 1

    chosen = None
    for info in devices:
        if info.get('usage_page') == 0x0001 and info.get('usage') == 0x0005:
            chosen = info
            break
    if chosen is None:
        chosen = devices[0]

    print('\n[2] 只读打开（检查数据流是否已在运行）')
    frames = 0
    try:
        import hid
        device = hid.device()
        device.open_path(chosen['path_bytes'])
        device.set_nonblocking(0)
        print('    ✅ 打开成功')
        started = time.perf_counter()
        while time.perf_counter() - started < 1.0:
            if device.read(64, 200):
                frames += 1
        try:
            device.close()
        except Exception:
            pass
        print(f'    1 秒内收到 {frames} 帧')
        if frames > 10:
            print('    → 数据流已经在运行，无需激活')
        else:
            print('    → 没有数据，需要激活')
    except Exception as error:
        print(f'    ❌ 打开失败: {type(error).__name__}: {error}')

    print('\n[3] 激活（发送 17 条厂商命令）')
    ok, note = H.activate(verbose=True)
    print(f'    {"✅" if ok else "❌"} {note}')

    print('\n[4] 激活后再读 1 秒')
    frames_after = 0
    try:
        import hid
        device = hid.device()
        device.open_path(chosen['path_bytes'])
        device.set_nonblocking(0)
        started = time.perf_counter()
        while time.perf_counter() - started < 1.0:
            raw = device.read(64, 200)
            if raw:
                frames_after += 1
                if frames_after == 1:
                    parsed = H.parse_report(bytes(raw))
                    print('    首帧报告ID=0x%02X' % raw[0])
                    print('    左摇杆原始: (%.3f, %.3f)' % parsed['left_raw'])
                    print('    右摇杆原始: (%.3f, %.3f)' % parsed['right_raw'])
        try:
            device.close()
        except Exception:
            pass
        print(f'    ✅ 1 秒内收到 {frames_after} 帧')
    except Exception as error:
        print(f'    ❌ 读取失败: {type(error).__name__}: {error}')

    print('\n' + '=' * 64)
    if frames_after > 50:
        print('✅ 硬件链路完全正常')
        print('   （250Hz 是设备原生速率，1 秒约 250 帧）')
        return 0
    if frames_after > 0:
        print('⚠️ 有数据但帧数偏少，可能被其它程序分流')
        return 0
    print('❌ 没有数据。常见原因：')
    print('   1. 有其它程序占着手柄（关掉 connection_check_gui / 主程序 / Steam 输入）')
    print('   2. 手柄休眠（按一下键唤醒）')
    print('   3. 激活被拒绝（Access denied）——见上面 [3] 的输出')
    return 2


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
