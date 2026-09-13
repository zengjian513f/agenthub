use super::*;
use portable_pty::{Child, ChildKiller, ExitStatus};

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
    kills: Arc<AtomicUsize>,
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
        panic!("stop must only poll, never wait")
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
        assert_eq!(self.polls.load(Ordering::SeqCst), 1);
        self.kills.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
    fn clone_killer(&self) -> Box<dyn ChildKiller + Send + Sync> {
        Box::new(self.clone())
    }
}

#[test]
fn a_reaped_child_or_failed_probe_never_receives_graceful_or_force_signals() {
    for observation in [Observation::Reaped, Observation::Failed] {
        for force in [false, true] {
            let polls = Arc::new(AtomicUsize::new(0));
            let kills = Arc::new(AtomicUsize::new(0));
            let child: Mutex<Box<dyn Child + Send + Sync>> = Mutex::new(Box::new(FakeChild {
                observation,
                polls: polls.clone(),
                kills: kills.clone(),
            }));
            let signals = AtomicUsize::new(0);
            stop_owned_child(&child, 42, force, |_| {
                signals.fetch_add(1, Ordering::SeqCst);
            });
            assert_eq!(polls.load(Ordering::SeqCst), 1);
            assert_eq!(kills.load(Ordering::SeqCst), 0);
            assert_eq!(signals.load(Ordering::SeqCst), 0);
        }
    }
}

#[cfg(unix)]
#[test]
fn a_live_graceful_signal_keeps_the_child_lock_until_the_signal_returns() {
    let polls = Arc::new(AtomicUsize::new(0));
    let kills = Arc::new(AtomicUsize::new(0));
    let child: Mutex<Box<dyn Child + Send + Sync>> = Mutex::new(Box::new(FakeChild {
        observation: Observation::Live,
        polls: polls.clone(),
        kills: kills.clone(),
    }));
    let signals = AtomicUsize::new(0);
    stop_owned_child(&child, 42, false, |pid| {
        assert_eq!(pid, 42);
        assert_eq!(polls.load(Ordering::SeqCst), 1);
        assert!(
            child.try_lock().is_err(),
            "another waiter could reap before the signal"
        );
        signals.fetch_add(1, Ordering::SeqCst);
    });
    assert_eq!(signals.load(Ordering::SeqCst), 1);
    assert_eq!(kills.load(Ordering::SeqCst), 0);
    assert!(child.try_lock().is_ok());
}

#[test]
fn a_live_force_cancel_uses_the_owned_child_handle_after_polling() {
    let polls = Arc::new(AtomicUsize::new(0));
    let kills = Arc::new(AtomicUsize::new(0));
    let child: Mutex<Box<dyn Child + Send + Sync>> = Mutex::new(Box::new(FakeChild {
        observation: Observation::Live,
        polls: polls.clone(),
        kills: kills.clone(),
    }));
    stop_owned_child(&child, 42, true, |_| {
        panic!("force must use the owned child handle")
    });
    assert_eq!(polls.load(Ordering::SeqCst), 1);
    assert_eq!(kills.load(Ordering::SeqCst), 1);
}
