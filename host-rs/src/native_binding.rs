//! One operator-declared association for the lifetime of one running host.
//! This module does not read native files, persist metadata or infer provenance.

use portable_pty::Child;
use serde_json::{Value, json};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

pub const OP: &str = "launch_bind_v1";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NativeBinding {
    instance: String,
    source: String,
    launch: String,
    sid: String,
    uid: String,
}
impl NativeBinding {
    pub fn sid(&self) -> &str {
        &self.sid
    }
    pub fn uid(&self) -> &str {
        &self.uid
    }
    pub fn value(&self) -> Value {
        json!({"version":1,"instance_id":self.instance,"source":self.source,
            "launch_id":self.launch,"sid":self.sid,"uid":self.uid,"method":"operator"})
    }
    pub fn acknowledge(&self) -> Value {
        json!({"ok":true,"launch_guard":{"version":1,"instance_id":self.instance,
            "source":self.source,"launch_id":self.launch},"native_binding":self.value()})
    }
    pub(super) fn matches_pending(&self, metadata: &Value) -> bool {
        metadata.is_object()
            && metadata.get("sid").is_none()
            && metadata.get("uid").is_none()
            && metadata["instance_id"] == self.instance
            && metadata["source"] == self.source
            && metadata["launch_id"] == self.launch
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Error {
    Rejected,
    Conflict,
}
impl Error {
    pub fn reply(self) -> Value {
        match self {
            Self::Rejected => {
                json!({"ok":false,"error":"native binding rejected","code":"native_binding_rejected"})
            }
            Self::Conflict => {
                json!({"ok":false,"error":"native binding conflict","code":"native_binding_conflict"})
            }
        }
    }
}

/// Parse only this independent outer operation after ordinary transport auth.
pub fn prepare(request: &Value, metadata: &Value) -> Result<NativeBinding, Error> {
    let rejected = Error::Rejected;
    if request["op"] != OP
        || !super::fields(
            request,
            &[
                "op",
                "token",
                "expected_instance_id",
                "expected_source",
                "expected_launch_id",
                "native",
            ],
        )
        || request.get("token").is_some_and(|token| !token.is_string())
        || !super::fields(&request["native"], &["sid", "uid"])
    {
        return Err(rejected);
    }
    let text = |value: &Value, minimum, maximum| {
        value
            .as_str()
            .filter(|s| s.len() >= minimum && super::identifier(s, maximum))
            .map(str::to_owned)
            .ok_or(rejected)
    };
    let candidate = NativeBinding {
        instance: text(&request["expected_instance_id"], 16, 128)?,
        source: text(&request["expected_source"], 1, 16)?,
        launch: text(&request["expected_launch_id"], 16, 128)?,
        sid: text(&request["native"]["sid"], 1, 256)?,
        uid: text(&request["native"]["uid"], 1, 256)?,
    };
    if !matches!(candidate.source.as_str(), "claude" | "codex" | "grok")
        || !candidate.uid.starts_with(&format!("{}:", candidate.source))
        || candidate.uid.len() <= candidate.source.len() + 1
        || !candidate.matches_pending(metadata)
    {
        return Err(rejected);
    }
    Ok(candidate)
}

#[derive(Default)]
pub struct State(Mutex<Option<NativeBinding>>);
impl State {
    pub fn snapshot(&self) -> Option<NativeBinding> {
        self.0
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }
    /// Child -> state is the only nested lock order. Waiting for the child must
    /// not hold the state lock and stall Info snapshots during exit/drain. Poll
    /// and first publication share the child lock used by wait/stop; no socket,
    /// filesystem or PTY work runs under either lock.
    pub fn bind(
        &self,
        candidate: NativeBinding,
        child: &Mutex<Box<dyn Child + Send + Sync>>,
        exited: &AtomicBool,
    ) -> Result<NativeBinding, Error> {
        let observed = self.0.lock().map_err(|_| Error::Rejected)?.clone();
        if let Some(existing) = observed {
            return if existing == candidate {
                Ok(existing)
            } else {
                Err(Error::Conflict)
            };
        }
        let mut child = child.lock().map_err(|_| Error::Rejected)?;
        let mut state = self.0.lock().map_err(|_| Error::Rejected)?;
        // Another caller may have won while this one waited for the child.
        if let Some(existing) = state.as_ref() {
            return if existing == &candidate {
                Ok(existing.clone())
            } else {
                Err(Error::Conflict)
            };
        }
        if exited.load(Ordering::Acquire) || !matches!(child.try_wait(), Ok(None)) {
            return Err(Error::Rejected);
        }
        *state = Some(candidate.clone());
        Ok(candidate)
    }
}

#[cfg(test)]
#[path = "native_binding_tests.rs"]
mod tests;
