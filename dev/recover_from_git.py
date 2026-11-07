"""从 .git 对象库直接恢复文件（不依赖 git 命令）

背景: 清理命令误删了工作区文件，但 .git 里有完整历史。
这个脚本自己解析 zlib 压缩的 git 对象，把指定提交的文件写回工作区。

用法:
    python recover_from_git.py --list            # 只列出最新提交里的文件
    python recover_from_git.py                   # 恢复到工作区（已存在的不覆盖）
    python recover_from_git.py --force           # 覆盖已存在的文件
    python recover_from_git.py --commit <sha>    # 指定提交
"""

import argparse
import os
import sys
import zlib

BASE = os.path.dirname(os.path.abspath(__file__))
GIT_DIR = os.path.join(BASE, '.git')
OBJECTS_DIR = os.path.join(GIT_DIR, 'objects')


def read_object(sha):
    """读取一个 git 对象，返回 (类型, 内容bytes)"""
    path = os.path.join(OBJECTS_DIR, sha[:2], sha[2:])
    if not os.path.exists(path):
        return None, None
    with open(path, 'rb') as file_object:
        raw = zlib.decompress(file_object.read())
    header, _, content = raw.partition(b'\x00')
    kind = header.split(b' ')[0].decode('ascii', 'replace')
    return kind, content


def parse_commit(content):
    """解析提交对象，返回 tree sha 和 message"""
    text = content.decode('utf-8', 'replace')
    tree_sha = None
    message_lines = []
    in_message = False
    for line in text.split('\n'):
        if in_message:
            message_lines.append(line)
            continue
        if line.startswith('tree '):
            tree_sha = line.split(' ', 1)[1].strip()
        elif line == '':
            in_message = True
    return tree_sha, '\n'.join(message_lines).strip()


def parse_tree(content):
    """解析树对象，返回 [(mode, name, sha), ...]

    注意: 树里存的是 **20 字节二进制** SHA（不是 40 字符 hex），
    这里必须先按二进制取 20 字节再 .hex()，否则算出来的 SHA 是错的。
    """
    entries = []
    index = 0
    while index < len(content):
        space = content.index(b' ', index)
        mode = content[index:space].decode('ascii')
        nul = content.index(b'\x00', space)
        name = content[space + 1:nul].decode('utf-8', 'replace')
        sha = content[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        index = nul + 21
    return entries


def walk_tree(tree_sha, prefix=''):
    """递归遍历树，返回 [(路径, blob sha), ...]

    模式判断要注意: 普通文件是 '100644'/'100755'，目录是 '40000'。
    之前用 startswith('40') 会把所有文件当成目录跳过。
    """
    kind, content = read_object(tree_sha)
    if kind != 'tree':
        return []
    files = []
    for mode, name, sha in parse_tree(content):
        path = f'{prefix}{name}'
        if mode == '40000':
            files.extend(walk_tree(sha, path + '/'))
        else:
            files.append((path, sha))
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', default=None, help='提交 sha（默认用 .git/refs/heads/main）')
    parser.add_argument('--list', action='store_true', help='只列出文件，不写出')
    parser.add_argument('--force', action='store_true', help='覆盖已存在的文件')
    args = parser.parse_args()

    commit_sha = args.commit
    if commit_sha is None:
        head_ref_path = os.path.join(GIT_DIR, 'refs', 'heads', 'main')
        if os.path.exists(head_ref_path):
            with open(head_ref_path, 'r', encoding='ascii') as file_object:
                commit_sha = file_object.read().strip()
    if not commit_sha:
        print('找不到提交 sha')
        return 1

    kind, content = read_object(commit_sha)
    if kind != 'commit':
        print(f'{commit_sha} 不是提交对象（是 {kind}）')
        return 1
    tree_sha, message = parse_commit(content)
    print(f'提交: {commit_sha}')
    print(f'说明: {message}')
    print(f'树:   {tree_sha}')

    files = walk_tree(tree_sha)
    print(f'\n该提交包含 {len(files)} 个文件:')
    for path, sha in files:
        kind, blob = read_object(sha)
        size = len(blob) if blob is not None else -1
        marker = ''
        if not args.list and os.path.exists(os.path.join(BASE, path)) and not args.force:
            marker = '  [已存在, 跳过]'
        print(f'  {size:>8} 字节  {path}{marker}')

    if args.list:
        return 0

    print('\n开始恢复…')
    restored = skipped = 0
    for path, sha in files:
        target = os.path.join(BASE, path.replace('/', os.sep))
        if os.path.exists(target) and not args.force:
            skipped += 1
            continue
        kind, blob = read_object(sha)
        if kind != 'blob' or blob is None:
            print(f'  跳过（不是 blob）: {path}')
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as file_object:
            file_object.write(blob)
        restored += 1
        print(f'  已恢复: {path} ({len(blob)} 字节)')
    print(f'\n完成: 恢复 {restored} 个, 跳过 {skipped} 个')
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
