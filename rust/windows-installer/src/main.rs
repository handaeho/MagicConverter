#![cfg(windows)]
#![windows_subsystem = "windows"]

mod platform;
mod wizard;

use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::os::windows::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use uuid::Uuid;
use zip::{CompressionMethod, ZipArchive, ZipWriter, write::SimpleFileOptions};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const VERSION: &str = env!("CARGO_PKG_VERSION");
const PRODUCT: &str = "MagicConverter";
const FOLDER: &str = "magic-converter";
const MANIFEST: &str = "payload.json";
const MARKER: &str = ".magicconverter-install-id";
const SIGNATURE: &[u8; 16] = b"MAGIC-OFFLINE-3\0";
const FOOTER: u64 = 56;
const MAX_ARCHIVE: u64 = 3 * 1024 * 1024 * 1024;
const MAX_FILES: usize = 2048;
const MAX_MANIFEST: u64 = 1024 * 1024;

#[derive(Parser)]
#[command(version)]
struct Cli {
    #[arg(long, global = true)]
    report: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    Install {
        #[arg(long)]
        parent: Option<PathBuf>,
        #[arg(long)]
        quiet: bool,
    },
    Uninstall {
        #[arg(long)]
        parent: Option<PathBuf>,
        #[arg(long)]
        quiet: bool,
    },
    #[command(hide = true)]
    Bundle {
        #[arg(long)]
        payload: PathBuf,
        #[arg(long)]
        output: PathBuf,
    },
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct Member {
    path: String,
    bytes: u64,
    sha256: String,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct Manifest {
    product: String,
    version: String,
    files: Vec<Member>,
}

#[derive(Deserialize, Serialize)]
struct Marker {
    id: Uuid,
    payload: Manifest,
}

#[derive(Deserialize, Serialize)]
struct Location {
    parent: PathBuf,
    id: Uuid,
}

fn program_root() -> Result<PathBuf> {
    Ok(PathBuf::from(
        env::var_os("LOCALAPPDATA").ok_or("Windows 사용자 데이터 폴더를 찾지 못했습니다.")?,
    )
    .join("Programs")
    .join(PRODUCT))
}

fn location_record() -> Result<PathBuf> {
    let record = program_root()?.join(format!("install-location-{VERSION}.json"));
    platform::plain(&record)?;
    Ok(record)
}

fn saved_location() -> Result<Option<Location>> {
    let path = location_record()?;
    match File::open(path) {
        Ok(file) if file.metadata()?.len() <= 8192 => Ok(Some(serde_json::from_reader(file)?)),
        Ok(_) => Err("설치 위치 기록이 너무 큽니다.".into()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn register(folder: &Path, id: Uuid) -> Result<()> {
    let parent = folder.parent().ok_or("설치 위치의 상위 폴더가 없습니다.")?;
    let worker = folder.join("magic_converter-cli.exe");
    let migration = std::process::Command::new(worker)
        .args(["presets", "--json"])
        .env_remove("MAGIC_CONVERTER_PROFILE_DIR")
        .creation_flags(platform::CREATE_NO_WINDOW)
        .output()?;
    if !migration.status.success() {
        return Err(format!(
            "이전 설정 가져오기에 실패했습니다. {}",
            String::from_utf8_lossy(&migration.stderr).trim()
        )
        .into());
    }
    platform::shortcut(folder)?;
    platform::register_shell(folder)?;
    write_json(
        &location_record()?,
        &Location {
            parent: parent.to_owned(),
            id,
        },
    )
}

fn write_json(path: &Path, data: &impl Serialize) -> Result<()> {
    let parent = path.parent().ok_or("결과 파일의 상위 폴더가 없습니다.")?;
    fs::create_dir_all(parent)?;
    let mut staged = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(staged.as_file_mut(), data)?;
    staged.as_file().sync_all()?;
    staged.persist(path)?;
    Ok(())
}

fn safe_path(root: &Path, member: &str) -> Result<PathBuf> {
    if member.is_empty() || member.len() > 230 || member.contains('\\') {
        return Err("설치 파일 경로가 올바르지 않습니다.".into());
    }
    let mut path = root.to_path_buf();
    for name in member.split('/') {
        let first = name
            .split('.')
            .next()
            .unwrap_or_default()
            .to_ascii_uppercase();
        let device = matches!(first.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || ["COM", "LPT"].iter().any(|prefix| {
                first.strip_prefix(prefix).is_some_and(|digit| {
                    matches!(
                        digit,
                        "0" | "1" | "2" | "3" | "4" | "5" | "6" | "7" | "8" | "9"
                    )
                })
            });
        if name.is_empty()
            || name.ends_with(['.', ' '])
            || device
            || name.chars().any(|ch| ch < ' ' || r#":*?"<>|"#.contains(ch))
        {
            return Err("설치 파일 경로가 올바르지 않습니다.".into());
        }
        path.push(name);
    }
    Ok(path)
}

fn validate_manifest(manifest: &Manifest, version: &str) -> Result<()> {
    if manifest.product != PRODUCT
        || manifest.version != version
        || manifest.files.is_empty()
        || manifest.files.len() > MAX_FILES
    {
        return Err("설치 파일 목록의 제품·버전·개수가 올바르지 않습니다.".into());
    }
    let mut names = BTreeSet::new();
    let mut total = 0_u64;
    for entry in &manifest.files {
        safe_path(Path::new(""), &entry.path)?;
        let lower = entry.path.to_lowercase();
        if lower == MANIFEST
            || lower == MARKER
            || entry.sha256.len() != 64
            || !entry.sha256.bytes().all(|ch| ch.is_ascii_hexdigit())
            || !names.insert(lower)
        {
            return Err("설치 파일 목록의 해시 또는 경로가 올바르지 않습니다.".into());
        }
        total = total
            .checked_add(entry.bytes)
            .filter(|&size| size <= MAX_ARCHIVE)
            .ok_or("설치 파일의 전체 크기 제한을 초과했습니다.")?;
    }
    if !names.contains("magic_converter-cli.exe") {
        return Err("설치 파일 목록에 magic_converter-cli.exe가 없습니다.".into());
    }
    Ok(())
}

fn transfer(mut reader: impl Read, mut writer: impl Write, expected: u64) -> Result<String> {
    let mut hash = Sha256::new();
    let mut left = expected;
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        left = left
            .checked_sub(count as u64)
            .ok_or("설치 파일의 기록된 크기와 실제 크기가 다릅니다.")?;
        let bytes = &buffer[..count];
        hash.update(bytes);
        writer.write_all(bytes)?;
    }
    if left != 0 {
        return Err("설치 파일의 기록된 크기와 실제 크기가 다릅니다.".into());
    }
    writer.flush()?;
    Ok(format!("{:x}", hash.finalize()))
}

fn install_folder(parent: Option<&Path>) -> Result<PathBuf> {
    let parent = if let Some(parent) = parent {
        if !parent.is_absolute() {
            return Err("설치 상위 폴더에는 Windows 절대 경로를 지정하세요.".into());
        }
        parent.to_owned()
    } else {
        program_root()?.join(VERSION)
    };
    if parent.parent().is_none()
        || parent
            .components()
            .any(|part| matches!(part, Component::ParentDir))
        || platform::system_path(&parent)?
    {
        return Err("설치 상위 폴더로 사용할 수 없는 경로입니다.".into());
    }
    platform::plain(&parent)?;
    Ok(parent.join(FOLDER))
}

struct Window {
    file: File,
    start: u64,
    length: u64,
    position: u64,
}

impl Read for Window {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let capacity = (self.length - self.position).min(buffer.len() as u64) as usize;
        let count = self.file.read(&mut buffer[..capacity])?;
        self.position += count as u64;
        Ok(count)
    }
}

impl Seek for Window {
    fn seek(&mut self, offset: SeekFrom) -> io::Result<u64> {
        let target = match offset {
            SeekFrom::Start(index) => i128::from(index),
            SeekFrom::End(index) => i128::from(self.length) + i128::from(index),
            SeekFrom::Current(index) => i128::from(self.position) + i128::from(index),
        };
        if !(0..=i128::from(self.length)).contains(&target) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "설치 번들의 범위를 벗어났습니다.",
            ));
        }
        self.file
            .seek(SeekFrom::Start(self.start + target as u64))?;
        self.position = target as u64;
        Ok(self.position)
    }
}

fn payload(exe: &Path) -> Result<ZipArchive<BufReader<Window>>> {
    let mut file = File::open(exe)?;
    let length = file.metadata()?.len();
    if length <= FOOTER {
        return Err("이 실행 파일에 오프라인 설치 번들이 없습니다.".into());
    }
    file.seek(SeekFrom::End(-(FOOTER as i64)))?;
    let mut raw = [0_u8; FOOTER as usize];
    file.read_exact(&mut raw)?;
    if &raw[..16] != SIGNATURE {
        return Err("이 실행 파일에 오프라인 설치 번들이 없습니다.".into());
    }
    let size = u64::from_le_bytes(raw[16..24].try_into()?);
    let start = length
        .checked_sub(size)
        .and_then(|length| length.checked_sub(FOOTER))
        .filter(|&start| start > 0 && (1..=MAX_ARCHIVE).contains(&size))
        .ok_or("설치 번들의 크기가 올바르지 않습니다.")?;
    file.seek(SeekFrom::Start(start))?;
    let mut reader = Window {
        file,
        start,
        length: size,
        position: 0,
    };
    let actual = transfer(&mut reader, io::sink(), size)?;
    let expected = raw[24..]
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if actual != expected {
        return Err("오프라인 설치 번들의 SHA-256이 일치하지 않습니다.".into());
    }
    reader.seek(SeekFrom::Start(0))?;
    Ok(ZipArchive::new(BufReader::new(reader))?)
}

fn package(input: &Path, output: &Path) -> Result<Value> {
    let input = fs::canonicalize(input)?;
    platform::plain(&input)?;
    let mut sources = BTreeMap::new();
    let mut folders = vec![input.clone()];
    while let Some(folder) = folders.pop() {
        for entry in fs::read_dir(folder)? {
            let path = entry?.path();
            platform::plain(&path)?;
            let data = fs::symlink_metadata(&path)?;
            if data.is_dir() {
                folders.push(path);
            } else if data.is_file() {
                let name = path
                    .strip_prefix(&input)?
                    .to_str()
                    .ok_or("설치 파일 경로가 유니코드가 아닙니다.")?
                    .replace('\\', "/");
                sources.insert(name, path);
                if sources.len() > MAX_FILES {
                    return Err("설치 파일 개수 제한을 초과했습니다.".into());
                }
            }
        }
    }
    let files = sources
        .iter()
        .map(|(name, path)| -> Result<Member> {
            let file = File::open(path)?;
            let bytes = file.metadata()?.len();
            let sha256 = transfer(file, io::sink(), bytes)?;
            Ok(Member {
                path: name.clone(),
                bytes,
                sha256,
            })
        })
        .collect::<Result<_>>()?;
    let manifest = Manifest {
        product: PRODUCT.to_owned(),
        version: VERSION.to_owned(),
        files,
    };
    validate_manifest(&manifest, VERSION)?;
    let output = std::path::absolute(output)?;
    let parent = output
        .parent()
        .ok_or("번들 출력 폴더가 올바르지 않습니다.")?;
    fs::create_dir_all(parent)?;
    let mut zip = tempfile::tempfile_in(parent)?;
    let mut writer = ZipWriter::new(&mut zip);
    let options = SimpleFileOptions::default();
    writer.start_file(
        MANIFEST,
        options.compression_method(CompressionMethod::Deflated),
    )?;
    serde_json::to_writer(&mut writer, &manifest)?;
    for member in &manifest.files {
        let compression = if member.bytes >= 64 * 1024 {
            CompressionMethod::Xz
        } else {
            CompressionMethod::Deflated
        };
        writer.start_file(&member.path, options.compression_method(compression))?;
        if transfer(
            File::open(&sources[&member.path])?,
            &mut writer,
            member.bytes,
        )? != member.sha256
        {
            return Err("번들 작성 중 설치 파일이 바뀌었습니다.".into());
        }
    }
    writer.finish()?;
    let size = zip.metadata()?.len();
    if size > MAX_ARCHIVE {
        return Err("설치 번들의 전체 크기 제한을 초과했습니다.".into());
    }
    zip.seek(SeekFrom::Start(0))?;
    let mut staged = tempfile::NamedTempFile::new_in(parent)?;
    {
        let mut out = BufWriter::new(staged.as_file_mut());
        io::copy(&mut File::open(env::current_exe()?)?, &mut out)?;
        let hash = transfer(&mut zip, &mut out, size)?;
        out.write_all(SIGNATURE)?;
        out.write_all(&size.to_le_bytes())?;
        for pair in hash.as_bytes().chunks_exact(2) {
            out.write_all(&[u8::from_str_radix(std::str::from_utf8(pair)?, 16)?])?;
        }
        out.flush()?;
    }
    staged.as_file().sync_all()?;
    staged.persist_noclobber(&output)?;
    Ok(json!({"ok": true, "bundle": output, "files": manifest.files.len(), "archive_bytes": size}))
}

fn read_marker(folder: &Path) -> Result<Marker> {
    let path = folder.join(MARKER);
    platform::plain(&path)?;
    let file = File::open(path)?;
    if file.metadata()?.len() > MAX_MANIFEST {
        return Err("설치 확인 파일이 너무 큽니다.".into());
    }
    Ok(serde_json::from_reader(BufReader::new(file))?)
}

fn marker(folder: &Path) -> Result<Marker> {
    let marker = read_marker(folder)?;
    validate_manifest(&marker.payload, VERSION)?;
    Ok(marker)
}

fn matches_file(file: &Path, member: &Member) -> Result<bool> {
    platform::plain(file)?;
    let file = File::open(file)?;
    if file.metadata()?.len() != member.bytes {
        return Ok(false);
    }
    Ok(transfer(file, io::sink(), member.bytes)? == member.sha256)
}

fn release_version(version: &str) -> Option<[u32; 3]> {
    let mut fields = version.split('.');
    let version = [
        fields.next()?.parse().ok()?,
        fields.next()?.parse().ok()?,
        fields.next()?.parse().ok()?,
    ];
    fields.next().is_none().then_some(version)
}

fn previous_rust_target(executable: &Path) -> Result<bool> {
    let Some(folder) = executable.parent() else {
        return Ok(false);
    };
    let Some(name) = executable.file_name() else {
        return Ok(false);
    };
    if !name.eq_ignore_ascii_case("magic_converter.exe") {
        return Ok(false);
    }
    let manifest = read_marker(folder)?.payload;
    let Some(previous) = release_version(&manifest.version) else {
        return Ok(false);
    };
    let current = release_version(VERSION).expect("installer release version");
    if previous[0] != current[0] || previous >= current {
        return Ok(false);
    }
    validate_manifest(&manifest, &manifest.version)?;
    let Some(member) = manifest
        .files
        .iter()
        .find(|member| member.path == "magic_converter.exe")
    else {
        return Ok(false);
    };
    matches_file(executable, member)
}

fn install(exe: &Path, folder: &Path, mut progress: impl FnMut(usize, usize)) -> Result<Value> {
    let mut archive = payload(exe)?;
    let manifest: Manifest = {
        let file = archive.by_name(MANIFEST)?;
        if file.size() > MAX_MANIFEST {
            return Err("설치 파일 목록이 너무 큽니다.".into());
        }
        serde_json::from_reader(file)?
    };
    validate_manifest(&manifest, VERSION)?;
    if archive.len() != manifest.files.len() + 1 {
        return Err("설치 번들과 파일 목록이 일치하지 않습니다.".into());
    }
    let base = folder.parent().ok_or("설치 위치의 상위 폴더가 없습니다.")?;
    fs::create_dir_all(base)?;
    platform::plain(base)?;
    let _lock = platform::lock(base)?;
    if folder.exists() {
        let previous = marker(folder)?;
        if previous.payload != manifest {
            return Err("기존 설치 폴더의 내용이 다릅니다.".into());
        }
        for (index, member) in manifest.files.iter().enumerate() {
            if !matches_file(&safe_path(folder, &member.path)?, member)? {
                return Err("기존 설치 파일을 확인하지 못했습니다.".into());
            }
            progress(index + 1, manifest.files.len());
        }
        register(folder, previous.id)?;
        return Ok(
            json!({"ok": true, "location": folder, "already_installed": true, "files": manifest.files.len()}),
        );
    }
    let stage = tempfile::Builder::new()
        .prefix(".magicconverter-install-")
        .tempdir_in(base)?;
    for (index, member) in manifest.files.iter().enumerate() {
        let mut entry = archive.by_name(&member.path)?;
        if !entry.is_file() || entry.size() != member.bytes {
            return Err("설치 파일의 형식이나 크기가 목록과 다릅니다.".into());
        }
        let destination = safe_path(stage.path(), &member.path)?;
        fs::create_dir_all(
            destination
                .parent()
                .ok_or("설치 파일 경로가 올바르지 않습니다.")?,
        )?;
        let output = OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(destination)?;
        if transfer(&mut entry, BufWriter::new(&output), member.bytes)? != member.sha256 {
            return Err("설치 파일의 SHA-256이 일치하지 않습니다.".into());
        }
        output.sync_all()?;
        progress(index + 1, manifest.files.len());
    }
    let count = manifest.files.len();
    let id = Uuid::new_v4();
    write_json(
        &stage.path().join(MARKER),
        &Marker {
            id,
            payload: manifest,
        },
    )?;
    platform::publish(stage.path(), folder)?;
    let _ = stage.keep();
    register(folder, id)?;
    Ok(json!({"ok": true, "location": folder, "already_installed": false, "files": count}))
}

fn in_use_message(apps: &[String]) -> String {
    let names = apps.iter().take(5).cloned().collect::<Vec<_>>().join(", ");
    let magic_converter = apps.iter().any(|name| {
        let name = name.to_ascii_lowercase();
        name.contains("magicconverter") || name.contains("magic_converter")
    });
    let reason = if magic_converter {
        "MagicConverter가 실행 중입니다."
    } else {
        "다른 프로그램이 MagicConverter 설치 파일을 사용 중입니다."
    };
    format!(
        "{reason}\r\n사용 중인 프로그램: {names}\r\n\r\n진행 중인 변환을 마친 뒤, 작업 표시줄 시계 옆의 MagicConverter 아이콘을 우클릭하여 ‘종료’를 선택하세요. 설치 파일을 사용 중인 다른 프로그램도 닫은 후 다시 시도하세요."
    )
}

fn removal_error(
    operation: &str,
    path: &Path,
    error: Box<dyn std::error::Error>,
) -> Box<dyn std::error::Error> {
    use windows_sys::Win32::Foundation::{
        ERROR_ACCESS_DENIED, ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION,
    };
    let code = error
        .downcast_ref::<io::Error>()
        .and_then(io::Error::raw_os_error);
    let detail = match code.map(|code| code as u32) {
        Some(ERROR_ACCESS_DENIED | ERROR_SHARING_VIOLATION | ERROR_LOCK_VIOLATION) => {
            let apps = path.is_file().then(|| {
                platform::applications_using(&[path.to_owned()], false).unwrap_or_default()
            });
            if let Some(apps) = apps.filter(|apps| !apps.is_empty()) {
                in_use_message(&apps)
            } else if code == Some(ERROR_ACCESS_DENIED as i32) {
                "Windows에서 액세스를 거부했습니다. 설치에 사용한 Windows 계정과 아래 위치의 삭제 권한을 확인하세요. 해당 위치를 열어 둔 프로그램이 있으면 닫은 뒤 다시 시도하세요.".to_owned()
            } else {
                "다른 프로그램이 아래 파일 또는 폴더를 사용 중입니다. 이 위치를 열어 둔 프로그램을 닫은 뒤 다시 시도하세요.".to_owned()
            }
        }
        _ if code.is_none() && !error.is::<io::Error>() => return error,
        _ => error.to_string(),
    };
    format!(
        "{operation}에 실패했습니다.\r\n\r\n{detail}\r\n\r\n위치: {}",
        path.display()
    )
    .into()
}

fn remove_file(path: &Path, operation: &str) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(removal_error(operation, path, error.into())),
    }
}

fn remove_installation(folder: &Path, mut progress: impl FnMut(usize, usize)) -> Result<Value> {
    platform::plain(folder)
        .map_err(|error| removal_error("설치 폴더 확인", folder, error.into()))?;
    let matching_record = || -> Result<Option<PathBuf>> {
        let matching = saved_location().ok().flatten().is_some_and(|saved| {
            folder
                .parent()
                .is_some_and(|parent| platform::same(&saved.parent, parent))
        });
        matching.then(location_record).transpose()
    };
    if !folder.exists() {
        if let Some(record) = matching_record()? {
            remove_file(&record, "설치 위치 기록 정리")?;
        }
        return Ok(json!({"ok": true, "location": folder, "removed": 0, "preserved": []}));
    }
    let parent = folder.parent().ok_or("설치 위치의 상위 폴더가 없습니다.")?;
    let _lock = platform::lock(parent).map_err(|error| {
        if error.kind() == io::ErrorKind::AlreadyExists {
            "다른 설치 또는 제거 작업이 진행 중입니다. 해당 작업을 마친 뒤 다시 시도하세요.".into()
        } else {
            removal_error("설치 폴더 잠금", parent, error.into())
        }
    })?;
    let marker_path = folder.join(MARKER);
    let marker =
        marker(folder).map_err(|error| removal_error("설치 기록 확인", &marker_path, error))?;
    let mut present = Vec::new();
    let mut directories = BTreeSet::new();
    for member in &marker.payload.files {
        let path = safe_path(folder, &member.path)?;
        platform::plain(&path)
            .map_err(|error| removal_error("설치 파일 확인", &path, error.into()))?;
        directories.extend(
            path.ancestors()
                .skip(1)
                .take_while(|path| *path != folder)
                .map(Path::to_owned),
        );
        if path
            .try_exists()
            .map_err(|error| removal_error("설치 파일 확인", &path, error.into()))?
        {
            present.push((path, member));
        }
    }
    let executables: Vec<_> = present
        .iter()
        .filter(|(path, _)| {
            path.extension()
                .is_some_and(|extension| extension.eq_ignore_ascii_case("exe"))
                && path.is_file()
        })
        .map(|(path, _)| path.clone())
        .collect();
    if let Ok(apps) = platform::applications_using(&executables, true)
        && !apps.is_empty()
    {
        return Err(in_use_message(&apps).into());
    }

    // Verify every deletion before removing the first file so a normal lock
    // or permission failure does not leave a partially removed installation.
    let record = matching_record()?;
    for path in std::iter::once(&marker_path).chain(&record) {
        platform::check_delete(path)
            .map_err(|error| removal_error("설치 기록 확인", path, error))?;
    }
    let mut preserved = Vec::new();
    let mut removable = Vec::new();
    for (path, member) in present {
        if matches_file(&path, member)
            .map_err(|error| removal_error("설치 파일 확인", &path, error))?
        {
            platform::check_delete(&path)
                .map_err(|error| removal_error("파일 삭제 권한 확인", &path, error))?;
            removable.push((path, member));
        } else {
            preserved.push(member.path.clone());
        }
    }
    let mut removed = 0;
    for (index, (path, member)) in removable.iter().enumerate() {
        if matches_file(path, member)
            .map_err(|error| removal_error("설치 파일 확인", path, error))?
        {
            remove_file(path, "설치 파일 제거")?;
            removed += 1;
        } else {
            preserved.push(member.path.clone());
        }
        progress(index + 1, removable.len());
    }
    platform::remove_shortcut(folder)
        .map_err(|error| removal_error("시작 메뉴 바로 가기 제거", folder, error))?;
    platform::remove_shell(folder)
        .map_err(|error| removal_error("탐색기 메뉴 제거", folder, error))?;
    for directory in directories.into_iter().rev() {
        remove_empty_dir(&directory)?;
    }
    if let Some(record) = record {
        remove_file(&record, "설치 위치 기록 정리")?;
    }
    remove_file(&marker_path, "설치 기록 제거")?;
    remove_empty_dir(folder)?;
    Ok(json!({"ok": true, "location": folder, "removed": removed, "preserved": preserved}))
}

fn remove_empty_dir(directory: &Path) -> Result<()> {
    platform::plain(directory)
        .map_err(|error| removal_error("설치 폴더 확인", directory, error.into()))?;
    match fs::remove_dir(directory) {
        Ok(()) => Ok(()),
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::DirectoryNotEmpty | io::ErrorKind::NotFound
            ) =>
        {
            Ok(())
        }
        Err(error) => Err(removal_error("설치 폴더 정리", directory, error.into())),
    }
}

fn uninstall_folder(parent: Option<&Path>) -> Result<PathBuf> {
    let location = if parent.is_none() {
        let record = program_root()?.join(format!("install-location-{VERSION}.json"));
        saved_location().map_err(|error| removal_error("설치 위치 기록 읽기", &record, error))?
    } else {
        None
    };
    let default;
    let base = if let Some(base) = parent.or(location.as_ref().map(|item| item.parent.as_path())) {
        base
    } else {
        default = program_root()?.join(VERSION);
        &default
    };
    let folder =
        install_folder(Some(base)).map_err(|error| removal_error("설치 폴더 확인", base, error))?;
    if let Some(location) = &location
        && folder.exists()
        && marker(&folder)
            .map_err(|error| removal_error("설치 기록 확인", &folder.join(MARKER), error))?
            .id
            != location.id
    {
        return Err("설치 위치 기록과 설치 폴더의 식별자가 다릅니다.".into());
    }
    Ok(folder)
}

fn execute(command: Command) -> Result<Value> {
    match command {
        Command::Bundle { payload, output } => package(&payload, &output),
        Command::Install { parent, quiet } => {
            let folder = install_folder(parent.as_deref())?;
            if quiet {
                install(&env::current_exe()?, &folder, |_, _| {})
            } else {
                wizard::run(wizard::Mode::Install, folder)
            }
        }
        Command::Uninstall { parent, quiet } => {
            let folder = uninstall_folder(parent.as_deref())?;
            if quiet {
                remove_installation(&folder, |_, _| {})
            } else {
                wizard::run(wizard::Mode::Remove, folder)
            }
        }
    }
}

fn main() -> std::process::ExitCode {
    let command_line = env::args_os().nth(1).is_some();
    if command_line {
        platform::connect_console();
    }
    let cli = Cli::parse();
    let command = cli.command.unwrap_or_else(|| {
        if env::current_exe().ok().and_then(|path| {
            path.file_stem()
                .map(|name| name.to_string_lossy().contains("uninstall"))
        }) == Some(true)
        {
            Command::Uninstall {
                parent: None,
                quiet: false,
            }
        } else {
            Command::Install {
                parent: None,
                quiet: false,
            }
        }
    });
    let interactive = matches!(
        &command,
        Command::Install { quiet: false, .. } | Command::Uninstall { quiet: false, .. }
    );
    let mut result = execute(command).unwrap_or_else(|error| {
        let message = error.to_string();
        if interactive {
            platform::notice(&message, true);
        }
        json!({"ok": false, "error": message})
    });
    if let Some(report) = cli.report
        && let Err(error) = write_json(&report, &result)
    {
        result["ok"] = json!(false);
        result["report_error"] = json!(error.to_string());
    }
    if command_line {
        let mut output = io::stdout().lock();
        let _ = serde_json::to_writer_pretty(&mut output, &result);
        let _ = writeln!(output);
    }
    std::process::ExitCode::from(u8::from(result["ok"] != true))
}
