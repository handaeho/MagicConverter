use super::*;
use std::collections::HashMap;
use std::ffi::{c_double, c_float, c_uint};
use std::io::Cursor;

mod paint;

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) struct Box2 {
    pub x0: f64,
    pub y0: f64,
    pub x1: f64,
    pub y1: f64,
}

impl Box2 {
    pub fn from_points(points: impl IntoIterator<Item = [f64; 2]>) -> Self {
        let mut result = Self {
            x0: f64::INFINITY,
            y0: f64::INFINITY,
            x1: f64::NEG_INFINITY,
            y1: f64::NEG_INFINITY,
        };
        for [x, y] in points {
            result.x0 = result.x0.min(x);
            result.y0 = result.y0.min(y);
            result.x1 = result.x1.max(x);
            result.y1 = result.y1.max(y);
        }
        result
    }

    pub fn width(self) -> f64 {
        self.x1 - self.x0
    }
    pub fn height(self) -> f64 {
        self.y1 - self.y0
    }
    pub fn center(self) -> [f64; 2] {
        [(self.x0 + self.x1) * 0.5, (self.y0 + self.y1) * 0.5]
    }
    pub fn contains(self, [x, y]: [f64; 2]) -> bool {
        x >= self.x0 && x <= self.x1 && y >= self.y0 && y <= self.y1
    }
    pub fn union(self, other: Self) -> Self {
        Self::from_points([
            [self.x0, self.y0],
            [self.x1, self.y1],
            [other.x0, other.y0],
            [other.x1, other.y1],
        ])
    }
    pub fn intersection(self, other: Self) -> Option<Self> {
        let bounds = Self {
            x0: self.x0.max(other.x0),
            y0: self.y0.max(other.y0),
            x1: self.x1.min(other.x1),
            y1: self.y1.min(other.y1),
        };
        (bounds.width() > 0.0 && bounds.height() > 0.0).then_some(bounds)
    }
}

pub(crate) struct Span {
    pub order: usize,
    pub text: String,
    pub bounds: Box2,
    pub selection: Box2,
    pub rotation: f64,
    pub standard_font: Option<[f64; 2]>,
    pub origin: Option<[f64; 2]>,
    pub size: f64,
    pub font: String,
    pub bold: bool,
    pub italic: bool,
    pub rgba: [u8; 4],
}

#[derive(Clone, Copy)]
pub(crate) enum PathCommand {
    Move([f64; 2]),
    Line([f64; 2]),
    Cubic([[f64; 2]; 3]),
    Close,
}

pub(crate) struct Drawing {
    pub order: usize,
    pub bounds: Box2,
    pub commands: Vec<PathCommand>,
    pub stroke: Option<[u8; 4]>,
    pub fill: Option<[u8; 4]>,
    pub gradient: Option<Gradient>,
    pub width: f64,
}

#[derive(Clone, Copy)]
pub(crate) struct Gradient {
    pub axis: [[f64; 2]; 2],
    pub alpha: [f64; 2],
}

pub(crate) struct Picture {
    pub order: usize,
    pub bounds: Box2,
    pub png: Vec<u8>,
    pub flattened: bool,
}

pub(crate) struct Page {
    pub bounds: Box2,
    pub spans: Vec<Span>,
    pub paths: Vec<Drawing>,
    pub pictures: Vec<Picture>,
    pub scanned: bool,
}

type ObjectNumber = unsafe extern "system" fn(Handle) -> c_int;
type ObjectHandle = unsafe extern "system" fn(Handle, c_int) -> Handle;
type ColorFn =
    unsafe extern "system" fn(Handle, *mut c_uint, *mut c_uint, *mut c_uint, *mut c_uint) -> c_int;
type Matrix = [c_float; 6];

macro_rules! layout_api {
    ($($name:ident = $symbol:literal : $ty:ty),+ $(,)?) => {
        struct Api { $($name: $ty),+ }
        impl Api {
            fn load(engine: &Engine) -> io::Result<Self> {
                Ok(Self { $($name: *unsafe { engine._library.get($symbol) }.map_err(io::Error::other)?),+ })
            }
        }
    };
}

layout_api! {
    objects = b"FPDFPage_CountObjects": ObjectNumber,
    object = b"FPDFPage_GetObject": ObjectHandle,
    kind = b"FPDFPageObj_GetType": ObjectNumber,
    transparent = b"FPDFPageObj_HasTransparency": ObjectNumber,
    set_active = b"FPDFPageObj_SetIsActive": unsafe extern "system" fn(Handle, c_int) -> c_int,
    bounds = b"FPDFPageObj_GetBounds": unsafe extern "system" fn(Handle, *mut c_float, *mut c_float, *mut c_float, *mut c_float) -> c_int,
    matrix = b"FPDFPageObj_GetMatrix": unsafe extern "system" fn(Handle, *mut Matrix) -> c_int,
    clip = b"FPDFPageObj_GetClipPath": unsafe extern "system" fn(Handle) -> Handle,
    clip_count = b"FPDFClipPath_CountPaths": ObjectNumber,
    clip_segments = b"FPDFClipPath_CountPathSegments": unsafe extern "system" fn(Handle, c_int) -> c_int,
    clip_segment = b"FPDFClipPath_GetPathSegment": unsafe extern "system" fn(Handle, c_int, c_int) -> Handle,
    image_size = b"FPDFImageObj_GetImagePixelSize": unsafe extern "system" fn(Handle, *mut u32, *mut u32) -> c_int,
    image_bitmap = b"FPDFImageObj_GetBitmap": unsafe extern "system" fn(Handle) -> Handle,
    bitmap_buffer = b"FPDFBitmap_GetBuffer": unsafe extern "system" fn(Handle) -> *const u8,
    bitmap_format = b"FPDFBitmap_GetFormat": ObjectNumber,
    bitmap_width = b"FPDFBitmap_GetWidth": ObjectNumber,
    bitmap_height = b"FPDFBitmap_GetHeight": ObjectNumber,
    bitmap_stride = b"FPDFBitmap_GetStride": ObjectNumber,
    annotation_count = b"FPDFPage_GetAnnotCount": ObjectNumber,
    annotation = b"FPDFPage_GetAnnot": ObjectHandle,
    close_annotation = b"FPDFPage_CloseAnnot": Close,
    annotation_bounds = b"FPDFAnnot_GetRect": unsafe extern "system" fn(Handle, *mut [f32; 4]) -> c_int,
    segments = b"FPDFPath_CountSegments": ObjectNumber,
    segment = b"FPDFPath_GetPathSegment": ObjectHandle,
    segment_kind = b"FPDFPathSegment_GetType": ObjectNumber,
    segment_point = b"FPDFPathSegment_GetPoint": unsafe extern "system" fn(Handle, *mut c_float, *mut c_float) -> c_int,
    segment_close = b"FPDFPathSegment_GetClose": ObjectNumber,
    mode = b"FPDFPath_GetDrawMode": unsafe extern "system" fn(Handle, *mut c_int, *mut c_int) -> c_int,
    stroke = b"FPDFPageObj_GetStrokeColor": ColorFn,
    fill = b"FPDFPageObj_GetFillColor": ColorFn,
    stroke_width = b"FPDFPageObj_GetStrokeWidth": unsafe extern "system" fn(Handle, *mut c_float) -> c_int,
    device_to_page = b"FPDF_DeviceToPage": unsafe extern "system" fn(Handle, c_int, c_int, c_int, c_int, c_int, c_int, c_int, *mut c_double, *mut c_double) -> c_int,
    unicode = b"FPDFText_GetUnicode": unsafe extern "system" fn(Handle, c_int) -> c_uint,
    text_object = b"FPDFText_GetTextObject": ObjectHandle,
    render_mode = b"FPDFTextObj_GetTextRenderMode": ObjectNumber,
    loose_box = b"FPDFText_GetLooseCharBox": unsafe extern "system" fn(Handle, c_int, *mut [f32; 4]) -> c_int,
    char_matrix = b"FPDFText_GetMatrix": unsafe extern "system" fn(Handle, c_int, *mut Matrix) -> c_int,
    font_size = b"FPDFText_GetFontSize": unsafe extern "system" fn(Handle, c_int) -> c_double,
    font = b"FPDFText_GetFontInfo": unsafe extern "system" fn(Handle, c_int, Handle, c_ulong, *mut c_int) -> c_ulong,
    char_box = b"FPDFText_GetCharBox": unsafe extern "system" fn(Handle, c_int, *mut c_double, *mut c_double, *mut c_double, *mut c_double) -> c_int,
    char_origin = b"FPDFText_GetCharOrigin": unsafe extern "system" fn(Handle, c_int, *mut c_double, *mut c_double) -> c_int,
}

fn selection_box(
    api: &Api,
    text: Handle,
    index: i32,
    map: &impl Fn([f64; 2]) -> [f64; 2],
) -> Option<Box2> {
    let mut rect = [0_f32; 4];
    if unsafe { (api.loose_box)(text, index, &mut rect) } == 0 {
        return None;
    }
    let [left, top, right, bottom] = rect.map(f64::from);
    let bounds =
        Box2::from_points([[left, top], [right, top], [left, bottom], [right, bottom]].map(map));
    (bounds.width() > 0.0 && bounds.height() > 0.0).then_some(bounds)
}

fn map_coordinates(
    api: &Api,
    page: Handle,
    width: f64,
    height: f64,
) -> io::Result<impl Fn([f64; 2]) -> [f64; 2]> {
    let coordinate = |x, y| {
        let (mut left, mut top) = (0.0, 0.0);
        let result =
            unsafe { (api.device_to_page)(page, 0, 0, 1000, 1000, 0, x, y, &mut left, &mut top) };
        if result == 0 {
            Err(invalid("PDF 페이지 좌표를 해석할 수 없습니다."))
        } else {
            Ok([left, top])
        }
    };
    let [ox, oy] = coordinate(0, 0)?;
    let [xx, xy] = coordinate(1000, 0)?;
    let [yx, yy] = coordinate(0, 1000)?;
    let (a, b, c, d) = (xx - ox, xy - oy, yx - ox, yy - oy);
    let det = a * d - b * c;
    if !det.is_finite() || det.abs() <= f64::EPSILON {
        return Err(invalid("PDF 페이지 좌표 행렬이 올바르지 않습니다."));
    }
    Ok(move |[x, y]: [f64; 2]| {
        let (x, y) = (x - ox, y - oy);
        [
            (d * x - c * y) * width / det,
            (a * y - b * x) * height / det,
        ]
    })
}

fn rgba(api: ColorFn, object: Handle) -> Option<[u8; 4]> {
    let (mut r, mut g, mut b, mut a) = (0, 0, 0, 0);
    let result = unsafe { api(object, &mut r, &mut g, &mut b, &mut a) };
    (result != 0).then_some([r as u8, g as u8, b as u8, a as u8])
}

fn object_bounds(api: &Api, object: Handle, map: &impl Fn([f64; 2]) -> [f64; 2]) -> Option<Box2> {
    let (mut l, mut b, mut r, mut t) = (0.0, 0.0, 0.0, 0.0);
    if object.is_null() || unsafe { (api.bounds)(object, &mut l, &mut b, &mut r, &mut t) } == 0 {
        return None;
    }
    let bounds = Box2::from_points(
        [[l, b], [l, t], [r, b], [r, t]].map(|[x, y]| map([f64::from(x), f64::from(y)])),
    );
    [bounds.x0, bounds.y0, bounds.x1, bounds.y1]
        .iter()
        .all(|v| v.is_finite())
        .then_some(bounds)
}

fn blend_region(
    api: &Api,
    objects: &[Handle],
    spans: &[Span],
    canvas: Box2,
    map: &impl Fn([f64; 2]) -> [f64; 2],
) -> Option<(Box2, Vec<bool>)> {
    // The region must contain every object it covers. That lets the PDF
    // renderer composite the original backdrop without painting it twice.
    if objects.len() > 2000 || spans.iter().any(|span| span.order >= objects.len()) {
        return None;
    }
    let padded = |rect: Box2| Box2 {
        x0: rect.x0 - 1.0,
        y0: rect.y0 - 1.0,
        x1: rect.x1 + 1.0,
        y1: rect.y1 + 1.0,
    };
    let mut bounds: Vec<_> = objects
        .iter()
        .map(|&obj| object_bounds(api, obj, map))
        .collect::<Option<_>>()?;
    for span in spans {
        bounds[span.order] = bounds[span.order].union(span.bounds);
    }
    let mut blended: Vec<_> = objects
        .iter()
        .map(|&obj| unsafe { (api.transparent)(obj) } != 0)
        .collect();
    let mut region = bounds
        .iter()
        .zip(&blended)
        .filter(|(_, member)| **member)
        .map(|(&rect, _)| padded(rect))
        .reduce(Box2::union)?;
    loop {
        let before = region;
        for (rect, member) in bounds.iter().zip(&mut blended) {
            if !*member && padded(*rect).intersection(region).is_some() {
                *member = true;
                region = region.union(padded(*rect));
            }
        }
        if before == region {
            return Some((region.intersection(canvas)?, blended));
        }
    }
}

#[derive(Default)]
struct PagePaint {
    patterned: bool,
    non_normal_blend: bool,
    simple_alpha: bool,
    editable: Option<Vec<Drawing>>,
}

fn page_paint(source: &Path) -> Vec<PagePaint> {
    let size = std::fs::metadata(source).map_or(0, |file| file.len());
    if size == 0 || size > 128 * 1024 * 1024 {
        return Vec::new();
    }
    let Ok(document) = lopdf::Document::load(source) else {
        return Vec::new();
    };
    document
        .page_iter()
        .take(1000)
        .map(|id| {
            let Ok((direct, inherited)) = document.get_page_resources(id) else {
                return PagePaint::default();
            };
            let dictionaries: Vec<_> = direct
                .into_iter()
                .chain(
                    inherited
                        .into_iter()
                        .filter_map(|id| document.get_dictionary(id).ok()),
                )
                .collect();
            let patterned = dictionaries
                .iter()
                .any(|resources| resources.has(b"Pattern"));
            let mut paint = PagePaint {
                patterned,
                ..PagePaint::default()
            };
            let mut masked = false;
            let mut simple = true;
            let mut alpha = false;
            for resource in &dictionaries {
                let Ok(states) = resource.get(b"ExtGState") else {
                    continue;
                };
                let Some(states) = pdf_dictionary(&document, states) else {
                    simple = false;
                    continue;
                };
                for (_, object) in states.iter() {
                    let Some(state) = pdf_dictionary(&document, object) else {
                        simple = false;
                        continue;
                    };
                    if state.get(b"BM").is_ok_and(|value| {
                        !matches!(value.as_name(), Ok(b"Normal" | b"Compatible"))
                    }) {
                        paint.non_normal_blend = true;
                        simple = false;
                    }
                    if state
                        .get(b"SMask")
                        .is_ok_and(|value| !matches!(value.as_name(), Ok(b"None")))
                    {
                        simple = false;
                        masked = true;
                    }
                    alpha |= state.has(b"ca") || state.has(b"CA");
                }
            }
            paint.simple_alpha = simple && alpha && !patterned;
            if !paint.non_normal_blend && (patterned || masked) {
                paint.editable = paint::rectangular_page(&document, id, &dictionaries);
            }
            paint
        })
        .collect()
}

fn pdf_dictionary<'a>(
    document: &'a lopdf::Document,
    object: &'a lopdf::Object,
) -> Option<&'a lopdf::Dictionary> {
    match object {
        lopdf::Object::Reference(id) => document.get_dictionary(*id).ok(),
        object => object.as_dict().ok(),
    }
}

fn rect_clip(api: &Api, clip: Handle, map: &impl Fn([f64; 2]) -> [f64; 2]) -> Option<Box2> {
    let count = unsafe { (api.clip_count)(clip) };
    if !(1..=64).contains(&count) {
        return None;
    }
    let mut result: Option<Box2> = None;
    for path in 0..count {
        let segments = unsafe { (api.clip_segments)(clip, path) };
        if !(4..=5).contains(&segments) {
            return None;
        }
        let mut points = Vec::new();
        for index in 0..segments {
            let segment = unsafe { (api.clip_segment)(clip, path, index) };
            let (mut x, mut y) = (0.0, 0.0);
            if segment.is_null()
                || unsafe { (api.segment_kind)(segment) } != if index == 0 { 2 } else { 0 }
                || unsafe { (api.segment_point)(segment, &mut x, &mut y) } == 0
            {
                return None;
            }
            points.push(map([f64::from(x), f64::from(y)]));
        }
        let bounds = rectangle(&points)?;
        result = Some(match result {
            Some(prior) => prior.intersection(bounds)?,
            None => bounds,
        });
    }
    result
}

fn filled_rectangle(drawing: &Drawing) -> Option<Box2> {
    if drawing.stroke.is_some() || drawing.fill.is_none() {
        return None;
    }
    let [PathCommand::Move(start), rest @ .., PathCommand::Close] = drawing.commands.as_slice()
    else {
        return None;
    };
    if !(3..=4).contains(&rest.len()) {
        return None;
    }
    let mut points = vec![*start];
    for command in rest {
        let PathCommand::Line(point) = command else {
            return None;
        };
        points.push(*point);
    }
    rectangle(&points)
}

// A single contour fully inside the filled rectangle can serve as its geometry.
// Multiple curved contours can have holes or intersections and use the renderer.
fn curved_clip(
    api: &Api,
    clip: Handle,
    map: &impl Fn([f64; 2]) -> [f64; 2],
    mut drawing: Drawing,
) -> Option<Drawing> {
    let mut outer = filled_rectangle(&drawing)?;
    let count = unsafe { (api.clip_count)(clip) };
    if !(1..=64).contains(&count) {
        return None;
    }
    let mut curved = None;
    for path in 0..count {
        let count = unsafe { (api.clip_segments)(clip, path) };
        if !(3..=1024).contains(&count) {
            return None;
        }
        let mut points = Vec::new();
        let mut commands = Vec::new();
        let mut cubic = Vec::new();
        for index in 0..count {
            let segment = unsafe { (api.clip_segment)(clip, path, index) };
            let (mut x, mut y) = (0.0, 0.0);
            if segment.is_null() || unsafe { (api.segment_point)(segment, &mut x, &mut y) } == 0 {
                return None;
            }
            let p = map([f64::from(x), f64::from(y)]);
            if p.iter().any(|v| !v.is_finite()) {
                return None;
            }
            points.push(p);
            match unsafe { (api.segment_kind)(segment) } {
                2 if index == 0 => commands.push(PathCommand::Move(p)),
                0 if index > 0 && cubic.is_empty() => commands.push(PathCommand::Line(p)),
                1 if index > 0 => {
                    cubic.push(p);
                    if let [a, b, end] = cubic.as_slice() {
                        commands.push(PathCommand::Cubic([*a, *b, *end]));
                        cubic.clear();
                    }
                }
                _ => return None,
            }
            if index + 1 < count && unsafe { (api.segment_close)(segment) } != 0 {
                return None;
            }
        }
        if !cubic.is_empty() {
            return None;
        }
        if points.len() <= 5
            && commands.iter().all(|c| !matches!(c, PathCommand::Cubic(_)))
            && let Some(rect) = rectangle(&points)
        {
            outer = outer.intersection(rect)?;
        } else {
            if curved.is_some() || !commands.iter().any(|c| matches!(c, PathCommand::Cubic(_))) {
                return None;
            }
            commands.push(PathCommand::Close);
            curved = Some((Box2::from_points(points), commands));
        }
    }
    let (bounds, commands) = curved?;
    if !outer.contains([bounds.x0, bounds.y0]) || !outer.contains([bounds.x1, bounds.y1]) {
        return None;
    }
    drawing.bounds = bounds;
    drawing.commands = commands;
    Some(drawing)
}

fn rectangle(points: &[[f64; 2]]) -> Option<Box2> {
    let bounds = Box2::from_points(points.iter().copied());
    if bounds.width().min(bounds.height()) <= 0.0 {
        return None;
    }
    let mut corners = 0_u8;
    for &[x, y] in points {
        let right = (x - bounds.x1).abs() < 0.01;
        let bottom = (y - bounds.y1).abs() < 0.01;
        if (!right && (x - bounds.x0).abs() >= 0.01) || (!bottom && (y - bounds.y0).abs() >= 0.01) {
            return None;
        }
        corners |= 1 << (u8::from(right) + 2 * u8::from(bottom));
    }
    (corners == 15
        && points
            .iter()
            .zip(points.iter().cycle().skip(1))
            .all(|(a, b)| (a[0] - b[0]).abs() < 0.01 || (a[1] - b[1]).abs() < 0.01))
    .then_some(bounds)
}

fn clip_drawing(mut drawing: Drawing, clip: Box2) -> Option<Drawing> {
    if clip.contains([drawing.bounds.x0, drawing.bounds.y0])
        && clip.contains([drawing.bounds.x1, drawing.bounds.y1])
        && drawing.stroke.is_none()
    {
        return Some(drawing);
    }
    let r = filled_rectangle(&drawing)?.intersection(clip)?;
    drawing.bounds = r;
    drawing.commands = vec![
        PathCommand::Move([r.x0, r.y0]),
        PathCommand::Line([r.x1, r.y0]),
        PathCommand::Line([r.x1, r.y1]),
        PathCommand::Line([r.x0, r.y1]),
        PathCommand::Close,
    ];
    Some(drawing)
}

fn path(
    api: &Api,
    object: Handle,
    map: &impl Fn([f64; 2]) -> [f64; 2],
    order: usize,
) -> Option<Drawing> {
    let (mut fill, mut stroke, mut width) = (0, 0, 0.0_f32);
    let mut matrix = [0.0; 6];
    unsafe {
        if (api.mode)(object, &mut fill, &mut stroke) == 0 || (api.matrix)(object, &mut matrix) == 0
        {
            return None;
        }
        (api.stroke_width)(object, &mut width);
    }
    let [a, b, c, d, e, f] = matrix;
    let count = unsafe { (api.segments)(object) };
    if !(2..=20_000).contains(&count) {
        return None;
    }
    let mut commands = Vec::new();
    let mut points = Vec::new();
    let mut cubic = Vec::new();
    for index in 0..count {
        let segment = unsafe { (api.segment)(object, index) };
        let (mut x, mut y) = (0.0, 0.0);
        if segment.is_null() || unsafe { (api.segment_point)(segment, &mut x, &mut y) } == 0 {
            return None;
        }
        let point = map([f64::from(a * x + c * y + e), f64::from(b * x + d * y + f)]);
        if point
            .iter()
            .any(|value| !value.is_finite() || value.abs() > 100_000.0)
        {
            return None;
        }
        points.push(point);
        match unsafe { (api.segment_kind)(segment) } {
            0 if cubic.is_empty() => commands.push(PathCommand::Line(point)),
            2 if cubic.is_empty() => commands.push(PathCommand::Move(point)),
            1 => {
                cubic.push(point);
                if cubic.len() == 3 {
                    commands.push(PathCommand::Cubic([cubic[0], cubic[1], cubic[2]]));
                    cubic.clear();
                }
            }
            _ => return None,
        }
        if cubic.is_empty() && unsafe { (api.segment_close)(segment) } != 0 {
            commands.push(PathCommand::Close);
        }
    }
    if !cubic.is_empty() {
        return None;
    }
    let fill = if fill != 0 {
        Some(rgba(api.fill, object)?)
    } else {
        None
    };
    let stroke = if stroke != 0 {
        Some(rgba(api.stroke, object)?)
    } else {
        None
    };
    Some(Drawing {
        order,
        bounds: Box2::from_points(points),
        commands,
        stroke,
        fill,
        gradient: None,
        width: f64::from(width * a.hypot(b)).max(0.1),
    })
}

fn cropped_png(
    engine: &Engine,
    page: Handle,
    canvas: Box2,
    region: Box2,
    annotations: bool,
    precise: bool,
) -> io::Result<Option<(Box2, Vec<u8>)>> {
    let scale = if precise { 200.0 / 72.0 } else { 2.0 };
    let margin = if precise { 0.5 } else { 0.0 };
    let (x, y) = (
        ((region.x0 - margin).max(0.0) * scale).floor() as i32,
        ((region.y0 - margin).max(0.0) * scale).floor() as i32,
    );
    let w = (((region.x1 + margin).min(canvas.x1) * scale).ceil() as i32 - x).max(1);
    let h = (((region.y1 + margin).min(canvas.y1) * scale).ceil() as i32 - y).max(1);
    if i64::from(w) * i64::from(h) > MAX_PIXELS as i64 {
        return Err(invalid("PDF의 잘라낸 그림 크기 제한을 초과했습니다."));
    }
    let mut pixels = vec![0_u8; w as usize * h as usize * 4];
    {
        let bitmap = Owned::new(
            unsafe { (engine.create_bitmap)(w, h, 4, pixels.as_mut_ptr().cast(), w * 4) },
            engine.free_bitmap,
            || invalid("PDF 비트맵을 만들 수 없습니다."),
        )?;
        unsafe {
            (engine.render)(
                bitmap.value,
                page,
                -x,
                -y,
                (canvas.width() * scale).ceil() as i32,
                (canvas.height() * scale).ceil() as i32,
                0,
                i32::from(annotations),
            )
        };
    }
    let (mut left, mut top, mut right, mut bottom) = (w, h, -1, -1);
    for (index, pixel) in pixels.chunks_exact_mut(4).enumerate() {
        if pixel[3] != 0 {
            let (x, y) = (index as i32 % w, index as i32 / w);
            left = left.min(x);
            top = top.min(y);
            right = right.max(x);
            bottom = bottom.max(y);
            pixel.swap(0, 2);
        }
    }
    if right < left || bottom < top {
        return Ok(None);
    }
    let pixels = image::RgbaImage::from_raw(w as u32, h as u32, pixels)
        .ok_or_else(|| invalid("PDF 그림의 버퍼 크기가 올바르지 않습니다."))?;
    let pixels = image::imageops::crop_imm(
        &pixels,
        left as u32,
        top as u32,
        (right - left + 1) as u32,
        (bottom - top + 1) as u32,
    )
    .to_image();
    let mut png = Cursor::new(Vec::new());
    pixels
        .write_to(&mut png, image::ImageFormat::Png)
        .map_err(io::Error::other)?;
    let bounds = Box2::from_points([
        [f64::from(x + left) / scale, f64::from(y + top) / scale],
        [
            f64::from(x + right + 1) / scale,
            f64::from(y + bottom + 1) / scale,
        ],
    ]);
    Ok(Some((bounds, png.into_inner())))
}

fn original_png(engine: &Engine, api: &Api, object: Handle) -> io::Result<Option<Vec<u8>>> {
    let mut matrix = Matrix::default();
    if unsafe { (api.matrix)(object, &mut matrix) } == 0
        || matrix[0] <= 0.0
        || matrix[3] <= 0.0
        || matrix[1].abs() > 0.0001
        || matrix[2].abs() > 0.0001
    {
        return Ok(None);
    }
    let (mut width, mut height) = (0, 0);
    if unsafe { (api.image_size)(object, &mut width, &mut height) } == 0
        || width == 0
        || height == 0
        || u64::from(width) * u64::from(height) > MAX_PIXELS
    {
        return Ok(None);
    }
    let Ok(bitmap) = Owned::new(
        unsafe { (api.image_bitmap)(object) },
        engine.free_bitmap,
        || invalid("PDF 원본 그림을 읽을 수 없습니다."),
    ) else {
        return Ok(None);
    };
    let (format, stride, buffer, w, h) = unsafe {
        (
            (api.bitmap_format)(bitmap.value),
            (api.bitmap_stride)(bitmap.value),
            (api.bitmap_buffer)(bitmap.value),
            (api.bitmap_width)(bitmap.value),
            (api.bitmap_height)(bitmap.value),
        )
    };
    let channels = match format {
        1 => 1,
        2 => 3,
        3 => 4,
        _ => return Ok(None),
    };
    let row_bytes = width as usize * channels;
    if w != width as i32
        || h != height as i32
        || buffer.is_null()
        || (stride as usize) < row_bytes
        || stride <= 0
        || stride as usize > row_bytes.saturating_add(16)
    {
        return Ok(None);
    }
    let mut rgb = Vec::with_capacity(width as usize * height as usize * 3);
    for row in 0..height as usize {
        let row =
            unsafe { std::slice::from_raw_parts(buffer.add(row * stride as usize), row_bytes) };
        for pixel in row.chunks_exact(channels) {
            if channels == 1 {
                rgb.extend_from_slice(&[pixel[0]; 3]);
            } else {
                rgb.extend_from_slice(&[pixel[2], pixel[1], pixel[0]]);
            }
        }
    }
    let image = image::RgbImage::from_raw(width, height, rgb)
        .ok_or_else(|| invalid("PDF 원본 그림 크기가 올바르지 않습니다."))?;
    let mut png = Cursor::new(Vec::new());
    image
        .write_to(&mut png, image::ImageFormat::Png)
        .map_err(io::Error::other)?;
    Ok(Some(png.into_inner()))
}

fn spans(
    engine: &Engine,
    api: &Api,
    page: Handle,
    map: &impl Fn([f64; 2]) -> [f64; 2],
    canvas: Box2,
    order: &HashMap<Handle, usize>,
    check: Check<'_>,
) -> io::Result<Vec<Span>> {
    let handle = Owned::new(
        unsafe { (engine.load_text)(page) },
        engine.close_text,
        || invalid("PDF에서 텍스트를 읽을 수 없습니다."),
    )?;
    let count = unsafe { (engine.chars)(handle.value) };
    if !(0..=500_000).contains(&count) {
        return Err(invalid("PDF 페이지의 편집용 텍스트 한도를 초과했습니다."));
    }
    let mut result: Vec<Span> = Vec::new();
    let mut previous = std::ptr::null_mut();
    let mut pending_space = false;
    for index in 0..count {
        if index % 1024 == 0 {
            check()?;
        }
        let Some(ch) = char::from_u32(unsafe { (api.unicode)(handle.value, index) }) else {
            continue;
        };
        if matches!(ch, '\r' | '\n') {
            previous = std::ptr::null_mut();
            pending_space = false;
            continue;
        }
        if ch.is_control() {
            continue;
        }
        let object = unsafe { (api.text_object)(handle.value, index) };
        if object.is_null() {
            pending_space = ch.is_whitespace();
            continue;
        }
        if matches!(unsafe { (api.render_mode)(object) }, 3 | 7) {
            continue;
        }
        let (mut left, mut right, mut bottom, mut top) = (0.0, 0.0, 0.0, 0.0);
        if unsafe {
            (api.char_box)(
                handle.value,
                index,
                &mut left,
                &mut right,
                &mut bottom,
                &mut top,
            )
        } == 0
        {
            continue;
        }
        let bounds = Box2::from_points(
            [[left, bottom], [left, top], [right, bottom], [right, top]].map(map),
        );
        if !canvas.contains(bounds.center()) {
            continue;
        }
        let selection = selection_box(api, handle.value, index, map).unwrap_or(bounds);
        if object == previous
            && let Some(last) = result.last_mut()
        {
            if pending_space {
                last.text.push(' ');
            }
            last.text.push(ch);
            last.bounds = last.bounds.union(bounds);
            last.selection = last.selection.union(selection);
        } else {
            let size = unsafe { (api.font_size)(handle.value, index) }.clamp(1.0, 500.0);
            let mut flags = 0;
            let mut buffer = [0_u8; 512];
            let length = unsafe {
                (api.font)(
                    handle.value,
                    index,
                    buffer.as_mut_ptr().cast(),
                    buffer.len() as c_ulong,
                    &mut flags,
                )
            } as usize;
            let mut font = if (1..=buffer.len()).contains(&length) {
                String::from_utf8_lossy(&buffer[..length - 1]).into_owned()
            } else {
                "Malgun Gothic".into()
            };
            if font.as_bytes().get(6) == Some(&b'+')
                && font.as_bytes()[..6].iter().all(u8::is_ascii_uppercase)
            {
                font = font[7..].to_owned();
            }
            let lowercase = font.to_ascii_lowercase();
            let standard_font = match lowercase.as_str() {
                "helvetica" => Some([1.075, 0.299]),
                "helvetica-bold" => Some([1.070, 0.307]),
                "helvetica-oblique" => Some([1.070, 0.284]),
                "helvetica-boldoblique" => Some([1.073, 0.309]),
                _ => None,
            };
            let bold = flags & (1 << 18) != 0 || lowercase.contains("bold");
            let italic = flags & (1 << 6) != 0
                || lowercase.contains("italic")
                || lowercase.contains("oblique");
            if lowercase.starts_with("helvetica") || lowercase.starts_with("arial") {
                font = "Arial".into();
            } else if lowercase.starts_with("times") {
                font = "Times New Roman".into();
            } else if lowercase.starts_with("courier") {
                font = "Courier New".into();
            }
            let rgba = rgba(api.fill, object).unwrap_or([0, 0, 0, 255]);
            let (mut x, mut y) = (0.0, 0.0);
            let origin = (unsafe { (api.char_origin)(handle.value, index, &mut x, &mut y) } != 0)
                .then(|| map([x, y]))
                .filter(|point| point.iter().all(|value| value.is_finite()));
            let mut matrix = Matrix::default();
            let rotation = if unsafe { (api.char_matrix)(handle.value, index, &mut matrix) } != 0 {
                let [a, b] = map([f64::from(matrix[0]), f64::from(matrix[1])]);
                let [x, y] = map([0.0, 0.0]);
                (b - y).atan2(a - x)
            } else {
                0.0
            };
            result.push(Span {
                order: order.get(&object).copied().unwrap_or(usize::MAX / 2),
                text: ch.to_string(),
                bounds,
                selection,
                rotation,
                standard_font,
                origin,
                size: if size.is_finite() { size } else { 10.0 },
                font,
                bold,
                italic,
                rgba,
            });
        }
        pending_space = false;
        previous = object;
    }
    result.retain(|span| !span.text.trim().is_empty());
    Ok(result)
}

pub(crate) fn pages(
    source: &Path,
    include_images: bool,
    keep_original_images: bool,
    check: Check<'_>,
    mut emit: impl FnMut(Page) -> io::Result<()>,
) -> io::Result<()> {
    check()?;
    let paint = if include_images {
        page_paint(source)
    } else {
        Vec::new()
    };
    read(source, check, |engine, document, count| {
        if count > 1000 {
            return Err(invalid("편집용 PDF는 1,000쪽까지 지원합니다."));
        }
        let api = Api::load(engine)?;
        let mut mupdf = None;
        let mut tried_mupdf = false;
        let mut picture_bytes = 0_usize;
        for index in 0..count {
            check()?;
            let paint = paint.get(index as usize);
            let patterned = paint.is_some_and(|paint| paint.patterned);
            let page = Owned::new(
                unsafe { (engine.load_page)(document, index) },
                engine.close_page,
                || invalid("PDF 페이지를 읽을 수 없습니다."),
            )?;
            let (width, height) = unsafe {
                (
                    f64::from((engine.width)(page.value)),
                    f64::from((engine.height)(page.value)),
                )
            };
            if !(1.0..=20_000.0).contains(&width) || !(1.0..=20_000.0).contains(&height) {
                return Err(invalid("PDF 페이지 크기가 허용 범위를 벗어납니다."));
            }
            let canvas = Box2::from_points([[0.0, 0.0], [width, height]]);
            let map = map_coordinates(&api, page.value, width, height)?;
            if keep_original_images
                && unsafe { (api.annotation_count)(page.value) } == 0
                && let Some(editable) = paint.and_then(|paint| paint.editable.as_ref())
            {
                emit(Page {
                    bounds: canvas,
                    spans: Vec::new(),
                    paths: editable
                        .iter()
                        .filter_map(|path| paint::map_path(path, &map, canvas))
                        .collect(),
                    pictures: Vec::new(),
                    scanned: false,
                })?;
                continue;
            }
            let count = unsafe { (api.objects)(page.value) };
            if !(0..=50_000).contains(&count) {
                return Err(invalid("PDF 페이지 개체의 개수 한도를 초과했습니다."));
            }
            let objects: Vec<_> = (0..count)
                .map(|index| unsafe { (api.object)(page.value, index) })
                .collect();
            let order = objects
                .iter()
                .copied()
                .enumerate()
                .map(|(index, object)| (object, index))
                .collect();
            let mut spans = spans(engine, &api, page.value, &map, canvas, &order, check)?;
            let mut paths = Vec::new();
            let mut regions = Vec::new();
            let mut scan_image = false;
            let mut blend_members = Vec::new();
            let mut composite = None;
            if keep_original_images && paint.is_some_and(|p| p.non_normal_blend) {
                if !tried_mupdf {
                    mupdf = crate::mupdf::Regions::open(source);
                    tried_mupdf = true;
                }
                let localized = if unsafe { (api.annotation_count)(page.value) } == 0 {
                    blend_region(&api, &objects, &spans, canvas, &map)
                } else {
                    None
                };
                let bounds = localized.as_ref().map_or(canvas, |(region, _)| *region);
                if let Some((bounds, png)) = mupdf.as_ref().and_then(|doc| doc.crop(index, bounds))
                {
                    picture_bytes = picture_bytes.saturating_add(png.len());
                    if picture_bytes > 512 * 1024 * 1024 {
                        return Err(invalid("PDF의 그림 변환 용량이 한도를 초과했습니다."));
                    }
                    if let Some((_, members)) = localized {
                        blend_members = members;
                        spans.retain(|s| !blend_members[s.order]);
                    } else {
                        blend_members = vec![true; objects.len()];
                        spans.clear();
                    }
                    composite = Some(Picture {
                        order: blend_members
                            .iter()
                            .position(|&affected| affected)
                            .unwrap_or(0),
                        bounds,
                        png,
                        flattened: true,
                    });
                }
            }
            for (index, object) in objects.iter().copied().enumerate() {
                if index % 256 == 0 {
                    check()?;
                }
                if object.is_null() || blend_members.get(index) == Some(&true) {
                    continue;
                }
                let kind = unsafe { (api.kind)(object) };
                let clip = unsafe { (api.clip)(object) };
                let clipped = !clip.is_null() && unsafe { (api.clip_count)(clip) } > 0;
                if kind == 1
                    && (!keep_original_images
                        || !clipped
                        || rect_clip(&api, clip, &map).is_some_and(|rect| {
                            spans.iter().filter(|s| s.order == index).all(|s| {
                                rect.contains([s.bounds.x0, s.bounds.y0])
                                    && rect.contains([s.bounds.x1, s.bounds.y1])
                            })
                        }))
                {
                    continue;
                }
                if kind == 1 && !spans.iter().any(|s| s.order == index) {
                    continue;
                }
                let Some(bounds) = object_bounds(&api, object, &map) else {
                    continue;
                };
                let transparent = unsafe { (api.transparent)(object) } != 0;
                if kind == 2
                    && let Some(path) = path(&api, object, &map, index)
                    && (!transparent
                        || keep_original_images
                            && paint.is_some_and(|p| p.simple_alpha)
                            && path
                                .fill
                                .into_iter()
                                .chain(path.stroke)
                                .any(|color| color[3] < 255)
                            && path.fill.is_none() != path.stroke.is_none())
                    && (!patterned || path.fill.is_none() && path.stroke.is_some())
                    && let Some(path) = if !clipped {
                        Some(path)
                    } else if keep_original_images {
                        match rect_clip(&api, clip, &map) {
                            Some(rect) => clip_drawing(path, rect),
                            None => curved_clip(&api, clip, &map, path),
                        }
                    } else {
                        None
                    }
                {
                    paths.push(path);
                } else if include_images && let Some(visible) = canvas.intersection(bounds) {
                    if regions.len() >= 1000 {
                        return Err(invalid("PDF 페이지의 그림 영역 한도를 초과했습니다."));
                    }
                    scan_image |=
                        kind == 3 && visible.width() * visible.height() >= width * height * 0.8;
                    let direct = keep_original_images
                        && kind == 3
                        && !clipped
                        && !transparent
                        && !patterned
                        && visible == bounds;
                    if kind == 1 {
                        spans.retain(|s| s.order != index);
                    }
                    regions.push((
                        index,
                        object,
                        visible,
                        kind != 3 || clipped || transparent,
                        direct,
                    ));
                }
            }
            let scanned = spans.is_empty() && scan_image;
            let mut annotation_box: Option<Box2> = None;
            if include_images && !(composite.is_some() && blend_members.iter().all(|&v| v)) {
                let count = unsafe { (api.annotation_count)(page.value) };
                for index in 0..count.clamp(0, 10_000) {
                    check()?;
                    let annotation = unsafe { (api.annotation)(page.value, index) };
                    if annotation.is_null() {
                        continue;
                    }
                    let mut raw = [0_f32; 4];
                    let valid = unsafe { (api.annotation_bounds)(annotation, &mut raw) } != 0;
                    unsafe { (api.close_annotation)(annotation) };
                    if valid {
                        let [l, t, r, b] = raw.map(f64::from);
                        let bounds = Box2::from_points([[l, t], [l, b], [r, t], [r, b]].map(&map));
                        if let Some(bounds) = canvas.intersection(bounds) {
                            annotation_box = Some(
                                annotation_box.map_or(bounds, |combined| combined.union(bounds)),
                            );
                        }
                    }
                }
            }
            if annotation_box.is_some() || !regions.is_empty() {
                for object in objects.into_iter().filter(|object| !object.is_null()) {
                    if unsafe { (api.set_active)(object, 0) } == 0 {
                        return Err(invalid("PDF의 개체를 개별 그림으로 분리할 수 없습니다."));
                    }
                }
            }
            let mut pictures: Vec<_> = composite.into_iter().collect();
            let isolated = keep_original_images
                && regions.len() == 1
                && count == 1
                && annotation_box.is_none()
                && regions[0].3;
            if isolated && !tried_mupdf {
                mupdf = crate::mupdf::Regions::open(source);
                tried_mupdf = true;
            }
            if let Some(bounds) = annotation_box {
                regions.push((usize::MAX, std::ptr::null_mut(), bounds, true, false));
            }
            for (order, object, bounds, flattened, direct) in regions {
                check()?;
                let annotation = object.is_null();
                let original = if isolated {
                    mupdf
                        .as_ref()
                        .and_then(|document| document.crop(index, bounds))
                } else if direct {
                    original_png(engine, &api, object)?.map(|png| (bounds, png))
                } else {
                    None
                };
                let rendered = if original.is_some() {
                    original
                } else {
                    if !annotation && unsafe { (api.set_active)(object, 1) } == 0 {
                        return Err(invalid("PDF의 개체를 개별 그림으로 렌더링할 수 없습니다."));
                    }
                    let rendered = cropped_png(
                        engine,
                        page.value,
                        canvas,
                        bounds,
                        annotation,
                        keep_original_images && flattened,
                    )?;
                    if !annotation {
                        unsafe { (api.set_active)(object, 0) };
                    }
                    rendered
                };
                let Some((bounds, png)) = rendered else {
                    continue;
                };
                picture_bytes = picture_bytes.saturating_add(png.len());
                if picture_bytes > 512 * 1024 * 1024 {
                    return Err(invalid("PDF의 그림 변환 용량이 한도를 초과했습니다."));
                }
                pictures.push(Picture {
                    order,
                    bounds,
                    png,
                    flattened,
                });
            }
            emit(Page {
                bounds: canvas,
                spans,
                paths,
                pictures,
                scanned,
            })?;
        }
        Ok(())
    })
}
