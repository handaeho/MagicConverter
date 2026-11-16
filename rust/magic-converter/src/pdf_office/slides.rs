use super::*;
use crate::pdfium::Drawing;
use pptx_image::emu;

const TABLE_NS: &str = "http://schemas.openxmlformats.org/drawingml/2006/table";

struct Fit {
    scale: f64,
    x: f64,
    y: f64,
}

impl Fit {
    fn frame(&self, bounds: Box2) -> [u64; 4] {
        [
            emu(self.x + bounds.x0 * self.scale),
            emu(self.y + bounds.y0 * self.scale),
            emu(bounds.width().max(0.1) * self.scale).max(1),
            emu(bounds.height().max(0.1) * self.scale).max(1),
        ]
    }
}

fn solid(value: Option<[u8; 4]>) -> String {
    value.map_or_else(|| "<a:noFill/>".to_owned(), |[r,g,b,a]| {
        let alpha = if a == 255 { String::new() } else { format!(r#"<a:alpha val="{}"/>"#, u32::from(a) * 100_000 / 255) };
        format!(r#"<a:solidFill><a:srgbClr val="{r:02X}{g:02X}{b:02X}">{alpha}</a:srgbClr></a:solidFill>"#)
    })
}

fn drawing_fill(shape: &Drawing) -> String {
    let (Some(gradient), Some([r, g, b, _])) = (shape.gradient, shape.fill) else {
        return solid(shape.fill);
    };
    let [start, end] = gradient.axis;
    let [dx, dy] = [end[0] - start[0], end[1] - start[1]];
    let angle =
        (dy.atan2(dx).to_degrees().rem_euclid(360.0) * 60_000.0).round() as u32 % 21_600_000;
    let mut output = String::from("<a:gradFill rotWithShape=\"1\"><a:gsLst>");
    for (position, alpha) in [0, 100000].into_iter().zip(gradient.alpha) {
        let opacity = (alpha.clamp(0.0, 1.0) * 100_000.0).round() as u32;
        write!(output, r#"<a:gs pos="{position}"><a:srgbClr val="{r:02X}{g:02X}{b:02X}"><a:alpha val="{opacity}"/></a:srgbClr></a:gs>"#).unwrap();
    }
    write!(
        output,
        r#"</a:gsLst><a:lin ang="{angle}" scaled="0"/></a:gradFill>"#
    )
    .unwrap();
    output
}

fn drawing(shape: &Drawing, fit: &Fit, id: usize, output: &mut String) {
    let [x, y, cx, cy] = fit.frame(shape.bounds);
    if let [PathCommand::Move(start), PathCommand::Line(end)] = shape.commands.as_slice() {
        let flip = if (start[0] - end[0]) * (start[1] - end[1]) < 0.0 {
            r#" flipV="1""#
        } else {
            ""
        };
        write!(output, r#"<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="{id}" name="PDF line {id}"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr><p:spPr><a:xfrm{flip}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="line"><a:avLst/></a:prstGeom><a:ln w="{}">{}</a:ln></p:spPr></p:cxnSp>"#, emu(shape.width * fit.scale).min(2_012_400), solid(shape.stroke)).unwrap();
        return;
    }
    let close = matches!(shape.commands.last(), Some(PathCommand::Close));
    let points: Vec<_> = shape
        .commands
        .iter()
        .filter_map(|command| match command {
            PathCommand::Move(point) | PathCommand::Line(point) => Some(*point),
            _ => None,
        })
        .collect();
    let bounds = shape.bounds;
    let near = |a: f64, b: f64| (a - b).abs() <= 0.1;
    let rect = close
        && matches!(points.len(), 4 | 5)
        && points.len() + 1 == shape.commands.len()
        && points.iter().all(|[x, y]| {
            (near(*x, bounds.x0) || near(*x, bounds.x1))
                && (near(*y, bounds.y0) || near(*y, bounds.y1))
        })
        && points
            .windows(2)
            .all(|p| near(p[0][0], p[1][0]) || near(p[0][1], p[1][1]));
    let center = bounds.center();
    let oval = close
        && points.len() == 1
        && shape.commands.len() == 6
        && shape.commands[1..5].iter().all(|cmd| {
            matches!(cmd, PathCommand::Cubic([_, _, [x,y]]) if
                (near(*x, center[0]) && (near(*y, bounds.y0) || near(*y, bounds.y1))) ||
                (near(*y, center[1]) && (near(*x, bounds.x0) || near(*x, bounds.x1))))
        });
    if rect || oval {
        let kind = if rect { "rect" } else { "ellipse" };
        write!(output, r#"<p:sp><p:nvSpPr><p:cNvPr id="{id}" name="PDF shape {id}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="{kind}"><a:avLst/></a:prstGeom>{}<a:ln w="{}">{}</a:ln></p:spPr></p:sp>"#,
            drawing_fill(shape), emu(shape.width * fit.scale).min(2_012_400), solid(shape.stroke)).unwrap();
        return;
    }
    let point = |output: &mut String, [x, y]: [f64; 2]| {
        write!(
            output,
            r#"<a:pt x="{}" y="{}"/>"#,
            emu((x - shape.bounds.x0) * fit.scale),
            emu((y - shape.bounds.y0) * fit.scale)
        )
        .unwrap();
    };
    let fill = if shape.fill.is_some() { "norm" } else { "none" };
    let stroke = u8::from(shape.stroke.is_some());
    write!(output, r#"<p:sp><p:nvSpPr><p:cNvPr id="{id}" name="PDF path {id}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="l" t="t" r="r" b="b"/><a:pathLst><a:path w="{cx}" h="{cy}" fill="{fill}" stroke="{stroke}">"#).unwrap();
    for command in &shape.commands {
        match *command {
            PathCommand::Move(p) => {
                output.push_str("<a:moveTo>");
                point(output, p);
                output.push_str("</a:moveTo>");
            }
            PathCommand::Line(p) => {
                output.push_str("<a:lnTo>");
                point(output, p);
                output.push_str("</a:lnTo>");
            }
            PathCommand::Cubic(points) => {
                output.push_str("<a:cubicBezTo>");
                for p in points {
                    point(output, p);
                }
                output.push_str("</a:cubicBezTo>");
            }
            PathCommand::Close => output.push_str("<a:close/>"),
        }
    }
    write!(
        output,
        "</a:path></a:pathLst></a:custGeom>{}<a:ln w=\"{}\">{}</a:ln></p:spPr></p:sp>",
        drawing_fill(shape),
        emu(shape.width * fit.scale).min(2_012_400),
        solid(shape.stroke)
    )
    .unwrap();
}

fn runs(line: &Line<'_>, scale: f64, leading: Option<f64>, margins: [f64; 2], output: &mut String) {
    let [left, after] = margins;
    let margin = emu(left * scale);
    write!(
        output,
        r#"<a:p><a:pPr fontAlgn="base" latinLnBrk="0" hangingPunct="0" marL="{margin}"><a:lnSpc>"#
    )
    .unwrap();
    if let Some(leading) = leading {
        let points = (leading * scale * 100.0).round_ties_even() as i32;
        write!(output, r#"<a:spcPts val="{points}"/>"#).unwrap();
    } else {
        output.push_str(r#"<a:spcPct val="100000"/>"#);
    }
    let after = (after * scale * 100.0).round_ties_even() as i32;
    write!(
        output,
        r#"</a:lnSpc><a:spcAft><a:spcPts val="{after}"/></a:spcAft></a:pPr>"#
    )
    .unwrap();
    join_line(line, |span, space| {
        let size = (span.size * 100.0 * scale)
            .round_ties_even()
            .clamp(100.0, 400_000.0) as u32;
        let font = escape(&span.font);
        write!(output, r#"<a:r><a:rPr sz="{size}" b="{}" i="{}">{}<a:latin typeface="{font}"/><a:ea typeface="{font}"/><a:cs typeface="{font}"/></a:rPr><a:t>{}{}</a:t></a:r>"#,
            u8::from(span.bold), u8::from(span.italic), solid(Some(span.rgba)), if space { " " } else { "" }, escape(&span.text)).unwrap();
    });
    output.push_str("</a:p>");
}

fn text(line: &Line<'_>, fit: &Fit, id: usize, output: &mut String) {
    let span = line.spans[0];
    let mut rotation = String::new();
    let mut bounds = span.selection;
    if span.rotation.abs() <= 0.001
        && let (Some([ascent, descent]), Some([_, y])) = (span.standard_font, span.origin)
    {
        bounds.y0 = y - ascent * span.size;
        bounds.y1 = y + descent * span.size;
    }
    let mut baseline = span.origin.map(|[_, y]| y - bounds.y0);
    if let Some([x, y]) = span.origin.filter(|_| span.rotation.abs() > 0.001) {
        let [cos, sin] = [span.rotation.cos(), span.rotation.sin()];
        let points = [
            [bounds.x0, bounds.y0],
            [bounds.x1, bounds.y0],
            [bounds.x1, bounds.y1],
            [bounds.x0, bounds.y1],
        ];
        let width = points
            .into_iter()
            .map(|[px, py]| (px - x) * cos + (py - y) * sin)
            .fold(line.size, f64::max);
        let height = line.size * 1.3;
        let ascent = line.size;
        let cx = x + width * cos / 2.0 + (ascent - height / 2.0) * sin;
        let cy = y + width * sin / 2.0 - (ascent - height / 2.0) * cos;
        bounds = Box2 {
            x0: cx - width / 2.0,
            y0: cy - height / 2.0,
            x1: cx + width / 2.0,
            y1: cy + height / 2.0,
        };
        rotation = format!(
            r#" rot="{}""#,
            (span.rotation.to_degrees().rem_euclid(360.0) * 60000.0).round() as i32
        );
        baseline = Some(ascent);
    }
    let leading = (line.size * 2.0).max(bounds.height() * 1.5);
    let inset = baseline
        .filter(|offset| offset.abs() <= line.size * 3.0)
        .map(|offset| ((offset - leading * 0.75) * fit.scale * 12700.0).round_ties_even() as i64);
    let [x, y, cx, cy] = fit.frame(bounds);
    let top = inset.unwrap_or(0);
    write!(output, r#"<p:sp><p:nvSpPr><p:cNvPr id="{id}" name="PDF text {id}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm{rotation}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr wrap="none" lIns="0" rIns="0" tIns="{top}" bIns="0"/><a:lstStyle/>"#).unwrap();
    runs(line, fit.scale, inset.map(|_| leading), [0.0; 2], output);
    output.push_str("</p:txBody></p:sp>");
}

type Border = (f64, [u8; 4]);

fn simple_ruling(shape: &Drawing) -> Option<(Ruling, Border)> {
    let [PathCommand::Move(start), PathCommand::Line(end)] = shape.commands.as_slice() else {
        return None;
    };
    if shape.fill.is_some() || !(0.0..=10.0).contains(&shape.width) {
        return None;
    }
    Some((segment(*start, *end)?, (shape.width, shape.stroke?)))
}

fn table_borders<'a>(table: &Table, page: &'a Page) -> Vec<(&'a Drawing, Ruling, Border)> {
    let mut edges = Vec::new();
    for shape in &page.paths {
        let Some((line, border)) = simple_ruling(shape) else {
            continue;
        };
        let (main, across) = if line.axis == 0 {
            (&table.xs, &table.ys)
        } else {
            (&table.ys, &table.xs)
        };
        let exact = |a: f64, b: f64| (a - b).abs() <= 0.1;
        if main.iter().any(|&v| exact(v, line.coordinate))
            && across.iter().any(|&v| exact(v, line.start))
            && across.iter().any(|&v| exact(v, line.end))
        {
            edges.push((shape, line, border));
        }
    }
    // Keep the original vectors when a grid edge crosses artwork or style varies.
    if edges.is_empty()
        || edges.iter().any(|entry| entry.2 != edges[0].2)
        || edges
            .iter()
            .map(|entry| entry.0.order)
            .max()
            .zip(edges.iter().map(|entry| entry.0.order).min())
            .is_some_and(|(max, min)| max - min + 1 != edges.len())
    {
        return Vec::new();
    }
    let covers = |axis, fixed, start, end| {
        edges.iter().any(|(_, line, _)| {
            line.axis == axis
                && exact_line(line.coordinate, fixed)
                && line.start <= start + 0.1
                && line.end >= end - 0.1
        })
    };
    if table.cells.iter().all(|cell| {
        let b = cell.bounds;
        covers(0, b.x0, b.y0, b.y1)
            && covers(0, b.x1, b.y0, b.y1)
            && covers(1, b.y0, b.x0, b.x1)
            && covers(1, b.y1, b.x0, b.x1)
    }) {
        edges
    } else {
        Vec::new()
    }
}

fn exact_line(a: f64, b: f64) -> bool {
    (a - b).abs() <= 0.1
}

fn cell_text_layout(lines: &[Line<'_>], cell: &Cell) -> Option<(f64, Vec<[f64; 3]>)> {
    let mut measured = Vec::new();
    for line in lines {
        let [left, baseline] = line.spans[0].origin?;
        if left < cell.bounds.x0 || left > cell.bounds.x1 || line.bounds.height() > line.size * 1.5
        {
            return None;
        }
        measured.push((left - cell.bounds.x0, baseline, line.size * 2.0));
    }
    let (_, first, leading) = *measured.first()?;
    let top = first - cell.bounds.y0 - leading * 0.75;
    let (_, last, leading) = *measured.last()?;
    if top < 0.0 || last + leading * 0.25 > cell.bounds.y1 {
        return None;
    }
    let layout = measured
        .iter()
        .enumerate()
        .map(|(index, &(left, y, leading))| {
            let after = measured
                .get(index + 1)
                .map_or(0.0, |&(_, next, next_leading)| {
                    next - y - leading * 0.25 - next_leading * 0.75
                });
            (after >= -0.01).then_some([left, leading, after.max(0.0)])
        })
        .collect::<Option<Vec<_>>>()?;
    Some((top, layout))
}

fn table(
    table: &Table,
    page: &Page,
    fit: &Fit,
    id: usize,
    border: Option<Border>,
    output: &mut String,
) {
    let [x, y, cx, cy] = fit.frame(table.bounds);
    write!(output, r#"<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{id}" name="PDF table {id}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm><a:graphic><a:graphicData uri="{TABLE_NS}"><a:tbl><a:tblPr/><a:tblGrid>"#).unwrap();
    for w in table.xs.windows(2) {
        write!(
            output,
            r#"<a:gridCol w="{}"/>"#,
            emu((w[1] - w[0]) * fit.scale)
        )
        .unwrap();
    }
    output.push_str("</a:tblGrid>");
    let mut positions = BTreeMap::new();
    for cell in &table.cells {
        for r in cell.row..cell.row + cell.rows {
            for c in cell.col..cell.col + cell.cols {
                positions.insert((r, c), cell);
            }
        }
    }
    for (row, h) in table.ys.windows(2).enumerate() {
        write!(output, r#"<a:tr h="{}">"#, emu((h[1] - h[0]) * fit.scale)).unwrap();
        for col in 0..table.xs.len() - 1 {
            let Some(cell) = positions.get(&(row, col)) else {
                continue;
            };
            output.push_str("<a:tc");
            if row > cell.row {
                output.push_str(" vMerge=\"1\"");
            }
            if col > cell.col {
                output.push_str(" hMerge=\"1\"");
            }
            if row == cell.row && cell.rows > 1 {
                write!(output, " rowSpan=\"{}\"", cell.rows).unwrap();
            }
            if col == cell.col && cell.cols > 1 {
                write!(output, " gridSpan=\"{}\"", cell.cols).unwrap();
            }
            output.push_str(r#"><a:txBody><a:bodyPr/><a:lstStyle/>"#);
            let lines = if row == cell.row && col == cell.col {
                lines(content(cell.bounds, page))
            } else {
                Vec::new()
            };
            if lines.is_empty() {
                output.push_str("<a:p/>");
            }
            let text_layout = cell_text_layout(&lines, cell);
            for (index, line) in lines.iter().enumerate() {
                if let Some((_, measures)) = &text_layout {
                    let [left, leading, after] = measures[index];
                    runs(line, fit.scale, Some(leading), [left, after], output);
                } else {
                    runs(line, fit.scale, None, [0.0; 2], output);
                }
            }
            let [left, right, top, bottom] =
                text_layout.map_or([3.0, 3.0, 1.0, 1.0], |(top, _)| [0.0, 0.0, top, 0.0]);
            let [left, right, top, bottom] = [left, right, top, bottom].map(|n| emu(n * fit.scale));
            write!(
                output,
                r#"</a:txBody><a:tcPr marL="{left}" marR="{right}" marT="{top}" marB="{bottom}">"#
            )
            .unwrap();
            if let Some((stroke, rgba)) = border {
                let width = emu(stroke * fit.scale).max(1);
                for (name, visible) in [
                    ("lnL", col == cell.col),
                    ("lnR", col + 1 == cell.col + cell.cols),
                    ("lnT", row == cell.row),
                    ("lnB", row + 1 == cell.row + cell.rows),
                ] {
                    write!(
                        output,
                        r#"<a:{name} w="{width}">{}</a:{name}>"#,
                        solid(visible.then_some(rgba))
                    )
                    .unwrap();
                }
            }
            output.push_str("<a:noFill/></a:tcPr></a:tc>");
        }
        output.push_str("</a:tr>");
    }
    output.push_str("</a:tbl></a:graphicData></a:graphic></p:graphicFrame>");
}

pub(super) fn slides(
    source: &Path,
    output: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut deck = pptx_image::Presentation::new(output)?;
    let (mut flat, mut scan) = (0, 0);
    let (mut page_number, mut estimated) = (0, 0);
    pdfium::pages(source, true, true, check, |page| {
        page_number += 1;
        let (scale, x, y) = deck.fit(page.bounds.width(), page.bounds.height())?;
        let fit = Fit { scale, x, y };
        let charts = charts::detect(&page, check)?;
        let chart_objects: HashSet<_> = charts
            .iter()
            .flat_map(|chart| chart.owners.iter().copied())
            .collect();
        let tables: Vec<_> = tables(&page)
            .into_iter()
            .filter(|table| {
                !content(table.bounds, &page).any(|span| chart_objects.contains(&span.order))
            })
            .collect();
        let table_edges: Vec<_> = tables
            .iter()
            .map(|table| table_borders(table, &page))
            .collect();
        let absorbed: HashSet<_> = table_edges
            .iter()
            .flatten()
            .map(|(shape, _, _)| shape.order)
            .collect();
        let mut shapes = Vec::new();
        let mut id = 2;
        scan += usize::from(page.scanned);
        for shape in &page.paths {
            check()?;
            if absorbed.contains(&shape.order) || chart_objects.contains(&shape.order) {
                continue;
            }
            let mut xml = String::new();
            drawing(shape, &fit, id, &mut xml);
            shapes.push((shape.order, xml));
            id += 1;
        }
        for (index, picture) in page.pictures.iter().enumerate() {
            check()?;
            let xml = pptx_image::picture(fit.frame(picture.bounds), id, index + 2);
            shapes.push((picture.order, xml));
            flat += usize::from(picture.flattened);
            id += 1;
        }
        for (item, edges) in tables.iter().zip(table_edges) {
            let order = content(item.bounds, &page)
                .map(|span| span.order)
                .max()
                .unwrap_or(0);
            let mut xml = String::new();
            table(
                item,
                &page,
                &fit,
                id,
                edges.first().map(|entry| entry.2),
                &mut xml,
            );
            shapes.push((order, xml));
            id += 1;
        }
        for span in page.spans.iter().filter(|span| {
            !chart_objects.contains(&span.order)
                && !tables
                    .iter()
                    .any(|table| table.bounds.contains(span.bounds.center()))
        }) {
            check()?;
            let mut xml = String::new();
            let line = Line {
                spans: vec![span],
                bounds: span.bounds,
                size: span.size,
            };
            text(&line, &fit, id, &mut xml);
            shapes.push((span.order, xml));
            id += 1;
        }
        let mut parts = Vec::new();
        for chart in &charts {
            check()?;
            let relationship = page.pictures.len() + 2 + parts.len();
            let [x, y, cx, cy] = fit.frame(chart.bounds);
            let uri = pptx_image::CHART_NS;
            let count = chart.estimated();
            estimated += count;
            let xml = format!(
                r#"<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{id}" name="PDF recovered chart" descr="PDF page {page_number}, {count} estimated values. See the PDF Recovery worksheet."/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr><p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm><a:graphic><a:graphicData uri="{uri}"><c:chart xmlns:c="{uri}" r:id="rId{relationship}"/></a:graphicData></a:graphic></p:graphicFrame>"#
            );
            shapes.push((*chart.owners.iter().min().expect("chart objects"), xml));
            parts.push(chart.part(page_number, scale)?);
            id += 1;
        }
        if shapes.iter().map(|(_, xml)| xml.len()).sum::<usize>() > 64 * 1024 * 1024 {
            return Err(io::Error::other(
                "PDF 슬라이드 개체가 크기 한도를 초과했습니다.",
            ));
        }
        shapes.sort_by_key(|(order, _)| *order);
        let shapes: String = shapes.into_iter().map(|(_, xml)| xml).collect();
        deck.slide_with_charts(
            &shapes,
            page.pictures.iter().map(|image| image.png.as_slice()),
            &parts,
        )
    })?;
    if flat > scan {
        warnings.push(format!(
            "클리핑·양식·복합 그래픽 {}개 영역은 그림으로 옮겼습니다.",
            flat - scan
        ));
    }
    if scan > 0 {
        warnings.push(format!(
            "스캔 PDF {scan}쪽은 그림으로 옮겼습니다. 스캔 문서 OCR은 지원하지 않습니다."
        ));
    }
    if estimated > 0 {
        warnings.push(format!("PDF 차트 값 {estimated}개를 축 눈금과 도형 위치에서 추정했습니다. 포함된 통합 문서의 PDF Recovery 시트에서 확인하세요."));
    }
    deck.finish()
}
