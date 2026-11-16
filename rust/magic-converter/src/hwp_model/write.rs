use super::*;
use scraper::Node as HtmlNode;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::fs::File;
use std::io::{Read, Write as _};
use zip::{CompressionMethod, ZipArchive, ZipWriter, write::SimpleFileOptions};

const TEMPLATE: &[u8] = include_bytes!("../../../../magic_converter/data/hwpx/Skeleton.hwpx");
const SECTION: &str = "Contents/section0.xml";
const HEADER: &str = "Contents/header.xml";
const CONTENT: &str = "Contents/content.hpf";
const SUBLIST: &str = r#"<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">"#;
const POSITION: &str = r#"<hp:pos treatAsChar="1" affectLSpacing="0" flowWithText="1" allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" horzRelTo="COLUMN" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>"#;
const ZERO_MARGIN: &str = r#"<hp:outMargin left="0" right="0" top="0" bottom="0"/>"#;
const MATRIX: &str = r#"e1="1" e2="0" e3="0" e4="0" e5="1" e6="0""#;

fn word_transform(item: ElementRef<'_>, x: i32, y: i32, w: i32, h: i32) -> String {
    transform(
        numbers(item.attr("data-word-transform"), [0; 3]),
        [x, y, w, h],
        [w, h],
    )
}

fn transform(
    [rotation, horizontal, vertical]: [i32; 3],
    [x, y, w, h]: [i32; 4],
    [ow, oh]: [i32; 2],
) -> String {
    let degrees = f64::from(rotation.rem_euclid(21_600_000)) / 60000.0;
    let (sin, cos) = degrees.to_radians().sin_cos();
    let (cx, cy) = (f64::from(w) / 2.0, f64::from(h) / 2.0);
    let (fx, fy) = (i32::from(horizontal == 1), i32::from(vertical == 1));
    let (sx, sy) = (1 - 2 * fx, 1 - 2 * fy);
    let (dx, dy) = (f64::from(w * fx), f64::from(h * fy));
    let (sx, sy) = (
        f64::from(sx * w) / f64::from(ow),
        f64::from(sy * h) / f64::from(oh),
    );
    let (rx, ry) = (cx - cos * cx + sin * cy, cy - sin * cx - cos * cy);
    let angle = (-(degrees.round() as i32)).rem_euclid(360);
    format!(
        r#"<hp:flip horizontal="{fx}" vertical="{fy}"/><hp:rotationInfo angle="{angle}" centerX="{}" centerY="{}" rotateimage="1"/><hp:renderingInfo><hc:transMatrix e1="1" e2="0" e3="{x}" e4="0" e5="1" e6="{y}"/><hc:scaMatrix e1="{sx}" e2="0" e3="{dx}" e4="0" e5="{sy}" e6="{dy}"/><hc:rotMatrix e1="{cos:.9}" e2="{:.9}" e3="{rx:.9}" e4="{sin:.9}" e5="{cos:.9}" e6="{ry:.9}"/></hp:renderingInfo>"#,
        w / 2,
        h / 2,
        -sin
    )
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct RunStyle {
    font: String,
    size: i32,
    bold: bool,
    italic: bool,
    color: String,
    tracking: i32,
    scale: i32,
    font_space: bool,
}

impl RunStyle {
    fn default() -> Self {
        Self {
            font: "맑은 고딕".into(),
            size: 1000,
            bold: false,
            italic: false,
            color: "000000".into(),
            tracking: 0,
            scale: 100,
            font_space: false,
        }
    }

    fn with(&self, element: ElementRef<'_>) -> Self {
        let mut style = self.clone();
        if let Some(font) = element.attr("data-font") {
            style.font = font.to_owned();
        }
        if let Some(size) = element
            .attr("data-size")
            .and_then(|value| value.parse::<f64>().ok())
        {
            style.size = (size * 100.0).round_ties_even().clamp(100.0, 100_000.0) as i32;
        }
        style.bold |= element.attr("data-bold") == Some("1")
            || matches!(element.value().name(), "b" | "strong");
        style.italic |= element.attr("data-italic") == Some("1")
            || matches!(element.value().name(), "i" | "em");
        if let Some(value) = element.attr("data-color").and_then(color) {
            style.color = value.to_owned();
        }
        if let Some(value) = element
            .attr("data-tracking")
            .and_then(|value| value.parse::<f64>().ok())
        {
            style.tracking = (value * 500.0 / f64::from(style.size))
                .round_ties_even()
                .clamp(-50.0, 50.0) as i32;
        }
        style.scale = numbers(element.attr("data-scale"), [style.scale])[0].clamp(50, 200);
        style.font_space |= element.attr("data-font-space") == Some("1");
        style
    }
}

#[derive(Clone, Eq, Hash, PartialEq)]
struct ParagraphStyle {
    align: &'static str,
    indent: [i32; 3],
    space: [i32; 3],
    rule: &'static str,
}

#[derive(Eq, Hash, PartialEq)]
struct CellStyle {
    fill: String,
    borders: Option<String>,
}

struct Writer<'a> {
    archive: &'a mut ZipWriter<File>,
    check: Check<'a>,
    pictures: HashMap<String, usize>,
    image_bytes: usize,
    manifest: String,
    styles: HashMap<RunStyle, usize>,
    paragraphs: HashMap<ParagraphStyle, usize>,
    fills: HashMap<CellStyle, usize>,
    counter: usize,
    preview: String,
    rendered_textboxes: usize,
}

fn dimension(element: ElementRef<'_>, name: &str, default: f64) -> f64 {
    element
        .attr(name)
        .and_then(|value| value.parse::<f64>().ok())
        .filter(|value| value.is_finite() && *value > 0.0)
        .unwrap_or(default)
}

fn table_size(output: &mut String, width: i32, height: i32) {
    write!(output, r#"<hp:sz width="{width}" widthRelTo="ABSOLUTE" height="{height}" heightRelTo="ABSOLUTE" protect="0"/>"#).unwrap();
}

impl Writer<'_> {
    fn id(&mut self) -> io::Result<usize> {
        self.counter += 1;
        if self.counter > 400_000 {
            return Err(invalid("한글 문서의 개체 한도를 초과했습니다."));
        }
        Ok(self.counter)
    }

    fn text(&mut self, output: &mut String, value: &str, style: &RunStyle) -> io::Result<()> {
        if value
            .chars()
            .any(|ch| ch < ' ' && !matches!(ch, '\t' | '\r' | '\n'))
        {
            return Err(invalid("한글 문서에 저장할 수 없는 제어 문자가 있습니다."));
        }
        let next = self.styles.len() + 7;
        let id = *self.styles.entry(style.clone()).or_insert(next);
        write!(output, r#"<hp:run charPrIDRef="{id}"><hp:t>"#).unwrap();
        output.push_str(
            &escape(value)
                .replace("\r\n", "\n")
                .replace('\r', "\n")
                .replace('\n', "<hp:lineBreak/>")
                .replace('\t', r#"<hp:tab width="0" leader="0" type="0"/>"#),
        );
        output.push_str("</hp:t></hp:run>");
        self.preview.push_str(value);
        Ok(())
    }

    fn image(
        &mut self,
        output: &mut String,
        item: ElementRef<'_>,
        available: i32,
        member: Option<([i32; 4], usize)>,
    ) -> io::Result<()> {
        let (_, payload) = item
            .attr("src")
            .and_then(|value| value.split_once(";base64,"))
            .filter(|(mime, _)| mime.starts_with("data:image/"))
            .ok_or_else(|| invalid("문서에는 포함 그림만 사용할 수 있습니다."))?;
        let mut bytes = STANDARD.decode(payload).map_err(io::Error::other)?;
        (self.check)()?;
        let image = image::ImageReader::new(Cursor::new(&bytes)).with_guessed_format()?;
        let format = image
            .format()
            .ok_or_else(|| invalid("한글 문서의 그림 형식을 읽을 수 없습니다."))?;
        let (w, h) = image.into_dimensions().map_err(io::Error::other)?;
        if u64::from(w) * u64::from(h) > 128_000_000 || w == 0 || h == 0 {
            return Err(invalid("한글 문서의 그림 크기가 허용 범위를 벗어납니다."));
        }
        let extension = match format {
            image::ImageFormat::Jpeg => "jpg",
            image::ImageFormat::Png => "png",
            image::ImageFormat::Gif => "gif",
            image::ImageFormat::Bmp => "bmp",
            image::ImageFormat::Tiff => "tiff",
            _ => {
                let decoded = image::load_from_memory(&bytes).map_err(io::Error::other)?;
                let mut png = Cursor::new(Vec::new());
                decoded
                    .write_to(&mut png, image::ImageFormat::Png)
                    .map_err(io::Error::other)?;
                bytes = png.into_inner();
                "png"
            }
        };
        let hash = format!("{:x}", Sha256::digest(&bytes));
        let next = self.pictures.len() + 1;
        let id = *self.pictures.entry(hash).or_insert(next);
        if id == next {
            self.image_bytes += bytes.len();
            if self.image_bytes > 128 * 1024 * 1024 {
                return Err(invalid("한글 문서의 포함 그림 크기 한도를 초과했습니다."));
            }
            let part = format!("BinData/image{id}.{extension}");
            self.archive
                .start_file(&part, SimpleFileOptions::default())?;
            self.archive.write_all(&bytes)?;
            let mime = if extension == "jpg" {
                "jpeg"
            } else {
                extension
            };
            write!(self.manifest, r#"<opf:item id="image{id}" href="{part}" media-type="image/{mime}" isEmbeded="1"/>"#).unwrap();
        }
        let width = dimension(item, "data-width", f64::from(w) * 0.75) * 100.0;
        let height = dimension(item, "data-height", f64::from(h) * 0.75) * 100.0;
        let scale = (f64::from(available) / width.max(1.0)).min(1.0);
        let width = (width * scale).round_ties_even().clamp(1.0, 500_000.0) as i32;
        let height = (height * scale).round_ties_even().clamp(1.0, 500_000.0) as i32;
        let (ow, oh) = member.map_or(
            (i64::from(w) * 75, i64::from(h) * 75),
            |([_, _, width, height], _)| (i64::from(width), i64::from(height)),
        );
        let [l, t, r, b] = numbers(item.attr("data-crop"), [0; 4]);
        let trim = |ratio: i32, size: i64| {
            ((f64::from(ratio.clamp(-100_000, 100_000)) * size as f64) / 100_000.0)
                .round_ties_even() as i64
        };
        let (l, t, r, b) = (trim(l, ow), trim(t, oh), ow - trim(r, ow), oh - trim(b, oh));
        if l >= r || t >= b {
            return Err(invalid("한글 그림의 자르기 영역이 올바르지 않습니다."));
        }
        let ([x, y, width, height], depth) = member.unwrap_or(([0, 0, width, height], 0));
        let object = self.id()? + 500_000;
        if member.is_none() {
            output.push_str(r#"<hp:run charPrIDRef="0">"#);
        }
        let group = member.map_or_else(String::new, |_| {
            format!(r#" groupLevel="{depth}" instid="{object}""#)
        });
        let transform = if member.is_some() {
            word_transform(item, x, y, width, height)
        } else {
            format!(
                r#"<hp:flip horizontal="0" vertical="0"/><hp:rotationInfo angle="0" centerX="{}" centerY="{}" rotateimage="1"/><hp:renderingInfo><hc:transMatrix {MATRIX}/><hc:scaMatrix {MATRIX}/><hc:rotMatrix {MATRIX}/></hp:renderingInfo>"#,
                width / 2,
                height / 2
            )
        };
        write!(output, r#"<hp:pic id="{object}" zOrder="0" numberingType="PICTURE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" reverse="0"{group}><hp:offset x="{}" y="{}"/><hp:orgSz width="{ow}" height="{oh}"/><hp:curSz width="{width}" height="{height}"/>{transform}<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{ow}" y="0"/><hc:pt2 x="{ow}" y="{oh}"/><hc:pt3 x="0" y="{oh}"/></hp:imgRect><hp:imgClip left="{l}" top="{t}" right="{r}" bottom="{b}"/><hp:inMargin left="0" right="0" top="0" bottom="0"/><hp:imgDim dimwidth="{ow}" dimheight="{oh}"/><hc:img binaryItemIDRef="image{id}" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/><hp:effects/>"#,x as u32,y as u32).unwrap();
        if member.is_none() {
            table_size(output, width, height);
            write!(output, "{POSITION}{ZERO_MARGIN}<hp:shapeComment>image{id}</hp:shapeComment></hp:pic></hp:run>").unwrap();
        } else {
            output.push_str("</hp:pic>");
        }
        Ok(())
    }

    fn inline(
        &mut self,
        output: &mut String,
        node: ego_tree::NodeRef<'_, HtmlNode>,
        style: &RunStyle,
        available: i32,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 64 {
            return Err(invalid("문서 개체의 중첩 한도를 초과했습니다."));
        }
        if let HtmlNode::Text(text) = node.value() {
            return self.text(output, &text.text, style);
        }
        let Some(element) = ElementRef::wrap(node) else {
            return Ok(());
        };
        match element.value().name() {
            "img" => return self.image(output, element, available, None),
            "br" => return self.text(output, "\n", style),
            _ => {}
        }
        if let Some(group) = element.attr("data-word-group") {
            let [_, _, width, _] = numbers(Some(group), [0; 4]);
            if !(1..=800_000).contains(&width) {
                return Err(invalid("Word 그룹 너비가 올바르지 않습니다."));
            }
            let scale = (f64::from(available) / f64::from(width)).min(1.0);
            output.push_str(r#"<hp:run charPrIDRef="0">"#);
            self.word_shape(output, element, scale, 0, [0; 2], false)?;
            output.push_str("</hp:run>");
            return Ok(());
        }
        if let Some(line) = element.attr("data-inline-line") {
            let values = numbers(Some(line), [0; 6]);
            let [w, h, left, right, y, stroke] = values;
            if !(1..=800_000).contains(&w)
                || !(1..=800_000).contains(&h)
                || left < 0
                || right <= left
                || right > w
                || !(0..=h).contains(&y)
                || !(1..=h * 2).contains(&stroke)
            {
                return Err(invalid("한글 문서의 선 좌표가 올바르지 않습니다."));
            }
            let scale = (f64::from(available) / f64::from(w)).min(1.0);
            let [w, h, left, right, y, stroke] =
                values.map(|v| (f64::from(v) * scale).round_ties_even() as i32);
            let (w, h, stroke) = (w.max(1), h.max(1), stroke.max(1));
            let color = element
                .attr("data-color")
                .and_then(color)
                .unwrap_or("000000");
            let id = self.id()? + 2_500_000;
            write!(output, r##"<hp:run charPrIDRef="0"><hp:line id="{id}" instid="{id}" zOrder="0" numberingType="NONE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" href="" groupLevel="0" isReverseHV="0"><hp:offset x="0" y="0"/><hp:orgSz width="{w}" height="{h}"/><hp:curSz width="{w}" height="{h}"/><hp:flip horizontal="0" vertical="0"/><hp:rotationInfo angle="0" centerX="{}" centerY="{}" rotateimage="1"/><hp:renderingInfo><hc:transMatrix {MATRIX}/><hc:scaMatrix {MATRIX}/><hc:rotMatrix {MATRIX}/></hp:renderingInfo><hp:lineShape color="#{color}" width="{stroke}" style="SOLID" endCap="FLAT" headStyle="NONE" tailStyle="NONE" headfill="0" tailfill="0" headSz="SMALL_SMALL" tailSz="SMALL_SMALL" outlineStyle="NORMAL" alpha="0"/><hc:startPt x="{left}" y="{y}"/><hc:endPt x="{right}" y="{y}"/>"##, w/2,h/2).unwrap();
            table_size(output, w, h);
            write!(
                output,
                "{POSITION}{ZERO_MARGIN}<hp:shapeComment>Line</hp:shapeComment></hp:line></hp:run>"
            )
            .unwrap();
            return Ok(());
        }
        if element.attr("data-page-number") == Some("1") {
            output.push_str(r#"<hp:run charPrIDRef="0"><hp:ctrl><hp:autoNum num="1" numType="PAGE"><hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar="" supscript="0"/></hp:autoNum></hp:ctrl></hp:run>"#);
            return Ok(());
        }
        if element.attr("data-equation") == Some("1") {
            let script = escape(&element.text().collect::<String>());
            let id = self.id()? + 1_000_000;
            let width =
                (script.chars().count().saturating_mul(550)).clamp(1000, available as usize);
            write!(output, r##"<hp:run charPrIDRef="0"><hp:equation id="{id}" zOrder="0" numberingType="EQUATION" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" version="Equation Version 60" baseLine="85" textColor="#000000" baseUnit="1000" lineMode="CHAR" font="HYhwpEQ"><hp:sz width="{width}" widthRelTo="ABSOLUTE" height="1600" heightRelTo="ABSOLUTE" protect="0"/>{POSITION}{ZERO_MARGIN}<hp:shapeComment>수식</hp:shapeComment><hp:script>{script}</hp:script></hp:equation></hp:run>"##).unwrap();
            return Ok(());
        }
        let style = style.with(element);
        for child in element.children() {
            self.inline(output, child, &style, available, depth + 1)?;
        }
        Ok(())
    }

    fn word_shape(
        &mut self,
        output: &mut String,
        item: ElementRef<'_>,
        scale: f64,
        depth: usize,
        origin: [i32; 2],
        rotated_group: bool,
    ) -> io::Result<()> {
        if depth >= 16 {
            return Err(invalid("Word 그룹의 중첩 한도를 초과했습니다."));
        }
        (self.check)()?;
        let group = item.attr("data-word-group");
        let kind = match (group, item.attr("data-word-shape")) {
            (Some(_), _) => "container",
            (None, _) if item.value().name() == "img" => "pic",
            (None, Some(kind @ ("rect" | "ellipse" | "line" | "polygon"))) => kind,
            _ => return Err(invalid("Word 그룹에 지원하지 않는 개체가 있습니다.")),
        };
        let [x, y, w, h] = numbers(group.or_else(|| item.attr("data-box")), [0; 4]);
        if x.unsigned_abs() > 800_000
            || y.unsigned_abs() > 800_000
            || !(0..=800_000).contains(&w)
            || !(0..=800_000).contains(&h)
        {
            return Err(invalid("Word 그룹의 개체 좌표가 올바르지 않습니다."));
        }
        let scaled = |value| (f64::from(value) * scale).round_ties_even() as i32;
        // HWPX group members use offsets in the outer group's coordinate space,
        // including members of nested containers.
        let x = origin[0] + scaled(x);
        let y = origin[1] + scaled(y);
        let (w, h) = (scaled(w).max(1), scaled(h).max(1));
        let [angle, flip_h, flip_v] = numbers(item.attr("data-word-transform"), [0; 3]);
        let transformed = angle != 0 || flip_h != 0 || flip_v != 0;
        if kind == "pic" {
            return self.image(output, item, w, Some(([x, y, w, h], depth)));
        }
        let id = self.id()? + 3_000_000;
        let attributes = match kind {
            "rect" => r#" ratio="0""#,
            "line" => r#" isReverseHV="0""#,
            _ => "",
        };
        let transform = word_transform(item, x, y, w, h);
        write!(output, r#"<hp:{kind} id="{id}" instid="{id}" zOrder="{id}" numberingType="PICTURE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" href="" groupLevel="{depth}"{attributes}><hp:offset x="{}" y="{}"/><hp:orgSz width="{w}" height="{h}"/><hp:curSz width="{w}" height="{h}"/>{transform}"#,x as u32,y as u32).unwrap();
        if group.is_some() {
            for member in item.children().filter_map(ElementRef::wrap) {
                self.word_shape(
                    output,
                    member,
                    scale,
                    depth + 1,
                    [x, y],
                    rotated_group || transformed,
                )?;
            }
        } else {
            let stroke = item.attr("data-stroke").and_then(color);
            let width = item
                .attr("data-width")
                .and_then(|value| value.parse::<i32>().ok())
                .unwrap_or(0)
                .clamp(0, 20_000);
            let (stroke, style, width) = match stroke {
                Some(stroke) => (stroke, "SOLID", scaled(width).max(1)),
                None => ("000000", "NONE", 0),
            };
            write!(output, r##"<hp:lineShape color="#{stroke}" width="{width}" style="{style}" endCap="FLAT" headStyle="NONE" tailStyle="NONE" headfill="0" tailfill="0" headSz="SMALL_SMALL" tailSz="SMALL_SMALL" outlineStyle="NORMAL" alpha="0"/>"##).unwrap();
            if let Some(fill) = item.attr("data-fill").and_then(color) {
                write!(output, r##"<hc:fillBrush><hc:winBrush faceColor="#{fill}" hatchColor="#000000" alpha="0"/></hc:fillBrush>"##).unwrap();
            }
            if let Some(payload) = item.attr("data-word-text")
                && !transformed
                && !rotated_group
            {
                let bytes = STANDARD.decode(payload).map_err(io::Error::other)?;
                let html = std::str::from_utf8(&bytes).map_err(io::Error::other)?;
                let document = scraper::Html::parse_fragment(html);
                let [l, t, r, b] = numbers(item.attr("data-word-insets"), [0; 4]).map(scaled);
                let anchor = match item.attr("data-word-anchor") {
                    Some("CENTER") => "CENTER",
                    Some("BOTTOM") => "BOTTOM",
                    _ => "TOP",
                };
                let available = (w - l - r).max(1);
                write!(output,r#"<hp:drawText lastWidth="{available}" name="" editable="1"><hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="{anchor}" linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" hasTextRef="0" hasNumRef="0">"#).unwrap();
                self.blocks(output, document.root_element(), available, 0)?;
                write!(output,r#"</hp:subList><hp:textMargin left="{l}" right="{r}" top="{t}" bottom="{b}"/></hp:drawText>"#).unwrap();
            }
            let mut point = |name: &str, x, y| {
                write!(output, r#"<hc:{name} x="{x}" y="{y}"/>"#).unwrap();
            };
            match kind {
                "rect" => {
                    for (name, x, y) in [("pt0", 0, 0), ("pt1", w, 0), ("pt2", w, h), ("pt3", 0, h)]
                    {
                        point(name, x, y);
                    }
                }
                "ellipse" => {
                    for (name, x, y) in [
                        ("center", w / 2, h / 2),
                        ("ax1", w, h / 2),
                        ("ax2", w / 2, h),
                        ("start1", 0, 0),
                        ("end1", 0, 0),
                        ("start2", 0, 0),
                        ("end2", 0, 0),
                    ] {
                        point(name, x, y);
                    }
                }
                _ => {
                    let vertices = values(item.attr("data-points"));
                    if !(4..=512).contains(&vertices.len())
                        || !vertices.len().is_multiple_of(2)
                        || vertices.iter().any(|&v| !(0..=800_000).contains(&v))
                        || (kind == "line" && vertices.len() != 4)
                        || (kind == "polygon" && vertices.len() < 6)
                    {
                        return Err(invalid("Word 그룹의 꼭짓점이 올바르지 않습니다."));
                    }
                    for (index, vertex) in vertices.chunks_exact(2).enumerate() {
                        let name = if kind == "polygon" {
                            "pt"
                        } else if index == 0 {
                            "startPt"
                        } else {
                            "endPt"
                        };
                        point(name, scaled(vertex[0]), scaled(vertex[1]));
                    }
                }
            }
        }
        if depth == 0 {
            table_size(output, w, h);
            output.push_str(POSITION);
            output.push_str(ZERO_MARGIN);
        }
        write!(output, "</hp:{kind}>").unwrap();
        if (transformed || rotated_group) && item.attr("data-word-text").is_some() {
            self.rotated_text(output, item, [x, y, w, h], depth, scale)?;
        }
        Ok(())
    }

    fn rotated_text(
        &mut self,
        output: &mut String,
        item: ElementRef<'_>,
        [x, y, w, h]: [i32; 4],
        depth: usize,
        scale: f64,
    ) -> io::Result<()> {
        let [l, t, r, b] = numbers(item.attr("data-word-insets"), [0; 4]);
        let [width, height] = [
            f64::from(w) / scale - f64::from(l + r),
            f64::from(h) / scale - f64::from(t + b),
        ];
        if width < 10.0 || height < 10.0 {
            return Ok(());
        }
        let data = STANDARD
            .decode(item.attr("data-word-text").unwrap_or_default())
            .map_err(io::Error::other)?;
        let html = std::str::from_utf8(&data).map_err(io::Error::other)?;
        let label = item.attr("data-word-text-label").unwrap_or_default();
        if label.trim().is_empty() {
            return Ok(());
        }
        let engine = crate::qt_render::engine()
            .ok_or_else(|| invalid("회전 글상자의 Qt 렌더러를 찾을 수 없습니다."))?;
        let directory = tempfile::Builder::new()
            .prefix("magic-word-textbox-")
            .tempdir()?;
        let layout = directory.path().join("layout.json");
        let anchor = item.attr("data-word-anchor").unwrap_or("TOP");
        let wrap = item.attr("data-word-wrap").unwrap_or("square");
        let args = [
            (width / 100.0).to_string(),
            (height / 100.0).to_string(),
            anchor.to_owned(),
            wrap.to_owned(),
        ]
        .map(Into::into);
        crate::qt_render::render(&engine, "textbox-layout", html, &layout, &args, self.check)?;
        type Run = (String, String, String, bool, bool, f64, f64, f64, f64, f64);
        let (left, top, ink_width, ink_height, lines, runs): (
            f64,
            f64,
            f64,
            f64,
            Vec<String>,
            Vec<Run>,
        ) = serde_json::from_slice(&std::fs::read(layout)?).map_err(io::Error::other)?;
        let text = lines.join("\n");
        if text.trim().is_empty() {
            return Ok(());
        }
        let [angle, flip_h, flip_v] = numbers(item.attr("data-word-transform"), [0; 3]);
        let degrees = f64::from(angle.rem_euclid(21_600_000)) / 60000.0;
        let (sin, cos) = degrees.to_radians().sin_cos();
        let insets = [l, t].map(|value| f64::from(value) * scale);
        let center = [f64::from(w) / 2.0, f64::from(h) / 2.0];
        let rotate = |[dx, dy]: [f64; 2]| {
            let dx = (dx - center[0]) * if flip_h != 0 { -1.0 } else { 1.0 };
            let dy = (dy - center[1]) * if flip_v != 0 { -1.0 } else { 1.0 };
            [
                center[0] + cos * dx - sin * dy,
                center[1] + sin * dx + cos * dy,
            ]
        };
        let style = |run: &Run| (run.1.clone(), run.2.clone(), run.3, run.4, run.9);
        let mixed = runs
            .first()
            .is_some_and(|first| runs.iter().skip(1).any(|run| style(first) != style(run)));
        let fallback = (
            "Malgun Gothic".to_owned(),
            "#000000".to_owned(),
            false,
            false,
            10.0,
        );
        let runs = if mixed {
            runs
        } else {
            let (font, color, bold, italic, points) = runs.first().map_or(fallback, style);
            vec![(
                text, font, color, bold, italic, left, top, ink_width, ink_height, points,
            )]
        };
        let [pdf_x, pdf_y] = rotate([
            insets[0] + width * scale / 2.0,
            insets[1] + height * scale / 2.0,
        ]);
        self.preview.push_str(label);
        self.rendered_textboxes += 1;
        for (text, font, rgb, bold, italic, left, top, ink_width, ink_height, points) in runs {
            let [aw, ah] = [ink_width.min(width / 100.0), ink_height.min(height / 100.0)]
                .map(|v| (v * 100.0 * scale).round().max(100.0) as i32);
            let [dx, dy] = rotate([
                left * 100.0 * scale + insets[0] + f64::from(aw) / 2.0,
                top * 100.0 * scale + insets[1] + f64::from(ah) / 2.0,
            ]);
            let ax = x + (dx - f64::from(aw) / 2.0).round() as i32;
            let ay = y + (dy - f64::from(ah) / 2.0).round() as i32;
            let mut metadata = crate::hwpx_graphics::Textbox {
                html: html.to_owned(),
                text: text.clone(),
                dimensions: [width / 100.0, height / 100.0],
                anchor: anchor.to_owned(),
                wrap: wrap.to_owned(),
                angle: degrees,
                flip: [flip_h != 0, flip_v != 0],
                size: [
                    (width * scale).round() as i32,
                    (height * scale).round() as i32,
                ],
                center_from_art: [x - ax + pdf_x.round() as i32, y - ay + pdf_y.round() as i32],
                fit_run: mixed,
                style: None,
            };
            if mixed {
                metadata.size = [aw, ah];
                metadata.dimensions = [f64::from(aw), f64::from(ah)].map(|v| v / 100.0);
                metadata.center_from_art = [aw / 2, ah / 2];
                metadata.html = crate::hwpx_graphics::styled_text(
                    &text, &font, &rgb, bold, italic, points, "left",
                );
            }
            let color = color(&rgb).unwrap_or("000000");
            let weight = match (bold, italic) {
                (true, true) => "굵게 기울임",
                (true, false) => "굵게",
                (false, true) => "기울임",
                _ => "보통",
            };
            if mixed {
                metadata.style = Some(crate::hwpx_graphics::TextStyle {
                    font: font.clone(),
                    color: rgb.clone(),
                    weight: weight.to_owned(),
                    points,
                });
            }
            let font = escape(&font);
            const BASE: i32 = 14173;
            let transform = transform([angle, flip_h, flip_v], [ax, ay, aw, ah], [BASE, BASE]);
            let id = self.id()? + 3_000_000;
            let metadata =
                STANDARD.encode(serde_json::to_vec(&metadata).map_err(io::Error::other)?);
            let value = escape(&text).replace('\n', "&#10;").replace('\t', "&#9;");
            write!(output, r##"<hp:textart id="{id}" instid="{id}" zOrder="{id}" numberingType="NONE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" href="" groupLevel="{depth}" text="{value}"><hp:offset x="{}" y="{}"/><hp:orgSz width="{BASE}" height="{BASE}"/><hp:curSz width="{aw}" height="{ah}"/>{transform}<hp:lineShape color="#000000" width="0" style="NONE" endCap="ROUND" headStyle="NORMAL" tailStyle="NORMAL" headfill="0" tailfill="0" headSz="SMALL_SMALL" tailSz="SMALL_SMALL" outlineStyle="INNER" alpha="0"/><hc:fillBrush><hc:winBrush faceColor="#{color}" hatchColor="#000000" alpha="0"/></hc:fillBrush><hp:shadow type="NONE" color="#000000" offsetX="0" offsetY="0" alpha="0"/><hc:pt0 x="0" y="0"/><hc:pt1 x="{BASE}" y="0"/><hc:pt2 x="{BASE}" y="{BASE}"/><hc:pt3 x="0" y="{BASE}"/><hp:textartPr fontName="{font}" fontStyle="{weight}" fontType="TTF" textShape="RECTANGLE" lineSpacing="120" charSpacing="100" align="CENTER"><hp:shadow type="NONE" color="#000000" offsetX="0" offsetY="0" alpha="0"/></hp:textartPr><hp:shapeComment>{}{metadata}</hp:shapeComment></hp:textart>"##,ax as u32,ay as u32,crate::hwpx_graphics::TEXTBOX).unwrap();
        }
        Ok(())
    }

    fn paragraph(&mut self, output: &mut String, item: Option<ElementRef<'_>>) -> io::Result<()> {
        let attr = |name| item.and_then(|element| element.attr(name));
        let align = match attr("align") {
            Some("left") => "LEFT",
            Some("center") => "CENTER",
            Some("right") => "RIGHT",
            Some("justify") => "JUSTIFY",
            _ => "JUSTIFY",
        };
        let style = ParagraphStyle {
            align,
            indent: numbers(attr("data-indent"), [0; 3])
                .map(|value| value.clamp(-100_000, 100_000)),
            space: numbers(attr("data-space"), [0; 3]).map(|value| value.clamp(0, 100_000)),
            rule: match attr("data-rule") {
                Some("exact") => "FIXED",
                Some("atLeast") => "AT_LEAST",
                _ => "PERCENT",
            },
        };
        let next = self.paragraphs.len() + 20;
        let style = *self.paragraphs.entry(style).or_insert(next);
        let page = u8::from(attr("data-page") == Some("1"));
        let id = self.id()?;
        write!(output, r#"<hp:p id="{id}" paraPrIDRef="{style}" styleIDRef="0" pageBreak="{page}" columnBreak="0" merged="0">"#).unwrap();
        Ok(())
    }

    fn blocks(
        &mut self,
        output: &mut String,
        parent: ElementRef<'_>,
        available: i32,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 32 {
            return Err(invalid("한글 문서 표의 중첩이 너무 깊습니다."));
        }
        let start = output.len();
        for child in parent.child_elements() {
            (self.check)()?;
            match child.value().name() {
                "header" | "footer" => {}
                "table" => self.table(output, child, available, depth + 1)?,
                "p" | "h1" | "h2" | "h3" | "img" => {
                    self.paragraph(output, Some(child))?;
                    let start = output.len();
                    self.inline(output, *child, &RunStyle::default(), available, 0)?;
                    if start == output.len() {
                        self.text(output, "", &RunStyle::default().with(child))?;
                    }
                    output.push_str("</hp:p>");
                    self.preview.push('\n');
                }
                _ => self.blocks(output, child, available, depth + 1)?,
            }
            if output.len() > 16 * 1024 * 1024 {
                return Err(invalid("한글 문서 본문 XML의 크기 한도를 초과했습니다."));
            }
        }
        if output.len() == start {
            self.paragraph(output, None)?;
            self.text(output, "", &RunStyle::default())?;
            output.push_str("</hp:p>");
        }
        Ok(())
    }

    fn table(
        &mut self,
        output: &mut String,
        table: ElementRef<'_>,
        available: i32,
        depth: usize,
    ) -> io::Result<()> {
        let grid = Grid::read(table)?;
        if grid.columns == 0 {
            return Ok(());
        }
        let widths = values(table.attr("data-columns"));
        let mut widths: Vec<_> = if widths.len() == grid.columns {
            widths
                .into_iter()
                .map(|w| w.clamp(1, 100_000) * 5)
                .collect()
        } else {
            vec![(available / grid.columns as i32).max(1); grid.columns]
        };
        let raw_width = widths.iter().map(|width| i64::from(*width)).sum::<i64>();
        let scale = (f64::from(available) / raw_width as f64).min(1.0);
        widths.iter_mut().for_each(|width| {
            *width = (f64::from(*width) * scale).round_ties_even().max(1.0) as i32
        });
        let heights: Vec<_> = grid
            .heights
            .iter()
            .map(|height| {
                if *height > 0 {
                    (*height).min(100_000) * 5
                } else {
                    1800
                }
            })
            .collect();
        let sum_heights = |range: std::ops::Range<usize>| {
            heights[range]
                .iter()
                .copied()
                .fold(0_i32, i32::saturating_add)
        };
        self.paragraph(output, Some(table))?;
        self.text(output, "", &RunStyle::default())?;
        let id = self.id()? + 1_500_000;
        write!(output, r#"<hp:run charPrIDRef="0"><hp:tbl id="{id}" zOrder="0" numberingType="TABLE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" pageBreak="CELL" repeatHeader="1" rowCnt="{}" colCnt="{}" cellSpacing="0" borderFillIDRef="3" noAdjust="0">"#, grid.rows, grid.columns).unwrap();
        table_size(output, widths.iter().sum(), sum_heights(0..grid.rows));
        write!(output, r#"{POSITION}<hp:outMargin left="0" right="0" top="283" bottom="283"/><hp:inMargin left="283" right="283" top="141" bottom="141"/>"#).unwrap();
        let mut active = vec![0; grid.columns];
        for r in 0..grid.rows {
            (self.check)()?;
            output.push_str("<hp:tr>");
            let mut c = 0;
            while c < grid.columns {
                if active[c] > r {
                    c += 1;
                    continue;
                }
                let (item, rs, cs) = grid.cells.get(&(r, c)).copied().unwrap_or((None, 1, 1));
                let width = widths[c..c + cs].iter().sum::<i32>();
                let height = sum_heights(r..r + rs);
                active[c..c + cs].fill(r + rs);
                let border = item
                    .map(|item| {
                        let fill = item.attr("bgcolor").and_then(color).unwrap_or_default();
                        let borders = item.attr("data-borders").map(str::to_owned);
                        if fill.is_empty() && borders.is_none() {
                            return 3;
                        }
                        let next = self.fills.len() + 4;
                        *self
                            .fills
                            .entry(CellStyle {
                                fill: fill.to_owned(),
                                borders,
                            })
                            .or_insert(next)
                    })
                    .unwrap_or(3);
                let explicit = item.and_then(|cell| cell.attr("data-margins"));
                let [top, left, bottom, right] = explicit.map_or([141, 283, 141, 283], |value| {
                    numbers(Some(value), [0; 4]).map(|value| value.clamp(0, 100_000) * 5)
                });
                let alignment = match item.and_then(|cell| cell.attr("data-valign")) {
                    Some("top") => "TOP",
                    Some("bottom") => "BOTTOM",
                    _ => "CENTER",
                };
                let list = SUBLIST.replace(
                    r#"vertAlign="CENTER""#,
                    &format!(r#"vertAlign="{alignment}""#),
                );
                write!(output, r#"<hp:tc name="" header="0" hasMargin="{}" protect="0" editable="0" dirty="0" borderFillIDRef="{border}">{list}"#, u8::from(explicit.is_some())).unwrap();
                if let Some(cell) = item {
                    self.blocks(output, cell, (width - left - right).max(100), depth)?;
                } else {
                    self.paragraph(output, None)?;
                    output.push_str(r#"<hp:run charPrIDRef="0"><hp:t/></hp:run></hp:p>"#);
                }
                write!(output, r#"</hp:subList><hp:cellAddr colAddr="{c}" rowAddr="{r}"/><hp:cellSpan colSpan="{cs}" rowSpan="{rs}"/><hp:cellSz width="{width}" height="{height}"/><hp:cellMargin left="{left}" right="{right}" top="{top}" bottom="{bottom}"/></hp:tc>"#).unwrap();
                c += cs;
            }
            output.push_str("</hp:tr>");
        }
        output.push_str("</hp:tbl></hp:run></hp:p>");
        Ok(())
    }
}

fn append_items(xml: &mut String, group: &str, start: usize, count: usize, elements: &str) {
    let opening = format!(r#"<hh:{group} itemCnt="{start}">"#);
    *xml = xml.replace(
        &opening,
        &format!(r#"<hh:{group} itemCnt="{}">"#, start + count),
    );
    *xml = xml.replace(
        &format!("</hh:{group}>"),
        &format!("{elements}</hh:{group}>"),
    );
}

fn metrics(value: impl std::fmt::Display) -> String {
    format!(
        r#"hangul="{value}" latin="{value}" hanja="{value}" japanese="{value}" other="{value}" symbol="{value}" user="{value}""#
    )
}

fn header(
    mut seed: String,
    styles: HashMap<RunStyle, usize>,
    paragraphs: HashMap<ParagraphStyle, usize>,
    fills: HashMap<CellStyle, usize>,
) -> String {
    let mut fonts = BTreeMap::new();
    let mut chars = String::new();
    let mut styles: Vec<_> = styles.into_iter().collect();
    styles.sort_by_key(|(_, id)| *id);
    for (style, id) in &styles {
        let next = fonts.len() + 2;
        let font = *fonts.entry(style.font.as_str()).or_insert(next);
        let bold = if style.bold { "<hh:bold/>" } else { "" };
        let italic = if style.italic { "<hh:italic/>" } else { "" };
        write!(chars, r##"<hh:charPr id="{id}" height="{}" textColor="#{}" shadeColor="none" useFontSpace="{}" useKerning="0" symMark="NONE" borderFillIDRef="2"><hh:fontRef {}/><hh:ratio {}/><hh:spacing {}/><hh:relSz {}/><hh:offset {}/>{italic}{bold}<hh:underline type="NONE" shape="SOLID" color="#000000"/><hh:strikeout shape="NONE" color="#000000"/><hh:outline type="NONE"/><hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/></hh:charPr>"##, style.size, style.color, u8::from(style.font_space), metrics(font), metrics(style.scale), metrics(style.tracking), metrics(100), metrics(0)).unwrap();
    }
    let mut faces = String::new();
    let mut faces_by_id: Vec<_> = fonts.iter().collect();
    faces_by_id.sort_by_key(|(_, id)| **id);
    for (name, id) in faces_by_id {
        write!(faces, r#"<hh:font id="{id}" face="{}" type="TTF" isEmbedded="0"><hh:typeInfo familyType="FCAT_GOTHIC" weight="6" proportion="4" contrast="0" strokeVariation="1" armStyle="1" letterform="1" midline="1" xHeight="1"/></hh:font>"#, escape(name)).unwrap();
    }
    seed = seed.replace("fontCnt=\"2\"", &format!("fontCnt=\"{}\"", fonts.len() + 2));
    seed = seed.replace("</hh:fontface>", &format!("{faces}</hh:fontface>"));
    append_items(&mut seed, "charProperties", 7, styles.len(), &chars);
    let mut paras = String::new();
    let mut paragraphs: Vec<_> = paragraphs.into_iter().collect();
    paragraphs.sort_by_key(|(_, id)| *id);
    for (style, id) in &paragraphs {
        let [left, right, indent] = style.indent.map(|value| value * 5);
        let [before, after, line] = style.space;
        let (rule, value) = match (line, style.rule) {
            (0, _) => ("PERCENT", 160),
            (_, "PERCENT") => ("PERCENT", (f64::from(line) / 2.4).round_ties_even() as i32),
            (_, rule) => (rule, line * 5),
        };
        let (prev, next) = (before * 5, after * 5);
        let margin = format!(
            r#"<hh:margin><hc:intent value="{indent}" unit="HWPUNIT"/><hc:left value="{left}" unit="HWPUNIT"/><hc:right value="{right}" unit="HWPUNIT"/><hc:prev value="{prev}" unit="HWPUNIT"/><hc:next value="{next}" unit="HWPUNIT"/></hh:margin><hh:lineSpacing type="{rule}" value="{value}" unit="HWPUNIT"/>"#
        );
        write!(paras, r#"<hh:paraPr id="{id}" tabPrIDRef="0" condense="0" fontLineHeight="0" snapToGrid="1" suppressLineNumbers="0" checked="0" textDir="LTR"><hh:align horizontal="{}" vertical="BASELINE"/><hh:heading type="NONE" idRef="0" level="0"/><hh:breakSetting breakLatinWord="KEEP_WORD" breakNonLatinWord="BREAK_WORD" widowOrphan="0" keepWithNext="0" keepLines="0" pageBreakBefore="0" lineWrap="BREAK"/><hh:autoSpacing eAsianEng="0" eAsianNum="0"/><hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">{margin}</hp:case><hp:default>{margin}</hp:default></hp:switch><hh:border borderFillIDRef="2" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" connect="0" ignoreMargin="0"/></hh:paraPr>"#, style.align).unwrap();
    }
    append_items(&mut seed, "paraProperties", 20, paragraphs.len(), &paras);
    let mut borders = String::new();
    let mut fill_styles: Vec<_> = fills.iter().map(|(style, id)| (Some(style), *id)).collect();
    fill_styles.sort_by_key(|(_, id)| *id);
    for (style, id) in std::iter::once((None, 3)).chain(fill_styles) {
        write!(borders, r#"<hh:borderFill id="{id}" threeD="0" shadow="0" centerLine="NONE" breakCellSeparateLine="0"><hh:slash type="NONE" Crooked="0" isCounter="0"/><hh:backSlash type="NONE" Crooked="0" isCounter="0"/>"#).unwrap();
        let values = style
            .and_then(|style| style.borders.as_deref())
            .map(|value| value.split_whitespace().collect::<Vec<_>>());
        for (edge, index) in [
            ("leftBorder", 3),
            ("rightBorder", 9),
            ("topBorder", 0),
            ("bottomBorder", 6),
            ("diagonal", 12),
        ] {
            let (kind, width, color) = if edge == "diagonal" {
                ("SOLID", 0.1, "000000")
            } else {
                let fields = values
                    .as_ref()
                    .and_then(|values| values.get(index..index + 3));
                fields.map_or(("SOLID", 0.12, "000000"), |fields| {
                    let kind = match fields[0] {
                        "nil" | "none" => "NONE",
                        "dotted" => "DOT",
                        "dashed" => "DASH",
                        "dashLong" => "LONG_DASH",
                        "dotDash" => "DASH_DOT",
                        "dotDotDash" => "DASH_DOT_DOT",
                        "double" => "DOUBLE_SLIM",
                        "thinThickMediumGap" => "SLIM_THICK",
                        "thickThinMediumGap" => "THICK_SLIM",
                        "thinThickThinMediumGap" => "SLIM_THICK_SLIM",
                        "wave" => "WAVE",
                        "doubleWave" => "DOUBLEWAVE",
                        _ => "SOLID",
                    };
                    let mm = fields[1].parse::<f64>().unwrap_or(4.0) * 25.4 / 576.0;
                    let width = [
                        0.1_f64, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 2.0,
                        3.0, 4.0, 5.0,
                    ]
                    .into_iter()
                    .min_by(|a, b| (a - mm).abs().total_cmp(&(b - mm).abs()))
                    .unwrap();
                    (kind, width, color(fields[2]).unwrap_or("000000"))
                })
            };
            write!(
                borders,
                r##"<hh:{edge} type="{kind}" width="{width} mm" color="#{color}"/>"##
            )
            .unwrap();
        }
        if let Some(fill) = style
            .map(|style| style.fill.as_str())
            .filter(|fill| !fill.is_empty())
        {
            write!(borders, r##"<hc:fillBrush><hc:winBrush faceColor="#{fill}" hatchColor="#000000" alpha="0"/></hc:fillBrush>"##).unwrap();
        }
        borders.push_str("</hh:borderFill>");
    }
    append_items(&mut seed, "borderFills", 2, fills.len() + 1, &borders);
    seed
}

fn part(zip: &mut ZipArchive<Cursor<&[u8]>>, name: &str) -> io::Result<String> {
    let mut xml = String::new();
    zip.by_name(name)?.read_to_string(&mut xml)?;
    Ok(xml)
}

pub(super) fn hwpx(
    html: &Html,
    output: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut seed = ZipArchive::new(Cursor::new(TEMPLATE))?;
    let mut archive = ZipWriter::new(File::create(output)?);
    archive.start_file("mimetype", SimpleFileOptions::default())?;
    archive.write_all(b"application/hwp+zip")?;
    let deflate = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    for index in 0..seed.len() {
        let mut member = seed.by_index(index)?;
        let name = member.name()?.into_owned();
        if ![
            "mimetype",
            SECTION,
            HEADER,
            CONTENT,
            "Preview/PrvImage.png",
            "Preview/PrvText.txt",
        ]
        .contains(&name.as_str())
        {
            archive.start_file(name, deflate)?;
            io::copy(&mut member, &mut archive)?;
        }
    }
    let mut writer = Writer {
        archive: &mut archive,
        check,
        pictures: HashMap::new(),
        image_bytes: 0,
        manifest: String::new(),
        styles: HashMap::new(),
        paragraphs: HashMap::new(),
        fills: HashMap::new(),
        counter: 0,
        preview: String::new(),
        rendered_textboxes: 0,
    };
    let body = html
        .root_element()
        .descendent_elements()
        .find(|item| item.value().name() == "body")
        .ok_or_else(|| invalid("문서 본문을 읽을 수 없습니다."))?;
    let body = body
        .child_elements()
        .find(|item| item.value().name() == "main")
        .unwrap_or(body);
    let [width, height, left, right, top, bottom, head, foot] =
        if let Some(geometry) = body.attr("data-geometry") {
            numbers(Some(geometry), GEOMETRY).map(|value| value.clamp(0, 100_000) * 5)
        } else {
            [59_528, 84_186, 7200, 7200, 7200, 7200, 3600, 3600]
        };
    let available = (width - left - right).max(1000);
    let mut section = part(&mut seed, SECTION)?;
    let root = section
        .find("<hp:p ")
        .ok_or_else(|| invalid("한글 문서 템플릿의 본문을 찾을 수 없습니다."))?;
    let run = section
        .find("<hp:run ")
        .ok_or_else(|| invalid("한글 문서 템플릿의 구역 속성을 찾을 수 없습니다."))?;
    let run_end = section[run..]
        .find("</hp:run>")
        .map(|i| i + run + 9)
        .ok_or_else(|| invalid("한글 문서 템플릿의 구역 속성이 잘못되었습니다."))?;
    let mut layout = section[run..run_end].to_owned();
    let page = layout
        .find("<hp:pagePr ")
        .ok_or_else(|| invalid("한글 문서 템플릿에 용지 정보가 없습니다."))?;
    let end = layout[page..]
        .find("</hp:pagePr>")
        .map(|i| i + page + 12)
        .ok_or_else(|| invalid("한글 문서 템플릿의 용지 정보가 잘못되었습니다."))?;
    let has = |name| {
        body.child_elements()
            .any(|item| item.value().name() == name)
    };
    let (header_top, footer_bottom) = (
        if has("header") { head.min(top) } else { top },
        if has("footer") {
            foot.min(bottom)
        } else {
            bottom
        },
    );
    let orientation = if width > height { "NARROWLY" } else { "WIDELY" };
    layout.replace_range(page..end, &format!(r#"<hp:pagePr landscape="{orientation}" width="{width}" height="{height}" gutterType="LEFT_ONLY"><hp:margin header="{}" footer="{}" gutter="0" left="{left}" right="{right}" top="{header_top}" bottom="{footer_bottom}"/></hp:pagePr>"#, top - header_top, bottom - footer_bottom));
    let mut controls = String::new();
    for item in body
        .child_elements()
        .filter(|item| matches!(item.value().name(), "header" | "footer"))
    {
        let name = item.value().name();
        let id = writer.id()? + 2_000_000;
        let list = SUBLIST.replace(r#"vertAlign="CENTER""#, r#"vertAlign="TOP""#);
        write!(
            controls,
            r#"<hp:run charPrIDRef="0"><hp:ctrl><hp:{name} id="{id}" applyPageType="BOTH">{list}"#
        )
        .unwrap();
        writer.blocks(&mut controls, item, available, 0)?;
        write!(controls, "</hp:subList></hp:{name}></hp:ctrl></hp:run>").unwrap();
    }
    writer.preview.clear();
    let mut blocks = String::new();
    writer.blocks(&mut blocks, body, available, 0)?;
    let at = blocks
        .find('>')
        .ok_or_else(|| invalid("한글 문서 본문이 비었습니다."))?
        + 1;
    blocks.insert_str(at, &format!("{layout}{controls}"));
    section.truncate(root);
    section.push_str(&blocks);
    section.push_str("</hs:sec>");
    let header = header(
        part(&mut seed, HEADER)?,
        writer.styles,
        writer.paragraphs,
        writer.fills,
    );
    let mut content = part(&mut seed, CONTENT)?;
    let metadata = content.find("<opf:metadata>").unwrap();
    let manifest = content.find("<opf:manifest>").unwrap();
    content.replace_range(
        metadata..manifest,
        "<opf:metadata><opf:title/><opf:language>ko</opf:language></opf:metadata>",
    );
    content = content.replace("</opf:manifest>", &format!(r#"{}<opf:item id="version" href="version.xml" media-type="application/xml"/></opf:manifest>"#, writer.manifest));
    let preview = writer.preview;
    let rendered_textboxes = writer.rendered_textboxes;
    for (name, value) in [(SECTION, section), (HEADER, header), (CONTENT, content)] {
        check()?;
        Document::parse(&value).map_err(io::Error::other)?;
        archive.start_file(name, deflate)?;
        archive.write_all(value.as_bytes())?;
    }
    archive.start_file("Preview/PrvText.txt", deflate)?;
    archive.write_all(preview.trim_end_matches('\n').as_bytes())?;
    archive.finish()?;
    if rendered_textboxes > 0 {
        warnings.push(format!("회전·반전된 Word 글상자 {rendered_textboxes}개를 편집할 수 있는 글맵시 개체로 변환했습니다. 글꼴이나 색이 바뀌는 문구는 여러 글맵시로 배치합니다."));
    }
    warnings.push("HWPX 문서 모델로 본문·표·그림·구역 속성을 옮겼습니다. 원본 쪽 나눔, 테두리와 지원하지 않는 개체는 달라질 수 있습니다.".to_owned());
    Ok(())
}
