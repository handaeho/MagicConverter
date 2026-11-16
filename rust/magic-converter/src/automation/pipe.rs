//! One bounded UTF-8 JSON message per local, same-user named-pipe connection.
use super::{check, sha256};
use serde_json::Value;
use std::env;
use std::ffi::c_void;
use std::io::{self, ErrorKind};
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::path::Path;
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant};
use unicode_casefold::UnicodeCaseFold;
use windows_sys::Win32::Foundation::{
    ERROR_FILE_NOT_FOUND, ERROR_IO_PENDING, ERROR_NO_DATA, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED,
    ERROR_PIPE_NOT_CONNECTED, GENERIC_READ, GENERIC_WRITE, HANDLE, INVALID_HANDLE_VALUE, LocalFree,
    WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, SECURITY_ATTRIBUTES, TOKEN_QUERY, TOKEN_USER, TokenUser,
};
use windows_sys::Win32::Storage::FileSystem::{
    CreateFileW, FILE_FLAG_FIRST_PIPE_INSTANCE, FILE_FLAG_OVERLAPPED, OPEN_EXISTING,
    PIPE_ACCESS_DUPLEX, ReadFile, SECURITY_IDENTIFICATION, SECURITY_SQOS_PRESENT, WriteFile,
};
use windows_sys::Win32::System::IO::{CancelIoEx, GetOverlappedResult, OVERLAPPED};
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, PIPE_READMODE_BYTE, PIPE_REJECT_REMOTE_CLIENTS,
    PIPE_TYPE_BYTE, PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, GetCurrentProcess, OpenProcessToken, WaitForSingleObject,
};
use windows_sys::Win32::System::WindowsProgramming::GetUserNameW;

const MAX_MESSAGE: usize = 2 * 1024 * 1024;
const CLIENTS: usize = 16;
const TIMEOUT: Duration = Duration::from_secs(10);

fn native_handle(handle: HANDLE) -> io::Result<OwnedHandle> {
    if handle.is_null() || handle == INVALID_HANDLE_VALUE {
        Err(io::Error::last_os_error())
    } else {
        Ok(unsafe { OwnedHandle::from_raw_handle(handle) })
    }
}

struct LocalMemory(*mut c_void);

impl Drop for LocalMemory {
    fn drop(&mut self) {
        unsafe { LocalFree(self.0) };
    }
}

fn user_acl() -> io::Result<LocalMemory> {
    let mut token = null_mut();
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let token = native_handle(token)?;
    let mut needed = 0_u32;
    unsafe { GetTokenInformation(token.as_raw_handle(), TokenUser, null_mut(), 0, &mut needed) };
    if needed == 0 {
        return Err(io::Error::last_os_error());
    }
    // Use pointer-sized storage because TOKEN_USER contains a SID pointer.
    let mut data = vec![0_usize; (needed as usize).div_ceil(size_of::<usize>())];
    if unsafe {
        GetTokenInformation(
            token.as_raw_handle(),
            TokenUser,
            data.as_mut_ptr().cast(),
            needed,
            &mut needed,
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let sid = unsafe { (*data.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    let mut string = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut string) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let _sid_string = LocalMemory(string.cast());
    let count = (0..)
        .find(|offset| unsafe { *string.add(*offset) } == 0)
        .unwrap();
    let sid = String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(string, count) });
    let sddl: Vec<_> = format!("D:P(A;;GA;;;{sid})")
        .encode_utf16()
        .chain([0])
        .collect();
    let mut descriptor = null_mut();
    if unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            SDDL_REVISION_1,
            &mut descriptor,
            null_mut(),
        )
    } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(LocalMemory(descriptor))
}

fn name(directory: &Path) -> io::Result<Vec<u16>> {
    let user = ["LOGNAME", "USER", "LNAME", "USERNAME"]
        .into_iter()
        .find_map(|key| env::var(key).ok().filter(|value| !value.is_empty()))
        .map_or_else(
            || {
                let mut raw = [0_u16; 257];
                let mut size = raw.len() as u32;
                if unsafe { GetUserNameW(raw.as_mut_ptr(), &mut size) } == 0 {
                    Err(io::Error::last_os_error())
                } else {
                    Ok(String::from_utf16_lossy(&raw[..size as usize - 1]))
                }
            },
            Ok,
        )?;
    let folder: String = directory.to_string_lossy().case_fold().collect();
    let key = sha256(format!("{user}|{folder}"));
    Ok(format!(r"\\.\pipe\MagicConverter-{}", &key[..24])
        .encode_utf16()
        .chain([0])
        .collect())
}

fn listener(name: &[u16], descriptor: &LocalMemory, first: bool) -> io::Result<OwnedHandle> {
    let flags = PIPE_ACCESS_DUPLEX
        | FILE_FLAG_OVERLAPPED
        | if first {
            FILE_FLAG_FIRST_PIPE_INSTANCE
        } else {
            0
        };
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.0,
        bInheritHandle: 0,
    };
    native_handle(unsafe {
        CreateNamedPipeW(
            name.as_ptr(),
            flags,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            CLIENTS as u32 + 1,
            64 * 1024,
            64 * 1024,
            500,
            &security,
        )
    })
}

fn operation(
    pipe: &OwnedHandle,
    deadline: Option<Instant>,
    stopping: &AtomicBool,
    connect: bool,
    start: impl FnOnce(*mut OVERLAPPED, *mut u32) -> i32,
) -> io::Result<usize> {
    check(stopping)?;
    let signal = native_handle(unsafe { CreateEventW(null(), 1, 0, null()) })?;
    let mut pending = OVERLAPPED {
        hEvent: signal.as_raw_handle(),
        ..OVERLAPPED::default()
    };
    let mut count = 0;
    if start(&mut pending, &mut count) != 0 {
        return Ok(count as usize);
    }
    let error = io::Error::last_os_error();
    match error.raw_os_error().map(|number| number as u32) {
        Some(ERROR_PIPE_CONNECTED) if connect => return Ok(0),
        Some(ERROR_IO_PENDING) => (),
        _ => return Err(error),
    }
    loop {
        let result = if stopping.load(Ordering::Relaxed) {
            Err(io::Error::new(
                ErrorKind::Interrupted,
                "IPC 작업을 취소했습니다.",
            ))
        } else if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            Err(io::Error::new(
                ErrorKind::TimedOut,
                "IPC 응답 시간이 초과되었습니다.",
            ))
        } else {
            match unsafe { WaitForSingleObject(signal.as_raw_handle(), 50) } {
                WAIT_TIMEOUT => continue,
                WAIT_OBJECT_0 => {
                    if unsafe { GetOverlappedResult(pipe.as_raw_handle(), &pending, &mut count, 0) }
                        != 0
                    {
                        return Ok(count as usize);
                    }
                    return Err(io::Error::last_os_error());
                }
                _ => Err(io::Error::last_os_error()),
            }
        };
        // The kernel can still own the OVERLAPPED and its buffer after cancellation.
        unsafe {
            CancelIoEx(pipe.as_raw_handle(), &pending);
            GetOverlappedResult(pipe.as_raw_handle(), &pending, &mut count, 1);
        }
        return result;
    }
}

fn receive(pipe: &OwnedHandle, deadline: Instant, stopping: &AtomicBool) -> io::Result<Value> {
    let mut buffer = [0_u8; 8192];
    let mut raw = Vec::new();
    loop {
        let count = operation(
            pipe,
            Some(deadline),
            stopping,
            false,
            |pending, count| unsafe {
                ReadFile(
                    pipe.as_raw_handle(),
                    buffer.as_mut_ptr(),
                    buffer.len() as u32,
                    count,
                    pending,
                )
            },
        )?;
        if count == 0 {
            return Err(io::Error::new(
                ErrorKind::UnexpectedEof,
                "IPC 연결이 끊겼습니다.",
            ));
        }
        let offset = raw.len();
        raw.extend_from_slice(&buffer[..count]);
        if raw.len() > MAX_MESSAGE {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "IPC 요청이 너무 큽니다.",
            ));
        }
        if let Some(end) = buffer[..count]
            .iter()
            .position(|&ch| ch == b'\n')
            .map(|index| offset + index)
        {
            if raw[end + 1..]
                .iter()
                .any(|byte| !byte.is_ascii_whitespace())
            {
                return Err(io::Error::new(
                    ErrorKind::InvalidData,
                    "IPC 연결에는 요청 하나만 보낼 수 있습니다.",
                ));
            }
            return serde_json::from_slice(&raw[..end]).map_err(io::Error::other);
        }
    }
}

fn send(
    pipe: &OwnedHandle,
    value: &Value,
    deadline: Instant,
    stopping: &AtomicBool,
) -> io::Result<()> {
    let mut raw = serde_json::to_vec(value).map_err(io::Error::other)?;
    raw.push(b'\n');
    if raw.len() > MAX_MESSAGE {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "IPC 요청이 너무 큽니다.",
        ));
    }
    let mut remaining = &raw[..];
    while !remaining.is_empty() {
        let count = operation(
            pipe,
            Some(deadline),
            stopping,
            false,
            |pending, count| unsafe {
                WriteFile(
                    pipe.as_raw_handle(),
                    remaining.as_ptr(),
                    remaining.len() as u32,
                    count,
                    pending,
                )
            },
        )?;
        if count == 0 {
            return Err(io::Error::new(
                ErrorKind::WriteZero,
                "IPC에 결과를 보낼 수 없습니다.",
            ));
        }
        remaining = &remaining[count..];
    }
    Ok(())
}

pub fn serve(
    directory: &Path,
    stopping: &AtomicBool,
    dispatch: impl Fn(Value) -> Value + Sync,
) -> io::Result<()> {
    let acl = user_acl()?;
    let name = name(directory)?;
    let mut pipe = listener(&name, &acl, true)?;
    thread::scope(|scope| {
        let mut active = Vec::new();
        loop {
            active.retain(|client: &thread::ScopedJoinHandle<'_, ()>| !client.is_finished());
            if stopping.load(Ordering::Relaxed) {
                return Ok(());
            }
            if active.len() >= CLIENTS {
                thread::sleep(Duration::from_millis(20));
                continue;
            }
            match operation(&pipe, None, stopping, true, |pending, _| unsafe {
                ConnectNamedPipe(pipe.as_raw_handle(), pending)
            }) {
                Ok(_) => (),
                Err(error) if error.kind() == ErrorKind::Interrupted => return Ok(()),
                Err(error)
                    if matches!(
                        error.raw_os_error().map(|code| code as u32),
                        Some(ERROR_NO_DATA | ERROR_PIPE_NOT_CONNECTED)
                    ) =>
                {
                    pipe = listener(&name, &acl, false)?;
                    continue;
                }
                Err(error) => return Err(error),
            }
            let connected = std::mem::replace(&mut pipe, listener(&name, &acl, false)?);
            let dispatch = &dispatch;
            active.push(scope.spawn(move || {
                let deadline = Instant::now() + TIMEOUT;
                let response = match receive(&connected, deadline, stopping) {
                    Ok(request) => dispatch(request),
                    Err(error)
                        if error.kind() == ErrorKind::InvalidData
                            || error.kind() == ErrorKind::Other =>
                    {
                        serde_json::json!({"ok": false, "error": error.to_string()})
                    }
                    Err(_) => return,
                };
                // A stop command must be able to deliver its own acknowledgement.
                let _ = send(&connected, &response, deadline, &AtomicBool::new(false));
            }));
        }
    })
}

pub fn call(directory: &Path, request: &Value) -> io::Result<Value> {
    let name = name(directory)?;
    let deadline = Instant::now() + TIMEOUT;
    let pipe = loop {
        let result = native_handle(unsafe {
            CreateFileW(
                name.as_ptr(),
                GENERIC_READ | GENERIC_WRITE,
                0,
                null(),
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION,
                null_mut(),
            )
        });
        match result {
            Ok(pipe) => break pipe,
            Err(error)
                if !matches!(
                    error.raw_os_error().map(|code| code as u32),
                    Some(ERROR_FILE_NOT_FOUND | ERROR_PIPE_BUSY)
                ) =>
            {
                return Err(error);
            }
            Err(_) if Instant::now() >= deadline => {
                return Err(io::Error::new(
                    ErrorKind::TimedOut,
                    "실행 중인 magic_converter에 연결하지 못했습니다.",
                ));
            }
            Err(_) => thread::sleep(Duration::from_millis(50)),
        }
    };
    let cancelled = AtomicBool::new(false);
    send(&pipe, request, deadline, &cancelled)?;
    receive(&pipe, deadline, &cancelled)
}
