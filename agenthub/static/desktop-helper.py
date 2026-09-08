#!/usr/bin/env python3
"""Run on the browser computer: explicitly mapped local file operations only.

Python 3.10+, standard library; Windows, macOS and Linux.
Configuration and pairing keys stay on this computer, outside the repository.
"""
import argparse
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

PORT = 18711
CONFIG = Path.home() / '.agenthub-desktop.json'


def local_target(config, request):
    node, raw, kind = (request.get(k) for k in ('node', 'path', 'kind'))
    if (not isinstance(node, str) or not re.fullmatch('[a-f0-9]{32}', node)
            or not isinstance(raw, str) or len(raw) > 4096
            or not raw.startswith('/') or '\x00' in raw or '\\' in raw
            or kind not in ('file', 'directory')):
        raise ValueError('无效的文件目标')
    remote = PurePosixPath(raw)
    if '..' in remote.parts:
        raise ValueError('路径不能包含上级目录跳转')
    matches = []
    for mapping in config.get('mappings', []):
        root = PurePosixPath(mapping['remote'])
        if mapping['node'] == node and remote.is_relative_to(root):
            matches.append((len(root.parts), mapping, remote.relative_to(root)))
    if not matches:
        raise ValueError('此节点路径没有本机目录映射，请在本机助手中配置')
    _, mapping, relative = max(matches, key=lambda item: item[0])
    # Reject platform-specific escape syntax in remote path components.
    if any(':' in part for part in relative.parts):
        raise ValueError('无法映射此路径')
    root = Path(mapping['local']).expanduser().resolve(strict=True)
    target = root.joinpath(*relative.parts).resolve(strict=True)
    if not target.is_relative_to(root):
        raise ValueError('本地路径超出已配置目录')
    if not (target.is_file() if kind == 'file' else target.is_dir()):
        raise ValueError('本地文件不存在或类型与节点不一致')
    return target


def open_target(target):
    # No shell, URLs or caller-supplied commands. Only a validated absolute path.
    if sys.platform == 'win32':
        os.startfile(str(target))
    else:
        command = 'open' if sys.platform == 'darwin' else 'xdg-open'
        argv = [command, str(target)]
        if sys.platform == 'darwin' and target.is_dir():
            argv = ['open', '-a', 'Finder', str(target)]
        result = subprocess.run(argv, capture_output=True, timeout=15)
        if result.returncode:
            raise OSError('默认应用或文件管理器未能打开此路径')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Never log pairing keys or personal paths.

    def allowed(self):
        return (self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'
                and self.headers.get('Origin') in self.server.config['origins'])

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if self.allowed():
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Vary', 'Origin')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.reply(200 if self.allowed() else 403, {})

    def do_POST(self):
        if not self.allowed():
            return self.reply(403, {'error': '此网页未获本机助手授权'})
        auth = self.headers.get('Authorization', '')
        if not hmac.compare_digest(auth.encode(), ('Bearer ' + self.server.config['token']).encode()):
            return self.reply(401, {'error': '本机助手配对码不正确'})
        if self.path not in ('/check', '/open'):
            return self.reply(404, {'error': '未知操作'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 16384:
                raise ValueError('请求大小无效')
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError('无效请求')
            target = local_target(self.server.config, request)
            if self.path == '/open':
                action = request.get('action')
                if action not in ('open-local', 'open-directory'):
                    raise ValueError('未知打开动作')
                if action == 'open-directory' and request['kind'] == 'file':
                    target = target.parent
                # Opening an executable is not a file viewing operation.
                if target.is_file() and target.suffix.lower() not in {
                        '.txt', '.log', '.md', '.rst', '.csv', '.json', '.yaml', '.yml',
                        '.toml', '.ini', '.pdf', '.docx', '.xlsx', '.pptx', '.odt',
                        '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff',
                        '.mp4', '.mov', '.webm', '.mkv', '.avi', '.mp3', '.wav', '.flac', '.ogg'}:
                    raise ValueError('此类型请在本地目录中手动打开')
                self.server.open_target(target)
            return self.reply(200, {'ok': True, 'path': str(target), 'kind': request['kind']})
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            # Do not leak unrelated local paths or OS exception details.
            return self.reply(400, {'error': '无法打开：请检查节点目录映射、本地路径及默认应用；可执行文件请从本地目录手动打开'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG)
    parser.add_argument('--origin', help='AgentHub 网页 origin，例如 https://hub.example.com')
    parser.add_argument('--node', help='网页菜单显示的节点 ID')
    parser.add_argument('--remote-root', help='节点目录的绝对路径')
    parser.add_argument('--local-root', help='浏览器电脑上已挂载/同步的对应目录')
    args = parser.parse_args()
    config = json.loads(args.config.read_text()) if args.config.exists() else {
        'token': secrets.token_urlsafe(32), 'origins': [], 'mappings': []}
    if any((args.origin, args.node, args.remote_root, args.local_root)):
        if not all((args.origin, args.node, args.remote_root, args.local_root)):
            parser.error('配置映射需要同时提供 --origin、--node、--remote-root、--local-root')
        url = urlsplit(args.origin)
        if (url.scheme not in ('http', 'https') or not url.netloc
                or url.username or url.password or url.path or url.query or url.fragment):
            parser.error('--origin 只能包含协议、主机和可选端口，不含路径')
        if not re.fullmatch('[a-f0-9]{32}', args.node):
            parser.error('节点 ID 无效')
        remote = PurePosixPath(args.remote_root)
        local = Path(args.local_root).expanduser().resolve(strict=True)
        if not remote.is_absolute() or '..' in remote.parts or not local.is_dir():
            parser.error('远端必须是绝对目录，本机目录必须存在')
        mapping = {'node': args.node, 'remote': str(remote), 'local': str(local)}
        config['mappings'] = [m for m in config['mappings']
                              if (m['node'], m['remote']) != (args.node, str(remote))] + [mapping]
        config['origins'] = sorted(set(config['origins'] + [args.origin]))
        args.config.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(config, out, ensure_ascii=False, indent=2)
    if not config['origins'] or not config['mappings']:
        parser.error('尚未配置网页来源与目录映射，使用 --help 查看参数')
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.config, server.open_target = config, open_target
    print('在浏览器的本地打开设置中输入配对码：', config['token'], flush=True)
    print(f'本机助手已启动：127.0.0.1:{PORT}；按 Ctrl+C 停止。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
