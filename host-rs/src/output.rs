//! One bounded FIFO and one socket writer per attachment. Publishers never write
//! to a socket. ACK, replay, live data and exit all use this same FIFO.

use std::collections::VecDeque;
use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

use crate::transport::Stream;

pub const OUTPUT_BYTES: usize = 8 << 20;
pub const OUTPUT_FRAMES: usize = 1024;
pub const WRITE_TIMEOUT: Duration = Duration::from_secs(2);
pub const DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

#[derive(Default)]
struct Queue {
    frames: VecDeque<Arc<[u8]>>,
    // Include the frame currently being written, until its entire write finishes.
    bytes: usize,
    count: usize,
    drain_deadline: Option<Instant>,
}

pub struct Client {
    pub id: u64,
    queue: Mutex<Queue>,
    changed: Condvar,
    shutdown: Stream,
    dead: AtomicBool,
    byte_limit: usize,
    frame_limit: usize,
}

impl Client {
    pub fn new(id: u64, stream: Stream) -> io::Result<Arc<Self>> {
        Self::with_limits(id, stream, OUTPUT_BYTES, OUTPUT_FRAMES)
    }

    fn with_limits(
        id: u64,
        stream: Stream,
        byte_limit: usize,
        frame_limit: usize,
    ) -> io::Result<Arc<Self>> {
        stream.set_write_timeout(Some(WRITE_TIMEOUT))?;
        let client = Arc::new(Self {
            id,
            queue: Mutex::new(Queue::default()),
            changed: Condvar::new(),
            shutdown: stream.try_clone()?,
            dead: AtomicBool::new(false),
            byte_limit,
            frame_limit,
        });
        let worker = client.clone();
        std::thread::Builder::new()
            .name("attach-output".into())
            .spawn(move || worker.write_loop(stream))?;
        Ok(client)
    }

    pub fn is_dead(&self) -> bool {
        self.dead.load(Ordering::Acquire)
    }

    pub fn send(&self, frame: Arc<[u8]>) -> bool {
        self.enqueue(vec![frame], None)
    }

    /// Admission is atomic: an oversized replay cannot leave a success ACK in
    /// the queue. Call before registration, under the session snapshot boundary.
    pub fn initialize(&self, acknowledgement: Vec<u8>, replay: Vec<u8>) -> bool {
        let mut frames = vec![Arc::from(acknowledgement)];
        if !replay.is_empty() {
            frames.push(Arc::from(replay));
        }
        self.enqueue(frames, None)
    }

    pub fn finish(&self, frame: Arc<[u8]>, deadline: Instant) {
        self.enqueue(vec![frame], Some(deadline));
    }

    fn enqueue(&self, frames: Vec<Arc<[u8]>>, deadline: Option<Instant>) -> bool {
        let mut queue = lock(&self.queue);
        if self.is_dead() || queue.drain_deadline.is_some() {
            return false;
        }
        let bytes = frames.iter().map(|f| f.len()).sum::<usize>();
        if bytes > self.byte_limit.saturating_sub(queue.bytes)
            || frames.len() > self.frame_limit.saturating_sub(queue.count)
        {
            // Publish death while holding the queue lock, so another publisher
            // cannot admit more bytes before shutdown wakes the socket writer.
            self.dead.store(true, Ordering::Release);
            drop(queue);
            self.disconnect();
            eprintln!("attach output disconnected: queue limit exceeded");
            return false;
        }
        queue.bytes += bytes;
        queue.count += frames.len();
        queue.frames.extend(frames);
        queue.drain_deadline = deadline;
        self.changed.notify_all();
        true
    }

    /// Shutdown uses its own descriptor, never the writer's lock. This wakes a
    /// blocked socket write and the connection's blocked input read together.
    pub fn disconnect(&self) {
        self.dead.store(true, Ordering::Release);
        self.shutdown.shutdown();
        let mut queue = lock(&self.queue);
        queue.frames.clear();
        queue.bytes = 0;
        queue.count = 0;
        self.changed.notify_all();
    }

    pub fn wait_closed(&self, deadline: Instant) {
        let mut queue = lock(&self.queue);
        while !self.is_dead() {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                break;
            }
            let (next, _) = self
                .changed
                .wait_timeout(queue, left)
                .unwrap_or_else(|e| e.into_inner());
            queue = next;
        }
        drop(queue);
        if !self.is_dead() {
            self.disconnect();
        }
    }

    fn write_loop(&self, mut stream: Stream) {
        loop {
            let frame = {
                let mut queue = lock(&self.queue);
                while queue.frames.is_empty() && !self.is_dead() && queue.drain_deadline.is_none() {
                    queue = self.changed.wait(queue).unwrap_or_else(|e| e.into_inner());
                }
                if self.is_dead() {
                    return;
                }
                match queue.frames.pop_front() {
                    Some(frame) => {
                        self.changed.notify_all();
                        frame
                    }
                    None => {
                        drop(queue);
                        self.disconnect();
                        return;
                    }
                }
            };
            if self.write_frame(&mut stream, &frame).is_err() {
                eprintln!("attach output disconnected: write failed or deadline expired");
                self.disconnect();
                return;
            }
            let mut queue = lock(&self.queue);
            queue.bytes = queue.bytes.saturating_sub(frame.len());
            queue.count = queue.count.saturating_sub(1);
            self.changed.notify_all();
        }
    }

    fn write_frame(&self, stream: &mut Stream, mut bytes: &[u8]) -> io::Result<()> {
        let frame_deadline = Instant::now() + WRITE_TIMEOUT;
        while !bytes.is_empty() {
            let deadline = lock(&self.queue)
                .drain_deadline
                .map_or(frame_deadline, |d| d.min(frame_deadline));
            let left = deadline.saturating_duration_since(Instant::now());
            if self.is_dead() || left.is_zero() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "attach write deadline",
                ));
            }
            // An absolute frame deadline also bounds peers making tiny progress;
            // write_all plus a per-syscall timeout alone does not do that.
            stream.set_write_timeout(Some(left))?;
            match stream.write(bytes) {
                Ok(0) => return Err(io::Error::from(io::ErrorKind::WriteZero)),
                Ok(n) => bytes = &bytes[n..],
                Err(e) if e.kind() == io::ErrorKind::Interrupted => (),
                Err(e) => return Err(e),
            }
        }
        stream.flush()
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::io::Read;
    use std::os::unix::net::UnixStream;

    fn full_socket() -> (Stream, UnixStream) {
        let (mut socket, peer) = UnixStream::pair().unwrap();
        socket.set_nonblocking(true).unwrap();
        loop {
            match socket.write(&[0; 65536]) {
                Ok(_) => (),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => break,
                Err(e) => panic!("{e}"),
            }
        }
        socket.set_nonblocking(false).unwrap();
        (Stream::Unix(socket), peer)
    }

    fn wait_inflight(client: &Client) {
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut queue = lock(&client.queue);
        while !queue.frames.is_empty() {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(!left.is_zero(), "writer never took the first frame");
            (queue, _) = client.changed.wait_timeout(queue, left).unwrap();
        }
        assert_eq!(queue.count, 1);
    }

    #[test]
    fn overflow_charges_inflight_bytes_and_disconnects_only_the_slow_peer() {
        let (stream, _slow_peer) = full_socket();
        let slow = Client::with_limits(1, stream, 8, 10).unwrap();
        let (socket, mut fast_peer) = UnixStream::pair().unwrap();
        fast_peer
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let fast = Client::new(2, Stream::Unix(socket)).unwrap();
        assert!(slow.send(Arc::from(&b"12345678"[..])));
        wait_inflight(&slow);
        assert_eq!(lock(&slow.queue).bytes, 8);
        assert!(!slow.send(Arc::from(&b"9"[..])));
        assert!(slow.is_dead());
        assert_eq!(lock(&slow.queue).bytes, 0);
        assert!(fast.send(Arc::from(&b"still live"[..])));
        let mut bytes = [0; 10];
        fast_peer.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, b"still live");
        assert!(!fast.is_dead());
        fast.disconnect();
    }

    #[test]
    fn tiny_frames_are_bounded_by_count_including_the_inflight_frame() {
        let (stream, _peer) = full_socket();
        let client = Client::with_limits(1, stream, 4096, 2).unwrap();
        assert!(client.send(Arc::from(&b"a"[..])));
        wait_inflight(&client);
        assert!(client.send(Arc::from(&b"b"[..])));
        assert!(!client.send(Arc::from(&b"c"[..])));
        assert!(client.is_dead());
    }

    #[test]
    fn oversized_initial_replay_never_sends_a_partial_success_ack() {
        let (socket, mut peer) = UnixStream::pair().unwrap();
        peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let client = Client::with_limits(1, Stream::Unix(socket), 16, 10).unwrap();
        assert!(!client.initialize(b"{\"ok\":true}\n".to_vec(), vec![1; 17]));
        let mut bytes = Vec::new();
        peer.read_to_end(&mut bytes).unwrap();
        assert!(bytes.is_empty());
    }

    #[test]
    fn disconnect_wakes_a_blocked_attachment_input_reader() {
        let (socket, _peer) = UnixStream::pair().unwrap();
        let mut input = socket.try_clone().unwrap();
        let client = Client::new(1, Stream::Unix(socket)).unwrap();
        let (tx, rx) = std::sync::mpsc::channel();
        let reader = std::thread::spawn(move || {
            tx.send(input.read(&mut [0; 1])).unwrap();
        });
        client.disconnect();
        assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap().unwrap(), 0);
        reader.join().unwrap();
    }

    #[test]
    fn many_slow_writers_share_one_exit_drain_deadline() {
        let mut clients = Vec::new();
        let mut peers = Vec::new();
        for id in 0..4 {
            let (stream, peer) = full_socket();
            let client = Client::new(id, stream).unwrap();
            assert!(client.send(Arc::from(&b"tail"[..])));
            wait_inflight(&client);
            clients.push(client);
            peers.push(peer);
        }
        let started = Instant::now();
        let deadline = started + Duration::from_millis(100);
        for client in &clients {
            client.finish(Arc::from(&b"exit"[..]), deadline);
            assert!(!client.send(Arc::from(&b"late"[..])));
        }
        for client in &clients {
            client.wait_closed(deadline);
            assert!(client.is_dead());
        }
        assert!(started.elapsed() < Duration::from_millis(350));
    }

    #[test]
    fn write_deadline_disconnects_a_peer_even_below_the_queue_limit() {
        let (stream, _peer) = full_socket();
        let client = Client::new(1, stream).unwrap();
        assert!(client.send(Arc::from(&b"one frame"[..])));
        let deadline = Instant::now() + WRITE_TIMEOUT + Duration::from_secs(1);
        let mut queue = lock(&client.queue);
        while !client.is_dead() {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(
                !left.is_zero(),
                "writer did not disconnect at its own deadline"
            );
            (queue, _) = client.changed.wait_timeout(queue, left).unwrap();
        }
    }
}
