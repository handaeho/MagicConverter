use super::wide;
use std::collections::HashSet;
use std::ffi::OsString;
use std::io;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::Command;
use windows_sys::Win32::Foundation::{GlobalFree, HWND};
use windows_sys::Win32::System::DataExchange::{
    CloseClipboard, EmptyClipboard, OpenClipboard, SetClipboardData,
};
use windows_sys::Win32::System::Memory::{GMEM_MOVEABLE, GlobalAlloc, GlobalLock, GlobalUnlock};
use windows_sys::Win32::System::Threading::CREATE_NEW_CONSOLE;

pub(super) fn is_local(action: &str) -> bool {
    matches!(action, "copy_path" | "terminal")
}

fn path_list(paths: &[PathBuf]) -> OsString {
    let mut value = OsString::new();
    for (index, path) in paths.iter().enumerate() {
        if index != 0 {
            value.push("\r\n");
        }
        value.push(path);
    }
    value
}

fn copy(window: HWND, paths: &[PathBuf]) -> io::Result<()> {
    struct Clipboard;
    impl Drop for Clipboard {
        fn drop(&mut self) {
            unsafe { CloseClipboard() };
        }
    }
    let value = wide(path_list(paths));
    let buffer = unsafe { GlobalAlloc(GMEM_MOVEABLE, value.len() * size_of::<u16>()) };
    if buffer.is_null() {
        return Err(io::Error::last_os_error());
    }
    let result = (|| {
        let destination = unsafe { GlobalLock(buffer) }.cast::<u16>();
        if destination.is_null() {
            return Err(io::Error::last_os_error());
        }
        unsafe {
            destination.copy_from_nonoverlapping(value.as_ptr(), value.len());
            GlobalUnlock(buffer);
        }
        if unsafe { OpenClipboard(window) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let _clipboard = Clipboard;
        const CF_UNICODETEXT: u32 = 13;
        if unsafe { EmptyClipboard() } == 0
            || unsafe { SetClipboardData(CF_UNICODETEXT, buffer) }.is_null()
        {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    })();
    if result.is_err() {
        unsafe { GlobalFree(buffer) };
    }
    result
}

fn directories(paths: &[PathBuf]) -> io::Result<Vec<&Path>> {
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    for path in paths {
        let directory = if path.is_dir() {
            path.as_path()
        } else {
            path.parent()
                .filter(|parent| parent.is_dir())
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::NotFound, "파일의 폴더를 찾을 수 없습니다.")
                })?
        };
        if seen.insert(directory.to_string_lossy().to_lowercase()) {
            result.push(directory);
        }
    }
    Ok(result)
}

fn powershell() -> io::Result<PathBuf> {
    let mut directory = vec![0; 32768];
    let size = unsafe {
        windows_sys::Win32::System::SystemInformation::GetSystemDirectoryW(
            directory.as_mut_ptr(),
            directory.len() as u32,
        )
    } as usize;
    use std::os::windows::ffi::OsStringExt;
    let path = PathBuf::from(OsString::from_wide(
        directory.get(..size).unwrap_or_default(),
    ))
    .join("WindowsPowerShell/v1.0/powershell.exe");
    if size == 0 || !path.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "PowerShell 실행 파일을 찾지 못했습니다.",
        ));
    }
    Ok(path)
}

pub(super) fn perform(
    window: HWND,
    action: &str,
    paths: &[PathBuf],
    local: bool,
) -> io::Result<String> {
    if !local
        || paths.is_empty()
        || paths
            .iter()
            .any(|path| !path.is_absolute() || !path.exists())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "실제 로컬 파일 또는 폴더를 선택하세요.",
        ));
    }
    if action == "copy_path" {
        copy(window, paths)?;
        return Ok(format!("경로 {}개를 클립보드에 복사했습니다.", paths.len()));
    }
    let folders = directories(paths)?;
    let executable = powershell()?;
    for (index, folder) in folders.iter().enumerate() {
        Command::new(&executable)
            .args(["-NoLogo", "-NoProfile", "-NoExit"])
            .current_dir(folder)
            .creation_flags(CREATE_NEW_CONSOLE)
            .spawn()
            .map_err(|error| {
                io::Error::other(format!(
                    "터미널 {index}개를 열었습니다. {}: {error}",
                    folder.display()
                ))
            })?;
    }
    Ok(format!("폴더 {}개에서 터미널을 열었습니다.", folders.len()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_actions_preserve_path_order_and_deduplicate_folders() {
        let root = tempfile::tempdir().unwrap();
        let files = [
            root.path().join("한글 & 둘.txt"),
            root.path().join("둘째.txt"),
        ];
        assert_eq!(directories(&files).unwrap(), [root.path()]);
        assert_eq!(
            path_list(&files).to_string_lossy(),
            format!("{}\r\n{}", files[0].display(), files[1].display())
        );
    }
}
