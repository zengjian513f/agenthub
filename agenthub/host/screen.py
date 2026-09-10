"""托管会话的最小 VT100/xterm 屏幕模型。

tmux 之前免费提供了三样东西：可见屏文本、光标位置、滚出屏幕的历史行。
自制宿主把 CLI 的原始字节直接转给浏览器，这个模型只用来回答
capture/cursor 一类查询和 attach 时的历史回放，不参与实时渲染。

只实现 Claude/Codex/Grok 这类 TUI 真正会用到的子集：光标移动、擦除、
插入/删除行与字符、滚动区域、SGR 属性、备用屏、保存/恢复光标、自动换行，
以及东亚宽字符和组合字符的列宽。OSC/DCS 字符串整体跳过。
"""

from __future__ import annotations

import codecs
import unicodedata
from collections import deque

# 单元格 = (字符, 属性串)。属性串是规范化的 SGR 参数，"" 表示默认渲染；
# 宽字符占两格，第二格字符为 "" 作为占位。
Cell = tuple[str, str]
BLANK: Cell = (" ", "")

_SIMPLE_ON = {1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 7: "7", 8: "8", 9: "9"}
_SIMPLE_OFF = {22: ("1", "2"), 23: ("3",), 24: ("4",), 25: ("5",),
               27: ("7",), 28: ("8",), 29: ("9",)}
_ORDER = ("1", "2", "3", "4", "5", "7", "8", "9")

DEFAULT_HISTORY = 10000


def char_width(ch: str) -> int:
    """0 = 组合/零宽, 2 = 东亚全角/宽, 其余 1。"""
    if not ch:
        return 0
    o = ord(ch)
    if o < 0x20 or o == 0x7F:
        return 0
    if o < 0x300:
        return 1
    if unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf") or o == 0x200D or 0xFE00 <= o <= 0xFE0F:
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


class _Attr:
    """当前 SGR 状态，序列化成规范参数串以便比较和重放。"""

    __slots__ = ("flags", "fg", "bg", "_cache")

    def __init__(self) -> None:
        self.flags: set[str] = set()
        self.fg = ""
        self.bg = ""
        self._cache: str | None = ""

    def reset(self) -> None:
        self.flags.clear()
        self.fg = self.bg = ""
        self._cache = ""

    def text(self) -> str:
        if self._cache is None:
            parts = [flag for flag in _ORDER if flag in self.flags]
            if self.fg:
                parts.append(self.fg)
            if self.bg:
                parts.append(self.bg)
            self._cache = ";".join(parts)
        return self._cache

    def bg_only(self) -> str:
        return self.bg

    def apply(self, params: list[int | list[int]]) -> None:
        i = 0
        n = len(params)
        while i < n:
            item = params[i]
            if isinstance(item, list):            # 冒号子参数, 如 38:2::r:g:b / 4:3
                code, sub = item[0], item[1:]
                if code in (38, 48, 58):
                    self._extended(code, sub)
                elif code == 4:
                    if sub and sub[0] == 0:
                        self.flags.discard("4")
                    else:
                        self.flags.add("4")
                else:
                    self.apply([code])
                i += 1
                continue
            code = item
            if code == 0:
                self.flags.clear()
                self.fg = self.bg = ""
            elif code in _SIMPLE_ON:
                self.flags.add(_SIMPLE_ON[code])
            elif code == 21:
                self.flags.add("4")
            elif code in _SIMPLE_OFF:
                for flag in _SIMPLE_OFF[code]:
                    self.flags.discard(flag)
            elif 30 <= code <= 37 or 90 <= code <= 97:
                self.fg = str(code)
            elif code == 39:
                self.fg = ""
            elif 40 <= code <= 47 or 100 <= code <= 107:
                self.bg = str(code)
            elif code == 49:
                self.bg = ""
            elif code in (38, 48, 58):
                rest = [p for p in params[i + 1:] if isinstance(p, int)]
                used = self._extended(code, rest)
                i += used
            i += 1
        self._cache = None

    def _extended(self, code: int, sub: list[int]) -> int:
        """处理 38/48 的 5;n 与 2;r;g;b 形式, 返回消耗的参数个数。"""
        if not sub:
            return 0
        if sub[0] == 5 and len(sub) >= 2:
            value = f"{code};5;{sub[1]}"
            used = 2
        elif sub[0] == 2 and len(sub) >= 4:
            r, g, b = sub[1], sub[2], sub[3]
            value = f"{code};2;{r};{g};{b}"
            used = 4
        else:
            return len(sub)
        if code == 38:
            self.fg = value
        elif code == 48:
            self.bg = value
        return used


class Screen:
    """字符网格 + 光标 + 有界历史。feed() 吃原始字节，其余方法只读。"""

    def __init__(self, cols: int = 120, rows: int = 32,
                 history_limit: int = DEFAULT_HISTORY):
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.history_limit = history_limit
        self.history: deque[tuple[str, bool]] = deque(maxlen=history_limit)
        self.lines: list[list[Cell]] = [self._blank_row() for _ in range(self.rows)]
        self.wrapped: list[bool] = [False] * self.rows
        self.alt = False
        self._main_saved: tuple[list[list[Cell]], list[bool]] | None = None
        self.x = 0
        self.y = 0
        self.attr = _Attr()
        self.saved: dict[bool, tuple[int, int, str, str, set[str]]] = {}
        self.top = 0
        self.bottom = self.rows - 1
        self.autowrap = True
        self.origin = False
        self.pending_wrap = False
        self.cursor_visible = True
        self.app_cursor = False            # DECCKM: 方向键发 ESC O A 而非 ESC [ A
        self.bracketed_paste = False       # 应用是否请求了 bracketed paste
        self.responses: list[bytes] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._state = "ground"
        self._params = ""
        self._inter = ""
        self._str_esc = False
        self.dirty = 0                     # feed 计数, 供调用方判断是否有变化

    # ------------------------------------------------------------------ 输入
    def feed(self, data: bytes | str) -> None:
        text = self._decoder.decode(data) if isinstance(data, bytes) else data
        for ch in text:
            self._handle(ch)
        if text:
            self.dirty += 1

    def _handle(self, ch: str) -> None:
        state = self._state
        if state == "ground":
            if ch == "\x1b":
                self._state = "esc"
            elif ch < " " or ch == "\x7f":
                self._control(ch)
            else:
                self._put(ch)
        elif state == "esc":
            self._escape(ch)
        elif state == "csi":
            if "0" <= ch <= "?":
                self._params += ch
            elif " " <= ch <= "/":
                self._inter += ch
            elif "@" <= ch <= "~":
                self._state = "ground"
                self._csi(ch)
            elif ch == "\x1b":
                self._state = "esc"
            elif ch < " ":
                self._control(ch)
            else:
                self._state = "ground"
        elif state == "esc_inter":
            self._state = "ground"           # ESC ( B 之类的字符集指定, 忽略
        elif state == "str":
            if self._str_esc:
                self._str_esc = False
                if ch == "\\":
                    self._state = "ground"
                    return
                self._state = "esc"
                self._escape(ch)
            elif ch == "\x1b":
                self._str_esc = True
            elif ch == "\x07":
                self._state = "ground"

    def _control(self, ch: str) -> None:
        if ch == "\r":
            self.x = 0
            self.pending_wrap = False
        elif ch in "\n\x0b\x0c":
            self._linefeed()
        elif ch == "\b":
            if self.pending_wrap:
                self.pending_wrap = False
            elif self.x > 0:
                self.x -= 1
        elif ch == "\t":
            self.pending_wrap = False
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)

    def _escape(self, ch: str) -> None:
        self._state = "ground"
        if ch == "[":
            self._state = "csi"
            self._params = ""
            self._inter = ""
        elif ch in "]PX^_":
            self._state = "str"
            self._str_esc = False
        elif ch == "7":
            self._save_cursor()
        elif ch == "8":
            self._restore_cursor()
        elif ch == "D":
            self._linefeed()
        elif ch == "E":
            self.x = 0
            self._linefeed()
        elif ch == "M":
            self._reverse_index()
        elif ch == "c":
            self._reset()
        elif " " <= ch <= "/":
            self._state = "esc_inter"
        # '=', '>', 其他单字符序列直接忽略

    # ------------------------------------------------------------ 基本操作
    def _blank_row(self, attr: str = "") -> list[Cell]:
        return [(" ", attr)] * self.cols

    def _blank(self) -> Cell:
        return (" ", self.attr.bg_only())

    def _put(self, ch: str) -> None:
        width = char_width(ch)
        if width == 0:
            # 组合字符附着到前一个单元格；行首没有宿主则丢弃
            col = self.x - 1 if not self.pending_wrap else self.x
            if col < 0:
                return
            if col > 0 and self.lines[self.y][col][0] == "":
                col -= 1
            prev = self.lines[self.y][col]
            self.lines[self.y][col] = (prev[0] + ch, prev[1])
            return
        if self.pending_wrap:
            if self.autowrap:
                self.wrapped[self.y] = True
                self.x = 0
                self._linefeed()
            self.pending_wrap = False
        if width == 2 and self.x == self.cols - 1:
            if self.autowrap:
                self.lines[self.y][self.x] = self._blank()
                self.wrapped[self.y] = True
                self.x = 0
                self._linefeed()
            else:
                self.x = max(0, self.cols - 2)
        row = self.lines[self.y]
        attr = self.attr.text()
        # 覆盖宽字符的一半时清掉另一半
        if row[self.x][0] == "" and self.x > 0:
            row[self.x - 1] = self._blank()
        end = self.x + width
        if end < self.cols and row[end][0] == "" and width == 1:
            row[end] = self._blank()
        row[self.x] = (ch, attr)
        if width == 2:
            row[self.x + 1] = ("", attr)
        self.x += width
        if self.x >= self.cols:
            self.x = self.cols - 1
            self.pending_wrap = True

    def _linefeed(self) -> None:
        if self.y == self.bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self) -> None:
        self.pending_wrap = False
        if self.y == self.top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    def _push_history(self, row: list[Cell], wrapped: bool) -> None:
        if self.alt or self.history_limit <= 0:
            return
        self.history.append((render_row(row, styled=True, keep_trailing=wrapped), wrapped))

    def _scroll_up(self, n: int) -> None:
        n = max(1, min(n, self.bottom - self.top + 1))
        for _ in range(n):
            if self.top == 0:
                self._push_history(self.lines[0], self.wrapped[0])
            del self.lines[self.top]
            del self.wrapped[self.top]
            self.lines.insert(self.bottom, self._blank_row(self.attr.bg_only()))
            self.wrapped.insert(self.bottom, False)

    def _scroll_down(self, n: int) -> None:
        n = max(1, min(n, self.bottom - self.top + 1))
        for _ in range(n):
            del self.lines[self.bottom]
            del self.wrapped[self.bottom]
            self.lines.insert(self.top, self._blank_row(self.attr.bg_only()))
            self.wrapped.insert(self.top, False)

    def _save_cursor(self) -> None:
        self.saved[self.alt] = (self.x, self.y, self.attr.fg, self.attr.bg,
                                set(self.attr.flags))

    def _restore_cursor(self) -> None:
        saved = self.saved.get(self.alt)
        self.pending_wrap = False
        if not saved:
            self.x = self.y = 0
            return
        self.x, self.y, fg, bg, flags = saved
        self.x = min(self.x, self.cols - 1)
        self.y = min(self.y, self.rows - 1)
        self.attr.fg, self.attr.bg, self.attr.flags = fg, bg, set(flags)
        self.attr._cache = None

    def _reset(self) -> None:
        self.attr.reset()
        self.top, self.bottom = 0, self.rows - 1
        self.autowrap, self.origin = True, False
        self.pending_wrap = False
        self.cursor_visible = True
        if self.alt:
            self._leave_alt()
        self.lines = [self._blank_row() for _ in range(self.rows)]
        self.wrapped = [False] * self.rows
        self.x = self.y = 0

    def _enter_alt(self) -> None:
        if self.alt:
            return
        self._main_saved = (self.lines, self.wrapped)
        self.lines = [self._blank_row() for _ in range(self.rows)]
        self.wrapped = [False] * self.rows
        self.alt = True

    def _leave_alt(self) -> None:
        if not self.alt:
            return
        self.alt = False
        if self._main_saved:
            self.lines, self.wrapped = self._main_saved
            self._main_saved = None
        self._fit_rows(self.lines, self.wrapped)

    # ----------------------------------------------------------------- CSI
    def _parse_params(self) -> tuple[list, bool]:
        raw = self._params
        private = bool(raw) and raw[0] in "?<=>"
        if private:
            raw = raw[1:]
        params: list = []
        for chunk in raw.split(";") if raw else []:
            if ":" in chunk:
                subs = [int(s) if s.isdigit() else 0 for s in chunk.split(":")]
                params.append(subs)
            else:
                params.append(int(chunk) if chunk.isdigit() else 0)
        return params, private

    def _csi(self, final: str) -> None:
        params, private = self._parse_params()
        inter = self._inter

        def arg(i: int, default: int = 1) -> int:
            if i < len(params) and isinstance(params[i], int) and params[i] > 0:
                return params[i]
            return default

        if inter:
            # CSI ? 或带中间字节的序列 (如 光标形状 " q") 全部忽略
            return
        if private:
            if final == "h":
                self._set_modes(params, True)
            elif final == "l":
                self._set_modes(params, False)
            return
        if final == "m":
            self.attr.apply(params or [0])
        elif final in "ABCDEFGHfd":
            self._move(final, params, arg)
        elif final == "J":
            self._erase_display(arg(0, 0))
        elif final == "K":
            self._erase_line(arg(0, 0))
        elif final == "L":
            self._insert_lines(arg(0))
        elif final == "M":
            self._delete_lines(arg(0))
        elif final == "@":
            self._insert_chars(arg(0))
        elif final == "P":
            self._delete_chars(arg(0))
        elif final == "X":
            self._erase_chars(arg(0))
        elif final == "S":
            self._scroll_up(arg(0))
        elif final == "T":
            self._scroll_down(arg(0))
        elif final == "r":
            top = arg(0, 1) - 1
            bottom = arg(1, self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
            else:
                self.top, self.bottom = 0, self.rows - 1
            self.x = self.y = 0
            self.pending_wrap = False
        elif final == "s":
            self._save_cursor()
        elif final == "u":
            self._restore_cursor()
        elif final == "n":
            if arg(0, 0) == 6:
                self.responses.append(f"\x1b[{self.y + 1};{self.x + 1}R".encode())
            elif arg(0, 0) == 5:
                self.responses.append(b"\x1b[0n")
        elif final == "c":
            self.responses.append(b"\x1b[?1;2c")
        elif final == "t":
            if arg(0, 0) == 18:
                self.responses.append(f"\x1b[8;{self.rows};{self.cols}t".encode())
        # 其他 (g, q, b, ...) 忽略

    def _set_modes(self, params: list, on: bool) -> None:
        for mode in params:
            if not isinstance(mode, int):
                continue
            if mode in (1049, 1047, 47):
                if on:
                    if mode == 1049:
                        self._save_cursor()
                    self._enter_alt()
                    if mode == 1049:
                        self.x = self.y = 0
                else:
                    self._leave_alt()
                    if mode == 1049:
                        self._restore_cursor()
            elif mode == 25:
                self.cursor_visible = on
            elif mode == 1:
                self.app_cursor = on
            elif mode == 2004:
                self.bracketed_paste = on
            elif mode == 7:
                self.autowrap = on
                self.pending_wrap = False
            elif mode == 6:
                self.origin = on
                self.x = 0
                self.y = self.top if on else 0

    def _move(self, final: str, params: list, arg) -> None:
        self.pending_wrap = False
        if final == "A":
            limit = self.top if self.y >= self.top else 0
            self.y = max(limit, self.y - arg(0))
        elif final == "B":
            limit = self.bottom if self.y <= self.bottom else self.rows - 1
            self.y = min(limit, self.y + arg(0))
        elif final == "C":
            self.x = min(self.cols - 1, self.x + arg(0))
        elif final == "D":
            self.x = max(0, self.x - arg(0))
        elif final == "E":
            self.x = 0
            self.y = min(self.rows - 1, self.y + arg(0))
        elif final == "F":
            self.x = 0
            self.y = max(0, self.y - arg(0))
        elif final == "G":
            self.x = min(self.cols - 1, arg(0) - 1)
        elif final in "Hf":
            row = arg(0) - 1
            col = arg(1) - 1
            if self.origin:
                row += self.top
                row = min(self.bottom, row)
            self.y = max(0, min(self.rows - 1, row))
            self.x = max(0, min(self.cols - 1, col))
        elif final == "d":
            row = arg(0) - 1
            if self.origin:
                row += self.top
            self.y = max(0, min(self.rows - 1, row))

    def _erase_display(self, mode: int) -> None:
        blank = self._blank()
        if mode == 0:
            self._erase_line(0)
            for i in range(self.y + 1, self.rows):
                self.lines[i] = [blank] * self.cols
                self.wrapped[i] = False
        elif mode == 1:
            self._erase_line(1)
            for i in range(0, self.y):
                self.lines[i] = [blank] * self.cols
                self.wrapped[i] = False
        elif mode in (2, 3):
            for i in range(self.rows):
                self.lines[i] = [blank] * self.cols
                self.wrapped[i] = False
            if mode == 3:
                self.history.clear()
        self.pending_wrap = False

    def _erase_line(self, mode: int) -> None:
        row = self.lines[self.y]
        blank = self._blank()
        if mode == 0:
            start, end = self.x, self.cols
            self.wrapped[self.y] = False
        elif mode == 1:
            start, end = 0, min(self.cols, self.x + 1)
        else:
            start, end = 0, self.cols
            self.wrapped[self.y] = False
        if start > 0 and row[start][0] == "":
            row[start - 1] = blank
        for i in range(start, end):
            row[i] = blank
        if end < self.cols and row[end][0] == "":
            row[end] = blank
        self.pending_wrap = False

    def _insert_lines(self, n: int) -> None:
        if not self.top <= self.y <= self.bottom:
            return
        n = min(n, self.bottom - self.y + 1)
        for _ in range(n):
            del self.lines[self.bottom]
            del self.wrapped[self.bottom]
            self.lines.insert(self.y, self._blank_row(self.attr.bg_only()))
            self.wrapped.insert(self.y, False)
        self.x = 0
        self.pending_wrap = False

    def _delete_lines(self, n: int) -> None:
        if not self.top <= self.y <= self.bottom:
            return
        n = min(n, self.bottom - self.y + 1)
        for _ in range(n):
            del self.lines[self.y]
            del self.wrapped[self.y]
            self.lines.insert(self.bottom, self._blank_row(self.attr.bg_only()))
            self.wrapped.insert(self.bottom, False)
        self.x = 0
        self.pending_wrap = False

    def _insert_chars(self, n: int) -> None:
        row = self.lines[self.y]
        n = min(n, self.cols - self.x)
        blank = self._blank()
        self.lines[self.y] = row[:self.x] + [blank] * n + row[self.x:self.cols - n]
        self.pending_wrap = False

    def _delete_chars(self, n: int) -> None:
        row = self.lines[self.y]
        n = min(n, self.cols - self.x)
        blank = self._blank()
        self.lines[self.y] = row[:self.x] + row[self.x + n:] + [blank] * n
        self.pending_wrap = False

    def _erase_chars(self, n: int) -> None:
        row = self.lines[self.y]
        blank = self._blank()
        for i in range(self.x, min(self.cols, self.x + n)):
            row[i] = blank
        self.pending_wrap = False

    # -------------------------------------------------------------- 尺寸
    def _fit_rows(self, lines: list[list[Cell]], wrapped: list[bool]) -> None:
        while len(lines) < self.rows:
            lines.append(self._blank_row())
            wrapped.append(False)
        del lines[self.rows:]
        del wrapped[self.rows:]
        for i, row in enumerate(lines):
            if len(row) < self.cols:
                lines[i] = row + [BLANK] * (self.cols - len(row))
            elif len(row) > self.cols:
                lines[i] = row[:self.cols]
                if lines[i] and lines[i][-1][0] == "":
                    lines[i][-1] = BLANK

    def resize(self, cols: int, rows: int) -> None:
        cols = max(1, int(cols))
        rows = max(1, int(rows))
        if (cols, rows) == (self.cols, self.rows):
            return
        old_rows = self.rows
        # 主屏: 缩小时把顶部多余行推进历史 (光标之下的空行优先裁掉),
        # 放大时从历史拉回, 与 tmux/xterm 的行为一致。
        if not self.alt:
            main, main_wrapped = self.lines, self.wrapped
        else:
            main, main_wrapped = self._main_saved or ([], [])
        if rows < old_rows:
            drop = 0
            trailing = 0
            for row in reversed(main):
                if any(c[0] not in (" ", "") or c[1] for c in row):
                    break
                trailing += 1
            cursor_y = self.y if not self.alt else (self.saved.get(False, (0, 0))[1])
            keep_bottom_blank = max(0, len(main) - 1 - cursor_y)
            trailing = min(trailing, keep_bottom_blank)
            excess = old_rows - rows
            cut_bottom = min(trailing, excess)
            del main[len(main) - cut_bottom:]
            del main_wrapped[len(main_wrapped) - cut_bottom:]
            drop = old_rows - cut_bottom - rows
            for _ in range(max(0, drop)):
                if main:
                    self._push_history_main(main[0], main_wrapped[0])
                    del main[0]
                    del main_wrapped[0]
            if not self.alt:
                self.y = max(0, self.y - drop)
        elif rows > old_rows:
            pull = rows - old_rows
            pulled = 0
            while pulled < pull and self.history:
                text, wrapped = self.history.pop()
                main.insert(0, parse_row(text, cols))
                main_wrapped.insert(0, wrapped)
                pulled += 1
            if not self.alt:
                self.y += pulled
        self.cols, self.rows = cols, rows
        self._fit_rows(main, main_wrapped)
        if self.alt:
            self._main_saved = (main, main_wrapped)
            self._fit_rows(self.lines, self.wrapped)
        self.top, self.bottom = 0, rows - 1
        self.x = min(self.x, cols - 1)
        self.y = min(self.y, rows - 1)
        self.pending_wrap = False
        self.dirty += 1

    def _push_history_main(self, row: list[Cell], wrapped: bool) -> None:
        if self.history_limit > 0:
            self.history.append((render_row(row, styled=True, keep_trailing=wrapped), wrapped))

    # ---------------------------------------------------------------- 输出
    @property
    def cursor(self) -> tuple[int, int]:
        return self.x, self.y

    def screen_lines(self, styled: bool = True, join: bool = False) -> list[str]:
        rows = [(render_row(row, styled, keep_trailing=self.wrapped[i] and join), self.wrapped[i])
                for i, row in enumerate(self.lines)]
        return _join_rows(rows) if join else [t for t, _ in rows]

    def scrollback_lines(self, limit: int, styled: bool = True,
                         join: bool = False) -> list[str]:
        """历史 + 可见屏的最后 limit 行, 对应 capture-pane -S -limit。"""
        rows: list[tuple[str, bool]] = []
        if limit > 0:
            hist = list(self.history)[-limit:]
            rows.extend(hist if styled else [(strip_sgr(t), w) for t, w in hist])
        rows.extend((render_row(row, styled, keep_trailing=self.wrapped[i] and join),
                     self.wrapped[i]) for i, row in enumerate(self.lines))
        return _join_rows(rows) if join else [t for t, _ in rows]

    def redraw_bytes(self) -> bytes:
        """整屏重绘序列: 放在回放的历史之后, 让浏览器视口精确等于可见屏。"""
        out = ["\x1b[0m"]
        rows = self.screen_lines(styled=True)
        out.append("\r\n".join(rows))
        out.append(f"\x1b[0m\x1b[{self.rows};1H")     # 停在最后一行再定位
        out.append(f"\x1b[{self.y + 1};{self.x + 1}H")
        out.append("\x1b[?25h" if self.cursor_visible else "\x1b[?25l")
        return "".join(out).encode("utf-8", "replace")


# ------------------------------------------------------------------ 行渲染
def render_row(row: list[Cell], styled: bool = True, keep_trailing: bool = False) -> str:
    end = len(row)
    if not keep_trailing:
        while end > 0 and row[end - 1][0] in (" ", "") and not row[end - 1][1]:
            end -= 1
    parts: list[str] = []
    current = ""
    for ch, attr in row[:end]:
        if ch == "":
            continue
        if styled and attr != current:
            parts.append("\x1b[0m")
            if attr:
                parts.append(f"\x1b[{attr}m")
            current = attr
        parts.append(ch)
    if styled and current:
        parts.append("\x1b[0m")
    return "".join(parts)


def _join_rows(rows: list[tuple[str, bool]]) -> list[str]:
    out: list[str] = []
    buf = ""
    joining = False
    for text, wrapped in rows:
        buf = buf + text if joining else text
        if wrapped:
            joining = True
            continue
        out.append(buf)
        buf = ""
        joining = False
    if joining:
        out.append(buf)
    return out


_SGR_RE = None


def strip_sgr(text: str) -> str:
    global _SGR_RE
    if _SGR_RE is None:
        import re
        _SGR_RE = re.compile(r"\x1b\[[0-9;:]*m")
    return _SGR_RE.sub("", text)


def parse_row(text: str, cols: int) -> list[Cell]:
    """把渲染过的一行还原成单元格 (resize 拉回历史时使用)。"""
    screen = Screen(cols, 1, history_limit=0)
    screen.autowrap = False
    screen.feed(text)
    return screen.lines[0]
