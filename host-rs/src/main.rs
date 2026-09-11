//! agenthub 会话宿主（Rust 实现）。
//!
//! 命令行与协议和 Python 参考实现 `python3 -m agenthub.host` 完全一致：
//!
//!   agenthub-host [--dir DIR] run --name N [--cwd DIR] [--cols C] [--rows R]
//!                                 [--meta JSON] [--history N] -- CMD...
//!   agenthub-host [--dir DIR] list
//!   agenthub-host [--dir DIR] attach NAME
//!   agenthub-host [--dir DIR] kill NAME [--force]
//!   agenthub-host [--dir DIR] send NAME TEXT [--enter]
//!   agenthub-host [--dir DIR] capture NAME [--lines N] [--plain] [--join]

mod client;
mod dsr;
mod protocol;
mod screen;
mod session;
mod transport;

#[cfg(unix)]
use std::io::Write;
use std::path::PathBuf;

use serde_json::{json, Value};

struct Args {
    dir: Option<String>,
    cmd: String,
    name: Option<String>,
    cwd: Option<String>,
    cols: u16,
    rows: u16,
    meta: Option<String>,
    history: usize,
    text: Option<String>,
    lines: usize,
    plain: bool,
    join: bool,
    enter: bool,
    force: bool,
    command: Vec<String>,
}

fn usage() -> ! {
    eprintln!(
        "用法: agenthub-host [--dir DIR] <run|list|attach|kill|send|capture> ...\n\
         详见 docs/session-host.md"
    );
    std::process::exit(2);
}

fn parse() -> Args {
    let mut raw: Vec<String> = std::env::args().skip(1).collect();
    let mut args = Args {
        dir: None,
        cmd: String::new(),
        name: None,
        cwd: None,
        cols: 120,
        rows: 32,
        meta: None,
        history: 10000,
        text: None,
        lines: 0,
        plain: false,
        join: false,
        enter: false,
        force: false,
        command: Vec::new(),
    };
    let mut positional: Vec<String> = Vec::new();
    let mut i = 0usize;
    while i < raw.len() {
        let item = raw[i].clone();
        let mut value = |key: &str| -> String {
            i += 1;
            raw.get(i)
                .cloned()
                .unwrap_or_else(|| {
                    eprintln!("{key} 缺少取值");
                    std::process::exit(2)
                })
        };
        match item.as_str() {
            "--dir" => args.dir = Some(value("--dir")),
            "--name" => args.name = Some(value("--name")),
            "--cwd" => args.cwd = Some(value("--cwd")),
            "--cols" => args.cols = value("--cols").parse().unwrap_or(120),
            "--rows" => args.rows = value("--rows").parse().unwrap_or(32),
            "--meta" => args.meta = Some(value("--meta")),
            "--history" => args.history = value("--history").parse().unwrap_or(10000),
            "--lines" => args.lines = value("--lines").parse().unwrap_or(0),
            "--plain" => args.plain = true,
            "--join" => args.join = true,
            "--enter" => args.enter = true,
            "--force" => args.force = true,
            "-h" | "--help" => usage(),
            "--" => {
                args.command = raw.split_off(i + 1);
                break;
            }
            other if other.starts_with("--") => {
                eprintln!("未知参数: {other}");
                std::process::exit(2);
            }
            other => positional.push(other.to_string()),
        }
        i += 1;
    }
    let mut it = positional.into_iter();
    args.cmd = it.next().unwrap_or_else(|| usage());
    match args.cmd.as_str() {
        "attach" | "kill" | "capture" => args.name = args.name.or_else(|| it.next()),
        "send" => {
            args.name = args.name.or_else(|| it.next());
            args.text = it.next();
        }
        _ => {}
    }
    args
}

/// Windows 下把 DLL 搜索范围收紧到「可执行文件所在目录 + 系统目录」。
///
/// portable-pty 用 `LoadLibrary("conpty.dll")` 找 sideload 版的伪控制台实现，
/// 默认搜索顺序包含 PATH：机器上任何一个装了自带 conpty.dll 的终端（实测
/// WezTerm）都会被优先加载，于是宿主起的是那个终端的 OpenConsole.exe 而不是
/// 系统 conhost，行为随机器而变，kill 之后还会留下孤儿进程。
///
/// `LOAD_LIBRARY_SEARCH_DEFAULT_DIRS` 把 PATH 和当前目录移出搜索顺序，同时保留
/// 可执行文件所在目录——要固定某个版本，把 conpty.dll 放到 exe 旁边即可，
/// 这仍然是显式的部署决定，而不是碰巧在 PATH 上。
#[cfg(windows)]
fn pin_dll_search_path() {
    const LOAD_LIBRARY_SEARCH_DEFAULT_DIRS: u32 = 0x0000_1000;
    unsafe extern "system" {
        fn SetDefaultDllDirectories(flags: u32) -> i32;
    }
    // 失败不致命：只是退回默认搜索顺序，和打补丁前一样。
    unsafe {
        SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    }
}

#[cfg(not(windows))]
fn pin_dll_search_path() {}

fn main() {
    pin_dll_search_path();
    let args = parse();
    let dir = client::host_dir(args.dir.as_deref());
    let code = match args.cmd.as_str() {
        "run" => cmd_run(&args, dir),
        "list" => cmd_list(&dir),
        "kill" => cmd_simple(&dir, &args, "kill", json!({"force": args.force})),
        "send" => cmd_send(&dir, &args),
        "capture" => cmd_capture(&dir, &args),
        "attach" => cmd_attach(&dir, &args),
        other => {
            eprintln!("未知子命令: {other}");
            2
        }
    };
    std::process::exit(code);
}

fn cmd_run(args: &Args, dir: PathBuf) -> i32 {
    let name = match args.name.clone() {
        Some(name) => name,
        None => {
            eprintln!("run 需要 --name");
            return 2;
        }
    };
    if args.command.is_empty() {
        eprintln!("缺少命令");
        return 2;
    }
    let meta: Value = args
        .meta
        .as_deref()
        .and_then(|raw| serde_json::from_str(raw).ok())
        .unwrap_or_else(|| json!({}));
    match session::Session::spawn(
        name,
        args.command.clone(),
        args.cwd.clone(),
        args.cols,
        args.rows,
        meta,
        dir,
        args.history,
    ) {
        Ok(session) => session.serve(),
        Err(e) => {
            eprintln!("agenthub-host: {e}");
            1
        }
    }
}

fn cmd_list(dir: &PathBuf) -> i32 {
    for row in client::list_sessions(dir) {
        let text = |key: &str| row.get(key).and_then(|v| v.as_str()).unwrap_or("");
        let num = |key: &str| row.get(key).and_then(|v| v.as_u64()).unwrap_or(0);
        println!(
            "{}\tpid={}\t{}x{}\t{}\t{}",
            text("name"),
            num("pid"),
            num("cols"),
            num("rows"),
            if row.get("attached").and_then(|v| v.as_bool()) == Some(true) {
                "attached"
            } else {
                "detached"
            },
            text("cwd")
        );
    }
    0
}

fn cmd_simple(dir: &PathBuf, args: &Args, op: &str, extra: Value) -> i32 {
    let name = match args.name.as_deref() {
        Some(name) => name,
        None => {
            eprintln!("{op} 需要会话名");
            return 2;
        }
    };
    match client::request(dir, name, op, extra) {
        Ok(_) => 0,
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

fn cmd_send(dir: &PathBuf, args: &Args) -> i32 {
    let name = match args.name.as_deref() {
        Some(name) => name,
        None => return 2,
    };
    let text = args.text.clone().unwrap_or_default();
    let op = if args.enter { "paste" } else { "send" };
    if let Err(e) = client::request(dir, name, op, json!({"text": text})) {
        eprintln!("{e}");
        return 1;
    }
    if args.enter {
        if let Err(e) = client::request(dir, name, "keys", json!({"keys": ["Enter"]})) {
            eprintln!("{e}");
            return 1;
        }
    }
    0
}

fn cmd_capture(dir: &PathBuf, args: &Args) -> i32 {
    let name = match args.name.as_deref() {
        Some(name) => name,
        None => return 2,
    };
    let kind = if args.lines > 0 { "scrollback" } else { "screen" };
    let reply = client::request(
        dir,
        name,
        "capture",
        json!({
            "kind": kind, "lines": args.lines,
            "styled": !args.plain, "join": args.join,
        }),
    );
    match reply {
        Ok(reply) => {
            println!("{}", reply.get("text").and_then(|v| v.as_str()).unwrap_or(""));
            let cursor = reply.get("cursor").and_then(|v| v.as_array()).cloned();
            if let Some(cursor) = cursor {
                let at = |i: usize| cursor.get(i).and_then(|v| v.as_u64()).unwrap_or(0);
                eprintln!(
                    "-- cursor {},{}{}",
                    at(0),
                    at(1),
                    if reply.get("alt").and_then(|v| v.as_bool()) == Some(true) {
                        " alt"
                    } else {
                        ""
                    }
                );
            }
            0
        }
        Err(e) => {
            eprintln!("{e}");
            1
        }
    }
}

#[cfg(unix)]
fn cmd_attach(dir: &PathBuf, args: &Args) -> i32 {
    use std::io::Read;
    let name = match args.name.as_deref() {
        Some(name) => name,
        None => return 2,
    };
    let (cols, rows) = terminal_size().unwrap_or((120, 32));
    let mut attach = match client::Attach::open(dir, name, cols, rows, true) {
        Ok(attach) => attach,
        Err(e) => {
            eprintln!("{e}");
            return 1;
        }
    };
    let saved = match RawMode::enter() {
        Ok(saved) => saved,
        Err(e) => {
            eprintln!("无法进入 raw 模式: {e}");
            return 1;
        }
    };
    eprintln!("[agenthub] attached to {name}; 按 Ctrl-\\ 退出 (不影响会话)");
    let stdin_thread = {
        let mut writer = match attach.stream.try_clone() {
            Ok(s) => s,
            Err(_) => return 1,
        };
        std::thread::spawn(move || {
            let mut buf = [0u8; 4096];
            let mut stdin = std::io::stdin();
            loop {
                let n = match stdin.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => n,
                };
                if buf[..n].contains(&0x1c) {
                    break;
                }
                if writer
                    .write_all(&protocol::pack_frame(protocol::FRAME_DATA, &buf[..n]))
                    .and_then(|_| writer.flush())
                    .is_err()
                {
                    break;
                }
            }
        })
    };
    let mut out = std::io::stdout();
    while !attach.dead {
        match attach.read() {
            Ok(data) => {
                if !data.is_empty() {
                    let _ = out.write_all(&data);
                    let _ = out.flush();
                }
            }
            Err(_) => break,
        }
    }
    drop(saved);
    eprintln!("\r\n[agenthub] detached");
    let _ = stdin_thread;
    0
}

#[cfg(not(unix))]
fn cmd_attach(_dir: &PathBuf, _args: &Args) -> i32 {
    eprintln!("Windows 下暂不支持命令行 attach, 请用网页控制台");
    2
}

#[cfg(unix)]
fn terminal_size() -> Option<(u16, u16)> {
    let mut size: libc::winsize = unsafe { std::mem::zeroed() };
    let ok = unsafe { libc::ioctl(libc::STDOUT_FILENO, libc::TIOCGWINSZ, &mut size) };
    if ok == 0 && size.ws_col > 0 {
        Some((size.ws_col, size.ws_row))
    } else {
        None
    }
}

#[cfg(unix)]
struct RawMode(libc::termios);

#[cfg(unix)]
impl RawMode {
    fn enter() -> std::io::Result<Self> {
        let mut original: libc::termios = unsafe { std::mem::zeroed() };
        if unsafe { libc::tcgetattr(libc::STDIN_FILENO, &mut original) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        let mut raw = original;
        unsafe { libc::cfmakeraw(&mut raw) };
        if unsafe { libc::tcsetattr(libc::STDIN_FILENO, libc::TCSADRAIN, &raw) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(Self(original))
    }
}

#[cfg(unix)]
impl Drop for RawMode {
    fn drop(&mut self) {
        unsafe {
            libc::tcsetattr(libc::STDIN_FILENO, libc::TCSADRAIN, &self.0);
        }
    }
}
