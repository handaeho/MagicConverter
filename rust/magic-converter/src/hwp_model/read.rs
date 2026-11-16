use super::*;
use std::collections::HashMap;
use std::fs::File;
use zip::ZipArchive;

fn numeric(item: Option<Node<'_, '_>>, key: &str, default: i32) -> i32 {
    item.and_then(|item| item.attribute(key))
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn twips(units: i32) -> i32 {
    (f64::from(units) / 5.0).round_ties_even() as i32
}

fn stored_line_height(p: Node<'_, '_>) -> Option<i32> {
    let mut segments = tag(p, "linesegarray")?
        .children()
        .filter(|item| item.tag_name().name() == "lineseg")
        .map(|item| {
            let read = |key| item.attribute(key).unwrap_or("0").parse::<i32>().ok();
            let height = read("vertsize")?.checked_add(read("spacing")?)?;
            (1..=50_000).contains(&height).then_some(height)
        });
    let height = segments.next()??;
    segments
        .all(|value| value == Some(height))
        .then_some(height)
}

fn cell_borders(fill: Node<'_, '_>, output: &mut String) {
    output.push_str(" data-borders=\"");
    for edge in ["topBorder", "leftBorder", "bottomBorder", "rightBorder"] {
        let line = tag(fill, edge);
        let style = match line.and_then(|node| node.attribute("type")) {
            None | Some("NONE") => "nil",
            Some("DOT") => "dotted",
            Some("DASH") => "dashed",
            Some("DASH_DOT") => "dotDash",
            Some("DASH_DOT_DOT") => "dotDotDash",
            Some("LONG_DASH") => "dashLong",
            Some("DOUBLE_SLIM") => "double",
            Some("SLIM_THICK") => "thinThickMediumGap",
            Some("THICK_SLIM") => "thickThinMediumGap",
            Some("SLIM_THICK_SLIM") => "thinThickThinMediumGap",
            Some("WAVE") => "wave",
            Some("DOUBLEWAVE") => "doubleWave",
            _ => "single",
        };
        let width = line
            .and_then(|node| node.attribute("width"))
            .and_then(|width| width.strip_suffix("mm"))
            .and_then(|width| width.trim().parse::<f64>().ok())
            .filter(|width| width.is_finite() && *width > 0.0)
            .map_or(4, |width| {
                (width * 576.0 / 25.4).round_ties_even().clamp(2.0, 96.0) as i32
            });
        let color = line
            .and_then(|node| node.attribute("color"))
            .and_then(color)
            .unwrap_or("000000");
        write!(output, "{style} {width} {color} ").unwrap();
    }
    output.push('"');
}

fn all<'a, 'b>(node: Node<'a, 'b>, name: &'a str) -> impl Iterator<Item = Node<'a, 'b>> + 'a {
    node.descendants()
        .filter(move |child| child.tag_name().name() == name)
}

struct Hwp<'a, 'b> {
    archive: ZipArchive<File>,
    assets: HashMap<String, String>,
    fonts: HashMap<(&'a str, &'a str), &'a str>,
    styles: HashMap<&'a str, Node<'a, 'b>>,
    paragraphs: HashMap<&'a str, Node<'a, 'b>>,
    fills: HashMap<&'a str, &'a str>,
    borders: HashMap<&'a str, Node<'a, 'b>>,
    headers: String,
    footers: String,
    dropped: bool,
    check: Check<'a>,
}

impl Hwp<'_, '_> {
    fn picture(&mut self, item: Node<'_, '_>) -> io::Result<String> {
        (self.check)()?;
        let path = all(item, "img")
            .next()
            .and_then(|node| node.attribute("binaryItemIDRef"))
            .and_then(|id| self.assets.get(id))
            .ok_or_else(|| invalid("HWPX 그림의 연결 데이터를 찾을 수 없습니다."))?;
        let bytes = word_model::read_part(&mut self.archive, path)?;
        let size = tag(item, "sz");
        let width = f64::from(numeric(size, "width", 0)) / 100.0;
        let height = f64::from(numeric(size, "height", 0)) / 100.0;
        let original = tag(item, "orgSz");
        let dims = tag(item, "imgDim");
        let w = numeric(dims, "dimwidth", numeric(original, "width", 0));
        let h = numeric(dims, "dimheight", numeric(original, "height", 0));
        let clip = tag(item, "imgClip");
        let ratio = |n, full| {
            (f64::from(n) * 100_000.0 / f64::from(full))
                .round_ties_even()
                .clamp(0.0, 100_000.0) as i32
        };
        let clip = if w > 0 && h > 0 {
            [
                ratio(numeric(clip, "left", 0), w),
                ratio(numeric(clip, "top", 0), h),
                100_000 - ratio(numeric(clip, "right", w), w),
                100_000 - ratio(numeric(clip, "bottom", h), h),
            ]
        } else {
            [0; 4]
        };
        image_html(&bytes, Some(width), Some(height), clip)
    }

    fn text(&self, value: &str, style: Option<Node<'_, '_>>, output: &mut String) {
        if value.is_empty() {
            return;
        }
        let size = numeric(style, "height", 1000).clamp(100, 100_000);
        let font_refs = style.and_then(|item| tag(item, "fontRef"));
        let spacing = style.and_then(|item| tag(item, "spacing"));
        let ratio = style.and_then(|item| tag(item, "ratio"));
        let color = style
            .and_then(|item| item.attribute("textColor"))
            .and_then(color)
            .unwrap_or("000000");
        let bold = style.is_some_and(|item| tag(item, "bold").is_some());
        let italic = style.is_some_and(|item| tag(item, "italic").is_some());
        let space = style.is_some_and(|item| item.attribute("useFontSpace") == Some("1"));
        let script = |ch: char| match ch as u32 {
            0x1100..=0x11ff | 0x3130..=0x318f | 0xa960..=0xa97f | 0xac00..=0xd7ff => "hangul",
            0x3400..=0x9fff | 0xf900..=0xfaff | 0x20000..=0x2ffff => "hanja",
            0x3040..=0x30ff | 0xff66..=0xff9f => "japanese",
            0x0000..=0x024f => "latin",
            0x2190..=0x2bff => "symbol",
            _ => "other",
        };
        let mut segments = Vec::new();
        let mut previous = "";
        for (index, ch) in value.char_indices() {
            let current = if ch.is_whitespace() && !previous.is_empty() {
                previous
            } else {
                script(ch)
            };
            if previous != current {
                segments.push((index, current));
                previous = current;
            }
        }
        for (index, &(start, key)) in segments.iter().enumerate() {
            let end = segments.get(index + 1).map_or(value.len(), |item| item.0);
            let font = font_refs
                .and_then(|item| item.attribute(key))
                .and_then(|id| self.fonts.get(&(key, id)))
                .copied()
                .unwrap_or("맑은 고딕");
            let tracking = numeric(spacing, key, 0).clamp(-50, 50);
            let tracking = (f64::from(tracking) * f64::from(size) / 500.0).round_ties_even() as i32;
            let scale = numeric(ratio, key, 100).clamp(50, 200);
            let value = escape(&value[start..end]).replace('\n', "<br>");
            write!(output, r#"<span data-font="{}" data-size="{}" data-bold="{}" data-italic="{}" data-color="{color}" data-tracking="{tracking}" data-scale="{scale}" data-font-space="{}">{value}</span>"#, escape(font), f64::from(size) / 100.0, u8::from(bold), u8::from(italic), u8::from(space)).unwrap();
        }
    }

    fn table(&mut self, item: Node<'_, '_>, output: &mut String, depth: usize) -> io::Result<()> {
        let rows = numeric(Some(item), "rowCnt", 1);
        let columns = numeric(Some(item), "colCnt", 1);
        if rows <= 0
            || columns <= 0
            || columns > 16_384
            || i64::from(rows) * i64::from(columns) > 100_000
        {
            return Err(invalid("HWPX 표의 크기가 허용 범위를 벗어납니다."));
        }
        let mut widths = vec![0; columns as usize];
        let mut heights = vec![0; rows as usize];
        let mut grid = vec![Vec::new(); rows as usize];
        for (row, group) in item
            .children()
            .filter(|node| node.tag_name().name() == "tr")
            .enumerate()
        {
            for (column, cell) in group
                .children()
                .filter(|node| node.tag_name().name() == "tc")
                .enumerate()
            {
                (self.check)()?;
                let address = tag(cell, "cellAddr");
                let span = tag(cell, "cellSpan");
                let size = tag(cell, "cellSz");
                let r = numeric(address, "rowAddr", row as i32);
                let c = numeric(address, "colAddr", column as i32);
                let rs = numeric(span, "rowSpan", 1);
                let cs = numeric(span, "colSpan", 1);
                if r < 0
                    || c < 0
                    || rs < 1
                    || cs < 1
                    || r.saturating_add(rs) > rows
                    || c.saturating_add(cs) > columns
                {
                    return Err(invalid("HWPX 표 병합 범위가 올바르지 않습니다."));
                }
                let width = twips(numeric(size, "width", 0));
                let height = twips(numeric(size, "height", 0));
                let (r, c, rs, cs) = (r as usize, c as usize, rs as usize, cs as usize);
                if cs == 1 {
                    widths[c] = widths[c].max(width);
                }
                if rs == 1 {
                    heights[r] = heights[r].max(height);
                }
                grid[r].push((c, rs, cs, width, height, cell));
            }
        }
        for (r, row) in grid.iter().enumerate() {
            for &(c, rs, cs, width, height, _) in row {
                let distribute = |slots: &mut [i32], size| {
                    let zeros = slots.iter().filter(|value| **value == 0).count() as i32;
                    if zeros > 0 {
                        let known = slots.iter().map(|v| i64::from(*v)).sum::<i64>();
                        let remainder =
                            ((i64::from(size) - known) / i64::from(zeros)).clamp(0, 100_000) as i32;
                        slots
                            .iter_mut()
                            .filter(|v| **v == 0)
                            .for_each(|v| *v = remainder);
                    }
                };
                distribute(&mut widths[c..c + cs], width);
                distribute(&mut heights[r..r + rs], height);
            }
        }
        let widths = widths
            .into_iter()
            .map(|width| if width > 0 { width } else { 1440 }.to_string())
            .collect::<Vec<_>>()
            .join(" ");
        write!(output, r#"<table data-columns="{widths}" data-fixed="1">"#).unwrap();
        let table_margins = tag(item, "inMargin");
        for (r, mut row) in grid.into_iter().enumerate() {
            write!(output, r#"<tr data-height="{}">"#, heights[r]).unwrap();
            row.sort_by_key(|item| item.0);
            for (c, rs, cs, _, _, cell) in row {
                let fill = cell
                    .attribute("borderFillIDRef")
                    .and_then(|id| self.fills.get(id))
                    .map_or_else(String::new, |fill| format!(r##" bgcolor="#{fill}""##));
                write!(
                    output,
                    r#"<td data-col="{c}" rowspan="{rs}" colspan="{cs}"{fill}"#
                )
                .unwrap();
                if let Some(border) = cell
                    .attribute("borderFillIDRef")
                    .and_then(|id| self.borders.get(id))
                {
                    cell_borders(*border, output);
                }
                let margins = if cell.attribute("hasMargin") == Some("1") {
                    tag(cell, "cellMargin")
                } else {
                    table_margins
                };
                if let Some(margins) = margins {
                    output.push_str(" data-margins=\"");
                    for key in ["top", "left", "bottom", "right"] {
                        let width = twips(numeric(Some(margins), key, 0).max(0));
                        write!(output, "{width} ").unwrap();
                    }
                    output.push('"');
                }
                let body = tag(cell, "subList");
                if let Some(alignment) = body.and_then(|body| body.attribute("vertAlign")) {
                    let alignment = match alignment {
                        "BOTTOM" => "bottom",
                        "CENTER" => "center",
                        _ => "top",
                    };
                    write!(output, r#" data-valign="{alignment}""#).unwrap();
                }
                output.push('>');
                if let Some(body) = body {
                    self.blocks(body, output, depth + 1)?;
                }
                output.push_str("</td>");
            }
            output.push_str("</tr>");
        }
        output.push_str("</table>");
        Ok(())
    }

    fn inline(
        &mut self,
        item: Node<'_, '_>,
        style: Option<Node<'_, '_>>,
        parts: &mut Vec<(bool, String)>,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 64 {
            return Err(invalid("HWPX의 개체 중첩 한도를 초과했습니다."));
        }
        let out = &mut parts.last_mut().unwrap().1;
        match item.tag_name().name() {
            "t" => {
                let mut value = String::new();
                for child in item.children() {
                    value.push_str(if child.is_text() {
                        child.text().unwrap_or_default()
                    } else {
                        match child.tag_name().name() {
                            "lineBreak" => "\n",
                            "tab" => "\t",
                            "nbSpace" | "fwSpace" => " ",
                            _ => "",
                        }
                    });
                }
                self.text(&value, style, out);
            }
            "tbl" => {
                let mut table = String::new();
                self.table(item, &mut table, depth + 1)?;
                parts.push((true, table));
                parts.push((false, String::new()));
            }
            "pic" => out.push_str(&self.picture(item)?),
            "textart" => {
                self.text(item.attribute("text").unwrap_or_default(), style, out);
                self.dropped = true;
            }
            "equation" => {
                let text = tag(item, "script")
                    .and_then(|item| item.text())
                    .unwrap_or_default();
                write!(out, r#"<span data-equation="1">{}</span>"#, escape(text)).unwrap();
            }
            "autoNum" if item.attribute("numType") == Some("PAGE") => {
                out.push_str(r#"<span data-page-number="1">1</span>"#)
            }
            "header" | "footer" => {
                if let Some(body) = tag(item, "subList") {
                    let mut markup = String::new();
                    self.blocks(body, &mut markup, depth + 1)?;
                    let target = if item.tag_name().name() == "header" {
                        &mut self.headers
                    } else {
                        &mut self.footers
                    };
                    if target.is_empty() {
                        *target = markup;
                    }
                }
            }
            "switch" => {
                if let Some(branch) = tag(item, "case").or_else(|| tag(item, "default")) {
                    for child in branch.children().filter(Node::is_element) {
                        self.inline(child, style, parts, depth + 1)?;
                    }
                }
            }
            "secPr" | "linesegarray" | "lineSegArray" => {}
            "line" | "rect" | "ole" | "chart" | "arc" | "ellipse" | "polygon" => {
                self.dropped = true
            }
            _ => {
                for child in item.children().filter(Node::is_element) {
                    self.inline(child, style, parts, depth + 1)?;
                }
            }
        }
        Ok(())
    }

    fn blocks(
        &mut self,
        parent: Node<'_, '_>,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        for p in parent
            .children()
            .filter(|item| item.tag_name().name() == "p")
        {
            (self.check)()?;
            let prop = p
                .attribute("paraPrIDRef")
                .and_then(|id| self.paragraphs.get(id))
                .copied();
            let align = match prop
                .and_then(|item| tag(item, "align"))
                .and_then(|item| item.attribute("horizontal"))
            {
                Some("RIGHT") => "right",
                Some("CENTER") => "center",
                Some("JUSTIFY" | "DISTRIBUTE" | "DISTRIBUTE_SPACE") => "justify",
                _ => "left",
            };
            let margin = prop.and_then(|item| all(item, "margin").next());
            let metric = |key| twips(numeric(margin.and_then(|item| tag(item, key)), "value", 0));
            let (left, right, indent, before, after) = (
                metric("left"),
                metric("right"),
                metric("intent"),
                metric("prev"),
                metric("next"),
            );
            let spacing = prop.and_then(|item| all(item, "lineSpacing").next());
            let value = numeric(spacing, "value", 160);
            let mut parts = vec![(false, String::new())];
            let mut first_size = None;
            let mut largest_text = None;
            for run in p.children().filter(|item| item.tag_name().name() == "run") {
                let style = run
                    .attribute("charPrIDRef")
                    .and_then(|id| self.styles.get(id))
                    .copied();
                let size = numeric(style, "height", 1000).clamp(100, 100_000);
                first_size.get_or_insert(size);
                if all(run, "t").any(|item| item.children().any(|child| child.is_text())) {
                    largest_text = Some(largest_text.unwrap_or(0).max(size));
                }
                for item in run.children().filter(Node::is_element) {
                    self.inline(item, style, &mut parts, depth + 1)?;
                }
            }
            let mut page = u8::from(p.attribute("pageBreak") == Some("1"));
            let is_blank = parts.len() == 1;
            for (table, text) in parts {
                if table {
                    if page == 1 {
                        output.push_str(r#"<p data-page="1"></p>"#);
                        page = 0;
                    }
                    output.push_str(&text);
                } else if is_blank || !text.is_empty() {
                    let inline_object = text.contains("<img ") || text.contains("data-equation=");
                    let hint = stored_line_height(p).filter(|height| {
                        is_blank
                            && !inline_object
                            && largest_text.is_none_or(|size| size <= *height)
                    });
                    let (rule, line) = if let Some(height) = hint {
                        ("exact", twips(height))
                    } else {
                        match spacing.and_then(|item| item.attribute("type")) {
                            Some("FIXED") => ("exact", twips(value)),
                            Some("BETWEEN_LINES" | "AT_LEAST") => ("atLeast", twips(value)),
                            _ if inline_object => (
                                "atLeast",
                                (f64::from(largest_text.unwrap_or(1000)) * f64::from(value) / 500.0)
                                    .round_ties_even() as i32,
                            ),
                            _ => ("auto", (f64::from(value) * 2.4).round_ties_even() as i32),
                        }
                    };
                    let mark = if text.is_empty() {
                        let size =
                            (f64::from(first_size.unwrap_or(1000)) / 50.0).round_ties_even() as i32;
                        format!(r#" data-mark-size="{size}""#)
                    } else {
                        String::new()
                    };
                    write!(output, r#"<p align="{align}" data-indent="{left} {right} {indent}" data-space="{before} {after} {line}" data-rule="{rule}" data-page="{page}"{mark}>{text}</p>"#).unwrap();
                    page = 0;
                }
            }
            if output.len() > 256 * 1024 * 1024 {
                return Err(invalid("HWPX 문서 모델이 크기 한도를 초과했습니다."));
            }
        }
        Ok(())
    }
}

pub(super) fn hwpx(
    source: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<String> {
    use crate::hwpx_text as package;
    let mut archive = package::package(source)?;
    let order = package::order(&mut archive)?;
    let header = package::part(&mut archive, "Contents/header.xml")?;
    let header = Document::parse(&header).map_err(io::Error::other)?;
    let content = package::part(&mut archive, "Contents/content.hpf")?;
    let content = Document::parse(&content).map_err(io::Error::other)?;
    let mut assets = HashMap::new();
    for asset in all(content.root(), "item") {
        if let Some((id, href)) = asset.attribute("id").zip(asset.attribute("href")) {
            let path = percent_encoding::percent_decode_str(href)
                .decode_utf8()
                .map_err(io::Error::other)?;
            let path = path.trim_start_matches("./");
            if path.contains(['\\', ':', '\0'])
                || path.split('/').any(|part| matches!(part, "." | ".."))
            {
                continue;
            }
            for candidate in [path.to_owned(), format!("Contents/{path}")] {
                if archive.index_for_name(&candidate).is_some() {
                    assets.insert(id.to_owned(), candidate);
                    break;
                }
            }
        }
    }
    let mut fonts = HashMap::new();
    for group in all(header.root(), "fontface") {
        let key = match group.attribute("lang") {
            Some("LATIN") => "latin",
            Some("HANGUL") => "hangul",
            Some("HANJA") => "hanja",
            Some("JAPANESE") => "japanese",
            Some("OTHER") => "other",
            Some("SYMBOL") => "symbol",
            Some("USER") => "user",
            _ => continue,
        };
        for font in group
            .children()
            .filter(|node| node.tag_name().name() == "font")
        {
            if let Some((id, face)) = font.attribute("id").zip(font.attribute("face")) {
                fonts.insert((key, id), face);
            }
        }
    }
    let by_id = |name| {
        all(header.root(), name)
            .filter_map(|item| item.attribute("id").map(|id| (id, item)))
            .collect()
    };
    let fills = all(header.root(), "borderFill")
        .filter_map(|item| {
            Some((
                item.attribute("id")?,
                all(item, "winBrush")
                    .next()?
                    .attribute("faceColor")
                    .and_then(color)?,
            ))
        })
        .collect();
    let mut reader = Hwp {
        archive,
        assets,
        fonts,
        styles: by_id("charPr"),
        paragraphs: by_id("paraPr"),
        borders: by_id("borderFill"),
        fills,
        headers: String::new(),
        footers: String::new(),
        dropped: false,
        check,
    };
    let mut geometry = GEOMETRY;
    let mut body = String::new();
    for (index, name) in order.iter().enumerate() {
        let source = package::part(&mut reader.archive, name)?;
        let section = Document::parse(&source).map_err(io::Error::other)?;
        if index == 0
            && let Some(page) = all(section.root(), "pagePr").next()
        {
            let margin = tag(page, "margin");
            let get = |name, default| twips(numeric(margin, name, default * 5).clamp(0, 500_000));
            let top = get("top", geometry[4]);
            let bottom = get("bottom", geometry[5]);
            geometry = [
                twips(numeric(Some(page), "width", geometry[0] * 5)),
                twips(numeric(Some(page), "height", geometry[1] * 5)),
                get("left", geometry[2]),
                get("right", geometry[3]),
                top + get("header", 0),
                bottom + get("footer", 0),
                top,
                bottom,
            ];
        } else if index > 0 {
            body.push_str(r#"<p data-page="1"></p>"#);
        }
        reader.blocks(section.root_element(), &mut body, 0)?;
    }
    if order.len() > 1 {
        warnings.push(
            "여러 HWPX 구역의 용지와 머리말·꼬리말에는 첫 구역 설정을 적용했습니다.".to_owned(),
        );
    }
    if reader.dropped {
        warnings.push("일부 HWPX 선·도형·차트·포함 개체는 DOCX에 옮기지 못했습니다.".to_owned());
    }
    let geometry = geometry.map(|value| value.to_string()).join(" ");
    Ok(format!(
        r#"<main data-geometry="{geometry}"><header>{}</header>{body}<footer>{}</footer></main>"#,
        reader.headers, reader.footers
    ))
}

fn slide_table(table: Node<'_, '_>, output: &mut String) {
    output.push_str("<table>");
    for row in table
        .children()
        .filter(|item| item.tag_name().name() == "tr")
    {
        output.push_str("<tr>");
        for (index, cell) in row
            .children()
            .filter(|item| item.tag_name().name() == "tc")
            .enumerate()
        {
            if ["hMerge", "vMerge"]
                .iter()
                .any(|attr| matches!(cell.attribute(*attr), Some("1" | "true")))
            {
                continue;
            }
            let rs = numeric(Some(cell), "rowSpan", 1);
            let cs = numeric(Some(cell), "gridSpan", 1);
            write!(
                output,
                r#"<td data-col="{index}" rowspan="{rs}" colspan="{cs}">"#
            )
            .unwrap();
            if let Some(text) = tag(cell, "txBody") {
                slide_paragraphs(text, output);
            }
            output.push_str("</td>");
        }
        output.push_str("</tr>");
    }
    output.push_str("</table>");
}

fn slide_paragraphs(text: Node<'_, '_>, output: &mut String) {
    for p in text.children().filter(|item| item.tag_name().name() == "p") {
        output.push_str("<p>");
        for item in p.children().filter(Node::is_element) {
            if item.tag_name().name() == "br" {
                output.push_str("<br>");
            } else if let Some(value) = tag(item, "t").and_then(|item| item.text()) {
                output.push_str(&escape(value));
            }
        }
        output.push_str("</p>");
    }
}

pub(super) fn slides(
    source: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<String> {
    let mut archive = word_model::package(source, check)?;
    let part = "ppt/presentation.xml";
    let bytes = word_model::read_part(&mut archive, part)?;
    let doc = parse(&bytes)?;
    let rels = word_model::relationships(&mut archive, part)?;
    let mut output = String::new();
    for (index, id) in all(doc.root(), "sldId").enumerate() {
        check()?;
        let path = id
            .attribute((
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                "id",
            ))
            .and_then(|id| rels.get(id))
            .ok_or_else(|| invalid("PowerPoint 슬라이드의 연결 항목을 찾을 수 없습니다."))?;
        let bytes = word_model::read_part(&mut archive, path)?;
        let doc = parse(&bytes)?;
        let rels = word_model::relationships(&mut archive, path)?;
        write!(
            output,
            r#"<p><span data-size="14" data-bold="1">슬라이드 {}</span></p>"#,
            index + 1
        )
        .unwrap();
        let tree = all(doc.root(), "spTree")
            .next()
            .ok_or_else(|| invalid("PowerPoint 슬라이드 개체 목록이 없습니다."))?;
        for shape in tree.descendants().filter(|shape| {
            matches!(shape.tag_name().name(), "sp" | "pic" | "graphicFrame")
                && shape
                    .ancestors()
                    .skip(1)
                    .take_while(|node| *node != tree)
                    .all(|node| node.tag_name().name() == "grpSp")
        }) {
            check()?;
            if let Some(text) = tag(shape, "txBody") {
                slide_paragraphs(text, &mut output);
            }
            if let Some(table) = all(shape, "tbl").next() {
                slide_table(table, &mut output);
            }
            if shape.tag_name().name() == "pic" {
                let reference = all(shape, "blip")
                    .next()
                    .and_then(|node| {
                        node.attribute((
                            "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                            "embed",
                        ))
                    })
                    .and_then(|id| rels.get(id))
                    .ok_or_else(|| invalid("PowerPoint 그림의 연결 항목을 찾을 수 없습니다."))?;
                let bytes = word_model::read_part(&mut archive, reference)?;
                let extent = tag(shape, "spPr")
                    .and_then(|node| tag(node, "xfrm"))
                    .and_then(|node| tag(node, "ext"));
                let points = |name| Some(f64::from(numeric(extent, name, 0)) / 12_700.0);
                let crop = all(shape, "srcRect").next();
                let crop = ["l", "t", "r", "b"].map(|name| numeric(crop, name, 0));
                write!(
                    output,
                    "<p>{}</p>",
                    image_html(&bytes, points("cx"), points("cy"), crop)?
                )
                .unwrap();
            }
            if output.len() > 256 * 1024 * 1024 {
                return Err(invalid("슬라이드 문서 모델이 크기 한도를 초과했습니다."));
            }
        }
    }
    warnings.push("슬라이드의 본문·표·그림을 문서 순서로 옮겼습니다. 차트·도형·애니메이션과 원래 배치는 보존하지 않습니다.".to_owned());
    Ok(output)
}
