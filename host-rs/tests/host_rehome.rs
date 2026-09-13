#![cfg(unix)]
//! `rehome`: a live session moves its endpoint files into another host
//! directory without touching the PTY, the child or attached clients.

use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

struct Host {
    child: Child,
    old: PathBuf,
    new: PathBuf,
}

fn private_dir(label: &str) -> PathBuf {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = std::env::temp_dir().join(format!("ptyhost-{label}-{}-{nonce}", std::process::id()));
    std::fs::DirBuilder::new().mode(0o700).create(&directory).unwrap();
    directory
}

impl Host {
    fn start() -> Self {
        let old = private_dir("rehome-old");
        let new = private_dir("rehome-new");
        let child = Command::new(env!("CARGO_BIN_EXE_ptyhost"))
            .arg("--dir").arg(&old)
            .args(["run", "--name", "fixture", "--", "/bin/sh", "-c",
                "stty -echo; printf 'READY_MARKER\\n'; while IFS= read -r line; do printf '<%s>\\n' \"$line\"; done"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().unwrap();
        let host = Self { child, old, new };
        let deadline = Instant::now() + Duration::from_secs(5);
        while !host.old.join("fixture.sock").exists() {
            assert!(Instant::now() < deadline, "host did not publish its socket");
            thread::sleep(Duration::from_millis(10));
        }
        host.wait_capture(&host.old, "READY_MARKER");
        host
    }

    fn request_at(&self, dir: &std::path::Path, value: Value) -> Value {
        let mut stream = UnixStream::connect(dir.join("fixture.sock")).unwrap();
        writeln!(stream, "{value}").unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        serde_json::from_str(&line).unwrap()
    }

    fn wait_capture(&self, dir: &std::path::Path, marker: &str) -> Value {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let capture = self.request_at(dir, json!({"op":"capture","styled":false}));
            if capture["text"].as_str().unwrap_or("").contains(marker) {
                return capture;
            }
            assert!(Instant::now() < deadline, "synthetic output not captured");
            thread::sleep(Duration::from_millis(5));
        }
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        for dir in [&self.new, &self.old] {
            if let Ok(mut stream) = UnixStream::connect(dir.join("fixture.sock")) {
                let _ = writeln!(stream, "{}", json!({"op":"kill","force":true}));
            }
        }
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.old);
        let _ = std::fs::remove_dir_all(&self.new);
    }
}

#[test]
fn rehome_moves_record_and_socket_and_keeps_the_session_and_attached_clients() {
    let host = Host::start();
    // An attached client from before the move keeps receiving output.
    let mut attached = UnixStream::connect(host.old.join("fixture.sock")).unwrap();
    writeln!(attached, "{}", json!({"op":"attach","cols":80,"rows":24,"replay":false})).unwrap();
    let mut attached_reader = BufReader::new(attached.try_clone().unwrap());
    let mut ack = String::new();
    attached_reader.read_line(&mut ack).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&ack).unwrap()["ok"], true);

    let before = host.request_at(&host.old, json!({"op":"info"}));
    let pid = before["info"]["pid"].as_u64().unwrap();

    // Refusals: relative, missing, occupied.
    assert_eq!(host.request_at(&host.old, json!({"op":"rehome","dir":"relative"}))["ok"], false);
    assert_eq!(host.request_at(&host.old, json!({"op":"rehome","dir":"/nonexistent/ptyhost-rehome"}))["ok"], false);
    std::fs::write(host.new.join("fixture.json"), b"{}").unwrap();
    assert_eq!(host.request_at(&host.old, json!({"op":"rehome","dir":host.new.to_string_lossy()}))["ok"], false);
    std::fs::remove_file(host.new.join("fixture.json")).unwrap();

    let reply = host.request_at(&host.old, json!({"op":"rehome","dir":host.new.to_string_lossy()}));
    assert_eq!(reply["ok"], true, "{reply}");
    assert_eq!(reply["name"], "fixture");
    assert_eq!(reply["sock"].as_str().unwrap(), host.new.join("fixture.sock").to_string_lossy());

    // Old files are gone, new ones serve the same session.
    assert!(!host.old.join("fixture.json").exists());
    assert!(!host.old.join("fixture.sock").exists());
    let record: Value = serde_json::from_slice(&std::fs::read(host.new.join("fixture.json")).unwrap()).unwrap();
    assert_eq!(record["pid"].as_u64().unwrap(), pid);
    assert_eq!(record["sock"].as_str().unwrap(), host.new.join("fixture.sock").to_string_lossy());
    let after = host.request_at(&host.new, json!({"op":"info"}));
    assert_eq!(after["info"]["pid"].as_u64().unwrap(), pid);
    assert_eq!(after["info"]["name"], "fixture");

    // The shell is still the same process: input through the new socket, output to the old client.
    assert_eq!(host.request_at(&host.new, json!({"op":"send","text":"AFTER_MOVE\n"}))["ok"], true);
    host.wait_capture(&host.new, "<AFTER_MOVE>");
    let mut seen = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(3);
    attached.set_read_timeout(Some(Duration::from_millis(200))).unwrap();
    while Instant::now() < deadline {
        let mut chunk = [0u8; 4096];
        match std::io::Read::read(&mut attached_reader, &mut chunk) {
            Ok(0) => break,
            Ok(n) => seen.extend_from_slice(&chunk[..n]),
            Err(_) => {}
        }
        if seen.windows(12).any(|w| w == b"<AFTER_MOVE>") {
            break;
        }
    }
    assert!(seen.windows(12).any(|w| w == b"<AFTER_MOVE>"), "attached client lost output after rehome");

    // Idempotent for the current directory; a later rename works in the new directory.
    assert_eq!(host.request_at(&host.new, json!({"op":"rehome","dir":host.new.to_string_lossy()}))["ok"], true);
    assert_eq!(host.request_at(&host.new, json!({"op":"rename","to":"fixture2"}))["ok"], true);
    assert!(host.new.join("fixture2.json").exists() && !host.old.join("fixture2.json").exists());
    let _ = std::fs::rename(host.new.join("fixture2.sock"), host.new.join("fixture.sock"));
}
