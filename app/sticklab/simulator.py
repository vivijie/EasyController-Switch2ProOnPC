r"""虚拟 Switch2 Pro 手柄发生器

用途：在没有物理手柄时，为整条链路（引擎 + Web 界面 + 虚拟输出）提供
**可复现、可脚本化**的输入。它模拟了真实设备的几个关键特征：

1. 物理行程只有 ±0.73 左右（这是本项目的核心问题来源）
2. 中心有偏移（真实手柄普遍存在）
3. 静置时有微小抖动（±0.004 白噪声）
4. 四方向行程不对称（右方向偏小 —— 实测数据）
5. ZL/ZR 是数字按键（button 13 / 5），不是轴

两种驱动方式：
- manual: 通过 HTTP 直接设置原始值/按键（给 Web 界面用）
- auto:   内置演示脚本：绕圈、扫角、定点、按键连击（给无人值守自测用）

用法:
    python -m sticklab.simulator --auto --seconds 30
"""

import math
import random
import time


# 模拟一台"略有瑕疵"的 Switch2 Pro
SIM_DEVICE = {
    'center': {'x': 0.011, 'y': 0.066},
    'max_x': 0.808, 'min_x': -0.826,
    'max_y': 0.734, 'min_y': -0.787,
    'noise': 0.004,
}

# 与真实设备一致的按键索引（21 键）
BUTTON_COUNT = 21
BTN_ZR = 5
BTN_ZL = 13

# Apex 常用按键在 Switch2 Pro 上的物理索引
BUTTON_LAYOUT = [
    (0, 'B'), (1, 'A'), (2, 'Y'), (3, 'X'),
    (4, 'R'), (5, 'ZR'), (6, 'Plus'), (7, 'RStick'),
    (8, 'Down'), (9, 'Right'), (10, 'Left'), (11, 'Up'),
    (12, 'L'), (13, 'ZL'), (14, 'Min'), (15, 'LStick'),
    (16, 'Home'), (17, 'Capture'), (18, 'BR'), (19, 'BL'), (20, 'C'),
]


class VirtualStick(object):
    """把"推杆归一化坐标"映射为原始值，并叠加噪声与漂移"""

    def __init__(self, device=None, seed=12345):
        self.device = dict(device or SIM_DEVICE)
        self.random = random.Random(seed)
        self.position = [0.0, 0.0]      # 归一化位置 (−1~1)
        self.drift = [0.0, 0.0]

    def set_position(self, x, y):
        self.position[0] = max(-1.0, min(1.0, float(x)))
        self.position[1] = max(-1.0, min(1.0, float(y)))

    def raw(self):
        """把归一化位置转换为设备原始值（含中心偏移、不对称行程、噪声）"""
        device = self.device
        center = device['center']
        x = self.position[0]
        y = self.position[1]
        # 按方向选行程，复现"右方向偏小"的不对称
        span_x = (device['max_x'] - center['x']) if x >= 0 else (center['x'] - device['min_x'])
        span_y = (device['max_y'] - center['y']) if y >= 0 else (center['y'] - device['min_y'])
        raw_x = center['x'] + x * span_x + self.drift[0] + self.random.uniform(-1, 1) * device['noise']
        raw_y = center['y'] + y * span_y + self.drift[1] + self.random.uniform(-1, 1) * device['noise']
        return max(-1.0, min(1.0, raw_x)), max(-1.0, min(1.0, raw_y))


class VirtualController(object):
    """虚拟手柄整体状态：两根摇杆 + 按键 + 触发器"""

    def __init__(self, device=None):
        self.left = VirtualStick(device)
        self.right = VirtualStick(device)
        self.buttons = [False] * BUTTON_COUNT
        self.triggers = {'left': 0.0, 'right': 0.0}
        self.pressed_log = []

    # ---- 手动控制（Web 界面用）----
    def set_stick(self, side, x, y):
        stick = self.left if side == 'left' else self.right
        stick.set_position(x, y)

    def set_button(self, index, pressed):
        if 0 <= index < BUTTON_COUNT:
            self.buttons[index] = bool(pressed)

    def press_buttons(self, indexes):
        for index in range(BUTTON_COUNT):
            self.buttons[index] = index in indexes

    def clear_buttons(self):
        self.buttons = [False] * BUTTON_COUNT

    def read(self):
        """返回一帧原始数据，格式与真实驱动一致"""
        left_raw = self.left.raw()
        right_raw = self.right.raw()
        return {
            'left_raw': left_raw,
            'right_raw': right_raw,
            'buttons': list(self.buttons),
            'trigger_left': self.triggers['left'],
            'trigger_right': self.triggers['right'],
        }


class AutoScript(object):
    """无人值守自测脚本：按时间轴产生可预期的输入

    阶段（每阶段 3 秒循环）：
      0: 静置（验证死区抑制、中心校准）
      1: 缓慢绕圈（验证全方向行程、圆轨迹保持）
      2: 定点追踪（模拟 Apex 微调，半径 0.15）
      3: 推到边缘（验证满偏饱和，四个方向轮流）
      4: 按住 ADS 键 + 半推（验证双曲线切换）
      5: 按键连击（验证按键映射与键盘输出）
    """

    PHASE_SECONDS = 3.0

    def __init__(self, controller):
        self.controller = controller
        self.started = time.perf_counter()

    def step(self):
        elapsed = time.perf_counter() - self.started
        phase = int(elapsed / self.PHASE_SECONDS) % 6
        t = (elapsed % self.PHASE_SECONDS) / self.PHASE_SECONDS
        controller = self.controller

        if phase == 0:
            controller.clear_buttons()
            controller.set_stick('left', 0.0, 0.0)
            controller.set_stick('right', 0.0, 0.0)
            return '静置（死区抑制）'

        if phase == 1:
            controller.clear_buttons()
            angle = t * 2 * math.pi
            radius = 0.75
            controller.set_stick('left', math.cos(angle) * radius, math.sin(angle) * radius)
            controller.set_stick('right', math.cos(angle) * radius, math.sin(angle) * radius)
            return '绕圈（全方向行程）'

        if phase == 2:
            controller.clear_buttons()
            # 小半径慢速绕圈 = Apex 微调追踪
            angle = t * 2 * math.pi
            radius = 0.15
            controller.set_stick('right', math.cos(angle) * radius, math.sin(angle) * radius)
            controller.set_stick('left', 0.0, 0.0)
            return '定点追踪（微调）'

        if phase == 3:
            controller.clear_buttons()
            directions = [(1, 0), (0, 1), (-1, 0), (0, -1)]
            dx, dy = directions[int(t * 4) % 4]
            controller.set_stick('right', dx, dy)
            controller.set_stick('left', 0.0, 0.0)
            return f'推到边缘 ({dx},{dy})'

        if phase == 4:
            # ADS：按住 ZL + 半推右摇杆
            controller.press_buttons({BTN_ZL})
            controller.set_stick('right', 0.5, 0.0)
            controller.set_stick('left', 0.0, 0.0)
            return 'ADS 切换（双曲线）'

        # phase 5: 按键连击
        controller.clear_buttons()
        controller.set_stick('left', 0.0, 0.0)
        controller.set_stick('right', 0.0, 0.0)
        cycle = int(t * 21) % 21
        controller.press_buttons({cycle})
        return f'按键测试 (按钮 {cycle})'


def main():
    import argparse

    parser = argparse.ArgumentParser(description='虚拟 Switch2 Pro 手柄发生器')
    parser.add_argument('--auto', action='store_true', help='运行内置演示脚本')
    parser.add_argument('--seconds', type=float, default=15.0)
    args = parser.parse_args()

    controller = VirtualController()
    print('虚拟手柄已创建')
    print(f'  模拟设备: 中心={SIM_DEVICE["center"]} '
          f'行程=X[{SIM_DEVICE["min_x"]:+.3f},{SIM_DEVICE["max_x"]:+.3f}] '
          f'Y[{SIM_DEVICE["min_y"]:+.3f},{SIM_DEVICE["max_y"]:+.3f}]')
    print(f'  按键数: {BUTTON_COUNT}')

    if not args.auto:
        print('\n未指定 --auto，仅打印一次读数后退出')
        frame = controller.read()
        print(f'  静置读数: L={frame["left_raw"]} R={frame["right_raw"]}')
        return 0

    script = AutoScript(controller)
    started = time.perf_counter()
    print(f'\n开始演示脚本，共 {args.seconds:.0f} 秒\n')
    try:
        while time.perf_counter() - started < args.seconds:
            label = script.step()
            frame = controller.read()
            pressed = [name for index, name in BUTTON_LAYOUT if frame['buttons'][index]]
            print(f'\r[{time.perf_counter() - started:5.1f}s] {label:<22} '
                  f'L={frame["left_raw"][0]:+.3f},{frame["left_raw"][1]:+.3f} '
                  f'R={frame["right_raw"][0]:+.3f},{frame["right_raw"][1]:+.3f} '
                  f'按键={",".join(pressed) or "-"}', end='', flush=True)
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    print('\n\n演示结束')
    return 0


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
