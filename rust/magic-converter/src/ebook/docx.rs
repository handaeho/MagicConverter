use super::*;
use crate::hwp_model::{self, Grid, color, numbers, values};
use scraper::{ElementRef, Node as HtmlNode};
use std::io::Read;

const TEMPLATE: &[u8] = include_bytes!("../../assets/empty-document.docx");
const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const R: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const WP: &str = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
const A: &str = "http://schemas.openxmlformats.org/drawingml/2006/main";
const PIC: &str = "http://schemas.openxmlformats.org/drawingml/2006/picture";
const MATH: &str = "http://schemas.openxmlformats.org/officeDocument/2006/math";
const DOC: &str = "word/document.xml";
const RELS: &str = "word/_rels/document.xml.rels";
const TYPES: &str = "[Content_Types].xml";
const CORE: &str = "docProps/core.xml";
type Html<'a> = ego_tree::NodeRef<'a, HtmlNode>;

#[derive(Clone, Default)]
struct Style {
    bold: bool,
    italic: bool,
    metrics: String,
}

struct Writer<'a> {
    archive: &'a mut ZipWriter<File>,
    images: HashMap<String, usize>,
    relationships: String,
    types: BTreeMap<&'static str, &'static str>,
    drawings: usize,
    size: usize,
    check: Check<'a>,
}

impl Writer<'_> {
    fn text(value: &str, style: &Style, output: &mut String) -> io::Result<()> {
        if value
            .chars()
            .any(|ch| ch < ' ' && !matches!(ch, '\t' | '\n' | '\r'))
        {
            return Err(invalid("DOCX로 저장할 수 없는 제어 문자가 있습니다."));
        }
        output.push_str("<w:r>");
        if style.bold || style.italic || !style.metrics.is_empty() {
            output.push_str("<w:rPr>");
            if style.bold {
                output.push_str("<w:b/>");
            }
            if style.italic {
                output.push_str("<w:i/>");
            }
            output.push_str(&style.metrics);
            output.push_str("</w:rPr>");
        }
        let mut start = 0;
        for (i, ch) in value.match_indices(['\n', '\r', '\t']) {
            if i > start {
                write!(
                    output,
                    "<w:t xml:space=\"preserve\">{}</w:t>",
                    safe_html::escape(&value[start..i])
                )
                .unwrap();
            }
            output.push_str(if ch == "\t" { "<w:tab/>" } else { "<w:br/>" });
            start = i + ch.len();
        }
        if start < value.len() {
            write!(
                output,
                "<w:t xml:space=\"preserve\">{}</w:t>",
                safe_html::escape(&value[start..])
            )
            .unwrap();
        }
        output.push_str("</w:r>");
        check_size(output.len())
    }

    fn image(&mut self, item: ElementRef<'_>, output: &mut String) -> io::Result<()> {
        let uri = item.attr("src").unwrap_or_default();
        let (mut data, mut mime, mut suffix) = unpack_image(uri)?;
        let (w, h) = image::ImageReader::new(Cursor::new(&data))
            .with_guessed_format()?
            .into_dimensions()
            .map_err(io::Error::other)?;
        if mime == "image/webp" {
            let image = image::load_from_memory(&data).map_err(io::Error::other)?;
            let mut cursor = Cursor::new(Vec::new());
            image
                .write_to(&mut cursor, image::ImageFormat::Png)
                .map_err(io::Error::other)?;
            data = cursor.into_inner();
            mime = "image/png";
            suffix = "png";
        }
        let hash = format!("{:x}", Sha256::digest(&data));
        let relationship = if let Some(id) = self.images.get(&hash) {
            *id
        } else {
            self.size = self.size.saturating_add(data.len());
            check_size(self.size)?;
            let index = self.images.len() + 20;
            let path = format!("media/{hash}.{suffix}");
            self.archive
                .start_file(format!("word/{path}"), SimpleFileOptions::default())?;
            self.archive.write_all(&data)?;
            self.types.insert(suffix, mime);
            self.images.insert(hash, index);
            write!(
                self.relationships,
                r#"<Relationship Id="rId{index}" Type="{R}/image" Target="{path}"/>"#
            )
            .unwrap();
            index
        };
        let metric = |key, default| {
            item.attr(key)
                .and_then(|v| v.parse::<f64>().ok())
                .filter(|v| *v > 0.0 && *v <= 5000.0)
                .unwrap_or(default)
        };
        let width = metric("data-width", (f64::from(w) * 0.75).min(400.0));
        let height = metric("data-height", width * f64::from(h) / f64::from(w));
        let (cx, cy) = (
            (width * 12700.0).round_ties_even() as u64,
            (height * 12700.0).round_ties_even() as u64,
        );
        self.drawings += 1;
        let id = self.drawings;
        let crop =
            numbers(item.attr("data-crop"), [0; 4]).map(|value| value.clamp(-100_000, 100_000));
        let [l, t, r, b] = crop;
        let crop = if crop == [0; 4] {
            String::new()
        } else {
            format!(r#"<a:srcRect l="{l}" t="{t}" r="{r}" b="{b}"/>"#)
        };
        let position = item.attr("data-page-position");
        let container = if let Some(position) = position {
            let [x, y] = numbers(Some(position), [0; 2]).map(|value| i64::from(value) * 635);
            write!(output, r#"<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="0" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/><wp:positionH relativeFrom="page"><wp:posOffset>{x}</wp:posOffset></wp:positionH><wp:positionV relativeFrom="page"><wp:posOffset>{y}</wp:posOffset></wp:positionV><wp:extent cx="{cx}" cy="{cy}"/><wp:wrapNone/>"#).unwrap();
            "anchor"
        } else {
            write!(output, r#"<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>"#).unwrap();
            "inline"
        };
        write!(output,r#"<wp:docPr id="{id}" name="Image {id}"/><wp:cNvGraphicFramePr/><a:graphic><a:graphicData uri="{PIC}"><pic:pic><pic:nvPicPr><pic:cNvPr id="{id}" name="Image {id}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rId{relationship}"/>{crop}<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:{container}></w:drawing></w:r>"#).unwrap();
        check_size(output.len().saturating_add(self.size))
    }

    fn inline(
        &mut self,
        node: Html<'_>,
        mut style: Style,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 128 {
            return Err(invalid("전자책 DOCX 입력이 너무 깊습니다."));
        }
        if let HtmlNode::Text(text) = node.value() {
            return Self::text(&text.text, &style, output);
        }
        let Some(element) = ElementRef::wrap(node) else {
            return Ok(());
        };
        match element.value().name() {
            "img" => return self.image(element, output),
            "br" => {
                output.push_str("<w:r><w:br/></w:r>");
                return Ok(());
            }
            "b" | "strong" => style.bold = true,
            "i" | "em" => style.italic = true,
            _ => {}
        }
        if element.attr("data-page-number") == Some("1") {
            output
                .push_str(r#"<w:fldSimple w:instr=" PAGE "><w:r><w:t>1</w:t></w:r></w:fldSimple>"#);
            return Ok(());
        }
        if element.attr("data-equation") == Some("1") {
            let text = element.text().collect::<String>();
            write!(
                output,
                "<m:oMath><m:r><m:t>{}</m:t></m:r></m:oMath>",
                safe_html::escape(&text)
            )
            .unwrap();
            return Ok(());
        }
        if let Some(font) = element.attr("data-font") {
            let font = safe_html::escape(font);
            write!(
                style.metrics,
                r#"<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}" w:cs="{font}"/>"#
            )
            .unwrap();
        }
        style.bold |= element.attr("data-bold") == Some("1");
        style.italic |= element.attr("data-italic") == Some("1");
        if let Some(color) = element.attr("data-color").and_then(color) {
            write!(style.metrics, r#"<w:color w:val="{color}"/>"#).unwrap();
        }
        let [spacing] = numbers(element.attr("data-tracking"), [0]);
        let [scale] = numbers(element.attr("data-scale"), [100]);
        if spacing != 0 {
            write!(
                style.metrics,
                r#"<w:spacing w:val="{}"/>"#,
                spacing.clamp(-31680, 31680)
            )
            .unwrap();
        }
        if scale != 100 {
            write!(style.metrics, r#"<w:w w:val="{}"/>"#, scale.clamp(1, 600)).unwrap();
        }
        if let Some(size) = element
            .attr("data-size")
            .and_then(|value| value.parse::<f64>().ok())
        {
            let size = (size * 2.0).round_ties_even().clamp(2.0, 2000.0) as i32;
            write!(
                style.metrics,
                r#"<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>"#
            )
            .unwrap();
        }
        for child in element.children() {
            self.inline(child, style.clone(), output, depth + 1)?;
        }
        Ok(())
    }

    fn blocks(&mut self, node: Html<'_>, output: &mut String, depth: usize) -> io::Result<()> {
        if depth > 128 {
            return Err(invalid("전자책 DOCX 입력이 너무 깊습니다."));
        }
        (self.check)()?;
        if let HtmlNode::Text(text) = node.value() {
            if !text.text.trim().is_empty() {
                output.push_str("<w:p>");
                Self::text(&text.text, &Style::default(), output)?;
                output.push_str("</w:p>");
            }
            return Ok(());
        }
        let Some(element) = ElementRef::wrap(node) else {
            return Ok(());
        };
        match element.value().name() {
            "table" => self.table(element, output, depth + 1)?,
            "p" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "pre" | "li" | "img" => {
                output.push_str("<w:p>");
                paragraph_properties(element, output);
                self.inline(node, Style::default(), output, depth + 1)?;
                output.push_str("</w:p>");
            }
            "header" | "footer"
                if element
                    .parent()
                    .and_then(ElementRef::wrap)
                    .is_some_and(|parent| parent.value().name() == "main") => {}
            _ => {
                for child in element.children() {
                    self.blocks(child, output, depth + 1)?;
                }
            }
        }
        check_size(output.len().saturating_add(self.size))
    }

    fn table(
        &mut self,
        table: ElementRef<'_>,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        let grid = Grid::read(table)?;
        let width = grid.columns;
        if width == 0 {
            return Ok(());
        }
        let fixed = table.attr("data-fixed") == Some("1");
        let widths = values(table.attr("data-columns"));
        let widths = if widths.len() == width {
            widths
                .into_iter()
                .map(|width| width.clamp(1, 100_000))
                .collect()
        } else {
            vec![(9026 / width).max(1) as i32; width]
        };
        let custom_borders = fixed
            && grid
                .cells
                .values()
                .any(|(cell, _, _)| cell.is_some_and(|cell| cell.attr("data-borders").is_some()));
        let style = if custom_borders {
            "TableNormal"
        } else {
            "TableGrid"
        };
        write!(output, r#"<w:tbl><w:tblPr><w:tblStyle w:val="{style}"/>"#).unwrap();
        if fixed {
            let total = widths.iter().sum::<i32>();
            write!(output, r#"<w:tblW w:w="{total}" w:type="dxa"/>"#).unwrap();
        } else {
            output.push_str(r#"<w:tblW w:w="0" w:type="auto"/>"#);
        }
        if let Some(offset) = table
            .attr("data-offset")
            .and_then(|value| value.parse::<i32>().ok())
        {
            write!(
                output,
                r#"<w:tblInd w:w="{offset}" w:type="dxa"/><w:tblLayout w:type="fixed"/>"#
            )
            .unwrap();
        } else if fixed {
            output.push_str(r#"<w:tblLayout w:type="fixed"/>"#);
        }
        output.push_str("</w:tblPr><w:tblGrid>");
        for width in &widths {
            write!(output, "<w:gridCol w:w=\"{width}\"/>").unwrap();
        }
        output.push_str("</w:tblGrid>");
        for r in 0..grid.rows {
            (self.check)()?;
            output.push_str("<w:tr>");
            let height = grid.heights[r].clamp(0, 100_000);
            if height > 0 {
                write!(
                    output,
                    r#"<w:trPr><w:trHeight w:val="{height}" w:hRule="atLeast"/></w:trPr>"#
                )
                .unwrap();
            }
            let mut c = 0;
            while c < width {
                let (element, rs, cs) = grid.cells.get(&(r, c)).copied().unwrap_or((None, 1, 1));
                write!(
                    output,
                    "<w:tc><w:tcPr><w:tcW w:w=\"{}\" w:type=\"dxa\"/>",
                    widths[c..c + cs].iter().sum::<i32>()
                )
                .unwrap();
                if cs > 1 {
                    write!(output, "<w:gridSpan w:val=\"{cs}\"/>").unwrap();
                }
                if rs > 1 {
                    output.push_str(if element.is_some() {
                        "<w:vMerge w:val=\"restart\"/>"
                    } else {
                        "<w:vMerge/>"
                    });
                }
                if let Some(cell) = element {
                    cell_borders(cell, output);
                }
                if let Some(fill) = element
                    .and_then(|item| item.attr("bgcolor"))
                    .and_then(color)
                {
                    write!(
                        output,
                        r#"<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>"#
                    )
                    .unwrap();
                }
                if let Some(cell) = element {
                    if let Some(margins) = cell.attr("data-margins") {
                        output.push_str("<w:tcMar>");
                        for (edge, value) in ["top", "left", "bottom", "right"]
                            .into_iter()
                            .zip(numbers(Some(margins), [0; 4]))
                        {
                            let value = value.clamp(0, 100_000);
                            write!(output, r#"<w:{edge} w:w="{value}" w:type="dxa"/>"#).unwrap();
                        }
                        output.push_str("</w:tcMar>");
                    }
                    if let Some(value @ ("top" | "center" | "bottom")) = cell.attr("data-valign") {
                        write!(output, r#"<w:vAlign w:val="{value}"/>"#).unwrap();
                    }
                }
                output.push_str("</w:tcPr>");
                if let Some(cell) = element {
                    if cell
                        .child_elements()
                        .any(|item| matches!(item.value().name(), "p" | "table"))
                    {
                        for node in cell.children() {
                            self.blocks(node, output, depth + 1)?;
                        }
                        if !output.ends_with("</w:p>") {
                            output.push_str("<w:p/>");
                        }
                    } else {
                        output.push_str("<w:p>");
                        for node in cell.children() {
                            self.inline(node, Style::default(), output, depth + 1)?;
                        }
                        output.push_str("</w:p>");
                    }
                } else {
                    output.push_str("<w:p/>");
                }
                output.push_str("</w:tc>");
                c += cs;
            }
            output.push_str("</w:tr>");
            check_size(output.len().saturating_add(self.size))?;
        }
        output.push_str("</w:tbl>");
        Ok(())
    }
}

fn cell_borders(cell: ElementRef<'_>, output: &mut String) {
    let Some(spec) = cell.attr("data-borders") else {
        return;
    };
    let mut fields = spec.split_whitespace();
    output.push_str("<w:tcBorders>");
    for edge in ["top", "left", "bottom", "right"] {
        let (style, width, shade) = (fields.next(), fields.next(), fields.next());
        let style = style
            .filter(|style| {
                matches!(
                    *style,
                    "single"
                        | "dotted"
                        | "dashed"
                        | "dotDash"
                        | "dotDotDash"
                        | "dashLong"
                        | "double"
                        | "thinThickMediumGap"
                        | "thickThinMediumGap"
                        | "thinThickThinMediumGap"
                        | "wave"
                        | "doubleWave"
                )
            })
            .unwrap_or("nil");
        if style == "nil" {
            write!(output, r#"<w:{edge} w:val="nil"/>"#).unwrap();
        } else {
            let width = width
                .and_then(|v| v.parse::<i32>().ok())
                .unwrap_or(4)
                .clamp(2, 96);
            let shade = shade.and_then(color).unwrap_or("000000");
            write!(
                output,
                r#"<w:{edge} w:val="{style}" w:sz="{width}" w:color="{shade}"/>"#
            )
            .unwrap();
        }
    }
    output.push_str("</w:tcBorders>");
}

fn paragraph_properties(element: ElementRef<'_>, output: &mut String) {
    let has_layout = element.attr("data-indent").is_some()
        || element.attr("data-space").is_some()
        || element.attr("data-page").is_some();
    if !has_layout {
        return;
    }
    output.push_str("<w:pPr>");
    if element.attr("data-page") == Some("1") {
        output.push_str("<w:pageBreakBefore/>");
    }
    let [before, after, line] =
        numbers(element.attr("data-space"), [0; 3]).map(|value| value.clamp(0, 100_000));
    let rule = match element.attr("data-rule") {
        Some("exact") => "exact",
        Some("atLeast") => "atLeast",
        _ => "auto",
    };
    let line = if line > 0 {
        format!(r#" w:line="{line}" w:lineRule="{rule}""#)
    } else {
        String::new()
    };
    write!(
        output,
        r#"<w:spacing w:before="{before}" w:after="{after}"{line}/>"#
    )
    .unwrap();
    let [left, right, indent] =
        numbers(element.attr("data-indent"), [0; 3]).map(|value| value.clamp(-100_000, 100_000));
    let key = if indent < 0 { "hanging" } else { "firstLine" };
    let indent = indent.abs();
    write!(
        output,
        r#"<w:ind w:left="{left}" w:right="{right}" w:{key}="{indent}"/>"#
    )
    .unwrap();
    let align = match element.attr("align") {
        Some("center") => "center",
        Some("right") => "right",
        Some("justify") => "both",
        _ => "left",
    };
    write!(output, r#"<w:jc w:val="{align}"/>"#).unwrap();
    if let Some(size) = element
        .attr("data-mark-size")
        .and_then(|v| v.parse::<u16>().ok())
    {
        let size = size.clamp(2, 2000);
        write!(
            output,
            r#"<w:rPr><w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr>"#
        )
        .unwrap();
    }
    output.push_str("</w:pPr>");
}

fn template_xml(zip: &mut ZipArchive<Cursor<&[u8]>>, name: &str) -> io::Result<String> {
    let mut xml = String::new();
    zip.by_name(name)?.read_to_string(&mut xml)?;
    Ok(xml)
}

fn section(geometry: [i32; 8], references: &str, output: &mut String) {
    let [width, height, left, right, top, bottom, header, footer] = geometry;
    let orientation = if width > height {
        r#" w:orient="landscape""#
    } else {
        ""
    };
    write!(output, r#"<w:sectPr>{references}<w:type w:val="nextPage"/><w:pgSz w:w="{width}" w:h="{height}"{orientation}/><w:pgMar w:top="{top}" w:right="{right}" w:bottom="{bottom}" w:left="{left}" w:header="{header}" w:footer="{footer}" w:gutter="0"/></w:sectPr>"#).unwrap();
}

pub(super) fn write(
    book: &Book,
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    write_package(book, None, path, check)?;
    warnings.push("DOCX 속성에 제목·저자·언어·설명을 저장했습니다. 출판사와 별도 표지 지정은 포함하지 않습니다.".to_owned());
    Ok(())
}

pub(super) fn write_layout(
    html: &str,
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    if html.contains("data-equation=") {
        warnings.push("한글 수식은 편집 가능한 Office 수식의 텍스트로 옮겼습니다. 원래 수식 구조와 조판은 달라질 수 있습니다.".to_owned());
    }
    let html = super::Html::parse_document(html);
    write_package(&Book::default(), Some(&html), path, check)
}

fn write_package(
    book: &Book,
    layout: Option<&super::Html>,
    path: &Path,
    check: Check<'_>,
) -> io::Result<()> {
    let mut template = ZipArchive::new(Cursor::new(TEMPLATE))?;
    let deflate = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    let mut archive = ZipWriter::new(File::create(path)?);
    for index in 0..template.len() {
        let mut member = template.by_index(index)?;
        let name = member.name()?.into_owned();
        if ![DOC, RELS, TYPES, CORE].contains(&name.as_str()) {
            archive.start_file(name, deflate)?;
            io::copy(&mut member, &mut archive)?;
        }
    }
    let mut writer = Writer {
        archive: &mut archive,
        images: HashMap::new(),
        relationships: String::new(),
        types: BTreeMap::new(),
        drawings: 0,
        size: 0,
        check,
    };
    let namespaces = format!(
        r#"xmlns:w="{W}" xmlns:r="{R}" xmlns:wp="{WP}" xmlns:a="{A}" xmlns:pic="{PIC}" xmlns:m="{MATH}""#
    );
    let mut document = format!("<w:document {namespaces}><w:body>");
    for (index, (title, html)) in book.chapters.iter().enumerate() {
        check()?;
        document.push_str("<w:p>");
        if index > 0 {
            document.push_str("<w:pPr><w:pageBreakBefore/></w:pPr>");
        }
        write!(document,r#"<w:r><w:rPr><w:b/><w:sz w:val="32"/></w:rPr><w:t xml:space="preserve">{}</w:t></w:r></w:p>"#,safe_html::escape(title)).unwrap();
        let html = super::Html::parse_document(html);
        for node in chapter_body(&html).children() {
            writer.blocks(node, &mut document, 0)?;
        }
    }
    let mut geometry = hwp_model::GEOMETRY;
    let mut part_types = String::new();
    let mut references = String::new();
    let mut section_rels = String::new();
    let mut sections = Vec::new();
    if let Some(layout) = layout {
        let body = chapter_body(layout);
        let body = body
            .child_elements()
            .find(|item| item.value().name() == "main")
            .unwrap_or(body);
        geometry =
            numbers(body.attr("data-geometry"), geometry).map(|value| value.clamp(0, 100_000));
        let pages: Vec<_> = body
            .child_elements()
            .filter(|item| item.value().name() == "section" && item.attr("data-geometry").is_some())
            .collect();
        if pages.is_empty() {
            for node in body.children() {
                writer.blocks(node, &mut document, 0)?;
            }
        } else {
            let count = pages.len();
            for (index, page) in pages.into_iter().enumerate() {
                geometry = numbers(page.attr("data-geometry"), geometry)
                    .map(|value| value.clamp(0, 100_000));
                for node in page.children() {
                    writer.blocks(node, &mut document, 0)?;
                }
                if index + 1 < count {
                    document.push_str(r#"<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>"#);
                    section(geometry, "", &mut document);
                    document.push_str("</w:pPr></w:p>");
                }
            }
        }
        for (name, prefix) in [("header", "hdr"), ("footer", "ftr")] {
            let Some(part) = body
                .child_elements()
                .find(|item| item.value().name() == name)
            else {
                continue;
            };
            if part.first_child().is_none() {
                continue;
            }
            let mut xml = format!("<w:{prefix} {namespaces}>");
            for node in part.children() {
                writer.blocks(node, &mut xml, 0)?;
            }
            write!(xml, "</w:{prefix}>").unwrap();
            sections.push((name, xml));
            write!(
                references,
                r#"<w:{name}Reference w:type="default" r:id="rIdMagic{name}"/>"#
            )
            .unwrap();
            write!(
                section_rels,
                r#"<Relationship Id="rIdMagic{name}" Type="{R}/{name}" Target="magic-{name}.xml"/>"#
            )
            .unwrap();
            write!(part_types, r#"<Override PartName="/word/magic-{name}.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.{name}+xml"/>"#).unwrap();
        }
    }
    section(geometry, &references, &mut document);
    document.push_str("</w:body></w:document>");
    let media_rels = format!(
        r#"<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{}</Relationships>"#,
        writer.relationships
    );
    let mut content_types = template_xml(&mut template, TYPES)?;
    let mut defaults = String::new();
    for (ext, mime) in writer.types {
        if !content_types.contains(&format!("Extension=\"{ext}\"")) {
            write!(
                defaults,
                r#"<Default Extension="{ext}" ContentType="{mime}"/>"#
            )
            .unwrap();
        }
    }
    content_types = content_types.replace("</Types>", &format!("{defaults}{part_types}</Types>"));
    let rels = template_xml(&mut template, RELS)?.replace(
        "</Relationships>",
        &format!("{}{section_rels}</Relationships>", writer.relationships),
    );
    let core = format!(
        r#"<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="{DC}"><dc:title>{}</dc:title><dc:creator>{}</dc:creator><dc:language>{}</dc:language><dc:description>{}</dc:description></cp:coreProperties>"#,
        safe_html::escape(&book.title),
        safe_html::escape(&book.authors.join("; ")),
        safe_html::escape(&book.language),
        safe_html::escape(&book.description)
    );
    for (name, xml) in [
        (DOC, document),
        (RELS, rels),
        (TYPES, content_types),
        (CORE, core),
    ] {
        archive.start_file(name, deflate)?;
        archive.write_all(xml.as_bytes())?;
    }
    for (name, xml) in sections {
        archive.start_file(format!("word/magic-{name}.xml"), deflate)?;
        archive.write_all(xml.as_bytes())?;
        if media_rels.contains("<Relationship Id=") {
            archive.start_file(format!("word/_rels/magic-{name}.xml.rels"), deflate)?;
            archive.write_all(media_rels.as_bytes())?;
        }
    }
    archive.finish()?;
    Ok(())
}
