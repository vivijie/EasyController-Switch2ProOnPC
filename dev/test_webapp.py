"""Web 服务端到端测试（用 Flask 测试客户端，不需要开服务器）

覆盖：页面渲染、配置读写、预设应用、校准、状态轮询、曲线数据、模拟控制
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sticklab import webapp
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


def main():
    print('=' * 70)
    print('Web 服务端到端测试')
    print('=' * 70)

    # 让引擎循环跑起来（测试 status 需要）
    import threading
    lab = webapp.lab
    thread = threading.Thread(target=lab.loop, daemon=True)
    thread.start()
    time.sleep(0.4)

    client = webapp.app.test_client()

    # ---------- 1. 首页 ----------
    print('\n[1] 首页渲染')
    response = client.get('/')
    check('GET / 返回 200', response.status_code == 200, str(response.status_code))
    html = response.get_data(as_text=True)
    for keyword in ('StickLab', 'curveCanvas', 'btnCalib', 'presetList', 'BUTTON_LAYOUT'):
        check(f'首页包含 {keyword}', keyword in html)
    check('首页无模板报错', 'Traceback' not in html and 'jinja2' not in html.lower())

    # ---------- 2. 配置读写 ----------
    print('\n[2] 配置读写')
    response = client.get('/api/config')
    check('GET /api/config 200', response.status_code == 200)
    config = response.get_json()
    check('配置含 left/right', 'left' in config and 'right' in config)
    check('配置含 curve_switch', 'curve_switch' in config)

    original_right = config['right'].get('gain', 1.0)
    config['right']['gain'] = 0.85
    config['right']['deadzone_inner'] = 0.07
    response = client.post('/api/config', json=config)
    check('POST /api/config 200', response.status_code == 200)
    saved = response.get_json()['config']
    check('保存后 gain 生效', abs(saved['right']['gain'] - 0.85) < 1e-6,
          str(saved['right']['gain']))
    check('保存后死区生效', abs(saved['right']['deadzone_inner'] - 0.07) < 1e-6)

    # 校验引擎已同步
    with lab.lock:
        engine_gain = lab.engine.right.config['gain']
    check('引擎已同步新配置', abs(engine_gain - 0.85) < 1e-6, str(engine_gain))

    # 脏数据不应崩溃
    response = client.post('/api/config', json={'right': 'bad', 'polling_hz': 'x'})
    check('脏配置被安全处理', response.status_code == 200, str(response.status_code))
    config = client.get('/api/config').get_json()
    check('脏配置后仍可用', 'right' in config and isinstance(config['right'], dict))

    # ---------- 3. 预设 ----------
    print('\n[3] 预设方案')
    response = client.get('/api/presets')
    presets = response.get_json()['presets']
    check('预设列表非空', len(presets) >= 4, f'{len(presets)} 个')
    keys = [p['key'] for p in presets]
    check('含 Apex 追踪预设', 'apex-tracking' in keys, str(keys))

    for key in keys:
        response = client.post('/api/preset', json={'name': key})
        ok = response.status_code == 200
        if not ok:
            check(f'应用预设 {key}', False, str(response.status_code))
            continue
        applied = response.get_json()['config']
        check(f'应用预设 {key}', True)
        # 预设应用后配置必须合法
        normalized = E.normalize_config(applied)
        check(f'  预设 {key} 配置合法', normalized['right']['range_mode'] in
              ('global_min', 'per_axis', 'none'))

    response = client.post('/api/preset', json={'name': 'nonexistent'})
    check('未知预设返回 400', response.status_code == 400, str(response.status_code))

    # ---------- 4. 曲线 ----------
    print('\n[4] 曲线数据')
    response = client.get('/api/curve?samples=51')
    check('GET /api/curve 200', response.status_code == 200)
    data = response.get_json()
    for key in ('left', 'right', 'ads'):
        points = data.get(key, [])
        check(f'曲线 {key} 有 {51} 点', len(points) == 51, str(len(points)))
        if points:
            check(f'曲线 {key} 单调不减',
                  all(points[i]['output'] <= points[i+1]['output'] + 1e-6
                      for i in range(len(points)-1)))
            check(f'曲线 {key} 首点≈0', points[0]['output'] < 0.02,
                  str(points[0]['output']))
    check('curve 含 ads_enabled 标记', 'ads_enabled' in data)

    # ---------- 5. 校准 ----------
    print('\n[5] 校准')
    response = client.post('/api/calibrate', json={'side': 'right'})
    check('POST /api/calibrate 200', response.status_code == 200)
    calib = response.get_json()
    check('校准返回中心值', 'center' in calib and 'x' in calib['center'])
    check('校准返回极限', all(k in calib['limits'] for k in
                              ('min_x', 'max_x', 'min_y', 'max_y')))
    check('校准四方向行程为正', all(v > 0.5 for v in calib['spans']),
          str(calib['spans']))
    check('校准推荐 global_min', calib['recommended_mode'] == 'global_min')
    config = client.get('/api/config').get_json()
    check('校准结果已写入配置',
          abs(config['right']['center']['x'] - calib['center']['x']) < 1e-5)
    check('校准后行程接近模拟设备',
          abs(config['right']['max_x'] - 0.808) < 0.02,
          str(config['right']['max_x']))

    response = client.post('/api/calibrate', json={'side': 'invalid'})
    check('非法 side 返回 400', response.status_code == 400)

    # ---------- 6. 模拟控制 ----------
    print('\n[6] 虚拟手柄控制')
    # 先把双曲线开关打开（前面的预设循环可能把它关掉了）
    config = client.get('/api/config').get_json()
    config['curve_switch']['enabled'] = True
    config['curve_switch']['button_index'] = 13
    config['curve_switch']['stick'] = 'right'
    client.post('/api/config', json=config)

    response = client.post('/api/sim/auto', json={'enabled': False})
    check('关闭自动演示', response.get_json()['auto'] is False)

    response = client.post('/api/sim/stick', json={'side': 'right', 'x': 0.5, 'y': -0.5})
    check('设置摇杆位置', response.status_code == 200)
    time.sleep(0.2)
    status = client.get('/api/status').get_json()
    check('状态里原始值反映设置',
          status['right_raw'][0] > 0.2 and status['right_raw'][1] < 0.2,
          str(status['right_raw']))

    response = client.post('/api/sim/button', json={'index': 13, 'pressed': True})
    check('按下 ZL', response.status_code == 200)
    time.sleep(0.25)
    status = client.get('/api/status').get_json()
    check('状态反映 ZL 按下', status['buttons'][13] is True)
    check('ADS 双曲线被触发', status['ads_active'] is True,
          f"ads_active={status['ads_active']}")

    client.post('/api/sim/button', json={'index': 13, 'pressed': False})
    time.sleep(0.2)
    status = client.get('/api/status').get_json()
    check('松开后 ADS 关闭', status['ads_active'] is False)

    response = client.post('/api/sim/button', json={'index': 999, 'pressed': True})
    check('非法按钮索引返回 400', response.status_code == 400, str(response.status_code))
    response = client.post('/api/sim/stick', json={'side': 'middle', 'x': 0, 'y': 0})
    check('非法摇杆名返回 400', response.status_code == 400)

    # ---------- 7. 状态与频率 ----------
    print('\n[7] 实时状态')
    time.sleep(1.1)
    status = client.get('/api/status').get_json()
    for key in ('left_raw', 'right_raw', 'left', 'right', 'buttons',
                'ads_active', 'left_stages', 'right_stages', 'history'):
        check(f'状态含 {key}', key in status)
    check('输出频率已统计', status.get('output_hz', 0) > 30,
          str(status.get('output_hz')))
    check('历史缓存非空', len(status.get('history', [])) > 0)

    # 输出值必须在 [-1,1]
    for side in ('left', 'right'):
        values = status[side]
        check(f'{side} 输出在合法范围',
              all(-1.0001 <= v <= 1.0001 for v in values), str(values))

    # ---------- 8. 配置文件落盘 ----------
    print('\n[8] 配置持久化')
    path = webapp.CONFIG_PATH
    check('配置文件已生成', os.path.exists(path))
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as file_object:
            on_disk = json.load(file_object)
        check('落盘配置含 right.center', 'center' in on_disk['right'])

    lab.running = False

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
