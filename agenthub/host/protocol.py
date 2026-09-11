"""宿主进程与客户端之间的本地协议（客户端侧）。

宿主本体是 Rust 实现（见 host-rs/），这里只保留 Web 服务作为客户端所需的编解码。

控制请求: 一行 JSON 请求, 一行 JSON 应答, 一次连接一条请求。
attach 请求应答后连接进入帧模式: 1 字节类型 + 4 字节大端长度 + 载荷。
"""

from __future__ import annotations

import json
import socket
import struct

FRAME_DATA = 1
FRAME_RESIZE = 2
FRAME_EXIT = 3
MAX_LINE = 4 * 1024 * 1024


class ProtocolError(RuntimeError):
    pass


def send_json(sock: socket.socket, obj: dict) -> None:
    sock.sendall(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")


def recv_json(sock: socket.socket, buffer: bytearray) -> dict:
    """读到一行 JSON 为止; buffer 保留多读的字节 (帧模式紧随其后)。"""
    while b"\n" not in buffer:
        chunk = sock.recv(65536)
        if not chunk:
            raise ProtocolError("连接已关闭")
        buffer.extend(chunk)
        if len(buffer) > MAX_LINE:
            raise ProtocolError("请求过大")
    line, _, rest = bytes(buffer).partition(b"\n")
    buffer[:] = rest
    try:
        obj = json.loads(line.decode("utf-8"))
    except ValueError as e:
        raise ProtocolError(f"非法 JSON: {e}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("请求必须是对象")
    return obj


def pack_frame(kind: int, payload: bytes) -> bytes:
    return struct.pack("!BI", kind, len(payload)) + payload


def read_frames(buffer: bytearray):
    """从 buffer 中尽量多地切出完整帧, 返回 [(kind, payload)] 并保留残余。"""
    frames = []
    pos = 0
    n = len(buffer)
    while n - pos >= 5:
        kind, length = struct.unpack_from("!BI", buffer, pos)
        if n - pos - 5 < length:
            break
        frames.append((kind, bytes(buffer[pos + 5:pos + 5 + length])))
        pos += 5 + length
    if pos:
        del buffer[:pos]
    return frames
