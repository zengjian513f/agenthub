use super::*;
use portable_pty::{ChildKiller, ExitStatus};
use std::sync::{Arc, Barrier, atomic::AtomicUsize};

fn metadata() -> Value {
    json!({"source":"codex","instance_id":"synthetic-instance-0001","launch_id":"synthetic-launch-0001"})
}
fn request() -> Value {
    json!({"op":OP,"expected_instance_id":"synthetic-instance-0001","expected_source":"codex",
        "expected_launch_id":"synthetic-launch-0001","native":{"sid":"native-id","uid":"codex:full-uid"}})
}
fn candidate() -> NativeBinding {
    prepare(&request(), &metadata()).unwrap()
}

#[derive(Clone, Copy, Debug)]
enum Observation {
    Live,
    Reaped,
    Failed,
}
#[derive(Clone, Debug)]
struct FakeChild {
    observation: Observation,
    polls: Arc<AtomicUsize>,
}
impl Child for FakeChild {
    fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        self.polls.fetch_add(1, Ordering::SeqCst);
        match self.observation {
            Observation::Live => Ok(None),
            Observation::Reaped => Ok(Some(ExitStatus::with_exit_code(0))),
            Observation::Failed => Err(std::io::ErrorKind::Other.into()),
        }
    }
    fn wait(&mut self) -> std::io::Result<ExitStatus> {
        panic!("binding must never wait")
    }
    fn process_id(&self) -> Option<u32> {
        Some(42)
    }
    #[cfg(windows)]
    fn as_raw_handle(&self) -> Option<std::os::windows::io::RawHandle> {
        None
    }
}
impl ChildKiller for FakeChild {
    fn kill(&mut self) -> std::io::Result<()> {
        panic!("binding must never signal")
    }
    fn clone_killer(&self) -> Box<dyn ChildKiller + Send + Sync> {
        Box::new(self.clone())
    }
}
fn child(observation: Observation) -> (Mutex<Box<dyn Child + Send + Sync>>, Arc<AtomicUsize>) {
    let polls = Arc::new(AtomicUsize::new(0));
    (
        Mutex::new(Box::new(FakeChild {
            observation,
            polls: polls.clone(),
        })),
        polls,
    )
}

#[test]
fn strict_identity_native_fields_and_no_predeclared_metadata() {
    let valid = candidate();
    assert_eq!(valid.sid(), "native-id");
    assert_eq!(valid.value()["method"], "operator");
    for key in [
        "expected_instance_id",
        "expected_source",
        "expected_launch_id",
        "native",
    ] {
        for bad in [Value::Null, json!(false), json!("wrong"), json!({})] {
            let mut req = request();
            req[key] = bad;
            assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
        }
        let mut req = request();
        req.as_object_mut().unwrap().remove(key);
        assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
    }
    for key in ["sid", "uid"] {
        for value in [
            Value::Null,
            json!(""),
            json!("bad space"),
            json!("x".repeat(257)),
            json!(2),
        ] {
            let mut req = request();
            req["native"][key] = value;
            assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
        }
        let mut req = request();
        req["native"].as_object_mut().unwrap().remove(key);
        assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
        for value in [Value::Null, json!("existing-native-id")] {
            let mut meta = metadata();
            meta[key] = value;
            assert_eq!(prepare(&request(), &meta), Err(Error::Rejected));
        }
    }
    for uid in ["codex:", "claude:full-uid"] {
        let mut req = request();
        req["native"]["uid"] = json!(uid);
        assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
    }
    for key in ["instance_id", "source", "launch_id"] {
        let mut meta = metadata();
        meta.as_object_mut().unwrap().remove(key);
        assert_eq!(prepare(&request(), &meta), Err(Error::Rejected));
    }
    for key in ["method", "request", "expected_uid", "extra"] {
        let mut req = request();
        req[key] = json!("private-value-must-not-leak");
        let error = prepare(&req, &metadata()).unwrap_err();
        assert_eq!(error, Error::Rejected);
        assert!(!error.reply().to_string().contains("private-value"));
    }
    let mut req = request();
    req["native"]["method"] = json!("operator");
    assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
    let mut req = request();
    req["token"] = Value::Null;
    assert_eq!(prepare(&req, &metadata()), Err(Error::Rejected));
}

#[test]
fn first_bind_requires_live_owned_child_but_exact_replay_does_not_reprobe() {
    for observation in [Observation::Reaped, Observation::Failed] {
        let (child, polls) = child(observation);
        let state = State::default();
        assert_eq!(
            state.bind(candidate(), &child, &AtomicBool::new(false)),
            Err(Error::Rejected)
        );
        assert!(state.snapshot().is_none());
        assert_eq!(polls.load(Ordering::SeqCst), 1);
    }
    let (child, polls) = child(Observation::Live);
    let state = State::default();
    assert_eq!(
        state.bind(candidate(), &child, &AtomicBool::new(true)),
        Err(Error::Rejected)
    );
    assert_eq!(polls.load(Ordering::SeqCst), 0);
    assert_eq!(
        state.bind(candidate(), &child, &AtomicBool::new(false)),
        Ok(candidate())
    );
    assert_eq!(
        state.bind(candidate(), &child, &AtomicBool::new(true)),
        Ok(candidate())
    );
    assert_eq!(polls.load(Ordering::SeqCst), 1);
    let mut changed = candidate();
    changed.sid = "different".into();
    assert_eq!(
        state.bind(changed, &child, &AtomicBool::new(true)),
        Err(Error::Conflict)
    );
    assert_eq!(state.snapshot(), Some(candidate()));
}

#[test]
fn simultaneous_first_binds_publish_one_complete_value_and_only_poll_once() {
    let state = Arc::new(State::default());
    let (child, polls) = child(Observation::Live);
    let child = Arc::new(child);
    let barrier = Arc::new(Barrier::new(16));
    let tasks: Vec<_> = (0..16)
        .map(|index| {
            let (state, child, barrier) = (state.clone(), child.clone(), barrier.clone());
            std::thread::spawn(move || {
                let mut candidate = candidate();
                candidate.sid = format!("native-{index}");
                candidate.uid = format!("codex:uid-{index}");
                barrier.wait();
                let result = state.bind(candidate.clone(), &child, &AtomicBool::new(false));
                let observed = state.snapshot().unwrap();
                assert_eq!(
                    observed.uid,
                    format!(
                        "codex:uid-{}",
                        observed.sid.strip_prefix("native-").unwrap()
                    )
                );
                if result.is_ok() {
                    assert_eq!(observed, candidate);
                }
                result
            })
        })
        .collect();
    let results: Vec<_> = tasks.into_iter().map(|task| task.join().unwrap()).collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| **result == Err(Error::Conflict))
            .count(),
        15
    );
    assert_eq!(polls.load(Ordering::SeqCst), 1);
}

#[test]
fn new_native_guard_requires_both_ids_without_changing_legacy_or_launch_guard() {
    let binding = candidate();
    let mut req = json!({"op":"guarded_v1","expected_instance_id":metadata()["instance_id"],
        "expected_source":"codex","expected_sid":binding.sid,"expected_uid":binding.uid,"request":{"op":"info"}});
    assert!(super::super::prepare(&req, &metadata()).is_err());
    let mut ack = json!({"ok":true});
    super::super::prepare_with_binding(&req, &metadata(), Some(&binding))
        .unwrap()
        .acknowledge(&mut ack);
    assert_eq!(
        ack["instance_guard"]["instance_id"],
        metadata()["instance_id"]
    );
    assert!(ack.get("launch_guard").is_none());
    req.as_object_mut().unwrap().remove("expected_uid");
    assert!(super::super::prepare_with_binding(&req, &metadata(), Some(&binding)).is_err());
    let mut legacy = metadata();
    legacy["sid"] = json!(binding.sid);
    assert!(super::super::prepare_with_binding(&req, &legacy, None).is_ok());
    let launch = json!({"op":"launch_guard_v1","expected_instance_id":metadata()["instance_id"],
        "expected_source":"codex","expected_launch_id":metadata()["launch_id"],"request":{"op":"info"}});
    assert!(super::super::prepare_with_binding(&launch, &metadata(), Some(&binding)).is_ok());
    for outer in ["guarded_v1", "launch_guard_v1"] {
        let mut nested = if outer == "guarded_v1" {
            req.clone()
        } else {
            launch.clone()
        };
        if outer == "guarded_v1" {
            nested["expected_uid"] = json!(binding.uid);
        }
        nested["request"] = request();
        assert!(super::super::prepare_with_binding(&nested, &metadata(), Some(&binding)).is_err());
    }
    assert_eq!(metadata().get("sid"), None);
}
