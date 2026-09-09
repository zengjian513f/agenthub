"""Authenticated operator file operations, durable jobs and a separate file trash.

Never invoke a shell. Jobs publish through temporary siblings and preserve replaced
destinations in trash. Links are copied/deleted as links, not followed recursively.
"""
from __future__ import annotations

import errno
import ctypes
from functools import lru_cache
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
import zipfile

CHUNK = 1024 * 1024
UPLOAD_CHUNK = 8 * CHUNK
MAX_ITEMS = 2000
PREVIEW_LIMIT = CHUNK
MEDIA = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.gif': 'image/gif', '.webp': 'image/webp', '.avif': 'image/avif',
         '.bmp': 'image/bmp', '.mp4': 'video/mp4', '.webm': 'video/webm',
         '.mov': 'video/quicktime', '.mp3': 'audio/mpeg', '.wav': 'audio/wav',
         '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.flac': 'audio/flac', '.pdf': 'application/pdf'}
_thumbnail_slots = threading.BoundedSemaphore(2)


@lru_cache(maxsize=256)
def thumbnail(path, modified, size):
    if not MEDIA.get(Path(path).suffix.lower(), '').startswith(('image/', 'video/')):
        raise ValueError('此格式没有缩略图')
    with _thumbnail_slots:
        try:
            result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-threads', '1',
                '-protocol_whitelist', 'file,pipe', '-i', path, '-frames:v', '1',
                '-vf', 'scale=192:192:force_original_aspect_ratio=decrease',
                '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1'],
                capture_output=True, timeout=10, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError('无法生成缩略图') from exc
    return result.stdout


class Conflict(ValueError):
    pass


class Cancelled(Exception):
    pass


def scope_for(view):
    return hashlib.sha256((str(view.get('uid', '')) + '\0' + str(view.get('path', ''))
                           + '\0' + str(view.get('agent_id', ''))).encode()).hexdigest()


def name_valid(value):
    if not isinstance(value, str) or not value or value in {'.', '..'} or len(value.encode()) > 255 \
            or any(c in value for c in '/\\\x00'):
        raise ValueError('名称无效：不能包含 /、\\ 或空字符')
    return value


def path_for(raw, *, exists=True):
    """Resolve parents but retain a final symlink for rename/trash/copy semantics."""
    if not isinstance(raw, str) or not raw.startswith('/') or '\x00' in raw or len(raw) > 4096:
        raise ValueError('需要有效的绝对路径')
    original = Path(os.path.normpath(raw))
    path = original.parent.resolve(strict=True) / original.name if original.name else original
    if exists:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ValueError('不支持设备、管道或套接字')
    return path


def snapshot(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def exists(path):
    return os.path.lexists(path)


def remove(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def rename_noreplace(source, destination):
    """Linux atomic no-clobber rename, including directories and symlinks."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def describe(path):
    info = path.lstat()
    result = {'name': path.name, 'path': str(path), 'size': info.st_size,
              'modified': info.st_mtime, 'mode': stat.filemode(info.st_mode),
              'owner': info.st_uid, 'group': info.st_gid,
              'kind': 'symlink' if path.is_symlink() else 'directory' if path.is_dir() else 'file',
              'mime': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}
    if path.is_symlink():
        result['link_target'] = os.readlink(path)
    if path.is_file():
        result['preview'] = MEDIA.get(path.suffix.lower(), 'text')
        if result['preview'] == 'text':
            with path.open('rb') as stream:
                data = stream.read(PREVIEW_LIMIT + 1)
            try:
                if b'\0' in data:
                    raise ValueError()
                result['text'] = data[:PREVIEW_LIMIT].decode('utf-8', errors='replace')
                result['truncated'] = len(data) > PREVIEW_LIMIT
            except ValueError:
                result['preview'] = 'unsupported'
    return result


class Manager:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ['jobs', 'trash', 'uploads', 'artifacts']:
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.operations = threading.RLock()
        self.running = set()
        self.cancels = set()
        self.last_save = {}
        try:
            self.grants = {tuple(pair) for pair in json.loads((self.root / 'grants.json').read_text())}
        except (FileNotFoundError, ValueError):
            self.grants = set()
        # Do not silently replay filesystem mutations after process interruption.
        for item in (self.root / 'jobs').glob('*.json'):
            try:
                job = json.loads(item.read_text())
                if job['state'] in {'queued', 'running'}:
                    job.update(state='interrupted', error='服务重启，已完成项目保留；可重试剩余项目')
                    self.save(job)
                if job['action'] == 'upload' and job['state'] != 'completed':
                    partial = self.root / 'uploads' / job['id']
                    if not partial.exists():
                        partial.touch()
                    job['bytes'] = partial.stat().st_size
                    self.save(job)
            except (OSError, ValueError, KeyError):
                continue

    def write_json(self, path, value):
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=True)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def save(self, job):
        with self.lock:
            job['updated'] = time.time()
            self.write_json(self.root / 'jobs' / (job['id'] + '.json'), job)

    def grant(self, scope, ref):
        with self.lock:
            self.grants.add((scope, ref))
            self.write_json(self.root / 'grants.json', sorted(self.grants))

    def get(self, scope, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('无效的任务编号')
        with self.lock:
            job = json.loads((self.root / 'jobs' / (ident + '.json')).read_text())
        if job['scope'] != scope:
            raise PermissionError('任务不属于此会话')
        return job

    def jobs(self, scope):
        jobs = []
        with self.lock:
            for path in (self.root / 'jobs').glob('*.json'):
                try:
                    job = json.loads(path.read_text())
                    if job['scope'] == scope:
                        jobs.append(self.public(job))
                except (ValueError, OSError, KeyError):
                    continue
        return sorted(jobs, key=lambda j: j['created'], reverse=True)[:100]

    @staticmethod
    def public(job):
        return {key: value for key, value in job.items()
                if key not in {'scope', 'spec', 'snapshots', 'temp'}}

    def guard(self, path):
        resolved = path.resolve()
        if path == Path('/') or path == Path.home() or self.root == resolved \
                or self.root in resolved.parents or resolved in self.root.parents:
            raise ValueError('不能修改根目录、用户主目录或文件管理器的数据目录')

    def trash_list(self, scope):
        result = []
        with self.lock:
            for path in (self.root / 'trash').glob('*/manifest.json'):
                try:
                    item = json.loads(path.read_text())
                    ready = item.get('state', 'ready') == 'ready' or not exists(Path(item['path']))
                    if item['scope'] == scope and ready and exists(path.parent / 'data'):
                        result.append({k: v for k, v in item.items() if k != 'scope'})
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(result, key=lambda x: x['deleted'], reverse=True)

    def trash_item(self, scope, ident):
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident):
            raise ValueError('无效的回收站编号')
        folder = self.root / 'trash' / ident
        item = json.loads((folder / 'manifest.json').read_text())
        if item['scope'] != scope:
            raise PermissionError('回收站项目不属于此会话')
        return folder, item

    def trash(self, path, scope, job=None):
        self.guard(path)
        folder = self.root / 'trash' / uuid.uuid4().hex
        folder.mkdir()
        manifest = {'id': folder.name, 'scope': scope, 'path': str(path), 'name': path.name,
                    'deleted': time.time(), 'state': 'preparing'}
        self.write_json(folder / 'manifest.json', manifest)
        try:
            rename_noreplace(path, folder / 'data')
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            if job is None:
                shutil.move(str(path), str(folder / 'data'))
            else:
                try:
                    self.copy(path, folder / 'data', job)
                    self.check(job)
                except Exception:
                    if exists(folder / 'data'):
                        remove(folder / 'data')
                    raise
                remove(path)
        manifest['state'] = 'ready'
        self.write_json(folder / 'manifest.json', manifest)
        return folder.name

    def target(self, dest, conflict, scope, job=None):
        self.guard(dest)
        if not exists(dest):
            return dest
        if conflict == 'skip':
            return None
        if conflict == 'keep':
            stem, suffix = dest.stem, dest.suffix
            for number in range(1, 100000):
                candidate = dest.with_name(f'{stem} ({number}){suffix}')
                if not exists(candidate):
                    return candidate
            raise ValueError('无法生成唯一名称')
        if conflict == 'replace':
            self.trash(dest, scope, job)
            return dest
        raise Conflict('目标已存在，请选择覆盖、跳过或保留两份')

    def start(self, scope, spec):
        spec = dict(spec)
        action = spec.get('action')
        if action not in {'mkdir', 'new-file', 'rename', 'copy', 'move', 'delete', 'trash', 'restore',
                          'purge', 'compress', 'extract', 'bundle', 'upload'}:
            raise ValueError('未知文件操作')
        if spec.get('conflict', 'error') not in {'error', 'skip', 'keep', 'replace'}:
            raise ValueError('无效的重名处理方式')
        paths = spec.get('paths', [])
        if not isinstance(paths, list) or len(paths) > MAX_ITEMS or any(not isinstance(p, str) for p in paths):
            raise ValueError('选择的文件过多或无效')
        if action in {'restore', 'purge'}:
            for ident in paths:
                self.trash_item(scope, ident)
        else:
            paths = list(dict.fromkeys(str(path_for(p)) for p in paths))
            # Selecting a parent and its child must not operate on the child twice.
            paths = [p for p in paths if not any(Path(other) in Path(p).parents for other in paths if other != p)]
            for raw in paths:
                if action != 'bundle':
                    self.guard(Path(raw))
        if action in {'rename', 'extract'} and len(paths) != 1:
            raise ValueError('请选择一个项目')
        if action not in {'mkdir', 'new-file', 'upload'} and not paths:
            raise ValueError('请先选择项目')
        if action in {'mkdir', 'new-file', 'rename', 'compress', 'upload'}:
            name_valid(spec.get('name'))
        if action in {'copy', 'move', 'mkdir', 'new-file', 'compress', 'extract', 'upload'}:
            dest = path_for(spec.get('destination'))
            if not dest.is_dir():
                raise ValueError('目标必须是目录')
            spec['destination'] = str(dest.resolve())
            for raw in paths:
                source = Path(raw)
                if source.is_dir() and not source.is_symlink() and (source == dest.resolve() or source in dest.resolve().parents):
                    raise ValueError('不能把目录放进自身或子目录')
        if action == 'upload' and (not isinstance(spec.get('size'), int) or not 0 <= spec['size'] <= 1024 ** 4):
            raise ValueError('无效的上传大小')
        spec['paths'] = paths
        job = {'id': uuid.uuid4().hex, 'scope': scope, 'action': action, 'spec': spec,
               'state': 'uploading' if action == 'upload' else 'queued', 'created': time.time(),
               'completed': [], 'errors': [], 'bytes': 0, 'items_done': 0,
               'items_total': len(paths) or 1, 'name': spec.get('name') or
                   (self.trash_item(scope, paths[0])[1]['name'] if action in {'restore', 'purge'} else Path(paths[0]).name if paths else ''),
               'snapshots': {p: snapshot(Path(p)) for p in paths} if action not in {'restore', 'purge'} else {}}
        if action == 'upload':
            job['total_bytes'] = spec['size']
            job['upload_name'] = spec['name']
            job['upload_modified'] = spec.get('modified', 0)
            (self.root / 'uploads' / job['id']).touch(exist_ok=False)
        self.save(job)
        if action != 'upload':
            self.launch(job)
        return self.public(job)

    def launch(self, job):
        with self.lock:
            if job['id'] in self.running:
                raise ValueError('任务仍在运行')
            self.running.add(job['id'])
            self.cancels.discard(job['id'])
        threading.Thread(target=self.run, args=(job,), daemon=True, name='file-task').start()

    def check(self, job, count=0):
        if job['id'] in self.cancels:
            raise Cancelled()
        job['bytes'] += count
        now = time.monotonic()
        if now - self.last_save.get(job['id'], 0) > .3:
            self.save(job)
            self.last_save[job['id']] = now

    def copy(self, source, dest, job):
        self.check(job)
        before = snapshot(source)
        if source.is_symlink():
            dest.symlink_to(os.readlink(source))
        elif source.is_dir():
            dest.mkdir()
            for item in source.iterdir():
                self.copy(item, dest / item.name, job)
            shutil.copystat(source, dest, follow_symlinks=False)
        elif source.is_file():
            # O_NOFOLLOW prevents a symlink swap from silently changing the source.
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as incoming, dest.open('xb') as outgoing:
                if not stat.S_ISREG(os.fstat(incoming.fileno()).st_mode):
                    raise ValueError('源文件类型已改变')
                while data := incoming.read(CHUNK):
                    self.check(job, len(data))
                    outgoing.write(data)
            shutil.copystat(source, dest, follow_symlinks=False)
        else:
            raise ValueError('目录中含有不支持的特殊文件')
        if snapshot(source) != before:
            raise ValueError('源文件在复制期间发生变化，未发布副本')

    def temporary(self, dest, job):
        temp = dest.parent / ('.agenthub-' + job['id'] + '-' + uuid.uuid4().hex)
        job['temp'] = str(temp)
        self.save(job)
        return temp

    def publish(self, temp, dest, job):
        self.check(job)
        for raw in job['spec'].get('paths', []):
            if dest in Path(raw).parents:
                raise ValueError('目标包含源项目，不能覆盖其上级目录')
        target = self.target(dest, job['spec'].get('conflict', 'error'), job['scope'], job)
        if target is None:
            return None
        # No application operation can race this section; refuse an OS-created file.
        if exists(target):
            raise Conflict('目标在操作期间发生变化')
        rename_noreplace(temp, target)
        return target

    def archive(self, paths, output, job):
        used = set()
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            def add(path, name):
                self.check(job)
                if path.is_symlink():
                    raise ValueError('ZIP 打包不包含符号链接，请单独复制链接或选择实际文件')
                if path.is_dir():
                    archive.writestr(name.rstrip('/') + '/', b'')
                    for child in path.iterdir():
                        add(child, name + '/' + child.name)
                elif path.is_file():
                    before = snapshot(path)
                    with path.open('rb') as src, archive.open(name, 'w', force_zip64=True) as dst:
                        while data := src.read(CHUNK):
                            self.check(job, len(data))
                            dst.write(data)
                    if snapshot(path) != before:
                        raise ValueError('源文件在打包时发生变化')
                else:
                    raise ValueError('ZIP 不支持此文件类型')
            for raw in paths:
                path = Path(raw)
                if path.name in used:
                    raise ValueError('所选项目同名，请分别打包')
                used.add(path.name)
                add(path, path.name)

    def extract(self, source, temp, job):
        temp.mkdir()
        with zipfile.ZipFile(source) as archive:
            items = archive.infolist()
            if len(items) > 100000 or sum(i.file_size for i in items) > shutil.disk_usage(temp).free:
                raise ValueError('解压项目过多或磁盘空间不足')
            seen = set()
            for info in items:
                parts = PurePosixPath(info.filename).parts
                mode = info.external_attr >> 16
                if not parts or info.filename.startswith('/') or '\\' in info.filename \
                        or any(p in {'.', '..'} or ':' in p for p in parts) \
                        or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('压缩包包含不安全路径或特殊文件')
                target = temp.joinpath(*parts)
                if str(target) in seen:
                    raise ValueError('压缩包包含重复路径')
                seen.add(str(target))
                self.check(job)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open('xb') as dst:
                        while data := src.read(CHUNK):
                            self.check(job, len(data))
                            dst.write(data)

    def run(self, job):
        with self.operations:
            try:
                self.check(job)
                job['state'] = 'running'
                self.save(job)
                spec, action = job['spec'], job['action']
                paths = spec['paths']
                work = ['@'] if action in {'mkdir', 'new-file', 'compress', 'bundle', 'upload'} else paths
                for raw in work:
                    if raw in job['completed']:
                        continue
                    self.check(job)
                    temp = None
                    try:
                        if raw in job['snapshots'] and snapshot(Path(raw)) != job['snapshots'][raw]:
                            raise ValueError('所选项目已改变，请重新选择')
                        source = Path(raw)
                        conflict = spec.get('conflict', 'error')
                        if action in {'mkdir', 'new-file'}:
                            dest = path_for(spec['destination']) / spec['name']
                            temp = self.temporary(dest, job)
                            temp.mkdir() if action == 'mkdir' else temp.touch(exist_ok=False)
                            self.publish(temp, dest, job)
                        elif action == 'upload':
                            incoming = self.root / 'uploads' / job['id']
                            if incoming.stat().st_size != job['total_bytes']:
                                raise ValueError('上传数据尚未完整到达')
                            dest = path_for(spec['destination']) / spec['name']
                            temp = self.temporary(dest, job)
                            job['bytes'] = 0
                            self.copy(incoming, temp, job)
                            self.publish(temp, dest, job)
                            incoming.unlink()
                        elif action in {'copy', 'move', 'rename'}:
                            dest = (source.parent / spec['name'] if action == 'rename'
                                    else path_for(spec['destination']) / source.name)
                            if source == dest and conflict != 'keep':
                                raise ValueError('源路径和目标相同')
                            if action == 'copy':
                                temp = self.temporary(dest, job)
                                self.copy(source, temp, job)
                                self.publish(temp, dest, job)
                            else:
                                if dest in source.parents:
                                    raise ValueError('不能覆盖源项目的上级目录')
                                target = self.target(dest, conflict, job['scope'], job)
                                if target:
                                    self.guard(source)
                                    try:
                                        rename_noreplace(source, target)
                                    except OSError as exc:
                                        if exc.errno != errno.EXDEV:
                                            raise
                                        temp = self.temporary(target, job)
                                        self.copy(source, temp, job)
                                        self.check(job)
                                        if exists(target):
                                            raise Conflict('目标已存在')
                                        rename_noreplace(temp, target)
                                        # Keep the original recoverable for cross-device moves.
                                        self.trash(source, job['scope'], job)
                        elif action == 'delete':
                            self.guard(source)
                            remove(source)
                        elif action == 'trash':
                            self.trash(source, job['scope'], job)
                        elif action in {'restore', 'purge'}:
                            folder, item = self.trash_item(job['scope'], raw)
                            if action == 'restore':
                                dest = path_for(item['path'], exists=False)
                                target = self.target(dest, conflict, job['scope'], job)
                                if target:
                                    try:
                                        rename_noreplace(folder / 'data', target)
                                    except OSError as exc:
                                        if exc.errno != errno.EXDEV:
                                            raise
                                        temp = self.temporary(target, job)
                                        self.copy(folder / 'data', temp, job)
                                        self.check(job)
                                        rename_noreplace(temp, target)
                                        remove(folder / 'data')
                            else:
                                remove(folder / 'data')
                        elif action in {'compress', 'bundle', 'extract'}:
                            if action == 'bundle':
                                dest = self.root / 'artifacts' / (job['id'] + '.zip')
                            elif action == 'extract':
                                dest = path_for(spec['destination']) / name_valid(source.stem)
                            else:
                                dest = path_for(spec['destination']) / spec['name']
                            temp = self.temporary(dest, job)
                            if action == 'extract':
                                self.extract(source, temp, job)
                            else:
                                self.archive(paths, temp, job)
                            if action == 'bundle':
                                self.check(job)
                                os.rename(temp, dest)
                                job['artifact'] = True
                                job['download_name'] = (Path(paths[0]).stem if len(paths) == 1 else 'files') + '.zip'
                            else:
                                self.publish(temp, dest, job)
                        job['completed'].append(raw)
                        job['items_done'] = job['items_total'] if action in {'compress', 'bundle'} else len(job['completed'])
                    except Cancelled:
                        raise
                    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as exc:
                        job['errors'].append({'path': raw, 'error': str(exc)})
                    finally:
                        if temp and exists(temp):
                            remove(temp)
                        job.pop('temp', None)
                        self.save(job)
                job['state'] = 'failed' if job['errors'] else 'completed'
            except Cancelled:
                job['state'] = 'cancelled'
            except Exception as exc:
                job.update(state='failed', error=str(exc))
            finally:
                self.save(job)
                with self.lock:
                    self.running.discard(job['id'])
                    self.last_save.pop(job['id'], None)

    def control(self, scope, ident, action, conflict=None):
        with self.lock:
            job = self.get(scope, ident)
            if action == 'cancel':
                self.cancels.add(ident)
                if job['state'] == 'uploading':
                    job['state'] = 'cancelled'
                    self.save(job)
                return self.public(job)
            if action != 'retry' or job['state'] not in {'failed', 'cancelled', 'interrupted'}:
                raise ValueError('此任务不能重试')
            if ident in self.running:
                raise ValueError('任务正在结束，请稍后重试')
            if job.get('temp'):
                stage = Path(job['temp'])
                if stage.name.startswith('.agenthub-' + ident + '-') and exists(stage):
                    remove(stage)
                job.pop('temp', None)
            if conflict is not None:
                if conflict not in {'error', 'skip', 'keep', 'replace'}:
                    raise ValueError('无效的重名处理方式')
                job['spec']['conflict'] = conflict
            self.cancels.discard(ident)
            job.update(errors=[], error='', state='uploading' if job['action'] == 'upload' else 'queued')
            if job['action'] == 'upload':
                job['bytes'] = (self.root / 'uploads' / ident).stat().st_size
            self.save(job)
            if job['action'] != 'upload':
                self.launch(job)
            return self.public(job)

    def upload(self, scope, ident, offset, data):
        with self.operations, self.lock:
            job = self.get(scope, ident)
            if job['state'] != 'uploading':
                raise ValueError('上传已结束或已取消')
            temp = self.root / 'uploads' / ident
            actual = temp.stat().st_size
            if offset != actual or actual + len(data) > job['total_bytes']:
                raise ValueError('上传位置不一致，请刷新任务后重试')
            with temp.open('ab') as stream:
                stream.write(data)
            job['bytes'] = actual + len(data)
            if job['bytes'] == job['total_bytes']:
                job['state'] = 'queued'
            self.save(job)
            if job['state'] == 'queued':
                self.launch(job)
            return self.public(job)

    def artifact(self, scope, ident):
        job = self.get(scope, ident)
        if job['state'] != 'completed' or not job.get('artifact'):
            raise FileNotFoundError('打包文件尚未完成')
        return self.root / 'artifacts' / (ident + '.zip'), job['download_name']


_manager = None
_manager_lock = threading.Lock()


def manager():
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = Manager(Path.home() / '.local/share/agenthub/file-manager')
        return _manager
