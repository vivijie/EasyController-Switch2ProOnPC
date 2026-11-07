"""核心算法层单元测试（不依赖硬件、不依赖 Web）

运行: python .\test_engine.py
"""

import copy
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticklab import engine as E


passed = []
failed = []


def check(name, condition, detail=''):
    if condition:
        passed.append(name)
        print(f'  [PASS] {name}')
    else:
        failed.append(f'{name}  {detail}')
        print(f'  [FAIL] {name}  {detail}')


# 模拟一台真实 Switch2 Pro 的校准结果（中心有偏移、行程不对称）
REAL_CALIBRATION = {
    'center': {'x': 0.011, 'y': 0.066},
    'min_x': -0.826, 'max_x': 0.808,
    'min_y': -0.787, 'max_y': 0.734,
}


def make_stick(**overrides):
    stick = copy.deepcopy(E.DEFAULT_STICK)
    stick.update(copy.deepcopy(REAL_CALIBRATION))
    stick['deadzone_inner'] = 0.03
    stick['deadzone_outer'] = 1.0
    stick['range_mode'] = 'global_min'
    stick['response_curve'] = 'linear'
    stick['gain'] = 1.0
    stick['square_strength'] = 0.0
    stick['turn_boost'] = 0.0
    stick['smoothing'] = 0.0
    stick.update(overrides)
    return stick


def test_calibration_center():
    print('\n[1] 校准：中心原始值 → 输出必须为 0')
    pipeline = E.StickPipeline(make_stick())
    x, y = pipeline.process(REAL_CALIBRATION['center']['x'],
                            REAL_CALIBRATION['center']['y'])
    check('中心输出为 0', abs(x) < 1e-9 and abs(y) < 1e-9, f'({x:.3e}, {y:.3e})')

    # 中心附近的小抖动也应在死区内
    x, y = pipeline.process(REAL_CALIBRATION['center']['x'] + 0.005,
                            REAL_CALIBRATION['center']['y'] - 0.005)
    check('中心微小抖动被死区吃掉', abs(x) == 0.0 and abs(y) == 0.0, f'({x}, {y})')


def test_saturation():
    print('\n[2] 行程补偿：global_min 下四个方向都能到 ≈1.0，且无串扰')
    stick = make_stick(range_mode='global_min')
    pipeline = E.StickPipeline(stick)
    center = stick['center']
    cases = [
        ('向右', stick['max_x'], center['y'], 'x'),
        ('向左', stick['min_x'], center['y'], 'x'),
        ('向下', center['x'], stick['max_y'], 'y'),
        ('向上', center['x'], stick['min_y'], 'y'),
    ]
    for name, raw_x, raw_y, axis in cases:
        x, y = pipeline.process(raw_x, raw_y)
        magnitude = math.hypot(x, y)
        check(f'{name} 满偏输出 ≈ 1.0', 0.99 <= magnitude <= 1.0001,
              f'实际 {magnitude:.4f}')
        other = y if axis == 'x' else x
        check(f'{name} 无串扰', abs(other) < 1e-9, f'另一轴={other:.3e}')


def test_range_modes():
    print('\n[3] 三种行程补偿模式的行为差异')
    for mode, low, high in (('none', 0.70, 0.85), ('global_min', 0.99, 1.0001),
                            ('per_axis', 0.99, 1.0001)):
        stick = make_stick(range_mode=mode)
        pipeline = E.StickPipeline(stick)
        x, _ = pipeline.process(stick['max_x'], stick['center']['y'])
        print(f'      {mode}: 推到右极限 -> x={x:.4f}')
        check(f'{mode} 输出落在 [{low}, {high}]', low <= x <= high, f'x={x:.4f}')


def test_deadzone_inner_outer():
    print('\n[4] 死区：内死区抑制、外死区压缩')
    stick = make_stick(deadzone_inner=0.1, deadzone_outer=1.0)
    pipeline = E.StickPipeline(stick)
    center = stick['center']
    basis_x, _ = E.compute_axis_basis(stick, 'global_min')

    # 内死区 0.1 对应推杆 physical 0.1；推到 0.6 × 内死区，必在内死区内
    raw_inside = center['x'] + basis_x * 0.1 * 0.6
    x, y = pipeline.process(raw_inside, center['y'])
    check('内死区内输出为 0', math.hypot(x, y) == 0.0, f'{math.hypot(x, y):.4f}')

    # 推到 3 倍内死区，必须有明显输出
    raw_outside = center['x'] + basis_x * 0.1 * 3.0
    x, y = pipeline.process(raw_outside, center['y'])
    check('内死区外有输出', math.hypot(x, y) > 0.2, f'{math.hypot(x, y):.4f}')

    stick2 = make_stick(deadzone_inner=0.1, deadzone_outer=0.8)
    pipeline2 = E.StickPipeline(stick2)
    x, y = pipeline2.process(stick2['max_x'], stick2['center']['y'])
    check('外死区 0.8 → 满偏封顶 1.0', abs(math.hypot(x, y) - 1.0) < 0.02,
          f'{math.hypot(x, y):.4f}')


def test_deadzone_shapes():
    print('\n[5] 死区形状：同一点输入下三种形状输出不同，方形对角最快')
    center_raw = (0.0, 0.0)
    push = 0.4            # 斜向总幅度（物理单位）
    inner = 0.2
    results = {}
    for shape in ('cross', 'circle', 'square'):
        stick = make_stick(range_mode='none', deadzone_inner=inner,
                           deadzone_shape=shape)
        pipeline = E.StickPipeline(stick)
        component = push / math.sqrt(2)
        raw = (component, component)
        x, y = pipeline.process(*raw)
        results[shape] = math.hypot(x, y)
        print(f'      {shape}: 斜向({component:.3f},{component:.3f}) -> '
              f'幅度 {results[shape]:.4f}')
    check('三种形状结果互不相同',
          len({round(v, 4) for v in results.values()}) == 3,
          str({k: round(v, 4) for k, v in results.items()}))
    # 说明：恰好 45° 时方形与圆形数学上等价（max 分量 = 半径/√2 的补偿抵消），
    # 所以用"接近对角但非 45°"的点来体现方形死区在对角区响应更快的特性。
    stick_square = make_stick(range_mode='none', deadzone_inner=inner,
                              deadzone_shape='square')
    stick_circle = make_stick(range_mode='none', deadzone_inner=inner,
                              deadzone_shape='circle')
    probe = (0.24, 0.16)      # 接近对角，最大分量刚过死区
    square_out = math.hypot(*E.StickPipeline(stick_square).process(*probe))
    circle_out = math.hypot(*E.StickPipeline(stick_circle).process(*probe))
    print(f'      近对角(0.24,0.16): square={square_out:.4f} circle={circle_out:.4f}')
    check('方形死区在近对角处响应更快', square_out > circle_out,
          f'square={square_out:.4f} circle={circle_out:.4f}')


def test_curves():
    print('\n[6] 响应曲线：单调不减、端点正确、手感差异符合预期')
    configs = [
        ('linear', {}),
        ('power', {'power': 0.6}),
        ('power', {'power': 1.6}),
        ('classic', {'classic_k': 0.6}),
        ('dynamic', {'dynamic': 0.5}),
    ]
    mid_values = {}
    for curve, extra in configs:
        stick = make_stick(response_curve=curve, deadzone_inner=0.0, **extra)
        pipeline = E.StickPipeline(stick)
        points = pipeline.curve_preview(101)
        outputs = [p['output'] for p in points]
        monotonic = all(outputs[i] <= outputs[i + 1] + 1e-9
                        for i in range(len(outputs) - 1))
        label = f'{curve}{extra if extra else ""}'
        check(f'{label} 单调不减', monotonic)
        check(f'{label} 满偏≈1.0', abs(outputs[-1] - 1.0) < 0.05,
              f'末值 {outputs[-1]:.3f}')
        mid_values[label] = outputs[50]
    print(f'      中段(50%)输出: {dict((k, round(v,3)) for k,v in mid_values.items())}')
    check('power=0.6 中段比 linear 灵敏',
          mid_values["power{'power': 0.6}"] > mid_values['linear'])
    check('power=1.6 中段比 linear 细腻',
          mid_values["power{'power': 1.6}"] < mid_values['linear'])


def test_gain_invert():
    print('\n[7] 增益与反向')
    stick = make_stick()
    pipeline = E.StickPipeline(stick)
    basis_x, _ = E.compute_axis_basis(stick, 'global_min')
    # 推到"半满偏" = 物理位移半个 basis
    half_raw = stick['center']['x'] + basis_x * 0.5
    x, _ = pipeline.process(half_raw, stick['center']['y'])
    check('半满偏 → ≈0.5', abs(x - 0.5) < 0.05, f'x={x:.4f}')

    stick2 = make_stick(gain=3.0)
    pipeline2 = E.StickPipeline(stick2)
    x2, _ = pipeline2.process(half_raw, stick2['center']['y'])
    check('增益 3.0 → 封顶 1.0', abs(x2 - 1.0) < 1e-6, f'x={x2:.4f}')

    stick3 = make_stick(invert_x=True)
    pipeline3 = E.StickPipeline(stick3)
    x3, _ = pipeline3.process(half_raw, stick3['center']['y'])
    check('X 反向生效', abs(x3 + 0.5) < 0.05, f'x={x3:.4f}')


def test_dual_curve():
    print('\n[8] 双曲线：按住 ADS 键右摇杆切换参数')
    config = copy.deepcopy(E.DEFAULT_CONFIG)
    config['left'] = make_stick(gain=0.5)
    config['right'] = make_stick(gain=0.5)
    config['curve_switch'] = {
        'enabled': True,
        'button_index': 3,
        'stick': 'right',
        'ads': make_stick(gain=1.0),
    }
    engine = E.Engine(config)
    raw_right = (config['right']['max_x'], config['right']['center']['y'])

    engine.process((0, 0), raw_right, [False] * 21)
    normal = math.hypot(*engine.right.stages['output'])

    buttons = [False] * 21
    buttons[3] = True
    engine.process((0, 0), raw_right, buttons)
    ads = math.hypot(*engine.ads.stages['output'])

    print(f'      腰射输出={normal:.4f}  ADS输出={ads:.4f}')
    check('切换后输出不同', abs(ads - normal) > 0.2, f'{normal:.3f} vs {ads:.3f}')
    check('ADS 状态标记正确', engine.ads_active is True)

    # 关闭开关后不应切换
    config2 = copy.deepcopy(config)
    config2['curve_switch']['enabled'] = False
    engine2 = E.Engine(config2)
    engine2.process((0, 0), raw_right, buttons)
    check('关闭开关后不切换', engine2.ads_active is False)


def test_square_and_turn():
    print('\n[9] 圆方混合与转向增益')
    magnitudes = {}
    for strength in (0.0, 0.5, 1.0):
        stick = make_stick(square_strength=strength)
        pipeline = E.StickPipeline(stick)
        center = stick['center']
        basis_x, basis_y = E.compute_axis_basis(stick, 'global_min')
        component = 0.5 / math.sqrt(2)
        raw_x = center['x'] + (stick['max_x'] - center['x']) * (component / basis_x)
        raw_y = center['y'] + (stick['max_y'] - center['y']) * (component / basis_y)
        x, y = pipeline.process(raw_x, raw_y)
        magnitudes[strength] = math.hypot(x, y)
        print(f'      方形强度 {strength}: 对角幅度 {magnitudes[strength]:.4f}')
    check('方形强度递增 → 对角幅度递增',
          magnitudes[1.0] > magnitudes[0.5] > magnitudes[0.0])

    stick = make_stick(turn_boost=1.0)
    pipeline = E.StickPipeline(stick)
    center = stick['center']
    basis_x, _ = E.compute_axis_basis(stick, 'global_min')
    # 0.5 满偏（低于 0.6 增益门槛）与 0.9 满偏（高于门槛）
    small_raw = center['x'] + basis_x * 0.5
    large_raw = center['x'] + basis_x * 0.9
    small, _ = pipeline.process(small_raw, center['y'])
    large, _ = pipeline.process(large_raw, center['y'])
    print(f'      半满偏 0.5 -> {small:.4f}   大幅 0.9 -> {large:.4f}')
    check('转向增益不影响 0.6 以下', abs(small - 0.5) < 0.05, f'{small:.4f}')
    check('转向增益放大大幅', large > 0.9, f'{large:.4f}')


def test_trigger():
    print('\n[10] 触发器处理')
    config = copy.deepcopy(E.DEFAULT_CONFIG)
    config['trigger'] = {'mode': 'digital', 'threshold': 0.35,
                         'curve': 'linear', 'power': 1.0}
    engine = E.Engine(config)
    check('digital 低于阈值 → 0', engine.process_trigger(0.2) == 0.0)
    check('digital 高于阈值 → 1', engine.process_trigger(0.8) == 1.0)

    config2 = copy.deepcopy(config)
    config2['trigger']['mode'] = 'analog'
    engine2 = E.Engine(config2)
    check('analog 透传', abs(engine2.process_trigger(0.5) - 0.5) < 1e-6)

    config3 = copy.deepcopy(config)
    config3['trigger'].update({'mode': 'analog', 'curve': 'power', 'power': 0.5})
    engine3 = E.Engine(config3)
    value = engine3.process_trigger(0.25)
    check('analog power 曲线生效', abs(value - 0.5) < 1e-6, f'{value:.4f}')


def test_config_sanitize():
    print('\n[11] 配置清洗：脏数据不应崩溃')
    weird = [
        None, {}, {'left': 'not a dict'}, {'polling_hz': 'abc'},
        {'polling_hz': 999999}, {'buttons': {'x': {}}},
        {'buttons': {1: {'virtual': 'A', 'keys': 'k'}}},
        {'curve_switch': {'stick': 'middle'}},
        {'left': {'deadzone_inner': 'x', 'range_mode': 123}},
        {'right': {'center': 'bad'}},
    ]
    for index, raw in enumerate(weird):
        try:
            config = E.normalize_config(raw)
            assert 'left' in config and 'right' in config
            assert 100 <= config['polling_hz'] <= 2000
            assert config['curve_switch']['stick'] in ('left', 'right')
            # 用清洗后的配置实际跑一帧，确认不会崩
            engine = E.Engine(config)
            engine.process((0.1, 0.1), (0.1, 0.1), [False] * 25)
        except Exception as error:
            check(f'脏数据用例{index}', False, f'{type(error).__name__}: {error}')
            return
    check('10 条脏数据全部安全通过', True)


def main():
    print('=' * 70)
    print('核心算法层单元测试')
    print('=' * 70)
    test_calibration_center()
    test_saturation()
    test_range_modes()
    test_deadzone_inner_outer()
    test_deadzone_shapes()
    test_curves()
    test_gain_invert()
    test_dual_curve()
    test_square_and_turn()
    test_trigger()
    test_config_sanitize()

    print('\n' + '=' * 70)
    print(f'通过 {len(passed)} 项，失败 {len(failed)} 项')
    if failed:
        print('\n失败项:')
        for item in failed:
            print('  - ' + item)
        print('VERDICT: FAIL')
        return 1
    print('VERDICT: PASS')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
