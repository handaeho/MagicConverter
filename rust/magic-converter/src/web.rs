//! Frozen web input metadata, followed by bounded transfer and content identification.
use crate::{Catalog, engine_process, write_report};
use base64::{Engine, engine::general_purpose::STANDARD};
use percent_encoding::percent_decode_str;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{self, ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};
use url::Url;

pub const INLINE_LIMIT: usize = 32 * 1024 * 1024;
const DOWNLOAD_LIMIT: u64 = 512 * 1024 * 1024;
const WEB_INPUT_ERROR: &str = "웹 파일은 인증 정보가 없는 HTTP/HTTPS 주소로 지정하세요.";

#[derive(Clone, PartialEq, Eq)]
pub struct Input {
    name: String,
    source: Source,
    hash: [u8; 32],
}

#[derive(Clone, PartialEq, Eq)]
enum Source {
    Url(String),
    Bytes(Arc<[u8]>),
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

pub fn safe_name(value: &str) -> String {
    let value: String = percent_decode_str(value)
        .decode_utf8_lossy()
        .chars()
        .map(|ch| {
            if ch < ' ' || r#"<>:"/\|?*"#.contains(ch) {
                '_'
            } else {
                ch
            }
        })
        .collect();
    let mut result: String = value.trim_matches([' ', '.']).chars().take(160).collect();
    let upper = result
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    if result.is_empty()
        || matches!(upper.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ["COM", "LPT"].iter().any(|prefix| {
            upper
                .strip_prefix(prefix)
                .is_some_and(|number| number.len() == 1 && ("1"..="9").contains(&number))
        })
    {
        result.insert_str(0, "download_");
    }
    result
}

fn valid_url(value: &str) -> io::Result<Url> {
    if value.chars().count() > 16000 || value.chars().any(|ch| ch < ' ' || ch == '\u{7f}') {
        return Err(invalid(WEB_INPUT_ERROR));
    }
    let url = Url::parse(value).map_err(|_| invalid(WEB_INPUT_ERROR))?;
    let authority = value
        .split_once("://")
        .map(|(_, rest)| rest.split(['/', '?', '#']).next().unwrap_or(""));
    if !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || authority.is_none_or(|value| value.contains('@') || value.contains('\\'))
    {
        return Err(invalid(WEB_INPUT_ERROR));
    }
    Ok(url)
}

impl Input {
    pub fn is_argument(value: &str) -> bool {
        ["https:", "http:", "data:"]
            .iter()
            .any(|prefix| value.starts_with(prefix))
    }

    pub fn url(value: &str, name: Option<&str>, image_hint: bool) -> io::Result<Self> {
        if value.starts_with("data:") {
            let (header, body) = value
                .split_once(',')
                .ok_or_else(|| invalid("지원하는 이미지 data URL이 아닙니다."))?;
            let extension = match header {
                "data:image/png;base64" => "png",
                "data:image/jpeg;base64" => "jpg",
                "data:image/gif;base64" => "gif",
                "data:image/webp;base64" => "webp",
                _ => return Err(invalid("지원하는 이미지 data URL이 아닙니다.")),
            };
            if body.len() > 44 * 1024 * 1024 {
                return Err(invalid("지원하는 이미지 data URL이 아닙니다."));
            }
            let bytes = STANDARD
                .decode(body)
                .map_err(|error| invalid(error.to_string()))?;
            return Self::bytes(&format!("image.{extension}"), bytes);
        }
        let url = valid_url(value)?;
        let mut name =
            safe_name(name.unwrap_or_else(|| url.path().rsplit('/').next().unwrap_or_default()));
        if Path::new(&name).extension().is_none() {
            name.push_str(if image_hint { ".jpg" } else { ".bin" });
        }
        Ok(Self {
            name,
            source: Source::Url(value.to_owned()),
            hash: Sha256::digest(value.as_bytes()).into(),
        })
    }

    pub fn bytes(name: &str, bytes: Vec<u8>) -> io::Result<Self> {
        if bytes.is_empty() || bytes.len() > INLINE_LIMIT {
            return Err(invalid("드래그 이미지 데이터는 32 MiB까지 지원합니다."));
        }
        let hash = Sha256::digest(&bytes).into();
        Ok(Self {
            name: safe_name(name),
            source: Source::Bytes(bytes.into()),
            hash,
        })
    }

    pub fn placeholder(&self) -> PathBuf {
        let hash: String = self.hash[..12]
            .iter()
            .map(|part| format!("{part:02x}"))
            .collect();
        std::env::temp_dir()
            .join("magic-web-placeholder")
            .join(hash)
            .join(&self.name)
    }

    pub fn materialize(
        &self,
        catalog: &Catalog,
        directory: &Path,
        check: &dyn Fn() -> io::Result<()>,
    ) -> io::Result<PathBuf> {
        let input = directory.join("input");
        fs::create_dir(&input)?;
        match &self.source {
            Source::Bytes(bytes) => {
                let temp = input.join("transfer.partial");
                let mut stream = File::create(&temp)?;
                for part in bytes.chunks(65536) {
                    check()?;
                    stream.write_all(part)?;
                }
                drop(stream);
                let path = input.join(identify(catalog, &temp, &self.name, "")?);
                fs::rename(temp, &path)?;
                Ok(path)
            }
            Source::Url(url) => {
                let request = directory.join("request.json");
                let report = directory.join("report.json");
                write_report(&request, &json!({"url": url, "name": self.name}))?;
                let args: Vec<OsString> = [
                    "web-fetch".into(),
                    request.into_os_string(),
                    report.as_os_str().into(),
                    input.as_os_str().into(),
                ]
                .into();
                let result = engine_process::run(
                    &std::env::current_exe()?,
                    &args,
                    Some(Duration::from_secs(600)),
                    None,
                    check,
                    &|| Ok(()),
                )?;
                let output: Value = File::open(&report)
                    .ok()
                    .and_then(|file| serde_json::from_reader(file.take(65536)).ok())
                    .unwrap_or(Value::Null);
                if result.code != 0 || output["ok"] != true {
                    return Err(invalid(
                        output["error"]
                            .as_str()
                            .unwrap_or("웹 파일 다운로드가 실패했습니다."),
                    ));
                }
                let name = output["name"]
                    .as_str()
                    .filter(|name| *name == safe_name(name))
                    .ok_or_else(|| invalid("웹 파일의 다운로드 경로가 올바르지 않습니다."))?;
                let path = input.join(name);
                if !path.is_file() {
                    return Err(invalid("웹 파일의 다운로드 경로가 올바르지 않습니다."));
                }
                Ok(path)
            }
        }
    }
}

fn identify(catalog: &Catalog, path: &Path, name: &str, mime: &str) -> io::Result<String> {
    let mut head = Vec::new();
    File::open(path)?.take(8192).read_to_end(&mut head)?;
    let hint = Path::new(name);
    let ext = hint
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if matches!(
        ext.as_str(),
        "exe"
            | "com"
            | "scr"
            | "dll"
            | "msi"
            | "lnk"
            | "url"
            | "cmd"
            | "bat"
            | "ps1"
            | "js"
            | "vbs"
            | "hta"
            | "py"
    ) || head.starts_with(b"MZ")
        || head.starts_with(b"\x7fELF")
    {
        return Err(invalid("웹 드래그에서 실행 파일은 받지 않습니다."));
    }
    let html = head.trim_ascii_start().to_ascii_lowercase();
    let mime = mime
        .split(';')
        .next()
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    if matches!(mime.as_str(), "text/html" | "application/xhtml+xml")
        || [b"<!doctype html".as_slice(), b"<html", b"<head", b"<script"]
            .iter()
            .any(|tag| html.starts_with(tag))
    {
        return Err(invalid(
            "파일 대신 웹페이지가 도착했습니다. 로그인 없이 받을 수 있는 파일 주소를 사용하세요.",
        ));
    }
    let signatures: &[(&[u8], &str)] = &[
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpg"),
        (b"GIF8", "gif"),
        (b"%PDF-", "pdf"),
        (b"II*\0", "tiff"),
        (b"MM\0*", "tiff"),
        (b"BM", "bmp"),
        (b"fLaC", "flac"),
        (b"ID3", "mp3"),
        (b"OggS", "ogg"),
        (b"wOFF", "woff"),
        (b"wOF2", "woff2"),
        (b"OTTO", "otf"),
        (b"\0\x01\0\0", "ttf"),
    ];
    let mut format = signatures
        .iter()
        .find_map(|(signature, kind)| head.starts_with(signature).then_some(*kind));
    if head.starts_with(b"RIFF") {
        format = match head.get(8..12) {
            Some(b"WEBP") => Some("webp"),
            Some(b"WAVE") => Some("wav"),
            Some(b"AVI ") => Some("avi"),
            _ => None,
        };
    }
    if head.len() > 12 && &head[4..8] == b"ftyp" {
        format = Some(match &head[8..12] {
            b"avif" | b"avis" => "avif",
            b"heic" | b"heix" | b"mif1" => "heic",
            b"M4A " => "m4a",
            _ => "mp4",
        });
    }
    if let Ok(archive) = zip::ZipArchive::new(File::open(path)?) {
        let markers = [
            ("word/document.xml", "docx"),
            ("ppt/presentation.xml", "pptx"),
            ("xl/workbook.xml", "xlsx"),
            ("Contents/section0.xml", "hwpx"),
            ("META-INF/container.xml", "epub"),
        ];
        format = Some(
            markers
                .iter()
                .find_map(|(marker, kind)| {
                    archive.index_for_name(marker).is_some().then_some(*kind)
                })
                .unwrap_or_else(|| match ext.as_str() {
                    "cbz"
                        if archive.file_names().filter_map(Result::ok).any(|member| {
                            matches!(
                                Path::new(member.as_ref())
                                    .extension()
                                    .and_then(|ext| ext.to_str())
                                    .unwrap_or_default()
                                    .to_ascii_lowercase()
                                    .as_str(),
                                "png" | "jpg" | "jpeg" | "webp" | "gif" | "bmp"
                            )
                        }) =>
                    {
                        "cbz"
                    }
                    "kmz"
                        if archive
                            .file_names()
                            .filter_map(Result::ok)
                            .any(|member| member.to_ascii_lowercase().ends_with(".kml")) =>
                    {
                        "kmz"
                    }
                    _ => "zip",
                }),
        );
    }
    let format = if let Some(format) = format {
        format
    } else {
        let (kind, _) = catalog
            .detect(hint)
            .filter(|_| !head.is_empty())
            .ok_or_else(|| invalid("웹 파일의 실제 형식을 식별하지 못했습니다."))?;
        if !matches!(
            kind.id.as_str(),
            "txt"
                | "md"
                | "csv"
                | "tsv"
                | "srt"
                | "vtt"
                | "svg"
                | "hwp"
                | "mp3"
                | "webm"
                | "mkv"
                | "tar"
                | "gz"
                | "tar.gz"
                | "rar"
                | "wma"
                | "wmv"
                | "aiff"
                | "json"
                | "jsonl"
                | "yaml"
                | "toml"
                | "xml"
                | "fb2"
                | "eml"
                | "mhtml"
                | "obj"
                | "stl"
                | "ply"
                | "geojson"
                | "kml"
                | "gpx"
        ) {
            return Err(invalid("파일 이름과 실제 파일 헤더가 일치하지 않습니다."));
        }
        &kind.extensions[0]
    };
    Ok(format!(
        "{}.{format}",
        safe_name(
            hint.file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or_default()
        )
    ))
}

fn attachment_name(header: &str) -> Option<String> {
    let mut standard = None;
    for part in header.split(';').skip(1) {
        let Some((key, value)) = part.trim().split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"');
        if key.eq_ignore_ascii_case("filename*") {
            if let Some((encoding, tail)) = value.split_once('\'')
                && encoding.eq_ignore_ascii_case("utf-8")
                && let Some((_, name)) = tail.split_once('\'')
            {
                return Some(name.to_owned());
            }
        } else if key.eq_ignore_ascii_case("filename") {
            standard = Some(value.to_owned());
        }
    }
    standard
}

fn fetch(catalog: &Catalog, value: &str, name: &str, folder: &Path) -> io::Result<String> {
    use ureq::tls::{RootCerts, TlsConfig, TlsProvider};
    let deadline = Duration::from_secs(15);
    let tls = TlsConfig::builder()
        .provider(TlsProvider::NativeTls)
        .root_certs(RootCerts::PlatformVerifier)
        .build();
    let config = ureq::Agent::config_builder()
        .tls_config(tls)
        .max_redirects(0)
        .http_status_as_error(false)
        .timeout_connect(Some(deadline))
        .timeout_recv_response(Some(deadline))
        .timeout_per_read(Some(deadline))
        .build();
    let agent = ureq::Agent::new_with_config(config);
    let mut url = valid_url(value)?;
    let mut response = None;
    for hop in 0..=5 {
        let current = agent
            .get(url.as_str())
            .header("User-Agent", "MagicConverter/6")
            .header("Accept-Encoding", "identity")
            .call()
            .map_err(io::Error::other)?;
        if current.status().as_u16() == 200 {
            response = Some(current);
            break;
        }
        if !matches!(current.status().as_u16(), 301 | 302 | 303 | 307 | 308) || hop == 5 {
            return Err(invalid(format!(
                "다운로드 HTTP {}",
                current.status().as_u16()
            )));
        }
        let location = current
            .headers()
            .get("Location")
            .and_then(|item| item.to_str().ok())
            .ok_or_else(|| invalid(format!("다운로드 HTTP {}", current.status().as_u16())))?;
        let next = url.join(location).map_err(|_| invalid(WEB_INPUT_ERROR))?;
        let next = valid_url(next.as_str())?;
        if url.scheme() == "https" && next.scheme() != "https" {
            return Err(invalid(
                "HTTPS에서 HTTP로 내려가는 리디렉션은 받지 않습니다.",
            ));
        }
        url = next;
    }
    let mut response = response.ok_or_else(|| invalid("웹 파일 다운로드가 실패했습니다."))?;
    let header = |name| {
        response
            .headers()
            .get(name)
            .and_then(|value| value.to_str().ok())
    };
    let expected = header("Content-Length")
        .map(|value| {
            value
                .parse::<u64>()
                .ok()
                .filter(|&count| count <= DOWNLOAD_LIMIT)
                .ok_or_else(|| invalid("다운로드 크기 제한을 초과했습니다."))
        })
        .transpose()?;
    let name = safe_name(
        &header("Content-Disposition")
            .and_then(attachment_name)
            .unwrap_or_else(|| name.to_owned()),
    );
    let mime = header("Content-Type").unwrap_or_default().to_owned();
    let temp = folder.join("transfer.partial");
    let mut output = File::options().write(true).create_new(true).open(&temp)?;
    let mut data = response
        .body_mut()
        .with_config()
        .limit(DOWNLOAD_LIMIT + 1)
        .reader();
    let mut bytes = [0; 65536];
    let mut received = 0_u64;
    let start = Instant::now();
    loop {
        if start.elapsed() >= Duration::from_secs(600) {
            return Err(io::Error::new(
                ErrorKind::TimedOut,
                "웹 다운로드 제한 시간을 초과했습니다.",
            ));
        }
        let count = data.read(&mut bytes).map_err(|error| {
            if expected.is_some_and(|size| received < size)
                && matches!(
                    error.kind(),
                    ErrorKind::UnexpectedEof | ErrorKind::ConnectionReset
                )
            {
                invalid("다운로드가 완료되기 전에 연결이 끊어졌습니다.")
            } else {
                error
            }
        })?;
        if count == 0 {
            break;
        }
        received += count as u64;
        if received > DOWNLOAD_LIMIT {
            return Err(invalid("다운로드 크기 제한을 초과했습니다."));
        }
        output.write_all(&bytes[..count])?;
    }
    if expected.is_some_and(|size| size != received) {
        return Err(invalid("다운로드가 완료되기 전에 연결이 끊어졌습니다."));
    }
    output.sync_all()?;
    drop(output);
    let actual = identify(catalog, &temp, &name, &mime)?;
    fs::rename(temp, folder.join(&actual))?;
    Ok(actual)
}

pub fn worker(catalog: &Catalog, request: &Path, report: &Path, folder: &Path) -> io::Result<u8> {
    let request: Value = serde_json::from_reader(File::open(request)?.take(131072))?;
    let result = fetch(
        catalog,
        request["url"].as_str().unwrap_or_default(),
        request["name"].as_str().unwrap_or("download"),
        folder,
    );
    let (code, report_value) = match result {
        Ok(name) => (0, json!({"ok": true, "name": name})),
        Err(error) => (1, json!({"ok": false, "error": error.to_string()})),
    };
    write_report(report, &report_value)?;
    Ok(code)
}
