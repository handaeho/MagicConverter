use super::Renderer;
use base64::Engine as _;
use roxmltree::Node;
use std::fmt::Write;
use std::io::{self, ErrorKind};

const A: &str = "http://schemas.openxmlformats.org/drawingml/2006/main";
const WP: &str = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing";
const WPG: &str = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup";
const WPS: &str = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape";
const PIC: &str = "http://schemas.openxmlformats.org/drawingml/2006/picture";

fn child<'a, 'b>(node: Node<'a, 'b>, ns: &str, name: &str) -> Option<Node<'a, 'b>> {
    node.children().find(|item| item.has_tag_name((ns, name)))
}

fn pair(node: Node<'_, '_>, keys: [&str; 2]) -> Option<[f64; 2]> {
    let values = keys.map(|key| {
        node.attribute(key)?
            .parse::<i32>()
            .ok()
            .filter(|value| value.unsigned_abs() <= 100_000_000)
            .map(f64::from)
    });
    Some([values[0]?, values[1]?])
}

fn rect(transform: Node<'_, '_>, offset: &str, size: &str) -> Option<[[f64; 2]; 2]> {
    Some([
        pair(child(transform, A, offset)?, ["x", "y"])?,
        pair(child(transform, A, size)?, ["cx", "cy"])?,
    ])
}

fn transform_attrs(node: Node<'_, '_>) -> Option<String> {
    let rotation = node.attribute("rot").unwrap_or("0").parse::<i32>().ok()?;
    let flip = |key| match node.attribute(key) {
        None | Some("0" | "false") => Some(0),
        Some("1" | "true") => Some(1),
        _ => None,
    };
    Some(format!(
        r#" data-word-transform="{} {} {}""#,
        rotation.rem_euclid(21_600_000),
        flip("flipH")?,
        flip("flipV")?
    ))
}

fn paint<'a>(node: Node<'a, '_>) -> Option<Option<&'a str>> {
    if child(node, A, "noFill").is_some() {
        return Some(None);
    }
    let fill = child(node, A, "solidFill")?;
    let color = child(fill, A, "srgbClr")?;
    if fill.children().filter(Node::is_element).count() != 1
        || color.children().any(|item| item.is_element())
    {
        return None;
    }
    Some(Some(crate::hwp_model::color(color.attribute("val")?)?))
}

fn hwp(value: f64) -> i32 {
    (value / 127.0).round_ties_even() as i32
}

fn members(
    renderer: &mut Renderer<'_, '_, '_>,
    group: Node<'_, '_>,
    size: [f64; 2],
    depth: usize,
    budget: &mut usize,
    output: &mut String,
    error: &mut Option<io::Error>,
) -> Option<()> {
    if depth >= 16
        || size
            .iter()
            .any(|&value| !(127.0..=100_000_000.0).contains(&value))
    {
        return None;
    }
    let properties = child(group, WPG, "grpSpPr")?;
    let transform = child(properties, A, "xfrm")?;
    let [origin, child_size] = rect(transform, "chOff", "chExt")?;
    if properties.children().filter(Node::is_element).count() != 1
        || child_size.iter().any(|&value| value <= 0.0)
    {
        return None;
    }
    let scale = [size[0] / child_size[0], size[1] / child_size[1]];
    let mut count = 0;
    for member in group.children().filter(Node::is_element) {
        let kind = member.tag_name();
        if kind.namespace() == Some(WPG)
            && matches!(kind.name(), "cNvPr" | "cNvGrpSpPr" | "grpSpPr")
        {
            continue;
        }
        *budget = budget.checked_sub(1)?;
        count += 1;
        let nested = kind.namespace() == Some(WPG) && matches!(kind.name(), "grpSp" | "wgp");
        let picture = member.has_tag_name((PIC, "pic"));
        let (ns, name) = if nested {
            (WPG, "grpSpPr")
        } else if picture {
            (PIC, "spPr")
        } else {
            (WPS, "spPr")
        };
        if !nested && !picture && !member.has_tag_name((WPS, "wsp")) {
            return None;
        }
        let properties = child(member, ns, name)?;
        let transform = child(properties, A, "xfrm")?;
        let transform_attributes = transform_attrs(transform)?;
        let [offset, extent] = rect(transform, "off", "ext")?;
        let offset = [0, 1].map(|axis| (offset[axis] - origin[axis]) * scale[axis]);
        let extent = [0, 1].map(|axis| extent[axis] * scale[axis]);
        let [x, y] = offset.map(hwp);
        let [w, h] = extent.map(hwp);
        if x.unsigned_abs() > 800_000
            || y.unsigned_abs() > 800_000
            || !(0..=800_000).contains(&w)
            || !(0..=800_000).contains(&h)
        {
            return None;
        }
        if nested {
            write!(
                output,
                r#"<span data-word-group="{x} {y} {w} {h}"{transform_attributes}>"#
            )
            .ok()?;
            members(renderer, member, extent, depth + 1, budget, output, error)?;
            output.push_str("</span>");
            continue;
        }
        if picture {
            if let Err(failure) = renderer.group_picture(
                member,
                properties,
                [x, y, w, h],
                &transform_attributes,
                output,
            ) {
                if matches!(failure.kind(), ErrorKind::Interrupted | ErrorKind::TimedOut) {
                    *error = Some(failure);
                }
                return None;
            }
            continue;
        }
        if member.children().filter(Node::is_element).any(|item| {
            !matches!(
                item.tag_name().name(),
                "cNvPr" | "cNvSpPr" | "spPr" | "bodyPr" | "txbx"
            )
        }) || properties.children().filter(Node::is_element).any(|item| {
            !matches!(
                item.tag_name().name(),
                "xfrm" | "prstGeom" | "custGeom" | "solidFill" | "noFill" | "ln"
            )
        }) {
            return None;
        }
        let (stroke, stroke_width) = match child(properties, A, "ln") {
            Some(line)
                if line.children().filter(Node::is_element).all(|item| {
                    matches!(item.tag_name().name(), "solidFill" | "noFill")
                        || item.has_tag_name((A, "prstDash"))
                            && item.attribute("val") == Some("solid")
                }) =>
            {
                let color = paint(line)?;
                let width = line.attribute("w").unwrap_or("12700").parse::<u32>().ok()?;
                (
                    color.unwrap_or("none"),
                    hwp(f64::from(width) * scale[0].min(scale[1])).clamp(1, 20_000),
                )
            }
            Some(_) => return None,
            None => ("none", 0),
        };
        let fill = paint(properties).unwrap_or(None).unwrap_or("none");
        if child(properties, A, "solidFill").is_some() && paint(properties).is_none() {
            return None;
        }
        if let Some(preset) = child(properties, A, "prstGeom") {
            let kind = preset.attribute("prst")?;
            if !matches!(kind, "rect" | "ellipse")
                || w <= 0
                || h <= 0
                || child(preset, A, "avLst")
                    .is_some_and(|list| list.children().any(|item| item.is_element()))
            {
                return None;
            }
            let mut box_attributes = String::new();
            if let Some(textbox) = child(member, WPS, "txbx") {
                if kind != "rect" {
                    return None;
                }
                match renderer.group_text(
                    textbox,
                    child(member, WPS, "bodyPr"),
                    w,
                    &mut box_attributes,
                ) {
                    Ok(()) => {}
                    Err(failure) => {
                        if matches!(failure.kind(), ErrorKind::Interrupted | ErrorKind::TimedOut) {
                            *error = Some(failure);
                        }
                        return None;
                    }
                }
            }
            write!(output, r#"<span data-word-shape="{kind}" data-box="{x} {y} {w} {h}" data-stroke="{stroke}" data-width="{stroke_width}" data-fill="{fill}"{transform_attributes}{box_attributes}></span>"#).ok()?;
        } else {
            if child(member, WPS, "txbx").is_some() {
                return None;
            }
            let geometry = child(properties, A, "custGeom")?;
            let paths = child(geometry, A, "pathLst")?;
            let path = child(paths, A, "path")?;
            if paths.children().filter(Node::is_element).count() != 1 {
                return None;
            }
            let pw = path
                .attribute("w")
                .map(str::parse::<u32>)
                .transpose()
                .ok()?
                .map_or(extent[0], f64::from);
            let ph = path
                .attribute("h")
                .map(str::parse::<u32>)
                .transpose()
                .ok()?
                .map_or(extent[1], f64::from);
            let mut vertices = Vec::new();
            let mut closed = false;
            for command in path.children().filter(Node::is_element) {
                match command.tag_name().name() {
                    "close" if vertices.len() >= 3 && !closed => closed = true,
                    "moveTo" | "lnTo"
                        if !closed
                            && vertices.len() < 256
                            && (command.tag_name().name() == "moveTo") == vertices.is_empty() =>
                    {
                        let [px, py] = pair(child(command, A, "pt")?, ["x", "y"])?;
                        if !(0.0..=pw).contains(&px) || !(0.0..=ph).contains(&py) {
                            return None;
                        }
                        vertices.push((
                            hwp(if pw == 0.0 { 0.0 } else { px / pw * extent[0] }),
                            hwp(if ph == 0.0 { 0.0 } else { py / ph * extent[1] }),
                        ));
                    }
                    _ => return None,
                }
            }
            let kind = match (vertices.len(), closed) {
                (2, false) if stroke != "none" => "line",
                (3.., true) => "polygon",
                _ => return None,
            };
            write!(output, r#"<span data-word-shape="{kind}" data-box="{x} {y} {w} {h}" data-stroke="{stroke}" data-width="{stroke_width}" data-fill="{fill}"{transform_attributes} data-points=""#).ok()?;
            for (px, py) in vertices {
                write!(output, "{px} {py} ").ok()?;
            }
            output.push_str(r#""></span>"#);
        }
    }
    (count > 0).then_some(())
}

impl Renderer<'_, '_, '_> {
    fn textbox_theme(&mut self) -> Option<[String; 2]> {
        let part = self
            .rels
            .values()
            .find(|name| name.starts_with("word/theme/"))?;
        let bytes = super::read_part(self.archive, part).ok()?;
        let xml = super::parse(std::str::from_utf8(&bytes).ok()?).ok()?;
        let scheme = xml
            .descendants()
            .find(|node| node.has_tag_name((A, "fontScheme")))?;
        Some(["majorFont", "minorFont"].map(|kind| {
            child(scheme, A, kind)
                .and_then(|font| child(font, A, "latin"))
                .and_then(|latin| latin.attribute("typeface"))
                .unwrap_or_default()
                .to_owned()
        }))
    }

    fn group_picture(
        &mut self,
        picture: Node<'_, '_>,
        properties: Node<'_, '_>,
        [x, y, w, h]: [i32; 4],
        transform: &str,
        output: &mut String,
    ) -> io::Result<()> {
        let invalid = || super::invalid("Word 그룹의 그림 속성을 보존할 수 없습니다.");
        let fill = child(picture, PIC, "blipFill").ok_or_else(invalid)?;
        if properties
            .children()
            .filter(Node::is_element)
            .any(|item| !matches!(item.tag_name().name(), "xfrm" | "prstGeom" | "noFill"))
            || child(properties, A, "prstGeom")
                .is_some_and(|geom| geom.attribute("prst") != Some("rect"))
            || child(fill, A, "tile").is_some()
        {
            return Err(invalid());
        }
        let blip = child(fill, A, "blip").ok_or_else(invalid)?;
        let id = blip.attribute((super::REL, "embed")).ok_or_else(invalid)?;
        let part = self.rels.get(id).ok_or_else(invalid)?;
        (self.check)()?;
        let bytes = super::read_part(self.archive, part)?;
        let decoder = image::ImageReader::new(io::Cursor::new(&bytes)).with_guessed_format()?;
        let mime = decoder.format().ok_or_else(invalid)?.to_mime_type();
        let (width, height) = decoder.into_dimensions().map_err(io::Error::other)?;
        if u64::from(width) * u64::from(height) > 128_000_000
            || w <= 0
            || h <= 0
            || output.len().saturating_add(bytes.len().saturating_mul(2)) > super::MAX_PART as usize
        {
            return Err(invalid());
        }
        let crop = child(fill, A, "srcRect");
        let [l, t, r, b] = ["l", "t", "r", "b"].map(|key| {
            crop.and_then(|node| node.attribute(key))
                .and_then(|n| n.parse::<i32>().ok())
                .unwrap_or(0)
                .clamp(-100_000, 100_000)
        });
        write!(output, r#"<img data-box="{x} {y} {w} {h}" data-crop="{l} {t} {r} {b}"{transform} src="data:{mime};base64,{}">"#,
            base64::engine::general_purpose::STANDARD.encode(bytes)).unwrap();
        Ok(())
    }

    fn group_text(
        &mut self,
        textbox: Node<'_, '_>,
        body: Option<Node<'_, '_>>,
        width: i32,
        output: &mut String,
    ) -> io::Result<()> {
        let invalid = || super::invalid("Word 그룹의 글상자 형식을 보존할 수 없습니다.");
        let content = super::node(textbox, "txbxContent").ok_or_else(invalid)?;
        if content
            .children()
            .filter(Node::is_element)
            .any(|n| !n.has_tag_name((super::WORD, "p")))
            || content.descendants().any(|n| {
                n.is_element()
                    && matches!(
                        n.tag_name().name(),
                        "drawing" | "pict" | "AlternateContent" | "object"
                    )
            })
            || body.is_some_and(|node| node.attribute("vert").is_some_and(|v| v != "horz"))
        {
            return Err(invalid());
        }
        let align = match body.and_then(|n| n.attribute("anchor")) {
            Some("ctr") => "CENTER",
            Some("b") => "BOTTOM",
            _ => "TOP",
        };
        let [l, t, r, b] = [
            ("lIns", 91440),
            ("tIns", 45720),
            ("rIns", 91440),
            ("bIns", 45720),
        ]
        .map(|(key, default)| {
            body.and_then(|n| n.attribute(key))
                .and_then(|s| s.parse::<i32>().ok())
                .unwrap_or(default)
                .clamp(0, 12_700_000)
                / 127
        });
        self.textbox_fonts = self.textbox_theme();
        self.in_textbox = true;
        let rendered = (|| {
            let mut html = String::new();
            let mut text = Vec::new();
            for p in content.children().filter(Node::is_element) {
                (self.check)()?;
                let paragraph = self.paragraph(p, f64::from((width - l - r).max(1)) / 100.0)?;
                html.push_str(&paragraph.html);
                text.push(paragraph.text);
                if html.len() > 128 * 1024 {
                    return Err(invalid());
                }
            }
            Ok((html, text))
        })();
        self.textbox_fonts = None;
        self.in_textbox = false;
        let (html, text) = rendered?;
        write!(
            output,
            r#" data-word-text="{}" data-word-insets="{l} {t} {r} {b}" data-word-anchor="{align}" data-word-wrap="{}" data-word-text-label="{}""#,
            base64::engine::general_purpose::STANDARD.encode(html),
            if body.is_some_and(|n| n.attribute("wrap") == Some("none")) { "none" } else { "square" },
            crate::safe_html::escape(&text.join("\n")).replace('\n', "&#10;").replace('\r', "&#13;").replace('\t', "&#9;")
        )
        .unwrap();
        Ok(())
    }

    pub(super) fn group(&mut self, drawing: Node<'_, '_>, output: &mut String) -> io::Result<bool> {
        let inline = child(drawing, WP, "inline");
        let group = inline
            .and_then(|node| child(node, A, "graphic"))
            .and_then(|node| child(node, A, "graphicData"))
            .and_then(|node| child(node, WPG, "wgp"));
        let Some(group) = group else { return Ok(false) };
        (self.check)()?;
        let mut html = String::new();
        let dimensions = inline
            .and_then(|node| child(node, WP, "extent"))
            .and_then(|node| pair(node, ["cx", "cy"]));
        if let Some(size) = dimensions {
            let [w, h] = size.map(hwp);
            let transform = child(group, WPG, "grpSpPr")
                .and_then(|p| child(p, A, "xfrm"))
                .and_then(transform_attrs);
            let Some(transform) = transform else {
                return Ok(false);
            };
            write!(html, r#"<span data-word-group="0 0 {w} {h}"{transform}>"#).unwrap();
            let mut error = None;
            if members(self, group, size, 0, &mut 512, &mut html, &mut error).is_some() {
                html.push_str("</span>");
                output.push_str(&html);
                self.pictures += html.matches("<img ").count();
                return Ok(true);
            }
            if let Some(error) = error {
                return Err(error);
            }
        }
        self.warn(
            "일부 Word 그룹 도형은 지원하지 않는 변형·개체가 있어 그룹으로 보존할 수 없습니다.",
        );
        Ok(false)
    }
}
