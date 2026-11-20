//! Own external encoder processes and their captured output.

use std::ffi::OsString;
use std::fs::File;
use std::io::{self, ErrorKind, Read, Seek, SeekFrom};
use std::path::Path;
use std::time::{Duration, Instant};

const CAPTURE_LIMIT: u64 = 2 * 1024 * 1024;
const LOG_LIMIT: u64 = 64 * 1024 * 1024;

pub struct ResultData {
    pub code: u32,
    pub stdout: String,
    pub stderr: String,
}

fn tail(mut file: File) -> io::Result<String> {
    let offset = file.metadata()?.len().saturating_sub(CAPTURE_LIMIT);
    file.seek(SeekFrom::Start(offset))?;
    let mut contents = Vec::new();
    file.take(CAPTURE_LIMIT).read_to_end(&mut contents)?;
    Ok(String::from_utf8_lossy(&contents).into_owned())
}

pub fn run(
    executable: &Path,
    arguments: &[OsString],
    timeout: Option<Duration>,
    idle_timeout: Option<Duration>,
    check: &dyn Fn() -> io::Result<()>,
    monitor: &dyn Fn() -> io::Result<()>,
) -> io::Result<ResultData> {
    check()?;
    let stdout = tempfile::tempfile()?;
    let stderr = tempfile::tempfile()?;
    let started = Instant::now();
    let mut activity = started;
    let process = native::Process::start(executable, arguments, &stdout, &stderr)?;
    let mut previous_size = 0;
    let code = loop {
        check()?;
        monitor()?;
        let size = stdout
            .metadata()?
            .len()
            .saturating_add(stderr.metadata()?.len());
        if size > LOG_LIMIT {
            return Err(io::Error::other(
                "변환 엔진의 로그 크기가 제한을 초과했습니다.",
            ));
        }
        if size != previous_size {
            activity = Instant::now();
            previous_size = size;
        }
        if let Some(code) = process.wait(Duration::from_millis(10))? {
            check()?;
            break code;
        }
        let label = executable
            .file_name()
            .unwrap_or(executable.as_os_str())
            .to_string_lossy();
        if let Some(timeout) = timeout
            && started.elapsed() >= timeout
        {
            return Err(io::Error::new(
                ErrorKind::TimedOut,
                format!(
                    "{label} 실행 제한 시간({}초)이 초과되었습니다.",
                    timeout.as_secs_f64()
                ),
            ));
        }
        if let Some(timeout) = idle_timeout
            && activity.elapsed() >= timeout
        {
            return Err(io::Error::new(
                ErrorKind::TimedOut,
                format!(
                    "{label}에서 {}초 동안 응답이 없습니다.",
                    timeout.as_secs_f64()
                ),
            ));
        }
    };
    // End any descendants before reading the final log lengths and returning.
    drop(process);
    monitor()?;
    if stdout
        .metadata()?
        .len()
        .saturating_add(stderr.metadata()?.len())
        > LOG_LIMIT
    {
        return Err(io::Error::other(
            "변환 엔진의 로그 크기가 제한을 초과했습니다.",
        ));
    }
    Ok(ResultData {
        code,
        stdout: tail(stdout)?,
        stderr: tail(stderr)?,
    })
}

#[cfg(windows)]
mod native {
    use super::*;
    use std::ffi::OsStr;
    use std::mem::{size_of, size_of_val};
    use std::os::windows::ffi::OsStrExt;
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle};
    use std::ptr::{null, null_mut};
    use windows_sys::Win32::Foundation::{
        DUPLICATE_SAME_ACCESS, DuplicateHandle, WAIT_OBJECT_0, WAIT_TIMEOUT,
    };
    use windows_sys::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_JOB_MEMORY,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_PROCESS_MEMORY,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectExtendedLimitInformation,
        SetInformationJobObject, TerminateJobObject,
    };
    use windows_sys::Win32::System::Threading::{
        CREATE_NO_WINDOW, CREATE_SUSPENDED, CreateProcessW, DeleteProcThreadAttributeList,
        EXTENDED_STARTUPINFO_PRESENT, GetCurrentProcess, GetExitCodeProcess, GetProcessId,
        InitializeProcThreadAttributeList, LPPROC_THREAD_ATTRIBUTE_LIST,
        PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROCESS_INFORMATION, ResumeThread, STARTF_USESHOWWINDOW,
        STARTF_USESTDHANDLES, STARTUPINFOEXW, TerminateProcess, UpdateProcThreadAttribute,
        WaitForSingleObject,
    };

    fn quote(argument: &OsStr, output: &mut Vec<u16>) -> io::Result<()> {
        const QUOTE: u16 = b'"' as u16;
        const SLASH: u16 = b'\\' as u16;
        let argument: Vec<_> = argument.encode_wide().collect();
        if argument.contains(&0) {
            return Err(io::Error::new(
                ErrorKind::InvalidInput,
                "argument contains NUL",
            ));
        }
        if !argument.is_empty()
            && !argument
                .iter()
                .any(|character| matches!(*character, 9 | 32 | QUOTE))
        {
            output.extend(argument);
            return Ok(());
        }
        output.push(QUOTE);
        let mut slashes = 0;
        for character in argument {
            match character {
                SLASH => slashes += 1,
                _ => {
                    let count = if character == QUOTE {
                        slashes * 2 + 1
                    } else {
                        slashes
                    };
                    output.extend(std::iter::repeat_n(SLASH, count));
                    output.push(character);
                    slashes = 0;
                }
            }
        }
        output.extend(std::iter::repeat_n(SLASH, slashes * 2));
        output.push(QUOTE);
        Ok(())
    }

    fn inherit(file: &File) -> io::Result<OwnedHandle> {
        let mut duplicate = null_mut();
        // The source File and process pseudo-handle remain valid for this call.
        let ok = unsafe {
            let current = GetCurrentProcess();
            DuplicateHandle(
                current,
                file.as_raw_handle(),
                current,
                &mut duplicate,
                0,
                1,
                DUPLICATE_SAME_ACCESS,
            )
        };
        if ok == 0 {
            Err(io::Error::last_os_error())
        } else {
            // DuplicateHandle gives this process a new, owned handle.
            Ok(unsafe { OwnedHandle::from_raw_handle(duplicate) })
        }
    }

    struct Attributes(Vec<usize>);

    impl Attributes {
        fn new(handles: &[RawHandle; 3]) -> io::Result<Self> {
            let mut bytes = 0;
            // Null is the documented size query. The second call initializes aligned storage.
            unsafe { InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut bytes) };
            if bytes == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut storage = vec![0; bytes.div_ceil(size_of::<usize>())];
            let pointer = storage.as_mut_ptr().cast();
            if unsafe { InitializeProcThreadAttributeList(pointer, 1, 0, &mut bytes) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let mut result = Self(storage);
            // The caller holds the three inheritable handles and their array through process creation.
            if unsafe {
                UpdateProcThreadAttribute(
                    result.pointer(),
                    0,
                    PROC_THREAD_ATTRIBUTE_HANDLE_LIST as usize,
                    handles.as_ptr().cast(),
                    size_of_val(handles),
                    null_mut(),
                    null(),
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            Ok(result)
        }

        fn pointer(&mut self) -> LPPROC_THREAD_ATTRIBUTE_LIST {
            self.0.as_mut_ptr().cast()
        }
    }

    impl Drop for Attributes {
        fn drop(&mut self) {
            // Only initialized lists become an Attributes value.
            unsafe { DeleteProcThreadAttributeList(self.pointer()) };
        }
    }

    pub struct Process {
        job: OwnedHandle,
        process: OwnedHandle,
    }

    impl Process {
        pub fn start(
            executable: &Path,
            args: &[OsString],
            stdout: &File,
            stderr: &File,
        ) -> io::Result<Self> {
            Self::start_limited(executable, args, stdout, stderr, None)
        }

        pub fn start_limited(
            executable: &Path,
            args: &[OsString],
            stdout: &File,
            stderr: &File,
            memory: Option<usize>,
        ) -> io::Result<Self> {
            let null_input = File::open("NUL")?;
            let stdin = inherit(&null_input)?;
            let stdout = inherit(stdout)?;
            let stderr = inherit(stderr)?;
            let handles = [
                stdin.as_raw_handle(),
                stdout.as_raw_handle(),
                stderr.as_raw_handle(),
            ];
            let mut attributes = Attributes::new(&handles)?;
            let mut startup = STARTUPINFOEXW::default();
            startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
            startup.StartupInfo.hStdInput = handles[0];
            startup.StartupInfo.hStdOutput = handles[1];
            startup.StartupInfo.hStdError = handles[2];
            startup.lpAttributeList = attributes.pointer();
            let mut command = Vec::new();
            quote(executable.as_os_str(), &mut command)?;
            for argument in args {
                command.push(b' ' as u16);
                quote(argument, &mut command)?;
            }
            command.push(0);
            let application: Vec<_> = executable.as_os_str().encode_wide().chain([0]).collect();
            let directory = if memory.is_some() {
                startup.StartupInfo.dwFlags |= STARTF_USESHOWWINDOW;
                startup.StartupInfo.wShowWindow = 0; // SW_HIDE
                executable.parent().map(|path| {
                    path.as_os_str()
                        .encode_wide()
                        .chain([0])
                        .collect::<Vec<_>>()
                })
            } else {
                None
            };
            // Create a private kill-on-close job. No process may run before it is assigned.
            let job = unsafe { CreateJobObjectW(null(), null()) };
            if job.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = unsafe { OwnedHandle::from_raw_handle(job) };
            let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if let Some(memory) = memory {
                info.BasicLimitInformation.LimitFlags |=
                    JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_PROCESS_MEMORY;
                info.ProcessMemoryLimit = memory;
                info.JobMemoryLimit = memory;
            }
            if unsafe {
                SetInformationJobObject(
                    job.as_raw_handle(),
                    JobObjectExtendedLimitInformation,
                    (&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                    size_of_val(&info) as u32,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            let mut child = PROCESS_INFORMATION::default();
            // All strings and inherited handles are live through this call.
            if unsafe {
                CreateProcessW(
                    application.as_ptr(),
                    command.as_mut_ptr(),
                    null(),
                    null(),
                    1,
                    CREATE_SUSPENDED | CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
                    null(),
                    directory.as_ref().map_or_else(null, |path| path.as_ptr()),
                    &startup.StartupInfo,
                    &mut child,
                )
            } == 0
            {
                return Err(io::Error::last_os_error());
            }
            // CreateProcessW transfers both handles to the caller.
            let process = unsafe { OwnedHandle::from_raw_handle(child.hProcess) };
            let thread = unsafe { OwnedHandle::from_raw_handle(child.hThread) };
            if unsafe { AssignProcessToJobObject(job.as_raw_handle(), process.as_raw_handle()) }
                == 0
            {
                let error = io::Error::last_os_error();
                unsafe { TerminateProcess(process.as_raw_handle(), 1) };
                return Err(error);
            }
            let owned = Self { job, process };
            if unsafe { ResumeThread(thread.as_raw_handle()) } == u32::MAX {
                return Err(io::Error::last_os_error());
            }
            Ok(owned)
        }

        pub fn id(&self) -> u32 {
            unsafe { GetProcessId(self.process.as_raw_handle()) }
        }

        pub fn wait(&self, interval: Duration) -> io::Result<Option<u32>> {
            // The owned process handle stays live for the wait and exit-code query.
            match unsafe {
                WaitForSingleObject(self.process.as_raw_handle(), interval.as_millis() as u32)
            } {
                WAIT_TIMEOUT => Ok(None),
                WAIT_OBJECT_0 => {
                    let mut status = 0;
                    if unsafe { GetExitCodeProcess(self.process.as_raw_handle(), &mut status) } == 0
                    {
                        return Err(io::Error::last_os_error());
                    }
                    Ok(Some(status))
                }
                _ => Err(io::Error::last_os_error()),
            }
        }
    }

    impl Drop for Process {
        fn drop(&mut self) {
            // The owned job includes descendants. The root wait lets their file writes settle
            // before the caller tears down staged output.
            unsafe {
                TerminateJobObject(self.job.as_raw_handle(), 1);
                WaitForSingleObject(self.process.as_raw_handle(), 3_000);
            }
        }
    }
}

#[cfg(any(windows, target_os = "macos"))]
pub(crate) use native::Process;

#[cfg(target_os = "macos")]
mod native {
    use super::*;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command, Stdio};
    use std::sync::Mutex;

    pub struct Process {
        child: Mutex<Child>,
        group: i32,
    }

    impl Process {
        pub fn start(
            executable: &Path,
            arguments: &[OsString],
            stdout: &File,
            stderr: &File,
        ) -> io::Result<Self> {
            let child = Command::new(executable)
                .args(arguments)
                .stdin(Stdio::null())
                .stdout(stdout.try_clone()?)
                .stderr(stderr.try_clone()?)
                .process_group(0)
                .spawn()?;
            let group = child.id() as i32;
            Ok(Self {
                child: Mutex::new(child),
                group,
            })
        }

        pub fn wait(&self, interval: Duration) -> io::Result<Option<u32>> {
            let deadline = Instant::now() + interval;
            loop {
                if let Some(status) = self.child.lock().unwrap().try_wait()? {
                    return Ok(Some(status.code().map_or(1, |code| code as u32)));
                }
                if Instant::now() >= deadline {
                    return Ok(None);
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }

    impl Drop for Process {
        fn drop(&mut self) {
            // Each encoder owns a new process group, including any subprocesses.
            unsafe {
                libc::kill(-self.group, libc::SIGKILL);
            }
            let _ = self.child.lock().unwrap().wait();
        }
    }
}

#[cfg(not(any(windows, target_os = "macos")))]
mod native {
    use super::*;
    pub struct Process;
    impl Process {
        pub fn start(_: &Path, _: &[OsString], _: &File, _: &File) -> io::Result<Self> {
            Err(io::Error::new(
                ErrorKind::Unsupported,
                "external conversion requires Windows",
            ))
        }
        pub fn wait(&self, _: Duration) -> io::Result<Option<u32>> {
            unreachable!("start is unavailable")
        }
    }
}
