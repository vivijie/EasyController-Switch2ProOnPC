"""全方向扫描：对比 引擎输出 vs 虚拟 XInput 手柄的实际读数

等于"手柄检测网站"看到的数据。
"""

import json
import os
import time
import urllib.request

os.environ['SDL_JOYSTICK_HIDAPI'] = '0'

BASE = 'http://127.0.0.1:8899'


def post(path, payload):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(request, timeout=15))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=15))


def read_virtual_pad():
    import pygame
    pygame.init()
    pygame.joystick.init()
    for index in range(pygame.joystick.get_count()):
        pad = pygame.joystick.Joystick(index)
        pad.init()
        if 'Xbox' in pad.get_name():
            pygame.event.pump()
            return [pad.get_axis(i) for i in range(6)]
    return None


def main():
    post('/api/source', {'source': 'sim'})
    post('/api/sim/auto', {'enabled': False})
    post('/api/params', {'output_amplifier': {'left_stick': 1.1,
                                             'right_stick': 1.0}})

    positions = [
        ('右推到底', 1.0, 0.0),
        ('左推到底', -1.0, 0.0),
        ('上推到底', 0.0, 1.0),
        ('下推到底', 0.0, -1.0),
        ('右上半满', 0.5, 0.5),
        ('右上到底', 1.0, 1.0),
    ]

    print('%-10s %-24s %-28s' % ('推杆', '引擎输出 (x,y)', '虚拟手柄实读 RY/RX'))
    print('-' * 74)
    for label, x, y in positions:
        post('/api/sim/stick', {'side': 'right', 'x': x, 'y': y})
        time.sleep(0.35)
        status = get('/api/status')
        engine = status['right']
        pad = read_virtual_pad()
        if pad is None:
            print('  (虚拟手柄未找到)')
            return 1
        # pad: 0=LX 1=LY 2=RX 3=RY
        print('%-10s (%+.3f, %+.3f)        RX=%+.3f RY=%+.3f' % (
            label, engine[0], engine[1], pad[2], pad[3]))

    post('/api/sim/stick', {'side': 'right', 'x': 0, 'y': 0})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
