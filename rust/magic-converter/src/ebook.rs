use crate::{Edge, safe_html};
use base64::{Engine, engine::general_purpose::STANDARD};
use roxmltree::{Document, Node};
use scraper::{Html, Selector};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, Cursor, ErrorKind, Write};
use std::path::Path;
use zip::{CompressionMethod, ZipArchive, ZipWriter, write::SimpleFileOptions};

mod docx;
mod epub;
mod fb2;

const LIMIT: usize = 256 * 1024 * 1024;
const PART_LIMIT: usize = 64 * 1024 * 1024;
const EPUB_MEDIA: &[u8] = b"application/epub+zip";
const XHTML: &str = "http://www.w3.org/1999/xhtml";
const OPF: &str = "http://www.idpf.org/2007/opf";
const DC: &str = "http://purl.org/dc/elements/1.1/";
const FB: &str = "http://www.gribuser.ru/xml/fictionbook/2.0";
const XLINK: &str = "http://www.w3.org/1999/xlink";
const CONTAINER: &str = "urn:oasis:names:tc:opendocument:xmlns:container";

type Check<'a> = &'a dyn Fn() -> io::Result<()>;

pub(crate) fn layout_docx(
    html: &str,
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    docx::write_layout(html, path, check, warnings)
}

#[derive(Default)]
struct Book {
    title: String,
    authors: Vec<String>,
    language: String,
    description: String,
    publisher: String,
    chapters: Vec<(String, String)>,
    cover: Option<String>,
    bytes: usize,
}

impl Book {
    fn new(title: impl Into<String>) -> Self {
        Self {
            title: title.into(),
            language: "und".to_owned(),
            ..Default::default()
        }
    }
    fn chapter(&mut self, title: String, html: String) -> io::Result<()> {
        self.bytes = self
            .bytes
            .saturating_add(title.len())
            .saturating_add(html.len());
        if self.chapters.len() >= 5000 || self.bytes > LIMIT {
            return Err(invalid("전자책 장과 리소스의 크기 한도를 초과했습니다."));
        }
        self.chapters.push((title, html));
        Ok(())
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn text(item: Node<'_, '_>) -> String {
    item.descendants()
        .filter(Node::is_text)
        .filter_map(|node| node.text())
        .collect::<String>()
        .trim()
        .to_owned()
}

fn xml(bytes: &[u8]) -> io::Result<String> {
    if bytes.starts_with(&[255, 254]) {
        let (text, error) = encoding_rs::UTF_16LE.decode_with_bom_removal(bytes);
        if !error {
            return Ok(text.into_owned());
        }
    } else if bytes.starts_with(&[254, 255]) {
        let (text, error) = encoding_rs::UTF_16BE.decode_with_bom_removal(bytes);
        if !error {
            return Ok(text.into_owned());
        }
    } else {
        return std::str::from_utf8(bytes.strip_prefix(&[239, 187, 191]).unwrap_or(bytes))
            .map(str::to_owned)
            .map_err(io::Error::other);
    }
    Err(invalid("전자책 XML의 인코딩을 읽을 수 없습니다."))
}

fn doc(data: &str) -> io::Result<Document<'_>> {
    Document::parse(data).map_err(io::Error::other)
}

fn name(value: &str) -> io::Result<String> {
    let value = value.replace('\\', "/");
    if value.is_empty()
        || value.starts_with('/')
        || value.contains([':', '\0'])
        || value.split('/').any(|part| matches!(part, "." | ".."))
    {
        return Err(invalid("패키지에 안전하지 않은 경로가 있습니다."));
    }
    Ok(value
        .split('/')
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("/"))
}

fn relative(base: &str, href: &str) -> io::Result<String> {
    let href = href.split(['#', '?']).next().unwrap_or_default();
    let href = percent_encoding::percent_decode_str(href)
        .decode_utf8()
        .map_err(io::Error::other)?;
    if href.starts_with('/') || href.contains([':', '\\', '\0']) || href.is_empty() {
        return Err(invalid("외부 리소스는 전자책에 포함할 수 없습니다."));
    }
    let mut parts: Vec<_> = base
        .rsplit_once('/')
        .into_iter()
        .flat_map(|(parent, _)| parent.split('/'))
        .collect();
    for part in href.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                if parts.pop().is_none() {
                    return Err(invalid("전자책 경로가 패키지 밖을 참조합니다."));
                }
            }
            part => parts.push(part),
        }
    }
    name(&parts.join("/"))
}

struct Package {
    inner: ZipArchive<File>,
    names: BTreeMap<String, (usize, u64)>,
}

impl Package {
    fn open(source: &Path, check: Check<'_>) -> io::Result<Self> {
        use unicode_casefold::UnicodeCaseFold;
        let mut inner = ZipArchive::new(File::open(source)?)?;
        let mut names = BTreeMap::new();
        let mut folded = HashSet::new();
        let mut size = 0_u64;
        if inner.len() > 5000 {
            return Err(invalid("전자책 패키지 항목 제한을 초과했습니다."));
        }
        for index in 0..inner.len() {
            check()?;
            let member = inner.by_index(index)?;
            size = size.saturating_add(member.size());
            if size > LIMIT as u64 {
                return Err(invalid("전자책 패키지 크기 제한을 초과했습니다."));
            }
            if member.is_dir() {
                continue;
            }
            let path = name(&member.name()?)?;
            if !folded.insert(path.case_fold().collect::<String>())
                || member.is_symlink()
                || member.encrypted()
                || member.size() > PART_LIMIT as u64
            {
                return Err(invalid(
                    "중복 경로·심볼릭 링크 또는 너무 큰 패키지 항목입니다.",
                ));
            }
            names.insert(path, (index, member.size()));
        }
        Ok(Self { inner, names })
    }

    fn read(&mut self, path: &str, check: Check<'_>) -> io::Result<Vec<u8>> {
        let (index, size) = self.names.get(path).ok_or_else(|| {
            io::Error::new(
                ErrorKind::NotFound,
                format!("전자책 파일이 없습니다: {path}"),
            )
        })?;
        let mut member = self.inner.by_index(*index)?;
        let mut data = Vec::new();
        crate::copy_stream(&mut member, &mut data, *size, check, |_| Ok(()))?;
        Ok(data)
    }
}

fn file(source: &Path, check: Check<'_>) -> io::Result<Vec<u8>> {
    let mut input = File::open(source)?;
    let size = input.metadata()?.len();
    if size > PART_LIMIT as u64 {
        return Err(invalid("전자책 입력 용량 제한을 초과했습니다."));
    }
    let mut buffer = Vec::new();
    crate::copy_stream(&mut input, &mut buffer, size, check, |_| Ok(()))?;
    Ok(buffer)
}

fn title(source: &Path) -> String {
    source
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned()
}

fn input_book(
    source: &Path,
    kind: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Book> {
    let book = match kind {
        "epub" => epub::read(source, check, warnings)?,
        "fb2" => fb2::read(source, check)?,
        "txt" | "md" | "docx" => {
            let raw = if kind == "docx" {
                crate::word_model::rich_html(source, check)?
            } else {
                let source = crate::text_documents::decode(&file(source, check)?)?;
                if kind == "txt" {
                    source
                        .lines()
                        .map(|line| format!("<p>{}</p>", safe_html::escape(line)))
                        .collect()
                } else {
                    let mut rendered = String::new();
                    pulldown_cmark::html::push_html(
                        &mut rendered,
                        pulldown_cmark::Parser::new_ext(
                            &source,
                            pulldown_cmark::Options::ENABLE_TABLES,
                        ),
                    );
                    rendered.trim_end_matches('\n').to_owned()
                }
            };
            let mut book = Book::new(title(source));
            let sanitized = safe_html::sanitize(&raw, &mut |_| Ok(None))?;
            book.chapter(book.title.clone(), sanitized)?;
            warnings.push("문서의 본문을 한 장으로 가져옵니다. 외부 이미지·페이지 배치·머리말·꼬리말·편집 수식은 보존하지 않습니다.".to_owned());
            book
        }
        _ => return Err(invalid("지원하지 않는 전자책 입력입니다.")),
    };
    if book.chapters.is_empty() {
        return Err(invalid("전자책에 읽을 장이 없습니다."));
    }
    warnings.push("전자책의 기본 메타데이터·장 순서·기본 강조/표·내장 래스터 이미지를 공통 모델로 읽었습니다. EPUB의 기본 내부 링크를 보존하며, 원래 CSS·페이지 배치·복잡한 주석 동작·스크립트·오디오/동영상·폰트는 보존하지 않습니다. 출력별 보존 범위가 다릅니다.".to_owned());
    Ok(book)
}

fn unpack_image(uri: &str) -> io::Result<(Vec<u8>, &'static str, &'static str)> {
    let (header, data) = uri
        .split_once(',')
        .ok_or_else(|| invalid("전자책 이미지 형식이 잘못되었습니다."))?;
    let (mime, suffix) = match header {
        "data:image/png;base64" => ("image/png", "png"),
        "data:image/jpeg;base64" => ("image/jpeg", "jpg"),
        "data:image/gif;base64" => ("image/gif", "gif"),
        "data:image/webp;base64" => ("image/webp", "webp"),
        "data:image/bmp;base64" => ("image/bmp", "bmp"),
        _ => return Err(invalid("전자책 이미지 형식이 잘못되었습니다.")),
    };
    let bytes = STANDARD.decode(data).map_err(io::Error::other)?;
    Ok((bytes, mime, suffix))
}

fn escape_attribute(value: &str) -> String {
    safe_html::escape(value)
}

fn chapter_body(html: &Html) -> scraper::ElementRef<'_> {
    let root = html.root_element();
    root.child_elements()
        .find(|node| node.value().name() == "body")
        .unwrap_or(root)
}

fn check_size(len: usize) -> io::Result<()> {
    if len > LIMIT {
        Err(invalid("전자책 출력 확장 용량 제한을 초과했습니다."))
    } else {
        Ok(())
    }
}

fn write_text(
    book: &Book,
    output: &Path,
    kind: &str,
    warnings: &mut Vec<String>,
    check: Check<'_>,
) -> io::Result<()> {
    let mut file = File::create(output)?;
    let mut bytes = 0;
    let mut write = |value: &str| -> io::Result<()> {
        bytes += value.len();
        check_size(bytes)?;
        file.write_all(value.as_bytes())
    };
    if kind == "html" {
        let opening = safe_html::page(&book.title, "");
        let (start, _) = opening.rsplit_once("</body>").expect("HTML body");
        write(start)?;
    }
    for (index, (title, body)) in book.chapters.iter().enumerate() {
        check()?;
        if index > 0 && kind != "html" {
            write("\n\n")?;
        }
        match kind {
            "txt" => {
                write(&format!("{title}\n"))?;
                write(&safe_html::text(body, true))?;
            }
            "md" => {
                write(&format!("# {title}\n\n"))?;
                write(body)?;
            }
            "html" => {
                write(&format!("<section><h1>{}</h1>", safe_html::escape(title)))?;
                write(body)?;
                write("</section>")?;
            }
            _ => unreachable!(),
        }
    }
    if kind == "html" {
        write("</body></html>")?;
    }
    warnings.push(match kind {
        "txt" => "TXT에는 이미지·표 구조·강조·메타데이터를 포함하지 않습니다.",
        "md" => "Markdown에 장 제목·기본 HTML 본문·data URI 이미지를 포함했습니다. 책 메타데이터와 별도 표지 지정은 제외하며, 렌더러에 따라 HTML/이미지 표시가 제한될 수 있습니다.",
        _ => "HTML에는 제목과 장 본문을 저장합니다. 저자·언어·설명·출판사 메타데이터와 별도 표지 지정은 포함하지 않습니다.",
    }.to_owned());
    Ok(())
}

pub fn supports(edge: &Edge) -> bool {
    edge.engine_id == "ebook"
        && match edge.source.as_str() {
            "epub" | "fb2" => {
                matches!(
                    edge.target.as_str(),
                    "epub" | "fb2" | "txt" | "md" | "html" | "docx"
                )
            }
            "txt" | "md" | "docx" => matches!(edge.target.as_str(), "epub" | "fb2"),
            "cbz" => matches!(edge.target.as_str(), "epub" | "pdf"),
            _ => false,
        }
}

pub fn convert(
    source: &Path,
    output: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    if edge.source == "cbz" {
        return comic(source, output, &edge.target, check, warnings);
    }
    let book = input_book(source, &edge.source, check, warnings)?;
    match edge.target.as_str() {
        "epub" => epub::write(&book, output, check, warnings),
        "fb2" => fb2::write(&book, output, check, warnings),
        "docx" => docx::write(&book, output, check, warnings),
        target => write_text(&book, output, target, warnings, check),
    }
}

#[derive(Eq, PartialEq, Ord, PartialOrd)]
enum Natural {
    Digits(usize, String),
    Text(String),
}

fn natural(name: &str) -> Vec<Natural> {
    use unicode_casefold::UnicodeCaseFold;
    let mut pieces = Vec::new();
    let mut start = 0;
    let mut digit = false;
    for (index, ch) in name.char_indices() {
        if ch.is_ascii_digit() != digit {
            let previous = &name[start..index];
            pieces.push(if digit {
                let value = previous.trim_start_matches('0').to_owned();
                Natural::Digits(value.len(), value)
            } else {
                Natural::Text(previous.case_fold().collect())
            });
            start = index;
            digit = !digit;
        }
    }
    let remainder = &name[start..];
    if digit {
        let value = remainder.trim_start_matches('0').to_owned();
        pieces.push(Natural::Digits(value.len(), value));
        pieces.push(Natural::Text(String::new()));
    } else {
        pieces.push(Natural::Text(remainder.case_fold().collect()));
    }
    pieces
}

fn animated(bytes: &[u8]) -> io::Result<bool> {
    use image::{AnimationDecoder, ImageFormat};
    let cursor = Cursor::new(bytes);
    let format = image::guess_format(bytes).map_err(io::Error::other)?;
    Ok(match format {
        ImageFormat::Gif => image::codecs::gif::GifDecoder::new(cursor)
            .map_err(io::Error::other)?
            .into_frames()
            .nth(1)
            .transpose()
            .map_err(io::Error::other)?
            .is_some(),
        ImageFormat::WebP => image::codecs::webp::WebPDecoder::new(cursor)
            .map_err(io::Error::other)?
            .has_animation(),
        ImageFormat::Png => image::codecs::png::PngDecoder::new(cursor)
            .map_err(io::Error::other)?
            .is_apng()
            .map_err(io::Error::other)?,
        _ => false,
    })
}

fn comic(
    source: &Path,
    output: &Path,
    kind: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut package = Package::open(source, check)?;
    let mut pages: Vec<_> = package
        .names
        .keys()
        .filter(|name| {
            Path::new(name)
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| {
                    matches!(
                        ext.to_ascii_lowercase().as_str(),
                        "png" | "jpg" | "jpeg" | "webp" | "gif" | "bmp"
                    )
                })
        })
        .cloned()
        .collect();
    pages.sort_by_cached_key(|name| (natural(name), name.clone()));
    if pages.is_empty() || pages.len() > 1000 {
        return Err(invalid("CBZ에 1~1000개의 이미지 페이지가 필요합니다."));
    }
    let mut animation = false;
    if kind == "pdf" {
        crate::pdf::comic_pages(output, pages.len(), check, |index| {
            let bytes = package.read(&pages[index], check)?;
            safe_html::image_uri(&bytes)?;
            animation |= animated(&bytes)?;
            image::load_from_memory(&bytes)
                .map(|image| image.to_rgb8())
                .map_err(io::Error::other)
        })?;
    } else {
        let mut book = Book::new(title(source));
        for name in pages {
            check()?;
            let bytes = package.read(&name, check)?;
            let uri = safe_html::image_uri(&bytes)?;
            animation |= animated(&bytes)?;
            book.chapter(
                name.clone(),
                format!(
                    "<p><img src=\"{uri}\" alt=\"{}\"/></p>",
                    escape_attribute(&name)
                ),
            )?;
        }
        epub::write(&book, output, check, warnings)?;
    }
    if animation {
        warnings.push("만화 페이지의 애니메이션은 첫 프레임만 표시합니다.".to_owned());
    }
    if kind == "epub" {
        warnings.push(
            "CBZ를 이미지 중심의 reflowable EPUB으로 저장했습니다. 고정 배치 EPUB은 아닙니다."
                .to_owned(),
        );
    }
    warnings.push("CBZ 페이지는 파일명 자연 정렬 순서입니다. ComicInfo 메타데이터·책갈피·원래 폴더는 보존하지 않습니다.".to_owned());
    Ok(())
}
