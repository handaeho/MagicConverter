use image::RgbaImage;
use regex::Regex;
use roxmltree::{Document, Node};
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::path::Path;
use std::sync::{Arc, OnceLock};
use svg2pdf::usvg::{Options, Tree, fontdb};

const SVG_NS: &str = "http://www.w3.org/2000/svg";
const LIMIT: usize = 32 * 1024 * 1024;
const PIXELS: u64 = 80_000_000;

fn invalid(text: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, text.into())
}

fn read(source: &Path, check: &dyn Fn() -> io::Result<()>) -> io::Result<String> {
    let mut source = File::open(source)?.take(LIMIT as u64 + 1);
    let mut raw = Vec::new();
    let mut chunk = [0_u8; 64 * 1024];
    loop {
        check()?;
        let count = source.read(&mut chunk)?;
        if count == 0 {
            break;
        }
        raw.extend_from_slice(&chunk[..count]);
    }
    if raw.len() > LIMIT {
        return Err(invalid("SVG PDF 입력의 32MiB 한도를 초과했습니다."));
    }
    let (decoded, _, malformed) = encoding_rs::UTF_8.decode(&raw);
    if malformed {
        return Err(invalid("유효한 SVG 파일이 아닙니다."));
    }
    Ok(decoded.into_owned())
}

fn length(raw: Option<&str>) -> io::Result<Option<f64>> {
    static LENGTH: OnceLock<Regex> = OnceLock::new();
    let Some(raw) = raw else {
        return Ok(None);
    };
    let captures = LENGTH
        .get_or_init(|| {
            Regex::new(r"^\s*([+\-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+\-]?\d+)?)\s*([a-z%]*)\s*$")
                .expect("SVG length expression")
        })
        .captures(raw);
    let unsupported = || {
        invalid(format!(
            "SVG PDF에서 지원하지 않는 페이지 크기입니다: {raw}"
        ))
    };
    let matches = captures.ok_or_else(&unsupported)?;
    let length = matches[1].parse::<f64>().map_err(|_| unsupported())?;
    if !length.is_finite() || length <= 0.0 {
        return Err(invalid("SVG 페이지 크기는 양수여야 합니다."));
    }
    let scale = match &matches[2] {
        "" | "px" => 1.0,
        "pt" => 96.0 / 72.0,
        "pc" => 16.0,
        "mm" => 96.0 / 25.4,
        "cm" => 96.0 / 2.54,
        "in" => 96.0,
        "q" => 96.0 / 101.6,
        "%" => return Ok(None),
        _ => return Err(unsupported()),
    };
    Ok(Some(length * scale))
}

fn page(root: Node<'_, '_>, notices: &mut Vec<String>) -> io::Result<(f64, f64)> {
    let width = length(root.attribute("width"))?;
    let height = length(root.attribute("height"))?;
    let size = if let Some(view_box) = root.attribute("viewBox").filter(|text| !text.is_empty()) {
        let fail = || invalid("SVG viewBox가 올바르지 않습니다.");
        let values = view_box
            .split(|ch: char| ch.is_whitespace() || ch == ',')
            .filter(|part| !part.is_empty())
            .map(|value| value.parse::<f64>())
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| fail())?;
        let [_, _, vw, vh] = values.as_slice() else {
            return Err(fail());
        };
        if !values.iter().all(|value| value.is_finite()) || *vw <= 0.0 || *vh <= 0.0 {
            return Err(fail());
        }
        if width.is_none() || height.is_none() {
            notices.push(
                "SVG에 고정 크기가 없는 방향은 viewBox 비율로 PDF 쪽 크기를 정합니다.".to_owned(),
            );
        }
        let width = width.unwrap_or_else(|| height.map_or(*vw, |height| height * vw / vh));
        (width, height.unwrap_or(width * vh / vw))
    } else {
        width
            .zip(height)
            .ok_or_else(|| invalid("SVG PDF 쪽 크기에 고정 너비·높이 또는 viewBox가 필요합니다."))?
    };
    if size.0.min(size.1) * 0.75 < 1.0
        || size.0.max(size.1) * 0.75 > 14400.0
        || size.0 * size.1 > PIXELS as f64
    {
        return Err(invalid("SVG PDF 쪽 크기가 지원 범위를 벗어났습니다."));
    }
    if let Some(ratio) = root.attribute("preserveAspectRatio") {
        static ASPECT: OnceLock<Regex> = OnceLock::new();
        if !ASPECT
            .get_or_init(|| {
                Regex::new(r"^(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?:\s+(?:meet|slice))?)$")
                    .expect("SVG aspect ratio expression")
            })
            .is_match(ratio.trim())
        {
            return Err(invalid("SVG preserveAspectRatio가 올바르지 않습니다."));
        }
    }
    Ok(size)
}

fn validate_pdf(root: Node<'_, '_>, check: &dyn Fn() -> io::Result<()>) -> io::Result<()> {
    static EFFECT: OnceLock<Regex> = OnceLock::new();
    static URL: OnceLock<Regex> = OnceLock::new();
    let effect = EFFECT.get_or_init(|| {
        Regex::new(
            r"(?i)(?:^|[;{])\s*(?:clip-path|mask|filter|writing-mode|animation(?:-[\w-]+)?)\s*:",
        )
        .expect("SVG unsupported styles expression")
    });
    let url = URL.get_or_init(|| {
        Regex::new(r#"(?i)url\(\s*['"]?([^\s)'"]+)"#).expect("SVG resource expression")
    });
    let mut nodes = vec![root];
    while let Some(node) = nodes.pop() {
        check()?;
        let tag = node.tag_name();
        let name = tag.name();
        let supported = tag.namespace().is_none_or(|namespace| namespace == SVG_NS)
            && match name {
                "svg" => node == root,
                "g" | "defs" | "title" | "desc" | "metadata" | "style" | "path" | "rect"
                | "circle" | "ellipse" | "line" | "polyline" | "polygon" | "text" | "tspan"
                | "linearGradient" | "radialGradient" | "stop" | "image" | "use" => true,
                _ => false,
            };
        if !supported {
            return Err(invalid(format!(
                "SVG PDF에서 지원하지 않는 요소입니다: {name}"
            )));
        }
        if name == "metadata" {
            continue;
        }
        for attribute in node.attributes() {
            let name = attribute.name();
            let value = attribute.value();
            if matches!(
                name,
                "clip-path" | "mask" | "filter" | "textLength" | "lengthAdjust" | "writing-mode"
            ) && value != "none"
            {
                return Err(invalid(format!(
                    "SVG PDF에서 지원하지 않는 속성입니다: {name}"
                )));
            }
            if name == "href"
                && !(value.starts_with('#')
                    || value.starts_with("data:image/png;")
                    || value.starts_with("data:image/jpeg;"))
            {
                return Err(invalid(
                    "외부 파일을 참조하는 SVG는 PDF로 변환할 수 없습니다.",
                ));
            }
        }
        let style = if name == "style" {
            node.text()
        } else {
            node.attribute("style")
        }
        .unwrap_or_default();
        if effect.is_match(style) || style.to_ascii_lowercase().contains("@import") {
            return Err(invalid(
                "SVG PDF에서 지원하지 않는 CSS 효과 또는 외부 참조가 있습니다.",
            ));
        }
        if node
            .attributes()
            .map(|attribute| attribute.value())
            .chain([style])
            .any(|value| {
                url.captures_iter(value)
                    .any(|reference| !reference[1].starts_with('#'))
            })
        {
            return Err(invalid(
                "외부 리소스를 참조하는 SVG는 PDF로 변환할 수 없습니다.",
            ));
        }
        nodes.extend(node.children().filter(Node::is_element).rev());
    }
    Ok(())
}

fn options() -> Options<'static> {
    static FONTS: OnceLock<Arc<fontdb::Database>> = OnceLock::new();
    let fontdb = FONTS.get_or_init(|| {
        let mut database = fontdb::Database::new();
        database.load_system_fonts();
        database.set_sans_serif_family("Arial");
        database.set_serif_family("Times New Roman");
        database.set_monospace_family("Courier New");
        Arc::new(database)
    });
    Options {
        fontdb: fontdb.clone(),
        font_family: "Arial".to_owned(),
        ..Options::default()
    }
}

fn set_page(xml: &str, root: Node<'_, '_>, (width, height): (f64, f64)) -> String {
    let mut xml_with_size = xml.to_owned();
    let start = root.range().start;
    let start = start
        + 1
        + xml[start + 1..]
            .find(|ch: char| ch.is_whitespace() || matches!(ch, '/' | '>'))
            .expect("parsed SVG element");
    let mut changes = Vec::new();
    let mut insertion = String::new();
    for (key, value) in [("width", width), ("height", height)] {
        if let Some(attribute) = root.attribute_node(key) {
            changes.push((attribute.range_value(), value.to_string()));
        } else {
            insertion.push_str(&format!(" {key}=\"{value}\""));
        }
    }
    if root.tag_name().namespace().is_none() {
        insertion.push_str(" xmlns=\"http://www.w3.org/2000/svg\"");
    }
    if !insertion.is_empty() {
        changes.push((start..start, insertion));
    }
    changes.sort_unstable_by_key(|(range, _)| std::cmp::Reverse(range.start));
    for (range, value) in changes {
        xml_with_size.replace_range(range, &value);
    }
    xml_with_size
}

pub fn pdf(
    source: &Path,
    target: &mut File,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    let xml = read(source, check)?;
    let parsed = Document::parse(&xml).map_err(|error| invalid(error.to_string()))?;
    let root = parsed.root_element();
    if root.tag_name().name() != "svg"
        || root
            .tag_name()
            .namespace()
            .is_some_and(|namespace| namespace != SVG_NS)
    {
        return Err(invalid("유효한 SVG 파일이 아닙니다."));
    }
    validate_pdf(root, check)?;
    let dimensions = page(root, notices)?;
    if let Some(engine) = crate::qt_render::engine() {
        return qt_pdf(&engine, &xml, root, dimensions, target, check);
    }
    let xml = set_page(&xml, root, dimensions);
    let tree = Tree::from_str(&xml, &options()).map_err(|error| invalid(error.to_string()))?;
    check()?;
    let pdf = svg2pdf::to_pdf(
        &tree,
        svg2pdf::ConversionOptions::default(),
        svg2pdf::PageOptions { dpi: 96.0 },
    )
    .map_err(|error| invalid(error.to_string()))?;
    check()?;
    target.write_all(&pdf)
}

fn qt_pdf(
    engine: &Path,
    xml: &str,
    root: Node<'_, '_>,
    (width, height): (f64, f64),
    target: &mut File,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    use lopdf::{Document, Object, Stream};
    let viewbox: Vec<f64> = root
        .attribute("viewBox")
        .filter(|value| !value.is_empty())
        .map_or_else(
            || vec![0.0, 0.0, width, height],
            |viewbox| {
                viewbox
                    .split(|ch: char| ch.is_whitespace() || ch == ',')
                    .filter(|part| !part.is_empty())
                    .map(|part| part.parse().expect("validated SVG viewbox"))
                    .collect()
            },
        );
    let viewbox: [f64; 4] = viewbox.try_into().expect("validated SVG viewbox");
    let aspect = root.attribute("preserveAspectRatio").unwrap_or("xMidYMid");
    let aspect: Vec<_> = aspect.split_whitespace().collect();
    let bounds = if aspect == ["none"] {
        [0.0, 0.0, width, height]
    } else {
        let alignment = aspect[0];
        let ratios = (width / viewbox[2], height / viewbox[3]);
        let scale = if aspect.get(1) == Some(&"slice") {
            ratios.0.max(ratios.1)
        } else {
            ratios.0.min(ratios.1)
        };
        let offset = |name| match name {
            "Min" => 0.0,
            "Max" => 1.0,
            _ => 0.5,
        };
        let w = viewbox[2] * scale;
        let h = viewbox[3] * scale;
        [
            (width - w) * offset(&alignment[1..4]),
            (height - h) * offset(&alignment[5..8]),
            w,
            h,
        ]
    };
    let values: Vec<_> = [width, height]
        .into_iter()
        .chain(viewbox)
        .chain(bounds)
        .map(|value| value.to_string().into())
        .collect();
    let stage = tempfile::Builder::new()
        .prefix("magic-svg-pdf-")
        .tempdir()?;
    let rendered = stage.path().join("result.pdf");
    crate::qt_render::render(engine, "svg-pdf", xml, &rendered, &values, check)?;
    let mut pdf = Document::load(&rendered).map_err(io::Error::other)?;
    let pages = pdf.get_pages();
    if pages.len() != 1 {
        return Err(invalid("SVG PDF 결과는 한 페이지여야 합니다."));
    }
    let page = pages[&1];
    let box_values = pdf
        .get_dictionary(page)
        .and_then(|dict| dict.get(b"MediaBox"))
        .and_then(Object::as_array)
        .map_err(io::Error::other)?;
    if box_values.len() != 4 {
        return Err(invalid("SVG PDF 쪽 크기가 유효하지 않습니다."));
    }
    let (actual_w, actual_h) = (
        box_values[2].as_float().map_err(io::Error::other)?,
        box_values[3].as_float().map_err(io::Error::other)?,
    );
    let (w, h) = (width * 0.75, height * 0.75);
    if actual_w <= 0.0 || actual_h <= 0.0 {
        return Err(invalid("SVG PDF 쪽 크기가 유효하지 않습니다."));
    }
    if f64::from(actual_w) != w || f64::from(actual_h) != h {
        let mut content = format!(
            "q\n{:.12} 0 0 {:.12} 0 0 cm\n",
            w / f64::from(actual_w),
            h / f64::from(actual_h)
        )
        .into_bytes();
        content.extend(pdf.get_page_content(page).map_err(io::Error::other)?);
        content.extend(b"\nQ\n");
        let stream = pdf.add_object(Stream::new(lopdf::Dictionary::new(), content));
        let dict = pdf.get_dictionary_mut(page).map_err(io::Error::other)?;
        dict.set("Contents", stream);
        dict.set(
            "MediaBox",
            vec![
                Object::Integer(0),
                Object::Integer(0),
                Object::Real(w as f32),
                Object::Real(h as f32),
            ],
        );
        pdf.prune_objects();
        check()?;
        pdf.save_to(target).map_err(io::Error::other)?;
    } else {
        let mut source = File::open(rendered)?;
        let len = source.metadata()?.len();
        crate::copy_stream(&mut source, target, len, check, |_| Ok(()))?;
    }
    check()
}

pub fn raster(source: &Path, check: &dyn Fn() -> io::Result<()>) -> io::Result<RgbaImage> {
    let xml = read(source, check)?;
    let parsed = Document::parse(&xml).map_err(|error| invalid(error.to_string()))?;
    let root = parsed.root_element();
    if root.tag_name().name() != "svg" {
        return Err(invalid("유효한 SVG 파일이 아닙니다."));
    }
    if let Some(engine) = crate::qt_render::engine() {
        validate_raster_resources(root, check)?;
        let stage = tempfile::Builder::new()
            .prefix("magic-svg-raster-")
            .tempdir()?;
        let png = stage.path().join("image.png");
        crate::qt_render::render(&engine, "svg-png", &xml, &png, &[], check)?;
        return image::open(png)
            .map(|image| image.into_rgba8())
            .map_err(io::Error::other);
    }
    let mut options = options();
    options.dpi = 90.0;
    let tree = if root.tag_name().namespace().is_none() {
        let dimensions = page(root, &mut Vec::new())?;
        Tree::from_str(&set_page(&xml, root, dimensions), &options)
    } else {
        Tree::from_str(&xml, &options)
    }
    .map_err(|error| invalid(error.to_string()))?;
    let size = tree.size().to_int_size();
    if u64::from(size.width()) * u64::from(size.height()) > PIXELS {
        return Err(invalid("SVG 크기가 없거나 너무 큽니다."));
    }
    let mut pixmap = resvg::tiny_skia::Pixmap::new(size.width(), size.height())
        .ok_or_else(|| invalid("SVG 크기가 없거나 너무 큽니다."))?;
    check()?;
    resvg::render(
        &tree,
        resvg::tiny_skia::Transform::identity(),
        &mut pixmap.as_mut(),
    );
    let mut bytes = Vec::with_capacity(pixmap.data().len());
    for pixel in pixmap.pixels() {
        let pixel = pixel.demultiply();
        bytes.extend([pixel.red(), pixel.green(), pixel.blue(), pixel.alpha()]);
    }
    check()?;
    RgbaImage::from_raw(size.width(), size.height(), bytes)
        .ok_or_else(|| invalid("유효한 SVG 파일이 아닙니다."))
}

fn validate_raster_resources(
    root: Node<'_, '_>,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    static URL: OnceLock<Regex> = OnceLock::new();
    let url = URL.get_or_init(|| {
        Regex::new(r#"(?i)url\(\s*['"]?([^\s)'"]+)"#).expect("SVG resource expression")
    });
    for node in root.descendants().filter(Node::is_element) {
        check()?;
        let embedded = |value: &str| {
            value.starts_with('#')
                || value.starts_with("data:image/png;")
                || value.starts_with("data:image/jpeg;")
        };
        let style = node.tag_name().name() == "style";
        if node
            .attributes()
            .any(|attr| attr.name() == "href" && !embedded(attr.value()))
            || node
                .attributes()
                .map(|attr| attr.value())
                .chain(node.text().filter(|_| style))
                .any(|value| {
                    value.to_ascii_lowercase().contains("@import")
                        || url
                            .captures_iter(value)
                            .any(|reference| !embedded(&reference[1]))
                })
        {
            return Err(invalid("외부 파일을 참조하는 SVG는 변환할 수 없습니다."));
        }
    }
    Ok(())
}
