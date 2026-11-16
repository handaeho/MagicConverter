use crate::{data, safe_html};
use encoding_rs::{Encoding, UTF_16BE, UTF_16LE, WINDOWS_1252};
use pulldown_cmark::{Options, Parser, html};
use scraper::{Html, Selector};
use std::fs::{self, File};
use std::io::{self, Cursor, ErrorKind, Read, Write};
use std::path::Path;
use std::sync::LazyLock;
use zip::write::SimpleFileOptions;

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

pub(crate) fn decode(bytes: &[u8]) -> io::Result<String> {
    let bad = || invalid("텍스트 인코딩을 인식할 수 없습니다.");
    let text = if bytes.contains(&0)
        && !bytes.starts_with(&[255, 254])
        && !bytes.starts_with(&[254, 255])
    {
        let mut nulls = [0_usize; 2];
        for (index, &byte) in bytes.iter().take(8192).enumerate() {
            if byte == 0 {
                nulls[index % 2] += 1;
            }
        }
        if !bytes.len().is_multiple_of(2) || nulls[0].max(nulls[1]) <= 3 * nulls[0].min(nulls[1]) {
            return Err(bad());
        }
        let encoding = if nulls[1] > nulls[0] {
            UTF_16LE
        } else {
            UTF_16BE
        };
        encoding
            .decode_without_bom_handling_and_without_replacement(bytes)
            .ok_or_else(bad)?
            .into_owned()
    } else {
        data::text(bytes)?
    };
    if text.contains('\0') {
        return Err(bad());
    }
    Ok(text)
}

#[derive(Clone, Copy)]
struct Group {
    ignore: bool,
    unicode_count: usize,
    encoding: &'static Encoding,
}

fn rtf(source: &str, check: Check<'_>) -> io::Result<String> {
    let mut groups = Vec::new();
    let mut active = Group {
        ignore: false,
        unicode_count: 1,
        encoding: WINDOWS_1252,
    };
    let mut units = Vec::<u16>::new();
    let mut cursor = 0;
    let mut fallback = 0;
    let bytes = source.as_bytes();
    while let Some(&byte) = bytes.get(cursor) {
        if cursor.is_multiple_of(16384) {
            check()?;
        }
        cursor += 1;
        match byte {
            b'{' => {
                if groups.len() >= 256 {
                    return Err(invalid("RTF 그룹이 너무 깊습니다."));
                }
                groups.push(active);
            }
            b'}' => {
                active = groups
                    .pop()
                    .ok_or_else(|| invalid("RTF 그룹 경계가 올바르지 않습니다."))?;
                fallback = 0;
            }
            b'\r' | b'\n' => {}
            b'\\' => {
                let Some(&escaped) = bytes.get(cursor) else {
                    break;
                };
                if escaped == b'\'' {
                    let mut payload = Vec::new();
                    loop {
                        let Some(value) = source
                            .get(cursor + 1..cursor + 3)
                            .and_then(|token| u8::from_str_radix(token, 16).ok())
                        else {
                            break;
                        };
                        cursor += 3;
                        if fallback > 0 {
                            fallback -= 1;
                        } else if !active.ignore {
                            payload.push(value);
                        }
                        if bytes.get(cursor..cursor + 2) != Some(b"\\'") {
                            break;
                        }
                        cursor += 1;
                    }
                    if bytes.get(cursor) == Some(&b'\'') {
                        cursor += 1;
                    }
                    let (text, _) = active.encoding.decode_without_bom_handling(&payload);
                    units.extend(text.encode_utf16());
                    continue;
                }
                if !escaped.is_ascii_alphabetic() {
                    cursor += 1;
                    if escaped == b'*' {
                        active.ignore = true;
                    }
                    if fallback > 0 {
                        fallback -= 1;
                        continue;
                    }
                    if !active.ignore
                        && let Some(ch) = match escaped {
                            b'{' | b'}' | b'\\' => Some(char::from(escaped)),
                            b'~' => Some('\u{a0}'),
                            b'_' => Some('\u{2011}'),
                            b'\n' | b'\r' => Some('\n'),
                            _ => None,
                        }
                    {
                        units.push(ch as u16);
                    }
                    continue;
                }
                let start = cursor;
                while bytes.get(cursor).is_some_and(u8::is_ascii_alphabetic) {
                    cursor += 1;
                }
                let word = &source[start..cursor];
                let parameter = cursor;
                if bytes.get(cursor) == Some(&b'-') {
                    cursor += 1;
                }
                while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
                    cursor += 1;
                }
                let number = source[parameter..cursor].parse::<i32>().ok();
                if bytes.get(cursor) == Some(&b' ') {
                    cursor += 1;
                }
                match word {
                    "fonttbl" | "colortbl" | "stylesheet" | "info" | "pict" | "object"
                    | "fldinst" | "header" | "footer" | "headerl" | "headerr" | "footerl"
                    | "footerr" | "listtable" | "listoverridetable" | "generator" | "themedata" => {
                        active.ignore = true
                    }
                    "bin" => {
                        let count = number
                            .and_then(|value| usize::try_from(value).ok())
                            .ok_or_else(|| invalid("잘못된 RTF 바이너리 길이입니다."))?;
                        cursor = cursor.saturating_add(count).min(bytes.len());
                        while !source.is_char_boundary(cursor) {
                            cursor += 1;
                        }
                    }
                    "ansicpg" if !active.ignore => {
                        if let Some(number) = number {
                            active.encoding = match number {
                                65001 => encoding_rs::UTF_8,
                                949 => encoding_rs::EUC_KR,
                                932 => encoding_rs::SHIFT_JIS,
                                936 => encoding_rs::GBK,
                                950 => encoding_rs::BIG5,
                                _ => Encoding::for_label(format!("windows-{number}").as_bytes())
                                    .unwrap_or(WINDOWS_1252),
                            };
                        }
                    }
                    "uc" => active.unicode_count = number.unwrap_or(1).clamp(0, 32) as usize,
                    "u" if !active.ignore => {
                        let value =
                            number.ok_or_else(|| invalid("잘못된 RTF Unicode 값입니다."))?;
                        units.push(value as u16);
                        fallback = active.unicode_count;
                    }
                    _ if !active.ignore => {
                        let ch = match word {
                            "par" | "line" | "page" | "row" => '\n',
                            "tab" => '\t',
                            "cell" => '|',
                            "emdash" => '\u{2014}',
                            "endash" => '\u{2013}',
                            "bullet" => '\u{2022}',
                            "lquote" => '\u{2018}',
                            "rquote" => '\u{2019}',
                            "ldblquote" => '\u{201c}',
                            "rdblquote" => '\u{201d}',
                            "emspace" | "enspace" | "qmspace" => ' ',
                            _ => continue,
                        };
                        units.push(ch as u16);
                    }
                    _ => {}
                }
            }
            _ => {
                let ch = source[cursor - 1..].chars().next().unwrap();
                cursor += ch.len_utf8() - 1;
                if fallback > 0 {
                    fallback -= 1;
                } else if !active.ignore {
                    units.extend(ch.encode_utf16(&mut [0; 2]).iter().copied());
                }
            }
        }
    }
    Ok(String::from_utf16_lossy(&units))
}

fn read_clipping(bytes: &[u8]) -> io::Result<String> {
    let bad = || {
        invalid("이 textClipping은 macOS 리소스 포크가 필요합니다. TXT/RTF로 저장한 뒤 변환하세요.")
    };
    let value = plist::Value::from_reader(Cursor::new(bytes)).map_err(|_| bad())?;
    let value = if let Some(fields) = value.as_dictionary() {
        [
            "public.utf8-plain-text",
            "NSStringPboardType",
            "utf8",
            "text",
        ]
        .iter()
        .find_map(|key| fields.get(key))
        .ok_or_else(bad)?
    } else {
        &value
    };
    match value {
        plist::Value::String(value) => Ok(value.clone()),
        plist::Value::Data(value) => String::from_utf8(value.clone()).map_err(|_| bad()),
        plist::Value::Boolean(value) => Ok(if *value { "True" } else { "False" }.to_owned()),
        plist::Value::Integer(value) => Ok(value.to_string()),
        plist::Value::Real(value) => Ok(format!("{value:?}")),
        _ => Err(bad()),
    }
}

fn body(text: &str, title: &str) -> String {
    let paragraphs: String = text
        .split("\n\n")
        .map(|paragraph| {
            format!(
                "<p>{}</p>",
                safe_html::escape(paragraph).replace('\n', "<br/>")
            )
        })
        .collect();
    format!(
        "<!doctype html><html><head><meta charset='utf-8'><title>{}</title></head><body>{paragraphs}</body></html>",
        safe_html::escape(title)
    )
}

pub(crate) fn write_pdf_text(
    path: &Path,
    output: &Path,
    text: &str,
    target: &str,
) -> io::Result<()> {
    let title = path.file_stem().unwrap_or_default().to_string_lossy();
    if target == "epub" {
        return write_epub(output, text, &title);
    }
    let text = if target == "html" {
        body(text, &title)
    } else {
        text.to_owned()
    };
    fs::write(output, text.replace('\n', "\r\n"))
}

fn rtfd(path: &Path, check: Check<'_>) -> io::Result<String> {
    let bad = || invalid("RTFD 번들에 RTF 문서가 없습니다.");
    if path.is_dir() {
        let item = fs::read_dir(path)?
            .filter_map(Result::ok)
            .find(|item| {
                item.path()
                    .extension()
                    .and_then(|ext| ext.to_str())
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("rtf"))
            })
            .ok_or_else(bad)?;
        return rtf(&decode(&data::bytes(&item.path())?)?, check);
    }
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    for index in 0..archive.len() {
        check()?;
        let mut file = archive.by_index(index)?;
        if !file.name()?.to_ascii_lowercase().ends_with(".rtf") {
            continue;
        }
        if file.size() > 64 * 1024 * 1024 {
            return Err(invalid("RTFD 본문이 너무 큽니다."));
        }
        let mut raw = Vec::new();
        file.read_to_end(&mut raw)?;
        return rtf(&String::from_utf8_lossy(&raw), check);
    }
    Err(bad())
}

fn write_epub(path: &Path, text: &str, title: &str) -> io::Result<()> {
    let content = body(text, title)
        .replace("<!doctype html>", "")
        .replace("<html>", "<html xmlns=\"http://www.w3.org/1999/xhtml\">")
        .replace("<meta charset='utf-8'>", "<meta charset=\"utf-8\"/>");
    let now = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .map_err(io::Error::other)?
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(io::Error::other)?;
    let id = uuid::Uuid::new_v4();
    let package = format!(
        "<package xmlns=\"http://www.idpf.org/2007/opf\" version=\"3.0\" unique-identifier=\"id\"><metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\"><dc:identifier id=\"id\">urn:uuid:{id}</dc:identifier><dc:title>{}</dc:title><dc:language>ko</dc:language><meta property=\"dcterms:modified\">{now}</meta></metadata><manifest><item id=\"content\" href=\"content.xhtml\" media-type=\"application/xhtml+xml\"/><item id=\"nav\" href=\"nav.xhtml\" media-type=\"application/xhtml+xml\" properties=\"nav\"/></manifest><spine><itemref idref=\"content\"/></spine></package>",
        safe_html::escape(title)
    );
    let container = r#"<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"#;
    let nav = r#"<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="content.xhtml">Document</a></li></ol></nav></body></html>"#;
    let mut zip = zip::ZipWriter::new(File::create(path)?);
    zip.start_file(
        "mimetype",
        SimpleFileOptions::default().compression_method(zip::CompressionMethod::Stored),
    )?;
    zip.write_all(b"application/epub+zip")?;
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for (name, content) in [
        ("META-INF/container.xml", container),
        ("EPUB/package.opf", &package),
        ("EPUB/content.xhtml", &content),
        ("EPUB/nav.xhtml", nav),
    ] {
        zip.start_file(name, options)?;
        zip.write_all(content.as_bytes())?;
    }
    zip.finish()?.sync_all()
}

fn document_runs(text: &str, output: &mut String) -> io::Result<()> {
    if text
        .chars()
        .any(|ch| ch < ' ' && !matches!(ch, '\r' | '\n' | '\t'))
    {
        return Err(invalid("DOCX로 저장할 수 없는 제어 문자가 있습니다."));
    }
    output.push_str("<w:r>");
    let mut end = 0;
    for (index, ch) in text.match_indices(['\r', '\n', '\t']) {
        if index > end {
            output.push_str("<w:t xml:space=\"preserve\">");
            output.push_str(&safe_html::escape(&text[end..index]));
            output.push_str("</w:t>");
        }
        output.push_str(if ch == "\t" { "<w:tab/>" } else { "<w:br/>" });
        end = index + ch.len();
    }
    if end < text.len() {
        output.push_str("<w:t xml:space=\"preserve\">");
        output.push_str(&safe_html::escape(&text[end..]));
        output.push_str("</w:t>");
    }
    output.push_str("</w:r>");
    Ok(())
}

pub(crate) fn write_docx(path: &Path, content: &str, check: Check<'_>) -> io::Result<()> {
    use std::fmt::Write as _;
    static BLOCKS: LazyLock<Selector> =
        LazyLock::new(|| Selector::parse("p,h1,h2,h3,h4,h5,h6,li,tr,pre").unwrap());
    let document = Html::parse_document(content);
    let mut body = format!("<w:document xmlns:w=\"{W}\"><w:body>");
    let mut styles = format!(
        "<w:styles xmlns:w=\"{W}\"><w:style w:type=\"paragraph\" w:default=\"1\" w:styleId=\"Normal\"><w:name w:val=\"Normal\"/></w:style>"
    );
    for level in 1..=6 {
        write!(styles, "<w:style w:type=\"paragraph\" w:styleId=\"Heading{level}\"><w:name w:val=\"heading {level}\"/><w:basedOn w:val=\"Normal\"/><w:pPr><w:outlineLvl w:val=\"{}\"/></w:pPr><w:rPr><w:b/><w:sz w:val=\"{}\"/></w:rPr></w:style>", level - 1, 32 - level * 2).unwrap();
    }
    styles.push_str("<w:style w:type=\"paragraph\" w:styleId=\"ListBullet\"><w:name w:val=\"List Bullet\"/><w:basedOn w:val=\"Normal\"/><w:pPr><w:numPr><w:numId w:val=\"1\"/></w:numPr></w:pPr></w:style></w:styles>");
    for element in document.select(&BLOCKS) {
        check()?;
        body.push_str("<w:p>");
        let tag = element.value().name();
        if tag == "li" {
            body.push_str("<w:pPr><w:pStyle w:val=\"ListBullet\"/></w:pPr>");
        } else if let Some(level) = tag.strip_prefix('h') {
            write!(body, "<w:pPr><w:pStyle w:val=\"Heading{level}\"/></w:pPr>").unwrap();
        }
        let text = element.text().collect::<Vec<_>>().join(" ");
        document_runs(&text, &mut body)?;
        body.push_str("</w:p>");
    }
    body.push_str("<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/><w:pgMar w:top=\"1440\" w:right=\"1800\" w:bottom=\"1440\" w:left=\"1800\"/></w:sectPr></w:body></w:document>");
    let types = r#"<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>"#;
    let package = format!(
        "<Relationships xmlns=\"{PKG_REL}\"><Relationship Id=\"rId1\" Type=\"{REL}/officeDocument\" Target=\"word/document.xml\"/></Relationships>"
    );
    let relations = format!(
        "<Relationships xmlns=\"{PKG_REL}\"><Relationship Id=\"rId1\" Type=\"{REL}/styles\" Target=\"styles.xml\"/><Relationship Id=\"rId2\" Type=\"{REL}/numbering\" Target=\"numbering.xml\"/></Relationships>"
    );
    let numbering = format!(
        "<w:numbering xmlns:w=\"{W}\"><w:abstractNum w:abstractNumId=\"0\"><w:lvl w:ilvl=\"0\"><w:start w:val=\"1\"/><w:numFmt w:val=\"bullet\"/><w:lvlText w:val=\"•\"/><w:lvlJc w:val=\"left\"/><w:pPr><w:ind w:left=\"720\" w:hanging=\"360\"/></w:pPr></w:lvl></w:abstractNum><w:num w:numId=\"1\"><w:abstractNumId w:val=\"0\"/></w:num></w:numbering>"
    );
    let mut zip = zip::ZipWriter::new(File::create(path)?);
    let options = SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated);
    for (name, content) in [
        ("[Content_Types].xml", types),
        ("_rels/.rels", &package),
        ("word/document.xml", &body),
        ("word/_rels/document.xml.rels", &relations),
        ("word/styles.xml", &styles),
        ("word/numbering.xml", &numbering),
    ] {
        zip.start_file(name, options)?;
        zip.write_all(content.as_bytes())?;
    }
    zip.finish()?.sync_all()
}

pub fn supports(source: &str, target: &str) -> bool {
    matches!(source, "txt" | "md" | "rtf" | "rtfd" | "textclipping")
        && matches!(target, "txt" | "md" | "html" | "docx" | "epub" | "pdf")
}

pub fn convert(
    path: &Path,
    output: &Path,
    source: &str,
    target: &str,
    check: Check<'_>,
) -> io::Result<()> {
    let title = path.file_stem().unwrap_or_default().to_string_lossy();
    let mut original = None;
    let text = match source {
        "rtfd" => rtfd(path, check)?,
        "textclipping" => read_clipping(&data::bytes(path)?)?,
        "rtf" => rtf(&decode(&data::bytes(path)?)?, check)?,
        "md" => {
            original = Some(decode(&data::bytes(path)?)?);
            let source = original.as_ref().unwrap();
            let mut rendered = String::new();
            html::push_html(
                &mut rendered,
                Parser::new_ext(source, Options::ENABLE_TABLES),
            );
            let rendered = rendered.trim_end_matches('\n');
            if target == "html" {
                return fs::write(
                    output,
                    format!("<html><meta charset='utf-8'><body>{rendered}</body></html>")
                        .replace('\n', "\r\n"),
                );
            }
            if target == "docx" {
                return write_docx(output, rendered, check);
            }
            if target == "pdf" {
                let rendered =
                    format!("<html><meta charset='utf-8'><body>{rendered}</body></html>");
                return crate::qt_render::html_pdf(&rendered, output, false, check);
            }
            safe_html::text(rendered, false)
        }
        _ => decode(&data::bytes(path)?)?,
    };
    check()?;
    match target {
        "txt" => fs::write(output, text.replace('\n', "\r\n")),
        "md" => fs::write(output, original.unwrap_or(text).replace('\n', "\r\n")),
        "html" => fs::write(
            output,
            body(&text, if source == "rtfd" { "" } else { &title }).replace('\n', "\r\n"),
        ),
        "docx" => write_docx(output, &body(&text, &title), check),
        "epub" => write_epub(output, &text, &title),
        "pdf" => {
            let content = if source == "txt" {
                let text = text.replace("\r\n", "\n").replace('\r', "\n");
                format!(
                    "<pre style=\"white-space:pre-wrap; font-family:'Malgun Gothic'; font-size:10pt;\">{}</pre>",
                    safe_html::escape(&text)
                )
            } else {
                body(&text, if source == "rtfd" { "" } else { &title })
            };
            crate::qt_render::html_pdf(&content, output, false, check)
        }
        _ => Err(invalid("지원하지 않는 문서 출력 형식입니다.")),
    }
}
