use encoding_rs::{EUC_KR, UTF_16BE, UTF_16LE};
use regex::Regex;
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::path::Path;

const BYTE_LIMIT: u64 = 64 * 1024 * 1024;
const TIME: &str = r"(?:[0-9]{2,}:)?[0-9]{2}:[0-9]{2}[.,][0-9]{3}";

struct Cue<'a> {
    start: i64,
    end: i64,
    text: String,
    identifier: &'a str,
    settings: &'a str,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn decode(bytes: &[u8]) -> io::Result<String> {
    let error = || invalid("텍스트 인코딩을 인식할 수 없습니다.");
    let utf16 = |data: &[u8], encoding: &'static encoding_rs::Encoding| {
        encoding
            .decode_without_bom_handling_and_without_replacement(data)
            .filter(|text| !text.contains('\0'))
            .map(|text| text.into_owned())
    };
    if let Some(body) = bytes.strip_prefix(b"\xff\xfe") {
        return utf16(body, UTF_16LE).ok_or_else(error);
    }
    if let Some(body) = bytes.strip_prefix(b"\xfe\xff") {
        return utf16(body, UTF_16BE).ok_or_else(error);
    }
    if bytes.contains(&0) {
        let (even, odd) = bytes
            .iter()
            .take(8192)
            .enumerate()
            .filter(|(_, byte)| **byte == 0)
            .fold((0, 0), |(even, odd), (index, _)| {
                if index % 2 == 0 {
                    (even + 1, odd)
                } else {
                    (even, odd + 1)
                }
            });
        if bytes.len().is_multiple_of(2) && even.max(odd) > 3 * even.min(odd) {
            return utf16(bytes, if odd > even { UTF_16LE } else { UTF_16BE }).ok_or_else(error);
        }
        return Err(error());
    }
    if let Ok(text) = std::str::from_utf8(bytes.strip_prefix(b"\xef\xbb\xbf").unwrap_or(bytes)) {
        return Ok(text.to_owned());
    }
    if let Some(text) = EUC_KR.decode_without_bom_handling_and_without_replacement(bytes) {
        return Ok(text.into_owned());
    }
    utf16(bytes, UTF_16LE).ok_or_else(error)
}

fn parse_time(text: &str) -> io::Result<i64> {
    let bad_time = || invalid("자막 구간의 시간 형식이 올바르지 않습니다.");
    let time = text.replace([',', '.'], ":");
    let mut parts = time.rsplit(':').map(str::parse::<i64>);
    let mut take = || parts.next().and_then(Result::ok).ok_or_else(bad_time);
    let millis = take()?;
    let seconds = take()?;
    let minutes = take()?;
    let hours = parts
        .next()
        .transpose()
        .map_err(|_| bad_time())?
        .unwrap_or(0);
    if minutes >= 60 || seconds >= 60 {
        return Err(invalid("자막의 분·초 값은 60보다 작아야 합니다."));
    }
    hours
        .checked_mul(3_600_000)
        .and_then(|value| value.checked_add(minutes * 60_000 + seconds * 1000 + millis))
        .ok_or_else(bad_time)
}

fn format_time(value: i64, separator: char) -> String {
    let millis = value % 1000;
    let seconds = value / 1000;
    format!(
        "{:02}:{:02}:{:02}{separator}{millis:03}",
        seconds / 3600,
        (seconds / 60) % 60,
        seconds % 60
    )
}

pub fn convert(
    source: &Path,
    output: &mut File,
    target: &str,
    offset: i64,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    let file = File::open(source)?;
    if file.metadata()?.len() > BYTE_LIMIT {
        return Err(invalid("자막 파일이 64 MiB 제한을 초과했습니다."));
    }
    let mut bytes = Vec::new();
    file.take(BYTE_LIMIT + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > BYTE_LIMIT {
        return Err(invalid("자막 파일이 64 MiB 제한을 초과했습니다."));
    }
    check()?;
    let normalized = decode(&bytes)?.replace("\r\n", "\n").replace('\r', "\n");
    let text = normalized.trim_start_matches('\u{feff}');
    let delimiter = Regex::new(r"\n\s*\n").expect("static block delimiter");
    let timestamps = Regex::new(&format!(r"^({TIME})\s*-->\s*({TIME})(\s+.*)?$"))
        .expect("static cue timestamp pattern");
    let inline = Regex::new(&format!(r"<({TIME})>")).expect("static inline timestamp pattern");
    let markup = Regex::new(r"<[^>]+>").expect("static markup pattern");
    let mut cues = Vec::new();
    let mut headers = Vec::new();
    for block in delimiter.split(text) {
        check()?;
        let mut lines = block.trim().lines();
        let Some(first) = lines.next() else { continue };
        if first.starts_with("WEBVTT")
            || matches!(first, "STYLE" | "REGION" | "NOTE")
            || first.starts_with("NOTE ")
        {
            headers.push(block.trim());
            continue;
        }
        let (identifier, captures) = if let Some(captures) = timestamps.captures(first.trim()) {
            ("", captures)
        } else if let Some(second) = lines.next()
            && let Some(captures) = timestamps.captures(second.trim())
        {
            (first, captures)
        } else {
            return Err(invalid("자막 구간의 시간 형식이 올바르지 않습니다."));
        };
        let start = parse_time(&captures[1])?;
        let end = parse_time(&captures[2])?;
        let content = lines.collect::<Vec<_>>().join("\n");
        if end <= start || content.is_empty() {
            return Err(invalid(
                "자막 구간은 종료가 시작보다 늦고 본문이 있어야 합니다.",
            ));
        }
        cues.push(Cue {
            start,
            end,
            text: content,
            identifier,
            settings: captures.get(3).map_or("", |part| part.as_str().trim()),
        });
        if cues.len() > 100_000 {
            return Err(invalid("자막 구간이 100,000개 제한을 초과했습니다."));
        }
    }
    if cues.is_empty() {
        return Err(invalid("유효한 SRT/VTT 자막 구간을 찾지 못했습니다."));
    }
    let shift = |value: i64| {
        value
            .checked_add(offset)
            .ok_or_else(|| invalid("자막 구간의 시간 형식이 올바르지 않습니다."))
    };
    let mut blocks = Vec::new();
    let mut dropped = 0;
    for cue in &cues {
        check()?;
        let start = shift(cue.start)?.max(0);
        let end = shift(cue.end)?;
        if end <= 0 {
            dropped += 1;
            continue;
        }
        let mut content = cue.text.clone();
        if offset != 0 {
            let mut shifted = String::new();
            let mut end = 0;
            for item in inline.captures_iter(&content) {
                let matched = item.get(0).expect("entire inline match");
                shifted.push_str(&content[end..matched.start()]);
                shifted.push_str(&format!(
                    "<{}>",
                    format_time(shift(parse_time(&item[1])?)?.max(0), '.')
                ));
                end = matched.end();
            }
            shifted.push_str(&content[end..]);
            content = shifted;
        }
        blocks.push(match target {
            "srt" => format!(
                "{}\n{} --> {}\n{content}",
                blocks.len() + 1,
                format_time(start, ','),
                format_time(end, ',')
            ),
            "vtt" => {
                let id = if cue.identifier.is_empty() {
                    String::new()
                } else {
                    format!("{}\n", cue.identifier)
                };
                let settings = if cue.settings.is_empty() {
                    String::new()
                } else {
                    format!(" {}", cue.settings)
                };
                format!(
                    "{id}{} --> {}{settings}\n{content}",
                    format_time(start, '.'),
                    format_time(end, '.')
                )
            }
            "txt" => {
                html_escape::decode_html_entities(&markup.replace_all(&content, "")).into_owned()
            }
            _ => return Err(invalid("지원하지 않는 자막 출력입니다.")),
        });
    }
    if blocks.is_empty() {
        return Err(invalid("시간 이동 후 남은 자막 구간이 없습니다."));
    }
    if dropped > 0 {
        notices.push(format!(
            "시간 이동 후 0초 이전에 끝나는 자막 {dropped}개를 제외했습니다."
        ));
    }
    if target == "srt"
        && (cues.iter().any(|cue| !cue.settings.is_empty())
            || headers
                .iter()
                .any(|header| header.starts_with("STYLE") || header.starts_with("REGION")))
    {
        notices.push("SRT가 지원하지 않는 WebVTT 위치·스타일·영역 설정은 제외했습니다.".to_owned());
    }
    let newline = if cfg!(windows) { "\r\n" } else { "\n" };
    if target == "vtt" {
        if !headers
            .first()
            .is_some_and(|header| header.starts_with("WEBVTT"))
        {
            headers.insert(0, "WEBVTT");
        }
        for header in headers {
            check()?;
            write!(
                output,
                "{}{newline}{newline}",
                header.replace('\n', newline)
            )?;
        }
    }
    let last = blocks.len() - 1;
    for (index, block) in blocks.iter().enumerate() {
        check()?;
        write!(output, "{}{newline}", block.replace('\n', newline))?;
        if index != last {
            output.write_all(newline.as_bytes())?;
        }
    }
    Ok(())
}
