r"""Switch 2 Pro 手柄连接检测（GUI，不会闪退）

功能:
    1. 启动后自动检测，每档最多 2 秒；收到数据即出结果
    2. 检测到 -> 显示「连接成功」+ 设备信息 + 实测帧率/帧间隔
    3. 2 秒收不到 -> 立刻尝试激活，再给一档机会（总上限约 2~5 秒）
    4. 仍收不到 -> 显示「未检测到」+ 【重新检测】按钮
    5. 连接成功后可【启动主程序】，或勾选“连接成功后自动启动主程序”

为什么用 GUI:
    控制台脚本在 input() 读不到键盘输入时会抛 EOFError 直接退出，窗口跟着关掉，
    看起来就是“闪退”。GUI 窗口不会有这个问题。

运行:
    python .\connection_check_gui.py

依赖: Python 自带库 + hid（hidapi）。pyusb 可选（仅用于激活）。
      主程序需要的 pygame / vgamepad / pyautogui 由本程序自动挑解释器。
"""

import ctypes
import os
import statistics
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# ==================== 要被启动的主程序配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MAIN_PROGRAM = os.path.join(BASE_DIR, 'Switch2ProControllerWorkOnPC.txt')
# 主程序需要的库（pygame / vgamepad / pyautogui）装在哪个解释器就填哪个；
# 留空时自动挑选（优先本程序所在解释器，其次带这些库的 Python）。
PREFERRED_PYTHON = ''
REQUIRED_MODULES_FOR_MAIN = ('pygame', 'vgamepad', 'pyautogui')

# 让主程序在新控制台窗口里运行，这样它的输出不会被本界面吞掉，
# 而且要发键盘映射（pyautogui）时需要真实窗口焦点。
CREATE_NEW_CONSOLE = 0x00000010

NINTENDO_VID = 0x057E
SWITCH2_PRO_PID = 0x2069

HID_CHUNK_TIMEOUT_MS = 250
SCAN_SECONDS = 2.0           # 每一档最多扫描 2 秒；收到数据立即出结果
MIN_FRAMES_FOR_SUCCESS = 20  # 连续收到这么多帧即判定成功，立即出结果
MIN_MEASURE_SECONDS = 1.0    # 判定成功后至少测这么久，好把频率测准
MAX_ATTEMPTS = 2             # 第一档收不到就去激活，再给一档机会（总上限约 2~5 秒）

# ProCon2Tool 的 17 条激活命令（原样字节，用途见 native_reader.py）
TOOL_COMMANDS = [
    bytes([0x03, 0x91, 0x00, 0x0d, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00,
           0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF]),
    bytes([0x07, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x16, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x15, 0x91, 0x00, 0x01, 0x00, 0x0e, 0x00, 0x00, 0x00, 0x02] + [0xFF] * 12),
    bytes([0x15, 0x91, 0x00, 0x02, 0x00, 0x11, 0x00, 0x00, 0x00] + [0xFF] * 16),
    bytes([0x15, 0x91, 0x00, 0x03, 0x00, 0x01, 0x00, 0x00, 0x00]),
    bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08] + [0x00] * 10),
    bytes([0x0c, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
    bytes([0x11, 0x91, 0x00, 0x03, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x0a, 0x91, 0x00, 0x08, 0x00, 0x14, 0x00, 0x00, 0x01] + [0xff] * 8
          + [0x35, 0x00, 0x46] + [0x00] * 8),
    bytes([0x0c, 0x91, 0x00, 0x04, 0x00, 0x04, 0x00, 0x00, 0x27, 0x00, 0x00, 0x00]),
    bytes([0x03, 0x91, 0x00, 0x0a, 0x00, 0x04, 0x00, 0x00, 0x09, 0x00, 0x00, 0x00]),
    bytes([0x10, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x01, 0x91, 0x00, 0x0c, 0x00, 0x00, 0x00, 0x00]),
    bytes([0x03, 0x91, 0x00, 0x01, 0x00, 0x00, 0x00]),
    bytes([0x0a, 0x91, 0x00, 0x02, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0x00]),
    bytes([0x09, 0x91, 0x00, 0x07, 0x00, 0x08, 0x00, 0x00, 0x01] + [0x00] * 7),
]


# ==================== 底层：查找 / 激活 / 读取 ====================
_libusb_dll_cache = {}


def find_libusb_dll():
    """跨 Python 环境找 libusb-1.0.dll（pyusb 自己在 site-packages 里找不到）

    结果会缓存，避免每次激活都重新搜索文件系统（否则第二档会明显变慢）。
    """
    import glob

    if 'path' in _libusb_dll_cache:
        return _libusb_dll_cache['path']

    candidates = []
    try:
        import libusb_package
        dll = libusb_package.find_library('libusb-1.0')
        if dll:
            candidates.append(dll)
    except Exception:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ('libusb-1.0.dll', 'libusb-1.0.so', 'libusb-1.0.dylib'):
        candidates.append(os.path.join(here, name))
    candidates.extend([r'C:\Windows\System32\libusb-1.0.dll',
                       r'C:\Windows\SysWOW64\libusb-1.0.dll'])

    local_appdata = os.environ.get('LOCALAPPDATA') or ''
    for root in (os.path.join(local_appdata, 'Programs', 'Python'), r'C:\Python*'):
        for pattern in ('**/libusb-1.0.dll', '**/libusb*.dll'):
            try:
                candidates.extend(glob.glob(os.path.join(root, pattern), recursive=True))
            except Exception:
                pass

    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            _libusb_dll_cache['path'] = candidate
            return candidate
    _libusb_dll_cache['path'] = None
    return None


def enumerate_controller():
    """返回手柄的 HID 接口信息列表（不需要 pyusb）"""
    import hid as hidapi
    devices = hidapi.enumerate(NINTENDO_VID, SWITCH2_PRO_PID)
    result = []
    for info in devices:
        result.append({
            'path': info['path'],
            'product': info.get('product_string') or '',
            'interface': info.get('interface_number'),
            'usage_page': info.get('usage_page'),
            'usage': info.get('usage'),
        })
    return result


def try_activate(log):
    """尝试激活（让手柄开始输出）。缺 pyusb/libusb 时返回 False 并写日志"""
    try:
        import usb.core
        import usb.util
        from usb.backend import libusb1
    except ImportError as error:
        log(f'跳过激活: 没装 pyusb（{error}）')
        return False

    dll = find_libusb_dll()
    if dll is None:
        log('跳过激活: 找不到 libusb-1.0.dll')
        log('  办法: pip install libusb，或把 libusb-1.0.dll 放到本程序目录')
        return False
    backend = libusb1.get_backend(find_library=lambda name: dll)
    if backend is None:
        log('跳过激活: libusb 后端加载失败')
        return False

    try:
        device = usb.core.find(idVendor=NINTENDO_VID, idProduct=SWITCH2_PRO_PID,
                               backend=backend)
        if device is None:
            log('跳过激活: pyusb 没找到设备')
            return False

        interface_number = out_endpoint = in_endpoint = None
        for interface in device.get_active_configuration():
            if interface.bInterfaceNumber == 1:
                interface_number = 1
                for endpoint in interface:
                    if (endpoint.bmAttributes & 0x03) != 0x02:
                        continue
                    if endpoint.bEndpointAddress & 0x80:
                        in_endpoint = endpoint.bEndpointAddress
                    else:
                        out_endpoint = endpoint.bEndpointAddress
        if interface_number is None or out_endpoint is None:
            log('跳过激活: 厂商接口(bulk)不可用')
            return False

        usb.util.claim_interface(device, interface_number)
        for command in TOOL_COMMANDS:
            try:
                device.write(out_endpoint, command, timeout=500)
            except Exception:
                pass
            time.sleep(0.004)
        # 排空回执，避免被当成输入数据
        deadline = time.perf_counter() + 0.5
        while time.perf_counter() < deadline:
            try:
                if not device.read(in_endpoint, 64, timeout=120):
                    break
            except Exception:
                break
        log('已发送激活序列（17 条命令）')
        try:
            usb.util.release_interface(device, interface_number)
        except Exception:
            pass
        return True
    except Exception as error:
        log(f'激活失败: {type(error).__name__}: {error}')
        return False


def scan_once(log, stop_event, progress_label=None):
    """扫描并判断连接，返回结果字典

    行为:
      - 一旦确认收到数据流，**立即返回成功**（不空等）
      - 每档 SCAN_SECONDS 秒；收不到就先激活，再给一档（最多 MAX_ATTEMPTS 档）
      - 判定成功需要连续 MIN_FRAMES_FOR_SUCCESS 帧，避免把残留回执当成数据流
    """
    result = {'found': False, 'reason': '', 'frames': 0, 'hz': 0.0,
              'median_ms': 0.0, 'product': '', 'interfaces': 0,
              'activated': False, 'elapsed': 0.0}

    log('正在枚举 HID 接口…')
    try:
        interfaces = enumerate_controller()
    except ImportError:
        result['reason'] = '缺少 hid 库（pip install hid）'
        log(result['reason'])
        return result
    except Exception as error:
        result['reason'] = f'枚举失败: {type(error).__name__}: {error}'
        log(result['reason'])
        return result

    result['interfaces'] = len(interfaces)
    if not interfaces:
        result['reason'] = '没有找到 VID 057E / PID 2069 的 HID 接口（手柄未连接？）'
        log(result['reason'])
        return result

    # 优先选标准游戏手柄集合
    chosen = None
    for info in interfaces:
        if info.get('usage_page') == 0x0001 and info.get('usage') == 0x0005:
            chosen = info
            break
    if chosen is None:
        chosen = interfaces[0]
    result['product'] = chosen['product'] or chosen['path']
    log(f'找到接口 {len(interfaces)} 个，使用: {result["product"]}')

    def open_device():
        import hid as hidapi
        device = hidapi.device()
        device.open_path(chosen['path'])
        device.set_nonblocking(0)
        return device

    def count_frames(device, seconds, tick=None, stop_after_frames=None):
        """读取若干秒；stop_after_frames 达到后立即返回（用于“成功就马上出结果”）"""
        frames = 0
        intervals = []
        started = time.perf_counter()
        previous = None
        while time.perf_counter() - started < seconds:
            if stop_event.is_set():
                break
            try:
                data = device.read(64, HID_CHUNK_TIMEOUT_MS)
            except Exception:
                data = None
            now = time.perf_counter()
            if data:
                frames += 1
                if previous is not None:
                    intervals.append((now - previous) * 1000.0)
                previous = now
            if tick is not None:
                tick(now - started, seconds, frames)
            if frames > 0 and stop_after_frames is not None:
                elapsed = now - started
                if frames >= stop_after_frames and elapsed >= MIN_MEASURE_SECONDS:
                    break
        return {'frames': frames, 'intervals': intervals,
                'elapsed': time.perf_counter() - started}

    device = None
    try:
        device = open_device()
    except Exception as error:
        result['reason'] = f'打开 HID 接口失败: {type(error).__name__}: {error}'
        log(result['reason'])
        return result

    started_at = time.perf_counter()
    try:
        first = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            log(f'第 {attempt}/{MAX_ATTEMPTS} 档：扫描数据流（最多 {SCAN_SECONDS:.0f} 秒）…')
            first = count_frames(
                device, SCAN_SECONDS,
                (lambda elapsed, total, frames, n=attempt:
                 progress_label(elapsed, total, frames, n)) if progress_label else None,
                stop_after_frames=MIN_FRAMES_FOR_SUCCESS)
            if first['frames'] > 0:
                break
            if attempt < MAX_ATTEMPTS:
                # 收不到数据才尝试激活，然后再扫一档
                log(f'{SCAN_SECONDS:.0f} 秒内没有数据，尝试激活手柄…')
                result['activated'] = try_activate(log)
                if not result['activated']:
                    log('激活未成功，再扫最后一档（手柄可能已被其它程序激活）')

        result['frames'] = first['frames'] if first else 0
        result['elapsed'] = time.perf_counter() - started_at
        if first and first['frames'] > 0:
            result['found'] = True
            intervals = first['intervals']
            result['hz'] = (first['frames'] / first['elapsed']) if first['elapsed'] else 0.0
            if intervals:
                result['median_ms'] = statistics.median(intervals)
                result['stdev_ms'] = (statistics.pstdev(intervals)
                                      if len(intervals) > 1 else 0.0)
            log(f'已确认数据流：{first["frames"]} 帧 / {result["hz"]:.0f} Hz '
                f'(耗时 {result["elapsed"]:.1f}s)')
        else:
            result['reason'] = (f'设备在，但 {SCAN_SECONDS * MAX_ATTEMPTS:.0f} 秒内没有收到'
                                '任何数据（未激活或手柄休眠）')
            log(result['reason'])
    finally:
        try:
            device.close()
        except Exception:
            pass
    return result


def find_python_for_main_program(log=None):
    """挑一个装了主程序所需库的 Python 解释器"""
    import glob

    def has_modules(exe):
        if not exe or not os.path.exists(exe):
            return False
        try:
            completed = subprocess.run(
                [exe, '-c', 'import ' + ', '.join(REQUIRED_MODULES_FOR_MAIN)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0)
            return completed.returncode == 0
        except Exception:
            return False

    candidates = []
    if PREFERRED_PYTHON:
        candidates.append(PREFERRED_PYTHON)
    candidates.append(sys.executable)
    local_appdata = os.environ.get('LOCALAPPDATA') or ''
    for pattern in (os.path.join(local_appdata, 'Programs', 'Python', 'Python*', 'python.exe'),
                    r'C:\Python*\python.exe'):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))

    seen = set()
    for candidate in candidates:
        candidate = os.path.normcase(os.path.abspath(candidate)) if candidate else ''
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if has_modules(candidate):
            if log:
                log(f'使用解释器: {candidate}')
            return candidate
    if log:
        log('没找到同时装了 pygame / vgamepad / pyautogui 的 Python')
    return None


# ==================== GUI ====================
class App(object):
    def __init__(self, root):
        self.root = root
        self.stop_event = threading.Event()
        self.worker = None
        self.log_queue = []
        self.result = None
        self.lock = threading.Lock()
        self.main_process = None
        self.last_result = None

        root.title('Switch 2 Pro 手柄连接检测')
        root.geometry('720x460')
        root.minsize(560, 380)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill='both', expand=True)

        self.status_var = tk.StringVar(value='准备中…')
        self.status_label = ttk.Label(frame, textvariable=self.status_var,
                                      font=('Microsoft YaHei UI', 15, 'bold'))
        self.status_label.pack(anchor='w')

        self.detail_var = tk.StringVar(value='')
        self.detail_label = ttk.Label(frame, textvariable=self.detail_var,
                                      font=('Microsoft YaHei UI', 10))
        self.detail_label.pack(anchor='w', pady=(6, 0))

        self.progress = ttk.Progressbar(frame, mode='determinate', maximum=100)
        self.progress.pack(fill='x', pady=10)

        log_frame = ttk.LabelFrame(frame, text='检测日志', padding=6)
        log_frame.pack(fill='both', expand=True)
        self.log_text = tk.Text(log_frame, height=12, wrap='word',
                                font=('Consolas', 9), state='disabled')
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.log_text.pack(side='left', fill='both', expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(10, 0))
        self.retry_button = ttk.Button(buttons, text='重新检测', command=self.start_scan,
                                       state='disabled')
        self.retry_button.pack(side='left')
        self.launch_button = ttk.Button(buttons, text='启动主程序', command=self.launch_main_program,
                                        state='disabled')
        self.launch_button.pack(side='left', padx=6)
        ttk.Button(buttons, text='退出', command=self.on_close).pack(side='left', padx=6)

        self.auto_launch_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(buttons, text='连接成功后自动启动主程序',
                        variable=self.auto_launch_var).pack(side='left', padx=10)

        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.after(100, self.start_scan)
        self.root.after(100, self.drain_queues)
        self.root.after(500, self.poll_main_process)

    # ---- 日志 ----
    def log(self, message):
        with self.lock:
            self.log_queue.append(message)

    def log_progress(self, elapsed, total, frames, attempt=1):
        self.progress['value'] = min(100.0, elapsed / total * 100.0)
        with self.lock:
            self.log_queue.append(f'__PROGRESS__{elapsed:.1f}|{total:.0f}|{frames}|{attempt}')

    def drain_queues(self):
        with self.lock:
            pending = self.log_queue
            self.log_queue = []
        for message in pending:
            if message.startswith('__PROGRESS__'):
                payload = message.split('__PROGRESS__', 1)[1]
                parts = payload.split('|')
                elapsed, total, frames = parts[0], parts[1], parts[2]
                attempt = parts[3] if len(parts) > 3 else '1'
                self.status_var.set(f'检测中… 第 {attempt}/{MAX_ATTEMPTS} 档 '
                                    f'{elapsed}s / {total}s（已收 {frames} 帧）')
                continue
            self.log_text.configure(state='normal')
            self.log_text.insert('end', message + '\n')
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        if self.result is not None:
            result = self.result
            self.result = None
            self.show_result(result)
        self.root.after(100, self.drain_queues)

    # ---- 扫描 ----
    def start_scan(self, *_):
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.retry_button.configure(state='disabled')
        self.launch_button.configure(state='disabled')   # 检测成功后再亮
        self.progress['value'] = 0
        self.status_var.set('正在检测手柄…')
        self.detail_var.set('')

        def worker():
            try:
                result = scan_once(self.log, self.stop_event, self.log_progress)
            except Exception as error:
                result = {'found': False, 'reason': f'内部错误: {type(error).__name__}: {error}',
                          'frames': 0, 'hz': 0.0, 'product': '', 'interfaces': 0,
                          'activated': False}
            self.result = result

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def show_result(self, result):
        self.last_result = result
        if result.get('found'):
            hz = result.get('hz', 0.0)
            median_ms = result.get('median_ms', 0.0)
            stdev_ms = result.get('stdev_ms', 0.0)
            self.status_var.set('✅ 连接成功')
            self.detail_var.set(
                f"设备: {result.get('product', '')}\n"
                f"数据流: {result.get('frames', 0)} 帧 / 约 {hz:.0f} Hz"
                f"（帧间隔中位数 {median_ms:.2f} ms，抖动 {stdev_ms:.2f} ms）\n"
                f"检测耗时: {result.get('elapsed', 0):.1f} 秒"
                f"（激活: {'已完成' if result.get('activated') else '无需激活'}）\n"
                f"现在可以点【启动主程序】。")
            self.log('—— 连接成功 ——')
            self.launch_button.configure(state='normal')
            if self.auto_launch_var.get():
                self.log('已勾选自动启动，正在启动主程序…')
                self.root.after(300, self.launch_main_program)
        else:
            self.status_var.set('❌ 未检测到手柄数据')
            self.detail_var.set(result.get('reason', '') + '\n'
                                '请确认：USB 线已插好、手柄未休眠，然后点【重新检测】。')
            self.log('—— 未检测到 ——')
            self.launch_button.configure(state='disabled')
        self.progress['value'] = 100
        self.retry_button.configure(state='normal')

    # ---- 启动主程序 ----
    def launch_main_program(self, *_):
        if self.main_process is not None and self.main_process.poll() is None:
            self.log('主程序已在运行中')
            return
        if self.last_result is None or not self.last_result.get('found'):
            self.log('还没检测到连接，先完成检测再启动')
            return
        if not os.path.exists(DEFAULT_MAIN_PROGRAM):
            messagebox.showerror('找不到主程序',
                                 f'文件不存在:\n{DEFAULT_MAIN_PROGRAM}')
            return

        python_exe = find_python_for_main_program(self.log)
        if python_exe is None:
            messagebox.showerror(
                '缺少运行环境',
                '没找到同时装了 pygame / vgamepad / pyautogui 的 Python。\n'
                '可在本文件顶部把 PREFERRED_PYTHON 设成正确的 python.exe 路径。')
            return

        self.log(f'启动主程序: {os.path.basename(DEFAULT_MAIN_PROGRAM)}')
        self.log('（新窗口里运行；它会把手柄按键映射成键盘，请注意当前焦点窗口）')
        try:
            self.main_process = subprocess.Popen(
                [python_exe, DEFAULT_MAIN_PROGRAM],
                cwd=BASE_DIR,
                creationflags=CREATE_NEW_CONSOLE)
        except Exception as error:
            self.log(f'启动失败: {type(error).__name__}: {error}')
            messagebox.showerror('启动失败', f'{type(error).__name__}: {error}')
            return
        self.launch_button.configure(state='disabled')
        self.status_var.set('✅ 已连接 · 主程序运行中')

    def poll_main_process(self):
        """监视主程序：退出后恢复按钮并提示"""
        if self.main_process is not None:
            returncode = self.main_process.poll()
            if returncode is None:
                self.root.after(500, self.poll_main_process)
                return
            self.log(f'主程序已退出（返回码 {returncode}）')
            if returncode != 0:
                self.log('返回码非 0：多半是运行中报错了，请查看那个窗口的最后几行')
            self.main_process = None
            if self.last_result is not None and self.last_result.get('found'):
                self.launch_button.configure(state='normal')
                self.status_var.set('✅ 连接成功')
        self.root.after(500, self.poll_main_process)

    def on_close(self):
        self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
