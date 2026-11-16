use crate::safe_html;
use encoding_rs::{EUC_KR, Encoding, UTF_8, WINDOWS_1252};
use mailparse::{DispositionType, MailHeaderMap, ParsedMail};
use percent_encoding::percent_decode_str;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, ErrorKind, Write};
use std::path::Path;
use unicode_casefold::UnicodeCaseFold;
use zip::write::SimpleFileOptions;

type Check<'a> = &'a dyn Fn() -> io::Result<()>;

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn decode(data: &[u8], charset: &str) -> io::Result<String> {
    Encoding::for_label(charset.as_bytes())
        .into_iter()
        .chain([UTF_8, EUC_KR, WINDOWS_1252])
        .find_map(|encoding| encoding.decode_without_bom_handling_and_without_replacement(data))
        .map(|text| text.into_owned())
        .ok_or_else(|| invalid("메일 문자 인코딩을 해석할 수 없습니다."))
}

fn from_raw<'a>(raw: &'a [u8], check: Check<'_>) -> io::Result<ParsedMail<'a>> {
    let message = mailparse::parse_mail(raw).map_err(io::Error::other)?;
    if message.headers.is_empty() {
        return Err(invalid("메일/MHTML 헤더가 없습니다."));
    }
    for (index, part) in message.parts().enumerate() {
        check()?;
        if index >= 5000 {
            return Err(invalid("MIME 파트가 너무 많습니다."));
        }
        if matches!(
            part.ctype.mimetype.as_str(),
            "multipart/encrypted" | "application/pkcs7-mime" | "application/x-pkcs7-mime"
        ) {
            return Err(invalid(
                "암호화된 메일은 해독 후 사용하세요. 서명/암호화 해제는 지원하지 않습니다.",
            ));
        }
        if part.ctype.mimetype.starts_with("multipart/") {
            let boundary = part
                .ctype
                .params
                .get("boundary")
                .ok_or_else(|| invalid("메일/MHTML MIME 경계가 없습니다."))?;
            let end = format!("--{boundary}--");
            if !part
                .raw_bytes
                .split(|byte| *byte == b'\n')
                .any(|line| line.trim_ascii_end() == end.as_bytes())
            {
                return Err(invalid(
                    "메일/MHTML MIME 구조가 중단되었거나 잘못되었습니다.",
                ));
            }
        }
    }
    Ok(message)
}

fn header_values(message: &ParsedMail<'_>) -> Vec<(String, String)> {
    message
        .headers
        .iter()
        .filter_map(|header| {
            let key = header.get_key_ref();
            matches!(
                key.to_ascii_lowercase().as_str(),
                "from" | "to" | "cc" | "bcc" | "date" | "subject"
            )
            .then(|| (key.into_owned(), header.get_value()))
        })
        .collect()
}

fn payload(part: &ParsedMail<'_>) -> io::Result<Vec<u8>> {
    let data = part.get_body_raw().map_err(io::Error::other)?;
    if part.ctype.mimetype != "message/rfc822" {
        return Ok(data);
    }
    let mut result = Vec::with_capacity(data.len());
    for (index, &value) in data.iter().enumerate() {
        if value == b'\n' && (index == 0 || data[index - 1] != b'\r') {
            result.push(b'\r');
        }
        result.push(value);
    }
    Ok(result)
}

fn attachment(part: &ParsedMail<'_>) -> Option<String> {
    let disposition = part.get_content_disposition();
    disposition
        .params
        .get("filename")
        .or_else(|| part.ctype.params.get("name"))
        .filter(|name| !name.is_empty())
        .cloned()
        .or_else(|| {
            if part.ctype.mimetype == "message/rfc822" {
                Some("attached.eml".to_owned())
            } else if disposition.disposition == DispositionType::Attachment {
                Some("attachment.bin".to_owned())
            } else {
                None
            }
        })
}

fn attachments<'a>(part: &'a ParsedMail<'a>, output: &mut Vec<(String, &'a ParsedMail<'a>)>) {
    if let Some(name) = attachment(part) {
        output.push((name, part));
    } else if part.ctype.mimetype.starts_with("image/") {
        output.push(("inline-image.bin".to_owned(), part));
    } else {
        for child in &part.subparts {
            attachments(child, output);
        }
    }
}

fn archive(
    raw: &[u8],
    message: &ParsedMail<'_>,
    headers: &[(String, String)],
    output: &Path,
    check: Check<'_>,
) -> io::Result<()> {
    let mut files = Vec::new();
    attachments(message, &mut files);
    let mut manifest = Vec::new();
    let mut target = zip::ZipWriter::new(File::create(output)?);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    target.start_file("original.eml", options)?;
    target.write_all(raw)?;
    for (index, (name, part)) in files.into_iter().enumerate() {
        check()?;
        let data = payload(part)?;
        let clean: String = name
            .chars()
            .map(|ch| {
                if ch < ' ' || "<>:\"/\\|?*".contains(ch) {
                    '_'
                } else {
                    ch
                }
            })
            .collect();
        let clean: String = clean.trim_matches([' ', '.']).chars().take(120).collect();
        let path = format!(
            "attachments/{:04}_{}",
            index + 1,
            if clean.is_empty() {
                "attachment.bin"
            } else {
                &clean
            }
        );
        target.start_file(&path, options)?;
        target.write_all(&data)?;
        manifest.push(
            json!({"original_name":name, "path":path, "mime":part.ctype.mimetype,
            "content_id":part.headers.get_first_value("Content-ID").unwrap_or_default(),
            "bytes":data.len(), "sha256":format!("{:x}", Sha256::digest(&data))}),
        );
    }
    target.start_file("manifest.json", options)?;
    serde_json::to_writer_pretty(
        &mut target,
        &json!({"headers":headers,"attachments":manifest}),
    )?;
    target.finish()?.sync_all()?;
    check()
}

fn choose_body<'a>(part: &'a ParsedMail<'a>, chosen: &mut Option<&'a ParsedMail<'a>>) {
    if part.get_content_disposition().disposition == DispositionType::Attachment {
        return;
    }
    match part.ctype.mimetype.as_str() {
        "text/html" => {
            if chosen.is_none_or(|prior| prior.ctype.mimetype == "text/plain") {
                *chosen = Some(part);
            }
        }
        "text/plain" if chosen.is_none() => {
            *chosen = Some(part);
        }
        "multipart/related" => {
            let root = part
                .ctype
                .params
                .get("start")
                .and_then(|id| {
                    part.subparts.iter().find(|child| {
                        child.headers.get_first_value("Content-ID").as_ref() == Some(id)
                    })
                })
                .or_else(|| part.subparts.first());
            if let Some(root) = root {
                choose_body(root, chosen);
            }
        }
        kind if kind.starts_with("multipart/") => {
            for child in &part.subparts {
                choose_body(child, chosen);
            }
        }
        _ => {}
    }
}

fn body(message: &ParsedMail<'_>, warnings: &mut Vec<String>) -> io::Result<String> {
    let mut selected = None;
    choose_body(message, &mut selected);
    let selected =
        selected.ok_or_else(|| invalid("메일/MHTML에서 표시할 본문을 찾지 못했습니다."))?;
    let text = decode(&payload(selected)?, &selected.ctype.charset)?;
    if selected.ctype.mimetype == "text/plain" {
        return Ok(format!("<pre>{}</pre>", safe_html::escape(&text)));
    }
    let mut resources = HashMap::new();
    for part in message
        .parts()
        .filter(|part| part.ctype.mimetype.starts_with("image/"))
    {
        if let Some(id) = part
            .headers
            .get_first_value("Content-ID")
            .filter(|value| !value.is_empty())
        {
            resources.insert(
                format!(
                    "cid:{}",
                    id.trim_matches(['<', '>']).case_fold().collect::<String>()
                ),
                part,
            );
        }
        if let Some(location) = part
            .headers
            .get_first_value("Content-Location")
            .filter(|value| !value.is_empty())
        {
            resources.insert(location, part);
        }
    }
    warnings.push("메일 HTML의 스크립트·CSS·폼·외부 이미지/리소스를 제거했습니다. CID/Content-Location 내장 래스터 이미지는 포함합니다.".to_owned());
    safe_html::sanitize(&text, &mut |src| {
        let mut key = src.to_owned();
        if src
            .get(..4)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("cid:"))
        {
            key = format!(
                "cid:{}",
                percent_decode_str(&src[4..])
                    .decode_utf8_lossy()
                    .case_fold()
                    .collect::<String>()
            );
        }
        resources
            .get(&key)
            .map(|part| safe_html::image_uri(&payload(part)?))
            .transpose()
    })
}

pub fn convert(
    source: &Path,
    output: &Path,
    input: &str,
    target: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let raw = crate::data::bytes(source)?;
    let message = from_raw(&raw, check)?;
    let headers = header_values(&message);
    if target == "zip" {
        archive(&raw, &message, &headers, output, check)?;
        warnings.push("원본 EML과 첨부파일의 디코딩된 바이트를 ZIP에 보관했습니다. 서명은 검증하지 않습니다. 중첩 메일 첨부는 MIME로 다시 직렬화합니다.".to_owned());
        return Ok(());
    }
    let body = body(&message, warnings)?;
    if input == "eml" {
        warnings.push("HTML/TXT에는 일반 첨부파일이 포함되지 않습니다. 메일 보관 ZIP을 사용하면 원본과 첨부를 함께 보존합니다. 메일 서명은 검증하지 않습니다.".to_owned());
    }
    let text = if target == "html" {
        let head = headers
            .iter()
            .map(|(key, value)| {
                format!(
                    "<tr><th>{}</th><td>{}</td></tr>",
                    safe_html::escape(key),
                    safe_html::escape(value)
                )
            })
            .collect::<String>();
        let stem = source.file_stem().unwrap_or_default().to_string_lossy();
        let title = headers
            .iter()
            .find(|(key, _)| key.eq_ignore_ascii_case("subject"))
            .map_or(stem.as_ref(), |(_, value)| value);
        safe_html::page(title, &format!("<table>{head}</table><hr>{body}"))
    } else {
        let head = headers
            .iter()
            .map(|(key, value)| format!("{key}: {value}"))
            .collect::<Vec<_>>()
            .join("\n");
        format!("{head}\n\n{}", safe_html::text(&body, true))
    };
    check()?;
    fs::write(output, text)?;
    check()
}
