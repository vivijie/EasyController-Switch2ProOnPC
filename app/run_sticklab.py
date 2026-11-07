"""StickLab 一键启动器

作用：
  1. 先做环境自检（缺什么直接说清楚）
  2. 有问题就停住等你按键，窗口不会一闪就关
  3. 自检通过才启动 Web 服务，并自动打开浏览器

用法（任选）:
    python .\run_sticklab.py
    python .\run_sticklab.py --port 8900
    python .\run_sticklab.py --no-browser
    python .\run_sticklab.py --check-only     # 只自检不启动
"""

import argparse
import os
import socket
import sys
import time
import traceback
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))

sys.path.insert(0, BASE)


def wait_for_enter(message='\n按回车键退出...'):
    """窗口停住，避免双击运行时一闪而过"""
    try:
        if sys.stdin and sys.stdin.isatty():
            input(message)
    except (EOFError, KeyboardInterrupt):
        pass


def find_free_port(host, start, count=30):
    for port in range(start, start + count):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    return None


def check_environment():
    """返回 (是否通过, 问题列表)"""
    problems = []

    print('[1/4] 检查依赖库')
    try:
        import flask  # noqa: F401
        print('      ✅ flask 已安装')
    except ImportError:
        problems.append('缺少 flask —— 运行: pip install flask')
        print('      ❌ flask 缺失')

    print('[2/4] 检查文件')
    required = ['sticklab/engine.py', 'sticklab/simulator.py',
                'sticklab/presets.py', 'sticklab/webapp.py',
                'sticklab/templates/index.html']
    for rel in required:
        path = os.path.join(BASE, rel)
        if not os.path.exists(path):
            problems.append(f'文件缺失: {rel}')
            print(f'      ❌ {rel}')
    if not problems:
        print('      ✅ 文件完整')

    print('[3/4] 检查模块导入')
    if 'flask' not in ' '.join(problems):
        try:
            from sticklab import webapp  # noqa: F401
            print('      ✅ 模块导入正常')
        except Exception as error:
            problems.append(f'模块导入失败: {type(error).__name__}: {error}')
            print(f'      ❌ {type(error).__name__}: {error}')
    else:
        print('      ⚪ 跳过（缺 flask）')

    print('[4/4] 检查引擎')
    try:
        from sticklab.engine import Engine
        engine = Engine()
        result = engine.process((0.0, 0.0), (0.4, -0.3), [False] * 21)
        print(f'      ✅ 引擎正常（右输出 {result["right"]["x"]:.3f}, '
              f'{result["right"]["y"]:.3f}）')
    except Exception as error:
        problems.append(f'引擎异常: {type(error).__name__}: {error}')
        print(f'      ❌ {type(error).__name__}: {error}')

    return len(problems) == 0, problems


def main():

    # 由服务自重启时设置：先等旧进程释放端口
    _delay = float(os.environ.get("STICKLAB_START_DELAY", "0") or 0)
    if _delay > 0:
        time.sleep(_delay)

    parser = argparse.ArgumentParser(description='StickLab 一键启动')
    parser.add_argument('--port', type=int, default=8899)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--no-output', action='store_true',
                        help='不自动开启虚拟 XInput 手柄')
    args = parser.parse_args()

    print('=' * 66)
    print('StickLab 启动器')
    print('=' * 66)
    print(f'  Python: {sys.executable}')
    print(f'  版本:   {sys.version.split()[0]}')
    print(f'  目录:   {BASE}')
    print()

    fast_start = os.environ.get('STICKLAB_FAST_START') == '1'
    if fast_start:
        print('（快速启动：跳过自检）')
        ok, problems = True, []
    else:
        ok, problems = check_environment()

    print('\n' + '-' * 66)
    if not ok:
        print('❌ 自检未通过，问题如下：')
        for item in problems:
            print('   • ' + item)
        print('\n修复后重新运行本脚本即可。')
        wait_for_enter()
        return 1

    print('✅ 自检通过')

    if args.check_only:
        print('\n（--check-only 模式，不启动服务）')
        wait_for_enter()
        return 0

    port = find_free_port(args.host, args.port)
    if port is None:
        print(f'❌ 端口 {args.port}~{args.port + 29} 都被占用')
        wait_for_enter()
        return 1
    if port != args.port:
        print(f'⚠️  端口 {args.port} 被占用，改用 {port}')

    url = f'http://{args.host}:{port}'
    print(f'\n🚀 正在启动服务: {url}')
    print('   服务运行期间请保持本窗口打开，关闭即停止。')
    print('   按 Ctrl+C 可退出\n')

    try:
        import threading

        from sticklab import webapp as web_module
    except Exception:
        traceback.print_exc()
        wait_for_enter('\n启动失败，按回车退出...')
        return 1

    # 引擎循环
    threading.Thread(target=web_module.lab.loop, daemon=True).start()

    # 后台自动检测物理手柄（不阻塞服务启动）
    auto_scan = getattr(web_module, 'auto_scan_hardware', None)
    if callable(auto_scan):
        threading.Thread(target=auto_scan, kwargs={'log': print}, daemon=True).start()
    else:
        print('[手柄] 该版本没有自动检测函数，请在网页"连接"页手动扫描')

    # 自动开启虚拟 XInput 手柄（让检测网站/游戏立刻能看到）
    auto_output = getattr(web_module, 'auto_enable_output', None)
    if callable(auto_output) and not args.no_output:
        threading.Thread(target=auto_output, kwargs={'log': print},
                         daemon=True).start()

    # 延迟打开浏览器（等服务起来）
    if not args.no_browser:
        def open_later():
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=open_later, daemon=True).start()

    try:
        web_module.app.run(host=args.host, port=port, debug=False,
                           use_reloader=False, threaded=True)
    except KeyboardInterrupt:
        print('\n已手动停止')
    except Exception:
        traceback.print_exc()
        print('\n❌ 服务启动失败')
        wait_for_enter()
        return 1
    finally:
        try:
            web_module.lab.running = False
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        wait_for_enter('\n发生未预期错误，按回车退出...')
        code = 1
    # 正常 Ctrl+C 退出时不阻塞；异常退出时已在上面停住
    sys.exit(code)
