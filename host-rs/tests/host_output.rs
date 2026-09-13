#![cfg(unix)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

// Fixed free shell fixture, private explicit host directory, no CLI home access.
struct Host {
    child: Child,
    directory: PathBuf,
}

impl Host {
    fn start() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("ptyhost-output-{}-{nonce}", std::process::id()));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .unwrap();
        let child = Command::new(env!("CARGO_BIN_EXE_ptyhost"))
            .env("PTYHOST_OUTPUT_TTY_FILE", directory.join("fixture.tty"))
            .arg("--dir").arg(&directory)
            .args(["run", "--name", "fixture", "--meta",
                r#"{"source":"codex","sid":"synthetic-sid","instance_id":"synthetic-instance"}"#,
                "--", "/bin/sh", "-c",
                "tty > \"$PTYHOST_OUTPUT_TTY_FILE\"; stty -echo; printf 'READY_MARKER\\n'; while IFS= read -r line; do if [ \"$line\" = EXIT ]; then printf 'FINAL_MARKER\\n'; exit 7; fi; printf '<%s>\\n' \"$line\"; done"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().unwrap();
        let mut host = Self { child, directory };
        let deadline = Instant::now() + Duration::from_secs(5);
        while !host.directory.join("fixture.json").exists() {
            assert!(
                host.child.try_wait().unwrap().is_none(),
                "host exited during setup"
            );
            assert!(
                Instant::now() < deadline,
                "host did not create its private record"
            );
            thread::sleep(Duration::from_millis(5));
        }
        loop {
            let capture = host.request(json!({"op":"capture", "styled":false}));
            if capture["text"].as_str().unwrap().contains("READY_MARKER") {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "synthetic shell did not become ready"
            );
        }
        host
    }

    fn connect(&self) -> UnixStream {
        let stream = UnixStream::connect(self.directory.join("fixture.sock")).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
    }

    fn hold_slave(&self) -> std::fs::File {
        let path = std::fs::read_to_string(self.directory.join("fixture.tty")).unwrap();
        std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(libc::O_NOCTTY)
            .open(path.trim())
            .unwrap()
    }

    fn request(&self, value: Value) -> Value {
        let mut stream = self.connect();
        writeln!(stream, "{value}").unwrap();
        read_json(&mut BufReader::new(stream))
    }

    fn attach(&self, guarded: bool, cols: u16) -> (Value, BufReader<UnixStream>) {
        let request = json!({"op":"attach", "replay":true, "cols":cols, "rows":24});
        let request = if guarded {
            json!({"op":"guarded_v1", "expected_instance_id":"synthetic-instance",
                "expected_source":"codex", "expected_sid":"synthetic-sid", "request":request})
        } else {
            request
        };
        let mut stream = self.connect();
        writeln!(stream, "{request}").unwrap();
        let mut reader = BufReader::new(stream);
        (read_json(&mut reader), reader)
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            if let Ok(mut stream) = UnixStream::connect(self.directory.join("fixture.sock")) {
                let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
                let _ = stream.write_all(b"{\"op\":\"kill\",\"force\":true}\n");
            }
            let deadline = Instant::now() + Duration::from_secs(6);
            while matches!(self.child.try_wait(), Ok(None)) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if matches!(self.child.try_wait(), Ok(None)) {
                // Keep ownership in the Child handle; a recorded PID may be reused.
                // Closing this host's PTY also releases the fixed childless shell.
                let _ = self.child.kill();
            }
            let _ = self.child.wait();
        }
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

fn read_json(reader: &mut BufReader<UnixStream>) -> Value {
    let mut line = String::new();
    reader.read_line(&mut line).unwrap();
    serde_json::from_str(&line).unwrap()
}

fn read_frame(reader: &mut BufReader<UnixStream>) -> (u8, Vec<u8>) {
    let mut header = [0; 5];
    reader.read_exact(&mut header).unwrap();
    let len = u32::from_be_bytes(header[1..].try_into().unwrap()) as usize;
    assert!(len < 8 << 20);
    let mut data = vec![0; len];
    reader.read_exact(&mut data).unwrap();
    (header[0], data)
}

#[test]
fn legacy_and_guarded_attach_preserve_replay_live_and_final_exit() {
    for guarded in [false, true] {
        let mut host = Host::start();
        let (ack, mut reader) = host.attach(guarded, 80);
        assert_eq!(ack["ok"], true);
        if guarded {
            assert_eq!(
                ack["instance_guard"],
                json!({"version":1,"instance_id":"synthetic-instance"})
            );
        } else {
            assert!(ack.get("instance_guard").is_none());
        }
        let (kind, replay) = read_frame(&mut reader);
        assert_eq!(kind, 1);
        assert_eq!(
            replay.windows(12).filter(|w| *w == b"READY_MARKER").count(),
            1
        );
        assert_eq!(
            host.request(json!({"op":"send","text":"ONE\nEXIT\n"}))["ok"],
            true
        );
        let mut live = Vec::new();
        loop {
            let (kind, bytes) = read_frame(&mut reader);
            if kind == 3 {
                assert_eq!(serde_json::from_slice::<Value>(&bytes).unwrap()["code"], 7);
                break;
            }
            assert_eq!(kind, 1);
            live.extend(bytes);
        }
        assert_eq!(
            String::from_utf8(live).unwrap(),
            "<ONE>\r\nFINAL_MARKER\r\n"
        );
        let mut tail = Vec::new();
        reader.read_to_end(&mut tail).unwrap();
        assert!(tail.is_empty());
        assert_eq!(host.child.wait().unwrap().code(), Some(7));
    }
}

#[test]
fn capacity_rejects_before_resize_and_keeps_existing_attachments_alive() {
    let host = Host::start();
    let mut readers = Vec::new();
    for _ in 0..32 {
        let (ack, mut reader) = host.attach(false, 80);
        assert_eq!(ack["ok"], true);
        assert_eq!(read_frame(&mut reader).0, 1);
        readers.push(reader);
    }
    let (ack, _) = host.attach(true, 123);
    assert_eq!(ack["ok"], false);
    assert_eq!(ack["code"], "attach_limit");
    assert_eq!(host.request(json!({"op":"info"}))["info"]["cols"], 80);
    assert_eq!(
        host.request(json!({"op":"send","text":"STILL_ALIVE\n"}))["ok"],
        true
    );
    for reader in &mut readers {
        let (kind, bytes) = read_frame(reader);
        assert_eq!(kind, 1);
        assert_eq!(bytes, b"<STILL_ALIVE>\r\n");
    }
}

fn exit_owned_shell(host: &Host, reader: &mut BufReader<UnixStream>) {
    let pid = host.request(json!({"op":"info"}))["info"]["pid"]
        .as_i64()
        .unwrap() as i32;
    host.request(json!({"op":"send","text":"EXIT\n"}));
    let mut output = Vec::new();
    while !output.windows(12).any(|w| w == b"FINAL_MARKER") {
        let (kind, bytes) = read_frame(reader);
        assert_eq!(kind, 1);
        output.extend(bytes);
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    while unsafe { libc::kill(pid, 0) } == 0 {
        assert!(Instant::now() < deadline, "owned shell was not reaped");
        thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn child_exit_does_not_discard_pty_tail_delayed_beyond_two_hundred_ms() {
    let mut host = Host::start();
    let mut slave = host.hold_slave();
    let (ack, mut reader) = host.attach(false, 80);
    assert_eq!(ack["ok"], true);
    read_frame(&mut reader);
    exit_owned_shell(&host, &mut reader);
    // This test-owned descriptor keeps the PTY alive, with no child descendants.
    thread::sleep(Duration::from_millis(600));
    assert!(
        host.child.try_wait().unwrap().is_none(),
        "host discarded a still-open PTY at child exit"
    );
    slave.write_all(b"DELAYED_TAIL\n").unwrap();
    drop(slave);
    let (kind, tail) = read_frame(&mut reader);
    assert_eq!(kind, 1);
    assert_eq!(tail, b"DELAYED_TAIL\r\n");
    let (kind, exit) = read_frame(&mut reader);
    assert_eq!(kind, 3);
    let exit: Value = serde_json::from_slice(&exit).unwrap();
    assert_eq!(exit["code"], 7);
    assert_eq!(exit["output_complete"], true);
    assert!(exit.get("reason").is_none());
}

#[test]
fn a_pty_that_never_reaches_eof_exits_with_an_explicit_incomplete_marker() {
    let host = Host::start();
    let slave = host.hold_slave();
    let (_, mut reader) = host.attach(false, 80);
    read_frame(&mut reader);
    exit_owned_shell(&host, &mut reader);
    let started = Instant::now();
    let (kind, exit) = read_frame(&mut reader);
    assert_eq!(kind, 3);
    let exit: Value = serde_json::from_slice(&exit).unwrap();
    assert_eq!(exit["code"], 7);
    assert_eq!(exit["output_complete"], false);
    assert_eq!(exit["reason"], "pty_drain_timeout");
    assert!(started.elapsed() >= Duration::from_secs(2));
    assert!(started.elapsed() < Duration::from_secs(5));
    drop(slave);
}
