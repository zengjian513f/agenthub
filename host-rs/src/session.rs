//! 单个托管会话的宿主进程，与 Python 参考实现同协议、同线程结构。
//!
//! 读线程只做两件事：把字节原样转发给已连接的客户端，并放进待喂队列。
//! 屏幕模型在独立线程里消费那个队列，只服务 capture/cursor 与 attach 回放，
//! 绝不挡住 pty → 浏览器这条路径；积压超过上限就丢最旧的一段并报出 dropped。

use std::collections::VecDeque;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use portable_pty::{CommandBuilder, MasterPty, PtySize};
use serde_json::{Value, json};

use crate::dsr::{self, Piece};
use crate::guard;
use crate::output::{Client, DRAIN_TIMEOUT};
use crate::protocol::{
    FRAME_DATA, FRAME_EXIT, FRAME_RESIZE, key_bytes, pack_frame, read_frames, recv_json, send_json,
};
use crate::screen::Screen;
use crate::transport::{Listener, Stream};

pub const BACKLOG_LIMIT: usize = 32 << 20;
pub const SCREEN_SYNC_TIMEOUT: Duration = Duration::from_secs(2);
/// After the owned child exits, descendants can retain a slave descriptor. Give
/// the reader time to drain, then report incompleteness instead of pretending EOF.
#[cfg(not(windows))]
const PTY_DRAIN_TIMEOUT: Duration = Duration::from_secs(3);
/// ConPTY never delivers EOF while this host holds the pseudo console, so the
/// drain always runs to its deadline and *is* the normal end of output there:
/// conhost has rendered the child's last writes well within this window, and
/// 3 s would turn every stop into "uncertain" for the service waiting on the
/// exit (WP-W).
#[cfg(windows)]
const PTY_DRAIN_TIMEOUT: Duration = Duration::from_millis(600);
/// What an elapsed drain means: incomplete output on a real PTY, the regular
/// exit on ConPTY.
const PTY_DRAIN_ELAPSED: Option<&str> = if cfg!(windows) {
    None
} else {
    Some("pty_drain_timeout")
};
const MAX_ATTACHMENTS: usize = 32;

struct AttachmentPermit<'a>(&'a AtomicUsize);

impl<'a> AttachmentPermit<'a> {
    fn acquire(slots: &'a AtomicUsize) -> Option<Self> {
        slots.fetch_update(Ordering::AcqRel, Ordering::Acquire, |n| {
            (n < MAX_ATTACHMENTS).then_some(n + 1)
        }).ok().map(|_| Self(slots))
    }
}

impl Drop for AttachmentPermit<'_> {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::AcqRel);
    }
}
/// 新会话必须清掉可能从 Web 服务继承的旧会话身份，否则 CLI 会接上错误的会话。
const STRIP_ENV: &[&str] = &[
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_COMPANION_SESSION_ID",
    "GROK_SESSION_ID",
    "TMUX",
];

/// 被 panic 标记失效的锁（PoisonError）照常用。持锁线程 panic 只说明它半途而废，
/// 数据本身还在；若在这里 `unwrap()`，一次屏幕模型的内部越界就会让 attach、capture、
/// 连 pty 读线程都跟着 panic，宿主变成"活着但永远连不上"（BUG-20260911-170830）。
fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// An instance guard identifies this host, not a still-live child PID. Keep the
/// same child lock across polling and signaling: another waiter must not reap
/// the child in between and make its PID available for reuse.
fn stop_owned_child(
    child: &Mutex<Box<dyn portable_pty::Child + Send + Sync>>,
    pid: u32,
    force: bool,
    hup: impl FnOnce(u32),
) {
    let mut child = lock(child);
    if !matches!(child.try_wait(), Ok(None)) {
        return;
    }
    if force || !cfg!(unix) {
        let _ = child.kill();
    } else if pid > 0 {
        hup(pid);
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

/// A completed model update includes retiring its inflight bytes. Both attach
/// snapshots and commits take screen before backlog; the reader only takes backlog.
fn apply_screen_piece(
    screen: &Mutex<Screen>,
    backlog: &Mutex<Backlog>,
    piece: &Piece,
    #[cfg(test)] after_feed: impl FnOnce(),
) -> Option<Vec<u8>> {
    let mut screen = lock(screen);
    let answer = match piece {
        Piece::Data(bytes) => {
            screen.feed(bytes);
            None
        }
        query => {
            let (col, row) = screen.cursor();
            dsr::reply(query, col, row)
        }
    };
    #[cfg(test)]
    after_feed();
    let mut backlog = lock(backlog);
    backlog.inflight = None;
    backlog.pending -= piece.data_len().min(backlog.pending);
    backlog.applied += piece.data_len() as u64;
    answer
}

fn with_replay_boundary<T>(
    screen: &Mutex<Screen>,
    backlog: &Mutex<Backlog>,
    action: impl FnOnce(&mut Screen, &Backlog) -> T,
) -> T {
    let mut screen = lock(screen);
    let backlog = lock(backlog);
    action(&mut screen, &backlog)
}

pub struct Session {
    pub name: Mutex<String>,
    argv: Vec<String>,
    cwd: Option<String>,
    meta: Value,
    native_binding: guard::binding::State,
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
    attachment_slots: AtomicUsize,
    writer: Mutex<Box<dyn Write + Send>>,
    master: Mutex<Box<dyn MasterPty + Send>>,
    child: Mutex<Box<dyn portable_pty::Child + Send + Sync>>,
    child_pid: u32,
    exited: AtomicBool,
    finishing: AtomicBool,
    reader_eof: AtomicBool,
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
            let _ = std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700));
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
            native_binding: guard::binding::State::default(),
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
            attachment_slots: AtomicUsize::new(0),
            writer: Mutex::new(writer),
            master: Mutex::new(pty.master),
            child: Mutex::new(child),
            child_pid,
            exited: AtomicBool::new(false),
            finishing: AtomicBool::new(false),
            reader_eof: AtomicBool::new(false),
            exit_code: AtomicI32::new(0),
            listener: Mutex::new(None),
            port: Mutex::new(0),
        });

        let listener = session.listen()?;
        *lock(&session.listener) = Some(listener.clone());
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
            *lock(&self.port) = port;
            Ok(Arc::new(listener))
        }
    }

    fn name_now(&self) -> String {
        lock(&self.name).clone()
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
        let (cols, rows) = *lock(&self.size);
        let attached = lock(&self.clients)
            .iter()
            .any(|c| !c.is_dead());
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
            "backend": "ptyhost",
        });
        let map = info.as_object_mut().unwrap();
        if cfg!(windows) {
            map.insert("port".into(), json!(*lock(&self.port)));
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
        let reason = loop {
            let n = match reader.read(&mut buf) {
                Ok(0) => {
                    self.reader_eof.store(true, Ordering::Release);
                    break None;
                }
                Err(_) => break Some("pty_read_error"),
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
                    [Piece::Data(bytes)] => bytes, // 常见情况：不额外拷贝
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
            {
                let mut backlog = lock(&self.backlog);
                if self.finishing.load(Ordering::Acquire) {
                    return;
                }
                for piece in pieces {
                    backlog.pending += piece.data_len();
                    backlog.fed += piece.data_len() as u64;
                    backlog.queue.push_back(piece);
                }
                backlog.trim(BACKLOG_LIMIT);
                self.backlog_cv.notify_all();
                // Admission stays within the same boundary as replay registration.
                // These sends only enqueue; no socket IO runs under backlog.
                if let Some(frame) = frame {
                    let frame: Arc<[u8]> = frame.into();
                    for client in self.live_clients() {
                        client.send(frame.clone());
                    }
                }
            }
        };
        self.finish(reason);
    }

    fn live_clients(&self) -> Vec<Arc<Client>> {
        lock(&self.clients)
            .iter()
            .filter(|c| !c.is_dead())
            .cloned()
            .collect()
    }

    fn screen_loop(self: Arc<Self>) {
        loop {
            let piece = {
                let mut backlog = lock(&self.backlog);
                while backlog.queue.is_empty() {
                    if self.exited.load(Ordering::Relaxed) {
                        return;
                    }
                    let (guard, _) = self
                        .backlog_cv
                        .wait_timeout(backlog, Duration::from_millis(200))
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    backlog = guard;
                }
                let piece = backlog.queue.pop_front().unwrap();
                // 出队后仍计入 pending：在途块既不在队列也没进模型，
                // attach 的回放必须把它算上，否则客户端会丢这一段。
                backlog.inflight = Some(piece.clone());
                piece
            };
            let answer = apply_screen_piece(
                &self.screen, &self.backlog, &piece,
                #[cfg(test)] || {},
            );
            self.backlog_cv.notify_all();
            if let Some(answer) = answer {
                self.write_pty(&answer);
            }
        }
    }

    /// 等模型追上已读入的字节；返回仍未应用的字节数（0 表示完全同步）。
    fn wait_applied(&self, timeout: Duration) -> usize {
        let deadline = Instant::now() + timeout;
        let mut backlog = lock(&self.backlog);
        while backlog.pending > 0 {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            let (guard, _) = self
                .backlog_cv
                .wait_timeout(backlog, left.min(Duration::from_millis(50)))
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            backlog = guard;
        }
        backlog.pending
    }

    fn finish(&self, reason: Option<&str>) {
        let reason = {
            // Stop publication/registration at the same boundary; neither a
            // late live frame nor a newly attached client may follow exit.
            let _backlog = lock(&self.backlog);
            if self.finishing.swap(true, Ordering::SeqCst) {
                return;
            }
            // EOF can win the race against the fallback after its deadline was
            // checked. All preceding publication is complete at this boundary.
            if self.reader_eof.load(Ordering::Acquire) { None } else { reason }
        };
        if let Some(reason) = reason {
            eprintln!("PTY output incomplete: {reason}");
        }
        // Drain the model for bytes already published; exited stays false while
        // it works. A timeout/read error cannot claim a complete PTY output tail.
        self.wait_applied(SCREEN_SYNC_TIMEOUT);

        let code = {
            let mut child = lock(&self.child);
            let deadline = Instant::now() + Duration::from_secs(3);
            loop {
                match child.try_wait() {
                    Ok(Some(status)) => break status.exit_code() as i32,
                    _ => {
                        if Instant::now() >= deadline {
                            let _ = child.kill();
                            break child.wait().map(|s| s.exit_code() as i32).unwrap_or(255);
                        }
                        std::thread::sleep(Duration::from_millis(50));
                    }
                }
            }
        };
        self.exit_code.store(code, Ordering::SeqCst);
        let clients: Vec<Arc<Client>> = std::mem::take(&mut *lock(&self.clients));
        let mut exit = json!({"code": code, "output_complete": reason.is_none()});
        if let Some(reason) = reason {
            exit["reason"] = json!(reason);
        }
        let frame: Arc<[u8]> = pack_frame(FRAME_EXIT, exit.to_string().as_bytes()).into();
        let deadline = Instant::now() + DRAIN_TIMEOUT;
        for client in &clients {
            client.finish(frame.clone(), deadline);
        }
        // All writers drain concurrently, with one total deadline. serve() must
        // not exit the process before their final data/exit frames are flushed.
        for client in clients {
            client.wait_closed(deadline);
        }
        self.exited.store(true, Ordering::SeqCst);
        self.backlog_cv.notify_all();
    }

    pub fn serve(&self) -> i32 {
        let mut drain_deadline = None;
        while !self.exited.load(Ordering::Relaxed) {
            std::thread::sleep(Duration::from_millis(100));
            // Child exit is not PTY EOF. Continue reading buffered/delayed output
            // without holding any reader/model locks while waiting for EOF.
            if !self.finishing.load(Ordering::Relaxed) {
                if let Some(deadline) = drain_deadline {
                    if Instant::now() >= deadline {
                        self.finish(PTY_DRAIN_ELAPSED);
                    }
                } else if matches!(lock(&self.child).try_wait(), Ok(Some(_))) {
                    drain_deadline = Some(Instant::now() + PTY_DRAIN_TIMEOUT);
                }
            }
        }
        self.cleanup();
        self.exit_code.load(Ordering::SeqCst)
    }

    pub fn stop(&self, force: bool) {
        stop_owned_child(&self.child, self.child_pid, force, |pid| {
            // 与 tmux kill-session 一致：先 HUP 整个前台进程组。交互式 shell
            // 会忽略 TERM 却响应 HUP；CLI 收到 HUP 也有机会存盘。
            #[cfg(unix)]
            unsafe {
                libc::killpg(pid as i32, libc::SIGHUP);
            }
            #[cfg(not(unix))]
            let _ = pid;
        });
    }

    // ------------------------------------------------------------------ 连接
    /// 每轮重新取当前 listener：改名会换掉它，旧 listener 被唤醒后自然退场。
    fn accept_loop(self: Arc<Self>) {
        while !self.exited.load(Ordering::Relaxed) {
            let listener = match lock(&self.listener).clone() {
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
                    let current = lock(&self.listener).clone();
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
        let _ = stream.set_write_timeout(Some(crate::output::WRITE_TIMEOUT));
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
        if req["op"] == guard::binding::OP {
            let reply = match guard::binding::prepare(&req, &self.meta)
                .and_then(|candidate| self.native_binding.bind(candidate, &self.child, &self.exited))
            {
                Ok(binding) => binding.acknowledge(),
                Err(error) => error.reply(),
            };
            let _ = send_json(&mut writer, &reply);
            return;
        }
        let binding = self.native_binding.snapshot();
        let prepared = match guard::prepare_with_binding(&req, &self.meta, binding.as_ref()) {
            Ok(prepared) => prepared,
            Err(error) => {
                let code = if req["op"] == guard::LAUNCH_OP {
                    "launch_guard_rejected"
                } else {
                    "instance_guard_rejected"
                };
                let _ = send_json(
                    &mut writer,
                    &json!({"ok":false,"error":error,"code":code}),
                );
                return;
            }
        };
        let req = prepared.request;
        let op = req
            .get("op")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        if op == "attach" {
            self.attach(reader, writer, buffer, prepared);
            return;
        }
        let mut reply = match self.dispatch(&op, req) {
            Ok(value) => value,
            Err(message) => json!({"ok": false, "error": message}),
        };
        prepared.acknowledge(&mut reply);
        let _ = send_json(&mut writer, &reply);
    }

    fn write_pty(&self, data: &[u8]) {
        let mut writer = lock(&self.writer);
        let _ = writer.write_all(data);
        let _ = writer.flush();
    }

    fn dispatch(&self, op: &str, req: &Value) -> Result<Value, String> {
        match op {
            "info" => Ok(json!({
                "ok": true, "info": self.info(), "exited": self.exited.load(Ordering::Relaxed),
                "capabilities": {"instance_guard":1,"launch_guard":1,"launch_bind":1},
                "native_binding": self.native_binding.snapshot().map(|binding| binding.value())
            })),
            "send" => {
                let text = req.get("text").and_then(|v| v.as_str()).unwrap_or("");
                self.write_pty(text.as_bytes());
                Ok(json!({"ok": true}))
            }
            "keys" => {
                let app_cursor = lock(&self.screen).app_cursor();
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
                let bracketed = lock(&self.screen).bracketed_paste();
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
                let dropped = lock(&self.backlog).dropped;
                let screen = lock(&self.screen);
                let (x, y) = screen.cursor();
                Ok(json!({
                    "ok": true, "x": x, "y": y,
                    "visible": screen.cursor_visible(), "alt": screen.alt(),
                    "lag": lag, "dropped": dropped, "resets": screen.resets()
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
        let dropped = lock(&self.backlog).dropped;
        let (cols, rows) = *lock(&self.size);
        let mut screen = lock(&self.screen);
        let text = if kind == "screen" {
            screen.screen_lines(styled, join)
        } else {
            screen.scrollback_lines(lines, styled, join)
        }
        .join("\n");
        let (x, y) = screen.cursor();
        Ok(json!({
            "ok": true, "text": text, "cursor": [x, y], "alt": screen.alt(),
            "cols": cols, "rows": rows, "lag": lag, "dropped": dropped,
            "resets": screen.resets()
        }))
    }

    fn resize(&self, cols: u16, rows: u16) {
        let (cols, rows) = (cols.max(1), rows.max(1));
        {
            let mut size = lock(&self.size);
            if *size == (cols, rows) {
                return;
            }
            *size = (cols, rows);
        }
        lock(&self.screen).resize(cols, rows);
        {
            let master = lock(&self.master);
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
            let fresh = Listener::bind_unix(&path).map_err(|e| format!("重建 socket 失败: {e}"))?;
            *lock(&self.listener) = Some(Arc::new(fresh));
        }
        *lock(&self.name) = new.to_string();
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

    fn attach(
        self: Arc<Self>,
        mut reader: Stream,
        mut writer: Stream,
        mut buffer: Vec<u8>,
        prepared: guard::Prepared<'_>,
    ) {
        let req = prepared.request;
        if self.finishing.load(Ordering::Acquire) {
            let _ = send_json(&mut writer, &json!({"ok": false, "error": "会话已结束"}));
            return;
        }
        // Reserve before resize, replay construction, registration or starting a
        // writer. Pending attaches count too; a full host never evicts a peer.
        let Some(_permit) = AttachmentPermit::acquire(&self.attachment_slots) else {
            let _ = send_json(&mut writer, &json!({
                "ok": false, "error": "attachment limit reached", "code": "attach_limit"
            }));
            return;
        };
        let (cur_cols, cur_rows) = *lock(&self.size);
        let cols = req
            .get("cols")
            .and_then(|v| v.as_u64())
            .unwrap_or(cur_cols as u64) as u16;
        let rows = req
            .get("rows")
            .and_then(|v| v.as_u64())
            .unwrap_or(cur_rows as u64) as u16;
        self.resize(cols, rows);

        let client = match Client::new(
            self.next_client.fetch_add(1, Ordering::Relaxed), writer,
        ) {
            Ok(client) => client,
            Err(_) => return,
        };
        // 回放必须和这个客户端之后收到的实时字节严格接续：模型可能还没消化完
        // 已读入的数据，所以回放 = 模型当前画面 + 尚未喂入的原始字节；客户端
        // 加入列表与快照在同一把 backlog 锁下完成，读线程分发不会插进中间。
        let (size_cols, size_rows) = *lock(&self.size);
        let mut acknowledgement = json!({"ok": true, "cols": size_cols, "rows": size_rows});
        prepared.acknowledge(&mut acknowledgement);
        let mut acknowledgement = acknowledgement.to_string().into_bytes();
        acknowledgement.push(b'\n');
        let registered = with_replay_boundary(&self.screen, &self.backlog, |screen, backlog| {
            let mut replay = Vec::new();
            if req.get("replay").and_then(|v| v.as_bool()).unwrap_or(true) {
                replay.extend_from_slice(&screen.replay_bytes(self.history));
                replay.extend_from_slice(&backlog.unapplied_bytes());
            }
            let replay = if replay.is_empty() { replay } else { pack_frame(FRAME_DATA, &replay) };
            if self.finishing.load(Ordering::Acquire)
                || !client.initialize(acknowledgement, replay)
            {
                false
            } else {
                lock(&self.clients).push(client.clone());
                true
            }
        });
        if !registered {
            client.disconnect();
            return;
        }
        self.write_info();
        let _ = reader.set_read_timeout(None);
        let mut chunk = vec![0u8; 65536];
        while !client.is_dead() && !self.exited.load(Ordering::Relaxed) {
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
        client.disconnect();
        self.drop_client(&client);
        if !self.exited.load(Ordering::Relaxed) {
            self.write_info();
        }
    }

    fn drop_client(&self, client: &Arc<Client>) {
        lock(&self.clients).retain(|c| c.id != client.id);
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
#[path = "stop_tests.rs"]
mod stop_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn attach_ack_must_precede_a_concurrently_published_live_frame() {
        let (socket, peer) = std::os::unix::net::UnixStream::pair().unwrap();
        let client = Client::new(1, Stream::Unix(socket)).unwrap();
        let mut ack = json!({"ok": true});
        guard::acknowledge(&mut ack, Some("test-instance"));
        let mut line = ack.to_string().into_bytes();
        line.push(b'\n');
        assert!(client.initialize(line, pack_frame(FRAME_DATA, b"REPLAY")));
        // Registration can now expose the client to the reader pump immediately.
        assert!(client.send(pack_frame(FRAME_DATA, b"LIVE").into()));
        client.finish(pack_frame(FRAME_EXIT, b"{\"code\":0}").into(), Instant::now() + DRAIN_TIMEOUT);
        let mut peer = Stream::Unix(peer);
        peer.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let mut buffer = Vec::new();
        assert_eq!(recv_json(&mut peer, &mut buffer).unwrap(), ack);
        peer.read_to_end(&mut buffer).unwrap();
        assert_eq!(read_frames(&mut buffer), vec![
            (FRAME_DATA, b"REPLAY".to_vec()),
            (FRAME_DATA, b"LIVE".to_vec()),
            (FRAME_EXIT, b"{\"code\":0}".to_vec()),
        ]);
    }

    #[cfg(unix)]
    #[test]
    fn a_full_client_socket_must_not_block_the_publisher() {
        let (mut socket, peer) = std::os::unix::net::UnixStream::pair().unwrap();
        socket.set_nonblocking(true).unwrap();
        loop {
            match socket.write(&[0; 65536]) {
                Ok(_) => (),
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("{e}"),
            }
        }
        socket.set_nonblocking(false).unwrap();
        let client = Client::new(1, Stream::Unix(socket)).unwrap();
        let cleanup = client.clone();
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        let publisher = std::thread::spawn(move || {
            client.send(pack_frame(FRAME_DATA, b"LIVE").into());
            done_tx.send(()).unwrap();
        });
        let nonblocking = done_rx.recv_timeout(Duration::from_millis(100)).is_ok();
        peer.shutdown(std::net::Shutdown::Both).unwrap();
        publisher.join().unwrap();
        cleanup.disconnect();
        assert!(nonblocking, "a slow socket blocked the PTY publisher");
    }

    #[test]
    fn attachment_capacity_includes_pending_work_and_is_reclaimed() {
        let slots = AtomicUsize::new(0);
        let mut permits: Vec<_> = (0..MAX_ATTACHMENTS)
            .map(|_| AttachmentPermit::acquire(&slots).unwrap()).collect();
        assert!(AttachmentPermit::acquire(&slots).is_none());
        permits.pop();
        assert!(AttachmentPermit::acquire(&slots).is_some());
        drop(permits);
        assert_eq!(slots.load(Ordering::Acquire), 0);
    }

    #[test]
    fn a_poisoned_lock_is_still_usable() {
        let shared = Arc::new(Mutex::new(vec![1]));
        let poisoner = shared.clone();
        let _ = std::thread::spawn(move || {
            let _guard = lock(&poisoner);
            panic!("模拟屏幕模型越界");
        })
        .join();
        assert!(shared.is_poisoned());
        lock(&shared).push(2);
        assert_eq!(*lock(&shared), vec![1, 2]);
    }

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
        assert_eq!(
            backlog.queue.front(),
            Some(&Piece::CursorReport { dec: false })
        );
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

    #[test]
    fn replay_contains_each_inflight_piece_once_before_and_after_model_commit() {
        let screen = Mutex::new(Screen::new(80, 24, 100));
        let piece = Piece::Data(b"UNIQUE_MARKER".to_vec());
        let mut state = backlog_with(&[b"UNIQUE_MARKER", b"QUEUED_TAIL"]);
        state.inflight = state.queue.pop_front();
        let backlog = Mutex::new(state);
        let snapshot = || with_replay_boundary(&screen, &backlog, |screen, backlog| {
            let mut replay = screen.replay_bytes(100);
            replay.extend_from_slice(&backlog.unapplied_bytes());
            replay
        });
        let before = snapshot();
        apply_screen_piece(&screen, &backlog, &piece, || {});
        let after = snapshot();
        for replay in [before, after] {
            assert_eq!(replay.windows(b"UNIQUE_MARKER".len())
                .filter(|w| *w == b"UNIQUE_MARKER").count(), 1);
            assert!(replay.ends_with(b"QUEUED_TAIL"));
        }
        let backlog = lock(&backlog);
        assert!(backlog.inflight.is_none());
        assert_eq!(backlog.pending, b"QUEUED_TAIL".len());
        assert_eq!(backlog.applied, b"UNIQUE_MARKER".len() as u64);
    }

    #[test]
    fn a_snapshot_cannot_enter_between_screen_feed_and_inflight_retirement() {
        let screen = Arc::new(Mutex::new(Screen::new(80, 24, 100)));
        let mut state = backlog_with(&[b"EXACTLY_ONCE"]);
        let piece = state.queue.pop_front().unwrap();
        state.inflight = Some(piece.clone());
        let backlog = Arc::new(Mutex::new(state));
        let (fed_tx, fed_rx) = std::sync::mpsc::channel();
        let (resume_tx, resume_rx) = std::sync::mpsc::channel();
        let worker_screen = screen.clone();
        let worker_backlog = backlog.clone();
        let worker = std::thread::spawn(move || {
            apply_screen_piece(&worker_screen, &worker_backlog, &piece, || {
                fed_tx.send(()).unwrap();
                resume_rx.recv().unwrap();
            });
        });
        fed_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        // This is the precise old duplicate window: data is already in the
        // screen and still inflight. The screen guard must remain unavailable.
        let snapshot_blocked = screen.try_lock().is_err();
        assert!(lock(&backlog).inflight.is_some());
        resume_tx.send(()).unwrap();
        worker.join().unwrap();
        assert!(snapshot_blocked, "snapshot could duplicate an already fed piece");
        let replay = with_replay_boundary(&screen, &backlog, |screen, backlog| {
            let mut replay = screen.replay_bytes(100);
            replay.extend_from_slice(&backlog.unapplied_bytes());
            replay
        });
        assert_eq!(replay.windows(12).filter(|w| *w == b"EXACTLY_ONCE").count(), 1);
    }
}
