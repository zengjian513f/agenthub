//! 单个托管会话的宿主进程，与 Python 参考实现同协议、同线程结构。
//!
//! 读线程只做两件事：把字节原样转发给已连接的客户端，并放进待喂队列。
//! 屏幕模型在独立线程里消费那个队列，只服务 capture/cursor 与 attach 回放，
//! 绝不挡住 pty → 浏览器这条路径；积压超过上限就丢最旧的一段并报出 dropped。

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use portable_pty::{CommandBuilder, MasterPty, PtySize};
use serde_json::{json, Value};

use crate::dsr::{self, Piece};
use crate::protocol::{
    key_bytes, pack_frame, read_frames, recv_json, send_json, FRAME_DATA, FRAME_EXIT,
    FRAME_RESIZE,
};
use crate::screen::Screen;
use crate::transport::{Listener, Stream};

pub const BACKLOG_LIMIT: usize = 32 << 20;
pub const SCREEN_SYNC_TIMEOUT: Duration = Duration::from_secs(2);
/// 新会话必须清掉可能从 Web 服务继承的旧会话身份，否则 CLI 会接上错误的会话。
const STRIP_ENV: &[&str] = &[
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_COMPANION_SESSION_ID",
    "GROK_SESSION_ID",
    "TMUX",
];

struct Client {
    id: u64,
    out: Mutex<Stream>,
    dead: AtomicBool,
}

impl Client {
    fn send(&self, frame: &[u8]) {
        if self.dead.load(Ordering::Relaxed) {
            return;
        }
        let mut out = match self.out.lock() {
            Ok(g) => g,
            Err(_) => return,
        };
        if out.write_all(frame).and_then(|_| out.flush()).is_err() {
            self.dead.store(true, Ordering::Relaxed);
        }
    }
}

#[derive(Default)]
struct Backlog {
    queue: VecDeque<Piece>,
    inflight: Option<Piece>,
    pending: usize,
    fed: u64,
    applied: u64,
    dropped: u64,
}

impl Backlog {
    /// 积压超限时丢最旧的数据，绝不阻塞读线程——等模型追赶会直接变成终端卡顿。
    /// 查询必须留下：丢掉它就等于让应用永远等不到应答。
    fn trim(&mut self, limit: usize) {
        while self.pending > limit {
            let at = self
                .queue
                .iter()
                .position(|piece| matches!(piece, Piece::Data(_)));
            match at.and_then(|at| self.queue.remove(at)) {
                Some(old) => {
                    self.pending -= old.data_len().min(self.pending);
                    self.dropped += old.data_len() as u64;
                }
                None => break,
            }
        }
    }

    /// attach 回放要带上尚未进入模型的原始字节；查询不是显示内容，跳过。
    fn unapplied_bytes(&self) -> Vec<u8> {
        let mut out = Vec::new();
        for piece in self.inflight.iter().chain(self.queue.iter()) {
            if let Piece::Data(bytes) = piece {
                out.extend_from_slice(bytes);
            }
        }
        out
    }
}

pub struct Session {
    pub name: Mutex<String>,
    argv: Vec<String>,
    cwd: Option<String>,
    meta: Value,
    directory: PathBuf,
    created: u64,
    token: String,
    history: usize,
    size: Mutex<(u16, u16)>,
    screen: Mutex<Screen>,
    backlog: Mutex<Backlog>,
    backlog_cv: Condvar,
    clients: Mutex<Vec<Arc<Client>>>,
    next_client: AtomicU64,
    writer: Mutex<Box<dyn Write + Send>>,
    master: Mutex<Box<dyn MasterPty + Send>>,
    child: Mutex<Box<dyn portable_pty::Child + Send + Sync>>,
    child_pid: u32,
    exited: AtomicBool,
    finishing: AtomicBool,
    exit_code: AtomicI32,
    listener: Mutex<Option<Arc<Listener>>>,
    port: Mutex<u16>,
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn atomic_write(path: &Path, data: &str) -> std::io::Result<()> {
    let tmp = path.with_extension(format!("json.{}.tmp", std::process::id()));
    std::fs::write(&tmp, data)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600))?;
    }
    std::fs::rename(&tmp, path)
}

impl Session {
    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        name: String,
        argv: Vec<String>,
        cwd: Option<String>,
        cols: u16,
        rows: u16,
        meta: Value,
        directory: PathBuf,
        history: usize,
    ) -> std::io::Result<Arc<Self>> {
        if argv.is_empty() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "缺少命令",
            ));
        }
        std::fs::create_dir_all(&directory)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(
                &directory,
                std::fs::Permissions::from_mode(0o700),
            );
        }

        let pty = portable_pty::native_pty_system()
            .openpty(PtySize {
                rows: rows.max(1),
                cols: cols.max(1),
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| std::io::Error::other(format!("打开 pty 失败: {e}")))?;

        let mut cmd = CommandBuilder::new(&argv[0]);
        for arg in &argv[1..] {
            cmd.arg(arg);
        }
        if let Some(dir) = cwd.as_deref() {
            if Path::new(dir).is_dir() {
                cmd.cwd(dir);
            }
        }
        for key in STRIP_ENV {
            cmd.env_remove(key);
        }
        if std::env::var_os("TERM").is_none() {
            cmd.env("TERM", "xterm-256color");
        }
        cmd.env("COLORTERM", "truecolor");
        cmd.env("AGENTHUB_SESSION", &name);

        let child = pty
            .slave
            .spawn_command(cmd)
            .map_err(|e| std::io::Error::other(format!("启动命令失败: {e}")))?;
        let child_pid = child.process_id().unwrap_or(0);
        let reader = pty
            .master
            .try_clone_reader()
            .map_err(|e| std::io::Error::other(format!("克隆 pty 读端失败: {e}")))?;
        let writer = pty
            .master
            .take_writer()
            .map_err(|e| std::io::Error::other(format!("取 pty 写端失败: {e}")))?;
        drop(pty.slave);

        let token = if cfg!(windows) {
            random_token()
        } else {
            String::new()
        };
        let session = Arc::new(Self {
            name: Mutex::new(name),
            argv,
            cwd: cwd.filter(|c| !c.is_empty()),
            meta,
            directory,
            created: now_secs(),
            token,
            history,
            size: Mutex::new((cols.max(1), rows.max(1))),
            screen: Mutex::new(Screen::new(cols.max(1), rows.max(1), history)),
            backlog: Mutex::new(Backlog::default()),
            backlog_cv: Condvar::new(),
            clients: Mutex::new(Vec::new()),
            next_client: AtomicU64::new(1),
            writer: Mutex::new(writer),
            master: Mutex::new(pty.master),
            child: Mutex::new(child),
            child_pid,
            exited: AtomicBool::new(false),
            finishing: AtomicBool::new(false),
            exit_code: AtomicI32::new(0),
            listener: Mutex::new(None),
            port: Mutex::new(0),
        });

        let listener = session.listen()?;
        *session.listener.lock().unwrap() = Some(listener.clone());
        session.write_info();

        let reader_session = session.clone();
        std::thread::Builder::new()
            .name("pty-reader".into())
            .spawn(move || reader_session.read_loop(reader))?;
        let screen_session = session.clone();
        std::thread::Builder::new()
            .name("screen".into())
            .spawn(move || screen_session.screen_loop())?;
        let accept_session = session.clone();
        std::thread::Builder::new()
            .name("acceptor".into())
            .spawn(move || accept_session.accept_loop())?;
        Ok(session)
    }

    fn listen(&self) -> std::io::Result<Arc<Listener>> {
        #[cfg(unix)]
        {
            let listener = Listener::bind_unix(&self.sock_path())?;
            return Ok(Arc::new(listener));
        }
        #[cfg(not(unix))]
        {
            let (listener, port) = Listener::bind_local_tcp()?;
            *self.port.lock().unwrap() = port;
            Ok(Arc::new(listener))
        }
    }

    fn name_now(&self) -> String {
        self.name.lock().unwrap().clone()
    }

    fn sock_path(&self) -> PathBuf {
        self.directory.join(format!("{}.sock", self.name_now()))
    }

    fn info_path(&self) -> PathBuf {
        self.directory.join(format!("{}.json", self.name_now()))
    }

    fn cmd_label(&self) -> String {
        for item in &self.argv {
            let base = Path::new(item)
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_default();
            if matches!(
                base.as_str(),
                "claude" | "codex" | "grok" | "claude.exe" | "codex.exe" | "grok.exe"
            ) {
                return base;
            }
        }
        if self.argv.len() == 1 {
            return Path::new(&self.argv[0])
                .file_name()
                .map(|s| s.to_string_lossy().into_owned())
                .unwrap_or_else(|| self.argv[0].clone());
        }
        self.argv[0].clone()
    }

    pub fn info(&self) -> Value {
        let (cols, rows) = *self.size.lock().unwrap();
        let attached = self
            .clients
            .lock()
            .unwrap()
            .iter()
            .any(|c| !c.dead.load(Ordering::Relaxed));
        let mut info = json!({
            "name": self.name_now(),
            "host_pid": std::process::id(),
            "pid": self.child_pid,
            "cwd": self.cwd.clone().unwrap_or_default(),
            "cmd": self.cmd_label(),
            "argv": self.argv,
            "created": self.created,
            "cols": cols,
            "rows": rows,
            "attached": attached,
            "meta": self.meta,
            "backend": "rust",
        });
        let map = info.as_object_mut().unwrap();
        if cfg!(windows) {
            map.insert("port".into(), json!(*self.port.lock().unwrap()));
            map.insert("token".into(), json!(self.token));
        } else {
            map.insert(
                "sock".into(),
                json!(self.sock_path().to_string_lossy().into_owned()),
            );
        }
        info
    }

    pub fn write_info(&self) {
        let _ = atomic_write(&self.info_path(), &self.info().to_string());
    }

    pub fn cleanup(&self) {
        let _ = std::fs::remove_file(self.info_path());
        let _ = std::fs::remove_file(self.sock_path());
        let log = self.directory.join(format!("{}.log", self.name_now()));
        if let Ok(meta) = std::fs::metadata(&log) {
            if meta.is_file() && meta.len() == 0 {
                let _ = std::fs::remove_file(&log);
            }
        }
    }

    // ------------------------------------------------------------------ 输出
    fn read_loop(self: Arc<Self>, mut reader: Box<dyn Read + Send>) {
        let mut buf = vec![0u8; 65536];
        let mut scanner = dsr::Scanner::default();
        loop {
            let n = match reader.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            // 扫一遍找设备状态查询。绝大多数 chunk 里没有；扫描只跟 CSI 终止符
            // 打交道，比后面的 VT 解析便宜几个数量级，不会拖慢这条热路径。
            let pieces = scanner.scan(&buf[..n]);
            // 查询不转发给客户端：浏览器的 xterm.js 看不到它就不会再答一遍，
            // 应答权完全在宿主这边，有没有客户端连着行为都一样。
            let frame = {
                let mut joined: Vec<u8> = Vec::new();
                let payload: &[u8] = match pieces.as_slice() {
                    [Piece::Data(bytes)] => bytes,          // 常见情况：不额外拷贝
                    _ => {
                        for piece in &pieces {
                            if let Piece::Data(bytes) = piece {
                                joined.extend_from_slice(bytes);
                            }
                        }
                        &joined
                    }
                };
                (!payload.is_empty()).then(|| pack_frame(FRAME_DATA, payload))
            };
            let clients: Vec<Arc<Client>> = {
                let mut backlog = self.backlog.lock().unwrap();
                for piece in pieces {
                    backlog.pending += piece.data_len();
                    backlog.fed += piece.data_len() as u64;
                    backlog.queue.push_back(piece);
                }
                backlog.trim(BACKLOG_LIMIT);
                self.backlog_cv.notify_all();
                self.live_clients()
            };
            if let Some(frame) = frame {
                for client in clients {
                    client.send(&frame);
                }
            }
        }
        self.finish();
    }

    fn live_clients(&self) -> Vec<Arc<Client>> {
        self.clients
            .lock()
            .unwrap()
            .iter()
            .filter(|c| !c.dead.load(Ordering::Relaxed))
            .cloned()
            .collect()
    }

    fn screen_loop(self: Arc<Self>) {
        loop {
            let piece = {
                let mut backlog = self.backlog.lock().unwrap();
                while backlog.queue.is_empty() {
                    if self.exited.load(Ordering::Relaxed) {
                        return;
                    }
                    let (guard, _) = self
                        .backlog_cv
                        .wait_timeout(backlog, Duration::from_millis(200))
                        .unwrap();
                    backlog = guard;
                }
                let piece = backlog.queue.pop_front().unwrap();
                // 出队后仍计入 pending：在途块既不在队列也没进模型，
                // attach 的回放必须把它算上，否则客户端会丢这一段。
                backlog.inflight = Some(piece.clone());
                piece
            };
            let answer = match &piece {
                Piece::Data(bytes) => {
                    self.screen.lock().unwrap().feed(bytes);
                    None
                }
                query => {
                    // 在这里应答，光标就是流里这个位置的光标：前面的字节都已喂完。
                    let (col, row) = self.screen.lock().unwrap().cursor();
                    dsr::reply(query, col, row)
                }
            };
            {
                let mut backlog = self.backlog.lock().unwrap();
                backlog.inflight = None;
                backlog.pending -= piece.data_len().min(backlog.pending);
                backlog.applied += piece.data_len() as u64;
                self.backlog_cv.notify_all();
            }
            if let Some(answer) = answer {
                self.write_pty(&answer);
            }
        }
    }

    /// 等模型追上已读入的字节；返回仍未应用的字节数（0 表示完全同步）。
    fn wait_applied(&self, timeout: Duration) -> usize {
        let deadline = Instant::now() + timeout;
        let mut backlog = self.backlog.lock().unwrap();
        while backlog.pending > 0 {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            let (guard, _) = self
                .backlog_cv
                .wait_timeout(backlog, left.min(Duration::from_millis(50)))
                .unwrap();
            backlog = guard;
        }
        backlog.pending
    }

    fn finish(&self) {
        if self.finishing.swap(true, Ordering::SeqCst) {
            return;
        }
        // 读线程已到 EOF；先把积压喂完（此时 exited 仍为假，屏幕线程继续工作），
        // 最后一次 capture 才能拿到完整画面。
        self.wait_applied(SCREEN_SYNC_TIMEOUT);

        let code = {
            let mut child = self.child.lock().unwrap();
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => break status.exit_code() as i32,
                    _ => {
                        if Instant::now() >= deadline {
                            let _ = child.kill();
                            break child
                                .wait()
                                .map(|s| s.exit_code() as i32)
                                .unwrap_or(255);
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                }
            }
        };
        self.exit_code.store(code, Ordering::SeqCst);
        self.exited.store(true, Ordering::SeqCst);
        let clients: Vec<Arc<Client>> = std::mem::take(&mut *self.clients.lock().unwrap());
        let frame = pack_frame(FRAME_EXIT, json!({"code": code}).to_string().as_bytes());
        for client in clients {
            client.send(&frame);
            client.dead.store(true, Ordering::Relaxed);
            if let Ok(out) = client.out.lock() {
                out.shutdown();
            }
        }
        self.backlog_cv.notify_all();
    }

    pub fn serve(&self) -> i32 {
        while !self.exited.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_millis(100));
            // 子进程已退出但 pty 迟迟不给 EOF（极少数平台）时的兜底判定。
            if !self.finishing.load(Ordering::Relaxed) {
                let dead = matches!(self.child.lock().unwrap().try_wait(), Ok(Some(_)));
                if dead {
                    std::thread::sleep(Duration::from_millis(200));
                    self.finish();
                }
            }
        }
        self.cleanup();
        self.exit_code.load(Ordering::SeqCst)
    }

    pub fn stop(&self, force: bool) {
        if force {
            let _ = self.child.lock().unwrap().kill();
            return;
        }
        #[cfg(unix)]
        {
            // 与 tmux kill-session 一致：先 HUP 整个前台进程组。交互式 shell
            // 会忽略 TERM 却响应 HUP；CLI 收到 HUP 也有机会存盘。
            if self.child_pid > 0 {
                unsafe {
                    libc::killpg(self.child_pid as i32, libc::SIGHUP);
                }
            }
        }
        #[cfg(not(unix))]
        {
            let _ = self.child.lock().unwrap().kill();
        }
    }

    // ------------------------------------------------------------------ 连接
    /// 每轮重新取当前 listener：改名会换掉它，旧 listener 被唤醒后自然退场。
    fn accept_loop(self: Arc<Self>) {
        while !self.exited.load(Ordering::Relaxed) {
            let listener = match self.listener.lock().unwrap().clone() {
                Some(listener) => listener,
                None => return,
            };
            match listener.accept() {
                Ok(stream) => {
                    let session = self.clone();
                    let _ = std::thread::Builder::new()
                        .name("conn".into())
                        .spawn(move || session.serve_conn(stream));
                }
                Err(_) => {
                    let current = self.listener.lock().unwrap().clone();
                    match current {
                        Some(current) if !Arc::ptr_eq(&current, &listener) => continue,
                        _ => return,
                    }
                }
            }
        }
    }

    fn serve_conn(self: Arc<Self>, stream: Stream) {
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let mut reader = match stream.try_clone() {
            Ok(s) => s,
            Err(_) => return,
        };
        let mut writer = stream;
        let mut buffer: Vec<u8> = Vec::new();
        let req = match recv_json(&mut reader, &mut buffer) {
            Ok(v) => v,
            Err(e) => {
                let _ = send_json(&mut writer, &json!({"ok": false, "error": e.to_string()}));
                return;
            }
        };
        if cfg!(windows) && req.get("token").and_then(|v| v.as_str()) != Some(&self.token) {
            let _ = send_json(&mut writer, &json!({"ok": false, "error": "凭据不匹配"}));
            return;
        }
        let op = req.get("op").and_then(|v| v.as_str()).unwrap_or("").to_string();
        if op == "attach" {
            self.attach(reader, writer, buffer, &req);
            return;
        }
        let reply = match self.dispatch(&op, &req) {
            Ok(value) => value,
            Err(message) => json!({"ok": false, "error": message}),
        };
        let _ = send_json(&mut writer, &reply);
    }

    fn write_pty(&self, data: &[u8]) {
        if let Ok(mut writer) = self.writer.lock() {
            let _ = writer.write_all(data);
            let _ = writer.flush();
        }
    }

    fn dispatch(&self, op: &str, req: &Value) -> Result<Value, String> {
        match op {
            "info" => Ok(json!({
                "ok": true, "info": self.info(), "exited": self.exited.load(Ordering::Relaxed)
            })),
            "send" => {
                let text = req.get("text").and_then(|v| v.as_str()).unwrap_or("");
                self.write_pty(text.as_bytes());
                Ok(json!({"ok": true}))
            }
            "keys" => {
                let app_cursor = self.screen.lock().unwrap().app_cursor();
                let mut out = Vec::new();
                if let Some(keys) = req.get("keys").and_then(|v| v.as_array()) {
                    for key in keys {
                        if let Some(name) = key.as_str() {
                            out.extend_from_slice(&key_bytes(name, app_cursor));
                        }
                    }
                }
                self.write_pty(&out);
                Ok(json!({"ok": true}))
            }
            "paste" => {
                let text = req.get("text").and_then(|v| v.as_str()).unwrap_or("");
                let bracketed = self.screen.lock().unwrap().bracketed_paste();
                let wanted = req
                    .get("bracketed")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(true);
                let mut out = Vec::new();
                if bracketed && wanted {
                    out.extend_from_slice(b"\x1b[200~");
                    out.extend_from_slice(text.as_bytes());
                    out.extend_from_slice(b"\x1b[201~");
                } else {
                    out.extend_from_slice(text.as_bytes());
                }
                self.write_pty(&out);
                Ok(json!({"ok": true, "bracketed": bracketed}))
            }
            "resize" => {
                let cols = req.get("cols").and_then(|v| v.as_u64()).unwrap_or(0) as u16;
                let rows = req.get("rows").and_then(|v| v.as_u64()).unwrap_or(0) as u16;
                self.resize(cols, rows);
                Ok(json!({"ok": true}))
            }
            "capture" => self.capture(req),
            "cursor" => {
                let lag = self.wait_applied(SCREEN_SYNC_TIMEOUT);
                let dropped = self.backlog.lock().unwrap().dropped;
                let screen = self.screen.lock().unwrap();
                let (x, y) = screen.cursor();
                Ok(json!({
                    "ok": true, "x": x, "y": y,
                    "visible": screen.cursor_visible(), "alt": screen.alt(),
                    "lag": lag, "dropped": dropped
                }))
            }
            "rename" => self.rename(req.get("to").and_then(|v| v.as_str()).unwrap_or("")),
            "kill" => {
                self.stop(req.get("force").and_then(|v| v.as_bool()).unwrap_or(false));
                Ok(json!({"ok": true}))
            }
            other => Err(format!("未知操作: {other}")),
        }
    }

    fn capture(&self, req: &Value) -> Result<Value, String> {
        let kind = req.get("kind").and_then(|v| v.as_str()).unwrap_or("screen");
        if kind != "screen" && kind != "scrollback" {
            return Err(format!("未知捕获类型: {kind}"));
        }
        let styled = req.get("styled").and_then(|v| v.as_bool()).unwrap_or(true);
        let join = req.get("join").and_then(|v| v.as_bool()).unwrap_or(false);
        let lines = req.get("lines").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
        // 先让模型追上刚写出的输出，再取屏幕；composer 判定依赖这一点。
        let lag = self.wait_applied(SCREEN_SYNC_TIMEOUT);
        let dropped = self.backlog.lock().unwrap().dropped;
        let (cols, rows) = *self.size.lock().unwrap();
        let mut screen = self.screen.lock().unwrap();
        let text = if kind == "screen" {
            screen.screen_lines(styled, join)
        } else {
            screen.scrollback_lines(lines, styled, join)
        }
        .join("\n");
        let (x, y) = screen.cursor();
        Ok(json!({
            "ok": true, "text": text, "cursor": [x, y], "alt": screen.alt(),
            "cols": cols, "rows": rows, "lag": lag, "dropped": dropped
        }))
    }

    fn resize(&self, cols: u16, rows: u16) {
        let (cols, rows) = (cols.max(1), rows.max(1));
        {
            let mut size = self.size.lock().unwrap();
            if *size == (cols, rows) {
                return;
            }
            *size = (cols, rows);
        }
        self.screen.lock().unwrap().resize(cols, rows);
        if let Ok(master) = self.master.lock() {
            let _ = master.resize(PtySize {
                rows,
                cols,
                pixel_width: 0,
                pixel_height: 0,
            });
        }
        self.write_info();
    }

    fn rename(&self, new: &str) -> Result<Value, String> {
        if new.is_empty() || new.contains('/') || new.contains('\\') || new.starts_with('.') {
            return Err("会话名不合法".into());
        }
        let old = self.name_now();
        if new == old {
            return Ok(json!({"ok": true, "name": new}));
        }
        if self.directory.join(format!("{new}.json")).exists() {
            return Err(format!("会话已存在: {new}"));
        }
        let old_info = self.info_path();
        let old_sock = self.sock_path();
        #[cfg(unix)]
        {
            let path = self.directory.join(format!("{new}.sock"));
            let fresh = Listener::bind_unix(&path)
                .map_err(|e| format!("重建 socket 失败: {e}"))?;
            *self.listener.lock().unwrap() = Some(Arc::new(fresh));
        }
        *self.name.lock().unwrap() = new.to_string();
        self.write_info();
        #[cfg(unix)]
        {
            // 唤醒仍阻塞在旧 socket 上的 accept：它会看到 listener 已更换并继续。
            let _ = Stream::connect_unix(&old_sock);
        }
        let _ = std::fs::remove_file(old_info);
        let _ = std::fs::remove_file(old_sock);
        Ok(json!({"ok": true, "name": new}))
    }

    fn attach(self: Arc<Self>, mut reader: Stream, mut writer: Stream, mut buffer: Vec<u8>, req: &Value) {
        if self.exited.load(Ordering::Relaxed) {
            let _ = send_json(&mut writer, &json!({"ok": false, "error": "会话已结束"}));
            return;
        }
        let (cur_cols, cur_rows) = *self.size.lock().unwrap();
        let cols = req.get("cols").and_then(|v| v.as_u64()).unwrap_or(cur_cols as u64) as u16;
        let rows = req.get("rows").and_then(|v| v.as_u64()).unwrap_or(cur_rows as u64) as u16;
        self.resize(cols, rows);

        let client = Arc::new(Client {
            id: self.next_client.fetch_add(1, Ordering::Relaxed),
            out: Mutex::new(match writer.try_clone() {
                Ok(s) => s,
                Err(_) => return,
            }),
            dead: AtomicBool::new(false),
        });
        // 回放必须和这个客户端之后收到的实时字节严格接续：模型可能还没消化完
        // 已读入的数据，所以回放 = 模型当前画面 + 尚未喂入的原始字节；客户端
        // 加入列表与快照在同一把 backlog 锁下完成，读线程分发不会插进中间。
        let replay = {
            let backlog = self.backlog.lock().unwrap();
            let mut replay = Vec::new();
            if req.get("replay").and_then(|v| v.as_bool()).unwrap_or(true) {
                replay.extend_from_slice(&self.screen.lock().unwrap().replay_bytes(self.history));
                replay.extend_from_slice(&backlog.unapplied_bytes());
            }
            self.clients.lock().unwrap().push(client.clone());
            replay
        };
        let (size_cols, size_rows) = *self.size.lock().unwrap();
        if send_json(&mut writer, &json!({"ok": true, "cols": size_cols, "rows": size_rows})).is_err() {
            self.drop_client(&client);
            return;
        }
        self.write_info();
        let _ = reader.set_read_timeout(None);
        if !replay.is_empty() {
            client.send(&pack_frame(FRAME_DATA, &replay));
        }

        let mut chunk = vec![0u8; 65536];
        while !client.dead.load(Ordering::Relaxed) && !self.exited.load(Ordering::Relaxed) {
            let n = match reader.read(&mut chunk) {
                Ok(0) | Err(_) => break,
                Ok(n) => n,
            };
            buffer.extend_from_slice(&chunk[..n]);
            for (kind, payload) in read_frames(&mut buffer) {
                if kind == FRAME_DATA {
                    self.write_pty(&payload);
                } else if kind == FRAME_RESIZE {
                    if let Ok(size) = serde_json::from_slice::<Value>(&payload) {
                        let c = size.get("cols").and_then(|v| v.as_u64()).unwrap_or(0) as u16;
                        let r = size.get("rows").and_then(|v| v.as_u64()).unwrap_or(0) as u16;
                        if c > 0 && r > 0 {
                            self.resize(c, r);
                        }
                    }
                }
            }
        }
        client.dead.store(true, Ordering::Relaxed);
        self.drop_client(&client);
        if !self.exited.load(Ordering::Relaxed) {
            self.write_info();
        }
    }

    fn drop_client(&self, client: &Arc<Client>) {
        self.clients.lock().unwrap().retain(|c| c.id != client.id);
    }
}

/// 仅用于 Windows 的本地端口鉴权（POSIX 走 0600 的 unix socket，不需要 token）。
///
/// 优先读系统随机源；拿不到时用纳秒时钟、pid 与一个堆地址混合出 SplitMix64 序列。
/// 它保护的是同机同用户下的 loopback 端口，不用于跨主机鉴权。
fn random_token() -> String {
    let mut bytes = [0u8; 24];
    let from_system = std::fs::File::open("/dev/urandom")
        .and_then(|mut f| std::io::Read::read_exact(&mut f, &mut bytes))
        .is_ok();
    if !from_system {
        let probe = Box::new(0u8);
        let mut state = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0)
            ^ (u64::from(std::process::id()) << 32)
            ^ (&*probe as *const u8 as u64);
        for chunk in bytes.chunks_mut(8) {
            state = state.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = state;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^= z >> 31;
            for (slot, byte) in chunk.iter_mut().zip(z.to_le_bytes()) {
                *slot = byte;
            }
        }
    }
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn backlog_with(chunks: &[&[u8]]) -> Backlog {
        let mut backlog = Backlog::default();
        for chunk in chunks {
            let piece = Piece::Data(chunk.to_vec());
            backlog.pending += piece.data_len();
            backlog.fed += piece.data_len() as u64;
            backlog.queue.push_back(piece);
        }
        backlog
    }

    #[test]
    fn backlog_over_the_limit_drops_oldest_instead_of_blocking() {
        let mut backlog = backlog_with(&[&[b'a'; 1024], &[b'b'; 1024], &[b'c'; 1024]]);
        assert_eq!(backlog.pending, 3072);
        backlog.trim(2048);
        assert!(backlog.pending <= 2048);
        assert_eq!(backlog.dropped, 1024);
        // 丢的是最旧的一段，最新的输出一定留下
        assert_eq!(backlog.queue.back(), Some(&Piece::Data(vec![b'c'; 1024])));
        assert_eq!(backlog.fed, 3072);
    }

    #[test]
    fn a_backlog_under_the_limit_is_left_alone() {
        let mut backlog = backlog_with(&[&[b'a'; 16]]);
        backlog.trim(BACKLOG_LIMIT);
        assert_eq!(backlog.dropped, 0);
        assert_eq!(backlog.pending, 16);
    }

    #[test]
    fn trimming_never_drops_a_pending_query() {
        // 丢掉查询就等于让应用永远等不到应答，宁可留着晚答。
        let mut backlog = backlog_with(&[&[b'a'; 1024]]);
        backlog.queue.push_back(Piece::CursorReport { dec: false });
        backlog.queue.push_back(Piece::Data(vec![b'b'; 1024]));
        backlog.pending += 1024;
        backlog.fed += 1024;
        backlog.trim(512);
        assert_eq!(backlog.dropped, 2048);
        assert_eq!(backlog.pending, 0);
        assert_eq!(backlog.queue.len(), 1);
        assert_eq!(backlog.queue.front(), Some(&Piece::CursorReport { dec: false }));
    }

    #[test]
    fn the_replay_covers_bytes_the_model_has_not_consumed_and_skips_queries() {
        let mut backlog = backlog_with(&[b"queued"]);
        backlog.inflight = Some(Piece::Data(b"inflight".to_vec()));
        backlog.queue.push_front(Piece::CursorReport { dec: false });
        // 在途块排在队列之前，查询不是显示内容所以不进回放
        assert_eq!(backlog.unapplied_bytes(), b"inflightqueued".to_vec());
    }

    #[test]
    fn inflight_bytes_still_count_as_pending() {
        let mut backlog = backlog_with(&[b"chunk"]);
        let piece = backlog.queue.pop_front().unwrap();
        backlog.inflight = Some(piece.clone());
        assert_eq!(backlog.pending, piece.data_len());
        backlog.inflight = None;
        backlog.pending -= piece.data_len();
        backlog.applied += piece.data_len() as u64;
        assert_eq!(backlog.pending, 0);
        assert_eq!(backlog.applied, backlog.fed);
    }
}
