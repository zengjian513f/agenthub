use super::*;

fn metadata() -> Value {
    json!({"source":"codex","instance_id":"synthetic-instance-0001","launch_id":"synthetic-launch-0001"})
}

fn envelope(inner: Value) -> Value {
    json!({"op":LAUNCH_OP,"token":"synthetic-private-token",
        "expected_instance_id":"synthetic-instance-0001","expected_source":"codex",
        "expected_launch_id":"synthetic-launch-0001","request":inner})
}

#[test]
fn every_validated_operation_accepts_pending_identity_and_has_only_a_launch_ack() {
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
        let request = envelope(inner.clone());
        let metadata = metadata();
        let original = metadata.clone();
        let prepared = prepare(&request, &metadata).unwrap();
        assert_eq!(prepared.request, &inner);
        assert!(prepared.instance.is_none());
        let mut reply = json!({"ok":true,"cols":120});
        prepared.acknowledge(&mut reply);
        assert_eq!(
            reply,
            json!({"ok":true,"cols":120,"launch_guard":{
            "version":1,"instance_id":"synthetic-instance-0001",
            "source":"codex","launch_id":"synthetic-launch-0001"}})
        );
        let mut failure = json!({"ok":false,"error":"operation failed"});
        prepared.acknowledge(&mut failure);
        assert_eq!(failure, json!({"ok":false,"error":"operation failed"}));
        assert_eq!(metadata, original);
    }
}

#[test]
fn source_and_both_launch_identifiers_are_required_and_exact() {
    for (expected, actual) in [
        ("expected_instance_id", "instance_id"),
        ("expected_source", "source"),
        ("expected_launch_id", "launch_id"),
    ] {
        for bad in [
            Value::Null,
            json!(false),
            json!(1),
            json!("wrong-identity-0001"),
        ] {
            let mut request = envelope(json!({"op":"info"}));
            request[expected] = bad.clone();
            assert_eq!(prepare(&request, &metadata()).err(), Some(LAUNCH_ERROR));
            let mut missing_metadata = metadata();
            missing_metadata[actual] = bad;
            assert_eq!(
                prepare(&envelope(json!({"op":"info"})), &missing_metadata).err(),
                Some(LAUNCH_ERROR)
            );
        }
        let mut request = envelope(json!({"op":"info"}));
        request.as_object_mut().unwrap().remove(expected);
        assert_eq!(prepare(&request, &metadata()).err(), Some(LAUNCH_ERROR));
        let mut missing_metadata = metadata();
        missing_metadata.as_object_mut().unwrap().remove(actual);
        assert_eq!(
            prepare(&envelope(json!({"op":"info"})), &missing_metadata).err(),
            Some(LAUNCH_ERROR)
        );
    }
}

#[test]
fn identifier_validation_applies_even_if_invalid_metadata_matches() {
    for (expected, actual) in [
        ("expected_instance_id", "instance_id"),
        ("expected_launch_id", "launch_id"),
    ] {
        for value in [
            "".into(),
            "x".repeat(15),
            "x".repeat(129),
            "invalid identity0001".into(),
            "invalid/identity0001".into(),
            "invalid\nidentity0001".into(),
            "标识符-identity0001".into(),
        ] {
            let mut request = envelope(json!({"op":"info"}));
            let mut metadata = metadata();
            request[expected] = json!(value);
            metadata[actual] = json!(value);
            assert_eq!(prepare(&request, &metadata).err(), Some(LAUNCH_ERROR));
        }
        for value in ["a".repeat(16), "A9_.:-".repeat(20), "Z".repeat(128)] {
            let mut request = envelope(json!({"op":"info"}));
            let mut metadata = metadata();
            request[expected] = json!(value);
            metadata[actual] = json!(value);
            assert!(prepare(&request, &metadata).is_ok());
        }
    }
    for source in ["claude", "codex", "grok", "Claude", "unknown", ""] {
        let mut request = envelope(json!({"op":"info"}));
        let mut metadata = metadata();
        request["expected_source"] = json!(source);
        metadata["source"] = json!(source);
        assert_eq!(
            prepare(&request, &metadata).is_ok(),
            matches!(source, "claude" | "codex" | "grok")
        );
    }
}

#[test]
fn nested_guards_extra_fields_and_malformed_effects_fail_closed() {
    for inner in [
        json!({"op":OP,"request":{"op":"send","text":"must not execute"}}),
        envelope(json!({"op":"send","text":"must not execute"})),
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
        json!({"op":"cancel"}),
    ] {
        assert_eq!(
            prepare(&envelope(inner), &metadata()).err(),
            Some(LAUNCH_ERROR)
        );
    }
    for key in [
        "expected_sid",
        "expected_uid",
        "instance_guard",
        "fallback",
        "launch_guard",
    ] {
        let mut request = envelope(json!({"op":"info"}));
        request[key] = json!("synthetic-private-token");
        assert_eq!(prepare(&request, &metadata()).err(), Some(LAUNCH_ERROR));
    }
    let mut request = envelope(json!({"op":"info"}));
    request["token"] = Value::Null;
    assert_eq!(prepare(&request, &metadata()).err(), Some(LAUNCH_ERROR));
    request.as_object_mut().unwrap().remove("token");
    assert!(prepare(&request, &metadata()).is_ok()); // Unix socket authentication is unchanged.
}

#[test]
fn launch_identity_does_not_upgrade_native_guard_or_change_legacy_dispatch() {
    let mut native = envelope(json!({"op":"info"}));
    native["op"] = json!(OP);
    assert_eq!(prepare(&native, &metadata()).err(), Some(ERROR));
    native.as_object_mut().unwrap().remove("expected_launch_id");
    assert_eq!(prepare(&native, &metadata()).err(), Some(ERROR));
    native["expected_sid"] = json!("invented-native-id");
    assert_eq!(prepare(&native, &metadata()).err(), Some(ERROR));

    let mut associated = metadata();
    associated["sid"] = json!("actual-native-id");
    native["expected_sid"] = json!("actual-native-id");
    let prepared = prepare(&native, &associated).unwrap();
    let mut reply = json!({"ok":true});
    prepared.acknowledge(&mut reply);
    assert!(reply.get("instance_guard").is_some());
    assert!(reply.get("launch_guard").is_none());
    native["request"] = envelope(json!({"op":"info"}));
    assert_eq!(prepare(&native, &associated).err(), Some(ERROR));

    let legacy = json!({"op":"send","text":"legacy","expected_launch_id":"ignored-as-before"});
    let prepared = prepare(&legacy, &Value::Null).unwrap();
    assert_eq!(prepared.request, &legacy);
    let mut reply = json!({"ok":true});
    prepared.acknowledge(&mut reply);
    assert_eq!(reply, json!({"ok":true}));
}
