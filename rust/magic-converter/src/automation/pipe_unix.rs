//! Bounded same-user JSON requests over a private Unix-domain socket.
use super::{check, sha256};
use serde_json::Value;
use std::fs::{self, DirBuilder};
use std::io::{self, BufRead, BufReader, ErrorKind, Write};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, PermissionsExt};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};

const LIMIT: usize = 2 * 1024 * 1024;
const TIMEOUT: Duration = Duration::from_secs(10);

fn name(directory: &Path) -> io::Result<PathBuf> {
    let owner = fs::metadata(directory)?.uid();
    let folder = PathBuf::from(format!("/tmp/magicconverter-{owner}"));
    match DirBuilder::new().mode(0o700).create(&folder) {
        Ok(()) => (),
        Err(error) if error.kind() == ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error),
    }
    let metadata = fs::symlink_metadata(&folder)?;
    if !metadata.is_dir() || metadata.uid() != owner || metadata.permissions().mode() & 0o077 != 0 {
        return Err(io::Error::new(
            ErrorKind::PermissionDenied,
            "자동화 소켓 폴더의 권한을 확인하세요.",
        ));
    }
    let key = sha256(directory.as_os_str().as_encoded_bytes());
    Ok(folder.join(format!("{}.sock", &key[..24])))
}

fn receive(stream: &UnixStream) -> io::Result<Value> {
    stream.set_read_timeout(Some(TIMEOUT))?;
    let mut reader = BufReader::new(stream);
    let mut value = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return Err(io::Error::new(
                ErrorKind::UnexpectedEof,
                "불완전한 IPC 요청입니다.",
            ));
        }
        let end = available.iter().position(|&byte| byte == b'\n');
        let length = end.map_or(available.len(), |index| index + 1);
        if value.len() + length > LIMIT {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "IPC 요청이 너무 큽니다.",
            ));
        }
        value.extend_from_slice(&available[..length]);
        reader.consume(length);
        if end.is_some() {
            return serde_json::from_slice(&value).map_err(io::Error::other);
        }
    }
}

fn send(mut stream: &UnixStream, value: &Value) -> io::Result<()> {
    let mut data = serde_json::to_vec(value).map_err(io::Error::other)?;
    data.push(b'\n');
    if data.len() > LIMIT {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "IPC 응답이 너무 큽니다.",
        ));
    }
    stream.set_write_timeout(Some(TIMEOUT))?;
    stream.write_all(&data)
}

pub fn serve(
    directory: &Path,
    stopping: &AtomicBool,
    dispatch: impl Fn(Value) -> Value + Sync,
) -> io::Result<()> {
    let path = name(directory)?;
    if path.exists() {
        if UnixStream::connect(&path).is_ok() {
            return Err(io::Error::new(
                ErrorKind::AddrInUse,
                "이미 실행 중인 인스턴스가 있습니다.",
            ));
        }
        fs::remove_file(&path)?;
    }
    let listener = UnixListener::bind(&path)?;
    struct SocketPath(PathBuf);
    impl Drop for SocketPath {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }
    let _socket = SocketPath(path);
    listener.set_nonblocking(true)?;
    thread::scope(|scope| {
        let mut clients = Vec::new();
        while !stopping.load(Ordering::Relaxed) {
            clients.retain(|client: &thread::ScopedJoinHandle<'_, ()>| !client.is_finished());
            if clients.len() >= 16 {
                thread::sleep(Duration::from_millis(20));
                continue;
            }
            match listener.accept() {
                Ok((stream, _)) => {
                    let dispatch = &dispatch;
                    clients.push(scope.spawn(move || {
                        let response = match receive(&stream) {
                            Ok(request) => dispatch(request),
                            Err(error) => {
                                serde_json::json!({"ok": false, "error": error.to_string()})
                            }
                        };
                        let _ = send(&stream, &response);
                    }));
                }
                Err(error) if error.kind() == ErrorKind::WouldBlock => {
                    thread::sleep(Duration::from_millis(20))
                }
                Err(error) => return Err(error),
            }
        }
        Ok(())
    })
}

pub fn call(directory: &Path, request: &Value) -> io::Result<Value> {
    let path = name(directory)?;
    let deadline = Instant::now() + TIMEOUT;
    let stream = loop {
        check(&crate::INTERRUPTED)?;
        match UnixStream::connect(&path) {
            Ok(stream) => break stream,
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::NotFound | ErrorKind::ConnectionRefused
                ) && Instant::now() < deadline =>
            {
                thread::sleep(Duration::from_millis(50))
            }
            Err(error) => return Err(error),
        }
    };
    send(&stream, request)?;
    receive(&stream)
}
