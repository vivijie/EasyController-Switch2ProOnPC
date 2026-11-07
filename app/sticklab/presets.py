"""预设配置：把 Apex 调参经验固化成可直接应用的方案

设计依据（Apex 手柄玩家的真实痛点）
--------------------------------
1. **追踪（Tracking）**：需要中近距离平滑跟随。
   → 小死区 + 线性或轻微 power 曲线，避免"起步太猛导致抖动"。
2. **开镜（ADS）**：灵敏度通常比腰射低，需要更细腻。
   → 独立曲线（gain 降低）+ 稍大死区，用 ZL 按住自动切换。
3. **转身（Flick / 180°）**：需要大角度快速转向。
   → turn_boost 只放大 0.6 以上幅度，不影响微调精度。
4. **手感一致性**：Switch2 Pro 物理行程约 0.73，
   → range_mode 用 global_min，保证各方向反应一致、圆轨迹不畸变。
"""

import copy

from .engine import DEFAULT_STICK


def _stick(**overrides):
    stick = copy.deepcopy(DEFAULT_STICK)
    stick.update(overrides)
    return stick


PRESETS = {
    'apex-tracking': {
        'name': 'Apex 追踪流（推荐）',
        'description': '小死区 + 近线性，专注中近距离平滑追踪；'
                       'ZL 切换开镜曲线（更细腻）；大角度自动加速。',
        'tags': ['Apex', '追踪', '通用'],
        'patch': {
            'profile_name': 'Apex 追踪流',
            'left': _stick(
                range_mode='global_min', response_curve='linear',
                deadzone_inner=0.04, deadzone_outer=1.0,
                deadzone_shape='circle', turn_boost=0.35, gain=1.0,
            ),
            'right': _stick(
                range_mode='global_min', response_curve='power', power=0.92,
                deadzone_inner=0.035, deadzone_outer=1.0,
                deadzone_shape='circle', turn_boost=0.45, gain=1.0,
            ),
            'curve_switch': {
                'enabled': True,
                'button_index': 13,
                'stick': 'right',
                'ads': _stick(
                    range_mode='global_min', response_curve='power', power=1.05,
                    deadzone_inner=0.05, deadzone_outer=1.0,
                    deadzone_shape='circle', turn_boost=0.0, gain=0.72,
                ),
            },
        },
    },
    'apex-flick': {
        'name': 'Apex 甩枪流',
        'description': '更激进的曲线与转向增益，适合喜欢快速甩枪、习惯高灵敏度的玩家。',
        'tags': ['Apex', '甩枪', '高灵敏'],
        'patch': {
            'profile_name': 'Apex 甩枪流',
            'left': _stick(
                range_mode='global_min', response_curve='linear',
                deadzone_inner=0.03, turn_boost=0.5, gain=1.05,
            ),
            'right': _stick(
                range_mode='global_min', response_curve='power', power=0.8,
                deadzone_inner=0.03, deadzone_shape='circle',
                turn_boost=0.7, gain=1.05,
            ),
            'curve_switch': {
                'enabled': True,
                'button_index': 13,
                'stick': 'right',
                'ads': _stick(
                    range_mode='global_min', response_curve='power', power=1.1,
                    deadzone_inner=0.05, gain=0.65,
                ),
            },
        },
    },
    'precision': {
        'name': '精准微调流',
        'description': '更细腻的中心区曲线（power>1）+ 更小增益，适合远距离对枪与爆头线。',
        'tags': ['精准', '远距离'],
        'patch': {
            'profile_name': '精准微调流',
            'left': _stick(range_mode='global_min', response_curve='linear',
                           deadzone_inner=0.04),
            'right': _stick(range_mode='global_min', response_curve='power',
                            power=1.25, deadzone_inner=0.04, gain=0.9),
            'curve_switch': {
                'enabled': True,
                'button_index': 13,
                'stick': 'right',
                'ads': _stick(range_mode='global_min', response_curve='power',
                              power=1.45, deadzone_inner=0.05, gain=0.6),
            },
        },
    },
    'square-classic': {
        'name': '方形手感（老手柄党）',
        'description': '还原传统"方形摇杆"手感：对角更快。适合习惯老式手柄的玩家，'
                       '但追踪时需注意对角灵敏度偏高。',
        'tags': ['方形', '传统'],
        'patch': {
            'profile_name': '方形手感',
            'left': _stick(range_mode='global_min', response_curve='linear',
                           deadzone_inner=0.04, square_strength=0.6),
            'right': _stick(range_mode='global_min', response_curve='linear',
                            deadzone_inner=0.04, square_strength=0.6),
            'curve_switch': {'enabled': False, 'button_index': 13,
                             'stick': 'right', 'ads': _stick()},
        },
    },
    'raw-none': {
        'name': '原始直通（对比用）',
        'description': '不做任何行程补偿与曲线处理，用于和修改后的手感做 A/B 对比。',
        'tags': ['对比', '调试'],
        'patch': {
            'profile_name': '原始直通',
            'left': _stick(range_mode='none', response_curve='linear',
                           deadzone_inner=0.0),
            'right': _stick(range_mode='none', response_curve='linear',
                            deadzone_inner=0.0),
            'curve_switch': {'enabled': False, 'button_index': 13,
                             'stick': 'right', 'ads': _stick()},
        },
    },
}


def list_presets():
    """返回给 Web 界面的预设摘要列表"""
    result = []
    for key, preset in PRESETS.items():
        result.append({
            'key': key,
            'name': preset['name'],
            'description': preset['description'],
            'tags': preset.get('tags', []),
        })
    return result


def get_preset(key):
    return PRESETS.get(key)
