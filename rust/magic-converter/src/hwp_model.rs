use crate::{Edge, office_package, safe_html::escape, word_model};
use base64::{Engine, engine::general_purpose::STANDARD};
use roxmltree::{Document, Node};
use scraper::{ElementRef, Html};
use std::fmt::Write as _;
use std::io::{self, Cursor, ErrorKind};
use std::path::Path;

#[cfg(windows)]
mod compound;
#[cfg(windows)]
mod legacy;
mod read;
mod write;

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
pub(crate) const GEOMETRY: [i32; 8] = [11906, 16837, 1440, 1440, 1440, 1440, 720, 720];

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn tag<'a, 'b>(node: Node<'a, 'b>, name: &str) -> Option<Node<'a, 'b>> {
    node.children()
        .find(|child| child.tag_name().name() == name)
}

fn parse(bytes: &[u8]) -> io::Result<Document<'_>> {
    Document::parse(std::str::from_utf8(bytes).map_err(io::Error::other)?).map_err(io::Error::other)
}

pub(crate) fn numbers<const N: usize>(value: Option<&str>, defaults: [i32; N]) -> [i32; N] {
    let mut tokens = value.unwrap_or_default().split_ascii_whitespace();
    defaults.map(|default| {
        tokens
            .next()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    })
}

pub(crate) fn values(value: Option<&str>) -> Vec<i32> {
    value
        .unwrap_or_default()
        .split_ascii_whitespace()
        .filter_map(|value| value.parse().ok())
        .collect()
}

pub(crate) fn color(value: &str) -> Option<&str> {
    let value = value.trim_start_matches('#');
    (value.len() == 6 && value.bytes().all(|byte| byte.is_ascii_hexdigit())).then_some(value)
}

pub(crate) fn image_html(
    bytes: &[u8],
    width: Option<f64>,
    height: Option<f64>,
    clip: [i32; 4],
) -> io::Result<String> {
    let image = image::ImageReader::new(Cursor::new(bytes)).with_guessed_format()?;
    let format = image
        .format()
        .ok_or_else(|| invalid("문서의 그림 형식을 읽을 수 없습니다."))?;
    let (w, h) = image.into_dimensions().map_err(io::Error::other)?;
    if u64::from(w) * u64::from(h) > 128_000_000 {
        return Err(invalid("문서 그림의 픽셀 수가 너무 많습니다."));
    }
    let width = width
        .filter(|value| *value > 0.0)
        .unwrap_or(f64::from(w) * 0.75);
    let height = height
        .filter(|value| *value > 0.0)
        .unwrap_or(f64::from(h) * 0.75);
    let [l, t, r, b] = clip;
    Ok(format!(
        r#"<img src="data:{};base64,{}" data-width="{width}" data-height="{height}" data-crop="{l} {t} {r} {b}">"#,
        format.to_mime_type(),
        STANDARD.encode(bytes)
    ))
}

pub(crate) struct Grid<'a> {
    pub rows: usize,
    pub columns: usize,
    pub heights: Vec<i32>,
    pub exact_heights: Vec<bool>,
    pub cells: std::collections::BTreeMap<(usize, usize), (Option<ElementRef<'a>>, usize, usize)>,
}

impl<'a> Grid<'a> {
    pub(crate) fn read(table: ElementRef<'a>) -> io::Result<Self> {
        let rows: Vec<_> = table
            .descendent_elements()
            .filter(|row| {
                row.value().name() == "tr"
                    && row
                        .ancestors()
                        .filter_map(ElementRef::wrap)
                        .find(|node| node.value().name() == "table")
                        == Some(table)
            })
            .collect();
        let mut occupied = std::collections::HashSet::new();
        let mut grid = Self {
            rows: rows.len(),
            columns: 0,
            heights: rows
                .iter()
                .map(|row| numbers(row.attr("data-height"), [0])[0])
                .collect(),
            exact_heights: rows
                .iter()
                .map(|row| row.attr("data-word-exact") == Some("1"))
                .collect(),
            cells: Default::default(),
        };
        for (r, row) in rows.into_iter().enumerate() {
            let mut c = 0;
            for cell in row
                .child_elements()
                .filter(|cell| matches!(cell.value().name(), "td" | "th"))
            {
                if let Some(column) = cell.attr("data-col").and_then(|value| value.parse().ok()) {
                    c = column;
                } else {
                    while occupied.contains(&(r, c)) {
                        c += 1;
                    }
                }
                let [rs] = numbers(cell.attr("rowspan"), [1]);
                let [cs] = numbers(cell.attr("colspan"), [1]);
                if !(1..=100_000).contains(&rs) || !(1..=16_384).contains(&cs) || c >= 16_384 {
                    return Err(invalid("문서 표 병합 범위가 잘못되었습니다."));
                }
                let (rs, cs) = (rs as usize, cs as usize);
                let end = c + cs;
                if r + rs > grid.rows || grid.rows.saturating_mul(end) > 100_000 || end > 16_384 {
                    return Err(invalid("문서 표의 셀 한도를 초과했습니다."));
                }
                for rr in r..r + rs {
                    for cc in c..end {
                        if !occupied.insert((rr, cc)) {
                            return Err(invalid("문서 표 병합 범위가 겹칩니다."));
                        }
                    }
                    grid.cells
                        .insert((rr, c), ((rr == r).then_some(cell), rs, cs));
                }
                c = end;
                grid.columns = grid.columns.max(c);
            }
        }
        Ok(grid)
    }
}

pub fn supports(edge: &Edge) -> bool {
    let hwp = matches!(edge.source.as_str(), "hwp" | "hwpx");
    let office = canonical(&edge.source).is_some();
    (hwp && edge.engine_id == "hwp_documents"
        && edge.target == "docx"
        && (cfg!(windows) || crate::rhwp::available() || edge.source == "hwpx"))
        || (office && edge.engine_id == "hwp_documents" && edge.target == "hwpx")
        || ((cfg!(windows) || crate::rhwp::available())
            && office
            && edge.engine_id == "rhwp"
            && edge.target == "hwp")
}

fn canonical(source: &str) -> Option<&'static str> {
    match source {
        "docx" | "docm" | "dotx" | "dotm" => Some("docx"),
        "pptx" | "pptm" | "potx" | "potm" | "ppsx" | "ppsm" => Some("pptx"),
        "xlsx" | "xlsm" | "xltx" | "xltm" => Some("xlsx"),
        _ => None,
    }
}

pub fn convert(
    source: &Path,
    destination: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let stage = tempfile::Builder::new()
        .prefix("magic-hwp-model-")
        .tempdir()?;
    if edge.target == "docx" {
        let staged = stage.path().join("model.hwpx");
        let input = if edge.source == "hwp" {
            to_hwpx(source, &staged, check, warnings)?;
            staged.as_path()
        } else {
            source
        };
        let html = read::hwpx(input, check, warnings)?;
        return crate::ebook::layout_docx(&html, destination, check, warnings);
    }
    let html = office_html(source, &edge.source, stage.path(), check, warnings)?;
    let hwpx = stage.path().join("output.hwpx");
    let target = if edge.target == "hwp" {
        hwpx.as_path()
    } else {
        destination
    };
    write::hwpx(&Html::parse_document(&html), target, check, warnings)?;
    if edge.target == "hwp" {
        let mut edge = edge.clone();
        edge.source = "hwpx".into();
        crate::rhwp::convert(&hwpx, destination, &edge, check, warnings)?;
        if warnings
            .iter()
            .any(|warning| warning.starts_with("회전·반전된 Word 글상자 "))
        {
            warnings.push(
                "HWP 출력에서는 번들 엔진의 호환을 위해 회전 글상자를 그림으로 저장합니다."
                    .to_owned(),
            );
        }
    }
    Ok(())
}

fn office_html(
    source: &Path,
    kind: &str,
    stage: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<String> {
    let canonical = canonical(kind).ok_or_else(|| invalid("지원하지 않는 Office 문서입니다."))?;
    let staged = stage.join(format!("model.{canonical}"));
    let source = if canonical == kind {
        source
    } else {
        office_package::convert(source, &staged, office_package::Action::Standardize, check)?;
        warnings.push("Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.".to_owned());
        staged.as_path()
    };
    match canonical {
        "docx" => word_model::layout_html(source, check, warnings),
        "pptx" => read::slides(source, check, warnings),
        _ => crate::sheets::document_html(source, check, warnings),
    }
}

#[cfg(windows)]
pub(crate) fn office_pdf(
    source: &Path,
    target: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let error = match crate::native_office::convert(source, target, edge, check, warnings) {
        Ok(()) => return Ok(()),
        Err(error) => error,
    };
    check()?;
    if edge.action_key != "document:pdf" || canonical(&edge.source).is_none() {
        return Err(error);
    }
    warnings.push("Office PDF 출력에 실패해 문서 모델의 기본 PDF 엔진으로 변환합니다.".to_owned());
    let stage = tempfile::Builder::new()
        .prefix("magic-office-fallback-")
        .tempdir()?;
    let html = office_html(source, &edge.source, stage.path(), check, warnings)?;
    let document = Html::parse_document(&html);
    if !crate::rhwp::shaping_required(&document.root_element().text().collect::<String>()) {
        let model = stage.path().join("document.hwpx");
        let pdf = stage.path().join("document.pdf");
        let result = write::hwpx(&document, &model, check, warnings)
            .and_then(|()| crate::rhwp::invoke(&model, &pdf, "hwpx", "pdf", check, warnings))
            .and_then(|()| crate::pdfium::page_count(&pdf, check));
        match result {
            Ok(_) => {
                warnings.push("Office PDF 대체: rhwp 문서 모델로 출력했습니다. 원래 배치는 달라질 수 있습니다.".to_owned());
                let mut input = std::fs::File::open(pdf)?;
                let length = input.metadata()?.len();
                crate::copy_stream(
                    &mut input,
                    &mut std::fs::File::create(target)?,
                    length,
                    check,
                    |_| Ok(()),
                )?;
                return Ok(());
            }
            Err(error) => {
                check()?;
                warnings.push(format!(
                    "rhwp PDF 출력 실패: {}",
                    error.to_string().chars().take(180).collect::<String>()
                ));
            }
        }
    }
    crate::qt_render::html_pdf(&html, target, false, check)?;
    warnings.push("기본 Qt PDF 엔진으로 본문·표·그림을 출력했습니다. 원래 배치와 수식 조판은 달라질 수 있습니다.".to_owned());
    Ok(())
}

pub(crate) fn to_hwpx(
    source: &Path,
    target: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<bool> {
    let result = crate::rhwp::invoke(source, target, "hwp", "hwpx", check, warnings);
    #[cfg(windows)]
    if let Err(error) = result {
        check()?;
        let html = legacy::read(source, check)
            .map_err(|legacy| io::Error::other(format!("{error} / 기본 HWP 리더: {legacy}")))?;
        warnings.push("문서 정보가 없는 HWP에 기본 레코드 리더를 사용했습니다. 수식·도형과 일부 조판은 옮기지 못합니다.".to_owned());
        return write::hwpx(&Html::parse_document(&html), target, check, warnings).map(|()| false);
    }
    result.map(|()| true)
}

pub(crate) fn pdf(
    source: &Path,
    target: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let html = read::hwpx(source, check, warnings)?;
    #[cfg(windows)]
    {
        let directory = tempfile::Builder::new()
            .prefix("magic-hwp-pdf-")
            .tempdir()?;
        let docx = directory.path().join("document.docx");
        crate::ebook::layout_docx(&html, &docx, check, warnings)?;
        let mut word = edge.clone();
        word.source = "docx".into();
        word.engine_id = "documents".into();
        if crate::native_office::convert(&docx, target, &word, check, warnings).is_ok() {
            return Ok(());
        }
        check()?;
        warnings.push("Word에서 PDF를 만들 수 없어 Qt로 출력했습니다.".to_owned());
    }
    #[cfg(not(windows))]
    let _ = edge;
    crate::qt_render::html_pdf(&html, target, false, check)
}
