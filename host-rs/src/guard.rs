//! Optional identity-checked envelope. An old host rejects this *operation*
//! before dispatch instead of ignoring an unfamiliar field on a legacy write.
use serde_json::{Value, json};

#[path = "native_binding.rs"]
pub mod binding;

pub const OP: &str = "guarded_v1";
pub const ERROR: &str = "instance guard rejected";
pub const LAUNCH_OP: &str = "launch_guard_v1";
pub const LAUNCH_ERROR: &str = "launch guard rejected";

struct LaunchIdentity<'a> {
    instance: &'a str,
    source: &'a str,
    launch: &'a str,
}

pub struct Prepared<'a> {
    pub request: &'a Value,
    pub instance: Option<&'a str>,
    launch: Option<LaunchIdentity<'a>>,
}

impl Prepared<'_> {
    pub fn acknowledge(&self, reply: &mut Value) {
        acknowledge(reply, self.instance);
        if let Some(identity) = &self.launch
            && reply["ok"] == true
        {
            reply["launch_guard"] = json!({"version":1,"instance_id":identity.instance,
                "source":identity.source,"launch_id":identity.launch});
        }
    }
}

fn identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.:-".contains(&byte))
}
fn fields(value: &Value, allowed: &[&str]) -> bool {
    value
        .as_object()
        .is_some_and(|object| object.keys().all(|key| allowed.contains(&key.as_str())))
}
fn optional_bool(value: &Value, key: &str) -> bool {
    value.get(key).is_none_or(Value::is_boolean)
}
fn dimension(value: &Value, key: &str) -> Option<u64> {
    value
        .get(key)?
        .as_u64()
        .filter(|number| (1..=1000).contains(number))
}
fn name(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 128
        || value.starts_with('.')
        || value.ends_with('.')
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
    {
        return false;
    }
    let stem = value.split('.').next().unwrap_or("").to_ascii_uppercase();
    !matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        && !(stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && matches!(stem.as_bytes()[3], b'1'..=b'9'))
}

fn operation(request: &Value) -> bool {
    let Some(op) = request["op"].as_str() else {
        return false;
    };
    match op {
        "info" | "cursor" => fields(request, &["op"]),
        "send" | "paste" => {
            fields(
                request,
                if op == "send" {
                    &["op", "text"]
                } else {
                    &["op", "text", "bracketed"]
                },
            ) && request["text"]
                .as_str()
                .is_some_and(|text| text.len() <= 1024 * 1024)
                && optional_bool(request, "bracketed")
        }
        "keys" => {
            fields(request, &["op", "keys"])
                && request["keys"].as_array().is_some_and(|keys| {
                    keys.len() <= 256
                        && keys.iter().all(|key| {
                            key.as_str()
                                .is_some_and(|key| !key.is_empty() && key.len() <= 256)
                        })
                })
        }
        "resize" | "attach" => {
            fields(
                request,
                if op == "resize" {
                    &["op", "cols", "rows"]
                } else {
                    &["op", "cols", "rows", "replay"]
                },
            ) && dimension(request, "cols")
                .zip(dimension(request, "rows"))
                .is_some_and(|(cols, rows)| cols * rows <= 250_000)
                && optional_bool(request, "replay")
        }
        "capture" => {
            fields(request, &["op", "kind", "styled", "join", "lines"])
                && request
                    .get("kind")
                    .is_none_or(|kind| matches!(kind.as_str(), Some("screen" | "scrollback")))
                && optional_bool(request, "styled")
                && optional_bool(request, "join")
                && request
                    .get("lines")
                    .is_none_or(|lines| lines.as_u64().is_some_and(|n| n <= 100_000))
        }
        "rename" => fields(request, &["op", "to"]) && request["to"].as_str().is_some_and(name),
        "kill" => fields(request, &["op", "force"]) && optional_bool(request, "force"),
        _ => false,
    }
}

/// Called only after ordinary socket/token authentication and before any
/// dispatch, resize, replay subscription, or PTY write. Session metadata is
/// immutable for the lifetime of the same Session used for the operation.
pub fn prepare<'a>(request: &'a Value, metadata: &Value) -> Result<Prepared<'a>, &'static str> {
    if request["op"] == LAUNCH_OP {
        return prepare_launch(request, metadata);
    }
    if request["op"] != OP {
        return Ok(Prepared {
            request,
            instance: None,
            launch: None,
        });
    }
    if !fields(
        request,
        &[
            "op",
            "token",
            "expected_instance_id",
            "expected_source",
            "expected_sid",
            "expected_uid",
            "request",
        ],
    ) || request.get("token").is_some_and(|token| !token.is_string())
    {
        return Err(ERROR);
    }
    let instance = request["expected_instance_id"].as_str().ok_or(ERROR)?;
    let source = request["expected_source"].as_str().ok_or(ERROR)?;
    if instance.len() < 16
        || !identifier(instance, 128)
        || !matches!(source, "claude" | "codex" | "grok")
        || metadata["instance_id"] != instance
        || metadata["source"] != source
    {
        return Err(ERROR);
    }
    let mut identities = 0;
    for (expected, key) in [("expected_sid", "sid"), ("expected_uid", "uid")] {
        if let Some(value) = request.get(expected) {
            let value = value.as_str().ok_or(ERROR)?;
            if !identifier(value, 256) || metadata[key] != value {
                return Err(ERROR);
            }
            if key == "uid"
                && (!value.starts_with(&format!("{source}:")) || value.len() <= source.len() + 1)
            {
                return Err(ERROR);
            }
            identities += 1;
        }
    }
    if identities == 0 || !operation(&request["request"]) {
        return Err(ERROR);
    }
    Ok(Prepared {
        request: &request["request"],
        instance: Some(instance),
        launch: None,
    })
}

/// A write-once binding is a separate native identity source. The immutable
/// launch metadata and legacy native declarations are never rewritten.
pub fn prepare_with_binding<'a>(
    request: &'a Value,
    metadata: &Value,
    binding: Option<&binding::NativeBinding>,
) -> Result<Prepared<'a>, &'static str> {
    if request["op"] == OP
        && let Some(binding) = binding
    {
        // Unlike legacy declarations, a new binding always declares both IDs.
        if !binding.matches_pending(metadata)
            || request["expected_sid"].as_str() != Some(binding.sid())
            || request["expected_uid"].as_str() != Some(binding.uid())
        {
            return Err(ERROR);
        }
        let mut effective = metadata.clone();
        effective["sid"] = json!(binding.sid());
        effective["uid"] = json!(binding.uid());
        return prepare(request, &effective);
    }
    prepare(request, metadata)
}

/// Launch identity deliberately needs no native SID/UID. It is an independent
/// immutable identity domain and never creates or upgrades native association.
fn prepare_launch<'a>(request: &'a Value, metadata: &Value) -> Result<Prepared<'a>, &'static str> {
    if !fields(request, &[
        "op", "token", "expected_instance_id", "expected_source", "expected_launch_id", "request",
    ]) || request.get("token").is_some_and(|token| !token.is_string()) {
        return Err(LAUNCH_ERROR);
    }
    let instance = request["expected_instance_id"].as_str().ok_or(LAUNCH_ERROR)?;
    let source = request["expected_source"].as_str().ok_or(LAUNCH_ERROR)?;
    let launch = request["expected_launch_id"].as_str().ok_or(LAUNCH_ERROR)?;
    if instance.len() < 16 || !identifier(instance, 128)
        || launch.len() < 16 || !identifier(launch, 128)
        || !matches!(source, "claude" | "codex" | "grok")
        || metadata["instance_id"] != instance
        || metadata["source"] != source
        || metadata["launch_id"] != launch
        || !operation(&request["request"])
    {
        return Err(LAUNCH_ERROR);
    }
    Ok(Prepared {
        request: &request["request"],
        instance: None,
        launch: Some(LaunchIdentity { instance, source, launch }),
    })
}

pub fn acknowledge(reply: &mut Value, instance: Option<&str>) {
    if let Some(instance) = instance
        && reply["ok"] == true
    {
        reply["instance_guard"] = json!({"version":1,"instance_id":instance});
    }
}

#[cfg(test)]
#[path = "launch_guard_tests.rs"]
mod launch_tests;

#[cfg(test)]
mod tests {
    use super::*;
    fn metadata() -> Value {
        json!({"source":"codex","sid":"full-native-id","uid":"codex:0123456789abcdef","instance_id":"synthetic-instance-0001"})
    }
    fn request(operation: Value) -> Value {
        json!({"op":OP,"token":"synthetic-private-token","expected_instance_id":"synthetic-instance-0001","expected_source":"codex","expected_sid":"full-native-id","expected_uid":"codex:0123456789abcdef","request":operation})
    }
    #[test]
    fn pending_launch_envelope_must_not_fall_through_to_legacy_dispatch() {
        let metadata = json!({"source":"codex", "instance_id":"synthetic-instance-0001", "launch_id":"synthetic-launch-0001"});
        let request = json!({"op":"launch_guard_v1", "expected_source":"codex",
            "expected_instance_id":"synthetic-instance-0001", "expected_launch_id":"synthetic-launch-0001",
            "request":{"op":"info"}});
        let prepared = prepare(&request, &metadata).unwrap();
        assert_eq!(prepared.request["op"], "info");
        assert!(prepared.instance.is_none());
    }

    #[test]
    fn wrong_launch_identity_must_fail_before_any_dispatch() {
        let metadata = json!({"source":"codex", "instance_id":"synthetic-instance-0001", "launch_id":"synthetic-launch-0001"});
        let request = json!({"op":"launch_guard_v1", "expected_source":"codex",
            "expected_instance_id":"synthetic-instance-0002", "expected_launch_id":"synthetic-launch-0001",
            "request":{"op":"send","text":"must not execute"}});
        assert!(prepare(&request, &metadata).is_err());
    }
    #[test]
    fn stale_instance_cannot_reach_dispatch_even_when_names_and_native_ids_match() {
        let req = request(json!({"op":"send","text":"must not execute"}));
        let mut replacement = metadata();
        replacement["instance_id"] = json!("synthetic-instance-0002");
        assert_eq!(prepare(&req, &replacement).err(), Some(ERROR));
        let prepared = prepare(&req, &metadata()).unwrap();
        assert_eq!(prepared.request["op"], "send");
        assert_eq!(prepared.instance, Some("synthetic-instance-0001"));
    }
    #[test]
    fn all_identity_fields_are_exact_and_errors_never_contain_credentials() {
        for key in [
            "expected_instance_id",
            "expected_source",
            "expected_sid",
            "expected_uid",
        ] {
            let mut req = request(json!({"op":"info"}));
            req[key] = json!("synthetic-private-token");
            assert_eq!(prepare(&req, &metadata()).err(), Some(ERROR));
            req[key] = Value::Null;
            assert!(prepare(&req, &metadata()).is_err());
        }
        let mut req = request(json!({"op":"info"}));
        req.as_object_mut().unwrap().remove("expected_uid");
        assert!(prepare(&req, &metadata()).is_ok());
        req.as_object_mut().unwrap().remove("expected_sid");
        assert!(prepare(&req, &metadata()).is_err());
    }
    #[test]
    fn unknown_fields_nested_guards_and_malformed_effects_fail_before_side_effects() {
        for inner in [
            json!({"op":OP,"request":{"op":"send","text":"x"}}),
            json!({"op":"send","text":null}),
            json!({"op":"send","text":"x","token":"nested"}),
            json!({"op":"resize","cols":0,"rows":20}),
            json!({"op":"attach","cols":1000,"rows":1000}),
            json!({"op":"keys","keys":[null]}),
            json!({"op":"rename","to":"../escape"}),
            json!({"op":"rename","to":"CON.txt"}),
            json!({"op":"kill","force":1}),
            json!({"op":"capture","lines":-1}),
            json!({"op":"cursor","unknown":"secret"}),
        ] {
            assert!(prepare(&request(inner), &metadata()).is_err());
        }
        let mut req = request(json!({"op":"info"}));
        req["fallback"] = json!(true);
        assert!(prepare(&req, &metadata()).is_err());
    }
    #[test]
    fn supported_operations_ack_the_exact_instance_and_legacy_operations_are_unchanged() {
        for inner in [
            json!({"op":"info"}),
            json!({"op":"cursor"}),
            json!({"op":"send","text":""}),
            json!({"op":"paste","text":"hello","bracketed":true}),
            json!({"op":"keys","keys":["Enter"]}),
            json!({"op":"resize","cols":120,"rows":40}),
            json!({"op":"attach","cols":120,"rows":40,"replay":true}),
            json!({"op":"capture","kind":"scrollback","lines":100}),
            json!({"op":"rename","to":"synthetic-new-name"}),
            json!({"op":"kill","force":false}),
        ] {
            let req = request(inner.clone());
            let prepared = prepare(&req, &metadata()).unwrap();
            let mut reply = json!({"ok":true,"cols":120});
            acknowledge(&mut reply, prepared.instance);
            assert_eq!(
                reply["instance_guard"],
                json!({"version":1,"instance_id":"synthetic-instance-0001"})
            );
            assert_eq!(reply["cols"], 120);
            let legacy = prepare(&inner, &Value::Null).unwrap();
            assert!(legacy.instance.is_none());
        }
        let mut reply = json!({"ok":false});
        acknowledge(&mut reply, Some("no-secret-reflection"));
        assert_eq!(reply, json!({"ok":false}));
    }
}
