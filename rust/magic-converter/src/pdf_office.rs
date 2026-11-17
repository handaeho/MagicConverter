use crate::{Edge, hwp_model, pdfium, pptx_image, safe_html::escape};
use pdfium::{Box2, Page, PathCommand, Span};
use std::collections::{BTreeMap, HashSet};
use std::fmt::Write as _;
use std::io;
use std::path::Path;

mod charts;
type Check<'a> = &'a dyn Fn() -> io::Result<()>;
const SNAP: f64 = 1.25;

pub fn supports(edge: &Edge) -> bool {
    edge.engine_id == "pdf"
        && matches!(
            edge.action_key.as_str(),
            "document:docx" | "document:xlsx" | "document:pptx" | "pptx_editable"
        )
}

struct Line<'a> {
    spans: Vec<&'a Span>,
    bounds: Box2,
    size: f64,
}

fn lines<'a>(spans: impl IntoIterator<Item = &'a Span>) -> Vec<Line<'a>> {
    let mut spans: Vec<_> = spans.into_iter().collect();
    spans.sort_by(|a, b| a.bounds.center()[1].total_cmp(&b.bounds.center()[1]));
    let mut lines: Vec<Line<'a>> = Vec::new();
    for span in spans {
        let same = |line: &Line<'_>| {
            (line.bounds.center()[1] - span.bounds.center()[1]).abs() <= (span.size * 0.35).max(2.0)
        };
        if let Some(line) = lines.last_mut().filter(|line| same(line)) {
            line.bounds = line.bounds.union(span.bounds);
            line.size = line.size.max(span.size);
            line.spans.push(span);
        } else {
            lines.push(Line {
                spans: vec![span],
                bounds: span.bounds,
                size: span.size,
            });
        }
    }
    for line in &mut lines {
        line.spans
            .sort_by(|a, b| a.bounds.x0.total_cmp(&b.bounds.x0));
    }
    lines
}

fn join_line(line: &Line<'_>, mut item: impl FnMut(&Span, bool)) {
    let mut end = f64::INFINITY;
    let mut trailing_space = true;
    for span in &line.spans {
        let space = !trailing_space
            && !span.text.starts_with(char::is_whitespace)
            && span.bounds.x0 - end > span.size * 0.16;
        item(span, space);
        trailing_space = span.text.ends_with(char::is_whitespace);
        end = span.bounds.x1;
    }
}

fn plain(line: &Line<'_>) -> String {
    let mut text = String::new();
    join_line(line, |span, space| {
        if space {
            text.push(' ');
        }
        text.push_str(&span.text);
    });
    text.trim().to_owned()
}

fn span_html(line: &Line<'_>, output: &mut String) {
    join_line(line, |span, space| {
        let [r, g, b, _] = span.rgba;
        write!(output, r#"<span data-font="{}" data-size="{}" data-bold="{}" data-italic="{}" data-color="{r:02X}{g:02X}{b:02X}">{}{}</span>"#, escape(&span.font), span.size, u8::from(span.bold), u8::from(span.italic), if space { " " } else { "" }, escape(&span.text)).unwrap();
    });
}

#[derive(Clone, Copy)]
struct Ruling {
    axis: usize,
    coordinate: f64,
    start: f64,
    end: f64,
}

fn segment(a: [f64; 2], b: [f64; 2]) -> Option<Ruling> {
    let axis = if (a[0] - b[0]).abs() <= SNAP && (a[1] - b[1]).abs() > 3.0 {
        0
    } else if (a[1] - b[1]).abs() <= SNAP && (a[0] - b[0]).abs() > 3.0 {
        1
    } else {
        return None;
    };
    Some(Ruling {
        axis,
        coordinate: (a[axis] + b[axis]) * 0.5,
        start: a[1 - axis].min(b[1 - axis]),
        end: a[1 - axis].max(b[1 - axis]),
    })
}

fn rulings(page: &Page) -> Vec<Ruling> {
    let mut segments = Vec::new();
    for path in &page.paths {
        if path.stroke.is_none() && path.fill.is_none() {
            continue;
        }
        let mut first = None;
        let mut previous = None;
        for command in &path.commands {
            let next = match *command {
                PathCommand::Move(point) => {
                    first = Some(point);
                    previous = Some(point);
                    continue;
                }
                PathCommand::Line(point) => Some(point),
                PathCommand::Close => first,
                PathCommand::Cubic(points) => {
                    previous = Some(points[2]);
                    continue;
                }
            };
            if let Some((a, b)) = previous.zip(next)
                && let Some(ruling) = segment(a, b)
            {
                segments.push(ruling);
                if segments.len() > 1600 {
                    return Vec::new();
                }
            }
            previous = next;
        }
    }
    segments
}

fn connected(a: Ruling, b: Ruling) -> bool {
    let near = |value, start, end| value >= start - SNAP && value <= end + SNAP;
    if a.axis == b.axis {
        (a.coordinate - b.coordinate).abs() <= SNAP
            && a.start <= b.end + SNAP
            && b.start <= a.end + SNAP
    } else {
        near(a.coordinate, b.start, b.end) && near(b.coordinate, a.start, a.end)
    }
}

struct Cell {
    row: usize,
    col: usize,
    rows: usize,
    cols: usize,
    bounds: Box2,
}

struct Table {
    xs: Vec<f64>,
    ys: Vec<f64>,
    cells: Vec<Cell>,
    bounds: Box2,
}

fn grid(component: &[Ruling]) -> Option<Table> {
    let coordinates = |axis| {
        let mut values: Vec<_> = component
            .iter()
            .filter(|line| line.axis == axis)
            .map(|line| line.coordinate)
            .collect();
        values.sort_by(f64::total_cmp);
        values.dedup_by(|a, b| (*a - *b).abs() <= SNAP);
        values
    };
    let (xs, ys) = (coordinates(0), coordinates(1));
    if xs.len() < 2 || ys.len() < 2 || xs.len() > 100 || ys.len() > 100 || xs.len() + ys.len() < 5 {
        return None;
    }
    let (cols, rows) = (xs.len() - 1, ys.len() - 1);
    let covers = |axis, fixed: f64, start: f64, end: f64| {
        let mut lines: Vec<_> = component
            .iter()
            .filter(|line| line.axis == axis && (line.coordinate - fixed).abs() <= SNAP)
            .collect();
        lines.sort_by(|a, b| a.start.total_cmp(&b.start));
        let mut cursor = start;
        for line in lines {
            if line.start > cursor + SNAP {
                break;
            }
            cursor = cursor.max(line.end);
            if cursor >= end - SNAP {
                return true;
            }
        }
        false
    };
    let bounds = Box2::from_points([[xs[0], ys[0]], [xs[cols], ys[rows]]]);
    let mut seen = HashSet::new();
    let mut cells = Vec::new();
    for r in 0..rows {
        for c in 0..cols {
            if seen.contains(&(r, c)) {
                continue;
            }
            let mut right = c + 1;
            while right < cols && !covers(0, xs[right], ys[r], ys[r + 1]) {
                right += 1;
            }
            let mut bottom = r + 1;
            while bottom < rows && !covers(1, ys[bottom], xs[c], xs[right]) {
                bottom += 1;
            }
            if !covers(0, xs[c], ys[r], ys[bottom])
                || !covers(0, xs[right], ys[r], ys[bottom])
                || !covers(1, ys[r], xs[c], xs[right])
                || !covers(1, ys[bottom], xs[c], xs[right])
            {
                return None;
            }
            for rr in r..bottom {
                for cc in c..right {
                    if !seen.insert((rr, cc)) {
                        return None;
                    }
                }
            }
            cells.push(Cell {
                row: r,
                col: c,
                rows: bottom - r,
                cols: right - c,
                bounds: Box2::from_points([[xs[c], ys[r]], [xs[right], ys[bottom]]]),
            });
        }
    }
    Some(Table {
        xs,
        ys,
        cells,
        bounds,
    })
}

fn tables(page: &Page) -> Vec<Table> {
    detect_tables(page, false)
}

// PDF table outlines can be open: a filled header can replace a top rule,
// and horizontal row rules can end at an unpainted left or right edge.
fn open_grid(component: &[Ruling], existing: Option<&Table>) -> Option<Table> {
    let mut borders = [[f64::INFINITY, f64::NEG_INFINITY]; 2];
    for line in component {
        let [lo, hi] = &mut borders[line.axis];
        *lo = lo.min(line.coordinate);
        *hi = hi.max(line.coordinate);
        let [lo, hi] = &mut borders[1 - line.axis];
        *lo = lo.min(line.start);
        *hi = hi.max(line.end);
    }
    let mut extended = component.to_vec();
    for axis in 0..=1 {
        let [start, end] = borders[1 - axis];
        for (index, coordinate) in borders[axis].into_iter().enumerate() {
            let mut ends: Vec<_> = component
                .iter()
                .filter_map(|line| {
                    if line.axis == axis {
                        return None;
                    }
                    let tip = if index == 0 { line.start } else { line.end };
                    ((tip - coordinate).abs() <= SNAP).then_some(line.coordinate)
                })
                .collect();
            ends.sort_by(f64::total_cmp);
            ends.dedup_by(|a, b| (*a - *b).abs() <= SNAP);
            if ends.len() >= 3
                && ends[ends.len() - 1] - ends[0] >= (end - start) * 0.5
                && !component
                    .iter()
                    .any(|line| line.axis == axis && (line.coordinate - coordinate).abs() <= SNAP)
            {
                extended.push(Ruling {
                    axis,
                    coordinate,
                    start,
                    end,
                });
            }
        }
    }
    if extended.len() == component.len() {
        return None;
    }
    let table = grid(&extended)?;
    if table.xs.len() < 4
        || table.ys.len() < 4
        || existing.is_some_and(|old| table.cells.len() <= old.cells.len())
    {
        return None;
    }
    Some(table)
}

fn detect_tables(page: &Page, recover_open_edges: bool) -> Vec<Table> {
    let segments = rulings(page);
    let mut seen = vec![false; segments.len()];
    let mut result = Vec::new();
    for i in 0..segments.len() {
        if seen[i] {
            continue;
        }
        let mut work = vec![i];
        let mut component = Vec::new();
        seen[i] = true;
        while let Some(index) = work.pop() {
            component.push(segments[index]);
            for (j, segment) in segments.iter().enumerate() {
                if !seen[j] && connected(*segment, segments[index]) {
                    seen[j] = true;
                    work.push(j);
                }
            }
        }
        let closed = grid(&component);
        let open = if recover_open_edges {
            open_grid(&component, closed.as_ref()).filter(|table| {
                closed.as_ref().is_none_or(|old| {
                    content(table.bounds, page)
                        .any(|span| !old.bounds.contains(span.bounds.center()))
                })
            })
        } else {
            None
        };
        if let Some(table) = open.or(closed)
            && page
                .spans
                .iter()
                .any(|span| table.bounds.contains(span.bounds.center()))
        {
            result.push(table);
        }
    }
    result.sort_by(|a, b| {
        a.bounds
            .y0
            .total_cmp(&b.bounds.y0)
            .then(a.bounds.x0.total_cmp(&b.bounds.x0))
    });
    result
}

fn content(bounds: Box2, page: &Page) -> impl Iterator<Item = &Span> {
    page.spans
        .iter()
        .filter(move |span| bounds.contains(span.bounds.center()))
}

fn twips(points: f64) -> i32 {
    (points * 20.0).round_ties_even() as i32
}

fn word_table(table: &Table, page: &Page, output: &mut String) {
    let columns = table
        .xs
        .windows(2)
        .map(|w| twips(w[1] - w[0]).to_string())
        .collect::<Vec<_>>()
        .join(" ");
    let offset = twips(table.bounds.x0);
    write!(
        output,
        r#"<table data-columns="{columns}" data-offset="{offset}">"#
    )
    .unwrap();
    for (row, pair) in table.ys.windows(2).enumerate() {
        let height = twips(pair[1] - pair[0]);
        write!(output, r#"<tr data-height="{height}">"#).unwrap();
        for cell in table.cells.iter().filter(|cell| cell.row == row) {
            write!(
                output,
                r#"<td data-col="{}" rowspan="{}" colspan="{}">"#,
                cell.col, cell.rows, cell.cols
            )
            .unwrap();
            for line in lines(content(cell.bounds, page)) {
                let height = twips(line.size * 1.15);
                write!(output, r#"<p data-space="0 0 {height}" data-rule="exact">"#).unwrap();
                span_html(&line, output);
                output.push_str("</p>");
            }
            output.push_str("</td>");
        }
        output.push_str("</tr>");
    }
    output.push_str("</table>");
}

enum Element<'a> {
    Text(Line<'a>),
    Picture(usize),
    Table(usize),
}

fn word(
    source: &Path,
    output: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut html = String::from("<main>");
    let mut scanned = 0;
    let mut flattened = 0;
    pdfium::pages(source, true, false, check, |page| {
        let (width, height) = (twips(page.bounds.width()), twips(page.bounds.height()));
        if width > 31680 || height > 31680 {
            return Err(io::Error::other(
                "PDF 페이지 크기가 Word 용지 범위를 벗어납니다.",
            ));
        }
        write!(
            html,
            r#"<section data-geometry="{width} {height} 0 0 0 0 0 0">"#
        )
        .unwrap();
        scanned += usize::from(page.scanned);
        flattened += page.pictures.iter().filter(|image| image.flattened).count();
        let tables = tables(&page);
        let mut elements: Vec<_> = tables
            .iter()
            .enumerate()
            .map(|(i, table)| (table.bounds, Element::Table(i)))
            .collect();
        elements.extend(
            page.pictures
                .iter()
                .enumerate()
                .map(|(i, picture)| (picture.bounds, Element::Picture(i))),
        );
        let outside = page.spans.iter().filter(|span| {
            !tables
                .iter()
                .any(|table| table.bounds.contains(span.bounds.center()))
        });
        elements.extend(
            lines(outside)
                .into_iter()
                .map(|line| (line.bounds, Element::Text(line))),
        );
        elements.sort_by(|a, b| a.0.y0.total_cmp(&b.0.y0).then(a.0.x0.total_cmp(&b.0.x0)));
        let mut cursor = 0.0_f64;
        for (bounds, element) in elements {
            check()?;
            let gap = twips((bounds.y0 - cursor).max(0.0));
            let left = twips(bounds.x0.max(0.0));
            let emitted = match element {
                Element::Text(line) => {
                    let height = twips(line.size * 1.15);
                    write!(html, r#"<p data-indent="{left} 0 0" data-space="{gap} 0 {height}" data-rule="exact">"#).unwrap();
                    span_html(&line, &mut html);
                    html.push_str("</p>");
                    line.size * 1.15
                }
                Element::Picture(index) => {
                    let picture = &page.pictures[index];
                    let width = bounds.width().max(1.0);
                    let height = bounds.height().max(1.0);
                    let image =
                        hwp_model::image_html(&picture.png, Some(width), Some(height), [0; 4])?;
                    if page.scanned {
                        let position =
                            format!(r#"<img data-page-position="{left} {}" "#, twips(bounds.y0));
                        let image = image.replacen("<img ", &position, 1);
                        write!(html, r#"<p data-space="0 0 20" data-rule="exact" data-mark-size="2">{image}</p>"#).unwrap();
                    } else {
                        write!(html, r#"<p data-indent="{left} 0 0" data-space="{gap} 0 {}" data-rule="atLeast" data-mark-size="2">{image}</p>"#,
                            twips(height)).unwrap();
                    }
                    height
                }
                Element::Table(index) => {
                    if gap > 0 {
                        write!(html, r#"<p data-space="0 0 {gap}" data-rule="exact"></p>"#)
                            .unwrap();
                    }
                    word_table(&tables[index], &page, &mut html);
                    bounds.height()
                }
            };
            cursor = cursor.max(bounds.y0) + emitted;
            if html.len() > 256 * 1024 * 1024 {
                return Err(io::Error::other(
                    "PDF의 Word 문서 모델이 크기 한도를 초과했습니다.",
                ));
            }
        }
        html.push_str("</section>");
        Ok(())
    })?;
    html.push_str("</main>");
    if scanned > 0 {
        warnings.push(format!("스캔 PDF {scanned}쪽은 이미지로 보존했습니다. 표·글자 편집에는 OCR 및 표 구조 인식이 필요합니다."));
    }
    if flattened > scanned {
        warnings.push(format!(
            "복합 그래픽 {}개 영역은 그림으로 옮겼습니다.",
            flattened - scanned
        ));
    }
    crate::ebook::layout_docx(&html, output, check, warnings)
}

fn workbook(source: &Path, output: &Path, check: Check<'_>) -> io::Result<()> {
    let mut workbook = rust_xlsxwriter::Workbook::new();
    let (mut count, mut found) = (0, false);
    pdfium::pages(source, false, false, check, |page| {
        count += 1;
        let sheet = workbook
            .add_worksheet()
            .set_name(format!("Page {count}"))
            .map_err(io::Error::other)?;
        let tables = tables(&page);
        let mut row = 0_u32;
        if tables.is_empty() {
            sheet.set_column_width(0, 70).map_err(io::Error::other)?;
            for line in lines(&page.spans) {
                check()?;
                let text = plain(&line);
                found |= !text.is_empty();
                sheet.write_string(row, 0, text).map_err(io::Error::other)?;
                row += 1;
            }
        } else {
            sheet.set_column_width(0, 24).map_err(io::Error::other)?;
            for table in tables {
                for cell in &table.cells {
                    check()?;
                    let text = lines(content(cell.bounds, &page))
                        .iter()
                        .map(plain)
                        .collect::<Vec<_>>()
                        .join("\n");
                    found |= !text.is_empty();
                    sheet
                        .write_string(row + cell.row as u32, cell.col as u16, text)
                        .map_err(io::Error::other)?;
                }
                row += table.ys.len() as u32;
            }
        }
        Ok(())
    })?;
    if !found {
        return Err(io::Error::other(
            "PDF에 추출할 표/텍스트가 없습니다. 스캔 문서 OCR은 지원하지 않습니다.",
        ));
    }
    workbook.save(output).map_err(io::Error::other)
}

pub fn convert(
    source: &Path,
    output: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    match edge.target.as_str() {
        "docx" => word(source, output, check, warnings),
        "xlsx" => workbook(source, output, check),
        _ => slides(source, output, check, warnings),
    }
}

mod slides;
use slides::slides;
