"""重连时彻底重建 SDL：避免拔插后进程内设备列表失效

在 connect() 最开始（激活之前）把 SDL 完全关闭，
激活完成后再重新 init，这样 SDL 会重新枚举所有设备。
若第一次仍找不到物理手柄，再重建一次重试。
"""

import io
import os

BASE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(BASE, 'sticklab', 'pygame_reader.py')
text = io.open(PATH, encoding='utf-8').read()


def sub(old, new, name=''):
    global text
    assert text.count(old) == 1, '锚点异常: %s (%d)' % (name, text.count(old))
    text = text.replace(old, new, 1)


# 1) connect 开头先关掉 SDL
sub("""    def _connect_locked(self):
        self.disconnect()

        devices = enumerate_devices()""",
    """    def _connect_locked(self):
        self.disconnect()

        # 关键：先彻底关掉 SDL
        # 拔插数据线后 SDL 会保留一份失效的设备列表，进程内不会重新枚举，
        # 必须先 quit 再 init，重新连接时才看得到手柄。
        self._sdl_shutdown()

        devices = enumerate_devices()""",
    name='connect 先关 SDL')

# 2) 辅助方法
sub("""    def _device_alive(self, pygame):""",
    """    def _sdl_shutdown(self):
        \"\"\"彻底关闭 SDL（下次 init 会重新枚举设备）\"\"\"
        try:
            import pygame as _pg
            self._pygame = _pg
            try:
                _pg.joystick.quit()
            except Exception:
                pass
            try:
                _pg.quit()
            except Exception:
                pass
        except Exception:
            pass

    def _sdl_start(self):
        \"\"\"重新初始化 SDL 并返回模块对象\"\"\"
        import pygame
        self._pygame = pygame
        pygame.init()
        pygame.joystick.init()
        return pygame

    def _device_alive(self, pygame):""",
    name='SDL 辅助方法')

# 3) pygame 初始化改用 _sdl_start
sub("""        # 2) pygame 打开手柄（必须在激活之后）
        try:
            import pygame
            self._pygame = pygame
            pygame.init()
            pygame.joystick.init()
        except Exception as error:""",
    """        # 2) pygame 打开手柄（必须在激活之后）
        try:
            pygame = self._sdl_start()
        except Exception as error:""",
    name='pygame 初始化')

# 4) 找不到手柄时重建 SDL 再试一次
sub("""        joystick = None
        chosen_index = 0
        fallback = None
        for index in range(count):""",
    """        joystick = None
        chosen_index = 0
        fallback = None
        # 一次找不到就重建 SDL 再枚举一次（拔插后常见）
        for attempt in range(2):
            for index in range(count):""",
    name='重试循环头')

io.open(PATH, 'w', encoding='utf-8', newline='\n').write(text)
print('已加入 SDL 重建与重试')
