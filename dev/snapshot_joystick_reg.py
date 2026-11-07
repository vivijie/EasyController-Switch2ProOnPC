"""快照 HKCU 手柄注册表，用于对比 joy.cpl「首选设备」改动前后的差异

用法:
    python .\snapshot_joystick_reg.py before
    （在 joy.cpl 里设置首选设备）
    python .\snapshot_joystick_reg.py after
    python .\snapshot_joystick_reg.py diff
"""

import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = r'HKCU\System\CurrentControlSet\Control\MediaProperties\PrivateProperties\Joystick'
TARGETS = ('before.json', 'after.json')


def path_for(name):
    return os.path.join(BASE, '.reg_' + name)


def dump_tree():
    """递归导出注册表子树，返回 {key: {value_name: data}}"""
    result = {}

    def walk(key):
        try:
            output = subprocess.run(['reg', 'query', key, '/s'],
                                    capture_output=True, text=True,
                                    encoding='mbcs', errors='replace').stdout
        except Exception as error:
            print('reg query 失败: %s' % error)
            return
        current = None
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('HKEY_'):
                current = stripped
                result.setdefault(current, {})
                continue
            parts = line.split('    ')
            if current and len(parts) >= 3:
                name = parts[0].strip()
                kind = parts[1].strip()
                data = parts[2].strip()
                result[current][name] = '%s|%s' % (kind, data)

    walk(ROOT)
    return result


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else 'before'
    if action in TARGETS or action in ('before', 'after'):
        data = dump_tree()
        path = path_for(action + '.json')
        with open(path, 'w', encoding='utf-8') as file_object:
            json.dump(data, file_object, ensure_ascii=False, indent=2)
        total = sum(len(v) for v in data.values())
        print('已保存 %s：%d 个键 / %d 个值' % (path, len(data), total))
        return 0

    if action == 'diff':
        before_path = path_for('before.json')
        after_path = path_for('after.json')
        if not (os.path.exists(before_path) and os.path.exists(after_path)):
            print('缺 before.json 或 after.json，先各跑一次')
            return 1
        before = json.load(open(before_path, encoding='utf-8'))
        after = json.load(open(after_path, encoding='utf-8'))
        found = False
        for key in sorted(set(before) | set(after)):
            a = before.get(key, {})
            b = after.get(key, {})
            for name in sorted(set(a) | set(b)):
                if a.get(name) != b.get(name):
                    found = True
                    print('变化:')
                    print('  键: %s' % key)
                    print('  值: %s' % name)
                    print('  旧: %s' % a.get(name, '(不存在)'))
                    print('  新: %s' % b.get(name, '(不存在)'))
        if not found:
            print('没有检测到变化——可能 joy.cpl 写在别的注册表位置')
        return 0

    print('用法: python .\\snapshot_joystick_reg.py before|after|diff')
    return 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
