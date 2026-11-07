r"""手柄信号处理核心算法层（与界面/硬件完全解耦）

设计要点
--------
1. **纯数学 + 可单测**：不碰 pygame / vgamepad。
2. **校准 → 行程补偿 → 死区 → 曲线 → 形状，顺序有讲究**：
   先用校准把中心漂移去掉（否则死区会被漂移吃掉），
   再按 range_mode 把 ~0.73 的物理行程放大到游戏满偏，
   然后才是死区（单位是"游戏满偏的百分比"）和曲线。
3. **两种行程补偿模式的真实语义**（Apex 手感关键）：
   - global_min：四方向共用最短行程做基准 → 方向比例不变（圆还是圆），
     且任何方向都能推到 1.0。**推荐**，职业选手要的是各方向反应一致。
   - per_axis  ：每轴各自拉满 → 对角会更快（部分玩家偏好）。
   - none      ：不补偿，物理推到底只有 ~0.73。
4. **双曲线（Dual Curve）**：右摇杆可配两套参数，按住某键（默认 ZL）切到 ADS 套，
   这是"开镜灵敏度"的手柄版本。

坐标约定：x 右为正、y 下为正（与 SDL / XInput 一致）。
"""

import math


# ==================== 默认配置 ====================

DEFAULT_STICK = {
    # 校准（由校准流程写入；这里给安全默认值）
    'center': {'x': 0.0, 'y': 0.0},
    'min_x': -0.8, 'max_x': 0.8, 'min_y': -0.8, 'max_y': 0.8,
    # 行程补偿
    'range_mode': 'global_min',      # global_min | per_axis | none
    'gain': 1.0,                     # 额外输出倍率
    # 响应曲线
    'response_curve': 'power',       # linear | power | classic | dynamic
    'power': 1.0,                    # <1 小幅度更灵敏，>1 更细腻
    'classic_k': 0.6,                # classic 曲线混入线性的比例
    'dynamic': 0.0,                  # dynamic 预压强度
    # 死区（单位：满偏百分比）
    'deadzone_shape': 'circle',      # circle | cross | square
    'deadzone_inner': 0.03,          # 内死区
    'deadzone_outer': 1.0,           # 外死区（1.0 = 用满行程）
    # 其他
    'smoothing': 0.0,                # 0~1 低通
    'square_strength': 0.0,          # 0 圆形 ~ 1 方形
    'turn_boost': 0.0,               # 转向变曲线强度
    'invert_x': False,
    'invert_y': False,
}

# 硬件输入层方向修正（在进入算法之前应用，只改符号不改算法）
# 不同读取路径（pygame / 原生 HID 解析）对 Y 轴的上/下约定可能相反，
# 这里提供一个显式开关，默认按"SDL 约定：上为负、下为正"。
DEFAULT_HARDWARE = {
    'invert_left_x': False,
    'invert_left_y': False,
    'invert_right_x': False,
    'invert_right_y': False,
}

DEFAULT_TRIGGER = {
    'mode': 'digital',               # digital（按键）| analog（轴）
    'threshold': 0.35,
    'curve': 'linear',               # linear | power
    'power': 1.0,
}

DEFAULT_CONFIG = {
    'version': 2,
    'profile_name': '默认 - Apex 追踪',
    'polling_hz': 1000,
    'left': dict(DEFAULT_STICK),
    'right': dict(DEFAULT_STICK),
    'trigger': dict(DEFAULT_TRIGGER),
    'curve_switch': {
        'enabled': False,
        'button_index': 13,          # 13 = ZL（Switch2 Pro）
        'stick': 'right',
        'ads': dict(DEFAULT_STICK),
    },
    'hardware': dict(DEFAULT_HARDWARE),
    'buttons': {},
}


# ==================== 配置清洗 ====================

def _merge_stick(raw):
    merged = {}
    for key, default in DEFAULT_STICK.items():
        if key not in raw:
            merged[key] = default
            continue
        value = raw[key]
        if isinstance(default, dict):
            merged[key] = dict(default)
            if isinstance(value, dict):
                for sub_key in default:
                    if sub_key in value:
                        merged[key][sub_key] = value[sub_key]
        elif isinstance(default, bool):
            merged[key] = bool(value)
        elif isinstance(default, (int, float)):
            try:
                merged[key] = float(value)
            except (TypeError, ValueError):
                merged[key] = default
        else:
            merged[key] = value if isinstance(value, str) else default
    return merged


def normalize_config(raw):
    """把任意输入整理成完整、安全、可直接计算的配置"""
    if not isinstance(raw, dict):
        raw = {}
    config = {
        'version': 2,
        'profile_name': str(raw.get('profile_name', DEFAULT_CONFIG['profile_name'])),
    }
    try:
        config['polling_hz'] = max(100, min(2000, int(raw.get('polling_hz', 1000))))
    except (TypeError, ValueError):
        config['polling_hz'] = 1000

    for stick in ('left', 'right'):
        stick_raw = raw.get(stick)
        config[stick] = _merge_stick(stick_raw if isinstance(stick_raw, dict) else {})

    trigger_raw = raw.get('trigger') if isinstance(raw.get('trigger'), dict) else {}
    config['trigger'] = {}
    for key, default in DEFAULT_TRIGGER.items():
        config['trigger'][key] = trigger_raw.get(key, default)

    switch_raw = raw.get('curve_switch') if isinstance(raw.get('curve_switch'), dict) else {}
    ads_raw = switch_raw.get('ads') if isinstance(switch_raw.get('ads'), dict) else {}
    try:
        button_index = int(switch_raw.get('button_index', 13))
    except (TypeError, ValueError):
        button_index = 13
    stick_target = switch_raw.get('stick', 'right')
    config['curve_switch'] = {
        'enabled': bool(switch_raw.get('enabled', False)),
        'button_index': button_index,
        'stick': stick_target if stick_target in ('left', 'right') else 'right',
        'ads': _merge_stick(ads_raw),
    }

    hw_raw = raw.get('hardware') if isinstance(raw.get('hardware'), dict) else {}
    config['hardware'] = {}
    for key, default in DEFAULT_HARDWARE.items():
        config['hardware'][key] = bool(hw_raw.get(key, default))

    buttons = {}
    raw_buttons = raw.get('buttons')
    if isinstance(raw_buttons, dict):
        for raw_index, entry in raw_buttons.items():
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            virtual = entry.get('virtual') or []
            keys = entry.get('keys') or []
            if isinstance(virtual, str):
                virtual = [virtual]
            if isinstance(keys, str):
                keys = [keys]
            buttons[index] = {
                'virtual': [v for v in virtual if isinstance(v, str)][:2],
                'keys': [k for k in keys if isinstance(k, str)][:3],
            }
    config['buttons'] = buttons
    return config


# ==================== 基础数学 ====================

def _clamp(value, low=-1.0, high=1.0):
    return low if value < low else (high if value > high else value)


def _sign(value):
    return -1.0 if value < 0 else 1.0


def compute_axis_spans(stick):
    """四个方向的物理行程（绝对值）"""
    center = stick['center']
    return (abs(stick['max_x'] - center['x']),
            abs(center['x'] - stick['min_x']),
            abs(stick['max_y'] - center['y']),
            abs(center['y'] - stick['min_y']))


def compute_axis_basis(stick, mode):
    """按 range_mode 决定每个轴的补偿基准（见模块 docstring）"""
    spans = compute_axis_spans(stick)
    usable = [s for s in spans if s > 1e-6]
    if not usable:
        return 1.0, 1.0
    global_min = min(usable)
    if mode == 'none':
        return 1.0, 1.0
    if mode == 'per_axis':
        x_basis = min(spans[0], spans[1])
        y_basis = min(spans[2], spans[3])
        return (x_basis if x_basis > 1e-6 else global_min,
                y_basis if y_basis > 1e-6 else global_min)
    return global_min, global_min


def normalize_axis(raw_value, center, negative_limit, positive_limit, basis):
    """把单轴原始值转换为游戏输入值

    公式：corrected × (1.0 / basis)
    basis = 该摇杆最短方向的物理行程（约 0.73）。
    因此"推到最短方向极限"→ 正好 1.0；更长行程的方向提前饱和（被限幅），
    这正是 global_min 的核心：各方向反应一致、圆轨迹仍是圆。
    """
    corrected = raw_value - center
    if basis <= 1e-6:
        return corrected
    return corrected / basis


def apply_deadzone(x, y, inner, outer, shape):
    """死区处理（单位：满偏百分比）

    circle: 径向死区，保留方向。Apex 追踪首选。
    cross : 分轴死区，消斜向串扰。
    square: 切比雪夫度量（取最大分量）→ 对角响应更快。
    """
    inner = max(0.0, min(0.9, float(inner)))
    outer = max(inner + 1e-6, min(1.0, float(outer)))
    span = outer - inner

    if shape == 'cross':
        x = _axis_deadzone(x, inner, span)
        y = _axis_deadzone(y, inner, span)
        return x, y

    if shape == 'square':
        # 方形死区（原程序"方形死区"的实现）：
        # 用最大分量判死区，并把最大分量线性映射到 right_value 处，再按比例缩放。
        # 效果：对角方向输出更快（同为 0.4 幅度，方形给 0.177，圆形给 0.25 之外的更快响应）
        judge = max(abs(x), abs(y))
        if judge <= inner:
            return 0.0, 0.0
        scaled = _clamp((judge - inner) / span, 0.0, 1.0)
        # 分母保证 judge 恰好映射到 scaled 位置（judge 的一半是死区中心补偿）
        denominator = judge - inner * 0.5
        if denominator <= 1e-9:
            denominator = judge if judge > 1e-9 else 1.0
        factor = scaled / denominator
        result_x = _clamp(x * factor)
        result_y = _clamp(y * factor)
        return result_x, result_y

    # circle
    magnitude = math.hypot(x, y)
    if magnitude <= inner:
        return 0.0, 0.0
    scaled = _clamp((magnitude - inner) / span, 0.0, 1.0)
    ratio = scaled / magnitude
    return x * ratio, y * ratio


def _axis_deadzone(value, inner, span):
    magnitude = abs(value)
    if magnitude <= inner:
        return 0.0
    return _sign(value) * _clamp((magnitude - inner) / span, 0.0, 1.0)


def apply_response_curve(magnitude, stick):
    """对幅度应用响应曲线（方向不变），输入输出都在 [0,1]"""
    if magnitude <= 0.0:
        return 0.0
    curve = stick.get('response_curve', 'power')
    if curve == 'power':
        power = max(0.1, min(4.0, float(stick.get('power', 1.0))))
        return magnitude ** power
    if curve == 'classic':
        k = max(0.0, min(1.0, float(stick.get('classic_k', 0.6))))
        return (1.0 - k) * (magnitude ** 3) + k * magnitude
    if curve == 'dynamic':
        strength = max(0.0, min(1.0, float(stick.get('dynamic', 0.0))))
        return magnitude ** (1.0 + strength)
    return magnitude


def apply_square_mapping(x, y, strength):
    """圆→方混合。Apex 追踪一般不建议开高（对角灵敏度会飙升）"""
    if strength <= 0.0:
        return x, y
    strength = min(1.0, float(strength))
    magnitude = math.hypot(x, y)
    if magnitude < 1e-9:
        return x, y
    angle = math.atan2(y, x)
    largest = max(abs(math.cos(angle)), abs(math.sin(angle)))
    scale = 1.0 / largest if largest > 1e-9 else 1.0
    square_x = _clamp(x * scale)
    square_y = _clamp(y * scale)
    return (x * (1.0 - strength) + square_x * strength,
            y * (1.0 - strength) + square_y * strength)


def apply_turn_boost(magnitude, strength):
    """转向变曲线：只放大 0.6 以上的幅度，帮助大角度转身，不影响精细追踪"""
    if strength <= 0.0 or magnitude <= 0.6:
        return magnitude
    strength = min(1.0, float(strength))
    t = (magnitude - 0.6) / 0.4
    return magnitude * (1.0 + strength * 0.5 * t * t)


class SmoothingState:
    """一阶低通滤波"""

    def __init__(self):
        self.x = 0.0
        self.y = 0.0

    def apply(self, x, y, strength):
        if strength <= 0.0:
            self.x, self.y = x, y
            return x, y
        alpha = 1.0 - min(1.0, float(strength)) * 0.9
        self.x = self.x * (1.0 - alpha) + x * alpha
        self.y = self.y * (1.0 - alpha) + y * alpha
        return self.x, self.y


# ==================== 管线 ====================

class StickPipeline:
    """单根摇杆：校准 → 补偿 → 死区 → 曲线 → 转向 → 形状 → 增益 → 平滑"""

    def __init__(self, stick_config):
        self.config = stick_config
        self.smoothing = SmoothingState()
        self.stages = {}

    def process(self, raw_x, raw_y):
        stick = self.config
        center = stick['center']
        mode = stick.get('range_mode', 'global_min')
        basis_x, basis_y = compute_axis_basis(stick, mode)

        # 1) 校准 + 行程补偿
        x = normalize_axis(raw_x, center['x'], stick['min_x'], stick['max_x'], basis_x)
        y = normalize_axis(raw_y, center['y'], stick['min_y'], stick['max_y'], basis_y)
        x, y = _clamp(x), _clamp(y)
        after_calibration = (x, y)

        # 2) 死区
        x, y = apply_deadzone(x, y, stick.get('deadzone_inner', 0.0),
                              stick.get('deadzone_outer', 1.0),
                              stick.get('deadzone_shape', 'circle'))
        after_deadzone = (x, y)

        # 3) 响应曲线（只改幅度）
        magnitude = math.hypot(x, y)
        if magnitude > 1e-9:
            curved = apply_response_curve(magnitude, stick)
            ratio = curved / magnitude
            x, y = x * ratio, y * ratio
        after_curve = (x, y)

        # 4) 转向变曲线
        magnitude = math.hypot(x, y)
        if magnitude > 1e-9:
            boosted = apply_turn_boost(magnitude, stick.get('turn_boost', 0.0))
            if boosted != magnitude:
                ratio = boosted / magnitude
                x, y = x * ratio, y * ratio

        # 5) 圆→方
        x, y = apply_square_mapping(x, y, stick.get('square_strength', 0.0))

        # 6) 增益
        gain = float(stick.get('gain', 1.0))
        x, y = x * gain, y * gain

        # 7) 反向
        if stick.get('invert_x'):
            x = -x
        if stick.get('invert_y'):
            y = -y

        # 8) 平滑
        x, y = self.smoothing.apply(x, y, stick.get('smoothing', 0.0))

        # 9) 限幅
        x, y = _clamp(x), _clamp(y)

        self.stages = {
            'raw': (raw_x, raw_y),
            'after_calibration': after_calibration,
            'after_deadzone': after_deadzone,
            'after_curve': after_curve,
            'output': (x, y),
        }
        return x, y

    def curve_preview(self, samples=101):
        """曲线预览：输入=推杆百分比(0~1 物理行程)，输出=最终幅度(0~1)"""
        stick = self.config
        inner = float(stick.get('deadzone_inner', 0.0))
        outer = float(stick.get('deadzone_outer', 1.0))
        mode = stick.get('range_mode', 'global_min')
        basis_x, basis_y = compute_axis_basis(stick, mode)
        # 物理行程里"推到底"的距离：global_min 用最短方向，none 用满量程
        if mode == 'none':
            full_push = 1.0
        else:
            full_push = max(basis_x, basis_y)
        points = []
        for index in range(samples):
            pushed = index / (samples - 1)
            physical = pushed * full_push
            value = physical / (basis_x if mode != 'none' else 1.0)
            x, y = apply_deadzone(min(1.0, value), 0.0, inner, outer,
                                  stick.get('deadzone_shape', 'circle'))
            magnitude = math.hypot(x, y)
            if magnitude > 1e-9:
                magnitude = apply_response_curve(magnitude, stick)
                magnitude = apply_turn_boost(magnitude, stick.get('turn_boost', 0.0))
                magnitude *= float(stick.get('gain', 1.0))
            points.append({'input': round(pushed, 4),
                           'output': round(_clamp(magnitude, 0.0, 1.0), 4)})
        return points


class Engine:
    """整体处理入口：两根摇杆 + 双曲线切换 + 触发器"""

    def __init__(self, config):
        self.config = normalize_config(config)
        self.left = StickPipeline(self.config['left'])
        self.right = StickPipeline(self.config['right'])
        self.ads = StickPipeline(self.config['curve_switch']['ads'])
        self.ads_active = False

    def set_config(self, config):
        self.config = normalize_config(config)
        self.left.config = self.config['left']
        self.right.config = self.config['right']
        self.ads.config = self.config['curve_switch']['ads']

    def _ads_pressed(self, buttons):
        switch = self.config['curve_switch']
        if not switch.get('enabled'):
            return False
        index = int(switch.get('button_index', -1))
        if 0 <= index < len(buttons):
            return bool(buttons[index])
        return False

    def process(self, left_raw, right_raw, buttons):
        use_ads = self._ads_pressed(buttons)
        self.ads_active = use_ads

        left_x, left_y = self.left.process(*left_raw)
        right_pipeline = self.ads if use_ads else self.right
        right_x, right_y = right_pipeline.process(*right_raw)

        return {
            'left': {'x': left_x, 'y': left_y},
            'right': {'x': right_x, 'y': right_y},
            'ads_active': use_ads,
            'left_stages': self.left.stages,
            'right_stages': right_pipeline.stages,
        }

    def process_trigger(self, raw_value):
        """触发器处理：digital 就按阈值变 0/1，analog 就按曲线映射"""
        trigger = self.config['trigger']
        if trigger.get('mode') == 'digital':
            threshold = float(trigger.get('threshold', 0.35))
            return 1.0 if raw_value >= threshold else 0.0
        value = _clamp(float(raw_value), 0.0, 1.0)
        if trigger.get('curve') == 'power':
            value = value ** max(0.1, min(4.0, float(trigger.get('power', 1.0))))
        return value


def make_engine(config=None):
    return Engine(config or DEFAULT_CONFIG)
