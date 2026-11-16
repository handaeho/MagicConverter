use crate::{Edge, office_package, safe_html};
use base64::Engine as _;
use image::ImageReader;
use roxmltree::{Document, Node};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{self, Cursor, ErrorKind, Read};
use std::path::Path;
use zip::ZipArchive;

mod groups;

const WORD: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const MATH: &str = "http://schemas.openxmlformats.org/officeDocument/2006/math";
const MAIN: &str = "word/document.xml";
const MAX_PART: u64 = 64 * 1024 * 1024;
const MAX_PACKAGE: u64 = 512 * 1024 * 1024;
const MODEL_WARNING: &str = "텍스트 중심 출력입니다. 그림·표 배치·수식 조판은 포함하지 않습니다.";
const VARIANT_WARNING: &str = "Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.";

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
type Zip = ZipArchive<File>;

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn node<'a, 'input>(parent: Node<'a, 'input>, name: &str) -> Option<Node<'a, 'input>> {
    parent
        .children()
        .find(|child| child.has_tag_name((WORD, name)))
}

fn edge_node<'a, 'input>(
    parent: Option<Node<'a, 'input>>,
    edge: &str,
    alias: &str,
) -> Option<Node<'a, 'input>> {
    parent.and_then(|item| node(item, edge).or_else(|| node(item, alias)))
}
fn path<'a, 'input>(parent: Node<'a, 'input>, names: &[&str]) -> Option<Node<'a, 'input>> {
    names
        .iter()
        .try_fold(parent, |parent, name| node(parent, name))
}

fn attr<'a>(item: Node<'a, '_>, name: &str) -> Option<&'a str> {
    item.attribute((WORD, name))
}

fn val<'a>(item: Option<Node<'a, '_>>) -> Option<&'a str> {
    attr(item?, "val")
}

fn inline_line(drawing: Node<'_, '_>) -> Option<String> {
    fn node<'a, 'input>(parent: Node<'a, 'input>, name: &str) -> Option<Node<'a, 'input>> {
        parent
            .children()
            .find(|item| item.is_element() && item.tag_name().name() == name)
    }
    fn path<'a, 'input>(parent: Node<'a, 'input>, names: &[&str]) -> Option<Node<'a, 'input>> {
        names
            .iter()
            .try_fold(parent, |parent, name| node(parent, name))
    }
    let inline = node(drawing, "inline")?;
    let data = path(inline, &["graphic", "graphicData"])?;
    let group = node(data, "wgp")?;
    if data.children().filter(Node::is_element).count() != 1
        || group
            .children()
            .filter(Node::is_element)
            .any(|item| !matches!(item.tag_name().name(), "cNvGrpSpPr" | "grpSpPr" | "wsp"))
        || group
            .children()
            .filter(|item| item.tag_name().name() == "wsp")
            .count()
            != 1
    {
        return None;
    }
    let shape = node(group, "wsp")?;
    let properties = node(shape, "spPr")?;
    if node(shape, "txbx").is_some()
        || properties.children().filter(Node::is_element).any(|item| {
            !matches!(
                item.tag_name().name(),
                "xfrm" | "custGeom" | "ln" | "noFill"
            )
        })
    {
        return None;
    }
    let group_properties = node(group, "grpSpPr")?;
    let (g, s) = (node(group_properties, "xfrm")?, node(properties, "xfrm")?);
    if group_properties.children().filter(Node::is_element).count() != 1
        || [g, s].iter().any(|t| {
            ["rot", "flipH", "flipV"]
                .iter()
                .any(|key| !matches!(t.attribute(*key), None | Some("0" | "false")))
        })
    {
        return None;
    }
    let outline = node(properties, "ln")?;
    let paint = path(outline, &["solidFill", "srgbClr"])?;
    if paint.children().any(|item| item.is_element())
        || outline
            .children()
            .filter(Node::is_element)
            .any(|item| !matches!(item.tag_name().name(), "solidFill" | "prstDash"))
        || node(outline, "prstDash").is_some_and(|item| item.attribute("val") != Some("solid"))
    {
        return None;
    }
    let color = paint.attribute("val").and_then(crate::hwp_model::color)?;
    let paths = path(properties, &["custGeom", "pathLst"])?;
    let geometry = node(paths, "path")?;
    let pieces: Vec<_> = geometry.children().filter(Node::is_element).collect();
    if paths.children().filter(Node::is_element).count() != 1
        || !matches!(pieces.as_slice(), [a,b] if a.tag_name().name()=="moveTo" && b.tag_name().name()=="lnTo")
    {
        return None;
    }
    let number = |item: Node<'_, '_>, key: &str| {
        item.attribute(key)?
            .parse::<i32>()
            .ok()
            .filter(|n| n.unsigned_abs() <= 100_000_000)
            .map(f64::from)
    };
    let pair = |item, a, b| Some([number(item, a)?, number(item, b)?]);
    let [w, h] = pair(node(inline, "extent")?, "cx", "cy")?;
    let [gx, gy] = pair(node(g, "chOff")?, "x", "y")?;
    let [gw, gh] = pair(node(g, "chExt")?, "cx", "cy")?;
    let [sx, sy] = pair(node(s, "off")?, "x", "y")?;
    let [sw, sh] = pair(node(s, "ext")?, "cx", "cy")?;
    let [ax, y] = pair(node(pieces[0], "pt")?, "x", "y")?;
    let [bx, by] = pair(node(pieces[1], "pt")?, "x", "y")?;
    let pw = number(geometry, "w").unwrap_or(sw);
    let ph = number(geometry, "h").unwrap_or(sh);
    if [w, h, gw, gh, sw, pw].iter().any(|v| *v <= 0.0)
        || y != by
        || ax == bx
        || ax.min(bx) < 0.0
        || ax.max(bx) > pw
        || y < 0.0
        || y > ph
    {
        return None;
    }
    let x = |p| (sx + sw * p / pw - gx) * w / gw;
    let (a, b) = (x(ax.min(bx)), x(ax.max(bx)));
    let y = (sy + if ph > 0.0 { sh * y / ph } else { 0.0 } - gy) * h / gh;
    let stroke = number(outline, "w")? * h / gh;
    if a < 0.0
        || b > w
        || b - a < 127.0
        || !(0.0..=h).contains(&y)
        || stroke <= 0.0
        || stroke > 2.0 * h
    {
        return None;
    }
    let [w, h, a, b, y, stroke] =
        [w, h, a, b, y, stroke].map(|emu| (emu / 127.0).round_ties_even() as i32);
    Some(format!(
        r#"<span data-inline-line="{w} {h} {a} {b} {y} {stroke}" data-color="{color}"></span>"#
    ))
}

pub(crate) fn read_part(archive: &mut Zip, part: &str) -> io::Result<Vec<u8>> {
    let file = archive.by_name(part)?;
    if file.size() > MAX_PART {
        return Err(invalid("Word 패키지 항목의 크기가 너무 큽니다."));
    }
    let mut bytes = Vec::new();
    file.take(MAX_PART + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_PART {
        return Err(invalid("Word 패키지 항목의 크기가 너무 큽니다."));
    }
    Ok(bytes)
}

fn parse(value: &str) -> io::Result<Document<'_>> {
    Document::parse(value).map_err(io::Error::other)
}

pub(crate) fn package(source: &Path, check: Check<'_>) -> io::Result<Zip> {
    let mut archive = Zip::new(File::open(source)?)?;
    let mut names = HashSet::new();
    let mut total = 0_u64;
    for index in 0..archive.len() {
        check()?;
        let entry = archive.by_index(index)?;
        if !names.insert(entry.name()?.into_owned()) {
            return Err(invalid("Word 패키지에 중복 항목이 있습니다."));
        }
        total = total.saturating_add(entry.size());
    }
    if total > MAX_PACKAGE || archive.len() > 100_000 {
        return Err(invalid("Word 패키지의 크기 제한을 초과했습니다."));
    }
    Ok(archive)
}

pub(crate) fn relationships(
    archive: &mut Zip,
    source: &str,
) -> io::Result<HashMap<String, String>> {
    let (parent, file) = source
        .rsplit_once('/')
        .ok_or_else(|| invalid("Word 항목 경로가 올바르지 않습니다."))?;
    let relationship = format!("{parent}/_rels/{file}.rels");
    if archive.index_for_name(&relationship).is_none() {
        return Ok(HashMap::new());
    }
    let bytes = read_part(archive, &relationship)?;
    let text = std::str::from_utf8(&bytes).map_err(io::Error::other)?;
    let doc = parse(text)?;
    let mut items = HashMap::new();
    for item in doc.root_element().children().filter(Node::is_element) {
        if item.attribute("TargetMode") == Some("External") {
            continue;
        }
        let Some((id, target)) = item.attribute("Id").zip(item.attribute("Target")) else {
            continue;
        };
        let target = percent_encoding::percent_decode_str(target)
            .decode_utf8()
            .map_err(io::Error::other)?;
        if target.contains(['\\', ':', '\0']) {
            continue;
        }
        let mut segments: Vec<&str> = if target.starts_with('/') {
            vec![]
        } else {
            parent.split('/').collect()
        };
        let mut valid = true;
        for segment in target.split('/') {
            match segment {
                "" | "." => {}
                ".." if segments.pop().is_none() => {
                    valid = false;
                    break;
                }
                ".." => {}
                value => segments.push(value),
            }
        }
        if valid {
            items.insert(id.to_owned(), segments.join("/"));
        }
    }
    Ok(items)
}

fn math(item: Node<'_, '_>, supported: &mut bool, depth: usize) -> String {
    if depth > 64 {
        *supported = false;
        return item
            .descendants()
            .filter(|part| part.has_tag_name((MATH, "t")))
            .filter_map(|part| part.text())
            .collect();
    }
    let tag = item.tag_name().name();
    if !item.is_element() || tag.ends_with("Pr") {
        return String::new();
    }
    if tag == "t" {
        return item.text().unwrap_or_default().to_owned();
    }
    let mut child = |name| {
        item.children()
            .find(|node| node.has_tag_name((MATH, name)))
            .map_or_else(String::new, |node| math(node, supported, depth + 1))
    };
    match tag {
        "f" => format!("{{{}}} over {{{}}}", child("num"), child("den")),
        "rad" => {
            let degree = child("deg");
            let base = child("e");
            if degree.is_empty() {
                format!("sqrt {{{base}}}")
            } else {
                format!("root {{{degree}}} of {{{base}}}")
            }
        }
        "sSup" | "sSub" | "sSubSup" => {
            let mut text = format!("{{{}}}", child("e"));
            if tag != "sSup" {
                text.push_str(&format!("_{{{}}}", child("sub")));
            }
            if tag != "sSub" {
                text.push_str(&format!("^{{{}}}", child("sup")));
            }
            text
        }
        _ => {
            if !matches!(
                tag,
                "oMath" | "oMathPara" | "e" | "num" | "den" | "deg" | "sub" | "sup" | "r"
            ) {
                *supported = false;
            }
            item.children()
                .map(|part| math(part, supported, depth + 1))
                .collect()
        }
    }
}

#[derive(Default)]
struct Block {
    text: String,
    html: String,
}

#[derive(Default)]
struct Styles<'a, 'input> {
    entries: HashMap<&'a str, Node<'a, 'input>>,
    defaults: HashMap<&'a str, &'a str>,
    document: Option<Node<'a, 'input>>,
}

impl<'a, 'input> Styles<'a, 'input> {
    fn new(source: &'a Document<'input>) -> Self {
        let root = source.root_element();
        let mut styles = Styles::default();
        for style in root
            .children()
            .filter(|node| node.has_tag_name((WORD, "style")))
        {
            if let Some(id) = attr(style, "styleId") {
                styles.entries.insert(id, style);
                if matches!(attr(style, "default"), Some("1" | "true" | "on"))
                    && let Some(kind) = attr(style, "type")
                {
                    styles.defaults.insert(kind, id);
                }
            }
        }
        styles.document = path(root, &["docDefaults", "rPrDefault", "rPr"]);
        styles
    }

    fn chain(&self, selected: Option<&str>, kind: &str) -> Vec<Node<'a, 'input>> {
        let mut current = selected.or_else(|| self.defaults.get(kind).copied());
        let mut seen = HashSet::new();
        let mut layers = Vec::new();
        while let Some(id) = current
            && let Some(style) = self.entries.get(id)
            && seen.len() < 64
            && seen.insert(id)
        {
            layers.extend(node(*style, "rPr"));
            current = val(node(*style, "basedOn"));
        }
        layers
    }
}

struct Renderer<'a, 'styles, 'input> {
    archive: &'a mut Zip,
    check: Check<'a>,
    warnings: &'a mut Vec<String>,
    rels: HashMap<String, String>,
    html: bool,
    layout: bool,
    tables: usize,
    pictures: usize,
    equations: usize,
    styles: Styles<'styles, 'input>,
    textbox_fonts: Option<[String; 2]>,
    in_textbox: bool,
}

impl Renderer<'_, '_, '_> {
    fn warn(&mut self, message: &str) {
        if !self.warnings.iter().any(|warning| warning == message) {
            self.warnings.push(message.to_owned());
        }
    }

    fn picture(
        &mut self,
        picture: Node<'_, '_>,
        available: f64,
        output: &mut String,
    ) -> io::Result<()> {
        if self.layout
            && let Some(line) = inline_line(picture)
        {
            output.push_str(&line);
            return Ok(());
        }
        if self.layout && self.group(picture, output)? {
            return Ok(());
        }
        let mut found = false;
        for blip in picture.descendants().filter(|child| {
            child.has_tag_name((
                "http://schemas.openxmlformats.org/drawingml/2006/main",
                "blip",
            ))
        }) {
            found = true;
            self.pictures += 1;
            let id = blip
                .attribute((REL, "embed"))
                .ok_or_else(|| invalid("외부 링크 그림은 포함 그림으로 저장한 뒤 변환하세요."))?;
            let part = self
                .rels
                .get(id)
                .ok_or_else(|| invalid("Word 그림의 연결 항목을 찾을 수 없습니다."))?;
            if !self.html {
                continue;
            }
            (self.check)()?;
            let mut bytes = read_part(self.archive, part)?;
            let reader = ImageReader::new(Cursor::new(&bytes)).with_guessed_format()?;
            let mut mime = reader
                .format()
                .map_or("image/png", |format| format.to_mime_type());
            let (pixel_w, pixel_h) = reader.into_dimensions().map_err(io::Error::other)?;
            if u64::from(pixel_w) * u64::from(pixel_h) > 128_000_000 {
                return Err(invalid("Word 그림의 픽셀 수가 너무 많습니다."));
            }
            let extent = picture.descendants().find(|node| {
                node.has_tag_name((
                    "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
                    "extent",
                ))
            });
            let size = |name, pixels| {
                extent
                    .and_then(|node| node.attribute(name))
                    .and_then(|s| s.parse::<f64>().ok())
                    .map_or(f64::from(pixels) * 0.75, |size| size / 12700.0)
            };
            let (width, height) = (size("cx", pixel_w), size("cy", pixel_h));
            let crop = picture.descendants().find(|node| {
                node.has_tag_name((
                    "http://schemas.openxmlformats.org/drawingml/2006/main",
                    "srcRect",
                ))
            });
            if let Some(crop) = crop.filter(|_| !self.layout) {
                let trim = |name, dimension| {
                    crop.attribute(name)
                        .and_then(|value| value.parse::<f64>().ok())
                        .map_or(0, |fraction| {
                            (fraction.max(0.0) * f64::from(dimension) / 100000.0).round_ties_even()
                                as u32
                        })
                };
                let (left, top, right, bottom) = (
                    trim("l", pixel_w),
                    trim("t", pixel_h),
                    trim("r", pixel_w),
                    trim("b", pixel_h),
                );
                if left.saturating_add(right) >= pixel_w || top.saturating_add(bottom) >= pixel_h {
                    return Err(invalid("Word 그림의 잘라내기 범위가 올바르지 않습니다."));
                }
                if left | top | right | bottom != 0 {
                    let decoded = image::load_from_memory(&bytes).map_err(io::Error::other)?;
                    let clipped =
                        decoded.crop_imm(left, top, pixel_w - left - right, pixel_h - top - bottom);
                    let mut saved = Cursor::new(Vec::new());
                    clipped
                        .write_to(&mut saved, image::ImageFormat::Png)
                        .map_err(io::Error::other)?;
                    bytes = saved.into_inner();
                    mime = "image/png";
                }
            }
            let scale = (available.max(1.0) / width.max(1.0)).min(1.0);
            use std::fmt::Write as _;
            let clip = if self.layout {
                ["l", "t", "r", "b"].map(|key| {
                    crop.and_then(|node| node.attribute(key))
                        .and_then(|value| value.parse::<i32>().ok())
                        .unwrap_or_default()
                })
            } else {
                [0; 4]
            };
            let [l, t, r, b] = clip;
            let geometry = if self.layout {
                format!(
                    r#" data-width="{width}" data-height="{height}" data-crop="{l} {t} {r} {b}""#
                )
            } else {
                String::new()
            };
            write!(
                output,
                r#"<img src="data:{mime};base64,{}" width="{}" height="{}"{geometry}>"#,
                base64::engine::general_purpose::STANDARD.encode(bytes),
                (width * scale / 0.75).round_ties_even(),
                (height * scale / 0.75).round_ties_even()
            )
            .unwrap();
            if output.len() > MAX_PACKAGE as usize {
                return Err(invalid("Word HTML 출력이 크기 한도를 초과했습니다."));
            }
        }
        if !found {
            self.warn("일부 Word 그리기 개체는 변환할 수 없어 생략됩니다.");
        }
        Ok(())
    }

    fn equation(&mut self, expression: Node<'_, '_>, block: &mut Block) {
        let mut supported = true;
        let value = math(expression, &mut supported, 0);
        self.equations += 1;
        if !supported {
            self.warn("일부 Office 수식 구조는 한글 출력에서 선형식으로 단순화됩니다.");
        }
        block.text.push_str(&value);
        if self.html {
            let marker = if self.layout {
                " data-equation=\"1\""
            } else {
                ""
            };
            block.html.push_str(&format!(
                "<span{marker}>{}</span>",
                safe_html::escape(&value)
            ));
        }
    }

    fn run(&mut self, run: Node<'_, '_>, available: f64, block: &mut Block) -> io::Result<()> {
        let properties = node(run, "rPr");
        let paragraph = run.ancestors().find(|node| node.has_tag_name((WORD, "p")));
        let char_style = val(properties.and_then(|props| node(props, "rStyle")));
        let para_style = val(paragraph.and_then(|para| path(para, &["pPr", "pStyle"])));
        let char_styles = self.styles.chain(char_style, "character");
        let para_styles = self.styles.chain(para_style, "paragraph");
        let prop = |name| {
            properties.and_then(|props| node(props, name)).or_else(|| {
                char_styles
                    .iter()
                    .chain(&para_styles)
                    .copied()
                    .chain(self.styles.document)
                    .find_map(|props| node(props, name))
            })
        };
        let size = val(prop("sz"))
            .and_then(|value| value.parse::<f64>().ok())
            .filter(|size| *size > 0.0);
        let size = size.map_or_else(|| "10".to_owned(), |size| format!("{:?}", size / 2.0));
        let bold =
            prop("b").is_some_and(|node| !matches!(attr(node, "val"), Some("0" | "false" | "off")));
        let color = val(prop("color"))
            .filter(|color| {
                color.len() == 6 && color.bytes().all(|value| value.is_ascii_hexdigit())
            })
            .unwrap_or("000000");
        let east_asian = run
            .children()
            .filter(|child| child.has_tag_name((WORD, "t")))
            .filter_map(|child| child.text())
            .flat_map(str::chars)
            .any(|ch| matches!(u32::from(ch), 0x1100..=0x11ff | 0x2e80..=0xd7ff | 0xf900..=0xfaff));
        let keys = if east_asian {
            ["eastAsia", "ascii", "hAnsi"]
        } else {
            ["ascii", "hAnsi", "eastAsia"]
        };
        let font = keys
            .into_iter()
            .find_map(|key| {
                properties
                    .and_then(|prop| node(prop, "rFonts"))
                    .and_then(|fonts| attr(fonts, key))
                    .or_else(|| {
                        char_styles
                            .iter()
                            .chain(&para_styles)
                            .copied()
                            .chain(self.styles.document)
                            .find_map(|prop| {
                                node(prop, "rFonts").and_then(|fonts| attr(fonts, key))
                            })
                    })
            })
            .or_else(|| {
                let [major, minor] = self.textbox_fonts.as_ref()?;
                let fonts = prop("rFonts")?;
                let selection = attr(fonts, "asciiTheme").or_else(|| attr(fonts, "hAnsiTheme"))?;
                if east_asian {
                    return None;
                }
                let font = if selection.starts_with("major") {
                    major
                } else {
                    minor
                };
                (!font.is_empty()).then_some(font.as_str())
            })
            .unwrap_or("맑은 고딕");
        let font = match font {
            "맑은 고딕" => "Malgun Gothic",
            "돋움" => "Dotum",
            "굴림" => "Gulim",
            "바탕" => "Batang",
            name => name,
        };
        let font = safe_html::escape(font);
        let italic =
            prop("i").is_some_and(|node| !matches!(attr(node, "val"), Some("0" | "false" | "off")));
        let layout = if self.layout {
            let metric = |key, default| {
                val(prop(key))
                    .and_then(|value| value.parse::<i32>().ok())
                    .unwrap_or(default)
            };
            let spacing = metric("spacing", 0);
            let scale = metric("w", 100);
            format!(
                r#" data-font="{font}" data-size="{size}" data-bold="{}" data-italic="{}" data-color="{color}" data-tracking="{spacing}" data-scale="{scale}""#,
                u8::from(bold),
                u8::from(italic)
            )
        } else {
            String::new()
        };
        let html = self.html;
        let italic_css = if self.in_textbox && italic {
            "font-style:italic;"
        } else {
            ""
        };
        let add_text = |block: &mut Block, value: &str| {
            block.text.push_str(value);
            if html {
                block.html.push_str(&format!("<span{layout} style=\"font-family:'{}';font-size:{size}pt;font-weight:{};{italic_css}color:#{color}\">{}</span>",
                    font, if bold { 700 } else { 400 }, safe_html::escape(value).replace('\n', "<br>")));
            }
        };
        for child in run.children().filter(Node::is_element) {
            match child.tag_name().name() {
                "t" => add_text(block, child.text().unwrap_or_default()),
                "tab" => add_text(block, "\t"),
                "br" | "cr" => add_text(block, "\n"),
                "drawing" => self.picture(child, available, &mut block.html)?,
                "AlternateContent" if self.layout => {
                    if let Some(drawing) = child
                        .children()
                        .filter(|item| {
                            item.has_tag_name((
                                "http://schemas.openxmlformats.org/markup-compatibility/2006",
                                "Choice",
                            ))
                        })
                        .find_map(|choice| node(choice, "drawing"))
                    {
                        self.picture(drawing, available, &mut block.html)?;
                    }
                }
                "oMath" => self.equation(child, block),
                _ => {}
            }
        }
        Ok(())
    }

    fn paragraph(&mut self, paragraph: Node<'_, '_>, available: f64) -> io::Result<Block> {
        let mut block = Block::default();
        let alignment = val(path(paragraph, &["pPr", "jc"])).unwrap_or("left");
        let alignment = match alignment {
            "center" | "right" => alignment,
            "both" => "justify",
            _ => "left",
        };
        if self.html {
            let layout = if self.layout {
                let pr = node(paragraph, "pPr");
                let mut properties: Vec<_> = pr.into_iter().collect();
                let mut style = val(pr.and_then(|props| node(props, "pStyle")))
                    .or_else(|| self.styles.defaults.get("paragraph").copied());
                for _ in 0..64 {
                    let Some(item) = style.and_then(|id| self.styles.entries.get(id)) else {
                        break;
                    };
                    properties.extend(node(*item, "pPr"));
                    style = val(node(*item, "basedOn"));
                }
                let property = |name, key| {
                    properties
                        .iter()
                        .filter_map(|props| node(*props, name))
                        .find_map(|item| attr(item, key))
                };
                let number = |name, key| {
                    property(name, key)
                        .and_then(|value| value.parse::<i32>().ok())
                        .unwrap_or_default()
                };
                let left = number("ind", "left");
                let right = number("ind", "right");
                let indent = number("ind", "firstLine").saturating_sub(number("ind", "hanging"));
                let before = number("spacing", "before");
                let after = number("spacing", "after");
                let line = number("spacing", "line");
                let rule = property("spacing", "lineRule")
                    .filter(|rule| matches!(*rule, "exact" | "atLeast"))
                    .unwrap_or("auto");
                let page = pr
                    .and_then(|node_| node(node_, "pageBreakBefore"))
                    .is_some_and(|node| !matches!(attr(node, "val"), Some("0" | "false" | "off")));
                format!(
                    r#" data-indent="{left} {right} {indent}" data-space="{before} {after} {line}" data-rule="{rule}" data-page="{}""#,
                    u8::from(page)
                )
            } else {
                String::new()
            };
            block.html.push_str(&format!(
                "<p align=\"{alignment}\" style=\"margin:0\"{layout}>"
            ));
        }
        for child in paragraph.children().filter(Node::is_element) {
            match child.tag_name().name() {
                "r" => self.run(child, available, &mut block)?,
                "hyperlink" | "fldSimple"
                    if child.has_tag_name((WORD, "hyperlink"))
                        || attr(child, "instr")
                            .is_some_and(|value| value.trim().eq_ignore_ascii_case("PAGE")) =>
                {
                    let length = block.text.len();
                    for run in child.children().filter(|run| run.has_tag_name((WORD, "r"))) {
                        self.run(run, available, &mut block)?;
                    }
                    if child.has_tag_name((WORD, "fldSimple")) && block.text.len() == length {
                        block.text.push('1');
                    }
                }
                "oMath" => self.equation(child, &mut block),
                "oMathPara" => {
                    for eq in child
                        .children()
                        .filter(|eq| eq.has_tag_name((MATH, "oMath")))
                    {
                        self.equation(eq, &mut block);
                    }
                }
                _ => {}
            }
        }
        if self.html {
            block.html.push_str("</p>");
        }
        Ok(block)
    }

    fn table(&mut self, table: Node<'_, '_>, available: f64, depth: usize) -> io::Result<Block> {
        self.tables += 1;
        let mut result = Block::default();
        let widths: Vec<_> = node(table, "tblGrid")
            .into_iter()
            .flat_map(|grid| grid.children())
            .filter(|column| column.has_tag_name((WORD, "gridCol")))
            .map(|column| {
                attr(column, "w")
                    .and_then(|value| value.parse::<f64>().ok())
                    .map_or(72.0, |width| width / 20.0)
            })
            .collect();
        let total: f64 = widths.iter().sum();
        let scale = (available.max(1.0) / total.max(1.0)).min(1.0);
        let rows: Vec<_> = table
            .children()
            .filter(|row| row.has_tag_name((WORD, "tr")))
            .collect();
        let table_properties = node(table, "tblPr");
        let mut table_styles = Vec::new();
        table_styles.extend(table_properties);
        let mut style = val(table_properties.and_then(|props| node(props, "tblStyle")))
            .or_else(|| self.styles.defaults.get("table").copied());
        for _ in 0..64 {
            let Some(entry) = style.and_then(|id| self.styles.entries.get(id)) else {
                break;
            };
            table_styles.extend(node(*entry, "tblPr"));
            style = val(node(*entry, "basedOn"));
        }
        let table_property = |key| table_styles.iter().find_map(|item| node(*item, key));
        let borders = table_property("tblBorders");
        let margins = table_property("tblCellMar");
        if self.html {
            let layout = if self.layout {
                let widths = widths
                    .iter()
                    .map(|width| ((width * 20.0).round_ties_even() as i64).to_string())
                    .collect::<Vec<_>>()
                    .join(" ");
                let align = val(table_properties.and_then(|props| node(props, "jc")))
                    .filter(|align| matches!(*align, "left" | "center" | "right"));
                let align = align.map_or_else(String::new, |align| format!(r#" align="{align}""#));
                format!(r#" data-columns="{widths}"{align}"#)
            } else {
                String::new()
            };
            result.html.push_str(&format!(
                r#"<table border="1" width="100%" cellspacing="0" cellpadding="3"{layout}>"#
            ));
        }
        for (index, row) in rows.iter().copied().enumerate() {
            (self.check)()?;
            if index != 0 {
                result.text.push('\n');
            }
            if self.html {
                if self.layout {
                    let height = path(row, &["trPr", "trHeight"])
                        .and_then(|item| attr(item, "val"))
                        .and_then(|value| value.parse::<i32>().ok())
                        .filter(|height| *height > 0)
                        .unwrap_or(240);
                    result
                        .html
                        .push_str(&format!(r#"<tr data-height="{height}">"#));
                } else {
                    result.html.push_str("<tr>");
                }
            }
            let mut column = row_start(row);
            let mut first = true;
            for cell in row
                .children()
                .filter(|cell| cell.has_tag_name((WORD, "tc")))
            {
                let colspan = span(cell);
                let start = column;
                column = column.saturating_add(colspan);
                let merged = path(cell, &["tcPr", "vMerge"]);
                if merged.is_some_and(|merge| attr(merge, "val") != Some("restart")) {
                    continue;
                }
                let rowspan = if merged.is_some() {
                    1 + rows[index + 1..]
                        .iter()
                        .take_while(|row| continues(**row, start, colspan))
                        .count()
                } else {
                    1
                };
                let width = widths
                    .get(start..column)
                    .map_or(available, |widths| widths.iter().sum::<f64>() * scale);
                let output = self.blocks(cell, (width - 8.0).max(1.0), depth + 1)?;
                if !first {
                    result.text.push('\t');
                }
                first = false;
                result.text.push_str(&output.text);
                if self.html {
                    let fill = path(cell, &["tcPr", "shd"])
                        .and_then(|shade| attr(shade, "fill"))
                        .filter(|fill| {
                            fill.len() == 6 && fill.bytes().all(|value| value.is_ascii_hexdigit())
                        });
                    let fill =
                        fill.map_or_else(String::new, |fill| format!(" bgcolor=\"#{fill}\""));
                    let col = if self.layout {
                        let props = node(cell, "tcPr");
                        let local_border = props.and_then(|item| node(item, "tcBorders"));
                        let local_margin = props.and_then(|item| node(item, "tcMar"));
                        let vertical = val(props.and_then(|item| node(item, "vAlign")))
                            .filter(|value| matches!(*value, "center" | "bottom"))
                            .unwrap_or("top");
                        let mut values = String::new();
                        let mut inset = Vec::new();
                        for (edge, alias, outside, fallback, padding) in [
                            ("top", "top", index == 0, "insideH", 0),
                            ("left", "start", start == 0, "insideV", 108),
                            (
                                "bottom",
                                "bottom",
                                index + rowspan == rows.len(),
                                "insideH",
                                0,
                            ),
                            ("right", "end", column == widths.len(), "insideV", 108),
                        ] {
                            let line = edge_node(local_border, edge, alias).or_else(|| {
                                if outside {
                                    edge_node(borders, edge, alias)
                                } else {
                                    borders.and_then(|p| node(p, fallback))
                                }
                            });
                            let line_style = val(line)
                                .filter(|value| value.bytes().all(|b| b.is_ascii_alphabetic()))
                                .unwrap_or("nil");
                            let size = line
                                .and_then(|line| attr(line, "sz"))
                                .and_then(|v| v.parse::<u32>().ok())
                                .unwrap_or(4)
                                .clamp(2, 96);
                            let color = line
                                .and_then(|line| attr(line, "color"))
                                .and_then(crate::hwp_model::color)
                                .unwrap_or("000000");
                            use std::fmt::Write as _;
                            write!(values, "{line_style} {size} {color} ").unwrap();
                            inset.push(
                                edge_node(local_margin, edge, alias)
                                    .or_else(|| edge_node(margins, edge, alias))
                                    .and_then(|item| attr(item, "w"))
                                    .and_then(|v| v.parse::<i32>().ok())
                                    .unwrap_or(padding)
                                    .clamp(0, 100_000)
                                    .to_string(),
                            );
                        }
                        let inset = inset.join(" ");
                        format!(
                            r#" data-col="{start}" data-borders="{values}" data-margins="{inset}" data-valign="{vertical}""#
                        )
                    } else {
                        String::new()
                    };
                    result.html.push_str(&format!(
                        "<td rowspan=\"{rowspan}\" colspan=\"{colspan}\"{fill}{col}>{}</td>",
                        output.html
                    ));
                }
            }
            if self.html {
                result.html.push_str("</tr>");
            }
        }
        if self.html {
            result.html.push_str("</table>");
        }
        Ok(result)
    }

    fn blocks(&mut self, root: Node<'_, '_>, available: f64, depth: usize) -> io::Result<Block> {
        if depth >= 32 {
            return Err(invalid("Word 표 중첩이 너무 깊습니다."));
        }
        let mut blocks = Block::default();
        let mut first = true;
        for child in root.children().filter(Node::is_element) {
            (self.check)()?;
            let rendered = if child.has_tag_name((WORD, "p")) {
                self.paragraph(child, available)?
            } else if child.has_tag_name((WORD, "tbl")) {
                self.table(child, available, depth)?
            } else {
                continue;
            };
            if !first {
                blocks.text.push('\n');
            }
            first = false;
            blocks.text.push_str(&rendered.text);
            blocks.html.push_str(&rendered.html);
            if blocks.text.len() + blocks.html.len() > MAX_PACKAGE as usize {
                return Err(invalid("Word 문서 모델이 크기 한도를 초과했습니다."));
            }
        }
        Ok(blocks)
    }
}

fn row_start(row: Node<'_, '_>) -> usize {
    val(path(row, &["trPr", "gridBefore"]))
        .and_then(|value| value.parse().ok())
        .unwrap_or(0)
}

fn span(cell: Node<'_, '_>) -> usize {
    val(path(cell, &["tcPr", "gridSpan"]))
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(1)
        .max(1)
}

fn continues(row: Node<'_, '_>, start: usize, colspan: usize) -> bool {
    let mut column = row_start(row);
    for cell in row
        .children()
        .filter(|cell| cell.has_tag_name((WORD, "tc")))
    {
        let span = span(cell);
        if column == start {
            return span == colspan
                && path(cell, &["tcPr", "vMerge"])
                    .is_some_and(|node| attr(node, "val") != Some("restart"));
        }
        column = column.saturating_add(span);
    }
    false
}

fn read(
    source: &Path,
    html: bool,
    layout: bool,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<(Block, [usize; 3])> {
    let mut archive = package(source, check)?;
    let bytes = read_part(&mut archive, MAIN)?;
    let document = parse(std::str::from_utf8(&bytes).map_err(io::Error::other)?)?;
    let body = node(document.root_element(), "body")
        .ok_or_else(|| invalid("Word 문서 본문을 찾을 수 없습니다."))?;
    let sections: Vec<_> = body
        .descendants()
        .filter(|child| child.has_tag_name((WORD, "sectPr")))
        .collect();
    if sections.len() > 1 {
        warnings
            .push("다중 구역의 서로 다른 용지·여백 설정은 첫 구역 기준으로 변환합니다.".to_owned());
    }
    let part_refs = relationships(&mut archive, MAIN)?;
    let style_bytes = if archive.index_for_name("word/styles.xml").is_some() {
        read_part(&mut archive, "word/styles.xml")?
    } else {
        b"<styles/>".to_vec()
    };
    let style_document = parse(std::str::from_utf8(&style_bytes).map_err(io::Error::other)?)?;
    let available = sections
        .first()
        .and_then(|section| {
            let size = node(*section, "pgSz")?;
            let margin = node(*section, "pgMar")?;
            let points = |element, name| attr(element, name)?.parse::<f64>().ok();
            Some((points(size, "w")? - points(margin, "left")? - points(margin, "right")?) / 20.0)
        })
        .unwrap_or(468.0);
    let mut renderer = Renderer {
        archive: &mut archive,
        check,
        warnings,
        rels: part_refs.clone(),
        html,
        layout,
        tables: 0,
        pictures: 0,
        equations: 0,
        styles: Styles::new(&style_document),
        textbox_fonts: None,
        in_textbox: false,
    };
    let mut parts = Vec::new();
    let mut table_objects = [0; 3];
    for slot in ["headerReference", "main", "footerReference"] {
        if slot == "main" {
            renderer.rels = part_refs.clone();
            parts.push(renderer.blocks(body, available, 0)?);
            table_objects = [renderer.tables, renderer.pictures, renderer.equations];
        } else if let Some(part) = sections
            .iter()
            .find_map(|section| {
                section.children().find(|item| {
                    item.has_tag_name((WORD, slot)) && attr(*item, "type") == Some("default")
                })
            })
            .and_then(|item| item.attribute((REL, "id")))
            .and_then(|id| part_refs.get(id))
        {
            let bytes = read_part(renderer.archive, part)?;
            let doc = parse(std::str::from_utf8(&bytes).map_err(io::Error::other)?)?;
            renderer.rels = relationships(renderer.archive, part)?;
            let mut part = renderer.blocks(doc.root_element(), available, 0)?;
            if layout {
                let tag = if slot == "headerReference" {
                    "header"
                } else {
                    "footer"
                };
                part.html = format!("<{tag}>{}</{tag}>", part.html);
            }
            parts.push(part);
        }
    }
    if body.descendants().any(|node| {
        matches!(
            node.tag_name().name(),
            "object" | "pict" | "AlternateContent" | "del" | "ins"
        )
    }) {
        renderer
            .warn("문서 모델은 일부 포함 개체·VML·호환 개체·변경 추적을 완전히 보존하지 않습니다.");
    }
    let found = body
        .descendants()
        .filter(|node| node.has_tag_name((MATH, "oMath")))
        .count();
    if found > table_objects[2] {
        renderer.warn(&format!(
            "Office 수식 손실 검사: 본문 원본 {found}개 / 문서 모델 {}개.",
            table_objects[2]
        ));
    }
    let text = parts
        .iter()
        .map(|part| part.text.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    let mut html: String = parts.into_iter().map(|part| part.html).collect();
    if layout && let Some(section) = sections.first() {
        let defaults = [11906, 16837, 1440, 1440, 1440, 1440, 720, 720];
        let geometry = [
            "w", "h", "left", "right", "top", "bottom", "header", "footer",
        ]
        .iter()
        .enumerate()
        .map(|(index, name)| {
            node(*section, if index < 2 { "pgSz" } else { "pgMar" })
                .and_then(|item| attr(item, name))
                .and_then(|value| value.parse::<i32>().ok())
                .unwrap_or(defaults[index])
                .to_string()
        })
        .collect::<Vec<_>>()
        .join(" ");
        html = format!(r#"<main data-geometry="{geometry}">{html}</main>"#);
    }
    Ok((
        Block { text, html },
        [renderer.tables, renderer.pictures, renderer.equations],
    ))
}

pub fn supports(edge: &Edge) -> bool {
    edge.engine_id == "documents"
        && matches!(edge.source.as_str(), "docx" | "docm" | "dotx" | "dotm")
        && matches!(
            edge.target.as_str(),
            "docx" | "txt" | "md" | "html" | "epub"
        )
}

pub(crate) fn rich_html(source: &Path, check: Check<'_>) -> io::Result<String> {
    let (result, _) = read(source, true, false, check, &mut Vec::new())?;
    Ok(result.html)
}

pub(crate) fn layout_html(
    source: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<String> {
    let (result, _) = read(source, true, true, check, warnings)?;
    Ok(result.html)
}

pub fn convert(
    source: &Path,
    output: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let variant = edge.source != "docx";
    if variant {
        warnings.push(VARIANT_WARNING.to_owned());
    }
    if edge.target == "docx" {
        office_package::convert(source, output, office_package::Action::Standardize, check)?;
        warnings.push("DOCX를 표준 OOXML 패키지로 다시 작성했습니다. 매크로·ActiveX·포함 개체·패키지 서명은 제외했습니다.".to_owned());
        return Ok(());
    }
    let stage;
    let source = if variant {
        stage = tempfile::Builder::new()
            .prefix("magic-office-model-")
            .tempdir()?;
        let target = stage.path().join("document.docx");
        office_package::convert(source, &target, office_package::Action::Standardize, check)?;
        target
    } else {
        source.to_owned()
    };
    let (result, counts) = read(&source, edge.target == "html", false, check, warnings)?;
    if edge.target != "html" {
        warnings.push(MODEL_WARNING.to_owned());
        crate::text_documents::write_pdf_text(&source, output, &result.text, &edge.target)
    } else {
        if counts[2] != 0 {
            warnings.push("HTML 수식은 스크립트 텍스트로 출력합니다.".to_owned());
        }
        fs::write(
            output,
            format!(
                "<html><meta charset='utf-8'><body>{}</body></html>",
                result.html
            ),
        )
    }
}
