#![cfg(unix)]

use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::{fs::DirBuilderExt, net::UnixStream};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn metadata() -> Value {
    json!({"source":"codex","instance_id":"synthetic-instance-0001","launch_id":"synthetic-launch-0001"})
}
fn launch(request: Value) -> Value {
    json!({"op":"launch_guard_v1","expected_instance_id":metadata()["instance_id"],
        "expected_source":"codex","expected_launch_id":metadata()["launch_id"],"request":request})
}
fn bind() -> Value {
    json!({"op":"launch_bind_v1","expected_instance_id":metadata()["instance_id"],
        "expected_source":"codex","expected_launch_id":metadata()["launch_id"],
        "native":{"sid":"true-native-id","uid":"codex:synthetic-native-uid"}})
}
fn native(request: Value) -> Value {
    json!({"op":"guarded_v1","expected_instance_id":metadata()["instance_id"],
        "expected_source":"codex","expected_sid":"true-native-id",
        "expected_uid":"codex:synthetic-native-uid","request":request})
}
fn binding() -> Value {
    json!({"version":1,"instance_id":metadata()["instance_id"],"source":"codex",
        "launch_id":metadata()["launch_id"],"sid":"true-native-id",
        "uid":"codex:synthetic-native-uid","method":"operator"})
}

struct Host {
    child: Child,
    directory: PathBuf,
    name: String,
}
impl Host {
    fn new(meta: Value) -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let directory =
            std::env::temp_dir().join(format!("ptyhost-native-{}-{nonce}", std::process::id()));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&directory)
            .unwrap();
        let child = Command::new(env!("CARGO_BIN_EXE_ptyhost"))
            .arg("--dir").arg(&directory).arg("--cwd").arg(&directory)
            .current_dir(&directory).env_clear().env("PATH", "/usr/bin:/bin")
            .env("TERM", "xterm-256color")
            .args(["run", "--name", "synthetic-native", "--meta", &meta.to_string(),
                "--", "/bin/sh", "-c", "stty -echo; printf 'NATIVE_READY\\n'; while IFS= read -r line; do printf '<%s>\\n' \"$line\"; done"])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).spawn().unwrap();
        let mut host = Self {
            child,
            directory,
            name: "synthetic-native".into(),
        };
        let deadline = Instant::now() + Duration::from_secs(5);
        while !host.directory.join("synthetic-native.json").is_file() {
            assert!(host.child.try_wait().unwrap().is_none());
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(5));
        }
        host.wait_capture("NATIVE_READY");
        host
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
    fn open(&self, request: Value) -> (Value, BufReader<UnixStream>) {
        let mut stream = self.connect();
        writeln!(stream, "{request}").unwrap();
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        (serde_json::from_str(&line).unwrap(), reader)
    }
    fn request(&self, request: Value) -> Value {
        self.open(request).0
    }
    fn wait_capture(&self, marker: &str) {
        let deadline = Instant::now() + Duration::from_secs(3);
        loop {
            let reply = self.request(json!({"op":"capture","styled":false}));
            if reply["text"].as_str().unwrap().contains(marker) {
                return;
            }
            assert!(Instant::now() < deadline);
            thread::sleep(Duration::from_millis(5));
        }
    }
    fn record(&self) -> Value {
        serde_json::from_slice(
            &std::fs::read(self.directory.join(format!("{}.json", self.name))).unwrap(),
        )
        .unwrap()
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

fn data(reader: &mut impl Read) -> Vec<u8> {
    let mut header = [0; 5];
    reader.read_exact(&mut header).unwrap();
    assert_eq!(header[0], 1);
    let len = u32::from_be_bytes(header[1..].try_into().unwrap()) as usize;
    assert!(len <= 8 << 20);
    let mut bytes = vec![0; len];
    reader.read_exact(&mut bytes).unwrap();
    bytes
}

#[test]
fn bind_publishes_only_root_state_and_keeps_pending_attachment_and_both_guards() {
    let mut host = Host::new(metadata());
    let initial = host.request(launch(json!({"op":"info"})));
    assert_eq!(initial["capabilities"]["launch_bind"], 1);
    assert_eq!(initial.get("native_binding"), Some(&Value::Null));
    assert_eq!(host.request(native(json!({"op":"info"})))["ok"], false);
    let (ack, mut attached) = host.open(launch(
        json!({"op":"attach","cols":80,"rows":24,"replay":true}),
    ));
    assert_eq!(ack["ok"], true);
    assert!(String::from_utf8_lossy(&data(&mut attached)).contains("NATIVE_READY"));
    let deadline = Instant::now() + Duration::from_secs(3);
    while host.record()["attached"] != true {
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(5));
    }
    let original = host.record();
    let ack = host.request(bind());
    assert_eq!(ack["ok"], true);
    assert_eq!(ack["launch_guard"], initial["launch_guard"]);
    assert_eq!(ack["native_binding"], binding());
    assert!(ack.get("instance_guard").is_none());
    assert_eq!(host.request(bind()), ack);
    assert_eq!(host.record(), original, "bind must not rewrite the record");
    for request in [
        json!({"op":"info"}),
        launch(json!({"op":"info"})),
        native(json!({"op":"info"})),
    ] {
        let reply = host.request(request);
        assert_eq!(reply["ok"], true);
        assert_eq!(reply["native_binding"], binding());
        assert_eq!(reply["info"]["meta"], metadata());
        assert!(reply["info"].get("native_binding").is_none());
    }
    let input = b"PENDING_STILL_WORKS\n";
    let mut frame = vec![1];
    frame.extend_from_slice(&(input.len() as u32).to_be_bytes());
    frame.extend_from_slice(input);
    attached.get_mut().write_all(&frame).unwrap();
    let mut received = Vec::new();
    while !String::from_utf8_lossy(&received).contains("<PENDING_STILL_WORKS>") {
        received.extend(data(&mut attached));
    }
    assert_eq!(
        host.request(native(json!({"op":"send","text":"NATIVE_WORKS\n"})))["ok"],
        true
    );
    host.wait_capture("<NATIVE_WORKS>");
    assert_eq!(
        host.request(launch(json!({"op":"rename","to":"synthetic-renamed"})))["ok"],
        true
    );
    host.name = "synthetic-renamed".into();
    let record = host.record();
    assert_eq!(record["meta"], metadata());
    assert!(record.get("native_binding").is_none());
    assert_eq!(
        host.request(launch(json!({"op":"info"})))["native_binding"],
        binding()
    );
}

#[test]
fn malformed_wrong_identity_and_old_entry_points_cannot_bind_or_change_the_pty() {
    let host = Host::new(metadata());
    let original = host.record();
    let mut requests = Vec::new();
    for key in [
        "expected_instance_id",
        "expected_source",
        "expected_launch_id",
    ] {
        let mut request = bind();
        request[key] = json!("private-wrong-identity");
        requests.push(request);
    }
    for key in [
        "native",
        "expected_instance_id",
        "expected_source",
        "expected_launch_id",
    ] {
        let mut request = bind();
        request.as_object_mut().unwrap().remove(key);
        requests.push(request);
    }
    let mut request = bind();
    request["request"] = json!({"op":"send","text":"FORBIDDEN\n"});
    requests.push(request);
    let mut request = bind();
    request["native"]["extra"] = json!("private-value");
    requests.push(request);
    for request in requests {
        assert_eq!(
            host.request(request),
            json!({"ok":false,"error":"native binding rejected","code":"native_binding_rejected"})
        );
    }
    for request in [
        launch(bind()),
        native(bind()),
        json!({"op":"bind","native":bind()["native"]}),
    ] {
        assert_eq!(host.request(request)["ok"], false);
    }
    // Raw legacy fields remain ignored; they never become a binding request.
    let reply = host.request(json!({"op":"info","native":bind()["native"]}));
    assert_eq!(reply["ok"], true);
    assert_eq!(reply.get("native_binding"), Some(&Value::Null));
    assert_eq!(host.record(), original);
    let capture = host.request(json!({"op":"capture","styled":false}));
    assert!(!capture["text"].as_str().unwrap().contains("FORBIDDEN"));

    let ack = host.request(bind());
    assert_eq!(ack["ok"], true);
    for key in ["sid", "uid"] {
        let mut conflict = bind();
        conflict["native"][key] = json!(if key == "uid" {
            "codex:another-uid"
        } else {
            "another-native-id"
        });
        assert_eq!(
            host.request(conflict),
            json!({"ok":false,"error":"native binding conflict","code":"native_binding_conflict"})
        );
        let mut guarded = native(json!({"op":"send","text":"FORBIDDEN\n"}));
        guarded
            .as_object_mut()
            .unwrap()
            .remove(&format!("expected_{key}"));
        assert_eq!(host.request(guarded)["ok"], false);
    }
    assert_eq!(
        host.request(launch(json!({"op":"info"})))["native_binding"],
        binding()
    );
    assert_eq!(host.record(), original);
}

#[test]
fn a_lost_bind_ack_is_observable_on_a_fresh_guarded_connection_without_record_mutation() {
    let host = Host::new(metadata());
    let original = host.record();
    let mut stream = host.connect();
    writeln!(stream, "{}", bind()).unwrap();
    // The caller disappears without reading an ACK. The host must publish
    // before its socket write and retain the one-time decision if it fails.
    drop(stream);
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        let reply = host.request(launch(json!({"op":"info"})));
        assert_eq!(reply["ok"], true);
        if reply["native_binding"] == binding() {
            break;
        }
        assert_eq!(reply.get("native_binding"), Some(&Value::Null));
        assert!(Instant::now() < deadline);
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(host.request(bind())["native_binding"], binding());
    assert_eq!(host.record(), original);
}

#[test]
fn simultaneous_socket_requests_have_one_native_identity_and_atomic_info_snapshots() {
    let host = Host::new(metadata());
    let barrier = std::sync::Barrier::new(8);
    let replies = thread::scope(|scope| {
        let tasks: Vec<_> = (0..8)
            .map(|index| {
                let (host, barrier) = (&host, &barrier);
                scope.spawn(move || {
                    let mut request = bind();
                    request["native"]["sid"] = json!(format!("native-{index}"));
                    request["native"]["uid"] = json!(format!("codex:uid-{index}"));
                    barrier.wait();
                    let reply = host.request(request);
                    let observed = host.request(launch(json!({"op":"info"})));
                    (reply, observed["native_binding"].clone())
                })
            })
            .collect();
        tasks
            .into_iter()
            .map(|task| task.join().unwrap())
            .collect::<Vec<_>>()
    });
    assert_eq!(
        replies
            .iter()
            .filter(|(reply, _)| reply["ok"] == true)
            .count(),
        1
    );
    let published = host.request(launch(json!({"op":"info"})))["native_binding"].clone();
    for (reply, observed) in replies {
        assert_eq!(observed, published);
        if reply["ok"] == true {
            assert_eq!(reply["native_binding"], published);
        } else {
            assert_eq!(reply["code"], "native_binding_conflict");
        }
    }
    assert_eq!(
        published["uid"],
        format!(
            "codex:uid-{}",
            published["sid"]
                .as_str()
                .unwrap()
                .strip_prefix("native-")
                .unwrap()
        )
    );
    assert_eq!(host.record()["meta"], metadata());
}

#[test]
fn existing_native_declarations_cannot_be_rebound_even_when_null_or_exact() {
    for value in [Value::Null, json!("true-native-id")] {
        let mut meta = metadata();
        meta["sid"] = value;
        let host = Host::new(meta.clone());
        assert_eq!(host.request(bind())["code"], "native_binding_rejected");
        assert_eq!(
            host.request(launch(json!({"op":"info"})))["native_binding"],
            Value::Null
        );
        assert_eq!(host.record()["meta"], meta);
    }
}
