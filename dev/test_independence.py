"""回归测试：输出增幅 = 圆形半径，方形映射 = 形状

核心要求（用户明确指定）：
  1. 增幅 N → 圆形半径 N（不被压回 1.0）
  2. 坐标轴方向受 XInput 域限制截到 1.0
  3. 方形映射独立控制形状：0=圆，1=方
  4. 顺序：归一化 → 曲线 → 增幅 → 圆形边界 → 方形映射 → 域限幅
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticklab import engine as E

CAL = {
    'limits': {
        'left': {'min_x': -1.0, 'max_x': 1.0, 'min_y': -1.0, 'max_y': 1.0},
        'right': {'min_x': -1.0, 'max_x': 1.0, 'min_y': -1.0, 'max_y': 1.0},
    },
    'offsets': {'left': {'x': 0.0, 'y': 0.0}, 'right': {'x': 0.0, 'y': 0.0}},
}

passed = 0
failed = 0


def check(name, condition, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print('  [PASS] %s' % name)
    else:
        failed += 1
        print('  [FAIL] %s  %s' % (name, detail))


def setup(amp, square_strength=0.0, square_deadzone=False, deadzone=0.0):
    E.OUTPUT_AMPLIFIER['left_stick'] = amp
    E.OUTPUT_AMPLIFIER['right_stick'] = amp
    E.SQUARE_MAPPING_STRENGTH['left_stick'] = square_strength
    E.SQUARE_MAPPING_STRENGTH['right_stick'] = square_strength
    for side in ('left', 'right'):
        E.SQUARE_DEADZONE_SWITCH[side + '_stick'] = 1 if square_deadzone else 0
        E.DEADZONE_CONFIG[side] = {'deadzone': deadzone,
                                   'outer_threshold': 1.0,
                                   'response_curve': 1.0}
    return E.Engine(CAL)


def probe(engine, px, py):
    result = engine.process((0.0, 0.0), (px, py))
    return result['right']['x'], result['right']['y']


def main():
    print('=' * 76)
    print('增幅 = 圆形半径  回归测试')
    print('=' * 76)

    print('\n[1] 圆形半径随增幅线性放大（对角输入 0.707,0.707）')
    for amp in (1.0, 1.1, 1.25, 1.4):
        engine = setup(amp)
        x, y = probe(engine, 0.70710678, 0.70710678)
        radius = math.hypot(x, y)
        check('增幅 %.2f → 半径 %.4f' % (amp, radius),
              abs(radius - amp) < 0.01, '期望 %.2f' % amp)

    print('\n[2] 坐标轴方向受 XInput 域限制（最高 1.0）')
    for amp in (1.0, 1.1, 1.5, 2.0):
        engine = setup(amp)
        x, y = probe(engine, 0.97, 0.0)
        expected = min(0.97 * amp, 1.0)
        check('增幅 %.2f → 直线 %.4f' % (amp, x),
              abs(x - expected) < 0.01 and x <= 1.0 + 1e-9,
              '期望 %.4f' % expected)

    print('\n[3] 增幅 1.0 时仍能暴露原始比例（0.97 不被强行拉满）')
    engine = setup(1.0)
    x, _ = probe(engine, 0.97, 0.0)
    check('增幅1.0 直线=0.97', abs(x - 0.97) < 0.01, str(x))

    print('\n[4] 方形映射独立控制形状')
    engine = setup(1.0, square_strength=0.0)
    circle_x, circle_y = probe(engine, 0.70710678, 0.70710678)
    check('方形0 → 对角保持圆形 0.707',
          abs(circle_x - 0.7071) < 0.01 and abs(circle_y - 0.7071) < 0.01,
          str((circle_x, circle_y)))

    engine = setup(1.0, square_strength=1.0)
    square_x, square_y = probe(engine, 0.70710678, 0.70710678)
    check('方形1 → 对角拉到 (1.0, 1.0)',
          abs(square_x - 1.0) < 0.01 and abs(square_y - 1.0) < 0.01,
          str((square_x, square_y)))

    print('\n[5] 增幅不改变"形状类型"')
    for amp in (1.0, 1.1, 1.25):
        engine = setup(amp, square_strength=0.0)
        x, y = probe(engine, 0.70710678, 0.70710678)
        # 圆形：两轴相等
        check('增幅%.2f 圆形下两轴对称' % amp, abs(abs(x) - abs(y)) < 0.01,
              str((x, y)))

    print('\n[6] 中段灵敏度随增幅提升')
    values = []
    for amp in (1.0, 1.25, 1.5):
        engine = setup(amp)
        x, _ = probe(engine, 0.5, 0.0)
        values.append(x)
    check('中段 0.5：%s 递增' % ['%.3f' % v for v in values],
          values[0] < values[1] < values[2])

    print('\n[7] 负死区仍然有效')
    engine = setup(1.0, deadzone=-0.1)
    x, _ = probe(engine, 0.3, 0.0)
    check('负死区放大中心（0.30 → %.3f）' % x, x > 0.30)

    print()
    print('=' * 76)
    print('通过 %d 项，失败 %d 项' % (passed, failed))
    print('VERDICT:', 'PASS' if failed == 0 else 'FAIL')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
