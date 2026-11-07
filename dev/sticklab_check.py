"""StickLab 环境自检 —— 一次跑完所有可能出问题的地方

用法:
    python .\sticklab_check.py

它会检查：Python 版本、依赖库、文件完整性、端口占用、能否启动服务，
并把每一步的结果打出来。最后会停住等你按键，避免窗口一闪就关。
"""

import importlib
import os
import socket
import sys
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))


def line(title):
    print('\n' + '=' * 66)
    print(title)
    print('=' * 66)


def pause_if_needed():
    """双击运行时窗口会立刻关闭，这里停住让人看到结果"""
    if sys.stdin and sys.stdin.isatty():
        try:
            input('\n按回车键退出...')
        except (EOFError, KeyboardInterrupt):
            pass


def safe_version(module_name, imported):
    """取版本号，避开 Flask 3.2 起 __version__ 的弃用警告"""
    try:
        from importlib.metadata import version as pkg_version
        return pkg_version(module_name)
    except Exception:
        return getattr(imported, '__version__', '') or ''


def main():
    ok = True

    line('1. Python 环境')
    print(f'  解释器: {sys.executable}')
    print(f'  版本:   {sys.version.split()[0]}')
    print(f'  工作目录: {os.getcwd()}')
    print(f'  脚本目录: {BASE}')
    if sys.version_info < (3, 8):
        print('  ❌ 需要 Python 3.8+')
        ok = False

    line('2. 依赖库')
    required = [('flask', 'Web 服务'), ('hid', '手柄读取（阶段二才需要）'),
                ('usb', '原生激活（阶段二才需要）')]
    optional = [('pygame', '主程序用'), ('vgamepad', '虚拟手柄输出')]
    for module, purpose in required:
        try:
            imported = importlib.import_module(module)
            print(f'  ✅ {module:<10} {safe_version(module, imported):<12} ({purpose})')
        except ImportError:
            print(f'  ❌ {module:<10} 缺失  ({purpose}) —— 请 pip install {module}')
            if module == 'flask':
                ok = False
    for module, purpose in optional:
        try:
            imported = importlib.import_module(module)
            print(f'  ✅ {module:<10} {safe_version(module, imported):<12} ({purpose})')
        except ImportError:
            print(f'  ⚪ {module:<10} 未安装      ({purpose})，暂不影响')

    line('3. 文件完整性')
    files = [
        'sticklab/__init__.py',
        'sticklab/engine.py',
        'sticklab/simulator.py',
        'sticklab/presets.py',
        'sticklab/webapp.py',
        'sticklab/templates/index.html',
    ]
    for rel in files:
        path = os.path.join(BASE, rel)
        if os.path.exists(path):
            size = os.path.getsize(path)
            print(f'  ✅ {rel:<34} {size:>7} 字节')
        else:
            print(f'  ❌ {rel:<34} 缺失！')
            ok = False

    line('4. 模块导入')
    sys.path.insert(0, BASE)
    for module in ('sticklab.engine', 'sticklab.simulator',
                   'sticklab.presets', 'sticklab.webapp'):
        try:
            importlib.import_module(module)
            print(f'  ✅ {module}')
        except Exception as error:
            print(f'  ❌ {module} 导入失败: {type(error).__name__}: {error}')
            traceback.print_exc()
            ok = False

    line('5. 端口可用性')
    host = '127.0.0.1'
    free_port = None
    for port in range(8899, 8920):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                free_port = port
                print(f'  ✅ 端口 {port} 可用')
                break
            except OSError:
                print(f'  ⚪ 端口 {port} 被占用，跳过')
    if free_port is None:
        print('  ❌ 8899~8919 全部被占用，请用 --port 指定其它端口')
        ok = False

    line('6. 引擎自检（跑一帧数据）')
    try:
        from sticklab.engine import Engine, normalize_config
        engine = Engine(normalize_config(None))
        result = engine.process((0.0, 0.0), (0.4, -0.3), [False] * 21)
        print(f'  ✅ 引擎正常，右摇杆输出 = '
              f'({result["right"]["x"]:.3f}, {result["right"]["y"]:.3f})')
    except Exception as error:
        print(f'  ❌ 引擎失败: {type(error).__name__}: {error}')
        traceback.print_exc()
        ok = False

    line('7. Flask 能否创建应用')
    try:
        from sticklab.webapp import app
        client = app.test_client()
        response = client.get('/')
        print(f'  ✅ 首页返回 {response.status_code}，'
              f'{len(response.get_data())} 字节')
    except Exception as error:
        print(f'  ❌ 失败: {type(error).__name__}: {error}')
        traceback.print_exc()
        ok = False

    line('总结')
    if ok:
        print('  ✅ 全部检查通过，可以直接启动：')
        print(f'     python "{os.path.join(BASE, "sticklab", "webapp.py")}"')
    else:
        print('  ❌ 有问题需要修复，请看上面标 ❌ 的项')

    pause_if_needed()
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
