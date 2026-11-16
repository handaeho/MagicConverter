//! Prepare editable Word graphics for the bundled HWPX PDF renderer.
use crate::hwpx_text::{P, child, invalid, order, package, part};
use base64::{Engine, engine::general_purpose::STANDARD};
use lopdf::{Document as Pdf, Object, Stream, dictionary};
use roxmltree::{Document, Node};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::fmt::Write as _;
use std::fs::{self, File};
use std::io::{self, Cursor, Read, Write};
use std::ops::Range;
use std::path::{Path, PathBuf};
use zip::{ZipWriter, write::SimpleFileOptions};

pub(crate) const TEXTBOX: &str = "MagicConverter rotated textbox v1: ";
const C: &str = "http://www.hancom.co.kr/hwpml/2011/core";
const MANIFEST: &str = "Contents/content.hpf";
const MARKER_EDGE: i64 = 4;
const PNG_TEXTBOX: &str = "MagicConverter Textbox";
type Check<'a> = crate::rhwp::Check<'a>;
type Edit = (Range<usize>, String);

#[derive(Serialize, Deserialize)]
pub(crate) struct Textbox {
    pub html: String,
    pub text: String,
    pub dimensions: [f64; 2],
    pub anchor: String,
    pub wrap: String,
    pub angle: f64,
    pub flip: [bool; 2],
    pub size: [i32; 2],
    pub center_from_art: [i32; 2],
    #[serde(default)]
    pub fit_run: bool,
    #[serde(default)]
    pub style: Option<TextStyle>,
}

#[derive(Clone, PartialEq, Serialize, Deserialize)]
pub(crate) struct TextStyle {
    pub font: String,
    pub color: String,
    pub weight: String,
    pub points: f64,
}

impl Textbox {
    fn render(&self, engine: &Path, mode: &str, output: &Path, check: Check<'_>) -> io::Result<()> {
        let mut args: Vec<_> = [
            self.dimensions[0].to_string(),
            self.dimensions[1].to_string(),
            self.anchor.clone(),
            self.wrap.clone(),
            self.angle.to_string(),
            u8::from(self.flip[0]).to_string(),
            u8::from(self.flip[1]).to_string(),
        ]
        .into_iter()
        .map(Into::into)
        .collect();
        if self.fit_run {
            args.push("fit".into());
        }
        crate::qt_render::render(engine, mode, &self.html, output, &args, check)
    }
}

pub(crate) fn styled_text(
    text: &str,
    font: &str,
    color: &str,
    bold: bool,
    italic: bool,
    points: f64,
    align: &str,
) -> String {
    let escape = crate::safe_html::escape;
    let font = escape(&font.replace(['"', '\'', ';', '\\'], ""));
    let points = if (1.0..=200.0).contains(&points) {
        points
    } else {
        10.0
    };
    format!(
        r#"<p style="text-align:{align}; color:{color}; font-family:'{font}'; font-size:{points}pt; font-weight:{}; font-style:{};">{}</p>"#,
        if bold { "bold" } else { "normal" },
        if italic { "italic" } else { "normal" },
        escape(text)
            .replace("\r\n", "\n")
            .replace('\r', "\n")
            .replace(' ', "&#160;")
            .replace('\n', "<br/>"),
    )
}

pub(super) struct Stage {
    _directory: tempfile::TempDir,
    pub source: PathBuf,
    boxes: Vec<Textbox>,
}

fn integer(node: Node<'_, '_>, key: &str) -> Option<i32> {
    let value = node.attribute(key)?;
    value
        .parse::<i32>()
        .ok()
        .or_else(|| value.parse::<u32>().ok().map(|n| n as i32))
}

fn marker(id: usize) -> Vec<u8> {
    let number = (id as u32).to_le_bytes();
    (0..MARKER_EDGE as usize * MARKER_EDGE as usize)
        .flat_map(|index| {
            let value = number[index % number.len()];
            [value ^ 0xb7, 23 + 11 * index as u8, value ^ 0xc2]
        })
        .collect()
}

fn png(pixels: image::DynamicImage, textbox: Option<&Textbox>) -> io::Result<Vec<u8>> {
    if let Some(textbox) = textbox {
        let rgba = pixels.into_rgba8();
        let mut output = Vec::new();
        let mut encoder = png::Encoder::new(&mut output, rgba.width(), rgba.height());
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        encoder
            .add_itxt_chunk(
                PNG_TEXTBOX.to_owned(),
                serde_json::to_string(textbox).map_err(io::Error::other)?,
            )
            .map_err(io::Error::other)?;
        encoder
            .write_header()
            .and_then(|mut writer| writer.write_image_data(rgba.as_raw()))
            .map_err(io::Error::other)?;
        return Ok(output);
    }
    let mut output = Cursor::new(Vec::new());
    pixels
        .write_to(&mut output, image::ImageFormat::Png)
        .map_err(io::Error::other)?;
    Ok(output.into_inner())
}

fn png_textbox(bytes: &[u8]) -> Option<Textbox> {
    if !bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return None;
    }
    let decoder = png::Decoder::new(Cursor::new(bytes));
    let reader = decoder.read_info().ok()?;
    let chunk = reader
        .info()
        .utf8_text
        .iter()
        .find(|chunk| chunk.keyword == PNG_TEXTBOX)?;
    let text = chunk.get_text().ok()?;
    serde_json::from_str(&text).ok()
}

fn bounds(textbox: &Textbox) -> [i32; 2] {
    let (sin, cos) = textbox.angle.to_radians().sin_cos();
    let [w, h] = textbox.size.map(f64::from);
    [
        (cos.abs() * w + sin.abs() * h).round().max(1.0) as i32,
        (sin.abs() * w + cos.abs() * h).round().max(1.0) as i32,
    ]
}

fn valid_textbox(textbox: &Textbox) -> bool {
    textbox.size.iter().all(|v| (1..=800_000).contains(v))
        && textbox
            .dimensions
            .iter()
            .all(|v| (0.1..=2000.0).contains(v))
        && textbox
            .center_from_art
            .iter()
            .all(|v| v.unsigned_abs() <= 1_000_000)
        && (0.0..360.0).contains(&textbox.angle)
        && textbox.html.len() <= 128 * 1024
}

fn textbox(node: Node<'_, '_>) -> io::Result<Option<Textbox>> {
    let encoded = child(node, "shapeComment")
        .and_then(|comment| comment.text())
        .and_then(|comment| comment.strip_prefix(TEXTBOX));
    let mut textbox = if let Some(encoded) = encoded {
        let value = STANDARD.decode(encoded).map_err(io::Error::other)?;
        serde_json::from_slice(&value).map_err(io::Error::other)?
    } else {
        let Some(textbox) = saved_textbox(node) else {
            return Ok(None);
        };
        textbox
    };
    if !valid_textbox(&textbox) {
        return Err(invalid(
            "회전 글상자의 크기 또는 회전값이 올바르지 않습니다.",
        ));
    }
    let current = node
        .attribute("text")
        .unwrap_or_default()
        .replace("\r\n", "\n")
        .replace(['\r', '\u{240a}'], "\n");
    if current.trim().is_empty() && textbox.html.is_empty() {
        return Ok(None);
    }
    let properties = child(node, "textartPr");
    let color = node
        .children()
        .find(|n| n.has_tag_name((C, "fillBrush")))
        .and_then(|brush| brush.children().find(|n| n.has_tag_name((C, "winBrush"))))
        .and_then(|brush| brush.attribute("faceColor"))
        .filter(|color| {
            color.len() == 7
                && color.starts_with('#')
                && color[1..].bytes().all(|b| b.is_ascii_hexdigit())
        });
    let resized = if textbox.fit_run {
        child(node, "curSz")
            .and_then(|n| Some([integer(n, "width")?, integer(n, "height")?]))
            .filter(|size| {
                size.iter().all(|v| (100..=200_000).contains(v)) && *size != textbox.size
            })
    } else {
        None
    };
    let style_changed = textbox.style.as_ref().is_some_and(|style| {
        properties
            .and_then(|p| p.attribute("fontName"))
            .is_some_and(|font| font != style.font)
            || properties
                .and_then(|p| p.attribute("fontStyle"))
                .is_some_and(|weight| weight != style.weight)
            || color.is_some_and(|value| !value.eq_ignore_ascii_case(&style.color))
    });
    if current != textbox.text || style_changed || resized.is_some() {
        let font = properties
            .and_then(|p| p.attribute("fontName"))
            .unwrap_or("Malgun Gothic");
        let weight = properties
            .and_then(|p| p.attribute("fontStyle"))
            .unwrap_or_default();
        let bold = weight.contains("굵게") || weight.contains("Bold");
        let italic = weight.contains("기울임") || weight.contains("Italic");
        let fill = color.unwrap_or("#000000");
        let fontsize = textbox
            .style
            .as_ref()
            .map(|style| style.points)
            .or_else(|| {
                child(node, "curSz")
                    .and_then(|size| integer(size, "height"))
                    .map(|height| {
                        (f64::from(height) / 125.0 / current.lines().count().max(1) as f64)
                            .clamp(1.0, 150.0)
                    })
            })
            .unwrap_or(10.0);
        let align = match properties.and_then(|p| p.attribute("align")) {
            Some("LEFT") => "left",
            Some("RIGHT") => "right",
            _ => "center",
        };
        textbox.html = styled_text(&current, font, fill, bold, italic, fontsize, align);
        if textbox.fit_run {
            textbox.anchor = "TOP".to_owned();
        }
        textbox.text = current;
    }
    if let Some(size) = resized {
        textbox.dimensions = size.map(|v| f64::from(v) / 100.0);
        textbox.size = size;
        textbox.center_from_art = size.map(|v| v / 2);
    }
    if textbox.fit_run {
        if let Some(angle) = textart_rotation(node) {
            textbox.angle = angle;
        }
        if let Some(flip) = child(node, "flip") {
            textbox.flip = ["horizontal", "vertical"].map(|axis| flip.attribute(axis) == Some("1"));
        }
    }
    Ok(Some(textbox))
}

fn textart_rotation(node: Node<'_, '_>) -> Option<f64> {
    if let Some(matrix) = child(node, "renderingInfo")?
        .children()
        .find(|n| n.has_tag_name((C, "rotMatrix")))
    {
        let x = matrix.attribute("e1")?.parse::<f64>().ok()?;
        let y = matrix.attribute("e4")?.parse::<f64>().ok()?;
        if x.is_finite() && y.is_finite() && x.hypot(y) > 0.01 {
            return Some(y.atan2(x).to_degrees().rem_euclid(360.0));
        }
    }
    let angle = integer(child(node, "rotationInfo")?, "angle")?;
    Some(f64::from((-i64::from(angle)).rem_euclid(360) as i32))
}

// Hancom rewrites the drawing XML and can remove the shape comment on save.
// Plain rectangular TextArt still carries its content, style and placement.
fn saved_textbox(node: Node<'_, '_>) -> Option<Textbox> {
    let properties = child(node, "textartPr")?;
    if properties.attribute("textShape")? != "RECTANGLE"
        || properties
            .attribute("charSpacing")
            .is_some_and(|value| value != "100")
    {
        return None;
    }
    let size_node = child(node, "curSz")?;
    let size = [integer(size_node, "width")?, integer(size_node, "height")?];
    if !size.iter().all(|v| (100..=200_000).contains(v)) {
        return None;
    }
    Some(Textbox {
        html: String::new(),
        text: String::new(),
        dimensions: size.map(|v| f64::from(v) / 100.0),
        size,
        center_from_art: size.map(|v| v / 2),
        anchor: "TOP".to_owned(),
        wrap: "none".to_owned(),
        angle: 0.0,
        flip: [false; 2],
        fit_run: true,
        style: None,
    })
}

fn placeholder(
    node: Node<'_, '_>,
    textbox: &Textbox,
    name: &str,
    legacy_hwp: bool,
) -> io::Result<String> {
    let id = node
        .attribute("id")
        .and_then(|v| v.parse::<u32>().ok())
        .unwrap_or(0);
    let level = node
        .attribute("groupLevel")
        .and_then(|v| v.parse::<u32>().ok())
        .unwrap_or(0);
    let offset = child(node, "offset");
    let [w, h] = bounds(textbox);
    let x = offset
        .and_then(|n| integer(n, "x"))
        .unwrap_or(0)
        .saturating_add(if node.has_tag_name((P, "pic")) {
            0
        } else {
            textbox.center_from_art[0] - w / 2
        });
    let y = offset
        .and_then(|n| integer(n, "y"))
        .unwrap_or(0)
        .saturating_add(if node.has_tag_name((P, "pic")) {
            0
        } else {
            textbox.center_from_art[1] - h / 2
        });
    let comment = if legacy_hwp {
        let text = crate::safe_html::escape(node.attribute("text").unwrap_or_default());
        format!(
            "<hp:shapeComment>{}{text}</hp:shapeComment>",
            crate::hwpx_text::WORD_TEXTBOX_COMMENT
        )
    } else {
        String::new()
    };
    let matrix = format!(
        r#"<hp:renderingInfo><hc:transMatrix e1="1" e2="0" e3="{x}" e4="0" e5="1" e6="{y}"/><hc:scaMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/><hc:rotMatrix e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/></hp:renderingInfo>"#
    );
    let placement: String = if level == 0 {
        ["sz", "pos", "outMargin"]
            .into_iter()
            .filter_map(|tag| child(node, tag).map(|node| (tag, node)))
            .map(|(tag, n)| {
                if tag != "sz" { return n.document().input_text()[n.range()].to_owned(); }
                format!(r#"<hp:sz width="{w}" widthRelTo="ABSOLUTE" height="{h}" heightRelTo="ABSOLUTE" protect="0"/>"#)
            })
            .collect()
    } else {
        String::new()
    };
    Ok(format!(
        r#"<hp:pic xmlns:hp="{P}" xmlns:hc="{C}" id="{id}" instid="{id}" zOrder="{id}" numberingType="NONE" textWrap="TOP_AND_BOTTOM" textFlow="BOTH_SIDES" lock="0" dropcapstyle="None" reverse="0" groupLevel="{level}"><hp:offset x="{}" y="{}"/><hp:orgSz width="{w}" height="{h}"/><hp:curSz width="{w}" height="{h}"/>{matrix}<hp:imgRect><hc:pt0 x="0" y="0"/><hc:pt1 x="{w}" y="0"/><hc:pt2 x="{w}" y="{h}"/><hc:pt3 x="0" y="{h}"/></hp:imgRect><hp:imgClip left="0" top="0" right="{w}" bottom="{h}"/><hp:inMargin left="0" right="0" top="0" bottom="0"/><hp:imgDim dimwidth="{w}" dimheight="{h}"/><hc:img binaryItemIDRef="{name}" bright="0" contrast="0" effect="REAL_PIC" alpha="0"/><hp:effects/>{placement}{comment}</hp:pic>"#,
        x as u32, y as u32
    ))
}

struct Flipped<'a> {
    image: Node<'a, 'a>,
    flip: Node<'a, 'a>,
    scale: Node<'a, 'a>,
    clip: Node<'a, 'a>,
    dimensions: [i32; 2],
    region: [i32; 4],
    horizontal: bool,
    vertical: bool,
}

fn flipped<'a>(node: Node<'a, 'a>) -> Option<Flipped<'a>> {
    if node.attribute("groupLevel")?.parse::<u32>().ok()? == 0 {
        return None;
    }
    let flip = child(node, "flip")?;
    let horizontal = flip.attribute("horizontal") == Some("1");
    let vertical = flip.attribute("vertical") == Some("1");
    if !horizontal && !vertical {
        return None;
    }
    let scale = child(node, "renderingInfo")?
        .children()
        .find(|n| n.has_tag_name((C, "scaMatrix")))?;
    let dim = child(node, "orgSz")?;
    let dimensions = [integer(dim, "width")?, integer(dim, "height")?];
    let expected = [
        if horizontal { -1.0 } else { 1.0 },
        0.0,
        if horizontal {
            f64::from(dimensions[0])
        } else {
            0.0
        },
        0.0,
        if vertical { -1.0 } else { 1.0 },
        if vertical {
            f64::from(dimensions[1])
        } else {
            0.0
        },
    ];
    for (key, expected) in ["e1", "e2", "e3", "e4", "e5", "e6"]
        .into_iter()
        .zip(expected)
    {
        if (scale.attribute(key)?.parse::<f64>().ok()? - expected).abs() > 1e-6 {
            return None;
        }
    }
    let clip = child(node, "imgClip")?;
    let region = [
        integer(clip, "left")?,
        integer(clip, "top")?,
        integer(clip, "right")?,
        integer(clip, "bottom")?,
    ];
    if dimensions
        .iter()
        .any(|&value| !(1..=800_000).contains(&value))
        || region.iter().any(|value| value.unsigned_abs() > 1_600_000)
    {
        return None;
    }
    Some(Flipped {
        image: node.children().find(|n| n.has_tag_name((C, "img")))?,
        flip,
        scale,
        clip,
        dimensions,
        region,
        horizontal,
        vertical,
    })
}

impl Flipped<'_> {
    fn edits(&self, name: &str, xml: &str, edits: &mut Vec<Edit>) -> io::Result<()> {
        let id = self
            .image
            .attribute_node("binaryItemIDRef")
            .ok_or_else(|| invalid("그림 참조가 없습니다."))?;
        let range = id.range_value();
        debug_assert!(xml.get(range.clone()).is_some());
        edits.push((range, name.to_owned()));
        let [w, h] = self.dimensions;
        let [mut left, mut top, mut right, mut bottom] = self.region;
        if self.horizontal {
            (left, right) = (w - right, w - left);
        }
        if self.vertical {
            (top, bottom) = (h - bottom, h - top);
        }
        edits.push((self.clip.range(), format!(r#"<hp:imgClip xmlns:hp="{P}" left="{left}" top="{top}" right="{right}" bottom="{bottom}"/>"#)));
        edits.push((
            self.scale.range(),
            format!(r#"<hc:scaMatrix xmlns:hc="{C}" e1="1" e2="0" e3="0" e4="0" e5="1" e6="0"/>"#),
        ));
        edits.push((
            self.flip.range(),
            format!(r#"<hp:flip xmlns:hp="{P}" horizontal="0" vertical="0"/>"#),
        ));
        Ok(())
    }
}

pub(super) fn prepare(
    source: &Path,
    legacy_hwp: bool,
    check: Check<'_>,
) -> io::Result<Option<Stage>> {
    let mut archive = package(source)?;
    if archive.index_for_name(MANIFEST).is_none() {
        return Ok(None);
    }
    let mut manifest = part(&mut archive, MANIFEST)?;
    let doc = Document::parse(&manifest).map_err(io::Error::other)?;
    let Some(list) = doc
        .descendants()
        .find(|node| node.tag_name().name() == "manifest")
    else {
        return Ok(None);
    };
    let prefix = list.tag_name().namespace().unwrap_or_default();
    let used: HashSet<_> = list
        .children()
        .filter_map(|item| item.attribute("id"))
        .collect();
    let original: HashMap<_, _> = list
        .children()
        .filter_map(|item| item.attribute("id").zip(item.attribute("href")))
        .collect();
    let end = manifest[..list.range().end]
        .rfind("</")
        .ok_or_else(|| invalid("HWPX manifest"))?;
    let mut extra = String::new();
    let mut replacements = HashMap::new();
    let mut images = Vec::new();
    let mut boxes = Vec::new();
    let mut cached_flips = HashMap::<_, String>::new();
    let mut image_bytes = 0;
    let directory = tempfile::Builder::new()
        .prefix("magic-hwpx-graphics-")
        .tempdir()?;
    for section in order(&mut archive)? {
        check()?;
        let mut xml = part(&mut archive, &section)?;
        let doc = Document::parse(&xml).map_err(io::Error::other)?;
        let mut edits = Vec::new();
        for node in doc
            .descendants()
            .filter(|n| n.has_tag_name((P, "textart")) || n.has_tag_name((P, "pic")))
        {
            check()?;
            let mut name = format!("magicPdfGraphic{}", images.len());
            while used.contains(name.as_str())
                || archive
                    .index_for_name(&format!("BinData/{name}.png"))
                    .is_some()
            {
                name.push('_');
            }
            let mut embedded = None;
            let pixels = if node.has_tag_name((P, "textart")) {
                let Some(textbox) = textbox(node)? else {
                    continue;
                };
                if boxes.len() >= 4096 {
                    return Err(invalid("회전 글상자의 개수 한도를 초과했습니다."));
                }
                edits.push((
                    node.range(),
                    placeholder(node, &textbox, &name, legacy_hwp)?,
                ));
                if legacy_hwp {
                    let engine = crate::qt_render::engine()
                        .ok_or_else(|| invalid("Qt 글상자 엔진이 없습니다."))?;
                    let rendered = directory.path().join("textbox.png");
                    textbox.render(&engine, "textbox-png", &rendered, check)?;
                    embedded = Some(textbox);
                    image::open(&rendered).map_err(io::Error::other)?
                } else {
                    let marker = image::RgbImage::from_raw(
                        MARKER_EDGE as u32,
                        MARKER_EDGE as u32,
                        marker(boxes.len()),
                    )
                    .expect("fixed marker dimensions");
                    boxes.push(textbox);
                    image::DynamicImage::ImageRgb8(marker)
                }
            } else {
                if legacy_hwp {
                    continue;
                }
                let Some(href) = node
                    .children()
                    .find(|n| n.has_tag_name((C, "img")))
                    .and_then(|image| image.attribute("binaryItemIDRef"))
                    .and_then(|id| original.get(id))
                    .copied()
                else {
                    continue;
                };
                let mut bytes = Vec::new();
                if href.to_ascii_lowercase().ends_with(".png") {
                    let mut entry = archive.by_name(href.trim_start_matches("./"))?;
                    // Text metadata is written before IDAT. Read the prefix only.
                    (&mut entry).take(256 * 1024).read_to_end(&mut bytes)?;
                    if let Some(textbox) = png_textbox(&bytes).filter(valid_textbox) {
                        if boxes.len() >= 4096 {
                            return Err(invalid("회전 글상자의 개수 한도를 초과했습니다."));
                        }
                        edits.push((node.range(), placeholder(node, &textbox, &name, false)?));
                        let image = image::RgbImage::from_raw(
                            MARKER_EDGE as u32,
                            MARKER_EDGE as u32,
                            marker(boxes.len()),
                        )
                        .expect("fixed marker dimensions");
                        boxes.push(textbox);
                        let path = format!("BinData/{name}.png");
                        write!(extra, r#"<item xmlns="{prefix}" id="{name}" href="{path}" media-type="image/png" isEmbeded="1"/>"#).unwrap();
                        images.push((path, png(image::DynamicImage::ImageRgb8(image), None)?));
                        continue;
                    }
                }
                let Some(flip) = flipped(node) else {
                    continue;
                };
                let key = (href, flip.horizontal, flip.vertical);
                if let Some(cached) = cached_flips.get(&key) {
                    flip.edits(cached, &xml, &mut edits)?;
                    continue;
                }
                bytes.clear();
                let mut entry = archive.by_name(href.trim_start_matches("./"))?;
                if entry.size() > 128 * 1024 * 1024 {
                    return Err(invalid("한글 문서의 포함 그림 크기 한도를 초과했습니다."));
                }
                entry.read_to_end(&mut bytes)?;
                let reader = image::ImageReader::new(Cursor::new(&bytes)).with_guessed_format()?;
                let (w, h) = match reader.into_dimensions() {
                    Ok(size) => size,
                    Err(_) => continue,
                };
                if u64::from(w) * u64::from(h) > 128_000_000 {
                    return Err(invalid("한글 문서의 그림 크기 한도를 초과했습니다."));
                }
                let mut image = image::load_from_memory(&bytes)
                    .map_err(io::Error::other)?
                    .into_rgba8();
                if flip.horizontal {
                    image::imageops::flip_horizontal_in_place(&mut image);
                }
                if flip.vertical {
                    image::imageops::flip_vertical_in_place(&mut image);
                }
                flip.edits(&name, &xml, &mut edits)?;
                cached_flips.insert(key, name.clone());
                image::DynamicImage::ImageRgba8(image)
            };
            let bytes = png(pixels, embedded.as_ref())?;
            image_bytes += bytes.len();
            if image_bytes > 128 * 1024 * 1024 {
                return Err(invalid(
                    "회전 글상자와 반전 그림의 임시 데이터가 너무 큽니다.",
                ));
            }
            let path = format!("BinData/{name}.png");
            write!(extra, r#"<item xmlns="{prefix}" id="{name}" href="{path}" media-type="image/png" isEmbeded="1"/>"#).unwrap();
            images.push((path, bytes));
        }
        if !edits.is_empty() {
            edits.sort_by_key(|(range, _)| range.start);
            for (range, value) in edits.into_iter().rev() {
                xml.replace_range(range, &value);
            }
            replacements.insert(section, xml);
        }
    }
    if replacements.is_empty() {
        return Ok(None);
    }
    manifest.insert_str(end, &extra);
    replacements.insert(MANIFEST.to_owned(), manifest);
    let source = directory.path().join("graphics.hwpx");
    let mut zip = ZipWriter::new(File::create(&source)?);
    for index in 0..archive.len() {
        check()?;
        let entry = archive.by_index(index)?;
        if let Some(value) = replacements.remove(entry.name()?.as_ref()) {
            zip.start_file(
                entry.name()?.as_ref(),
                SimpleFileOptions::default().compression_method(entry.compression()),
            )?;
            zip.write_all(value.as_bytes())?;
        } else {
            zip.raw_copy_file(entry)?;
        }
    }
    for (name, bytes) in images {
        zip.start_file(name, SimpleFileOptions::default())?;
        zip.write_all(&bytes)?;
    }
    zip.finish()?;
    Ok(Some(Stage {
        _directory: directory,
        source,
        boxes,
    }))
}

impl Stage {
    pub(super) fn contains_text(&self) -> bool {
        !self.boxes.is_empty()
    }
    pub fn finish(self, input: &Path, output: &Path, check: Check<'_>) -> io::Result<()> {
        if self.boxes.is_empty() {
            fs::rename(input, output).or_else(|_| fs::copy(input, output).map(|_| ()))?;
            return Ok(());
        }
        let mut pdf = Pdf::load(input).map_err(io::Error::other)?;
        // rhwp places each marker as a unit-square image XObject, even inside
        // nested forms. Replacing that object keeps the same page transform,
        // clipping and paint order while retaining the PDF text operators.
        let candidates: HashMap<_, _> = self
            .boxes
            .iter()
            .enumerate()
            .map(|(index, _)| (marker(index), index))
            .collect();
        let matches: Vec<_> = pdf
            .objects
            .iter()
            .filter_map(|(&id, object)| {
                let stream = object.as_stream().ok()?;
                let dict = &stream.dict;
                if dict.get(b"Subtype").ok()?.as_name().ok()? != b"Image"
                    || dict.get(b"Width").ok()?.as_i64().ok()? != MARKER_EDGE
                    || dict.get(b"Height").ok()?.as_i64().ok()? != MARKER_EDGE
                {
                    return None;
                }
                let bytes = stream.get_plain_content().ok()?;
                Some((id, *candidates.get(&bytes)?))
            })
            .collect();
        if matches
            .iter()
            .map(|(_, index)| *index)
            .collect::<HashSet<_>>()
            .len()
            != self.boxes.len()
        {
            return Err(invalid("PDF에서 회전 글상자의 위치를 찾을 수 없습니다."));
        }
        let engine =
            crate::qt_render::engine().ok_or_else(|| invalid("Qt 글상자 엔진이 없습니다."))?;
        let rendered = self._directory.path().join("textbox.pdf");
        let mut forms = HashMap::new();
        for (id, index) in matches {
            check()?;
            if let Some(form) = forms.get(&index).cloned() {
                pdf.objects.insert(id, form);
                continue;
            }
            let textbox = &self.boxes[index];
            textbox.render(&engine, "textbox-pdf", &rendered, check)?;
            let mut fragment = Pdf::load(&rendered).map_err(io::Error::other)?;
            fragment.renumber_objects_with(pdf.max_id + 1);
            let page = fragment
                .page_iter()
                .next()
                .ok_or_else(|| invalid("글상자 PDF에 쪽이 없습니다."))?;
            let dict = fragment.get_dictionary(page).map_err(io::Error::other)?;
            let media = dict
                .get(b"MediaBox")
                .and_then(Object::as_array)
                .map_err(io::Error::other)?;
            let dimension = |i| -> io::Result<f32> {
                let value = media
                    .get(i)
                    .and_then(|v: &Object| v.as_float().ok())
                    .unwrap_or(0.0);
                if value > 0.0 {
                    Ok(value)
                } else {
                    Err(invalid("글상자 PDF의 크기가 잘못되었습니다."))
                }
            };
            let (w, h) = (dimension(2)?, dimension(3)?);
            let resources = dict.get(b"Resources").map_err(io::Error::other)?.clone();
            let contents = fragment.get_page_content(page).map_err(io::Error::other)?;
            let form = Object::Stream(Stream::new(
                dictionary! {
                    "Type" => "XObject", "Subtype" => "Form",
                    "BBox" => vec![0.into(), 0.into(), w.into(), h.into()],
                    "Matrix" => vec![(1.0/w).into(), 0.into(), 0.into(), (1.0/h).into(), 0.into(), 0.into()],
                    "Resources" => resources,
                },
                contents,
            ));
            pdf.max_id = fragment.max_id;
            pdf.objects.extend(fragment.objects);
            pdf.objects.insert(id, form.clone());
            forms.insert(index, form);
        }
        pdf.prune_objects();
        pdf.compress();
        pdf.save(output)?;
        Ok(())
    }
}
