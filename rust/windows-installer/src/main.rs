#![cfg(windows)]

mod platform;

use clap::{Parser, Subcommand};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
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
    let Some(member) = manifest.files.iter().find(|member| member.path == "magic_converter.exe")
    else {
        return Ok(false);
    };
    matches_file(executable, member)
}

fn install(exe: &Path, folder: &Path) -> Result<Value> {
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
        for member in &manifest.files {
            if !matches_file(&safe_path(folder, &member.path)?, member)? {
                return Err("기존 설치 파일을 확인하지 못했습니다.".into());
            }
        }
        register(folder, previous.id)?;
        return Ok(
            json!({"ok": true, "location": folder, "already_installed": true, "files": manifest.files.len()}),
        );
    }
    let stage = tempfile::Builder::new()
        .prefix(".magicconverter-install-")
        .tempdir_in(base)?;
    for member in &manifest.files {
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

fn uninstall(folder: &Path) -> Result<Value> {
    platform::plain(folder)?;
    if !folder.exists() {
        return Ok(json!({"ok": true, "location": folder, "removed": 0, "preserved": []}));
    }
    let _lock = platform::lock(folder.parent().ok_or("설치 위치의 상위 폴더가 없습니다.")?)?;
    let marker = marker(folder)?;
    let mut preserved = Vec::new();
    let mut removed = 0;
    let mut directories = BTreeSet::new();
    for member in &marker.payload.files {
        let path = safe_path(folder, &member.path)?;
        if !path.exists() {
            continue;
        }
        if !matches_file(&path, member)? {
            preserved.push(member.path.clone());
            continue;
        }
        for directory in path.ancestors().skip(1).take_while(|path| *path != folder) {
            directories.insert(directory.to_owned());
        }
        fs::remove_file(path)?;
        removed += 1;
    }
    platform::remove_shortcut(folder)?;
    platform::remove_shell(folder)?;
    fs::remove_file(folder.join(MARKER))?;
    for directory in directories.into_iter().rev().chain([folder.to_owned()]) {
        match fs::remove_dir(directory) {
            Ok(()) => (),
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::DirectoryNotEmpty | io::ErrorKind::NotFound
                ) => {}
            Err(error) => return Err(error.into()),
        }
    }
    Ok(json!({"ok": true, "location": folder, "removed": removed, "preserved": preserved}))
}

fn execute(command: Command) -> Result<Value> {
    match command {
        Command::Bundle { payload, output } => package(&payload, &output),
        Command::Install { parent, quiet } => {
            let parent = if !quiet && parent.is_none() {
                let default = install_folder(None)?;
                let Some(parent) = platform::choose_install_parent(&default)? else {
                    return Ok(json!({"ok": true, "cancelled": true}));
                };
                Some(parent)
            } else {
                parent
            };
            let folder = install_folder(parent.as_deref())?;
            if !quiet
                && !platform::confirm(&format!(
                    "{PRODUCT} {VERSION}\n\n{}\n\nMagicConverter는 AGPL-3.0-or-later로 제공됩니다. 설치된 LICENSE 및 각 엔진의 라이선스 안내를 참조하세요.\n\n이 폴더에 설치하시겠습니까?",
                    folder.display()
                ))
            {
                return Ok(json!({"ok": true, "cancelled": true}));
            }
            let report = install(&env::current_exe()?, &folder)?;
            if !quiet && platform::confirm("설치가 완료되었습니다. MagicConverter를 여시겠습니까?")
            {
                std::process::Command::new(folder.join("magic_converter.exe")).spawn()?;
            }
            Ok(report)
        }
        Command::Uninstall { parent, quiet } => {
            let location = if parent.is_none() {
                saved_location()?
            } else {
                None
            };
            let folder = install_folder(
                parent
                    .as_deref()
                    .or(location.as_ref().map(|item| item.parent.as_path())),
            )?;
            if let Some(location) = &location
                && folder.exists()
                && marker(&folder)?.id != location.id
            {
                return Err("설치 위치 기록과 설치 폴더의 식별자가 다릅니다.".into());
            }
            if !quiet
                && !platform::confirm(&format!(
                    "{PRODUCT} {VERSION}\n\n{}\n\n설치 파일을 제거하시겠습니까?",
                    folder.display()
                ))
            {
                return Ok(json!({"ok": true, "cancelled": true}));
            }
            let report = uninstall(&folder)?;
            if saved_location().ok().flatten().is_some_and(|saved| {
                folder
                    .parent()
                    .is_some_and(|parent| platform::same(&saved.parent, parent))
            }) {
                fs::remove_file(location_record()?)?;
            }
            if !quiet {
                platform::notice(
                    "설치 파일을 제거했습니다. 설정과 변환 결과는 보존했습니다.",
                    false,
                );
            }
            Ok(report)
        }
    }
}

fn main() -> std::process::ExitCode {
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
    println!(
        "{}",
        serde_json::to_string_pretty(&result).expect("valid result JSON")
    );
    std::process::ExitCode::from(u8::from(result["ok"] != true))
}
