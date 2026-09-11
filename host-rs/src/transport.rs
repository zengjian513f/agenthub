//! 本地传输：POSIX 用 unix socket（0600），Windows 用 127.0.0.1 端口 + 随机 token。

use std::io::{self, Read, Write};
#[cfg(unix)]
use std::path::Path;

/// Windows 没有 unix socket，改用 127.0.0.1 端口加随机 token；
/// 因此 Tcp 分支在 POSIX 构建里是死代码，在 Windows 构建里才是唯一通路。
pub enum Listener {
    #[cfg(unix)]
    Unix(std::os::unix::net::UnixListener),
    #[cfg_attr(unix, allow(dead_code))]
    Tcp(std::net::TcpListener),
}

pub enum Stream {
    #[cfg(unix)]
    Unix(std::os::unix::net::UnixStream),
    #[cfg_attr(unix, allow(dead_code))]
    Tcp(std::net::TcpStream),
}

impl Listener {
    #[cfg(unix)]
    pub fn bind_unix(path: &Path) -> io::Result<Self> {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::remove_file(path);
        let listener = std::os::unix::net::UnixListener::bind(path)?;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
        Ok(Self::Unix(listener))
    }

    #[cfg_attr(unix, allow(dead_code))]
    pub fn bind_local_tcp() -> io::Result<(Self, u16)> {
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0))?;
        let port = listener.local_addr()?.port();
        Ok((Self::Tcp(listener), port))
    }

    pub fn accept(&self) -> io::Result<Stream> {
        match self {
            #[cfg(unix)]
            Self::Unix(l) => l.accept().map(|(s, _)| Stream::Unix(s)),
            Self::Tcp(l) => l.accept().map(|(s, _)| Stream::Tcp(s)),
        }
    }
}

impl Stream {
    #[cfg(unix)]
    pub fn connect_unix(path: &Path) -> io::Result<Self> {
        Ok(Self::Unix(std::os::unix::net::UnixStream::connect(path)?))
    }

    #[cfg_attr(unix, allow(dead_code))]
    pub fn connect_tcp(port: u16) -> io::Result<Self> {
        Ok(Self::Tcp(std::net::TcpStream::connect(("127.0.0.1", port))?))
    }

    pub fn try_clone(&self) -> io::Result<Self> {
        match self {
            #[cfg(unix)]
            Self::Unix(s) => s.try_clone().map(Self::Unix),
            Self::Tcp(s) => s.try_clone().map(Self::Tcp),
        }
    }

    pub fn set_read_timeout(&self, timeout: Option<std::time::Duration>) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Self::Unix(s) => s.set_read_timeout(timeout),
            Self::Tcp(s) => s.set_read_timeout(timeout),
        }
    }

    pub fn shutdown(&self) {
        let _ = match self {
            #[cfg(unix)]
            Self::Unix(s) => s.shutdown(std::net::Shutdown::Both),
            Self::Tcp(s) => s.shutdown(std::net::Shutdown::Both),
        };
    }
}

impl Read for Stream {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Self::Unix(s) => s.read(buf),
            Self::Tcp(s) => s.read(buf),
        }
    }
}

impl Write for Stream {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        match self {
            #[cfg(unix)]
            Self::Unix(s) => s.write(buf),
            Self::Tcp(s) => s.write(buf),
        }
    }

    fn flush(&mut self) -> io::Result<()> {
        match self {
            #[cfg(unix)]
            Self::Unix(s) => s.flush(),
            Self::Tcp(s) => s.flush(),
        }
    }
}
