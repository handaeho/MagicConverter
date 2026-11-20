//! Stream ordinary ZIP, TAR, TAR.GZ and single GZIP members with bounded I/O.

pub(crate) mod rar;

use bzip2::read::MultiBzDecoder;
use filetime::FileTime;
use flate2::{Compression, read::MultiGzDecoder, write::GzEncoder};
use lzma_rust2::XzReader;
use magic_copy_worker::copy_stream;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Cursor, ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use tar::{Archive, Builder, EntryType, Header};
use time::{OffsetDateTime, PrimitiveDateTime};
use unicode_casefold::UnicodeCaseFold;
use zip::{CompressionMethod, DateTime, System, ZipArchive, ZipWriter, write::FullFileOptions};

const MAX_BYTES: u64 = 4 * 1024 * 1024 * 1024;
const MAX_ENTRIES: usize = 100_000;
const MAX_HEADER: u64 = 1024 * 1024;
const MAX_HEADERS: u64 = 64 * MAX_HEADER;
const NS: i128 = 1_000_000_000;
const FILETIME_EPOCH: i128 = 116_444_736_000_000_000;
const MTIME_LOST: &str =
    "출력 파일 시스템이 일부 압축 항목의 수정 시각을 정확하게 복원하지 못했습니다.";
const LIMIT: &str = "압축 항목 수 또는 해제 크기 제한(100,000개 / 4 GiB)을 초과했습니다.";

pub fn supports(input: &str) -> bool {
    matches!(
        input,
        "zip" | "tar" | "tar.gz" | "gz" | "tar.xz" | "tar.bz2" | "tar.zst" | "rar"
    )
}

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
pub type Notices = BTreeSet<&'static str>;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

struct Member {
    name: String,
    size: u64,
    directory: bool,
    modified: Option<i128>,
    mode: Option<u32>,
}

impl Member {
    fn unix_permissions(&self) -> u32 {
        self.mode
            .unwrap_or(if self.directory { 0o755 } else { 0o644 })
    }
}

#[derive(Default)]
struct Limits {
    names: HashSet<String>,
    components: HashMap<String, String>,
    total: u64,
}

fn casefold(name: &str) -> String {
    name.case_fold().collect()
}

fn safe_name(name: &str) -> io::Result<String> {
    let normalized = name.replace('\\', "/");
    let parts: Vec<_> = normalized
        .split('/')
        .filter(|part| !matches!(*part, "" | "."))
        .collect();
    if normalized.starts_with('/')
        || parts.is_empty()
        || parts.iter().any(|part| {
            let base = part.split('.').next().unwrap_or("").to_ascii_uppercase();
            let reserved = matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
                || ["COM", "LPT"].iter().any(|prefix| {
                    base.strip_prefix(prefix).is_some_and(|n| {
                        (n.len() == 1 && ("1"..="9").contains(&n)) || matches!(n, "¹" | "²" | "³")
                    })
                });
            part.ends_with(['.', ' '])
                || reserved
                || part.chars().any(|ch| ch < ' ' || r#"<>:"|?*"#.contains(ch))
        })
    {
        return Err(invalid(format!("안전하지 않은 압축 내부 경로: {name}")));
    }
    Ok(parts.join("/"))
}

impl Limits {
    fn accept(&mut self, member: &mut Member) -> io::Result<()> {
        if self.names.len() >= MAX_ENTRIES || member.size > MAX_BYTES - self.total {
            return Err(invalid(LIMIT));
        }
        self.total += member.size;
        member.name = safe_name(&member.name)?;
        if !self.names.insert(casefold(&member.name)) {
            return Err(invalid(format!(
                "압축 안에 중복된 이름이 있습니다: {}",
                member.name
            )));
        }
        for end in member
            .name
            .match_indices('/')
            .map(|(index, _)| index)
            .chain([member.name.len()])
        {
            let prefix = &member.name[..end];
            let actual = self
                .components
                .entry(casefold(prefix))
                .or_insert_with(|| prefix.to_owned());
            if actual != prefix {
                return Err(invalid(format!(
                    "대소문자가 충돌하는 압축 경로입니다: {}",
                    member.name
                )));
            }
        }
        Ok(())
    }
}

fn extra_fields(mut input: &[u8]) -> impl Iterator<Item = (u16, &[u8])> {
    std::iter::from_fn(move || {
        let tag = u16::from_le_bytes(input.get(0..2)?.try_into().ok()?);
        let size = u16::from_le_bytes(input.get(2..4)?.try_into().ok()?) as usize;
        let (value, rest) = input.get(4..)?.split_at_checked(size)?;
        input = rest;
        Some((tag, value))
    })
}

fn zip_mtime(extra: &[u8], dos: Option<DateTime>, notices: &mut Notices) -> Option<i128> {
    let mut unix = None;
    for (tag, value) in extra_fields(extra) {
        if tag == 0x5455
            && let Some(flags) = value.first()
        {
            if flags & 6 != 0 {
                notices.insert("ZIP 항목의 접근·생성 시각은 보존하지 않습니다.");
            }
            if flags & 1 != 0 {
                if let Some(raw) = value.get(1..5).and_then(|data| data.try_into().ok()) {
                    unix = Some(i128::from(i32::from_le_bytes(raw)) * NS);
                } else {
                    notices.insert("유효하지 않은 ZIP 확장 수정 시각을 건너뛰었습니다.");
                }
            }
        }
        if tag == 0x000a
            && let Some(attributes) = value.get(4..)
        {
            for (kind, times) in extra_fields(attributes) {
                if kind == 1 && times.len() >= 24 {
                    let ticks = u64::from_le_bytes(times[..8].try_into().unwrap());
                    if times[8..24].iter().any(|&byte| byte != 0) {
                        notices.insert("ZIP 항목의 접근·생성 시각은 보존하지 않습니다.");
                    }
                    if ticks != 0 {
                        return Some((i128::from(ticks) - FILETIME_EPOCH) * 100);
                    }
                }
            }
        }
    }
    unix.or_else(|| {
        let modified = dos.and_then(|value| PrimitiveDateTime::try_from(value).ok());
        if modified.is_none() {
            notices.insert("유효하지 않은 ZIP 수정 시각은 보존하지 못했습니다.");
        }
        modified.map(|value| value.assume_utc().unix_timestamp_nanos())
    })
}

type Visitor<'a> = dyn FnMut(&Member, &mut dyn Read, &mut Notices) -> io::Result<()> + 'a;

fn visit_zip(
    file: File,
    check: Check<'_>,
    visitor: &mut Visitor<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    let mut central = file.try_clone()?;
    let mut archive = ZipArchive::new(file)?;
    if archive.len() > MAX_ENTRIES {
        return Err(invalid(LIMIT));
    }
    if archive.has_duplicate_names() {
        return Err(invalid("압축 안에 중복된 이름이 있습니다."));
    }
    let mut limits = Limits::default();
    for index in 0..archive.len() {
        check()?;
        let mut entry = archive.by_index(index)?;
        let mode = entry.external_attributes() >> 16;
        if !matches!(mode & 0o170000, 0 | 0o100000 | 0o040000) || entry.is_symlink() {
            return Err(invalid("링크/장치 압축 항목은 지원하지 않습니다."));
        }
        // The ZIP parser normalizes typed extra fields, including UT flags.
        // Read the original central bytes and restore the shared file offset.
        let restore = central.stream_position()?;
        central.seek(SeekFrom::Start(entry.central_header_start()))?;
        let mut header = [0; 46];
        central.read_exact(&mut header)?;
        if &header[..4] != b"PK\x01\x02" {
            return Err(invalid("ZIP 중앙 디렉터리 헤더가 올바르지 않습니다."));
        }
        let name_length = u16::from_le_bytes(header[28..30].try_into().unwrap());
        let extra_length = u16::from_le_bytes(header[30..32].try_into().unwrap());
        let mut extra = vec![0; usize::from(extra_length)];
        central.seek(SeekFrom::Current(i64::from(name_length)))?;
        central.read_exact(&mut extra)?;
        central.seek(SeekFrom::Start(restore))?;
        let name = entry.name()?.into_owned();
        let directory = name.ends_with('/');
        let mut member = Member {
            name,
            directory,
            size: if directory { 0 } else { entry.size() },
            modified: zip_mtime(&extra, entry.last_modified(), notices),
            mode: (entry.system() == System::Unix && mode != 0).then_some(mode & 0o777),
        };
        limits.accept(&mut member)?;
        visitor(&member, &mut entry, notices)?;
    }
    Ok(())
}

fn pax_time(raw: &str, notices: &mut Notices) -> Option<i128> {
    let negative = raw.starts_with('-');
    let value = raw.trim_start_matches(['-', '+']);
    let (seconds, fraction) = value.split_once('.').unwrap_or((value, ""));
    let seconds: i128 = seconds.parse().ok()?;
    if seconds > i128::from(i64::MAX) || !fraction.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let digits = fraction.len().min(9);
    let nanos = fraction[..digits].parse::<i128>().unwrap_or(0) * 10_i128.pow((9 - digits) as u32);
    if fraction[digits..].bytes().any(|byte| byte != b'0') {
        notices.insert("TAR 수정 시각은 나노초 정밀도까지 보존합니다.");
    }
    let signed = seconds * NS + nanos;
    Some(if negative { -signed } else { signed })
}

fn visit_tar(
    reader: impl Read,
    check: Check<'_>,
    visitor: &mut Visitor<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    let mut archive = Archive::new(reader);
    let mut limits = Limits::default();
    let mut global = HashMap::new();
    let mut local = HashMap::new();
    let mut long_name = None;
    let mut metadata = 0;
    for entry in archive.entries()?.raw(true) {
        check()?;
        let mut entry = entry?;
        let kind = entry.header().entry_type();
        if kind.is_pax_local_extensions()
            || kind.is_pax_global_extensions()
            || kind.is_gnu_longname()
        {
            if entry.size() > MAX_HEADER || entry.size() > MAX_HEADERS - metadata {
                return Err(invalid("TAR 확장 헤더의 크기 한도를 초과했습니다."));
            }
            metadata += entry.size();
            if kind.is_gnu_longname() {
                let mut bytes = Vec::new();
                entry.read_to_end(&mut bytes)?;
                let name = bytes.split(|&byte| byte == 0).next().unwrap_or_default();
                long_name = Some(
                    String::from_utf8(name.to_vec())
                        .map_err(|_| invalid("TAR 항목 이름은 UTF-8이어야 합니다."))?,
                );
            } else if let Some(fields) = entry.pax_extensions()? {
                let settings = if kind.is_pax_global_extensions() {
                    &mut global
                } else {
                    &mut local
                };
                for field in fields {
                    let field = field?;
                    if let (Ok(key), Ok(value)) = (field.key(), field.value())
                        && matches!(
                            key,
                            "path"
                                | "mtime"
                                | "size"
                                | "uid"
                                | "gid"
                                | "uname"
                                | "gname"
                                | "atime"
                                | "ctime"
                        )
                    {
                        settings.insert(key.to_owned(), value.to_owned());
                    }
                }
            }
            continue;
        }
        let directory = kind.is_dir();
        if !(directory || kind.is_file() || kind.is_contiguous()) {
            return Err(invalid("링크/장치 압축 항목은 지원하지 않습니다."));
        }
        let setting = |key| local.get(key).or_else(|| global.get(key));
        if setting("size").is_some_and(|raw| raw.parse::<u64>().ok() != Some(entry.size())) {
            return Err(invalid("TAR PAX 항목 크기가 헤더와 일치하지 않습니다."));
        }
        let name = setting("path")
            .cloned()
            .or_else(|| long_name.take())
            .map(Ok)
            .unwrap_or_else(|| {
                String::from_utf8(entry.path_bytes().into_owned())
                    .map_err(|_| invalid("TAR 항목 이름은 UTF-8이어야 합니다."))
            })?;
        long_name = None;
        let header = entry.header();
        let mode = header.mode()?;
        if header.uid().unwrap_or(0) != 0
            || header.gid().unwrap_or(0) != 0
            || mode & 0o7000 != 0
            || ["uid", "gid"]
                .iter()
                .any(|key| setting(key).is_some_and(|s| s != "0"))
            || ["uname", "gname"]
                .iter()
                .any(|key| setting(key).is_some_and(|s| !s.is_empty()))
            || header.username_bytes().is_some_and(|s| !s.is_empty())
            || header.groupname_bytes().is_some_and(|s| !s.is_empty())
        {
            notices.insert("TAR 항목의 소유권과 특수 권한 비트는 변환 결과에 적용하지 않습니다.");
        }
        if setting("atime").is_some() || setting("ctime").is_some() {
            notices.insert("TAR 항목의 접근·상태 변경 시각은 보존하지 않습니다.");
        }
        let modified = if let Some(raw) = setting("mtime") {
            pax_time(raw, notices)
        } else {
            header.mtime().ok().map(|value| i128::from(value) * NS)
        };
        if modified.is_none() {
            notices.insert("유효하지 않은 TAR 수정 시각은 보존하지 못했습니다.");
        }
        local.clear();
        if directory && matches!(name.as_str(), "." | "./") {
            continue;
        }
        let mut member = Member {
            name,
            size: if directory { 0 } else { entry.size() },
            directory,
            modified,
            mode: Some(mode & 0o777),
        };
        limits.accept(&mut member)?;
        visitor(&member, &mut entry, notices)?;
    }
    Ok(())
}

fn tar_header(prefix: &[u8]) -> bool {
    prefix.len() == 512
        && (prefix.iter().all(|&byte| byte == 0) || {
            let mut header = Header::new_ustar();
            header.as_mut_bytes().copy_from_slice(prefix);
            let checksum = header.cksum().ok();
            header.set_cksum();
            checksum.is_some() && checksum == header.cksum().ok()
        })
}

fn visit_compressed_tar(
    mut incoming: impl Read,
    check: Check<'_>,
    visitor: &mut Visitor<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    visit_tar(&mut incoming, check, visitor, notices)?;
    // Read through the integrity footer with a bound on non-member data.
    let mut buffer = [0; 8192];
    let mut trailing = 0;
    loop {
        check()?;
        let count = incoming.read(&mut buffer)?;
        if count == 0 {
            return Ok(());
        }
        trailing += count as u64;
        if trailing > MAX_HEADERS {
            return Err(invalid(LIMIT));
        }
    }
}

fn visit(
    path: &Path,
    input: &str,
    check: Check<'_>,
    visitor: &mut Visitor<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    if input == "rar" {
        return rar::visit(path, check, visitor, notices);
    }
    let mut file = File::open(path)?;
    if input == "zip" {
        return visit_zip(file, check, visitor, notices);
    }
    let mut prefix = [0; 6];
    file.read_exact(&mut prefix)?;
    file.rewind()?;
    if prefix == *b"\xfd7zXZ\0" {
        let decoder = XzReader::new_mem_limit(file, true, 1024 * 1024);
        return visit_compressed_tar(decoder, check, visitor, notices);
    }
    if prefix.starts_with(b"BZh") {
        return visit_compressed_tar(MultiBzDecoder::new(file), check, visitor, notices);
    }
    if prefix.starts_with(b"\x28\xb5\x2f\xfd")
        || (0x50..=0x5f).contains(&prefix[0]) && prefix[1..4] == [0x2a, 0x4d, 0x18]
    {
        let mut decoder = zstd::stream::read::Decoder::new(file)?;
        decoder.window_log_max(30)?;
        return visit_compressed_tar(decoder, check, visitor, notices);
    }
    if !prefix.starts_with(&[0x1f, 0x8b]) {
        return visit_tar(file, check, visitor, notices);
    }
    let mut decoder = MultiGzDecoder::new(file);
    let modified = decoder
        .header()
        .map(|header| header.mtime())
        .filter(|&time| time != 0)
        .map(|time| i128::from(time) * NS);
    let filename = decoder
        .header()
        .and_then(|header| header.filename())
        .map(|name| name.iter().copied().map(char::from).collect::<String>());
    let mut prefix = Vec::with_capacity(512);
    Read::by_ref(&mut decoder)
        .take(512)
        .read_to_end(&mut prefix)?;
    let mut incoming = Cursor::new(prefix.as_slice()).chain(decoder);
    if tar_header(&prefix) {
        return visit_compressed_tar(incoming, check, visitor, notices);
    }
    let mut name = safe_name(&path.file_stem().unwrap_or_default().to_string_lossy())?;
    if let Some(candidate) = filename {
        if candidate.chars().count() <= 255
            && !candidate.contains('/')
            && safe_name(&candidate).is_ok_and(|safe| safe == candidate)
        {
            name = candidate;
        } else {
            notices.insert("GZIP 내부 이름을 사용할 수 없어 압축 파일 이름으로 대체했습니다.");
        }
    }
    let mut spool = tempfile::tempfile()?;
    let mut size = 0;
    let mut buffer = vec![0; 1024 * 1024];
    loop {
        check()?;
        let count = incoming.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        size += count as u64;
        if size > MAX_BYTES {
            return Err(invalid("GZIP 해제 크기가 제한을 초과했습니다."));
        }
        spool.write_all(&buffer[..count])?;
    }
    spool.rewind()?;
    visitor(
        &Member {
            name,
            size,
            directory: false,
            modified,
            mode: None,
        },
        &mut spool,
        notices,
    )
}

fn zip_options(
    member: &Member,
    notices: &mut Notices,
) -> io::Result<FullFileOptions<'static, 'static>> {
    let mut options = FullFileOptions::default()
        .system(System::Unix)
        .unix_permissions(member.unix_permissions())
        .last_modified_time(DateTime::default())
        .compression_method(if member.directory {
            CompressionMethod::Stored
        } else {
            CompressionMethod::Deflated
        })
        .compression_level((!member.directory).then_some(6))
        .large_file(!member.directory);
    let Some(modified) = member.modified else {
        return Ok(options);
    };
    let seconds = modified.div_euclid(NS);
    let fraction = modified.rem_euclid(NS);
    if let Ok(stamp) = OffsetDateTime::from_unix_timestamp_nanos(seconds * NS)
        && let Ok(dos) = DateTime::try_from(PrimitiveDateTime::new(stamp.date(), stamp.time()))
    {
        options = options.last_modified_time(dos);
    }
    let unix = i32::try_from(seconds).ok();
    if let Some(seconds) = unix {
        let mut field = [0; 5];
        field[0] = 1;
        field[1..].copy_from_slice(&seconds.to_le_bytes());
        options.add_extra_field(0x5455, field, false)?;
    }
    if fraction != 0 || unix.is_none() {
        if let Ok(ticks) = u64::try_from(modified.div_euclid(100) + FILETIME_EPOCH)
            && ticks != 0
        {
            let mut field = [0; 32];
            field[4..8].copy_from_slice(&[1, 0, 24, 0]);
            field[8..16].copy_from_slice(&ticks.to_le_bytes());
            options.add_extra_field(0x000a, field, false)?;
            if modified % 100 != 0 {
                notices.insert("ZIP NTFS 확장 수정 시각의 정밀도는 100ns입니다.");
            }
        } else if unix.is_none() {
            notices.insert("ZIP이 표현할 수 없는 수정 시각은 기본값으로 저장했습니다.");
        }
    }
    Ok(options)
}

fn write_tar<W: Write>(
    source: &Path,
    input: &str,
    output: W,
    check: Check<'_>,
    notices: &mut Notices,
) -> io::Result<W> {
    let mut archive = Builder::new(output);
    visit(
        source,
        input,
        check,
        &mut |member, incoming, _| {
            let mut header = Header::new_ustar();
            header.set_entry_type(if member.directory {
                EntryType::Directory
            } else {
                EntryType::Regular
            });
            header.set_mode(member.unix_permissions());
            header.set_size(member.size);
            let mut headers = Vec::new();
            if header.set_path(&member.name).is_err() || !member.name.is_ascii() {
                headers.push(("path", member.name.as_bytes()));
                header.set_path("entry")?;
            }
            let precise;
            if let Some(modified) = member.modified {
                let seconds = modified.div_euclid(NS);
                header.set_mtime(u64::try_from(seconds).unwrap_or(0));
                if modified < 0 || modified % NS != 0 {
                    let value = modified.abs();
                    precise = format!(
                        "{}{}.{:09}",
                        if modified < 0 { "-" } else { "" },
                        value / NS,
                        value % NS
                    );
                    headers.push(("mtime", precise.as_bytes()));
                }
            }
            archive.append_pax_extensions(headers)?;
            header.set_cksum();
            if member.directory {
                archive.append(&header, io::empty())
            } else {
                let mut read = |buffer: &mut [u8]| {
                    // std::io::copy retries Interrupted. Cancellation is terminal here.
                    check().map_err(|error| {
                        if error.kind() == ErrorKind::Interrupted {
                            io::Error::other(error)
                        } else {
                            error
                        }
                    })?;
                    incoming.read(buffer)
                };
                let mut read = std::io::Read::take(CheckedRead(&mut read), member.size + 1);
                archive.append(&header, &mut read)?;
                if read.limit() != 1 {
                    return Err(invalid("압축 항목의 실제 크기가 올바르지 않습니다."));
                }
                Ok(())
            }
        },
        notices,
    )?;
    archive.into_inner()
}

struct CheckedRead<F>(F);
impl<F: FnMut(&mut [u8]) -> io::Result<usize>> Read for CheckedRead<F> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        (self.0)(buffer)
    }
}

pub fn convert(
    source: &Path,
    input: &str,
    target: &str,
    output: &mut File,
    check: Check<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    match target {
        "zip" => {
            let mut archive = ZipWriter::new(&mut *output);
            visit(
                source,
                input,
                check,
                &mut |member, incoming, notices| {
                    let options = zip_options(member, notices)?;
                    if member.directory {
                        archive.add_directory(&member.name, options)?;
                    } else {
                        archive.start_file(&member.name, options)?;
                        copy_stream(incoming, &mut archive, member.size, check, |_| Ok(()))?;
                    }
                    Ok(())
                },
                notices,
            )?;
            archive.finish()?;
        }
        "tar.gz" => {
            write_tar(
                source,
                input,
                GzEncoder::new(&mut *output, Compression::best()),
                check,
                notices,
            )?
            .finish()?;
        }
        _ => {
            write_tar(source, input, &mut *output, check, notices)?;
        }
    }
    if output.metadata()?.len() > MAX_BYTES {
        return Err(invalid(LIMIT));
    }
    check()
}

pub fn extract(
    source: &Path,
    input: &str,
    output: &Path,
    check: Check<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    let mut directories = Vec::new();
    let restore = |path: &Path, modified: i128, notices: &mut Notices| {
        let seconds = i64::try_from(modified.div_euclid(NS));
        let nanos = modified.rem_euclid(NS) as u32;
        if !seconds.is_ok_and(|seconds| {
            let stamp = FileTime::from_unix_time(seconds, nanos);
            filetime::set_file_mtime(path, stamp).is_ok()
                && fs::metadata(path)
                    .is_ok_and(|meta| FileTime::from_last_modification_time(&meta) == stamp)
        }) {
            notices.insert(MTIME_LOST);
        }
    };
    visit(
        source,
        input,
        check,
        &mut |member, incoming, notices| {
            let destination: PathBuf = member
                .name
                .split('/')
                .fold(output.to_owned(), |path, part| path.join(part));
            if member.mode.is_some() {
                notices.insert("압축 안의 POSIX 권한은 해제 폴더에 적용하지 않습니다.");
            }
            if member.directory {
                fs::create_dir_all(&destination)?;
                directories.push((destination, member.modified));
            } else {
                if member.size > free_space(output)?.saturating_sub(64 * 1024 * 1024) {
                    return Err(invalid("압축 해제에 필요한 디스크 공간이 부족합니다."));
                }
                fs::create_dir_all(destination.parent().expect("archive member parent"))?;
                let target = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .open(&destination)?;
                copy_stream(incoming, target, member.size, check, |_| Ok(()))?;
                if let Some(modified) = member.modified {
                    restore(&destination, modified, notices);
                }
            }
            Ok(())
        },
        notices,
    )?;
    for (directory, modified) in directories {
        check()?;
        if let Some(modified) = modified {
            restore(&directory, modified, notices);
        }
    }
    check()
}

#[cfg(windows)]
fn free_space(path: &Path) -> io::Result<u64> {
    use std::os::windows::ffi::OsStrExt;
    use std::ptr::null_mut;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    let path: Vec<u16> = path.as_os_str().encode_wide().chain([0]).collect();
    let mut available = 0;
    // The path buffer is NUL-terminated and valid for the duration of the call.
    if unsafe { GetDiskFreeSpaceExW(path.as_ptr(), &mut available, null_mut(), null_mut()) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(available)
}

#[cfg(target_os = "macos")]
fn free_space(path: &Path) -> io::Result<u64> {
    use std::ffi::CString;
    let path = CString::new(path.as_os_str().as_encoded_bytes())?;
    let mut stats = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    if unsafe { libc::statvfs(path.as_ptr(), stats.as_mut_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    let stats = unsafe { stats.assume_init() };
    Ok((stats.f_bavail as u64).saturating_mul(stats.f_frsize as u64))
}

#[cfg(not(any(windows, target_os = "macos")))]
fn free_space(_: &Path) -> io::Result<u64> {
    Err(io::Error::new(
        ErrorKind::Unsupported,
        "archive extraction requires Windows",
    ))
}
