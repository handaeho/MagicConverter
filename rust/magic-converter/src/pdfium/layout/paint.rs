use super::{Box2, Drawing, Gradient, PathCommand, pdf_dictionary};
use lopdf::content::{Content, Operation};
use lopdf::{Dictionary, Document, Object, ObjectId, Stream};

const MAX_RECTS: usize = 4096;
const MAX_COORD: f64 = 100_000.0;

fn number(object: &Object) -> Option<f64> {
    let value = match object {
        Object::Integer(value) => *value as f64,
        Object::Real(value) => f64::from(*value),
        _ => return None,
    };
    (value.abs() <= MAX_COORD).then_some(value)
}

fn numbers<const N: usize>(objects: &[Object]) -> Option<[f64; N]> {
    let mut result = [0.0; N];
    if objects.len() != N {
        return None;
    }
    for (target, object) in result.iter_mut().zip(objects) {
        *target = number(object)?;
    }
    Some(result)
}

fn rectangle(values: &[Object], dimensions: bool) -> Option<Box2> {
    let [x, y, mut r, mut t] = numbers(values)?;
    if dimensions {
        r += x;
        t += y;
    }
    let bounds = Box2::from_points([[x, y], [r, t]]);
    (bounds.width() > 0.0 && bounds.height() > 0.0).then_some(bounds)
}

fn color<const N: usize>(objects: &[Object]) -> Option<[u8; N]> {
    let values = numbers::<N>(objects)?;
    values
        .iter()
        .all(|value| (0.0..=1.0).contains(value))
        .then(|| values.map(|value| (value * 255.0).round() as u8))
}

fn lookup<'a>(
    document: &'a Document,
    resources: &[&'a Dictionary],
    category: &[u8],
    name: &[u8],
) -> Option<&'a Object> {
    resources.iter().find_map(|resource| {
        pdf_dictionary(document, resource.get(category).ok()?)?
            .get(name)
            .ok()
    })
}

fn stream<'a>(document: &'a Document, object: &'a Object) -> Option<&'a Stream> {
    document
        .get_object(object.as_reference().ok()?)
        .ok()?
        .as_stream()
        .ok()
}

fn operations(stream: &Stream) -> Option<Vec<Operation>> {
    let content = stream.get_plain_content().ok()?;
    if content.len() > 65_536 {
        return None;
    }
    let mut operations = Content::decode(&content).ok()?.operations;
    if operations.first()?.operator == "q" && operations.last()?.operator == "Q" {
        operations.pop();
        operations.remove(0);
    }
    Some(operations)
}

fn stream_transform(stream: &Stream) -> Option<[f64; 4]> {
    let Ok(matrix) = stream.dict.get(b"Matrix") else {
        return Some([1.0, 1.0, 0.0, 0.0]);
    };
    let [a, b, c, d, x, y] = numbers(matrix.as_array().ok()?)?;
    (a > 0.0 && d > 0.0 && b == 0.0 && c == 0.0).then_some([a, d, x, y])
}

fn stream_bbox(stream: &Stream) -> Option<Box2> {
    rectangle(stream.dict.get(b"BBox").ok()?.as_array().ok()?, false)
}

fn tiled_rectangles(outer: Box2, tile: Box2, dx: f64, dy: f64) -> Option<Vec<Box2>> {
    let columns =
        ((outer.x0 - tile.x1) / dx).ceil() as i64..=((outer.x1 - tile.x0) / dx).floor() as i64;
    let rows =
        ((outer.y0 - tile.y1) / dy).ceil() as i64..=((outer.y1 - tile.y0) / dy).floor() as i64;
    if (columns.end() - columns.start() + 1)
        .max(0)
        .saturating_mul((rows.end() - rows.start() + 1).max(0))
        > MAX_RECTS as i64
    {
        return None;
    }
    let mut tiles = Vec::new();
    for row in rows {
        for column in columns.clone() {
            let (x, y) = (dx * column as f64, dy * row as f64);
            if let Some(visible) = outer.intersection(Box2 {
                x0: tile.x0 + x,
                x1: tile.x1 + x,
                y0: tile.y0 + y,
                y1: tile.y1 + y,
            }) {
                tiles.push(visible);
            }
        }
    }
    Some(tiles)
}

fn tiling(document: &Document, pattern: &Object, outer: Box2) -> Option<Vec<([u8; 3], Vec<Box2>)>> {
    let pattern = stream(document, pattern)?;
    let dict = &pattern.dict;
    for key in [b"PatternType".as_slice(), b"PaintType", b"TilingType"] {
        if dict.get(key).ok()?.as_i64().ok()? != 1 {
            return None;
        }
    }
    let cell = stream_bbox(pattern)?;
    let [sx, sy, x, y] = stream_transform(pattern)?;
    let dx = number(dict.get(b"XStep").ok()?)?;
    let dy = number(dict.get(b"YStep").ok()?)?;
    if dx < cell.width() || dy < cell.height() {
        return None;
    }
    let mut shapes = Vec::new();
    let mut rectangles = Vec::new();
    let mut rgb = [0; 3];
    let mut budget = 0;
    for op in operations(pattern)? {
        let args = &op.operands;
        match op.operator.as_str() {
            "rg" => rgb = color(args)?,
            "g" => rgb = [color::<1>(args)?[0]; 3],
            "re" => {
                let rect = rectangle(args, true)?;
                if rectangles.len() >= 64 {
                    return None;
                }
                if let Some(rect) = rect.intersection(cell) {
                    rectangles.push(rect);
                }
            }
            "f" | "f*" | "F" if args.is_empty() => {
                // The PowerPoint compound path uses nonzero winding. Fall
                // back when overlapping rectangles in a PDF fill could cancel.
                for (i, rect) in rectangles.iter().enumerate() {
                    if rectangles[..i]
                        .iter()
                        .any(|prior| prior.intersection(*rect).is_some())
                    {
                        return None;
                    }
                }
                let mut tiles = Vec::new();
                for rect in rectangles.drain(..) {
                    let tile = Box2 {
                        x0: sx * rect.x0 + x,
                        x1: sx * rect.x1 + x,
                        y0: sy * rect.y0 + y,
                        y1: sy * rect.y1 + y,
                    };
                    let visible = tiled_rectangles(outer, tile, dx * sx, dy * sy)?;
                    budget += visible.len();
                    if budget > MAX_RECTS {
                        return None;
                    }
                    tiles.extend(visible);
                }
                if !tiles.is_empty() {
                    shapes.push((rgb, tiles));
                }
            }
            _ => return None,
        }
    }
    rectangles.is_empty().then_some(shapes)
}

#[derive(Clone, Copy)]
enum Mask {
    Solid(Option<Box2>, f64),
    Axial(Box2, Gradient),
}

fn axial(document: &Document, group: &Stream, name: &[u8], bbox: Box2) -> Option<Mask> {
    let resources = pdf_dictionary(document, group.dict.get(b"Resources").ok()?)?;
    let shading = pdf_dictionary(document, lookup(document, &[resources], b"Shading", name)?)?;
    let function = pdf_dictionary(document, shading.get(b"Function").ok()?)?;
    if shading.get(b"ShadingType").ok()?.as_i64().ok()? != 2
        || shading.get(b"ColorSpace").ok()?.as_name().ok()? != b"DeviceGray"
        || function.get(b"FunctionType").ok()?.as_i64().ok()? != 2
        || number(function.get(b"N").ok()?)? != 1.0
        || shading.has(b"Background")
        || shading.has(b"BBox")
        || function.has(b"Range")
    {
        return None;
    }
    for dict in [shading, function] {
        if let Ok(domain) = dict.get(b"Domain")
            && numbers::<2>(domain.as_array().ok()?)? != [0.0, 1.0]
        {
            return None;
        }
    }
    let [x, y, u, v] = numbers(shading.get(b"Coords").ok()?.as_array().ok()?)?;
    let extend = shading
        .get(b"Extend")
        .ok()
        .map(Object::as_array)
        .transpose()
        .ok()?;
    let extension = match extend {
        Some(values) if values.len() == 2 => [values[0].as_bool().ok()?, values[1].as_bool().ok()?],
        None => [false, false],
        _ => return None,
    };
    let stop = |key, default| {
        let value = match function.get(key) {
            Ok(values) => numbers::<1>(values.as_array().ok()?)?[0],
            Err(_) => default,
        };
        (0.0..=1.0).contains(&value).then_some(value)
    };
    let alpha = [stop(b"C0", 0.0)?, stop(b"C1", 1.0)?];
    // Restrict to an axis aligned ramp, so cropping its rectangle only
    // remaps the two opacity stops. Extended regions use a fallback.
    let (axis, lower, upper) = if y == v && x != u {
        (0, bbox.x0, bbox.x1)
    } else if x == u && y != v {
        (1, bbox.y0, bbox.y1)
    } else {
        return None;
    };
    let start = [x, y][axis];
    let end = [u, v][axis];
    let (lo, hi) = (start.min(end), start.max(end));
    if (extension[usize::from(start > end)] && lower < lo)
        || (extension[usize::from(start < end)] && upper > hi)
    {
        return None;
    }
    let bounds = if axis == 0 {
        Box2 {
            x0: lower.max(lo),
            x1: upper.min(hi),
            ..bbox
        }
    } else {
        Box2 {
            y0: lower.max(lo),
            y1: upper.min(hi),
            ..bbox
        }
    };
    (bounds.width() > 0.0 && bounds.height() > 0.0).then_some(Mask::Axial(
        bounds,
        Gradient {
            axis: [[x, y], [u, v]],
            alpha,
        },
    ))
}

fn mask(document: &Document, object: &Object) -> Option<Mask> {
    let mask = pdf_dictionary(document, object)?;
    if mask.has(b"BC")
        || mask
            .get(b"TR")
            .is_ok_and(|value| !matches!(value.as_name(), Ok(b"Identity")))
    {
        return None;
    }
    let mode = mask.get(b"S").ok()?.as_name().ok()?;
    let group = stream(document, mask.get(b"G").ok()?)?;
    let transparency = pdf_dictionary(document, group.dict.get(b"Group").ok()?)?;
    if group.dict.get(b"Subtype").ok()?.as_name().ok()? != b"Form"
        || transparency.get(b"S").ok()?.as_name().ok()? != b"Transparency"
    {
        return None;
    }
    if stream_transform(group)? != [1.0, 1.0, 0.0, 0.0] {
        return None;
    }
    let bbox = stream_bbox(group)?;
    let ops = operations(group)?;
    if let [op] = ops.as_slice()
        && op.operator == "sh"
        && op.operands.len() == 1
        && mode == b"Luminosity"
        && transparency.get(b"CS").ok()?.as_name().ok()? == b"DeviceGray"
    {
        return axial(document, group, op.operands[0].as_name().ok()?, bbox);
    }
    let [gray, geometry, fill] = ops.as_slice() else {
        return None;
    };
    if gray.operator != "g"
        || geometry.operator != "re"
        || !matches!(fill.operator.as_str(), "f" | "f*" | "F")
    {
        return None;
    }
    let [gray] = numbers::<1>(&gray.operands)?;
    let opacity = match mode {
        b"Alpha" => 1.0,
        b"Luminosity" if transparency.get(b"CS").ok()?.as_name().ok()? == b"DeviceGray" => gray,
        _ => return None,
    };
    if !(0.0..=1.0).contains(&opacity) {
        return None;
    }
    Some(Mask::Solid(
        rectangle(&geometry.operands, true)?.intersection(bbox),
        opacity,
    ))
}

#[derive(Clone)]
struct State {
    rgb: [u8; 3],
    pattern: Option<Vec<u8>>,
    pattern_space: bool,
    clip: Option<Box2>,
    opacity: f64,
    mask: Option<Mask>,
}

pub(super) fn rectangular_page(
    document: &Document,
    page: ObjectId,
    resources: &[&Dictionary],
) -> Option<Vec<Drawing>> {
    if document.get_dictionary(page).ok()?.has(b"Group") {
        return None;
    }
    let bytes = document.get_page_content(page).ok()?;
    if bytes.len() > 262_144 {
        return None;
    }
    let mut state = State {
        rgb: [0; 3],
        pattern: None,
        pattern_space: false,
        clip: None,
        opacity: 1.0,
        mask: None,
    };
    let mut stack = Vec::new();
    let mut pending = None;
    let mut clipping = false;
    let mut result = Vec::new();
    let mut rects = 0;
    let mut recovered = false;
    for op in Content::decode(&bytes).ok()?.operations {
        let args = op.operands.as_slice();
        match op.operator.as_str() {
            "q" if args.is_empty() && pending.is_none() && stack.len() < 64 => {
                stack.push(state.clone())
            }
            "Q" if args.is_empty() && pending.is_none() => state = stack.pop()?,
            "rg" | "g" => {
                state.rgb = if op.operator == "rg" {
                    color(args)?
                } else {
                    [color::<1>(args)?[0]; 3]
                };
                state.pattern = None;
                state.pattern_space = false;
            }
            "cs" if args.len() == 1 && args[0].as_name().ok()? == b"Pattern" => {
                state.pattern_space = true;
                state.pattern = None;
            }
            "scn" if state.pattern_space && args.len() == 1 => {
                state.pattern = Some(args[0].as_name().ok()?.to_vec())
            }
            "gs" if args.len() == 1 => {
                let dict = pdf_dictionary(
                    document,
                    lookup(document, resources, b"ExtGState", args[0].as_name().ok()?)?,
                )?;
                for (key, value) in dict.iter() {
                    match key.as_slice() {
                        b"Type" | b"CA" => (),
                        b"BM" if matches!(value.as_name(), Ok(b"Normal" | b"Compatible")) => (),
                        b"ca" => {
                            state.opacity =
                                number(value).filter(|value| (0.0..=1.0).contains(value))?
                        }
                        b"SMask" => {
                            state.mask = if matches!(value.as_name(), Ok(b"None")) {
                                None
                            } else {
                                Some(mask(document, value)?)
                            };
                        }
                        _ => return None,
                    }
                }
            }
            "re" if pending.is_none() => pending = Some(rectangle(args, true)?),
            "W" | "W*" if pending.is_some() && !clipping && args.is_empty() => clipping = true,
            "n" if clipping && args.is_empty() => {
                let rect = pending.take()?;
                state.clip = Some(match state.clip {
                    Some(prior) => prior.intersection(rect)?,
                    None => rect,
                });
                clipping = false;
            }
            "f" | "F" | "f*" if !clipping && args.is_empty() => {
                let mut rect = pending.take()?;
                if let Some(clip) = state.clip {
                    let Some(visible) = rect.intersection(clip) else {
                        continue;
                    };
                    rect = visible;
                }
                let mut opacity = state.opacity;
                let mut gradient = None;
                if let Some(mask) = state.mask {
                    recovered = true;
                    let (bounds, alpha) = match mask {
                        Mask::Solid(bounds, alpha) => (bounds, alpha),
                        Mask::Axial(bounds, mut stops) => {
                            if state.pattern_space {
                                return None;
                            }
                            stops.alpha = stops.alpha.map(|a| a * opacity);
                            gradient = Some(stops);
                            (Some(bounds), 1.0)
                        }
                    };
                    let Some(visible) = bounds.and_then(|mask| rect.intersection(mask)) else {
                        continue;
                    };
                    opacity *= alpha;
                    rect = visible;
                }
                if opacity == 0.0 {
                    continue;
                }
                let layers = if state.pattern_space {
                    recovered = true;
                    let pattern =
                        lookup(document, resources, b"Pattern", state.pattern.as_deref()?)?;
                    let layers = tiling(document, pattern, rect)?;
                    if opacity != 1.0 && layers.len() > 1 {
                        return None;
                    }
                    layers
                } else {
                    vec![(state.rgb, vec![rect])]
                };
                for (rgb, tiles) in layers {
                    rects += tiles.len();
                    if rects > MAX_RECTS {
                        return None;
                    }
                    let commands = tiles.iter().flat_map(|rect| contour(*rect)).collect();
                    if let Some(bounds) = tiles.into_iter().reduce(Box2::union) {
                        result.push(Drawing {
                            order: result.len(),
                            bounds,
                            commands,
                            stroke: None,
                            fill: Some([rgb[0], rgb[1], rgb[2], (255.0 * opacity).round() as u8]),
                            gradient,
                            width: 0.0,
                        });
                    }
                }
            }
            _ => return None,
        }
    }
    (recovered && stack.is_empty() && pending.is_none()).then_some(result)
}

fn contour(rect: Box2) -> [PathCommand; 5] {
    [
        PathCommand::Move([rect.x0, rect.y0]),
        PathCommand::Line([rect.x1, rect.y0]),
        PathCommand::Line([rect.x1, rect.y1]),
        PathCommand::Line([rect.x0, rect.y1]),
        PathCommand::Close,
    ]
}

pub(super) fn map_path(
    path: &Drawing,
    map: &impl Fn([f64; 2]) -> [f64; 2],
    canvas: Box2,
) -> Option<Drawing> {
    let mut commands = Vec::with_capacity(path.commands.len());
    let mut bounds = None::<Box2>;
    for commands_in_rect in path.commands.chunks_exact(5) {
        let [
            PathCommand::Move(first),
            _,
            PathCommand::Line(opposite),
            _,
            PathCommand::Close,
        ] = commands_in_rect
        else {
            return None;
        };
        if let Some(rect) = Box2::from_points([map(*first), map(*opposite)]).intersection(canvas) {
            bounds = Some(bounds.map_or(rect, |prior| prior.union(rect)));
            commands.extend(contour(rect));
        }
    }
    let bounds = bounds?;
    let gradient = path.gradient.map(|gradient| {
        let [start, end] = gradient.axis.map(map);
        let axis = usize::from((end[1] - start[1]).abs() > (end[0] - start[0]).abs());
        let midpoint = bounds.center();
        let edges = if axis == 0 {
            [bounds.x0, bounds.x1]
        } else {
            [bounds.y0, bounds.y1]
        };
        let direction = end[axis] - start[axis];
        let alpha = edges.map(|edge| {
            let t = ((edge - start[axis]) / direction).clamp(0.0, 1.0);
            gradient.alpha[0] + t * (gradient.alpha[1] - gradient.alpha[0])
        });
        Gradient {
            axis: edges.map(|edge| {
                let mut point = midpoint;
                point[axis] = edge;
                point
            }),
            alpha,
        }
    });
    Some(Drawing {
        bounds,
        gradient,
        commands,
        ..*path
    })
}
