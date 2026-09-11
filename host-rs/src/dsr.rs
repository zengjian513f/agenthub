//! 从 pty 输出里切出设备状态查询（DSR），其余字节原样放行。
//!
//! 为什么必须做：ConPTY 以 `PSUEDOCONSOLE_INHERIT_CURSOR` 创建伪控制台，conhost
//! 启动后先发 `ESC[6n` 问"光标在哪"，拿到 `ESC[row;colR` 之前既不产出输出也不消费
//! 输入。Unix pty 从不主动问，所以这个依赖在 Linux 上永远看不见。宿主不应答就是
//! 双向死锁：会话看起来活着，send/capture 全部为空。
//!
//! 只截 DSR（`ESC[5n` / `ESC[6n` / `ESC[?6n`）。查询不会被转发给浏览器，因此
//! xterm.js 不会再答一遍，应答权完全在宿主这边——无论有没有客户端连着，
//! 行为都一样。其他查询（例如 DA `ESC[c`）保持透传，交给连着的终端回答。

/// 切分结果里的一片：要喂给模型并转发的数据，或一个待应答的查询。
#[derive(Debug, Clone, PartialEq)]
pub enum Piece {
    Data(Vec<u8>),
    /// `ESC[6n`（dec=false）或 `ESC[?6n`（dec=true）：报告光标位置。
    CursorReport { dec: bool },
    /// `ESC[5n`：报告设备状态，固定回 `ESC[0n`。
    DeviceOk,
}

impl Piece {
    pub fn data_len(&self) -> usize {
        match self {
            Self::Data(bytes) => bytes.len(),
            _ => 0,
        }
    }
}

#[derive(Default)]
enum State {
    #[default]
    Ground,
    Esc,
    Csi,
}

/// 跨 chunk 保持状态的最小 CSI 扫描器：只认 DSR，别的序列一概不碰。
#[derive(Default)]
pub struct Scanner {
    state: State,
    params: Vec<u8>,
    /// 已经吃进 state/params、但还没决定去向的字节；不是 DSR 就原样吐回。
    held: Vec<u8>,
}

impl Scanner {
    /// 返回按原始顺序排列的片段。没有查询时恰好是一片 Data，调用方可以据此
    /// 跳过拼接、直接转发原 chunk。
    pub fn scan(&mut self, data: &[u8]) -> Vec<Piece> {
        let mut pieces: Vec<Piece> = Vec::new();
        // held 跨 chunk 保留：证明不是 DSR 时才按原顺序吐回流里。这里不能预先
        // 清空它，否则一个被切断的查询会漏成普通字节。
        let mut plain: Vec<u8> = Vec::new();
        for &byte in data {
            match self.state {
                State::Ground => {
                    if byte == 0x1b {
                        self.state = State::Esc;
                        self.held.push(byte);
                    } else {
                        plain.push(byte);
                    }
                }
                State::Esc => {
                    if byte == b'[' {
                        self.held.push(byte);
                        self.state = State::Csi;
                        self.params.clear();
                    } else if byte == 0x1b {
                        // 连续两个 ESC：前一个原样放回，后一个重新开始计。
                        self.flush_held(&mut plain);
                        self.held.push(byte);
                    } else {
                        // 不是 CSI：整段原样放回，不猜它的意思。
                        self.held.push(byte);
                        self.flush_held(&mut plain);
                        self.state = State::Ground;
                    }
                }
                State::Csi => {
                    self.held.push(byte);
                    if (0x30..=0x3f).contains(&byte) || (0x20..=0x2f).contains(&byte) {
                        self.params.push(byte);
                        continue;
                    }
                    if (0x40..=0x7e).contains(&byte) {
                        let piece = match (byte, self.params.as_slice()) {
                            (b'n', b"6") => Some(Piece::CursorReport { dec: false }),
                            (b'n', b"?6") => Some(Piece::CursorReport { dec: true }),
                            (b'n', b"5") => Some(Piece::DeviceOk),
                            _ => None,
                        };
                        match piece {
                            Some(piece) => {
                                self.held.clear();
                                if !plain.is_empty() {
                                    pieces.push(Piece::Data(std::mem::take(&mut plain)));
                                }
                                pieces.push(piece);
                            }
                            None => self.flush_held(&mut plain),
                        }
                        self.state = State::Ground;
                        self.params.clear();
                    } else {
                        // 非法字节终止这个序列；原样放回，不猜它的意思。
                        self.flush_held(&mut plain);
                        self.state = State::Ground;
                        self.params.clear();
                    }
                }
            }
        }
        if !plain.is_empty() {
            pieces.push(Piece::Data(plain));
        }
        pieces
    }

    fn flush_held(&mut self, plain: &mut Vec<u8>) {
        plain.extend_from_slice(&std::mem::take(&mut self.held));
    }
}

/// DSR 应答。CPR 的行列是 1 起算。
pub fn reply(piece: &Piece, col: u16, row: u16) -> Option<Vec<u8>> {
    match piece {
        Piece::Data(_) => None,
        Piece::DeviceOk => Some(b"\x1b[0n".to_vec()),
        Piece::CursorReport { dec } => Some(if *dec {
            format!("\x1b[?{};{};1R", row + 1, col + 1).into_bytes()
        } else {
            format!("\x1b[{};{}R", row + 1, col + 1).into_bytes()
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data(bytes: &[u8]) -> Piece {
        Piece::Data(bytes.to_vec())
    }

    #[test]
    fn a_lone_cursor_query_is_lifted_out_of_the_stream() {
        let mut scanner = Scanner::default();
        assert_eq!(scanner.scan(b"\x1b[6n"), vec![Piece::CursorReport { dec: false }]);
    }

    #[test]
    fn surrounding_output_keeps_its_order() {
        let mut scanner = Scanner::default();
        assert_eq!(
            scanner.scan(b"before\x1b[6nafter"),
            vec![data(b"before"), Piece::CursorReport { dec: false }, data(b"after")]
        );
    }

    #[test]
    fn a_query_split_across_reads_is_still_found() {
        let mut scanner = Scanner::default();
        // conhost 通常单独发这 4 个字节，但不能指望它永远如此
        assert_eq!(scanner.scan(b"x\x1b"), vec![data(b"x")]);
        assert_eq!(scanner.scan(b"[6"), vec![]);
        assert_eq!(scanner.scan(b"ny"), vec![Piece::CursorReport { dec: false }, data(b"y")]);
    }

    #[test]
    fn other_sequences_pass_through_untouched() {
        let mut scanner = Scanner::default();
        let stream = b"\x1b[1;31mRED\x1b[0m\x1b[2K\x1b[?25l\x1b[c\x1b[1C";
        assert_eq!(scanner.scan(stream), vec![data(stream)]);
    }

    #[test]
    fn a_sequence_split_across_reads_is_reassembled_in_order() {
        let mut scanner = Scanner::default();
        assert_eq!(scanner.scan(b"a\x1b[1;3"), vec![data(b"a")]);
        assert_eq!(scanner.scan(b"1mred"), vec![data(b"\x1b[1;31mred")]);
    }

    #[test]
    fn the_dec_variant_and_device_status_are_recognised() {
        let mut scanner = Scanner::default();
        assert_eq!(scanner.scan(b"\x1b[?6n"), vec![Piece::CursorReport { dec: true }]);
        assert_eq!(scanner.scan(b"\x1b[5n"), vec![Piece::DeviceOk]);
    }

    #[test]
    fn replies_are_one_based() {
        assert_eq!(reply(&Piece::CursorReport { dec: false }, 0, 0).unwrap(), b"\x1b[1;1R");
        assert_eq!(reply(&Piece::CursorReport { dec: false }, 11, 16).unwrap(), b"\x1b[17;12R");
        assert_eq!(reply(&Piece::CursorReport { dec: true }, 4, 2).unwrap(), b"\x1b[?3;5;1R");
        assert_eq!(reply(&Piece::DeviceOk, 9, 9).unwrap(), b"\x1b[0n");
        assert!(reply(&data(b"x"), 0, 0).is_none());
    }

    #[test]
    fn an_escape_that_is_not_a_csi_is_returned_as_is() {
        let mut scanner = Scanner::default();
        assert_eq!(scanner.scan(b"\x1bOA"), vec![data(b"\x1bOA")]);
        assert_eq!(scanner.scan(b"\x1b\x1b[6n"), vec![data(b"\x1b"), Piece::CursorReport { dec: false }]);
    }
}
