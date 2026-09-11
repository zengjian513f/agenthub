//! 宿主会话的客户端：扫描会话目录、发控制请求、建立 attach 流。
//! 与 Python 参考实现共用同一套文件布局和协议。

use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{json, Value};

use crate::protocol::{read_frames, recv_json, send_json, FRAME_DATA, FRAME_EXIT};
use crate::transport::Stream;

pub fn host_dir(explicit: Option<&str>) -> PathBuf {
    if let Some(dir) = explicit {
        return PathBuf::from(dir);
    }
    if let Some(dir) = std::env::var_os("AGENTHUB_HOST_DIR") {
        return PathBuf::from(dir);
    }
    home().join(".local").join("share").join("agenthub").join("host")
}

fn home() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

pub fn info_path(dir: &Path, name: &str) -> PathBuf {
    dir.join(format!("{name}.json"))
}

pub fn read_info(path: &Path) -> Option<Value> {
    let raw = std::fs::read(path).ok()?;
    let value: Value = serde_json::from_slice(&raw).ok()?;
    if value.get("name").and_then(|v| v.as_str()).is_some() {
        Some(value)
    } else {
        None
    }
}

fn host_gone(info: &Value) -> bool {
    let pid = info.get("host_pid").and_then(|v| v.as_i64()).unwrap_or(0);
    if pid <= 0 {
        return true;
    }
    #[cfg(unix)]
    {
        // 僵尸也算结束：它的 /proc 条目要等父进程收尸才消失。
        if let Ok(stat) = std::fs::read_to_string(format!("/proc/{pid}/stat")) {
            if let Some(rest) = stat.rfind(')').map(|at| &stat[at + 2..]) {
                return rest.split_whitespace().next() == Some("Z");
            }
            return false;
        }
        true
    }
    #[cfg(not(unix))]
    {
        false
    }
}

fn remove_session_files(dir: &Path, name: &str) {
    let _ = std::fs::remove_file(info_path(dir, name));
    let _ = std::fs::remove_file(dir.join(format!("{name}.sock")));
}

pub fn list_sessions(dir: &Path) -> Vec<Value> {
    let mut rows = Vec::new();
    let mut entries: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(iter) => iter
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().map(|e| e == "json").unwrap_or(false))
            .collect(),
        Err(_) => return rows,
    };
    entries.sort();
    for path in entries {
        let stem = match path.file_stem().map(|s| s.to_string_lossy().into_owned()) {
            Some(stem) => stem,
            None => continue,
        };
        let info = match read_info(&path) {
            Some(info) => info,
            None => continue,
        };
        if info.get("name").and_then(|v| v.as_str()) != Some(stem.as_str()) {
            continue;
        }
        if host_gone(&info) {
            remove_session_files(dir, &stem);
            continue;
        }
        rows.push(public_row(&info));
    }
    rows
}

pub fn public_row(info: &Value) -> Value {
    let get_u64 = |key: &str, default: u64| info.get(key).and_then(|v| v.as_u64()).unwrap_or(default);
    let get_str = |key: &str| {
        info.get(key)
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string()
    };
    json!({
        "name": get_str("name"),
        "created": get_u64("created", 0),
        "attached": info.get("attached").and_then(|v| v.as_bool()).unwrap_or(false),
        "pid": get_u64("pid", 0),
        "cwd": get_str("cwd"),
        "cmd": get_str("cmd"),
        "cols": get_u64("cols", 80),
        "rows": get_u64("rows", 24),
        "owned": true,
        "server": "host",
        "backend": info.get("backend").and_then(|v| v.as_str()).unwrap_or("host"),
        "host_pid": get_u64("host_pid", 0),
        "meta": info.get("meta").cloned().unwrap_or(json!({})),
    })
}

pub fn session_info(dir: &Path, name: &str) -> Option<Value> {
    let info = read_info(&info_path(dir, name))?;
    if info.get("name").and_then(|v| v.as_str()) != Some(name) {
        return None;
    }
    if host_gone(&info) {
        remove_session_files(dir, name);
        return None;
    }
    Some(info)
}

pub fn connect(info: &Value) -> io::Result<Stream> {
    if let Some(port) = info.get("port").and_then(|v| v.as_u64()) {
        if cfg!(windows) || info.get("sock").is_none() {
            return Stream::connect_tcp(port as u16);
        }
    }
    #[cfg(unix)]
    {
        let sock = info
            .get("sock")
            .and_then(|v| v.as_str())
            .ok_or_else(|| io::Error::other("会话信息缺少 socket 路径"))?;
        return Stream::connect_unix(Path::new(sock));
    }
    #[cfg(not(unix))]
    Err(io::Error::other("会话信息缺少端口"))
}

pub fn request(dir: &Path, name: &str, op: &str, extra: Value) -> Result<Value, String> {
    let info = session_info(dir, name).ok_or_else(|| format!("会话不存在: {name}"))?;
    let mut body = json!({
        "op": op,
        "token": info.get("token").and_then(|v| v.as_str()).unwrap_or(""),
    });
    if let Some(map) = extra.as_object() {
        for (key, value) in map {
            body.as_object_mut().unwrap().insert(key.clone(), value.clone());
        }
    }
    let mut stream = connect(&info).map_err(|e| format!("宿主请求失败 ({op}): {e}"))?;
    let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
    send_json(&mut stream, &body).map_err(|e| format!("宿主请求失败 ({op}): {e}"))?;
    let mut buffer = Vec::new();
    let reply = recv_json(&mut stream, &mut buffer)
        .map_err(|e| format!("宿主请求失败 ({op}): {e}"))?;
    if reply.get("ok").and_then(|v| v.as_bool()) != Some(true) {
        return Err(reply
            .get("error")
            .and_then(|v| v.as_str())
            .unwrap_or(&format!("宿主拒绝 {op}"))
            .to_string());
    }
    Ok(reply)
}

/// 帧模式连接，供命令行 attach 使用（Windows 下没有命令行 attach，故是死代码）。
#[cfg_attr(not(unix), allow(dead_code))]
pub struct Attach {
    pub stream: Stream,
    buffer: Vec<u8>,
    pub exit_code: Option<i32>,
    pub dead: bool,
}

#[cfg_attr(not(unix), allow(dead_code))]
impl Attach {
    pub fn open(dir: &Path, name: &str, cols: u16, rows: u16, replay: bool) -> Result<Self, String> {
        let info = session_info(dir, name).ok_or_else(|| format!("会话不存在: {name}"))?;
        let mut stream = connect(&info).map_err(|e| format!("attach 失败: {e}"))?;
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        send_json(
            &mut stream,
            &json!({
                "op": "attach",
                "token": info.get("token").and_then(|v| v.as_str()).unwrap_or(""),
                "cols": cols, "rows": rows, "replay": replay,
            }),
        )
        .map_err(|e| format!("attach 失败: {e}"))?;
        let mut buffer = Vec::new();
        let reply = recv_json(&mut stream, &mut buffer).map_err(|e| format!("attach 失败: {e}"))?;
        if reply.get("ok").and_then(|v| v.as_bool()) != Some(true) {
            return Err(reply
                .get("error")
                .and_then(|v| v.as_str())
                .unwrap_or("attach 被拒绝")
                .to_string());
        }
        let _ = stream.set_read_timeout(None);
        Ok(Self { stream, buffer, exit_code: None, dead: false })
    }

    /// 读出一批数据帧；返回空表示这次没有新数据。
    pub fn read(&mut self) -> io::Result<Vec<u8>> {
        let mut out = Vec::new();
        for (kind, payload) in read_frames(&mut self.buffer) {
            self.absorb(kind, payload, &mut out);
        }
        if !out.is_empty() || self.dead {
            return Ok(out);
        }
        let mut chunk = vec![0u8; 65536];
        let n = self.stream.read(&mut chunk)?;
        if n == 0 {
            self.dead = true;
            return Ok(out);
        }
        self.buffer.extend_from_slice(&chunk[..n]);
        for (kind, payload) in read_frames(&mut self.buffer) {
            self.absorb(kind, payload, &mut out);
        }
        Ok(out)
    }

    fn absorb(&mut self, kind: u8, payload: Vec<u8>, out: &mut Vec<u8>) {
        if kind == FRAME_DATA {
            out.extend_from_slice(&payload);
        } else if kind == FRAME_EXIT {
            self.exit_code = serde_json::from_slice::<Value>(&payload)
                .ok()
                .and_then(|v| v.get("code").and_then(|c| c.as_i64()))
                .map(|c| c as i32)
                .or(Some(0));
            self.dead = true;
        }
    }

}
