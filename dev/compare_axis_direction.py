"""对比 pygame 与原生 HID 解析的 Y 轴方向

用途：确认 SDL/pygame 是否把 Y 轴反向，从而决定硬件层要不要取反。
请在运行后把右摇杆推到不同方向，观察两边数值符号是否一致。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticklab import hardware as H


def main():
    print('=' * 74)
    print('Y 轴方向对比（请按提示推动摇杆）')
    print('=' * 74)

    # 打开原生 HID 读数
    devices = H.enumerate_devices()
    if not devices:
        print('❌ 没检测到手柄')
        return 1
    chosen = devices[0]
    import hid
    device = hid.device()
    device.open_path(chosen['path_bytes'])
    device.set_nonblocking(0)

    # 打开 pygame（不 init 全部，避免抢设备）
    os.environ.setdefault('SDL_JOYSTICK_HIDAPI', '0')
    import pygame
    pygame.init()
    pygame.joystick.init()
    joystick = None
    for index in range(pygame.joystick.get_count()):
        candidate = pygame.joystick.Joystick(index)
        candidate.init()
        if 'If_Hid' in candidate.get_name():
            joystick = candidate
            break
    if joystick is None and pygame.joystick.get_count() > 0:
        joystick = pygame.joystick.Joystick(0)
        joystick.init()

    if joystick is None:
        print('❌ pygame 没看到手柄（可能被原生句柄独占，这是正常的）')
        joystick = None
    else:
        print(f'pygame 手柄: {joystick.get_name()}  轴数={joystick.get_numaxes()}')

    print('\n说明：')
    print('  SDL 约定  = pygame.get_axis()  -- 上为负、下为正（XInput 约定上为正，SDL 是下为正）')
    print('  原生解析  = 我从 12-bit 报告换算的值')
    print('  两者符号若相反 -> 硬件层需要取反')
    print('\n请按提示推动摇杆，观察 ↓ 的数值\n')

    prompts = [
        ('把【右摇杆向上】推到底并按住', '右摇杆 Y'),
        ('把【右摇杆向下】推到底并按住', '右摇杆 Y'),
        ('把【右摇杆向右】推到底并按住', '右摇杆 X'),
    ]
    results = []
    try:
        for prompt, axis_name in prompts:
            input(f'>>> {prompt}，然后按回车采样…')
            # 采样 0.5 秒取平均
            pygame_values = []
            native_values = []
            deadline = time.perf_counter() + 0.5
            while time.perf_counter() < deadline:
                pygame.event.pump()
                if joystick is not None:
                    try:
                        pygame_values.append(joystick.get_axis(3))   # 右摇杆 Y
                        pygame_values.append(joystick.get_axis(2))   # 右摇杆 X
                    except Exception:
                        pass
                raw = device.read(64, 100)
                if raw:
                    parsed = H.parse_report(bytes(raw))
                    native_values.append(parsed)
                time.sleep(0.005)

            pygame_y = pygame_x = None
            if pygame_values:
                # 每帧压入两个值 (Y, X)，取平均
                ys = pygame_values[0::2]
                xs = pygame_values[1::2]
                pygame_y = sum(ys) / len(ys) if ys else None
                pygame_x = sum(xs) / len(xs) if xs else None

            native_y = native_x = None
            if native_values:
                native_y = sum(v['right_raw'][1] for v in native_values) / len(native_values)
                native_x = sum(v['right_raw'][0] for v in native_values) / len(native_values)

            print(f'    pygame:  右X={pygame_x if pygame_x is None else round(pygame_x,3)}  '
                  f'右Y={pygame_y if pygame_y is None else round(pygame_y,3)}')
            print(f'    原生解析: 右X={native_x if native_x is None else round(native_x,3)}  '
                  f'右Y={native_y if native_y is None else round(native_y,3)}')
            if pygame_y is not None and native_y is not None:
                same_sign = (pygame_y >= 0) == (native_y >= 0)
                print(f'    Y 符号: {"一致" if same_sign else "❌ 相反（需要取反）"}\n')
            else:
                print()
            results.append((prompt, pygame_x, pygame_y, native_x, native_y))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            device.close()
        except Exception:
            pass

    print('=' * 74)
    print('结论')
    print('=' * 74)
    if len(results) >= 1:
        first = results[0]
        if first[4] is not None and first[2] is not None:
            if (first[2] >= 0) != (first[4] >= 0):
                print('原生解析的 Y 轴与 pygame 相反 → hardware.parse_report 需要把 Y 取反')
            else:
                print('原生解析的 Y 轴与 pygame 一致 → 方向问题在别处')
        else:
            print('pygame 未能读到数据，无法对比（可能被原生句柄独占）')
            print('这种情况下：原生解析的 Y 轴向上为负、向下为正，')
            print('而 SDL 约定向上为负（get_axis 返回负值）... 需结合实际情况判断')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
