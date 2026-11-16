use super::{Box2, Page, PathCommand, Span, escape};
use crate::pdfium::Drawing;
use crate::pptx_image::{CHART_NS, ChartPart};
use rust_xlsxwriter::Workbook;
use std::collections::HashSet;
use std::fmt::Write;
use std::io;

#[derive(Clone, Copy)]
struct Mark<'a> {
    shape: &'a Drawing,
    bounds: Box2,
}

struct Axis<'a> {
    ticks: Vec<&'a Span>,
    min: f64,
    max: f64,
    step: f64,
    slope: f64,
    intercept: f64,
    percent: bool,
}

impl Axis<'_> {
    fn at(&self, value: f64) -> f64 {
        (value - self.intercept) / self.slope
    }
}

struct Point {
    value: f64,
    label: Option<String>,
    color: [u8; 4],
}

struct Series {
    name: Option<String>,
    points: Vec<Point>,
    line: Option<LineStyle>,
    secondary: bool,
}

struct LineStyle {
    width: f64,
    marker: Option<(f64, [u8; 4])>,
}

pub(super) struct Chart {
    pub bounds: Box2,
    pub owners: HashSet<usize>,
    plot: Box2,
    categories: Vec<String>,
    series: Vec<Series>,
    horizontal: bool,
    scale: [f64; 3],
    secondary: Option<[f64; 3]>,
    percent: bool,
    gap: u32,
    font_size: f64,
    grid: Option<[u8; 4]>,
    between_categories: bool,
    legend: bool,
}

fn number(text: &str) -> Option<f64> {
    let value = text.trim();
    let percent = value.ends_with('%');
    let value = value.trim_end_matches('%').trim().replace(['−', '–'], "-");
    let value = if value.contains(',') {
        let integer = value.trim_start_matches(['+', '-']).split('.').next()?;
        let mut groups = integer.split(',');
        if !(1..=3).contains(&groups.next()?.len())
            || !groups.all(|group| group.len() == 3 && group.bytes().all(|b| b.is_ascii_digit()))
        {
            return None;
        }
        value.replace(',', "")
    } else {
        value
    };
    let value = value.parse::<f64>().ok()?;
    (value.is_finite() && value.abs() <= 1e12).then_some(if percent {
        value / 100.0
    } else {
        value
    })
}

fn rectangle(points: &[[f64; 2]]) -> Option<Box2> {
    let bounds = Box2::from_points(points.iter().copied());
    if !matches!(points.len(), 4 | 5) || bounds.width().min(bounds.height()) <= 1.0 {
        return None;
    }
    let mut corners = 0_u8;
    for &[x, y] in points {
        let bit =
            u8::from((x - bounds.x1).abs() < 0.06) + 2 * u8::from((y - bounds.y1).abs() < 0.06);
        if ((x - bounds.x0).abs().min((x - bounds.x1).abs()) > 0.06)
            || ((y - bounds.y0).abs().min((y - bounds.y1).abs()) > 0.06)
        {
            return None;
        }
        corners |= 1 << bit;
    }
    (corners == 15
        && points
            .iter()
            .zip(points.iter().cycle().skip(1))
            .all(|(a, b)| (a[0] - b[0]).abs().min((a[1] - b[1]).abs()) < 0.06))
    .then_some(bounds)
}

fn rectangles(shape: &Drawing) -> Option<Vec<Mark<'_>>> {
    let [r, g, b, a] = shape.fill?;
    if a != 255 || r.min(g).min(b) > 245 {
        return None;
    }
    let mut marks = Vec::new();
    let mut points = Vec::new();
    for command in &shape.commands {
        match *command {
            PathCommand::Move(point) => {
                if !points.is_empty() {
                    marks.push(Mark {
                        shape,
                        bounds: rectangle(&points)?,
                    });
                    points.clear();
                }
                points.push(point);
            }
            PathCommand::Line(point) if (1..5).contains(&points.len()) => points.push(point),
            PathCommand::Close => {
                marks.push(Mark {
                    shape,
                    bounds: rectangle(&points)?,
                });
                points.clear();
            }
            _ => return None,
        }
    }
    if !points.is_empty() {
        marks.push(Mark {
            shape,
            bounds: rectangle(&points)?,
        });
    }
    (!marks.is_empty()).then_some(marks)
}

fn find_axis<'a>(
    page: &'a Page,
    horizontal: bool,
    near: f64,
    low: f64,
    high: f64,
    right: bool,
) -> Option<Axis<'a>> {
    let mut labels: Vec<_> = page
        .spans
        .iter()
        .filter_map(|label| {
            let [x, y] = label.selection.center();
            let distance = if horizontal {
                label.selection.y0 - near
            } else if right {
                label.selection.x0 - near
            } else {
                near - label.selection.x1
            };
            let position = if horizontal { x } else { y };
            ((2.0..=45.0).contains(&distance)
                && (position >= low - 90.0 || !horizontal)
                && (position <= high + 90.0 || horizontal))
                .then(|| Some((label, number(&label.text)?)))
                .flatten()
        })
        .take(201)
        .collect();
    if labels.len() > 200 {
        return None;
    }
    labels.sort_by(|a, b| coordinate(a.0, horizontal).total_cmp(&coordinate(b.0, horizontal)));
    let sign = if horizontal { 1.0 } else { -1.0 };
    let mut best = None;
    for (first, alignment) in labels
        .iter()
        .flat_map(|(span, _)| [(span, 0), (span, 1), (span, 2)])
    {
        let edge = |span: &Span| {
            let bounds = span.selection;
            match (horizontal, alignment) {
                (false, 0) => bounds.x1,
                (false, 1) => bounds.center()[0],
                (false, _) => bounds.x0,
                (true, 0) => bounds.y0,
                (true, 1) => bounds.center()[1],
                (true, _) => bounds.y1,
            }
        };
        let row: Vec<_> = labels
            .iter()
            .copied()
            .filter(|(l, _)| (edge(first) - edge(l)).abs() < 2.0)
            .collect();
        if row.len() < 3
            || best
                .as_ref()
                .is_some_and(|axis: &Axis<'_>| axis.ticks.len() >= row.len())
        {
            continue;
        }
        let (a, min0) = row[0];
        let (b, max0) = row[row.len() - 1];
        let (start, end) = (coordinate(a, horizontal), coordinate(b, horizontal));
        let (min, max) = (min0.min(max0), min0.max(max0));
        if start > low + 3.0 || end < high - 3.0 || end - start < 15.0 {
            continue;
        }
        let step = (max - min) / (row.len() - 1) as f64;
        let slope = (max0 - min0) / (end - start);
        let intercept = min0 - slope * start;
        if slope * sign <= 0.0
            || step <= 0.0
            || row
                .windows(2)
                .any(|pair| ((pair[1].1 - pair[0].1) * sign - step).abs() > step * 0.025)
            || row.iter().any(|(l, v)| {
                (slope * coordinate(l, horizontal) + intercept - v).abs() > step * 0.035
            })
        {
            continue;
        }
        let percent = row
            .iter()
            .all(|(label, _)| label.text.trim_end().ends_with('%'));
        best = Some(Axis {
            ticks: row.into_iter().map(|(l, _)| l).collect(),
            min,
            max,
            step,
            slope,
            intercept,
            percent,
        });
    }
    best
}

fn coordinate(label: &Span, horizontal: bool) -> f64 {
    label.selection.center()[usize::from(!horizontal)]
}

fn regular(positions: &[f64]) -> Option<f64> {
    let pitch = (positions.last()? - positions[0]) / (positions.len() - 1) as f64;
    (pitch >= 6.0
        && positions
            .windows(2)
            .all(|pair| (pair[1] - pair[0] - pitch).abs() < pitch * 0.11))
    .then_some(pitch)
}

fn extent(mark: Mark<'_>, dimension: usize) -> [f64; 2] {
    let r = mark.bounds;
    if dimension == 0 {
        [r.x0, r.x1]
    } else {
        [r.y0, r.y1]
    }
}

fn chart(
    page: &Page,
    marks: &[Mark<'_>],
    horizontal: bool,
    baseline: f64,
    reserved: &HashSet<usize>,
) -> Option<Chart> {
    let d = usize::from(horizontal);
    let value = 1 - d;
    let mut marks = marks.to_vec();
    let width = |mark: Mark<'_>| {
        let [a, b] = extent(mark, d);
        b - a
    };
    let center = |mark: Mark<'_>| mark.bounds.center()[d];
    let bar_width = width(*marks.first()?);
    if marks
        .iter()
        .any(|&m| (width(m) - bar_width).abs() > bar_width * 0.12)
    {
        return None;
    }
    marks.sort_by(|a, b| center(*a).total_cmp(&center(*b)));
    let positions: Vec<_> = marks.iter().map(|&m| center(m)).collect();
    let mut clusters: Vec<Vec<Mark<'_>>> = Vec::new();
    let inner = positions
        .windows(2)
        .map(|p| p[1] - p[0])
        .min_by(f64::total_cmp)?;
    if inner < bar_width * 0.85 {
        return None;
    }
    if regular(&positions).is_some() {
        clusters.extend(marks.iter().map(|&m| vec![m]));
    } else {
        for (i, &m) in marks.iter().enumerate() {
            if i == 0 || positions[i] - positions[i - 1] > inner * 1.28 {
                clusters.push(Vec::new());
            }
            clusters.last_mut()?.push(m);
        }
    }
    let count = clusters.first()?.len();
    if !(3..=100).contains(&clusters.len())
        || !(1..=6).contains(&count)
        || clusters.iter().any(|c| c.len() != count)
    {
        return None;
    }
    let centers: Vec<_> = clusters
        .iter()
        .map(|c| c.iter().map(|&m| center(m)).sum::<f64>() / count as f64)
        .collect();
    let pitch = regular(&centers)?;
    let (near, far) = (centers[0] - pitch / 2.0, centers.last()? + pitch / 2.0);
    let low = marks
        .iter()
        .map(|&m| extent(m, value)[0])
        .min_by(f64::total_cmp)?;
    let high = marks
        .iter()
        .map(|&m| extent(m, value)[1])
        .max_by(f64::total_cmp)?;
    let mut axis = find_axis(
        page,
        horizontal,
        if horizontal { far } else { near },
        low,
        high,
        false,
    )?;
    let anchor = [axis.min, 0.0, axis.max].into_iter().min_by(|a, b| {
        (axis.at(*a) - baseline)
            .abs()
            .total_cmp(&(axis.at(*b) - baseline).abs())
    })?;
    if (axis.at(anchor) - baseline).abs() > 2.0 {
        return None;
    }
    axis.intercept = anchor - axis.slope * baseline;
    let [start, end] = if horizontal {
        [axis.at(axis.min), axis.at(axis.max)]
    } else {
        [axis.at(axis.max), axis.at(axis.min)]
    };
    let plot = if horizontal {
        Box2 {
            x0: start,
            y0: near,
            x1: end,
            y1: far,
        }
    } else {
        Box2 {
            x0: near,
            y0: start,
            x1: far,
            y1: end,
        }
    };
    let mut owners: HashSet<_> = axis.ticks.iter().map(|span| span.order).collect();
    let mut bounds = axis
        .ticks
        .iter()
        .fold(plot, |b, span| b.union(span.selection));
    let categories: Vec<_> = centers
        .iter()
        .map(|&center| {
            let label = page
                .spans
                .iter()
                .filter(|span| {
                    let distance = if horizontal {
                        start - span.selection.x1
                    } else {
                        span.selection.y0 - end
                    };
                    distance >= 1.0
                        && distance <= (pitch * 1.3).min(70.0)
                        && (span.selection.center()[d] - center).abs() < pitch * 0.38
                })
                .min_by(|a, b| {
                    let distance = |span: &Span| {
                        if horizontal {
                            start - span.selection.x1
                        } else {
                            span.selection.y0 - end
                        }
                    };
                    distance(a).total_cmp(&distance(b))
                })?;
            owners.insert(label.order);
            bounds = bounds.union(label.selection);
            Some(label.text.trim().to_owned())
        })
        .collect::<Option<_>>()?;
    let mut series = Vec::new();
    for slot in 0..count {
        let mut points = Vec::new();
        for category in &clusters {
            let mark = category[slot];
            let edge = extent(mark, value)
                .into_iter()
                .max_by(|a, b| (a - baseline).abs().total_cmp(&(b - baseline).abs()))?;
            if edge < start - 1.5 || edge > end + 1.5 {
                return None;
            }
            owners.insert(mark.shape.order);
            points.push(point(
                page,
                &axis,
                [center(mark), edge],
                horizontal,
                pitch,
                mark.shape.fill?,
                (&mut owners, &mut bounds),
            ));
        }
        series.push(Series {
            name: None,
            points,
            line: None,
            secondary: false,
        });
    }
    let mut second_scale = None;
    if !horizontal {
        for shape in &page.paths {
            let Some(vertices) = polyline(shape) else {
                continue;
            };
            if vertices.len() == centers.len()
                && vertices
                    .iter()
                    .zip(&centers)
                    .all(|(p, x)| (p[0] - x).abs() < pitch * 0.08)
            {
                if series.len() >= 8 {
                    return None;
                }
                let ymin = vertices.iter().map(|p| p[1]).min_by(f64::total_cmp)?;
                let ymax = vertices.iter().map(|p| p[1]).max_by(f64::total_cmp)?;
                let second = find_axis(page, false, far, ymin, ymax, true);
                let score = |candidate: &Axis<'_>| {
                    vertices
                        .iter()
                        .filter(|&&p| observed(page, candidate, p, false, pitch, &owners).is_some())
                        .count()
                };
                let other = second
                    .as_ref()
                    .filter(|s| score(s) > score(&axis) && score(s) * 5 >= vertices.len() * 3);
                let scale = other.unwrap_or(&axis);
                if vertices.iter().any(|&p| {
                    !(scale.min - 0.01..=scale.max + 0.01)
                        .contains(&(p[1] * scale.slope + scale.intercept))
                }) {
                    return None;
                }
                if let Some(other) = other {
                    second_scale = Some([other.min, other.max, other.step]);
                    for tick in &other.ticks {
                        owners.insert(tick.order);
                        bounds = bounds.union(tick.selection);
                    }
                }
                owners.insert(shape.order);
                let line = line_style(page, shape, &vertices, &mut owners)?;
                let points = vertices
                    .into_iter()
                    .map(|pos| {
                        point(
                            page,
                            scale,
                            pos,
                            false,
                            pitch,
                            shape.stroke.unwrap(),
                            (&mut owners, &mut bounds),
                        )
                    })
                    .collect();
                series.push(Series {
                    name: None,
                    points,
                    line: Some(line),
                    secondary: other.is_some(),
                });
            }
        }
    }
    let mut grid = None;
    for shape in &page.paths {
        if let ([PathCommand::Move(a), PathCommand::Line(b)], Some(stroke)) =
            (shape.commands.as_slice(), shape.stroke)
        {
            let [a, b] = if a[d] < b[d] { [a, b] } else { [b, a] };
            if (a[d] - near).abs() < 2.0
                && (b[d] - far).abs() < 2.0
                && (a[value] - b[value]).abs() < 0.2
                && axis
                    .ticks
                    .iter()
                    .any(|l| (coordinate(l, horizontal) - a[value]).abs() < 2.0)
            {
                owners.insert(shape.order);
                grid = Some(stroke);
            }
        }
    }
    // Match visible legend swatches to one series by color and a nearby label.
    let mut legend = false;
    if count > 1 {
        for item in &mut series {
            let color = item.points[0].color;
            let pairs: Vec<_> = page
                .paths
                .iter()
                .filter(|p| !owners.contains(&p.order) && p.fill == Some(color))
                .filter_map(|p| {
                    let items = rectangles(p)?;
                    let [mark] = items.as_slice() else {
                        return None;
                    };
                    let r = mark.bounds;
                    if r.width() > 30.0 || r.height() > 15.0 || !plot.contains(r.center()) {
                        return None;
                    }
                    let label = page
                        .spans
                        .iter()
                        .filter(|s| {
                            !owners.contains(&s.order)
                                && number(&s.text).is_none()
                                && (1.0..=48.0).contains(&(s.selection.x0 - r.x1))
                                && (s.selection.center()[1] - r.center()[1]).abs() < s.size * 0.55
                        })
                        .min_by(|a, b| a.selection.x0.total_cmp(&b.selection.x0))?;
                    Some((p, label))
                })
                .collect();
            if let [(path, label)] = pairs.as_slice() {
                owners.insert(path.order);
                owners.insert(label.order);
                bounds = bounds.union(label.selection);
                item.name = Some(label.text.clone());
                legend = true;
            }
        }
    }
    // Reject a whole paint object if any of its subpaths or text runs would be lost.
    if owners.iter().any(|id| reserved.contains(id))
        || page
            .paths
            .iter()
            .filter(|p| marks.iter().any(|m| m.shape.order == p.order))
            .any(|p| {
                rectangles(p).is_some_and(|all| {
                    all.iter().any(|mark| {
                        !marks
                            .iter()
                            .any(|m| m.shape.order == p.order && m.bounds == mark.bounds)
                    })
                })
            })
        || page
            .spans
            .iter()
            .any(|s| owners.contains(&s.order) && !contains(bounds, s.selection, 0.6))
        || page
            .pictures
            .iter()
            .any(|p| p.bounds.intersection(plot).is_some())
        || page
            .paths
            .iter()
            .filter(|p| !owners.contains(&p.order) && p.fill.is_some())
            .any(|p| {
                rectangles(p)
                    .is_some_and(|rects| rects.iter().any(|r| plot.contains(r.bounds.center())))
            })
    {
        return None;
    }
    let fraction = (extent(clusters[0][count - 1], d)[1] - extent(clusters[0][0], d)[0]) / pitch;
    Some(Chart {
        bounds: padded(bounds, 3.0),
        plot,
        categories,
        series,
        horizontal,
        owners,
        scale: [axis.min, axis.max, axis.step],
        secondary: second_scale,
        percent: axis.percent,
        gap: (count as f64 * (fraction.max(0.1).recip() - 1.0) * 100.0)
            .round()
            .clamp(0.0, 500.0) as u32,
        font_size: axis.ticks[0].size,
        grid,
        between_categories: true,
        legend,
    })
}

fn point(
    page: &Page,
    axis: &Axis<'_>,
    [category, edge]: [f64; 2],
    horizontal: bool,
    pitch: f64,
    color: [u8; 4],
    (owners, bounds): (&mut HashSet<usize>, &mut Box2),
) -> Point {
    let estimated = axis.slope * edge + axis.intercept;
    if let Some((span, value, _)) =
        observed(page, axis, [category, edge], horizontal, pitch, owners)
    {
        owners.insert(span.order);
        *bounds = bounds.union(span.selection);
        Point {
            value,
            label: Some(span.text.clone()),
            color,
        }
    } else {
        let digits = 10_f64.powf(
            (-((axis.slope.abs() * 0.5).max(1e-8)).log10())
                .ceil()
                .clamp(0.0, 6.0),
        );
        Point {
            value: (estimated * digits).round() / digits,
            label: None,
            color,
        }
    }
}

fn observed<'a>(
    page: &'a Page,
    axis: &Axis<'_>,
    [category, edge]: [f64; 2],
    horizontal: bool,
    pitch: f64,
    owners: &HashSet<usize>,
) -> Option<(&'a Span, f64, f64)> {
    let estimated = axis.slope * edge + axis.intercept;
    let tolerance = (axis.slope.abs() * 2.0).max(axis.step * 0.06);
    page.spans
        .iter()
        .filter_map(|span| {
            let transverse = span.selection.center()[usize::from(horizontal)];
            let distance = (coordinate(span, horizontal) - edge).abs();
            let value = number(&span.text)?;
            (!owners.contains(&span.order)
                && (transverse - category).abs() < pitch * 0.28
                && distance <= span.size.mul_add(2.5, 0.0).max(20.0)
                && (value - estimated).abs() <= tolerance)
                .then_some((
                    span,
                    value,
                    (value - estimated).abs() + distance * tolerance * 0.01,
                ))
        })
        .min_by(|a, b| a.2.total_cmp(&b.2))
}

fn polyline(shape: &Drawing) -> Option<Vec<[f64; 2]>> {
    if shape.fill.is_some() || shape.stroke?[3] != 255 {
        return None;
    }
    let mut vertices = Vec::new();
    for command in &shape.commands {
        match *command {
            PathCommand::Move(p) if vertices.is_empty() => vertices.push(p),
            PathCommand::Line(p) if !vertices.is_empty() => vertices.push(p),
            _ => return None,
        }
    }
    if vertices.len() < 3 || vertices.len() > 100 {
        return None;
    }
    regular(&vertices.iter().map(|p| p[0]).collect::<Vec<_>>())?;
    Some(vertices)
}

fn line_style(
    page: &Page,
    line: &Drawing,
    vertices: &[[f64; 2]],
    owners: &mut HashSet<usize>,
) -> Option<LineStyle> {
    let mut marker = None;
    let mut indices = HashSet::new();
    for path in &page.paths {
        let Some(fill) = path.fill.filter(|color| color[3] == 255) else {
            continue;
        };
        if path.stroke != line.stroke
            || !vertices
                .iter()
                .any(|&p| padded(path.bounds, 0.65).contains(p))
        {
            continue;
        }
        let ends: Vec<_> = path
            .commands
            .iter()
            .filter_map(|command| match *command {
                PathCommand::Move(p) | PathCommand::Line(p) => Some(p),
                PathCommand::Cubic([_, _, p]) => Some(p),
                PathCommand::Close => None,
            })
            .collect();
        let cubics = path
            .commands
            .iter()
            .filter(|command| matches!(command, PathCommand::Cubic(_)))
            .count();
        if cubics == 0 {
            continue;
        }
        let bounds = Box2::from_points(ends);
        let width = bounds.width();
        let centers = vertices.iter().position(|p| {
            (p[0] - bounds.center()[0]).abs() < 0.65 && (p[1] - bounds.center()[1]).abs() < 0.65
        });
        let Some(index) = centers else {
            if cubics > 4 {
                return None;
            }
            continue;
        };
        if cubics != 4
            || !(2.0..=20.0).contains(&width)
            || (width - bounds.height()).abs() > 0.3
            || path
                .commands
                .iter()
                .filter(|command| matches!(command, PathCommand::Move(_)))
                .count()
                != 1
            || !indices.insert(index)
            || marker.is_some_and(|(size, color): (f64, [u8; 4])| {
                (width - size).abs() > 0.3 || color != fill
            })
        {
            return None;
        }
        marker = Some((width, fill));
        owners.insert(path.order);
    }
    if marker.is_some() && indices.len() != vertices.len() {
        return None;
    }
    Some(LineStyle {
        width: line.width,
        marker,
    })
}

fn contains(outer: Box2, inner: Box2, margin: f64) -> bool {
    let outer = padded(outer, margin);
    outer.contains([inner.x0, inner.y0]) && outer.contains([inner.x1, inner.y1])
}
fn padded(b: Box2, m: f64) -> Box2 {
    Box2 {
        x0: b.x0 - m,
        y0: b.y0 - m,
        x1: b.x1 + m,
        y1: b.y1 + m,
    }
}

fn line_chart(
    page: &Page,
    base: &Drawing,
    vertices: &[[f64; 2]],
    reserved: &HashSet<usize>,
) -> Option<Chart> {
    let positions: Vec<_> = vertices.iter().map(|p| p[0]).collect();
    let pitch = regular(&positions)?;
    let (left, right) = (positions[0], *positions.last()?);
    let min = vertices.iter().map(|p| p[1]).min_by(f64::total_cmp)?;
    let max = vertices.iter().map(|p| p[1]).max_by(f64::total_cmp)?;
    let axis = find_axis(page, false, left, min, max, false)?;
    let plot = Box2 {
        x0: left,
        y0: axis.at(axis.max),
        x1: right,
        y1: axis.at(axis.min),
    };
    let mut bounds = axis
        .ticks
        .iter()
        .fold(plot, |bounds, l| bounds.union(l.selection));
    let mut owners: HashSet<_> = axis.ticks.iter().map(|l| l.order).collect();
    let mut categories = Vec::new();
    for x in positions {
        let span = page
            .spans
            .iter()
            .filter(|l| {
                let distance = l.selection.y0 - plot.y1;
                (1.0..=70.0).contains(&distance)
                    && (l.selection.center()[0] - x).abs() < pitch * 0.35
            })
            .min_by(|a, b| a.selection.y0.total_cmp(&b.selection.y0))?;
        owners.insert(span.order);
        bounds = bounds.union(span.selection);
        categories.push(span.text.clone());
    }
    let mut series = Vec::new();
    for shape in &page.paths {
        let Some(line) = polyline(shape) else {
            continue;
        };
        if line.len() != vertices.len()
            || line
                .iter()
                .zip(vertices)
                .any(|(a, b)| (a[0] - b[0]).abs() > 0.5 || !padded(plot, 0.5).contains(*a))
        {
            continue;
        }
        if series.len() >= 8 {
            return None;
        }
        owners.insert(shape.order);
        let style = line_style(page, shape, &line, &mut owners)?;
        let points = line
            .into_iter()
            .map(|p| {
                point(
                    page,
                    &axis,
                    p,
                    false,
                    pitch,
                    shape.stroke.unwrap(),
                    (&mut owners, &mut bounds),
                )
            })
            .collect();
        series.push(Series {
            name: None,
            points,
            line: Some(style),
            secondary: false,
        });
    }
    if series.is_empty()
        || !owners.contains(&base.order)
        || owners.iter().any(|n| reserved.contains(n))
        || page
            .spans
            .iter()
            .any(|s| owners.contains(&s.order) && !contains(bounds, s.selection, 0.6))
        || page
            .pictures
            .iter()
            .any(|p| p.bounds.intersection(plot).is_some())
    {
        return None;
    }
    Some(Chart {
        bounds: padded(bounds, 3.0),
        owners,
        plot,
        categories,
        series,
        horizontal: false,
        scale: [axis.min, axis.max, axis.step],
        secondary: None,
        percent: axis.percent,
        gap: 0,
        font_size: axis.ticks[0].size,
        grid: None,
        between_categories: false,
        legend: false,
    })
}

pub(super) fn detect(page: &Page, check: super::Check<'_>) -> io::Result<Vec<Chart>> {
    let marks: Vec<_> = page
        .paths
        .iter()
        .filter_map(rectangles)
        .flatten()
        .take(1001)
        .collect();
    if marks.len() > 1000 || page.spans.len() > 1500 || page.paths.len() > 3000 {
        return Ok(Vec::new());
    }
    let mut charts = Vec::new();
    let mut reserved = HashSet::new();
    for horizontal in [false, true] {
        let mut edges: Vec<_> = marks
            .iter()
            .flat_map(|&mark| extent(mark, usize::from(!horizontal)).map(|edge| (edge, mark)))
            .collect();
        edges.sort_by(|a, b| a.0.total_cmp(&b.0));
        let groups: Vec<_> = edges
            .chunk_by(|a, b| (a.0 - b.0).abs() < 0.5)
            .filter(|row| row.len() >= 3)
            .collect();
        for group in groups {
            check()?;
            let baseline = group[group.len() / 2].0;
            let selected: Vec<_> = group.iter().map(|(_, mark)| *mark).collect();
            if let Some(chart) = chart(page, &selected, horizontal, baseline, &reserved) {
                reserved.extend(&chart.owners);
                charts.push(chart);
            }
        }
    }
    for shape in &page.paths {
        if reserved.contains(&shape.order) {
            continue;
        }
        let Some(vertices) = polyline(shape) else {
            continue;
        };
        check()?;
        if let Some(chart) = line_chart(page, shape, &vertices, &reserved) {
            reserved.extend(&chart.owners);
            charts.push(chart);
        }
    }
    Ok(charts)
}

fn hex([r, g, b, _]: [u8; 4]) -> String {
    format!("{r:02X}{g:02X}{b:02X}")
}

impl Chart {
    pub(super) fn part(&self, page: usize, scale: f64) -> io::Result<ChartPart> {
        let mut workbook = Workbook::new();
        let data = workbook
            .add_worksheet()
            .set_name("Chart Data")
            .map_err(io::Error::other)?;
        data.write_string(0, 0, "Category")
            .map_err(io::Error::other)?;
        let number_format = if self.percent { "0%" } else { "General" };
        let format = rust_xlsxwriter::Format::new().set_num_format(number_format);
        let mut tx = String::new();
        let n = self.categories.len();
        for (i, category) in self.categories.iter().enumerate() {
            data.write_string(i as u32 + 1, 0, category)
                .map_err(io::Error::other)?;
            write!(
                tx,
                r#"<c:pt idx="{i}"><c:v>{}</c:v></c:pt>"#,
                escape(category)
            )
            .unwrap();
        }
        let mut plots = [String::new(), String::new(), String::new()];
        for (s, series) in self.series.iter().enumerate() {
            let name = series
                .name
                .clone()
                .unwrap_or_else(|| format!("Series {}", s + 1));
            let column = char::from(b'B' + s as u8);
            data.write_string(0, s as u16 + 1, &name)
                .map_err(io::Error::other)?;
            let name = escape(&name);
            let xml =
                &mut plots[usize::from(series.line.is_some()) + usize::from(series.secondary)];
            write!(xml,r#"<c:ser><c:idx val="{s}"/><c:order val="{s}"/><c:tx><c:strRef><c:f>'Chart Data'!${column}$1</c:f><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{name}</c:v></c:pt></c:strCache></c:strRef></c:tx>"#).unwrap();
            let rgb = hex(series.points[0].color);
            if let Some(style) = &series.line {
                let width = (style.width * 12700.0).round() as i64;
                write!(xml,r#"<c:spPr><a:ln w="{width}"><a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill></a:ln></c:spPr>"#).unwrap();
                if let Some((size, fill)) = style.marker {
                    let size = size.round().clamp(2.0, 72.0) as u32;
                    let fill = hex(fill);
                    write!(xml,r#"<c:marker><c:symbol val="circle"/><c:size val="{size}"/><c:spPr><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill></a:ln></c:spPr></c:marker>"#).unwrap();
                } else {
                    xml.push_str(r#"<c:marker><c:symbol val="none"/></c:marker>"#);
                }
            } else {
                write!(xml,r#"<c:spPr><a:solidFill><a:srgbClr val="{rgb}"/></a:solidFill></c:spPr><c:invertIfNegative val="0"/>"#).unwrap();
                for (i, p) in series.points.iter().enumerate() {
                    if p.color != series.points[0].color {
                        write!(xml,r#"<c:dPt><c:idx val="{i}"/><c:spPr><a:solidFill><a:srgbClr val="{}"/></a:solidFill></c:spPr></c:dPt>"#,hex(p.color)).unwrap();
                    }
                }
            }
            if series.points.iter().any(|p| p.label.is_some()) {
                xml.push_str("<c:dLbls>");
                for (i, _) in series
                    .points
                    .iter()
                    .enumerate()
                    .filter(|(_, p)| p.label.is_none())
                {
                    write!(
                        xml,
                        r#"<c:dLbl><c:idx val="{i}"/><c:delete val="1"/></c:dLbl>"#
                    )
                    .unwrap();
                }
                write!(xml,r#"<c:numFmt formatCode="{number_format}" sourceLinked="0"/><c:dLblPos val="{}"/><c:showLegendKey val="0"/><c:showVal val="1"/><c:showCatName val="0"/><c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/></c:dLbls>"#,if series.line.is_some() {"t"} else {"outEnd"}).unwrap();
            }
            let end = n + 1;
            write!(xml,r#"<c:cat><c:strRef><c:f>'Chart Data'!$A$2:$A${end}</c:f><c:strCache><c:ptCount val="{n}"/>{tx}</c:strCache></c:strRef></c:cat><c:val><c:numRef><c:f>'Chart Data'!${column}$2:${column}${end}</c:f><c:numCache><c:formatCode>{number_format}</c:formatCode><c:ptCount val="{n}"/>"#).unwrap();
            for (i, p) in series.points.iter().enumerate() {
                data.write_number_with_format(i as u32 + 1, s as u16 + 1, p.value, &format)
                    .map_err(io::Error::other)?;
                write!(xml, r#"<c:pt idx="{i}"><c:v>{}</c:v></c:pt>"#, p.value).unwrap();
            }
            xml.push_str("</c:numCache></c:numRef></c:val></c:ser>");
        }
        let provenance = workbook
            .add_worksheet()
            .set_name("PDF Recovery")
            .map_err(io::Error::other)?;
        for (col, label) in [
            "PDF page",
            "Series",
            "Category",
            "Value",
            "Source",
            "Visible label",
        ]
        .into_iter()
        .enumerate()
        {
            provenance
                .write_string(0, col as u16, label)
                .map_err(io::Error::other)?;
        }
        let mut row = 1;
        for (i, s) in self.series.iter().enumerate() {
            for (cat, p) in self.categories.iter().zip(&s.points) {
                provenance
                    .write_number(row, 0, page as f64)
                    .and_then(|ws| {
                        ws.write_string(
                            row,
                            1,
                            s.name
                                .clone()
                                .unwrap_or_else(|| format!("Series {}", i + 1)),
                        )
                    })
                    .and_then(|ws| ws.write_string(row, 2, cat))
                    .and_then(|ws| ws.write_number(row, 3, p.value))
                    .and_then(|ws| {
                        ws.write_string(
                            row,
                            4,
                            if p.label.is_some() {
                                "Read from visible PDF label"
                            } else {
                                "Estimated from vector geometry and axis ticks"
                            },
                        )
                    })
                    .and_then(|ws| ws.write_string(row, 5, p.label.as_deref().unwrap_or_default()))
                    .map_err(io::Error::other)?;
                row += 1;
            }
        }
        let [bars, lines, secondary_lines] = plots;
        let bar_dir = if self.horizontal { "bar" } else { "col" };
        let bar = if bars.is_empty() {
            String::new()
        } else {
            format!(
                r#"<c:barChart><c:barDir val="{bar_dir}"/><c:grouping val="clustered"/><c:varyColors val="0"/>{bars}<c:gapWidth val="{}"/><c:overlap val="0"/><c:axId val="101"/><c:axId val="102"/></c:barChart>"#,
                self.gap
            )
        };
        let line_plot = |lines: &str, cat, value| {
            if lines.is_empty() {
                String::new()
            } else {
                format!(
                    r#"<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>{lines}<c:axId val="{cat}"/><c:axId val="{value}"/></c:lineChart>"#
                )
            }
        };
        let line = line_plot(&lines, 101, 102);
        let secondary_line = line_plot(&secondary_lines, 103, 104);
        let [min, max, step] = self.scale;
        let grid=self.grid.map_or_else(String::new,|color|format!(r#"<c:majorGridlines><c:spPr><a:ln w="5080"><a:solidFill><a:srgbClr val="{}"/></a:solidFill></a:ln></c:spPr></c:majorGridlines>"#,hex(color)));
        let [x, y, w, h] = [
            (self.plot.x0 - self.bounds.x0) / self.bounds.width(),
            (self.plot.y0 - self.bounds.y0) / self.bounds.height(),
            self.plot.width() / self.bounds.width(),
            self.plot.height() / self.bounds.height(),
        ];
        let (category, axis, orientation) = if self.horizontal {
            ("l", "b", "maxMin")
        } else {
            ("b", "l", "minMax")
        };
        let size = (self.font_size * scale * 100.0)
            .round()
            .clamp(100.0, 400000.0) as u32;
        let font = format!(
            r#"<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="{size}"><a:latin typeface="Arial"/></a:defRPr></a:pPr><a:endParaRPr/></a:p></c:txPr>"#
        );
        let between = if self.between_categories {
            "between"
        } else {
            "midCat"
        };
        let extra_axes=self.secondary.map_or_else(String::new,|[min,max,step]|format!(
            r#"<c:catAx><c:axId val="103"/><c:scaling><c:orientation val="minMax"/></c:scaling><c:delete val="1"/><c:axPos val="b"/><c:crossAx val="104"/><c:crosses val="autoZero"/><c:auto val="1"/></c:catAx><c:valAx><c:axId val="104"/><c:scaling><c:orientation val="minMax"/><c:max val="{max}"/><c:min val="{min}"/></c:scaling><c:delete val="0"/><c:axPos val="r"/><c:numFmt formatCode="General" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>{font}<c:crossAx val="103"/><c:crosses val="max"/><c:crossBetween val="{between}"/><c:majorUnit val="{step}"/></c:valAx>"#));
        let legend = if self.legend {
            r#"<c:legend><c:legendPos val="t"/><c:overlay val="1"/></c:legend>"#
        } else {
            ""
        };
        let xml = format!(
            r#"<c:chartSpace xmlns:c="{CHART_NS}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><c:chart><c:autoTitleDeleted val="1"/><c:plotArea><c:layout><c:manualLayout><c:layoutTarget val="inner"/><c:xMode val="edge"/><c:yMode val="edge"/><c:wMode val="factor"/><c:hMode val="factor"/><c:x val="{x}"/><c:y val="{y}"/><c:w val="{w}"/><c:h val="{h}"/></c:manualLayout></c:layout>{bar}{line}{secondary_line}<c:catAx><c:axId val="101"/><c:scaling><c:orientation val="{orientation}"/></c:scaling><c:delete val="0"/><c:axPos val="{category}"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="low"/>{font}<c:crossAx val="102"/><c:crosses val="autoZero"/><c:auto val="1"/><c:lblAlgn val="ctr"/><c:lblOffset val="35"/></c:catAx><c:valAx><c:axId val="102"/><c:scaling><c:orientation val="minMax"/><c:max val="{max}"/><c:min val="{min}"/></c:scaling><c:delete val="0"/><c:axPos val="{axis}"/>{grid}<c:numFmt formatCode="{number_format}" sourceLinked="0"/><c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>{font}<c:crossAx val="101"/><c:crosses val="autoZero"/><c:crossBetween val="{between}"/><c:majorUnit val="{step}"/></c:valAx>{extra_axes}</c:plotArea>{legend}<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart><c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>{font}<c:externalData r:id="rId1"><c:autoUpdate val="0"/></c:externalData></c:chartSpace>"#
        );
        Ok(ChartPart {
            xml,
            workbook: workbook.save_to_buffer().map_err(io::Error::other)?,
        })
    }
    pub(super) fn estimated(&self) -> usize {
        self.series
            .iter()
            .flat_map(|s| &s.points)
            .filter(|p| p.label.is_none())
            .count()
    }
}
