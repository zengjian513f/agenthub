"""Isolated HTTP/SSE/WebSocket nodes; never starts a CLI or accesses real sessions."""
import base64
import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from agenthub import server, wsock

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j0i8AAAAASUVORK5CYII=')


class NodeHandler(server.Handler):
    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        s = self.state
        if self.headers.get('X-AgentHub-Protocol') and self.headers.get('X-AgentHub-Node-Token') != s['token']:
            return self._json({'error': 'forbidden'}, 403)
        if s.get('offline') and u.path.startswith('/api/'):
            return self._json({'error': 'offline'}, 503)
        s['gets'].append((u.path, q))
        if u.path == '/api/meta':
            return self._json({'mode': 'local', 'protocol': 1, 'node_id': s['id'],
                               'build': server.ASSET_VERSION, 'hostname': s['name']})
        if u.path == '/api/nodes':
            return self._json({'mode': 'local', 'nodes': []})
        if u.path == '/api/sessions':
            return self._json({'sessions': [s['row']] if not s.get('deleted') else [], 'sig': 'fixture', 'built_at': 0})
        if u.path == '/api/live':
            return self._json({'uids': [], 'tmux_uids': [], 'started_at': {}})
        if u.path == '/api/search':
            return self._json({'results': [{**s['row'], 'hits': 1, 'snippet': s['name'] + ' needle'}],
                               'total_pool': 1, 'truncated': False})
        if u.path == '/api/term/list':
            return self._json({'enabled': True, 'sources': {'claude': True, 'codex': True},
                               'home': '/home/' + s['name'], 'sessions': [], 'pending': s['pending']})
        if u.path == '/api/term/complete-dir':
            return self._json({'directories': ['/home/' + s['name'] + '/work/']})
        if u.path == '/api/term/new-status':
            return self._json({'waiting': True, 'running': True})
        if u.path == '/api/session/outbox':
            return self._json({'outbox': [], 'outbox_version': {'epoch': 'same-epoch', 'revision': 0}})
        if u.path == '/api/session/input-history':
            return self._json({'history': []})
        if u.path.startswith('/api/messages/'):
            if unquote(u.path.rsplit('/', 1)[1]) != s['row']['uid']:
                return self._json({'error': 'wrong UID'}, 404)
            return self._json(self.messages(q))
        if u.path == '/api/watch':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                last = -1
                while not s.get('stopped'):
                    if s.get('pause_stream'):
                        time.sleep(.1)
                        continue
                    if last != len(s['messages']):
                        last = len(s['messages'])
                        data = self.messages(q)
                        self.wfile.write(b'data: ' + json.dumps(data).encode() + b'\n\n')
                        q['start'] = [str(last)]
                    else:
                        self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    time.sleep(.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if u.path.startswith('/api/media/'):
            return self._send(200, PNG, 'image/png')
        if u.path == '/api/term/attach':
            if wsock.handshake(self):
                self.close_connection = True
                wsock.send(self.connection, s['name'].encode(), wsock.OP_BIN)
                try:
                    while True:
                        op, payload = wsock.recv(self.connection)
                        if op == wsock.OP_CLOSE:
                            break
                        s['frames'].append(payload)
                        wsock.send(self.connection, payload, op)
                except (OSError, ConnectionError):
                    pass
            return
        if u.path == '/api/trash':
            return self._json({'items': [{'id': 'claude/same-trash', 'uid': s['row']['uid'], 'source': 'claude',
                 'title': s['name'] + ' deleted', 'cwd': '/same/project', 'origin': '/same/file',
                 'deleted_at': '2026-09-01T00:00:00Z', 'size': 10, 'restorable': True}], 'size': 10})
        if u.path.startswith('/api/'):
            return self._json({'error': 'not found'}, 404)
        return self._static(u.path)

    def messages(self, q):
        start = int(q.get('start', ['0'])[0])
        s = self.state
        return {'meta': s['row'], 'version': {'head': 'fixed', 'size': len(s['messages']), 'mtime': 1},
                'anchor': 'anchor', 'reset': start == 0, 'start': start, 'end': len(s['messages']),
                'messages': s['messages'][start:], 'message_total': len(s['messages']),
                'partial': None, 'activity': None, 'activity_changed': False}

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.headers.get('X-AgentHub-Protocol') and self.headers.get('X-AgentHub-Node-Token') != self.state['token']:
            return self._json({'error': 'forbidden'}, 403)
        if u.path == '/api/session/attachment':
            self.state['uploads'].append((q, raw))
            return self._json({'ok': True, 'name': q['name'][0], 'size': len(raw), 'attachment_id': '1',
                               'media': {'src': '/api/media/dddddddddddddddddddddddddddddddd'}})
        body = json.loads(raw or b'{}')
        self.state['writes'].append((u.path, body))
        if u.path == '/api/term/create':
            info = {'name': 'same-terminal', 'source': body['source'], 'sid': 'new-sid',
                    'cwd': body['cwd'], 'token': 'fixture-lease', 'started': time.time()}
            self.state['pending'].append(info)
            return self._json(info)
        if u.path == '/api/term/claim':
            return self._json({'ok': True, 'token': 'fixture-lease'})
        if u.path == '/api/sessions/delete':
            self.state['deleted'] = True
            return self._json({'ok': True, 'deleted': [{'uid': uid} for uid in body['uids']], 'errors': []})
        if u.path == '/api/session/star':
            self.state['row']['starred'] = body['starred']
        return self._json({'ok': True, 'uid': body.get('uid', ''), 'path': '/same/file', 'removed': 1, 'freed': 10})


def start_node(nid, name):
    srv = ThreadingHTTPServer(('127.0.0.1', 0), NodeHandler)
    srv.daemon_threads = True
    srv.hub_mode = False
    row = {'uid': 'claude:same-file-hash', 'sid': 'same-native-id', 'source': 'claude',
           'title': name + ' session', 'cwd': '/same/project', 'size': 20,
           'created': '2026-09-01T00:00:00Z', 'updated': '2026-09-07T00:00:00Z', 'agents': 0}
    srv.state = {'id': nid, 'name': name, 'token': name * 32, 'row': row,
                 'messages': [{'role': 'user', 'text': name + ' needle', 'ts': row['created']},
                              {'role': 'assistant', 'text': 'reply ' + name, 'ts': row['updated'],
                               'media': [{'src': '/api/media/dddddddddddddddddddddddddddddddd', 'mime': 'image/png'}]}],
                 'pending': [], 'writes': [], 'gets': [], 'uploads': [], 'frames': []}
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def stop(srv):
    if hasattr(srv, 'state'):
        srv.state['stopped'] = True
    srv.shutdown()
    srv.server_close()
