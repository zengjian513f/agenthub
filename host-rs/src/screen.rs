//! vt100 之上的薄封装，提供与 Python 参考实现同语义的截屏 / 光标 / 回放。
//!
//! 只服务 capture / cursor 查询和 attach 回放：实时字节由读线程直接转发，
//! 不经过这里。SGR 的具体字节由 vt100 生成，和参考实现不必逐字节相同；
//! 调用方（claude_bridge / codex_bridge）都先剥离转义再解析文本与光标。

pub struct Screen {
    parser: vt100::Parser,
    cols: u16,
    rows: u16,
}

impl Screen {
    pub fn new(cols: u16, rows: u16, history: usize) -> Self {
        Self {
            parser: vt100::Parser::new(rows.max(1), cols.max(1), history),
            cols: cols.max(1),
            rows: rows.max(1),
        }
    }

    pub fn feed(&mut self, data: &[u8]) {
        self.parser.process(data);
    }

    pub fn resize(&mut self, cols: u16, rows: u16) {
        self.cols = cols.max(1);
        self.rows = rows.max(1);
        self.parser.screen_mut().set_size(self.rows, self.cols);
    }

    pub fn cursor(&self) -> (u16, u16) {
        let (row, col) = self.parser.screen().cursor_position();
        (col, row)
    }

    pub fn cursor_visible(&self) -> bool {
        !self.parser.screen().hide_cursor()
    }

    pub fn alt(&self) -> bool {
        self.parser.screen().alternate_screen()
    }

    pub fn app_cursor(&self) -> bool {
        self.parser.screen().application_cursor()
    }

    pub fn bracketed_paste(&self) -> bool {
        self.parser.screen().bracketed_paste()
    }

    /// 当前可见屏的各行；join 为真时把软换行合并成逻辑行。
    pub fn screen_lines(&mut self, styled: bool, join: bool) -> Vec<String> {
        self.parser.screen_mut().set_scrollback(0);
        let rows = self.collect_visible(styled);
        finish(rows, join)
    }

    /// vt100 不公开历史长度；set_scrollback 会被 clamp 到真实长度，借此探测。
    fn scrollback_len(&mut self) -> usize {
        let screen = self.parser.screen_mut();
        let keep = screen.scrollback();
        screen.set_scrollback(usize::MAX);
        let available = screen.scrollback();
        screen.set_scrollback(keep);
        available
    }

    /// 按绝对行号取一段行。绝对编号：历史为 0..available，可见屏接在后面。
    ///
    /// `set_scrollback(off)` 让可见窗口覆盖绝对行 `[available-off, available-off+rows)`，
    /// 一次只能看到一屏，所以要按窗口翻页。推进必须用绝对行号，用"已取条数"会
    /// 在 limit 不是整屏倍数时重复取行。
    fn collect_range(
        &mut self,
        from_abs: usize,
        end_abs: usize,
        styled: bool,
    ) -> Vec<(String, bool)> {
        let available = self.scrollback_len();
        let mut out: Vec<(String, bool)> = Vec::new();
        let mut next = from_abs.min(end_abs);
        while next < end_abs {
            let window_start = next.min(available);
            self.parser
                .screen_mut()
                .set_scrollback(available - window_start);
            let window = self.collect_visible(styled);
            let skip = next - window_start;
            if skip >= window.len() {
                break;                      // 窗口无法再前进，避免空转
            }
            for row in window.into_iter().skip(skip) {
                out.push(row);
                next += 1;
                if next >= end_abs {
                    break;
                }
            }
        }
        self.parser.screen_mut().set_scrollback(0);
        out
    }

    /// 历史最后 limit 行加上可见屏，对应 capture-pane -S -limit。
    pub fn scrollback_lines(&mut self, limit: usize, styled: bool, join: bool) -> Vec<String> {
        let available = self.scrollback_len();
        let rows = usize::from(self.rows).max(1);
        let from = available - limit.min(available);
        let collected = self.collect_range(from, available + rows, styled);
        finish(collected, join)
    }

    /// attach 回放：历史文本 + 完整终端状态（含模式与光标）。
    pub fn replay_bytes(&mut self, history: usize) -> Vec<u8> {
        let available = self.scrollback_len();
        let from = available - history.min(available);
        let mut out = Vec::new();
        if from < available {
            // 只取历史部分；可见屏由 state_formatted 负责，避免重复一屏。
            for line in finish(self.collect_range(from, available, true), true) {
                out.extend_from_slice(line.as_bytes());
                out.extend_from_slice(b"\r\n");
            }
            out.extend_from_slice(b"\x1b[0m");
        }
        // state_formatted 同时带回屏幕内容、属性、光标与各项模式，
        // 比自己拼重绘序列更完整（备用屏、DECCKM、bracketed paste 都在内）。
        out.extend_from_slice(&self.parser.screen().state_formatted());
        out
    }

    fn collect_visible(&self, styled: bool) -> Vec<(String, bool)> {
        let screen = self.parser.screen();
        let wrapped: Vec<bool> = (0..self.rows).map(|i| screen.row_wrapped(i)).collect();
        if styled {
            screen
                .rows_formatted(0, self.cols)
                .enumerate()
                .map(|(i, raw)| {
                    (String::from_utf8_lossy(&raw).into_owned(), wrapped[i])
                })
                .collect()
        } else {
            screen
                .rows(0, self.cols)
                .enumerate()
                .map(|(i, text)| (text, wrapped[i]))
                .collect()
        }
    }
}

/// 软换行合并：与参考实现一致，wrapped 行与下一行拼成同一条逻辑行。
fn finish(rows: Vec<(String, bool)>, join: bool) -> Vec<String> {
    if !join {
        return rows.into_iter().map(|(text, _)| text).collect();
    }
    let mut out: Vec<String> = Vec::new();
    let mut buf = String::new();
    let mut joining = false;
    for (text, wrapped) in rows {
        if joining {
            buf.push_str(&text);
        } else {
            buf = text;
        }
        if wrapped {
            joining = true;
            continue;
        }
        out.push(std::mem::take(&mut buf));
        joining = false;
    }
    if joining {
        out.push(buf);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(screen: &mut Screen) -> Vec<String> {
        screen.screen_lines(false, false)
    }

    #[test]
    fn lines_scroll_into_history_and_soft_wraps_join() {
        let mut screen = Screen::new(10, 3, 100);
        screen.feed(b"hello\r\nworld\r\nabcdefghijklmno");
        assert_eq!(plain(&mut screen), vec!["world", "abcdefghij", "klmno"]);
        assert_eq!(screen.cursor(), (5, 2));
        let joined = screen.scrollback_lines(100, false, true);
        assert!(joined.iter().any(|l| l.trim_end() == "hello"), "{joined:?}");
        assert!(
            joined.iter().any(|l| l.starts_with("abcdefghijklmno")),
            "软换行未合并: {joined:?}"
        );
    }

    #[test]
    fn styled_rows_carry_sgr_that_strips_back_to_plain_text() {
        let mut screen = Screen::new(20, 2, 0);
        screen.feed(b"\x1b[1;31mRED\x1b[0m ok");
        let styled = screen.screen_lines(true, false).remove(0);
        assert!(styled.contains("RED"));
        assert!(styled.contains('\x1b'), "样式行应带 SGR: {styled:?}");
        let stripped = strip_ansi(&styled);
        assert_eq!(stripped.trim_end(), "RED ok");
    }

    #[test]
    fn cursor_moves_erase_and_insert_delete() {
        let mut screen = Screen::new(10, 4, 0);
        screen.feed(b"line1\r\nline2\r\nline3\r\nline4");
        screen.feed(b"\x1b[2;1H\x1b[K");
        screen.feed(b"\x1b[1;3H\x1b[2@");
        screen.feed(b"\x1b[4;1H\x1b[2P");
        let rows: Vec<String> = plain(&mut screen).iter().map(|r| r.trim_end().to_string()).collect();
        assert_eq!(rows, vec!["li  ne1", "", "line3", "ne4"]);
        screen.feed(b"\x1b[2J\x1b[H");
        assert!(plain(&mut screen).iter().all(|r| r.trim().is_empty()));
        assert_eq!(screen.cursor(), (0, 0));
    }

    #[test]
    fn alternate_screen_restores_main_content() {
        let mut screen = Screen::new(10, 3, 10);
        screen.feed(b"main\r\n");
        screen.feed(b"\x1b[?1049h\x1b[HALT");
        assert!(screen.alt());
        assert_eq!(plain(&mut screen)[0].trim_end(), "ALT");
        screen.feed(b"\x1b[?1049l");
        assert!(!screen.alt());
        assert_eq!(plain(&mut screen)[0].trim_end(), "main");
    }

    #[test]
    fn wide_characters_take_two_columns() {
        let mut screen = Screen::new(6, 2, 0);
        screen.feed("你好x".as_bytes());
        assert_eq!(plain(&mut screen)[0].trim_end(), "你好x");
        assert_eq!(screen.cursor(), (5, 0));
    }

    #[test]
    fn modes_needed_by_the_host_are_tracked() {
        let mut screen = Screen::new(10, 3, 0);
        assert!(!screen.app_cursor());
        assert!(!screen.bracketed_paste());
        assert!(screen.cursor_visible());
        screen.feed(b"\x1b[?1h\x1b[?2004h\x1b[?25l");
        assert!(screen.app_cursor());
        assert!(screen.bracketed_paste());
        assert!(!screen.cursor_visible());
        screen.feed(b"\x1b[?1l\x1b[?2004l\x1b[?25h");
        assert!(!screen.app_cursor());
        assert!(!screen.bracketed_paste());
        assert!(screen.cursor_visible());
    }

    #[test]
    fn scrollback_spans_more_rows_than_one_screen_in_order() {
        let mut screen = Screen::new(20, 3, 100);
        for i in 1..=20 {
            screen.feed(format!("line-{i}\r\n").as_bytes());
        }
        let rows: Vec<String> = screen
            .scrollback_lines(100, false, false)
            .iter()
            .map(|r| r.trim_end().to_string())
            .filter(|r| !r.is_empty())
            .collect();
        // 历史要按顺序完整取回，不能因为按窗口分页而漏行或重复
        let expected: Vec<String> = (1..=20).map(|i| format!("line-{i}")).collect();
        assert_eq!(rows, expected, "历史行不连续: {rows:?}");
    }

    #[test]
    fn a_limit_keeps_only_the_tail_of_the_history() {
        let mut screen = Screen::new(20, 3, 100);
        for i in 1..=20 {
            screen.feed(format!("line-{i}\r\n").as_bytes());
        }
        let rows: Vec<String> = screen
            .scrollback_lines(5, false, false)
            .iter()
            .map(|r| r.trim_end().to_string())
            .filter(|r| !r.is_empty())
            .collect();
        // 历史按 limit 截尾后必须连续且不重复，末尾仍是最新一行
        assert_eq!(rows.last().map(String::as_str), Some("line-20"), "{rows:?}");
        let mut seen = std::collections::HashSet::new();
        assert!(rows.iter().all(|r| seen.insert(r.clone())), "历史重复: {rows:?}");
        let numbers: Vec<usize> = rows
            .iter()
            .map(|r| r.trim_start_matches("line-").parse().unwrap())
            .collect();
        assert!(
            numbers.windows(2).all(|w| w[1] == w[0] + 1),
            "历史不连续: {numbers:?}"
        );
    }

    #[test]
    fn replay_carries_history_then_current_state() {
        let mut screen = Screen::new(20, 3, 100);
        for i in 1..=10 {
            screen.feed(format!("line-{i}\r\n").as_bytes());
        }
        let replay = String::from_utf8_lossy(&screen.replay_bytes(100)).into_owned();
        let stripped = strip_ansi(&replay);
        assert!(stripped.contains("line-1"), "回放缺少历史");
        assert!(stripped.contains("line-10"), "回放缺少当前画面");
        assert!(
            stripped.find("line-1").unwrap() < stripped.find("line-10").unwrap(),
            "回放顺序颠倒"
        );
    }

    #[test]
    fn resize_keeps_the_model_usable() {
        let mut screen = Screen::new(10, 3, 50);
        screen.feed(b"a\r\nb\r\nc");
        screen.resize(20, 5);
        assert_eq!(screen.screen_lines(false, false).len(), 5);
        screen.feed(b"\r\nd");
        assert!(screen
            .screen_lines(false, false)
            .iter()
            .any(|r| r.trim_end() == "d"));
    }

    /// 与 claude_bridge / codex_bridge 剥离转义的正则等价的最小实现。
    fn strip_ansi(text: &str) -> String {
        let mut out = String::new();
        let mut chars = text.chars().peekable();
        while let Some(c) = chars.next() {
            if c != '\x1b' {
                out.push(c);
                continue;
            }
            match chars.peek() {
                Some('[') => {
                    chars.next();
                    while let Some(&c) = chars.peek() {
                        chars.next();
                        if ('@'..='~').contains(&c) {
                            break;
                        }
                    }
                }
                Some(']') => {
                    chars.next();
                    while let Some(&c) = chars.peek() {
                        chars.next();
                        if c == '\x07' {
                            break;
                        }
                    }
                }
                _ => {
                    chars.next();
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod bench {
    use super::*;

    /// 不是断言性能的测试，而是把吞吐量打印出来，方便和参考实现对比。
    #[test]
    #[ignore]
    fn throughput() {
        let mut data = Vec::new();
        let words = ["hello", "world", "你好", "世界", "def", "return", "错误", "OK"];
        for i in 0..120_000usize {
            let line: String = (0..10)
                .map(|j| words[(i + j) % words.len()])
                .collect::<Vec<_>>()
                .join(" ");
            data.extend_from_slice(
                format!("\x1b[2K\x1b[38;5;{}m{line}\x1b[0m\n", i % 255 + 1).as_bytes(),
            );
        }
        let mut screen = Screen::new(200, 50, 10_000);
        let start = std::time::Instant::now();
        for chunk in data.chunks(65536) {
            screen.feed(chunk);
        }
        let secs = start.elapsed().as_secs_f64();
        println!(
            "vt100 吞吐: {:.1} MB/s ({:.2} MB / {:.2}s)",
            data.len() as f64 / secs / 1e6,
            data.len() as f64 / 1e6,
            secs
        );
    }
}
