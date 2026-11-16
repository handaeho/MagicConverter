use crate::{PRODUCT, Result, VERSION};
use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, ErrorKind};
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::thread;
use std::time::{Duration, Instant};
use windows::Win32::System::Com::{
    CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, CoCreateInstance, CoInitializeEx,
    CoTaskMemFree, CoUninitialize, IPersistFile, STGM_READ,
};
use windows::Win32::UI::Shell::{
    FOLDERID_Programs, FOS_FORCEFILESYSTEM, FOS_PICKFOLDERS, FileOpenDialog, IFileOpenDialog,
    IShellLinkW, KF_FLAG_DEFAULT, SHGetKnownFolderPath, SIGDN_FILESYSPATH, ShellLink,
};
use windows::Win32::UI::WindowsAndMessaging::{
    IDNO, IDYES, MB_ICONERROR, MB_ICONINFORMATION, MB_ICONQUESTION, MB_OK, MB_YESNO,
    MB_YESNOCANCEL, MessageBoxW,
};
use windows::core::{Interface, PCWSTR};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_FLAG_DELETE_ON_CLOSE, FILE_SHARE_READ, MoveFileExW,
};
use windows_sys::Win32::System::Registry::{
    HKEY_CURRENT_USER, RRF_RT_REG_SZ, RegDeleteKeyW, RegGetValueW,
};

fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain([0]).collect()
}

pub fn plain(path: &Path) -> io::Result<()> {
    for path in path.ancestors() {
        if path.as_os_str().is_empty() {
            continue;
        }
        match fs::symlink_metadata(path) {
            Ok(data) if data.file_attributes() & 0x400 != 0 => {
                return Err(io::Error::new(
                    ErrorKind::InvalidInput,
                    "재분석 지점이나 링크가 포함된 설치 경로입니다.",
                ));
            }
            Ok(_) => (),
            Err(error) if error.kind() == ErrorKind::NotFound => (),
            Err(error) => return Err(error),
        }
    }
    Ok(())
}

pub fn same(a: &Path, b: &Path) -> bool {
    a.to_string_lossy().to_lowercase() == b.to_string_lossy().to_lowercase()
}

pub fn system_path(path: &Path) -> Result<bool> {
    for name in ["WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"] {
        if let Some(base) = std::env::var_os(name) {
            let base = Path::new(&base);
            if path.ancestors().any(|parent| same(parent, base)) {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

pub fn publish(source: &Path, target: &Path) -> io::Result<()> {
    let source = wide(source);
    let target = wide(target);
    let started = Instant::now();
    loop {
        if unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), 0) } != 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if !matches!(error.raw_os_error(), Some(5 | 32 | 33))
            || started.elapsed() >= Duration::from_secs(1)
        {
            return Err(error);
        }
        thread::sleep(Duration::from_millis(25));
    }
}

pub fn lock(directory: &Path) -> io::Result<File> {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
        .share_mode(FILE_SHARE_READ)
        .open(directory.join(".magicconverter-installer.lock"))
}

struct Apartment;

impl Apartment {
    fn new() -> windows::core::Result<Self> {
        unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED).ok()? };
        Ok(Self)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { CoUninitialize() };
    }
}

fn link_path(name: &str) -> Result<PathBuf> {
    let raw = unsafe { SHGetKnownFolderPath(&FOLDERID_Programs, KF_FLAG_DEFAULT, None)? };
    let slice = unsafe { raw.as_wide() };
    let path = PathBuf::from(OsString::from_wide(slice)).join(name);
    unsafe { CoTaskMemFree(Some(raw.0.cast())) };
    plain(&path)?;
    Ok(path)
}

fn link_target(link: &Path) -> Result<PathBuf> {
    let shell: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER)? };
    let persist: IPersistFile = shell.cast()?;
    unsafe { persist.Load(PCWSTR(wide(link).as_ptr()), STGM_READ)? };
    let mut path = vec![0_u16; 32_768];
    unsafe { shell.GetPath(&mut path, null_mut(), 0)? };
    path.truncate(
        path.iter()
            .position(|&value| value == 0)
            .unwrap_or(path.len()),
    );
    Ok(PathBuf::from(OsString::from_wide(&path)))
}

const SHORTCUTS: [&str; 2] = ["MagicConverter.lnk", "MagicConverter 3.0.lnk"];

fn legacy_target(exe: &Path) -> bool {
    exe.parent().is_some_and(|folder| {
        let marker = folder.join(".installed");
        exe.file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("magic_converter.exe"))
            && plain(&marker).is_ok()
            && fs::metadata(&marker).is_ok_and(|data| data.is_file() && data.len() <= 16)
            && fs::read_to_string(marker).is_ok_and(|version| version.trim() == "2.2.39")
            && folder.join("runtime/python.exe").is_file()
            && folder.join("app/run_magic_converter.py").is_file()
    })
}

pub fn shortcut(folder: &Path) -> Result<()> {
    let exe = folder.join("magic_converter.exe");
    if !exe.is_file() {
        return Ok(());
    }
    let _apartment = Apartment::new()?;
    let mut destination = None;
    for name in SHORTCUTS {
        let link = link_path(name)?;
        if !link.exists() {
            destination = Some((link, false));
            break;
        }
        if let Ok(target) = link_target(&link) {
            if same(&target, &exe) {
                return Ok(());
            }
            if legacy_target(&target) {
                destination = Some((link, true));
                break;
            }
        }
    }
    let (link, replace) =
        destination.ok_or("시작 메뉴에 같은 이름의 다른 바로 가기가 있습니다.")?;
    let parent = link.parent().ok_or("시작 메뉴 폴더가 없습니다.")?;
    fs::create_dir_all(parent)?;
    let stage = tempfile::Builder::new()
        .suffix(".lnk")
        .tempfile_in(parent)?
        .into_temp_path();
    let shell: IShellLinkW = unsafe { CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER)? };
    unsafe {
        shell.SetPath(PCWSTR(wide(&exe).as_ptr()))?;
        shell.SetWorkingDirectory(PCWSTR(wide(folder).as_ptr()))?;
        shell.SetDescription(PCWSTR(wide(format!("{PRODUCT} {VERSION}")).as_ptr()))?;
    }
    let persist: IPersistFile = shell.cast()?;
    unsafe { persist.Save(PCWSTR(wide(&stage).as_ptr()), true)? };
    if replace {
        stage.persist(link)?;
    } else {
        stage.persist_noclobber(link)?;
    }
    Ok(())
}

pub fn remove_shortcut(folder: &Path) -> Result<()> {
    let _apartment = Apartment::new()?;
    for name in SHORTCUTS {
        let link = link_path(name)?;
        if link_target(&link).is_ok_and(|target| same(&target, &folder.join("magic_converter.exe")))
        {
            fs::remove_file(link)?;
        }
    }
    Ok(())
}

const SHELL_KEY: &str = r"Software\Classes\*\shell\MagicConverter";
const OPEN_ARGUMENTS: &str = " --open \"%1\"";

fn shell_target() -> Result<Option<PathBuf>> {
    let command = wide(format!("{SHELL_KEY}\\command"));
    let mut buffer = vec![0_u16; 4096];
    let mut length = (buffer.len() * size_of::<u16>()) as u32;
    let result = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            command.as_ptr(),
            null(),
            RRF_RT_REG_SZ,
            null_mut(),
            buffer.as_mut_ptr().cast(),
            &mut length,
        )
    };
    if result == 2 {
        return Ok(Some(PathBuf::new()));
    }
    if result != 0 {
        return Err(io::Error::from_raw_os_error(result as i32).into());
    }
    let end = buffer
        .iter()
        .position(|&unit| unit == 0)
        .unwrap_or(buffer.len());
    let found = String::from_utf16_lossy(&buffer[..end]);
    Ok(found
        .strip_suffix(OPEN_ARGUMENTS)
        .map(|path| PathBuf::from(path.trim_matches('"'))))
}

pub fn register_shell(folder: &Path) -> Result<()> {
    if shell_target()?.is_some_and(|target| target.as_os_str().is_empty() || legacy_target(&target))
    {
        let worker = folder.join("magic_converter-cli.exe");
        if worker.is_file()
            && !std::process::Command::new(worker)
                .args(["shell", "install"])
                .output()?
                .status
                .success()
        {
            return Err("탐색기 메뉴를 등록하지 못했습니다.".into());
        }
    }
    Ok(())
}

pub fn remove_shell(folder: &Path) -> Result<()> {
    if shell_target()?.is_some_and(|target| same(&target, &folder.join("magic_converter.exe"))) {
        for key in [wide(format!("{SHELL_KEY}\\command")), wide(SHELL_KEY)] {
            let result = unsafe { RegDeleteKeyW(HKEY_CURRENT_USER, key.as_ptr()) };
            if !matches!(result, 0 | 2) {
                return Err(io::Error::from_raw_os_error(result as i32).into());
            }
        }
    }
    Ok(())
}

fn message_box(
    message: &str,
    flags: windows::Win32::UI::WindowsAndMessaging::MESSAGEBOX_STYLE,
) -> windows::Win32::UI::WindowsAndMessaging::MESSAGEBOX_RESULT {
    unsafe {
        MessageBoxW(
            None,
            PCWSTR(wide(message).as_ptr()),
            PCWSTR(wide(PRODUCT).as_ptr()),
            flags,
        )
    }
}

pub fn confirm(message: &str) -> bool {
    message_box(message, MB_YESNO | MB_ICONQUESTION) == IDYES
}

pub fn notice(message: &str, error: bool) {
    message_box(
        message,
        MB_OK
            | if error {
                MB_ICONERROR
            } else {
                MB_ICONINFORMATION
            },
    );
}

pub fn choose_install_parent(default: &Path) -> Result<Option<PathBuf>> {
    match message_box(
        &format!(
            "{PRODUCT} {VERSION}\n\n기본 위치에 설치하시겠습니까?\n{}\n\n아니요를 선택하면 다른 상위 폴더를 고를 수 있습니다.",
            default.display()
        ),
        MB_YESNOCANCEL | MB_ICONQUESTION,
    ) {
        IDYES => Ok(default.parent().map(Path::to_path_buf)),
        IDNO => {
            let _apartment = Apartment::new()?;
            let dialog: IFileOpenDialog =
                unsafe { CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)? };
            unsafe {
                dialog.SetOptions(dialog.GetOptions()? | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)?;
                dialog.SetTitle(PCWSTR(wide("MagicConverter 설치 상위 폴더").as_ptr()))?;
                if let Err(error) = dialog.Show(None) {
                    if error.code().0 as u32 == 0x800704c7 {
                        return Ok(None);
                    }
                    return Err(error.into());
                }
                let raw = dialog.GetResult()?.GetDisplayName(SIGDN_FILESYSPATH)?;
                let path = PathBuf::from(OsString::from_wide(raw.as_wide()));
                CoTaskMemFree(Some(raw.0.cast()));
                Ok(Some(path))
            }
        }
        _ => Ok(None),
    }
}
