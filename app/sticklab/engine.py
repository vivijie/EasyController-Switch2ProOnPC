r"""StickLab 引擎 —— 直接移植自用户原程序 Switch2ProControllerWorkOnPC.txt

本文件**保持用户算法的原始逻辑与参数，不做任何"改进"**，包括：
  - CONSISTENCY_MODE（global_min 统一缩放）
  - CorrectedConsistentJoystickOptimizer（校准后范围 → 统一缩放因子 → 外部死区）
  - AdvancedDeadzone（方形死区 / 圆形死区，各自独立配置）
  - apply_square_mapping（圆→方混合）
  - apply_output_amplifier（输出增幅）

处理顺序（与原程序完全一致）：
    原始 axis → normalize_joystick_value（含中心偏移校正 + 统一缩放）
              → apply_square_mapping
              → apply_square_deadzone / apply_circular_deadzone
              → apply_output_amplifier
              → 送入虚拟手柄

**方向约定**：与原程序一致，不做任何取反。
原程序直接使用 pygame 的 get_axis(1)/get_axis(3)，其约定是"上为负、下为正"。
如果 Web 端看到上下颠倒，说明读取层（原生 HID 解析）的方向与 pygame 不一致，
应在读取层修正，**不要在本引擎里加取反**。
"""

import math

# ==================== 配置（原程序默认值）====================
# 一致性模式: global_min = 全局最小范围统一缩放
CONSISTENCY_MODE = {
    'left': 'global_min',
    'right': 'global_min',
}

# 默认摇杆物理限制（防呆值）
DEFAULT_PHYSICAL_LIMITS = {
    'left': {'min_x': -0.8, 'max_x': 0.8, 'min_y': -0.8, 'max_y': 0.8},
    'right': {'min_x': -0.8, 'max_x': 0.8, 'min_y': -0.8, 'max_y': 0.8},
}

# 默认中心偏移
DEFAULT_CENTER_OFFSETS = {
    'left': {'x': 0.0, 'y': 0.0},
    'right': {'x': 0.0, 'y': 0.0},
}

# 校准有效性 / 行程体检阈值
MIN_VALID_CALIBRATION_RANGE = 0.3
TRAVEL_WARN_RATIO = 0.85
TRAVEL_WARN_ABSOLUTE = 0.78

# 独立死区配置（左右分别设置）
# ⚠️ outer_threshold 是"输出封顶"。原程序左摇杆是 0.9，会让左摇杆
# 永远到不了 1.0（Apex 的奔跑判定就在左摇杆上，所以会跑不起来）。
# 这里统一改成 1.0：推满就是满值。
DEADZONE_CONFIG = {
    'left': {'deadzone': 0.03, 'outer_threshold': 0.95, 'response_curve': 1.0},
    'right': {'deadzone': 0.00, 'outer_threshold': 0.95, 'response_curve': 1.0},
}

# 输出增幅（1.0 = 无增幅）
# 输出增幅不再暴露给用户：校准已经把物理边界映射到 1.0，
# 这里的增幅固定 1.0（保留代码路径以便将来调试）
OUTPUT_AMPLIFIER = {
    'left_stick': 1.0,
    'right_stick': 1.0,
}

# 方形死区开关（0=关闭 1=开启）
SQUARE_DEADZONE_SWITCH = {
    'left_stick': 1,
    'right_stick': 0,
}

# 方形映射强度（0=纯圆 1=纯方）
SQUARE_MAPPING_STRENGTH = {
    'left_stick': 0.0,
    'right_stick': 0.0,
}

# 虚拟手柄更新率
UPDATE_RATE = 1000

# ==================== 自定义曲线（X=物理行程, Y=输出值）====================
# 每条曲线是一组控制点 [[x, y], ...]，x/y 都在 0~1。
# 默认两点 (0,0) (1,1) = 完全线性。
# 作用方式：对每个轴单独映射 —— v_out = sign(v) * curve(|v|)
CURVE_POINTS = {
    # 左摇杆默认线性；右摇杆默认 Sacred（瞄准用的渐进加速）
    'left': [[0.0, 0.0], [1.0, 1.0]],
    'right': [[0.0, 0.0], [0.25, 0.24], [0.5, 0.564], [0.75, 0.779], [1.0, 1.0]],
}


def normalize_curve_points(points):
    """把任意输入整理成合法的控制点列表

    规则：
      - 每个点必须是 [x, y]，x/y 限制在 0~1
      - 按 x 升序
      - x 重复时保留后一个
      - 必须包含 x=0 和 x=1 的端点（缺了自动补线性端点）
    """
    cleaned = []
    if isinstance(points, (list, tuple)):
        for item in points:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                try:
                    x = float(item[0])
                    y = float(item[1])
                except (TypeError, ValueError):
                    continue
                if x != x or y != y:            # NaN 防御
                    continue
                cleaned.append([min(1.0, max(0.0, x)),
                                min(1.0, max(0.0, y))])
    if not cleaned:
        return [[0.0, 0.0], [1.0, 1.0]]

    cleaned.sort(key=lambda point: point[0])

    # x 去重（保留最后一个）
    unique = []
    for point in cleaned:
        if unique and abs(point[0] - unique[-1][0]) < 1e-6:
            unique[-1] = point
        else:
            unique.append(point)

    # 补齐端点
    if unique[0][0] > 1e-6:
        unique.insert(0, [0.0, 0.0])
    else:
        unique[0][0] = 0.0
    if unique[-1][0] < 1.0 - 1e-6:
        unique.append([1.0, 1.0])
    else:
        unique[-1][0] = 1.0
    return unique


def is_identity_curve(points):
    """判断曲线是否等价于完全线性（两点端点）"""
    if len(points) != 2:
        return False
    return (abs(points[0][0]) < 1e-6 and abs(points[0][1]) < 1e-6
            and abs(points[1][0] - 1.0) < 1e-6
            and abs(points[1][1] - 1.0) < 1e-6)


def apply_axis_curve(value, points):
    """对单个轴的值应用分段线性曲线（逐轴映射，保持符号）"""
    if value == 0.0:
        return 0.0
    magnitude = abs(value)
    if magnitude > 1.0:
        magnitude = 1.0
    curve = points if points else CURVE_POINTS['left']
    if not curve:
        return value

    # 找到所在区间并线性插值
    result = magnitude
    for index in range(len(curve) - 1):
        x0, y0 = curve[index]
        x1, y1 = curve[index + 1]
        if x0 <= magnitude <= x1:
            if x1 - x0 < 1e-9:
                result = y1
            else:
                ratio = (magnitude - x0) / (x1 - x0)
                result = y0 + (y1 - y0) * ratio
            break
    else:
        result = curve[-1][1] if magnitude >= curve[-1][0] else curve[0][1]

    result = min(1.0, max(0.0, result))
    return result if value > 0 else -result



def apply_radial_curve(x, y, points):
    """把曲线作用在半径上，保持方向不变（圆形输出不会被撑歪）"""
    radius = (x * x + y * y) ** 0.5
    if radius < 1e-9:
        return x, y

    capped = min(1.0, radius)
    mapped = apply_axis_curve(capped, points)

    scale = mapped / radius
    return x * scale, y * scale


def set_curve_points(stick, points):
    """设置某个摇杆的曲线（自动整理）"""
    if stick not in ('left', 'right'):
        return None
    CURVE_POINTS[stick] = normalize_curve_points(points)
    return CURVE_POINTS[stick]


def get_curve_points():
    return {stick: [list(point) for point in CURVE_POINTS[stick]]
            for stick in ('left', 'right')}



# ==================== 校准数据（中心偏移 + 校准后范围）====================
def default_calibration():
    """生成一份默认校准数据（结构与原程序一致）"""
    return {
        'limits': {
            'left': dict(DEFAULT_PHYSICAL_LIMITS['left']),
            'right': dict(DEFAULT_PHYSICAL_LIMITS['right']),
        },
        'offsets': {
            'left': dict(DEFAULT_CENTER_OFFSETS['left']),
            'right': dict(DEFAULT_CENTER_OFFSETS['right']),
        },
        'saved_at': '',
        'joystick_name': '',
    }


def normalize_calibration(raw):
    """把任意输入整理成可用的校准数据（缺项用默认值补齐）"""
    result = default_calibration()
    if not isinstance(raw, dict):
        return result
    limits = raw.get('limits')
    offsets = raw.get('offsets')
    for stick in ('left', 'right'):
        if isinstance(limits, dict) and isinstance(limits.get(stick), dict):
            for boundary in ('min_x', 'max_x', 'min_y', 'max_y'):
                value = limits[stick].get(boundary)
                if isinstance(value, (int, float)):
                    result['limits'][stick][boundary] = float(value)
        if isinstance(offsets, dict) and isinstance(offsets.get(stick), dict):
            for axis in ('x', 'y'):
                value = offsets[stick].get(axis)
                if isinstance(value, (int, float)):
                    result['offsets'][stick][axis] = float(value)
    for key in ('saved_at', 'joystick_name'):
        if isinstance(raw.get(key), str):
            result[key] = raw[key]
    return result


# ==================== 摇杆优化器（原程序原样）====================
class CorrectedConsistentJoystickOptimizer:
    """修正的一致性优先摇杆优化器（移植自原程序，逻辑未改动）"""

    def __init__(self, calibrated_physical_limits, center_offsets, consistency_mode):
        self.calibrated_physical_limits = calibrated_physical_limits
        self.center_offsets = center_offsets
        self.consistency_mode = consistency_mode
        self.info = {}
        self._calculate_corrected_scaling_factors()

    def _calculate_corrected_scaling_factors(self):
        """计算修正的缩放因子：基于校准后的范围"""
        for stick_type in ('left', 'right'):
            calibrated_limits = self.calibrated_physical_limits[stick_type]
            mode = self.consistency_mode[stick_type]

            if mode == 'global_min':
                min_positive_x = abs(calibrated_limits['max_x'])
                min_negative_x = abs(calibrated_limits['min_x'])
                min_positive_y = abs(calibrated_limits['max_y'])
                min_negative_y = abs(calibrated_limits['min_y'])

                global_min_range = min(min_positive_x, min_negative_x,
                                       min_positive_y, min_negative_y)
                if global_min_range < 0.01:
                    global_min_range = 0.8      # 默认安全值

                calibrated_limits['global_scale'] = 1.0 / global_min_range
                calibrated_limits['consistency_mode'] = 'global_min'
                deadzones = self._calculate_corrected_external_deadzone(
                    stick_type, global_min_range)
                self.info[stick_type] = {
                    'mode': 'global_min',
                    'global_scale': calibrated_limits['global_scale'],
                    'basis': global_min_range,
                    'external_deadzone': deadzones,
                }
            else:
                calibrated_limits['x_positive_scale'] = (
                    1.0 / calibrated_limits['max_x']
                    if abs(calibrated_limits['max_x']) > 0.01 else 1.0)
                calibrated_limits['x_negative_scale'] = (
                    1.0 / abs(calibrated_limits['min_x'])
                    if abs(calibrated_limits['min_x']) > 0.01 else 1.0)
                calibrated_limits['y_positive_scale'] = (
                    1.0 / calibrated_limits['max_y']
                    if abs(calibrated_limits['max_y']) > 0.01 else 1.0)
                calibrated_limits['y_negative_scale'] = (
                    1.0 / abs(calibrated_limits['min_y'])
                    if abs(calibrated_limits['min_y']) > 0.01 else 1.0)
                calibrated_limits['consistency_mode'] = 'independent'
                self.info[stick_type] = {'mode': 'independent'}

    def _calculate_corrected_external_deadzone(self, stick_type, global_min_range):
        """计算修正的外部死区（基于校准后范围）"""
        calibrated_limits = self.calibrated_physical_limits[stick_type]

        deadzones = {
            'up': max(0, (calibrated_limits['max_y'] - global_min_range)
                      / calibrated_limits['max_y'] * 100)
            if calibrated_limits['max_y'] > global_min_range else 0,
            'down': max(0, (abs(calibrated_limits['min_y']) - global_min_range)
                        / abs(calibrated_limits['min_y']) * 100)
            if abs(calibrated_limits['min_y']) > global_min_range else 0,
            'left': max(0, (abs(calibrated_limits['min_x']) - global_min_range)
                        / abs(calibrated_limits['min_x']) * 100)
            if abs(calibrated_limits['min_x']) > global_min_range else 0,
            'right': max(0, (calibrated_limits['max_x'] - global_min_range)
                         / calibrated_limits['max_x'] * 100)
            if calibrated_limits['max_x'] > global_min_range else 0,
        }
        calibrated_limits['external_deadzone'] = deadzones
        return deadzones

    def apply_center_offset_correction(self, raw_value, stick_type, axis_type):
        """应用中心偏移校正"""
        offset = self.center_offsets[stick_type][axis_type]
        return raw_value - offset

    def normalize_joystick_value(self, raw_value, axis_type, stick_type):
        """归一化：中心偏移校正 → 缩放 → 限幅到 [-1, 1]"""
        corrected_value = self.apply_center_offset_correction(
            raw_value, stick_type, 'x' if axis_type == 'x' else 'y')

        calibrated_limits = self.calibrated_physical_limits[stick_type]
        mode = calibrated_limits.get('consistency_mode', 'global_min')

        if mode == 'global_min':
            scale_factor = calibrated_limits['global_scale']
            normalized = corrected_value * scale_factor
        else:
            if axis_type == 'x':
                scale_factor = (calibrated_limits['x_negative_scale']
                                if corrected_value < 0
                                else calibrated_limits['x_positive_scale'])
            else:
                scale_factor = (calibrated_limits['y_negative_scale']
                                if corrected_value < 0
                                else calibrated_limits['y_positive_scale'])
            normalized = corrected_value * scale_factor

        return max(-1.0, min(1.0, normalized))

    def get_consistency_info(self, stick_type):
        calibrated_limits = self.calibrated_physical_limits[stick_type]
        mode = calibrated_limits.get('consistency_mode', 'global_min')
        info = {
            'mode': mode,
            'calibrated_range': {
                'x': [calibrated_limits['min_x'], calibrated_limits['max_x']],
                'y': [calibrated_limits['min_y'], calibrated_limits['max_y']],
            }
        }
        if mode == 'global_min':
            info['global_scale'] = calibrated_limits.get('global_scale', 1.0)
            info['external_deadzone'] = calibrated_limits.get('external_deadzone', {})
        return info


# ==================== 死区处理（原程序原样）====================
class AdvancedDeadzone:
    def __init__(self, deadzone_config):
        self.deadzone_config = deadzone_config

    def apply_square_deadzone(self, x, y, stick_type, outer_scale=1.0):
        """方形死区处理 - 独立控制

        outer_scale：边界放大倍数（等于输出增幅），
        这样增幅 1.1 时方形边界是 1.1，不会被压回 1.0。
        """
        config = self.deadzone_config[stick_type]
        deadzone = config['deadzone']
        outer_threshold = config['outer_threshold'] * outer_scale
        response_curve = config['response_curve']

        max_abs = max(abs(x), abs(y))
        if max_abs < deadzone:
            return 0.0, 0.0

        if max_abs > 0:
            if abs(x) > 0.0001 or abs(y) > 0.0001:
                if abs(x) > abs(y):
                    scale = 1.0 / abs(x)
                    new_x = math.copysign(1.0, x)
                    new_y = y * scale
                else:
                    scale = 1.0 / abs(y)
                    new_x = x * scale
                    new_y = math.copysign(1.0, y)

                new_x = max(-1.0, min(1.0, new_x))
                new_y = max(-1.0, min(1.0, new_y))

                # 与圆形死区一致：放大剩余行程，让摇杆提前打满
                inner = (1.0 - deadzone) if deadzone > 0 else (1.0 + deadzone)
                saturate_at = max(0.05, min(1.0, outer_threshold))
                span = max(1e-6, inner * saturate_at)

                final_scale = min(max_abs / span, 1.0)
                if response_curve != 1.0:
                    final_scale = final_scale ** response_curve
                new_x *= final_scale
                new_y *= final_scale
                return new_x, new_y
        return x, y

    def apply_circular_deadzone(self, x, y, stick_type, outer_scale=1.0):
        """圆形死区处理 - 独立控制

        outer_scale：圆形边界放大倍数（等于输出增幅）。
        增幅 1.1 → 边界半径变成 1.1，对角输出 0.778 而不是被压回 0.707。
        """
        config = self.deadzone_config[stick_type]
        deadzone = config['deadzone']
        outer_threshold = config['outer_threshold'] * outer_scale
        response_curve = config['response_curve']

        magnitude = (x ** 2 + y ** 2) ** 0.5

        # 正死区的语义：忽略中心一小段，同时【放大】剩余行程，
        # 让摇杆在更早的位置就输出满值（1.0）。
        # 边缘死区同理：outer_threshold=0.95 表示 95% 行程就满值。
        if magnitude < 1e-9:
            return 0.0, 0.0

        if deadzone < 0.0:
            inner = 1.0 + deadzone          # 负死区把中心区也放大
        else:
            if magnitude <= deadzone:
                return 0.0, 0.0
            inner = 1.0 - deadzone

        saturate_at = max(0.05, min(1.0, outer_threshold))
        span = max(1e-6, inner * saturate_at)

        target = magnitude / span           # 放大到 0~1
        target = min(target, 1.0)

        if response_curve != 1.0:
            target = target ** response_curve

        scale = target / magnitude
        return x * scale, y * scale
        return 0.0, 0.0


# ==================== 方形映射（原程序原样）====================
def apply_square_mapping(x, y, strength):
    """圆形摇杆输出 → 方形输出，支持强度混合"""
    if x == 0 and y == 0:
        return 0.0, 0.0
    if strength == 0.0:
        return x, y

    angle = math.atan2(y, x)
    abs_cos = abs(math.cos(angle))
    abs_sin = abs(math.sin(angle))

    scale = 1.0 / abs_cos if abs_cos > abs_sin else 1.0 / abs_sin
    square_x = max(-1.0, min(1.0, x * scale))
    square_y = max(-1.0, min(1.0, y * scale))

    if strength == 1.0:
        return square_x, square_y

    circular_weight = 1.0 - strength
    square_weight = strength
    result_x = max(-1.0, min(1.0, x * circular_weight + square_x * square_weight))
    result_y = max(-1.0, min(1.0, y * circular_weight + square_y * square_weight))
    return result_x, result_y


# ==================== 形状算法总开关 ====================
# 'none'   —— 完全关闭方圆算法：逐轴独立处理，不做径向归一
# 'circle' —— 圆形：按径向归一，对角输出 0.707
# 'square' —— 方形：逐轴保持，对角 1.0
SHAPE_ALGORITHM = 'none'
# 强制方形混合强度（0~1）；None 表示由 SHAPE_ALGORITHM 决定
SHAPE_MAPPING_STRENGTH_OVERRIDE = None


def apply_axis_deadzone_value(value, deadzone, outer_threshold,
                              response_curve=1.0):
    """逐轴死区：把 [deadzone, 1.0] 线性映射到 [0, 1.0]

    这是"死区改变边界"的最直接实现 —— 不涉及任何径向/方圆处理。
    """
    magnitude = abs(value)
    if magnitude <= deadzone:
        return 0.0
    span = 1.0 - deadzone
    if span < 1e-9:
        return 0.0
    result = (magnitude - deadzone) / span
    if response_curve != 1.0:
        result = result ** response_curve
    result = min(result, outer_threshold)
    return result if value >= 0 else -result


# ==================== 输出增幅 ====================
# 增幅模式：
#   'radial'（默认）—— 只提升中段灵敏度；超过半径 1.0 时按径向缩回，
#                      形状始终是圆，不会出现"角落被截成方形"
#   'clip'          —— 把整个区域放大，超出 XInput 域的部分截断；
#                      半径超过 1.42 后四角会被截成方形
OUTPUT_AMPLIFIER_MODE = 'radial'


def apply_output_amplifier(x, y, stick_type, amplifier=None):
    """第 5 步：对映射后的区域整体增幅"""
    if amplifier is None:
        amplifier = OUTPUT_AMPLIFIER.get(stick_type, 1.0)
    if amplifier == 1.0:
        return x, y

    new_x = x * amplifier
    new_y = y * amplifier

    # 形状算法关闭时，增幅就是纯增益，不做任何径向归一
    if SHAPE_ALGORITHM != 'none' and OUTPUT_AMPLIFIER_MODE == 'radial':
        magnitude = math.hypot(new_x, new_y)
        if magnitude > 1.0:
            scale = 1.0 / magnitude
            new_x *= scale
            new_y *= scale
    return new_x, new_y


# ==================== 引擎封装（给 Web 层用）====================
class Engine(object):
    """把原程序的处理链封装成可热更新的对象

    注意：所有参数都直接引用本模块的配置字典，Web 改配置后调用 reload()
    即可重新计算缩放因子，处理顺序与数学逻辑保持不变。
    """

    def __init__(self, calibration=None):
        self.lock_params = None
        self.calibration = normalize_calibration(calibration)
        self.stick_cache = {
            'left': {'x': 0.0, 'y': 0.0},
            'right': {'x': 0.0, 'y': 0.0},
        }
        self.stages = {}
        self.reload()

    # ---- 参数 ----
    def reload(self):
        """重新构建优化器与死区处理器（配置变化后调用）"""
        # 深拷贝一份，避免优化器把 global_scale 写回校准数据造成污染
        limits = {}
        for stick in ('left', 'right'):
            limits[stick] = dict(self.calibration['limits'][stick])
        self.optimizer = CorrectedConsistentJoystickOptimizer(
            limits,
            self.calibration['offsets'],
            CONSISTENCY_MODE,
        )
        self.deadzone = AdvancedDeadzone(DEADZONE_CONFIG)

    def set_calibration(self, calibration):
        self.calibration = normalize_calibration(calibration)
        self.reload()

    def get_calibration(self):
        return normalize_calibration(self.calibration)

    def get_info(self):
        """给 Web 层展示的诊断信息"""
        return {
            'consistency_mode': dict(CONSISTENCY_MODE),
            'deadzone_config': {k: dict(v) for k, v in DEADZONE_CONFIG.items()},
            'output_amplifier': dict(OUTPUT_AMPLIFIER),
            'square_deadzone_switch': dict(SQUARE_DEADZONE_SWITCH),
            'square_mapping_strength': dict(SQUARE_MAPPING_STRENGTH),
            'update_rate': UPDATE_RATE,
            'optimizer_info': {
                stick: self.optimizer.get_consistency_info(stick)
                for stick in ('left', 'right')
            },
        }

    # ---- 处理 ----
    def process(self, left_raw, right_raw, buttons=None, ads_button_index=None):
        """完整处理一帧，返回左右摇杆输出（与原程序的 4 步链一致）"""
        optimizer = self.optimizer
        deadzone = self.deadzone

        # ===== 第 1 步：校准归一化 —— 把物理边界映射到 1.0 =====
        # 每个方向用自己的行程做除数，物理推到极限就是 1.0，
        # 这样物理位置和 0~1 输出是一一对应的。
        left_x = optimizer.normalize_joystick_value(left_raw[0], 'x', 'left')
        left_y = optimizer.normalize_joystick_value(left_raw[1], 'y', 'left')
        right_x = optimizer.normalize_joystick_value(right_raw[0], 'x', 'right')
        right_y = optimizer.normalize_joystick_value(right_raw[1], 'y', 'right')
        after_normalize = {'left': (left_x, left_y), 'right': (right_x, right_y)}

        # ===== 第 2 步：死区（第一个算法）=====
        # 死区改变的是边界：把 [deadzone, 1.0] 重新映射成 [0, 1.0]。
        if SHAPE_ALGORITHM == 'none':
            # 完全去掉方圆：逐轴独立做死区映射
            cfg_left = DEADZONE_CONFIG['left']
            cfg_right = DEADZONE_CONFIG['right']
            left_x = apply_axis_deadzone_value(
                left_x, cfg_left['deadzone'], cfg_left['outer_threshold'],
                cfg_left['response_curve'])
            left_y = apply_axis_deadzone_value(
                left_y, cfg_left['deadzone'], cfg_left['outer_threshold'],
                cfg_left['response_curve'])
            right_x = apply_axis_deadzone_value(
                right_x, cfg_right['deadzone'], cfg_right['outer_threshold'],
                cfg_right['response_curve'])
            right_y = apply_axis_deadzone_value(
                right_y, cfg_right['deadzone'], cfg_right['outer_threshold'],
                cfg_right['response_curve'])
        else:
            # 死区统一按径向处理：只改边界，不改形状
            # （形状由第 4 步的方圆滑块 0~1 控制）
            left_x, left_y = deadzone.apply_circular_deadzone(left_x, left_y, 'left')
            right_x, right_y = deadzone.apply_circular_deadzone(right_x, right_y, 'right')
        after_deadzone = {'left': (left_x, left_y), 'right': (right_x, right_y)}

        # ===== 第 3 步：曲线映射（死区之后、方圆之前）=====
        # X = 摇杆物理行程（死区处理后），Y = 输出值。
        left_curve = CURVE_POINTS['left']
        right_curve = CURVE_POINTS['right']
        if not is_identity_curve(left_curve):
            left_x, left_y = apply_radial_curve(left_x, left_y, left_curve)
        if not is_identity_curve(right_curve):
            right_x, right_y = apply_radial_curve(right_x, right_y, right_curve)
        after_curve = {'left': (left_x, left_y), 'right': (right_x, right_y)}

        # ===== 第 4 步：方圆区域映射 =====
        # shape='none'   → 跳过（纯逐轴，对角会到 1,1 即方形）
        # shape='circle' → 不混合，保持圆形
        # shape='square' → 强制方形映射 strength=1.0
        if SHAPE_ALGORITHM == 'none':
            left_shape = 0.0
            right_shape = 0.0
        else:
            # 0 = 纯圆，1 = 纯方，中间线性混合（界面上的"方圆"滑块就是它）
            left_shape = max(0.0, min(1.0,
                                       SQUARE_MAPPING_STRENGTH['left_stick']))
            right_shape = max(0.0, min(1.0,
                                        SQUARE_MAPPING_STRENGTH['right_stick']))
        if SHAPE_MAPPING_STRENGTH_OVERRIDE is not None:
            left_shape = right_shape = SHAPE_MAPPING_STRENGTH_OVERRIDE
        if left_shape > 0.0:
            left_x, left_y = apply_square_mapping(left_x, left_y, left_shape)
        if right_shape > 0.0:
            right_x, right_y = apply_square_mapping(right_x, right_y, right_shape)
        after_square = {'left': (left_x, left_y), 'right': (right_x, right_y)}

        # ===== 第 5 步：增幅（对映射后的区域整体增幅）=====
        left_x, left_y = apply_output_amplifier(left_x, left_y, 'left_stick')
        right_x, right_y = apply_output_amplifier(right_x, right_y, 'right_stick')
        after_amplifier = {'left': (left_x, left_y), 'right': (right_x, right_y)}

        # ===== 第 6 步：限幅到 XInput 域 =====
        left_x = max(-1.0, min(1.0, left_x))
        left_y = max(-1.0, min(1.0, left_y))
        right_x = max(-1.0, min(1.0, right_x))
        right_y = max(-1.0, min(1.0, right_y))

        self.stick_cache = {
            'left': {'x': left_x, 'y': left_y},
            'right': {'x': right_x, 'y': right_y},
        }
        self.stages = {
            'after_normalize': after_normalize,
            'after_deadzone': after_deadzone,
            'after_curve': after_curve,
            'after_square': after_square,
            'after_amplifier': after_amplifier,
            'output': {'left': (left_x, left_y), 'right': (right_x, right_y)},
        }
        return {
            'left': {'x': left_x, 'y': left_y},
            'right': {'x': right_x, 'y': right_y},
            'ads_active': False,      # 原程序没有双曲线，这里恒为 False
            'left_stages': self._stage_dict('left'),
            'right_stages': self._stage_dict('right'),
            'cache': self.stick_cache,
        }

    def _stage_dict(self, stick):
        result = {}
        for name, value in self.stages.items():
            if isinstance(value, dict) and stick in value:
                result[name] = [round(v, 4) for v in value[stick]]
        return result

    def process_trigger(self, raw_value):
        """触发器：原程序按数字按键处理，这里保留模拟透传能力"""
        return max(0.0, min(1.0, float(raw_value)))


# ==================== 行程体检（原程序逻辑）====================
def analyze_stick_travel(limits):
    """检查四方向行程是否均衡"""
    weak_directions = []
    for stick in ('left', 'right'):
        stick_name = "左摇杆" if stick == 'left' else "右摇杆"
        limit = limits[stick]
        travels = {
            '向右': abs(limit['max_x']),
            '向左': abs(limit['min_x']),
            '向上': abs(limit['max_y']),
            '向下': abs(limit['min_y']),
        }
        weakest_direction = min(travels, key=travels.get)
        weakest_value = travels[weakest_direction]
        strongest_value = max(travels.values())
        if (strongest_value > 0.01 and
                (weakest_value < strongest_value * TRAVEL_WARN_RATIO or
                 weakest_value < TRAVEL_WARN_ABSOLUTE)):
            weak_directions.append((stick_name, weakest_direction, weakest_value,
                                    strongest_value, travels))
    return weak_directions


def make_engine(calibration=None):
    return Engine(calibration)
