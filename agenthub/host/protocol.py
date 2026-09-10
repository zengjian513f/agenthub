"""宿主进程与客户端之间的本地协议。

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


# tmux 风格键名 → 字节序列。方向键在 DECCKM 模式下改用 SS3 前缀。
_NAMED = {
    "Enter": "\r", "Escape": "\x1b", "Tab": "\t", "BTab": "\x1b[Z",
    "BSpace": "\x7f", "Space": " ", "DC": "\x1b[3~", "IC": "\x1b[2~",
    "Home": "\x1b[H", "End": "\x1b[F", "PPage": "\x1b[5~", "NPage": "\x1b[6~",
    "F1": "\x1bOP", "F2": "\x1bOQ", "F3": "\x1bOR", "F4": "\x1bOS",
    "F5": "\x1b[15~", "F6": "\x1b[17~", "F7": "\x1b[18~", "F8": "\x1b[19~",
    "F9": "\x1b[20~", "F10": "\x1b[21~", "F11": "\x1b[23~", "F12": "\x1b[24~",
}
_ARROWS = {"Up": "A", "Down": "B", "Right": "C", "Left": "D"}
_CTRL_SPECIAL = {"@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e",
                 "_": "\x1f", "?": "\x7f", "Space": "\x00"}


def key_bytes(key: str, app_cursor: bool = False) -> bytes:
    """未知键名按字面文本发送, 与 tmux send-keys 的宽松行为一致。"""
    if key in _ARROWS:
        prefix = "\x1bO" if app_cursor else "\x1b["
        return (prefix + _ARROWS[key]).encode()
    if key in _NAMED:
        return _NAMED[key].encode()
    if key.startswith(("C-", "^")) and len(key) > 1:
        rest = key[2:] if key.startswith("C-") else key[1:]
        if rest in _CTRL_SPECIAL:
            return _CTRL_SPECIAL[rest].encode()
        if len(rest) == 1 and rest.isalpha():
            return bytes([ord(rest.lower()) & 0x1F])
    if key.startswith("M-") and len(key) > 2:
        return b"\x1b" + key_bytes(key[2:], app_cursor)
    if key.startswith("S-") and key[2:] in _ARROWS:
        return f"\x1b[1;2{_ARROWS[key[2:]]}".encode()
    return key.encode("utf-8", "replace")
