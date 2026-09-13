#![cfg(unix)]

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::DirBuilderExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

fn metadata() -> Value {
    json!({"source":"codex","instance_id":"synthetic-instance-0001","launch_id":"synthetic-launch-0001"})
}

fn envelope(metadata: &Value, request: Value) -> Value {
    json!({"op":"launch_guard_v1","expected_instance_id":metadata["instance_id"],
        "expected_source":metadata["source"],"expected_launch_id":metadata["launch_id"],"request":request})
}

struct Host {
    child: Child,
    directory: PathBuf,
    name: String,
    metadata: Value,
}

impl Host {
    fn new(metadata: Value) -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("ptyhost-launch-{}-{nonce}", std::process::id()));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .unwrap();
        let name = "synthetic-launch".to_string();
        let child = Self::spawn(&directory, &name, &metadata);
        let mut host = Self {
            child,
            directory,
            name,
            metadata,
        };
        host.ready();
        host
    }

    fn spawn(directory: &Path, name: &str, metadata: &Value) -> Child {
        Command::new(env!("CARGO_BIN_EXE_ptyhost"))
            .arg("--dir").arg(directory)
            .arg("--cwd").arg(directory)
            .current_dir(directory)
            .env_clear().env("PATH", "/usr/bin:/bin").env("TERM", "xterm-256color")
            .args(["run", "--name", name, "--meta", &metadata.to_string(), "--cols", "80", "--rows", "24",
                "--", "/bin/sh", "-c", "stty -echo; printf 'LAUNCH_READY\\n'; while IFS= read -r line; do printf '<%s>\\n' \"$line\"; done"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .spawn().unwrap()
    }

    fn ready(&mut self) {
        let deadline = Instant::now() + Duration::from_secs(5);
        while !self.directory.join(format!("{}.json", self.name)).is_file() {
            assert!(self.child.try_wait().unwrap().is_none());
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(5));
        }
        self.wait_capture("LAUNCH_READY");
    }

    fn connect(&self) -> UnixStream {
        let stream =
            UnixStream::connect(self.directory.join(format!("{}.sock", self.name))).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(3)))
            .unwrap();
        stream
            .set_write_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        stream
    }

    fn open(&self, value: Value) -> (Value, BufReader<UnixStream>) {
        let mut stream = self.connect();
        writeln!(stream, "{value}").unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        (serde_json::from_str(&line).unwrap(), reader)
    }

    fn request(&self, value: Value) -> Value {
        self.open(value).0
    }

    fn guarded(&self, inner: Value) -> Value {
        self.request(envelope(&self.metadata, inner))
    }

    fn wait_capture(&self, marker: &str) -> Value {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let capture = self.request(json!({"op":"capture","styled":false}));
            if capture["text"].as_str().unwrap().contains(marker) {
                return capture;
            }
            assert!(Instant::now() < deadline, "synthetic output not captured");
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn assert_ack(&self, reply: &Value) {
        assert_eq!(reply["ok"], true);
        assert_eq!(
            reply["launch_guard"],
            json!({"version":1,
            "instance_id":self.metadata["instance_id"],"source":self.metadata["source"],"launch_id":self.metadata["launch_id"]})
        );
        assert!(reply.get("instance_guard").is_none());
    }

    fn cancel(&mut self) {
        self.assert_ack(&self.guarded(json!({"op":"kill","force":true})));
        let deadline = Instant::now() + Duration::from_secs(5);
        while self.child.try_wait().unwrap().is_none() {
            assert!(
                Instant::now() < deadline,
                "owned synthetic host did not exit"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn restart(&mut self, metadata: Value) {
        self.cancel();
        self.child = Self::spawn(&self.directory, &self.name, &metadata);
        self.metadata = metadata;
        self.ready();
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        if matches!(self.child.try_wait(), Ok(None)) {
            if let Ok(mut stream) =
                UnixStream::connect(self.directory.join(format!("{}.sock", self.name)))
            {
                let _ = stream.set_write_timeout(Some(Duration::from_secs(1)));
                let _ = stream.write_all(b"{\"op\":\"kill\",\"force\":true}\n");
            }
            let deadline = Instant::now() + Duration::from_secs(6);
            while matches!(self.child.try_wait(), Ok(None)) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if matches!(self.child.try_wait(), Ok(None)) {
                let _ = self.child.kill();
            }
            let _ = self.child.wait();
        }
        let _ = std::fs::remove_dir_all(&self.directory);
    }
}

fn rejected(reply: Value) {
    assert_eq!(
        reply,
        json!({"ok":false,"code":"launch_guard_rejected","error":"launch guard rejected"})
    );
}

#[test]
fn pending_launch_info_attach_send_rename_and_cancel_use_only_launch_identity() {
    let mut host = Host::new(metadata());
    let legacy = host.request(json!({"op":"info"}));
    assert_eq!(
        legacy["capabilities"],
        json!({"instance_guard":1,"launch_guard":1,"launch_bind":1})
    );
    assert!(legacy.get("launch_guard").is_none());
    assert!(legacy["info"]["meta"].get("sid").is_none());
    assert!(legacy["info"]["meta"].get("uid").is_none());
    let info = host.guarded(json!({"op":"info"}));
    host.assert_ack(&info);
    assert_eq!(info["info"]["meta"], host.metadata);

    for field in [
        "expected_instance_id",
        "expected_launch_id",
        "expected_source",
    ] {
        for inner in [
            json!({"op":"send","text":"FORBIDDEN\n"}),
            json!({"op":"attach","cols":99,"rows":30,"replay":true}),
            json!({"op":"rename","to":"forbidden-name"}),
            json!({"op":"kill","force":true}),
        ] {
            let mut bad = envelope(&host.metadata, inner);
            bad[field] = json!(if field == "expected_source" {
                "claude"
            } else {
                "synthetic-wrong-0001"
            });
            rejected(host.request(bad));
        }
    }
    let unchanged = host.request(json!({"op":"info"}));
    assert_eq!(unchanged["info"]["cols"], 80);
    assert_eq!(unchanged["info"]["rows"], 24);
    assert_eq!(unchanged["info"]["attached"], false);
    assert_eq!(unchanged["info"]["name"], host.name);
    assert!(!host.directory.join("forbidden-name.json").exists());

    host.assert_ack(&host.guarded(json!({"op":"send","text":"ALLOWED\n"})));
    let captured = host.wait_capture("<ALLOWED>");
    assert!(!captured["text"].as_str().unwrap().contains("FORBIDDEN"));
    let (ack, mut attached) = host.open(envelope(
        &host.metadata,
        json!({"op":"attach","cols":100,"rows":40,"replay":true}),
    ));
    host.assert_ack(&ack);
    assert_eq!(
        (ack["cols"].as_u64(), ack["rows"].as_u64()),
        (Some(100), Some(40))
    );
    let mut header = [0; 5];
    attached.read_exact(&mut header).unwrap();
    assert_eq!(header[0], 1);
    let length = u32::from_be_bytes(header[1..].try_into().unwrap()) as usize;
    assert!(length < 8 << 20);
    let mut replay = vec![0; length];
    attached.read_exact(&mut replay).unwrap();
    assert!(replay.windows(9).any(|w| w == b"<ALLOWED>"));

    host.assert_ack(&host.guarded(json!({"op":"rename","to":"synthetic-renamed"})));
    host.name = "synthetic-renamed".into();
    let renamed = host.guarded(json!({"op":"info"}));
    host.assert_ack(&renamed);
    assert_eq!(renamed["info"]["meta"], host.metadata);
    assert_eq!(renamed["info"]["name"], "synthetic-renamed");
    drop(attached);
    host.cancel();
}

#[test]
fn same_name_replacement_rejects_both_stale_instance_and_stale_launch() {
    let mut host = Host::new(metadata());
    let stale = envelope(&host.metadata, json!({"op":"send","text":"FORBIDDEN\n"}));
    let mut replacement = metadata();
    replacement["instance_id"] = json!("synthetic-instance-0002");
    host.restart(replacement);
    rejected(host.request(stale));
    let stale_launch = envelope(&host.metadata, json!({"op":"kill","force":true}));
    let mut replacement = host.metadata.clone();
    replacement["launch_id"] = json!("synthetic-launch-0002");
    host.restart(replacement);
    rejected(host.request(stale_launch));
    host.assert_ack(&host.guarded(json!({"op":"send","text":"NEW_INSTANCE\n"})));
    let captured = host.wait_capture("<NEW_INSTANCE>");
    assert!(!captured["text"].as_str().unwrap().contains("FORBIDDEN"));
    host.cancel();
}

#[test]
fn missing_metadata_native_envelopes_and_nested_protocols_cannot_authorize_pending_input() {
    for missing in ["instance_id", "source", "launch_id"] {
        let mut partial = metadata();
        partial.as_object_mut().unwrap().remove(missing);
        let host = Host::new(partial);
        rejected(host.request(envelope(
            &metadata(),
            json!({"op":"send","text":"FORBIDDEN\n"}),
        )));
        assert_eq!(host.request(json!({"op":"info"}))["exited"], false);
    }
    let mut host = Host::new(metadata());
    let native = json!({"op":"guarded_v1","expected_source":"codex",
        "expected_instance_id":host.metadata["instance_id"],"expected_sid":"invented-native-id",
        "request":{"op":"send","text":"FORBIDDEN\n"}});
    assert_eq!(
        host.request(native.clone())["code"],
        "instance_guard_rejected"
    );
    rejected(host.request(envelope(&host.metadata, native)));
    rejected(host.request(envelope(
        &host.metadata,
        envelope(&host.metadata, json!({"op":"kill","force":true})),
    )));
    let mut extra_native = envelope(&host.metadata, json!({"op":"kill","force":true}));
    extra_native["expected_sid"] = json!("invented-native-id");
    rejected(host.request(extra_native));
    let unknown =
        host.request(json!({"op":"launch_guard_v0","request":{"op":"kill","force":true}}));
    assert_eq!(unknown["ok"], false);
    assert_eq!(host.request(json!({"op":"info"}))["exited"], false);
    host.assert_ack(&host.guarded(json!({"op":"send","text":"STILL_PENDING\n"})));
    assert!(
        !host.wait_capture("<STILL_PENDING>")["text"]
            .as_str()
            .unwrap()
            .contains("FORBIDDEN")
    );
    host.cancel();
}
