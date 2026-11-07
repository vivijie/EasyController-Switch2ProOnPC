r"""StickLab —— Switch2 Pro 手柄调参工具（Web 界面 + 用户原算法）

启动:
    python .\sticklab\webapp.py
    python .\run_sticklab.py

设计说明
--------
- **算法层**：直接使用用户原程序 Switch2ProControllerWorkOnPC.txt 的处理链
  （global_min 归一化 → 方形映射 → 死区 → 输出增幅），未做任何"改进"
- **输入源**：物理 Switch2 Pro（hidapi 原生读取 + 必要时激活）或虚拟手柄
- **校准数据**：默认直接读取用户原程序的
  Switch2ProControllerWorkOnPC_calibration.json（方向约定与算法完全一致）
"""

if __package__ in (None, ''):
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = 'sticklab'

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque

from flask import (Flask, Response, jsonify, render_template, request,
                   stream_with_context)

from . import engine as E

# 读取层使用 pygame（与用户原程序完全一致，避免手写字节解析出错）
try:
    from . import pygame_reader as hardware
except Exception as _hw_error:
    hardware = None
    _HW_IMPORT_ERROR = str(_hw_error)

# 输出层：虚拟 XInput 手柄（移植自原程序的 vgamepad 用法）
try:
    from . import xinput as xinput_module
except Exception as _xi_error:
    xinput_module = None
    _XINPUT_IMPORT_ERROR = str(_xi_error)

from .simulator import (BUTTON_LAYOUT, BUTTON_COUNT, AutoScript,
                        VirtualController, SIM_DEVICE)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
CONFIG_PATH = os.path.join(DATA_DIR, 'sticklab_config.json')
# 用户原程序的校准文件（方向约定的权威来源）
LEGACY_CALIBRATION = os.path.join(PROJECT_DIR,
                                  'Switch2ProControllerWorkOnPC_calibration.json')
os.makedirs(DATA_DIR, exist_ok=True)


def load_legacy_calibration():
    """读取用户原程序的校准文件；没有就返回 None"""
    if not os.path.exists(LEGACY_CALIBRATION):
        return None
    try:
        with open(LEGACY_CALIBRATION, 'r', encoding='utf-8') as file_object:
            raw = json.load(file_object)
    except Exception as error:
        print(f'[校准] 读取原程序校准文件失败: {error}')
        return None
    return {
        'limits': raw.get('limits'),
        'offsets': raw.get('offsets'),
        'saved_at': raw.get('saved_at', ''),
        'joystick_name': raw.get('joystick_name', ''),
    }


def load_saved_settings():
    """读取保存的配置（校准 + 曲线点）"""
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as file_object:
            data = json.load(file_object)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_settings(calibration, curve_points, params=None, button_map=None):
    """把校准、曲线点、算法参数、按键映射一起落盘"""
    payload = {
        'version': 4,
        'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'calibration': calibration,
        'curve_points': curve_points,
        'params': params,
        'button_map': button_map,
    }
    with open(CONFIG_PATH, 'w', encoding='utf-8') as file_object:
        json.dump(payload, file_object, ensure_ascii=False, indent=2)
    return payload


class LabState(object):
    """全局状态：校准、引擎、虚拟手柄、物理手柄（带锁）"""

    def __init__(self):
        self.lock = threading.Lock()
        calibration = load_legacy_calibration()
        if calibration:
            print('[校准] 已载入原程序校准数据: '
                  f"{calibration.get('saved_at') or '未知时间'}")
        else:
            print('[校准] 未找到原程序校准文件，使用默认值（建议先跑一次校准）')
            calibration = E.default_calibration()
        self.calibration = E.normalize_calibration(calibration)
        self.engine = E.Engine(self.calibration)

        # 载入保存过的曲线点（没有就用线性）
        saved_settings = load_saved_settings()
        if saved_settings:
            saved_curves = saved_settings.get('curve_points')
            if isinstance(saved_curves, dict):
                for stick in ('left', 'right'):
                    if stick in saved_curves:
                        E.set_curve_points(stick, saved_curves[stick])
            saved_params = saved_settings.get('params')
            if isinstance(saved_params, dict) and isinstance(
                    saved_params.get('amplifier_mode'), str):
                E.OUTPUT_AMPLIFIER_MODE = saved_params['amplifier_mode']
            if isinstance(saved_params, dict) and isinstance(
                    saved_params.get('shape_algorithm'), str):
                E.SHAPE_ALGORITHM = saved_params['shape_algorithm']
            saved_map = saved_settings.get('button_map')
            if isinstance(saved_map, dict) and xinput_module is not None:
                xinput_module.set_button_map(saved_map)
            if isinstance(saved_params, dict):
                try:
                    self.update_params(saved_params, persist=False)
                except Exception as error:
                    print('[参数] 恢复上次参数失败: %s' % error)

        self.controller = VirtualController()
        self.auto = AutoScript(self.controller)
        self.auto_enabled = True

        self.input_source = 'sim'
        self.hw_state = 'idle'
        self.hw_message = ''
        self.hw_devices = []
        self.hw_activation = ''
        # 虚拟 XInput 输出
        self.xinput = xinput_module.output if xinput_module else None
        self.output_enabled = False
        self.output_message = ''

        self.status = {}
        # 是否有人在看按键/摇杆数据（键位页打开时为 True）
        self.monitor_keys = False
        self._scanning = False
        # 拔过线 → SDL 必坏，下次检测直接重启
        self.need_restart = False
        self.history = deque(maxlen=240)
        self.calib = {}
        self.started = time.perf_counter()
        self.running = True
        self.output_hz = 0.0
        self._frames = 0
        self._hz_mark = time.perf_counter()

    # ---- 校准（分阶段，左右摇杆一次完成）----
    def start_calibration(self, prepare_seconds=3.0, center_seconds=1.0,
                          ready_seconds=2.0, edge_seconds=6.0):
        """启动分阶段校准：一次同时校准左右两个摇杆

        阶段顺序（对应原程序）：
            prepare → 采中心（左右） → ready → 同时转圈采边缘 → compute → done
        """
        if hardware is None:
            return {'ok': False, 'message': '硬件模块不可用'}
        if self.input_source != 'hw':
            return {'ok': False,
                    'message': '请先在"连接"页连接物理手柄（输入源需为物理手柄）'}
        with self.lock:
            if self.calib.get('running'):
                return {'ok': False, 'message': '校准正在进行中，请稍候'}
            self.calib = {
                'running': True,
                'phase': 'prepare',
                'phase_started': time.perf_counter(),
                'phase_seconds': float(prepare_seconds),
                'message': '准备中',
                'center': None,
                'live_range': None,
                'result': None,
                'error': '',
                'points': 0,
                'cancel': False,
                'lines': [],
                'live': '',
                'live_pair': None,
                'coverage': None,
            }
        thread = threading.Thread(
            target=self._calibration_worker,
            args=(float(prepare_seconds), float(center_seconds),
                  float(ready_seconds), float(edge_seconds)),
            daemon=True)
        thread.start()
        return {'ok': True}

    def get_calibration_status(self):
        with self.lock:
            state = dict(self.calib)
        if not state:
            return {'running': False, 'phase': 'idle', 'message': '未开始'}
        started = state.get('phase_started') or 0.0
        seconds = state.get('phase_seconds') or 0.0
        elapsed = time.perf_counter() - started if started else 0.0
        state['phase_elapsed'] = round(elapsed, 2)
        state['phase_remaining'] = round(max(0.0, seconds - elapsed), 2)
        state['phase_progress'] = (round(min(1.0, elapsed / seconds), 3)
                                   if seconds > 0 else 1.0)
        return state

    def cancel_calibration(self):
        with self.lock:
            if self.calib.get('running'):
                self.calib['cancel'] = True
        return {'ok': True}

    def _calibration_worker(self, prepare_seconds, center_seconds,
                            ready_seconds, edge_seconds):
        state = self.calib
        sides = ('left', 'right')

        def emit(line='', highlight=False):
            """追加一行文本；highlight=True 时前端显示为黄色"""
            lines = state.setdefault('lines', [])
            lines.append(('\x01' + line) if highlight else line)
            if len(lines) > 400:
                del lines[:len(lines) - 400]

        def set_live(line):
            """刷新最后一行（等价于原程序的 \\r 覆盖）"""
            state['live'] = line

        def set_phase(phase, seconds, message):
            state['phase'] = phase
            state['phase_started'] = time.perf_counter()
            state['phase_seconds'] = seconds
            state['message'] = message

        def cancelled():
            return bool(state.get('cancel'))

        def read_pair():
            snapshot = hardware.status()
            latest = snapshot.get('latest')
            if latest is None:
                return None
            return latest['left_raw'], latest['right_raw']

        def wait_phase(seconds):
            deadline = time.perf_counter() + seconds
            while time.perf_counter() < deadline:
                if cancelled():
                    raise RuntimeError('校准已取消')
                time.sleep(0.02)

        try:
            emit('=' * 80)
            emit('🎮 修正版摇杆范围与中心偏移自动识别')
            emit('=' * 80)
            emit('第一步: 请将左右摇杆保持在中心位置，不要触碰')
            emit('第二步: 片刻后开始范围校准，请同时转动左右摇杆3圈，覆盖所有方向',
                 highlight=True)
            emit('=' * 80)
            emit()

            # 阶段 1：准备（双手离开摇杆）
            set_phase('prepare', prepare_seconds,
                      '请把手离开左右两个摇杆，保持静止')
            emit('📊 正在检测摇杆中心偏移...')
            emit('请确保左右摇杆都保持在中心位置，不要触碰', highlight=True)
            wait_phase(prepare_seconds)

            # 阶段 2：同时采左右中心点
            set_phase('center', center_seconds,
                      '正在采集两个摇杆的中心点，请不要触碰')
            samples = {side: ([], []) for side in sides}
            deadline = time.perf_counter() + center_seconds
            while time.perf_counter() < deadline:
                if cancelled():
                    raise RuntimeError('校准已取消')
                pair = read_pair()
                if pair is not None:
                    for index, side in enumerate(sides):
                        samples[side][0].append(pair[index][0])
                        samples[side][1].append(pair[index][1])
                if pair is not None:
                    state['live_pair'] = {
                        'left': [round(pair[0][0], 4), round(pair[0][1], 4)],
                        'right': [round(pair[1][0], 4), round(pair[1][1], 4)],
                    }
                elapsed = time.perf_counter() - state['phase_started']
                ratio = min(1.0, elapsed / center_seconds) if center_seconds else 1.0
                set_live('采集中心样本: %d%%' % int(ratio * 100))
                time.sleep(0.004)

            centers = {}
            for side in sides:
                xs, ys = samples[side]
                if len(xs) < 10:
                    raise RuntimeError('中心采样失败：没有收到手柄数据')
                centers[side] = {
                    'x': round(sum(xs) / len(xs), 5),
                    'y': round(sum(ys) / len(ys), 5),
                    'drift_x': round(max(xs) - min(xs), 5),
                    'drift_y': round(max(ys) - min(ys), 5),
                    'samples': len(xs),
                }
            state['center'] = centers
            set_live('')
            emit('✅ 中心偏移检测完成!')
            for side in sides:
                label = '左摇杆' if side == 'left' else '右摇杆'
                emit('%s中心偏移: X=%+.4f, Y=%+.4f'
                     % (label, centers[side]['x'], centers[side]['y']))
            emit()

            emit('🔄 正在校准摇杆物理范围...')
            emit('请同时转动左右摇杆3圈，覆盖所有方向', highlight=True)

            # 阶段 3：准备同时转圈
            set_phase('ready', ready_seconds,
                      '现在【同时】把左右摇杆都贴着边缘慢慢转 3~5 圈')
            wait_phase(ready_seconds)

            # 阶段 4：同时采边缘
            set_phase('edge', edge_seconds,
                      '正在采集边缘，请让左右摇杆都贴着边缘转圈')
            limits = {side: {'min_x': None, 'max_x': None,
                             'min_y': None, 'max_y': None} for side in sides}
            SECTORS = 36
            sectors = {side: [0] * SECTORS for side in sides}
            state['coverage'] = {'left': list(sectors['left']),
                                 'right': list(sectors['right'])}
            points = 0
            deadline = time.perf_counter() + edge_seconds
            while time.perf_counter() < deadline:
                if cancelled():
                    raise RuntimeError('校准已取消')
                pair = read_pair()
                if pair is not None:
                    for index, side in enumerate(sides):
                        raw_x, raw_y = pair[index]
                        box = limits[side]
                        box['min_x'] = raw_x if box['min_x'] is None else min(box['min_x'], raw_x)
                        box['max_x'] = raw_x if box['max_x'] is None else max(box['max_x'], raw_x)
                        box['min_y'] = raw_y if box['min_y'] is None else min(box['min_y'], raw_y)
                        box['max_y'] = raw_y if box['max_y'] is None else max(box['max_y'], raw_y)
                    # 记录扇区覆盖（幅度足够大才算）
                    for index, side in enumerate(sides):
                        sx, sy = pair[index]
                        scx = centers[side]['x']
                        scy = centers[side]['y']
                        vx, vy = sx - scx, sy - scy
                        if (vx * vx + vy * vy) ** 0.5 > 0.62:
                            import math as _math
                            angle = (_math.atan2(vy, vx) + _math.pi) / (2 * _math.pi)
                            slot = int(angle * SECTORS) % SECTORS
                            sectors[side][slot] = 1
                    state['coverage'] = {'left': list(sectors['left']),
                                         'right': list(sectors['right'])}

                    points += 1
                    state['points'] = points
                    live = {}
                    for side in sides:
                        box = limits[side]
                        center = centers[side]
                        if box['min_x'] is not None:
                            live[side] = {
                                'min_x': round(box['min_x'] - center['x'], 4),
                                'max_x': round(box['max_x'] - center['x'], 4),
                                'min_y': round(box['min_y'] - center['y'], 4),
                                'max_y': round(box['max_y'] - center['y'], 4),
                            }
                    state['live_range'] = live
                    if pair is not None:
                        state['live_pair'] = {
                            'left': [round(pair[0][0], 4), round(pair[0][1], 4)],
                            'right': [round(pair[1][0], 4), round(pair[1][1], 4)],
                        }
                    remaining = max(0.0, edge_seconds -
                                    (time.perf_counter() - state['phase_started']))
                    fmt = []
                    for side in sides:
                        box = live.get(side)
                        if box:
                            fmt.append('%s:[X%+.3f↔%+.3f Y%+.3f↔%+.3f]'
                                       % ('左' if side == 'left' else '右',
                                          box['min_x'], box['max_x'],
                                          box['min_y'], box['max_y']))
                    set_live('校准中... %.1f秒 %s' % (remaining, ' '.join(fmt)))
                time.sleep(0.004)

            for side in sides:
                if limits[side]['min_x'] is None:
                    raise RuntimeError('边缘采样失败：没有收到手柄数据')

            set_live('')
            emit()
            emit('✅ 摇杆校准完成!')
            emit()

            # 阶段 5：计算与校验（左右分别判定）
            set_phase('compute', 0.2, '正在计算校准结果…')
            min_valid = E.MIN_VALID_CALIBRATION_RANGE
            candidates = {}
            warnings = []
            saved_sides = []
            failed_sides = []

            calibration = self.get_calibration()
            for side in sides:
                box = limits[side]
                center = centers[side]
                candidate = {
                    'min_x': box['min_x'] - center['x'],
                    'max_x': box['max_x'] - center['x'],
                    'min_y': box['min_y'] - center['y'],
                    'max_y': box['max_y'] - center['y'],
                }
                if candidate['min_x'] > candidate['max_x']:
                    candidate['min_x'], candidate['max_x'] = \
                        candidate['max_x'], candidate['min_x']
                if candidate['min_y'] > candidate['max_y']:
                    candidate['min_y'], candidate['max_y'] = \
                        candidate['max_y'], candidate['min_y']
                candidate = {k: round(v, 5) for k, v in candidate.items()}
                candidates[side] = candidate

                label = '左摇杆' if side == 'left' else '右摇杆'
                span_x = candidate['max_x'] - candidate['min_x']
                span_y = candidate['max_y'] - candidate['min_y']
                if span_x < min_valid or span_y < min_valid:
                    failed_sides.append(side)
                    warnings.append(
                        '%s行程过小（X=%.3f, Y=%.3f，需要 ≥ %.2f），未写入'
                        % (label, span_x, span_y, min_valid))
                    continue
                if (center['drift_x'] > 0.06 or center['drift_y'] > 0.06):
                    warnings.append(
                        '%s采中心时被碰到（抖动 X=%.3f Y=%.3f），建议静止重做'
                        % (label, center['drift_x'], center['drift_y']))
                calibration['limits'][side] = candidate
                calibration['offsets'][side] = {
                    'x': center['x'], 'y': center['y']}
                saved_sides.append(side)

                spans = [candidate['max_x'], abs(candidate['min_x']),
                         candidate['max_y'], abs(candidate['min_y'])]
                weakest = min(spans)
                strongest = max(spans)
                if strongest > 0.01 and weakest < strongest * 0.85:
                    direction = ['向右', '向左', '向上', '向下'][
                        spans.index(weakest)]
                    warnings.append(
                        '%s%s方向行程偏小（%.3f / 最强 %.3f），建议再转几圈'
                        % (label, direction, weakest, strongest))

            if saved_sides:
                calibration['saved_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
                self.save_calibration(calibration)
                if len(saved_sides) == 2:
                    warnings.append('左右摇杆都已校准并生效 ✅')
                else:
                    warnings.append('只保存了%s，另一个未通过'
                                    % ('左摇杆' if saved_sides[0] == 'left'
                                       else '右摇杆'))

            emit('=' * 80)
            emit('🎯 校准结果对比')
            emit('=' * 80)
            for side in sides:
                label = '左摇杆' if side == 'left' else '右摇杆'
                box = limits[side]
                cand = candidates[side]
                emit('%s:' % label)
                emit('  原始范围:   X[%+.3f ↔ %+.3f] Y[%+.3f ↔ %+.3f]'
                     % (box['min_x'], box['max_x'],
                        box['min_y'], box['max_y']))
                emit('  校准后范围: X[%+.3f ↔ %+.3f] Y[%+.3f ↔ %+.3f]'
                     % (cand['min_x'], cand['max_x'],
                        cand['min_y'], cand['max_y']))
                emit('  中心偏移:   X=%+.4f, Y=%+.4f'
                     % (centers[side]['x'], centers[side]['y']))
            emit('=' * 80)
            emit()

            if failed_sides:
                emit('⚠️  本次校准有效数据不足，未写入本地文件')
            else:
                emit('✅ 行程体检: 四个方向行程均衡，无需重新校准')
            for warning in warnings:
                emit('• %s' % warning)
            emit()

            result = {
                'ok': True,
                'center': centers,
                'candidates': candidates,
                'warnings': warnings,
                'saved': saved_sides,
                'failed': failed_sides,
                'calibration': self.get_calibration() if saved_sides else None,
            }
            state['result'] = result
            set_phase('done', 0.0,
                      '校准完成' if saved_sides else '校准未通过，未写入')
        except Exception as error:
            state['error'] = str(error)
            set_phase('error', 0.0, str(error))
        finally:
            state['running'] = False
    # ---- 校准数据存取 ----
    def save_calibration(self, calibration):
        with self.lock:
            self.calibration = E.normalize_calibration(calibration)
            self.engine.set_calibration(self.calibration)
            snapshot = json.loads(json.dumps(self.calibration))
        write_settings(snapshot, E.get_curve_points(), self.get_params(),
                           self.get_button_map())
        return snapshot

    def get_calibration(self):
        with self.lock:
            return json.loads(json.dumps(self.calibration))

    # ---- 算法参数 ----
    def update_params(self, params, persist=True):
        """更新算法参数（死区/增幅/方形映射等），立即生效并落盘"""
        changed = []
        if isinstance(params.get('deadzone_config'), dict):
            for stick in ('left', 'right'):
                incoming = params['deadzone_config'].get(stick)
                if not isinstance(incoming, dict):
                    continue
                target = E.DEADZONE_CONFIG[stick]
                for key in ('deadzone', 'outer_threshold', 'response_curve'):
                    if key in incoming and isinstance(incoming[key], (int, float)):
                        value = float(incoming[key])
                        if key == 'deadzone':
                            # 支持负死区：负值表示中心区放大
                            value = max(-0.2, min(0.5, value))
                        elif key == 'outer_threshold':
                            value = max(0.3, min(1.0, value))
                        else:
                            value = max(0.2, min(3.0, value))
                        target[key] = value
                changed.append('%s 死区' % stick)
        if isinstance(params.get('output_amplifier'), dict):
            # 增幅固定 1.0，忽略外部修改
            E.OUTPUT_AMPLIFIER['left_stick'] = 1.0
            E.OUTPUT_AMPLIFIER['right_stick'] = 1.0
        if isinstance(params.get('square_mapping_strength'), dict):
            for stick_key in ('left_stick', 'right_stick'):
                if stick_key in params['square_mapping_strength']:
                    value = params['square_mapping_strength'][stick_key]
                    if isinstance(value, (int, float)):
                        E.SQUARE_MAPPING_STRENGTH[stick_key] = max(
                            0.0, min(1.0, float(value)))
                        changed.append('%s 方形映射' % stick_key)
        shape = params.get('shape_algorithm')
        if isinstance(shape, str) and shape in ('none', 'circle', 'square'):
            E.SHAPE_ALGORITHM = shape
            changed.append('形状算法')
        mode = params.get('amplifier_mode')
        if isinstance(mode, str) and mode in ('radial', 'clip'):
            E.OUTPUT_AMPLIFIER_MODE = mode
            changed.append('增幅模式')
        if isinstance(params.get('square_deadzone_switch'), dict):
            for stick_key in ('left_stick', 'right_stick'):
                if stick_key in params['square_deadzone_switch']:
                    E.SQUARE_DEADZONE_SWITCH[stick_key] = (
                        1 if params['square_deadzone_switch'][stick_key] else 0)
                    changed.append('%s 方形死区' % stick_key)
        for key in ('left', 'right'):
            if key in params and isinstance(params[key], str) and \
                    params[key] in ('global_min', 'independent'):
                E.CONSISTENCY_MODE[key] = params[key]
                changed.append('%s 一致性模式' % key)
        if isinstance(params.get('update_rate'), (int, float)):
            E.UPDATE_RATE = max(100, min(2000, int(params['update_rate'])))
            changed.append('更新率')
        # 参数变了要重建优化器（缩放因子依赖一致性模式）
        with self.lock:
            self.engine.reload()
        if persist:
            with self.lock:
                snapshot = json.loads(json.dumps(self.calibration))
            write_settings(snapshot, E.get_curve_points(), self.get_params(),
                           self.get_button_map())
        return changed

    def get_params(self):
        return {
            'consistency_mode': dict(E.CONSISTENCY_MODE),
            'deadzone_config': {k: dict(v) for k, v in E.DEADZONE_CONFIG.items()},
            'output_amplifier': dict(E.OUTPUT_AMPLIFIER),
            'square_deadzone_switch': dict(E.SQUARE_DEADZONE_SWITCH),
            'square_mapping_strength': dict(E.SQUARE_MAPPING_STRENGTH),
            'amplifier_mode': E.OUTPUT_AMPLIFIER_MODE,
            'shape_algorithm': E.SHAPE_ALGORITHM,
            'update_rate': E.UPDATE_RATE,
        }

    # ---- 物理手柄 ----
    def scan_hardware(self):
        if hardware is None:
            self.hw_state = 'failed'
            self.hw_message = '硬件模块不可用（缺 hid 或 pyusb）'
            return {'ok': False, 'message': self.hw_message}
        self.hw_state = 'scanning'
        self.hw_message = '正在扫描…'
        try:
            result = hardware.scan_and_connect()
        except Exception as error:
            result = {'ok': False,
                      'message': '扫描异常: %s: %s' % (type(error).__name__, error)}
        self.hw_devices = result.get('devices') or []
        self.hw_activation = result.get('activation') or ''
        if result.get('ok'):
            self.hw_state = 'connected'
            self.hw_message = result.get('message', '已连接')
            self.input_source = 'hw'
            self.auto_enabled = False
        else:
            self.hw_state = 'failed'
            self.hw_message = result.get('message', '未检测到手柄')
        return result

    def hardware_frame(self):
        if hardware is None:
            return None
        snapshot = hardware.status()
        # 读取层已经断开，或者数据不再更新（拔线的典型表现）就返回 None，
        # 否则主循环会一直拿最后一帧旧数据，以为手柄还连着。
        if not snapshot.get('connected'):
            return None
        age = snapshot.get('age')
        if age is not None and age > 0.4:
            return None
        latest = snapshot.get('latest')
        if latest is None:
            return None
        return {
            'left_raw': latest['left_raw'],
            'right_raw': latest['right_raw'],
            'buttons': latest['buttons'],
            'axes': latest.get('axes') or [],
            'trigger_left': 1.0 if latest['buttons'][13] else 0.0,
            'trigger_right': 1.0 if latest['buttons'][5] else 0.0,
        }

    # ---- 自检 ----
    def run_selftest(self, side='left', seconds=5.0):
        """记录一段时间内某一侧摇杆的 原始值 / 归一化 / 最终输出 的极值"""
        if side not in ('left', 'right'):
            return {'ok': False, 'message': 'side 必须是 left/right'}
        if hardware is None:
            return {'ok': False, 'message': '硬件模块不可用'}
        if self.input_source != 'hw':
            return {'ok': False,
                    'message': '当前不是物理手柄输入，请先在"连接"页连接手柄'}

        test_engine = E.Engine(self.get_calibration())
        raw_values = []
        norm_values = []
        out_values = []
        deadline = time.perf_counter() + max(1.0, min(seconds, 15.0))
        while time.perf_counter() < deadline:
            snapshot = hardware.status()
            latest = snapshot.get('latest')
            if latest is None:
                time.sleep(0.01)
                continue
            raw = latest['%s_raw' % side]
            result = test_engine.process(
                latest['left_raw'] if side == 'left' else (0.0, 0.0),
                latest['right_raw'] if side == 'right' else (0.0, 0.0),
                latest['buttons'])
            stages = result['%s_stages' % side]
            normalized = stages.get('after_normalize', [0.0, 0.0])
            output = [result[side]['x'], result[side]['y']]
            raw_values.append((raw[0], raw[1]))
            norm_values.append((normalized[0], normalized[1]))
            out_values.append((output[0], output[1]))
            time.sleep(0.005)

        if not raw_values:
            return {'ok': False, 'message': '没有采集到数据'}

        def ranges(values):
            xs = [v[0] for v in values]
            ys = [v[1] for v in values]
            return {
                'x': [round(min(xs), 4), round(max(xs), 4)],
                'y': [round(min(ys), 4), round(max(ys), 4)],
                'x_span': round(max(xs) - min(xs), 4),
                'y_span': round(max(ys) - min(ys), 4),
            }

        raw_range = ranges(raw_values)
        norm_range = ranges(norm_values)
        out_range = ranges(out_values)

        raw_moved = max(raw_range['x_span'], raw_range['y_span']) > 0.15
        out_moved = max(out_range['x_span'], out_range['y_span']) > 0.05
        if not raw_moved and not out_moved:
            verdict = '没检测到摇杆移动（可能没推，或被其它程序占用）'
        elif raw_moved and out_moved:
            verdict = '正常：原始值和输出都在变化'
        elif raw_moved and not out_moved:
            verdict = '异常：原始值在动但输出不变 → 问题在算法/死区参数'
        else:
            verdict = '异常：输出在动但原始值没动 → 数据来源可疑'

        return {
            'ok': True,
            'side': side,
            'samples': len(raw_values),
            'raw': raw_range,
            'normalized': norm_range,
            'output': out_range,
            'raw_moved': raw_moved,
            'output_moved': out_moved,
            'verdict': verdict,
        }

    # ---- 摇杆曲线 ----
    def get_curves(self):
        return E.get_curve_points()

    def set_curves(self, payload):
        changed = []
        if isinstance(payload, dict):
            if 'stick' in payload and 'points' in payload:
                stick = payload['stick']
                if stick in ('left', 'right'):
                    E.set_curve_points(stick, payload['points'])
                    changed.append(stick)
            else:
                for stick in ('left', 'right'):
                    if stick in payload:
                        E.set_curve_points(stick, payload[stick])
                        changed.append(stick)
        with self.lock:
            snapshot = json.loads(json.dumps(self.calibration))
        write_settings(snapshot, E.get_curve_points(), self.get_params(),
                           self.get_button_map())
        return {'ok': True, 'changed': changed,
                'curves': E.get_curve_points()}

    # ---- 按键映射 ----
    def get_button_map(self):
        if xinput_module is None:
            return {}
        return xinput_module.get_button_map()

    def set_button_map(self, payload):
        if xinput_module is None:
            return {'ok': False, 'message': '虚拟手柄模块不可用'}
        changed, error = xinput_module.set_button_map(payload)
        with self.lock:
            snapshot = json.loads(json.dumps(self.calibration))
        write_settings(snapshot, E.get_curve_points(), self.get_params(),
                       self.get_button_map())
        return {'ok': not error, 'changed': changed, 'message': error,
                'button_map': self.get_button_map()}

    def reset_button_map(self):
        if xinput_module is None:
            return {'ok': False}
        xinput_module.set_button_map(xinput_module.DEFAULT_BUTTON_MAP)
        with self.lock:
            snapshot = json.loads(json.dumps(self.calibration))
        write_settings(snapshot, E.get_curve_points(), self.get_params(),
                       self.get_button_map())
        return {'ok': True, 'button_map': self.get_button_map()}

    # ---- 虚拟 XInput 输出 ----
    def set_output(self, enabled, update_rate=None):
        if self.xinput is None:
            message = '虚拟手柄模块不可用: %s' % globals().get(
                '_XINPUT_IMPORT_ERROR', '未加载')
            self.output_message = message
            return {'ok': False, 'message': message, 'enabled': False}
        if enabled:
            ok, message = self.xinput.start(update_rate or E.UPDATE_RATE)
            self.output_enabled = bool(ok)
            self.output_message = message
            print('[输出] %s' % message)
            return {'ok': ok, 'message': message, 'enabled': self.output_enabled}
        ok, message = self.xinput.stop()
        self.output_enabled = False
        self.output_message = message
        print('[输出] %s' % message)
        return {'ok': ok, 'message': message, 'enabled': False}

    def output_status(self):
        if self.xinput is None:
            return {
                'available': False,
                'running': False,
                'enabled': False,
                'error': globals().get('_XINPUT_IMPORT_ERROR', '模块未加载'),
            }
        snapshot = self.xinput.snapshot()
        snapshot['enabled'] = self.output_enabled
        snapshot['message'] = self.output_message
        return snapshot

    # ---- 主循环 ----
    def loop(self):
        while self.running:
            use_hw = (self.input_source == 'hw' and hardware is not None)
            frame = None
            if use_hw:
                frame = self.hardware_frame()
                if frame is None:
                    use_hw = False
            if not use_hw and self.input_source == 'hw':
                # 用户选的是物理手柄但它掉线了：绝不能退回模拟数据，
                # 否则虚拟手柄会乱发按键。这里持续输出中位并等待重连。
                if self.hw_state != 'disconnected':
                    if self.hw_state == 'connected':
                        _log('手柄掉线：读取层无数据，界面切到断开状态')
                        self.need_restart = True
                    self.hw_state = 'disconnected'
                    self.hw_message = '手柄已断开，请重新连接后点【重新检测】'
                if self.output_enabled and self.xinput is not None:
                    try:
                        self.xinput.set_state((0.0, 0.0), (0.0, 0.0),
                                              0.0, 0.0, [False] * 21)
                    except Exception:
                        pass
                time.sleep(0.01)
                continue

            if not use_hw:
                if self.auto_enabled:
                    self.auto.step()
                frame = self.controller.read()

            with self.lock:
                result = self.engine.process(frame['left_raw'],
                                             frame['right_raw'],
                                             frame['buttons'])

            left_out = [round(result['left']['x'], 4),
                        round(result['left']['y'], 4)]
            right_out = [round(result['right']['x'], 4),
                         round(result['right']['y'], 4)]
            watching = (self.monitor_keys
                        or bool(self.calib.get('running')))
            if watching:
                self.status = {
                    'source': 'hw' if use_hw else 'sim',
                    'left_raw': [round(v, 5) for v in frame['left_raw']],
                    'right_raw': [round(v, 5) for v in frame['right_raw']],
                    'left': left_out,
                    'right': right_out,
                    'ads_active': False,
                    'buttons': frame['buttons'],
                    'axes': frame.get('axes') or [],
                    'triggers': [
                        self.engine.process_trigger(frame['trigger_left']),
                        self.engine.process_trigger(frame['trigger_right']),
                    ],
                    'auto': self.auto_enabled,
                    'elapsed': round(time.perf_counter() - self.started, 2),
                    'left_stages': result.get('left_stages', {}),
                    'right_stages': result.get('right_stages', {}),
                    'output_enabled': self.output_enabled,
                }
            else:
                # 没人看时就只保留最小字段，省掉分阶段计算与历史记录
                self.status = {
                    'source': 'hw' if use_hw else 'sim',
                    'left': left_out,
                    'right': right_out,
                    'buttons': None,
                    'output_enabled': self.output_enabled,
                }

            # 推送到虚拟 XInput 手柄（与原程序一致：摇杆 + 扳机 + 按键）
            if self.output_enabled and self.xinput is not None:
                buttons = list(frame['buttons'])
                if len(buttons) < 21:
                    buttons = buttons + [False] * (21 - len(buttons))
                # 扳机由按键映射推导：映射到 LT / RT 的按键按下即为满扳机
                trigger_left = 0.0
                trigger_right = 0.0
                button_map = (xinput_module.get_button_map()
                              if xinput_module is not None else {})
                for index, pressed in enumerate(buttons):
                    if not pressed:
                        continue
                    target = (button_map.get(index) or {}).get('xinput')
                    if target == 'LT':
                        trigger_left = 1.0
                    elif target == 'RT':
                        trigger_right = 1.0
                self.xinput.set_state(
                    (result['left']['x'], result['left']['y']),
                    (result['right']['x'], result['right']['y']),
                    trigger_left,
                    trigger_right,
                    buttons,
                )
            if watching:
                self.history.append({
                    't': time.perf_counter() - self.started,
                    'right_in': list(self.status['right_raw']),
                    'right_out': list(self.status['right']),
                })
            self._frames += 1
            now = time.perf_counter()
            if now - self._hz_mark >= 1.0:
                self.output_hz = self._frames / (now - self._hz_mark)
                self._frames = 0
                self._hz_mark = now
            time.sleep(1.0 / max(60, min(1000, E.UPDATE_RATE)))



def _log(message):
    """带时间戳的日志（同时进控制台和 logs/sticklab.log）"""
    stamp = time.strftime('%H:%M:%S')
    try:
        print('[%s] %s' % (stamp, message), flush=True)
    except Exception:
        pass


# 子进程一律隐藏控制台窗口（否则 tasklist 会闪窗）
_NO_WINDOW = 0
if os.name == 'nt':
    import subprocess as _subprocess
    _NO_WINDOW = _subprocess.CREATE_NO_WINDOW


RESTART_GUARD = os.path.join(BASE_DIR, 'data', '.restart_guard')


def _restart_allowed():
    """20 秒内只重启一次，避免死循环"""
    try:
        return (time.time() - os.path.getmtime(RESTART_GUARD)) > 5
    except Exception:
        return True


def _restart_service():
    """起一个全新进程（SDL 状态完全重建），然后退出当前进程"""
    import subprocess

    time.sleep(0.3)
    try:
        os.makedirs(os.path.dirname(RESTART_GUARD), exist_ok=True)
        with open(RESTART_GUARD, 'w', encoding='utf-8') as handle:
            handle.write(str(time.time()))
    except Exception:
        pass

    env = dict(os.environ)
    env['STICKLAB_START_DELAY'] = '0.8'
    env['STICKLAB_FAST_START'] = '1'
    flags = 0
    if os.name == 'nt':
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen([sys.executable] + sys.argv, cwd=BASE_DIR, env=env,
                         creationflags=flags | _NO_WINDOW,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         close_fds=True)
        print('[手柄] SDL 状态异常，正在用新进程重启服务…', flush=True)
    except Exception as error:
        print('[手柄] 重启失败: %s' % error, flush=True)
        return
    time.sleep(0.5)
    os._exit(0)


lab = LabState()


# ==================== Flask ====================
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, 'templates'))
app.config['JSON_AS_ASCII'] = False


def _public_devices(devices):
    result = []
    for info in devices or []:
        if isinstance(info, dict):
            result.append({k: v for k, v in info.items() if k != 'path_bytes'})
    return result


@app.route('/pad.png')
def pad_image():
    """改键页用的手柄示意图"""
    from flask import send_from_directory
    return send_from_directory(os.path.join(BASE_DIR, 'static'), 'pad.png')


@app.route('/')
def index():
    return render_template('index.html',
                           button_layout=BUTTON_LAYOUT,
                           button_count=BUTTON_COUNT,
                           sim_device=SIM_DEVICE,
                           calibration=lab.get_calibration(),
                           params=lab.get_params(),
                           curves=lab.get_curves(),
                           button_map=lab.get_button_map(),
                           xinput_targets=(list(xinput_module.XINPUT_TARGETS)
                                           if xinput_module else []))


@app.route('/api/calibration', methods=['GET'])
def get_calibration():
    return jsonify(lab.get_calibration())


@app.route('/api/calibration', methods=['POST'])
def set_calibration():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效数据'}), 400
    saved = lab.save_calibration(data)
    return jsonify({'status': 'ok', 'calibration': saved})


@app.route('/api/params', methods=['GET'])
def get_params():
    return jsonify(lab.get_params())


@app.route('/api/params', methods=['POST'])
def set_params():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效数据'}), 400
    changed = lab.update_params(data)
    return jsonify({'status': 'ok', 'changed': changed, 'params': lab.get_params()})


@app.route('/api/info')
def get_info():
    return jsonify(lab.engine.get_info())


@app.route('/api/device/scan', methods=['POST'])
def device_scan():
    data = request.get_json(silent=True) or {}
    force = bool(data.get('force'))

    if lab._scanning:
        return jsonify({
            'ok': False,
            'busy': True,
            'message': '正在检测中，请稍候…',
            'state': lab.hw_state,
            'source': lab.input_source,
        })
    # 已经连着就别重连：重连会先 disconnect，把正在工作的读取线程停掉
    if not force and lab.hw_state == 'connected':
        return jsonify({
            'ok': True,
            'skipped': True,
            'message': '手柄已连接，无需重新检测',
            'activation': '',
            'devices': _public_devices(lab.hw_devices),
            'state': lab.hw_state,
            'source': lab.input_source,
        })
    # 拔过线的话进程内 SDL 一定坏了，直接重启，别白等一次必失败的扫描
    if force and lab.need_restart and _restart_allowed():
        try:
            device_present = bool(hardware.enumerate_devices()) if hardware else False
        except Exception:
            device_present = False
        if device_present:
            _log('重新检测：检测到曾拔线，直接重启服务')
            threading.Thread(target=_restart_service, daemon=True).start()
            return jsonify({
                'ok': False,
                'restarting': True,
                'message': '手柄已插回，正在快速重启服务…',
                'state': lab.hw_state,
                'source': lab.input_source,
            })

    _log('重新检测：开始（force=%s）' % force)
    lab._scanning = True
    try:
        result = lab.scan_hardware()
    finally:
        lab._scanning = False
    _log('重新检测：结果 ok=%s msg=%s activation=%s'
         % (result.get('ok'), result.get('message'),
            (result.get('activation') or '')[:60]))
    payload = {
        'ok': bool(result.get('ok')),
        'message': result.get('message', ''),
        'activation': result.get('activation', ''),
        'devices': _public_devices(lab.hw_devices),
        'state': lab.hw_state,
        'source': lab.input_source,
    }

    # 设备插着但 SDL 初始化不了 → 换新进程最可靠
    try:
        device_present = bool(hardware.enumerate_devices()) if hardware else False
    except Exception:
        device_present = False
    _log('重新检测：设备存在=%s 冷却允许=%s'
         % (device_present, _restart_allowed()))
    if payload['ok']:
        lab.need_restart = False
        try:
            os.remove(RESTART_GUARD)
        except Exception:
            pass

    if (not payload['ok']) and device_present and _restart_allowed():
        payload['restarting'] = True
        payload['message'] = '读取层状态异常，正在用新进程重启服务…'
        threading.Thread(target=_restart_service, daemon=True).start()

    return jsonify(payload)


@app.route('/api/device/status')
def device_status():
    snapshot = hardware.status() if hardware is not None else {}

    # 读取层没在跑（比如刚拔线）就立刻对外报"已断开"，
    # 不要等主循环把 hw_state 改过来
    state = lab.hw_state
    message = lab.hw_message
    if hardware is not None and state == 'connected':
        live = bool(snapshot.get('connected'))
        age = snapshot.get('age')
        if not live or (age is not None and age > 0.5):
            state = 'disconnected'
            message = '手柄已断开，请重新连接后点【重新检测】'

    return jsonify({
        'state': state,
        'message': message,
        'activation': lab.hw_activation,
        'devices': _public_devices(lab.hw_devices),
        'source': lab.input_source,
        'connected': bool(snapshot.get('connected')),
        'frames': snapshot.get('frames', 0),
        'rate': snapshot.get('rate', 0),
        'error': snapshot.get('error', ''),
        'age': snapshot.get('age'),
        'stage': (hardware.status().get('stage') if hardware else '') or '',
        'hardware_available': hardware is not None,
    })


@app.route('/api/device/disconnect', methods=['POST'])
def device_disconnect():
    if hardware is not None:
        hardware.disconnect()
    lab.hw_state = 'idle'
    lab.hw_message = '已断开物理手柄'
    lab.input_source = 'sim'
    lab.auto_enabled = True
    return jsonify({'ok': True, 'source': lab.input_source})


@app.route('/api/source', methods=['POST'])
def set_source():
    data = request.get_json(silent=True) or {}
    source = data.get('source')
    if source not in ('sim', 'hw'):
        return jsonify({'error': 'source 必须是 sim 或 hw'}), 400
    if source == 'hw':
        snapshot = hardware.status() if hardware is not None else {}
        if not snapshot.get('connected'):
            return jsonify({'error': '物理手柄未连接，请先扫描'}), 400
        lab.input_source = 'hw'
        lab.auto_enabled = False
    else:
        lab.input_source = 'sim'
    return jsonify({'ok': True, 'source': lab.input_source})


@app.route('/api/selftest', methods=['POST'])
def selftest():
    data = request.get_json(silent=True) or {}
    side = data.get('side', 'left')
    seconds = float(data.get('seconds', 5.0))
    return jsonify(lab.run_selftest(side, seconds))


@app.route('/api/axes')
def get_axes():
    """全部轴的当前值 + 自连接以来的摆动区间（用于定位摇杆在哪个轴）"""
    if hardware is None:
        return jsonify({'available': False, 'axes': [], 'span': {}})
    snapshot = hardware.status()
    latest = snapshot.get('latest') or {}
    return jsonify({
        'available': True,
        'connected': snapshot.get('connected'),
        'num_axes': snapshot.get('num_axes', 0),
        'axes': latest.get('axes') or [],
        'span': snapshot.get('axis_span') or {},
        'joystick_name': snapshot.get('joystick_name', ''),
        'button_limit': snapshot.get('button_limit', 0),
    })


@app.route('/api/curves', methods=['GET'])
def get_curves():
    return jsonify(lab.get_curves())


@app.route('/api/curves', methods=['POST'])
def set_curves():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效数据'}), 400
    return jsonify(lab.set_curves(data))


@app.route('/api/buttonmap', methods=['GET'])
def get_buttonmap():
    if xinput_module is None:
        return jsonify({'button_map': {}, 'xinput_targets': [],
                        'names': {}})
    return jsonify({
        'button_map': lab.get_button_map(),
        'xinput_targets': list(xinput_module.XINPUT_TARGETS),
        'names': {str(k): v for k, v in xinput_module.PHYSICAL_TO_NAME.items()},
    })


@app.route('/api/buttonmap', methods=['POST'])
def set_buttonmap():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': '无效数据'}), 400
    if data.get('reset'):
        return jsonify(lab.reset_button_map())
    return jsonify(lab.set_button_map(data))


@app.route('/api/output', methods=['GET'])
def output_get():
    return jsonify(lab.output_status())


@app.route('/api/output', methods=['POST'])
def output_post():
    data = request.get_json(silent=True) or {}
    return jsonify(lab.set_output(bool(data.get('enabled')),
                                  data.get('update_rate')))


@app.route('/api/keyrelease', methods=['POST'])
def keyrelease():
    """一键释放卡住的修饰键"""
    if xinput_module is None:
        return jsonify({'ok': False, 'message': '虚拟手柄模块不可用'})
    ok, message = xinput_module.release_all_keys()
    return jsonify({'ok': ok, 'message': message})


@app.route('/api/keytest', methods=['POST'])
def keytest():
    """测试一组键盘按键能否真的发出去（用于排查键盘映射）"""
    data = request.get_json(silent=True) or {}
    keys = data.get('keys')
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split('+') if k.strip()]
    if not keys:
        return jsonify({'ok': False, 'message': '没有要测试的按键'})
    try:
        from . import keys as key_sender
    except Exception:
        key_sender = None
    if key_sender is None:
        return jsonify({'ok': False, 'message': '键盘模块不可用'})
    try:
        for key in keys:
            if key_sender.resolve_key(key) is None:
                return jsonify({'ok': False,
                                'message': '无法识别的键: %s' % key})
        key_sender.press(keys, hold=0.05)
        return jsonify({
            'ok': True,
            'mode': 'scancode',
            'message': '已用扫描码发送 %s（记事本能打出来的话，还要看游戏是否认）'
                       % '+'.join(keys),
        })
    except Exception as error:
        return jsonify({'ok': False,
                        'message': '%s: %s' % (type(error).__name__, error)})


@app.route('/api/system/joycpl', methods=['POST'])
def open_joycpl():
    """打开 Windows「游戏控制器」面板（设备顺序 / 首选设备在那里改）"""
    try:
        import subprocess
        subprocess.Popen(['control', 'joy.cpl'], creationflags=_NO_WINDOW)
        return jsonify({'ok': True, 'message': '已打开「游戏控制器」面板'})
    except Exception as error:
        return jsonify({'ok': False,
                        'message': '%s: %s' % (type(error).__name__, error)})


@app.route('/api/system/devices')
def system_devices():
    """按 SDL 枚举顺序返回手柄列表（游戏就是按这个顺序认设备）"""
    devices = []
    if hardware is not None and hasattr(hardware, 'reader'):
        try:
            devices = hardware.reader.list_joysticks()
        except Exception:
            devices = []
    return jsonify({
        'devices': devices,
        'steam_running': _steam_running(),
        'preferred_hint': '在「游戏控制器」里选中虚拟手柄 → 属性 → 高级 → 设为首选设备',
    })


def _steam_running():
    try:
        import subprocess
        output = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq steam.exe'],
                                capture_output=True, text=True,
                                encoding='mbcs', errors='replace',
                                creationflags=_NO_WINDOW).stdout
        return 'steam.exe' in output.lower()
    except Exception:
        return False


@app.route('/api/status')
def get_status():
    payload = dict(lab.status)
    payload['history'] = list(lab.history)[-120:]
    payload['output_hz'] = lab.output_hz
    return jsonify(payload)


def _pressed_indices(buttons):
    """只保留按下的下标，减小推送体积"""
    if not buttons:
        return []
    return [index for index, pressed in enumerate(buttons) if pressed]


@app.route('/api/monitor', methods=['POST'])
def monitor():
    """前端告知是否正在看按键/摇杆（不在键位页就关掉监测）"""
    data = request.get_json(silent=True) or {}
    if 'keys' in data:
        lab.monitor_keys = bool(data['keys'])
    return jsonify({'ok': True, 'monitor_keys': lab.monitor_keys})


@app.route('/api/stream')
def stream_status():
    """SSE 推送摇杆与按键（~60Hz），供前端做平滑动画"""
    def generate():
        last = None
        last_ping = time.perf_counter()
        while True:
            snapshot = lab.status or {}
            payload = json.dumps({
                'l': snapshot.get('left') or [0.0, 0.0],
                'r': snapshot.get('right') or [0.0, 0.0],
                'b': _pressed_indices(snapshot.get('buttons')),
            }, separators=(',', ':'))
            if payload != last:
                last = payload
                yield 'data: ' + payload + '\n\n'
            else:
                now = time.perf_counter()
                if now - last_ping >= 1.0:
                    last_ping = now
                    yield ': ping\n\n'   # 心跳，防止连接被回收
            time.sleep(0.02)   # 50Hz 推送，配合前端 rAF 插值足够顺滑

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/api/calibrate/start', methods=['POST'])
def calibrate_start():
    """启动分阶段校准（左右摇杆一次完成）"""
    data = request.get_json(silent=True) or {}
    return jsonify(lab.start_calibration(
        prepare_seconds=float(data.get('prepare_seconds', 3.0)),
        center_seconds=float(data.get('center_seconds', 1.0)),
        ready_seconds=float(data.get('ready_seconds', 2.0)),
        edge_seconds=float(data.get('edge_seconds', 6.0)),
    ))


@app.route('/api/calibrate/status')
def calibrate_status():
    return jsonify(lab.get_calibration_status())


@app.route('/api/calibrate/cancel', methods=['POST'])
def calibrate_cancel():
    return jsonify(lab.cancel_calibration())


@app.route('/api/sim/stick', methods=['POST'])
def sim_stick():
    data = request.get_json(silent=True) or {}
    side = data.get('side', 'right')
    if side not in ('left', 'right'):
        return jsonify({'error': 'side 必须是 left/right'}), 400
    try:
        x = float(data.get('x', 0.0))
        y = float(data.get('y', 0.0))
    except (TypeError, ValueError):
        return jsonify({'error': 'x/y 必须是数字'}), 400
    lab.controller.set_stick(side, x, y)
    return jsonify({'status': 'ok'})


@app.route('/api/sim/button', methods=['POST'])
def sim_button():
    data = request.get_json(silent=True) or {}
    try:
        index = int(data.get('index'))
    except (TypeError, ValueError):
        return jsonify({'error': 'index 必须是整数'}), 400
    if not 0 <= index < BUTTON_COUNT:
        return jsonify({'error': f'index 超出范围 0~{BUTTON_COUNT - 1}'}), 400
    lab.controller.set_button(index, bool(data.get('pressed')))
    return jsonify({'status': 'ok', 'buttons': lab.controller.buttons})


@app.route('/api/sim/auto', methods=['POST'])
def sim_auto():
    data = request.get_json(silent=True) or {}
    lab.auto_enabled = bool(data.get('enabled', True))
    lab.auto = AutoScript(lab.controller)
    if not lab.auto_enabled:
        lab.controller.clear_buttons()
        lab.controller.set_stick('left', 0.0, 0.0)
        lab.controller.set_stick('right', 0.0, 0.0)
    return jsonify({'status': 'ok', 'auto': lab.auto_enabled})


def auto_enable_output(log=print, delay=1.5):
    """启动后自动打开虚拟 XInput 手柄（让检测网站/游戏立刻能看到）"""
    time.sleep(delay)
    if lab.xinput is None:
        log('[输出] 虚拟手柄模块不可用，跳过')
        return
    try:
        result = lab.set_output(True) or {}
    except Exception as error:
        log('[输出] ⚠️ 启动失败: %s: %s' % (type(error).__name__, error))
        return
    message = result.get('message', '')
    if result.get('ok'):
        log('[输出] ✅ %s' % message)
    else:
        log('[输出] ⚠️ %s' % message)


def auto_scan_hardware(log=print):
    """启动时后台自动检测物理手柄"""
    if hardware is None:
        log('[手柄] 硬件模块不可用（缺 hid/pyusb），仅使用虚拟手柄')
        return
    time.sleep(1.0)
    log('[手柄] 正在自动检测 Switch2 Pro…')
    try:
        result = lab.scan_hardware()
    except Exception as error:
        log(f'[手柄] 自动检测异常: {type(error).__name__}: {error}')
        return
    if result.get('ok'):
        log(f'[手柄] ✅ {result.get("message")}')
        if result.get('activation'):
            log(f'[手柄] {result["activation"]}')
    else:
        log(f'[手柄] ⚠️ {result.get("message")}')
        log('[手柄] 仍使用虚拟手柄；可在网页"连接"页点【重新扫描】')


def main():
    parser = argparse.ArgumentParser(description='StickLab 手柄调参工具')
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--no-auto', action='store_true')
    parser.add_argument('--no-output', action='store_true',
                        help='启动时不自动开启虚拟 XInput 手柄')
    args = parser.parse_args()

    import socket
    port = args.port
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((args.host, port))
                break
            except OSError:
                port += 1
    args.port = port

    if args.no_auto:
        lab.auto_enabled = False

    threading.Thread(target=lab.loop, daemon=True).start()
    threading.Thread(target=auto_scan_hardware, daemon=True).start()
    if not args.no_output:
        threading.Thread(target=auto_enable_output, daemon=True).start()

    url = f'http://{args.host}:{args.port}'
    print('=' * 64)
    print('StickLab —— Switch2 Pro 手柄调参（用户原算法）')
    print('=' * 64)
    print(f'  界面地址: {url}')
    print(f'  校准数据: {lab.get_calibration().get("saved_at") or "默认值"}')
    print(f'  输入源:   物理手柄（自动检测）+ 虚拟手柄备用')
    print('  按 Ctrl+C 退出')
    print('=' * 64)

    if not args.headless:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False,
            threaded=True)


if __name__ == '__main__':
    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        lab.running = False
        print('\n已退出')
