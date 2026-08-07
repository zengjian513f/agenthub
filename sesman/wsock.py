"""RFC 6455 WebSocket 的最小服务端实现 (只用标准库)。

只覆盖服务端需要的部分: 握手、文本/二进制帧的收发、ping/pong、close。
不做扩展协商 (permessage-deflate 一律拒绝)。
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def handshake(handler) -> bool:
    """把一个 BaseHTTPRequestHandler 的连接升级成 WebSocket。"""
    if handler.headers.get("Upgrade", "").lower() != "websocket":
        return False
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        return False
    handler.send_response(101)
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    return True


def send(sock, payload: bytes, opcode: int = OP_TEXT) -> None:
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < (1 << 16):
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    sock.sendall(bytes(head) + payload)


def _read_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def recv(sock) -> tuple[int, bytes]:
    """读一帧, 返回 (opcode, payload)。分片帧会被拼起来。"""
    frag, first_op = b"", None
    while True:
        b0, b1 = _read_exact(sock, 2)
        fin = b0 & 0x80
        opcode = b0 & 0x0F
        masked = b1 & 0x80
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", _read_exact(sock, 2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", _read_exact(sock, 8))[0]
        if ln > 8 << 20:                       # 客户端不该发这么大的帧
            raise ConnectionError("frame too large")
        mask = _read_exact(sock, 4) if masked else b""
        data = _read_exact(sock, ln) if ln else b""
        if masked:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        if opcode == OP_CONT:
            frag += data
        else:
            first_op, frag = opcode, data
        if fin:
            return first_op or OP_TEXT, frag


def close(sock, code: int = 1000) -> None:
    try:
        send(sock, struct.pack(">H", code), OP_CLOSE)
    except OSError:
        pass
