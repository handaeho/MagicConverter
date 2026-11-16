use std::collections::{BTreeMap, HashMap};
use std::fmt::Write as _;
use std::fs::File;
use std::io::{self, BufWriter, ErrorKind, Read, Write};
use std::path::Path;
use std::sync::LazyLock;
use unicode_bidi::{BidiClass, BidiInfo, Level, bidi_class};
use unicode_properties::{GeneralCategory, UnicodeGeneralCategory};

const WIDTH: f64 = 842.0;
const USABLE: f64 = WIDTH - 48.0;
const LANES: usize = 59;
const MAX_PAGES: usize = 100_000;
type Check<'a> = &'a dyn Fn() -> io::Result<()>;

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

pub fn large(path: &Path, check: Check<'_>) -> io::Result<bool> {
    if !path.is_file() {
        return Ok(false);
    }
    if path.metadata()?.len() >= 16 * 1024 * 1024 {
        return Ok(true);
    }
    static DIMENSION: LazyLock<regex::bytes::Regex> = LazyLock::new(|| {
        regex::bytes::Regex::new(r#"<(?:\w+:)?dimension\b[^>]*\bref=["'][^"']*?([0-9]+)["']"#)
            .unwrap()
    });
    let mut package = zip::ZipArchive::new(File::open(path)?)?;
    for index in 0..package.len() {
        check()?;
        let file = package.by_index(index)?;
        let name = file.name()?;
        if !name.starts_with("xl/worksheets/sheet") || !name.ends_with(".xml") {
            continue;
        }
        let mut header = Vec::new();
        file.take(65536).read_to_end(&mut header)?;
        if let Some(capture) = DIMENSION.captures(&header)
            && std::str::from_utf8(&capture[1])
                .ok()
                .and_then(|value| value.parse::<u32>().ok())
                .is_some_and(|row| row >= 100_000)
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[derive(Clone, Copy)]
struct Glyph {
    code: u16,
    index: u16,
    advance: f64,
}

struct Font {
    data: &'static [u8],
    face: ttf_parser::Face<'static>,
    glyphs: HashMap<char, Glyph>,
    used: Vec<(char, Glyph)>,
}

impl Font {
    fn new() -> io::Result<Self> {
        let data = mupdf_fonts_droid::cjk_font(0, false)
            .ok_or_else(|| invalid("PDF 출력 글꼴을 찾지 못했습니다."))?
            .data;
        let face = ttf_parser::Face::parse(data, 0).map_err(io::Error::other)?;
        Ok(Self {
            data,
            face,
            glyphs: HashMap::new(),
            used: Vec::new(),
        })
    }

    fn glyph(&mut self, ch: char) -> io::Result<Glyph> {
        if let Some(&glyph) = self.glyphs.get(&ch) {
            return Ok(glyph);
        }
        let missing = || {
            invalid(format!(
                "대형 표 PDF 출력 글꼴에 U+{:04X} 글자가 없습니다.",
                ch as u32
            ))
        };
        let glyph = self.face.glyph_index(ch).ok_or_else(missing)?;
        let units = self.face.glyph_hor_advance(glyph).ok_or_else(missing)?;
        let advance = f64::from(units) / f64::from(self.face.units_per_em());
        let code = u16::try_from(self.used.len() + 1).map_err(io::Error::other)?;
        let value = Glyph {
            code,
            index: glyph.0,
            advance,
        };
        self.used.push((ch, value));
        self.glyphs.insert(ch, value);
        Ok(value)
    }

    fn width(&mut self, text: &str) -> io::Result<f64> {
        text.chars()
            .try_fold(0.0, |sum, ch| Ok(sum + self.glyph(ch)?.advance * 7.0))
    }

    fn encode(&mut self, text: &str, output: &mut String) -> io::Result<()> {
        for ch in text.chars() {
            write!(output, "{:04X}", self.glyph(ch)?.code).unwrap();
        }
        Ok(())
    }
}

struct Fallback {
    name: &'static str,
    data: &'static [u8],
    face: rustybuzz::Face<'static>,
    used: BTreeMap<u16, char>,
}

impl Fallback {
    fn new(name: &'static str, family: &str) -> io::Result<Self> {
        let data = mupdf_fonts_noto::find_by_name(family, false, false)
            .ok_or_else(|| invalid(format!("대형 표 PDF 출력 글꼴을 찾지 못했습니다: {family}")))?
            .data;
        let face = rustybuzz::Face::from_slice(data, 0).ok_or_else(|| {
            invalid(format!(
                "대형 표 PDF 출력 글꼴을 읽을 수 없습니다: {family}"
            ))
        })?;
        Ok(Self {
            name,
            data,
            face,
            used: BTreeMap::new(),
        })
    }

    fn advance(&self, glyph: u16) -> f64 {
        f64::from(
            self.face
                .glyph_hor_advance(ttf_parser::GlyphId(glyph))
                .unwrap_or(0),
        ) / f64::from(self.face.units_per_em())
    }
}

fn complex(ch: char) -> bool {
    !ch.is_ascii()
        && (matches!(bidi_class(ch), BidiClass::R | BidiClass::AL | BidiClass::AN)
            || matches!(
                ch.general_category(),
                GeneralCategory::NonspacingMark
                    | GeneralCategory::SpacingMark
                    | GeneralCategory::EnclosingMark
                    | GeneralCategory::Format
            ))
}

struct PdfOutput {
    file: BufWriter<File>,
    position: u64,
}

impl Write for PdfOutput {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let written = self.file.write(bytes)?;
        self.position += written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

struct Writer {
    output: PdfOutput,
    objects: Vec<u64>,
    pages: Vec<usize>,
    font: Font,
    fallbacks: Vec<Fallback>,
}

impl Writer {
    fn new(path: &Path) -> io::Result<Self> {
        let mut output = PdfOutput {
            file: BufWriter::with_capacity(64 * 1024, File::create(path)?),
            position: 0,
        };
        output.write_all(b"%PDF-1.7\n%\xE2\xE3\xCF\xD3\n")?;
        Ok(Self {
            output,
            objects: vec![0; 10],
            pages: Vec::new(),
            font: Font::new()?,
            fallbacks: vec![
                Fallback::new("NotoNaskhArabic", "Noto Naskh Arabic")?,
                Fallback::new("NotoSerifHebrew", "Noto Serif Hebrew")?,
                Fallback::new("NotoSerifThai", "Noto Serif Thai")?,
                Fallback::new("NotoSans", "Noto Sans")?,
            ],
        })
    }

    fn object(&mut self, id: usize) -> io::Result<()> {
        if self.objects.len() <= id {
            self.objects.resize(id + 1, 0);
        }
        self.objects[id] = self.output.position;
        writeln!(self.output, "{id} 0 obj")
    }

    fn dictionary(&mut self, id: usize, value: std::fmt::Arguments<'_>) -> io::Result<()> {
        self.object(id)?;
        writeln!(self.output, "<<{value}>>\nendobj")
    }

    fn stream(&mut self, id: usize, extra: &str, bytes: &[u8]) -> io::Result<()> {
        let mut compressed =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::new(7));
        compressed.write_all(bytes)?;
        let bytes = compressed.finish()?;
        self.object(id)?;
        writeln!(
            self.output,
            "<< /Length {} /Filter /FlateDecode {extra} >>\nstream",
            bytes.len()
        )?;
        self.output.write_all(&bytes)?;
        self.output.write_all(b"\nendstream\nendobj\n")
    }

    fn family(&self, ch: char) -> Option<usize> {
        if self.font.face.glyph_index(ch).is_some() {
            return Some(0);
        }
        self.fallbacks
            .iter()
            .position(|font| font.face.glyph_index(ch).is_some())
            .map(|index| index + 1)
    }

    fn advance(&mut self, ch: char) -> io::Result<f64> {
        if ch.is_ascii_control() {
            return Ok(0.0);
        }
        if ch.is_ascii() || self.font.glyphs.contains_key(&ch) {
            return Ok(self.font.glyph(ch)?.advance * 7.0);
        }
        if let Some(index) = self.family(ch) {
            if index == 0 {
                return Ok(self.font.glyph(ch)?.advance * 7.0);
            }
            let font = &self.fallbacks[index - 1];
            return Ok(font.advance(font.face.glyph_index(ch).unwrap().0) * 7.0);
        }
        if matches!(
            ch.general_category(),
            GeneralCategory::Control | GeneralCategory::Format
        ) {
            return Ok(0.0);
        }
        Err(invalid(format!(
            "대형 표 PDF 출력 글꼴에 U+{:04X} 글자가 없습니다.",
            ch as u32
        )))
    }

    fn width(&mut self, text: &str) -> io::Result<f64> {
        text.chars()
            .try_fold(0.0, |sum, ch| Ok(sum + self.advance(ch)?))
    }

    fn line(
        &mut self,
        text: &str,
        mut x: f64,
        y: f64,
        size: f64,
        output: &mut String,
    ) -> io::Result<()> {
        if text.chars().all(|ch| {
            !ch.is_ascii_control() && !complex(ch) && (ch.is_ascii() || self.family(ch) == Some(0))
        }) {
            write!(output, "/F0 {size} Tf 1 0 0 1 {x:.5} {y:.5} Tm <").unwrap();
            self.font.encode(text, output)?;
            output.push_str("> Tj\n");
            return Ok(());
        }
        let bidi = BidiInfo::new(text, Some(Level::ltr()));
        for paragraph in &bidi.paragraphs {
            let (levels, runs) = bidi.visual_runs(paragraph, paragraph.range.clone());
            for run in runs {
                let rtl = levels[run.start].is_rtl();
                let mut parts: Vec<(usize, String)> = Vec::new();
                for ch in text[run].chars() {
                    let Some(index) = self.family(ch) else {
                        continue;
                    };
                    if let Some((family, value)) = parts.last_mut()
                        && *family == index
                    {
                        value.push(ch);
                    } else {
                        parts.push((index, ch.to_string()));
                    }
                }
                if rtl {
                    parts.reverse();
                }
                for (family, part) in parts {
                    if family == 0 {
                        write!(output, "/F0 {size} Tf 1 0 0 1 {x:.5} {y:.5} Tm <").unwrap();
                        if rtl {
                            self.font
                                .encode(&part.chars().rev().collect::<String>(), output)?;
                        } else {
                            self.font.encode(&part, output)?;
                        }
                        output.push_str("> Tj\n");
                        x += self.font.width(&part)? * size / 7.0;
                        continue;
                    }
                    let font = &mut self.fallbacks[family - 1];
                    let mut buffer = rustybuzz::UnicodeBuffer::new();
                    buffer.push_str(&part);
                    buffer.set_direction(if rtl {
                        rustybuzz::Direction::RightToLeft
                    } else {
                        rustybuzz::Direction::LeftToRight
                    });
                    buffer.guess_segment_properties();
                    let shaped = rustybuzz::shape(&font.face, &[], buffer);
                    let scale = size / f64::from(font.face.units_per_em());
                    writeln!(output, "/F{family} {size} Tf").unwrap();
                    for (glyph, pos) in shaped.glyph_infos().iter().zip(shaped.glyph_positions()) {
                        let id = u16::try_from(glyph.glyph_id).map_err(io::Error::other)?;
                        let ch = part
                            .get(glyph.cluster as usize..)
                            .and_then(|tail| tail.chars().next())
                            .unwrap_or(' ');
                        font.used.entry(id).or_insert(ch);
                        let left = x + f64::from(pos.x_offset) * scale;
                        let baseline = y + f64::from(pos.y_offset) * scale;
                        writeln!(output, "1 0 0 1 {left:.5} {baseline:.5} Tm <{id:04X}> Tj")
                            .unwrap();
                        x += f64::from(pos.x_advance) * scale;
                    }
                }
            }
        }
        Ok(())
    }

    fn simple_page(
        &mut self,
        header: &str,
        columns: usize,
        lines: &[String],
    ) -> io::Result<Vec<u8>> {
        let mut content = Vec::with_capacity(4096);
        content.extend_from_slice(b"BT /F0 8 Tf 24 565 Td ");
        self.literal(header, &mut content)?;
        content.extend_from_slice(b"/F0 7 Tf 9 TL\n");
        for (index, column) in lines.chunks(LANES).enumerate() {
            let x = 24.0 + index as f64 * USABLE / columns as f64;
            write!(content, "1 0 0 1 {x:.5} 549 Tm ")?;
            for (row, line) in column.iter().enumerate() {
                if row != 0 {
                    content.extend_from_slice(b"T* ");
                }
                self.literal(line, &mut content)?;
            }
        }
        content.extend_from_slice(b"ET\n");
        Ok(content)
    }

    fn literal(&mut self, text: &str, output: &mut Vec<u8>) -> io::Result<()> {
        output.push(b'(');
        for ch in text.chars() {
            for byte in self.font.glyph(ch)?.code.to_be_bytes() {
                match byte {
                    b'(' | b')' | b'\\' => output.extend_from_slice(&[b'\\', byte]),
                    b'\r' => output.extend_from_slice(b"\\r"),
                    b'\n' => output.extend_from_slice(b"\\n"),
                    _ => output.push(byte),
                }
            }
        }
        output.extend_from_slice(b") Tj\n");
        Ok(())
    }

    fn page(
        &mut self,
        header: &str,
        columns: usize,
        lines: &[String],
        check: Check<'_>,
    ) -> io::Result<()> {
        check()?;
        if self.pages.len() >= MAX_PAGES {
            return Err(invalid("대형 표 PDF의 100000쪽 한도를 넘었습니다."));
        }
        let marked = std::iter::once(header)
            .chain(lines.iter().map(String::as_str))
            .any(|line| {
                line.chars().any(|ch| {
                    ch.is_ascii_control()
                        || complex(ch)
                        || (!ch.is_ascii() && self.family(ch) != Some(0))
                })
            });
        let content = if marked {
            self.shaped_page(header, columns, lines)?
        } else {
            self.simple_page(header, columns, lines)?
        };
        let id = self.objects.len();
        self.dictionary(id, format_args!("/Type /Page /Parent 2 0 R /MediaBox [0 0 842 595] /Resources 8 0 R /Contents {} 0 R", id + 1))?;
        self.stream(id + 1, "", &content)?;
        self.pages.push(id);
        Ok(())
    }

    fn shaped_page(
        &mut self,
        header: &str,
        columns: usize,
        lines: &[String],
    ) -> io::Result<Vec<u8>> {
        let mut content = String::new();
        content.push_str("/Span << /ActualText <feff");
        for (index, line) in std::iter::once(header)
            .chain(lines.iter().map(String::as_str))
            .enumerate()
        {
            if index > 0 {
                content.push_str("000a");
            }
            for unit in line.encode_utf16() {
                write!(content, "{unit:04x}").unwrap();
            }
        }
        content.push_str("> /MagicWorkbookText true >> BDC\n");
        content.push_str("BT\n");
        self.line(header, 24.0, 565.0, 8.0, &mut content)?;
        for (number, line) in lines.iter().enumerate() {
            let x = 24.0 + (number / LANES) as f64 * USABLE / columns as f64;
            let y = 549 - (number % LANES) * 9;
            self.line(line, x, y as f64, 7.0, &mut content)?;
        }
        content.push_str("ET\nEMC\n");
        Ok(content.into_bytes())
    }

    fn finish(mut self, check: Check<'_>) -> io::Result<usize> {
        check()?;
        self.dictionary(1, format_args!("/Type /Catalog /Pages 2 0 R"))?;
        self.object(2)?;
        write!(
            self.output,
            "<</Type /Pages /Count {} /Kids [",
            self.pages.len()
        )?;
        for id in &self.pages {
            write!(self.output, "{id} 0 R ")?;
        }
        writeln!(self.output, "]>>\nendobj")?;
        self.dictionary(3, format_args!("/Type /Font /Subtype /Type0 /BaseFont /DroidSansFallback /Encoding /Identity-H /DescendantFonts [4 0 R] /ToUnicode 6 0 R"))?;
        self.object(4)?;
        write!(
            self.output,
            "<</Type /Font /Subtype /CIDFontType2 /BaseFont /DroidSansFallback /CIDSystemInfo <</Registry (Adobe) /Ordering (Identity) /Supplement 0>> /FontDescriptor 5 0 R /CIDToGIDMap 7 0 R /W [1 ["
        )?;
        for (_, glyph) in &self.font.used {
            write!(self.output, "{:.5} ", glyph.advance * 1000.0)?;
        }
        writeln!(self.output, "]]>>\nendobj")?;
        let glyph_bbox = self.font.face.global_bounding_box();
        let bbox = [
            glyph_bbox.x_min,
            glyph_bbox.y_min,
            glyph_bbox.x_max,
            glyph_bbox.y_max,
        ]
        .map(|value| {
            (f64::from(value) * 1000.0 / f64::from(self.font.face.units_per_em())).round() as i32
        });
        self.dictionary(5, format_args!("/Type /FontDescriptor /FontName /DroidSansFallback /Flags 4 /FontBBox [{} {} {} {}] /ItalicAngle 0 /Ascent 1000 /Descent -250 /CapHeight 720 /StemV 90 /FontFile2 9 0 R", bbox[0], bbox[1], bbox[2], bbox[3]))?;
        let mut cmap = "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def\n/CMapName /MagicConverterWorkbook def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n".to_owned();
        let mut gids = vec![0_u8; 2];
        for chunk in self.font.used.chunks(100) {
            check()?;
            writeln!(cmap, "{} beginbfchar", chunk.len()).unwrap();
            for (ch, glyph) in chunk {
                write!(cmap, "<{:04X}> <", glyph.code).unwrap();
                for unit in ch.encode_utf16(&mut [0; 2]) {
                    write!(cmap, "{unit:04X}").unwrap();
                }
                cmap.push_str(">\n");
                gids.extend_from_slice(&glyph.index.to_be_bytes());
            }
            cmap.push_str("endbfchar\n");
        }
        cmap.push_str("endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n");
        self.stream(6, "", cmap.as_bytes())?;
        self.stream(7, "", &gids)?;
        let mut resources = "/Font << /F0 3 0 R".to_owned();
        let fallbacks = std::mem::take(&mut self.fallbacks);
        for (index, font) in fallbacks
            .iter()
            .enumerate()
            .filter(|(_, font)| !font.used.is_empty())
        {
            let id = self.fallback(font, check)?;
            write!(resources, " /F{} {id} 0 R", index + 1).unwrap();
        }
        resources.push_str(" >>");
        self.dictionary(8, format_args!("{resources}"))?;
        let font = self.font.data;
        self.stream(9, &format!("/Length1 {}", font.len()), font)?;
        let xref = self.output.position;
        writeln!(
            self.output,
            "xref\n0 {}\n0000000000 65535 f ",
            self.objects.len()
        )?;
        for &offset in &self.objects[1..] {
            if offset == 0 || offset > 9_999_999_999 {
                return Err(invalid("PDF 오브젝트 위치 범위를 벗어났습니다."));
            }
            writeln!(self.output, "{offset:010} 00000 n ")?;
        }
        writeln!(
            self.output,
            "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF",
            self.objects.len()
        )?;
        self.output.flush()?;
        self.output.file.get_ref().sync_all()?;
        Ok(self.pages.len())
    }

    fn fallback(&mut self, font: &Fallback, check: Check<'_>) -> io::Result<usize> {
        check()?;
        let id = self.objects.len();
        let name = font.name;
        self.dictionary(id, format_args!("/Type /Font /Subtype /Type0 /BaseFont /{name} /Encoding /Identity-H /DescendantFonts [{} 0 R] /ToUnicode {} 0 R", id + 1, id + 3))?;
        self.object(id + 1)?;
        write!(
            self.output,
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{name} /CIDSystemInfo <</Registry (Adobe) /Ordering (Identity) /Supplement 0>> /FontDescriptor {} 0 R /W [",
            id + 2
        )?;
        for &glyph in font.used.keys() {
            write!(
                self.output,
                "{glyph} [{:.5}] ",
                font.advance(glyph) * 1000.0
            )?;
        }
        writeln!(self.output, "] >>\nendobj")?;
        let bounds = font.face.global_bounding_box();
        let unit = 1000.0 / f64::from(font.face.units_per_em());
        let bbox = [bounds.x_min, bounds.y_min, bounds.x_max, bounds.y_max]
            .map(|value| (f64::from(value) * unit).round() as i32);
        let ascent = (f64::from(font.face.ascender()) * unit).round() as i32;
        let descent = (f64::from(font.face.descender()) * unit).round() as i32;
        self.dictionary(id + 2, format_args!("/Type /FontDescriptor /FontName /{name} /Flags 32 /FontBBox [{} {} {} {}] /ItalicAngle 0 /Ascent {ascent} /Descent {descent} /CapHeight 720 /StemV 80 /FontFile3 {} 0 R", bbox[0], bbox[1], bbox[2], bbox[3], id + 4))?;
        let mut cmap = "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CIDSystemInfo <</Registry (Adobe) /Ordering (UCS) /Supplement 0>> def\n/CMapName /MagicConverterWorkbookFallback def\n/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n".to_owned();
        let used: Vec<_> = font.used.iter().collect();
        for chunk in used.chunks(100) {
            writeln!(cmap, "{} beginbfchar", chunk.len()).unwrap();
            for (id, ch) in chunk {
                write!(cmap, "<{id:04X}> <").unwrap();
                for unit in ch.encode_utf16(&mut [0; 2]) {
                    write!(cmap, "{unit:04X}").unwrap();
                }
                cmap.push_str(">\n");
            }
            cmap.push_str("endbfchar\n");
        }
        cmap.push_str("endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n");
        self.stream(id + 3, "", cmap.as_bytes())?;
        self.stream(id + 4, "/Subtype /OpenType", font.data)?;
        Ok(id)
    }
}

struct Row {
    index: u32,
    cells: Vec<(u32, String)>,
}

fn address(col: u32, row: u32) -> String {
    let mut col = col;
    let mut letters = [0_u8; 3];
    let mut count = 0;
    while col > 0 {
        col -= 1;
        letters[2 - count] = b'A' + (col % 26) as u8;
        count += 1;
        col /= 26;
    }
    format!(
        "{}{row}",
        std::str::from_utf8(&letters[3 - count..]).unwrap()
    )
}

fn column_count(writer: &mut Writer, rows: &[Row], check: Check<'_>) -> io::Result<usize> {
    let mut costs = [0_usize; 4];
    let mut common = 0;
    let continuation = writer.width("+999")?;
    for row in rows {
        check()?;
        for (col, text) in &row.cells {
            let prefix = writer.width(&format!("{} ", address(*col, row.index)))?;
            for line in text.split('\n') {
                let width = writer.width(line)?;
                if width <= USABLE / 6.0 - 10.0 - prefix {
                    common += 1;
                    continue;
                }
                for (index, cost) in costs.iter_mut().enumerate() {
                    let available = USABLE / (index + 3) as f64 - 10.0 - prefix;
                    *cost += if width <= available {
                        1
                    } else {
                        (width / (available - continuation).max(1.0))
                            .ceil()
                            .max(1.0) as usize
                    };
                }
            }
        }
    }
    Ok((3..=6)
        .min_by_key(|columns| (common + costs[columns - 3]).div_ceil(LANES * columns))
        .unwrap())
}

fn write_batch(writer: &mut Writer, sheet: &str, rows: &[Row], check: Check<'_>) -> io::Result<()> {
    let columns = column_count(writer, rows, check)?;
    let width = USABLE / columns as f64 - 10.0;
    let header = format!(
        "{sheet} / {}-{} / Cell values and formulas",
        rows[0].index,
        rows[rows.len() - 1].index
    );
    let mut lines = Vec::with_capacity(columns * LANES);
    for row in rows {
        for (col, text) in &row.cells {
            check()?;
            let cell = address(*col, row.index);
            let mut continuation = 0;
            for paragraph in text.split('\n') {
                let mut remaining = paragraph;
                loop {
                    let prefix = if continuation == 0 {
                        format!("{cell} ")
                    } else {
                        format!("{cell}+{continuation} ")
                    };
                    let limit = width - writer.width(&prefix)?;
                    let mut used = 0.0;
                    let mut end = 0;
                    for (offset, ch) in remaining.char_indices() {
                        used += writer.advance(ch)?;
                        if used > limit {
                            break;
                        }
                        end = offset + ch.len_utf8();
                    }
                    if end == 0 && !remaining.is_empty() {
                        return Err(invalid("셀 문자를 PDF 열 너비에 배치할 수 없습니다."));
                    }
                    lines.push(prefix + &remaining[..end]);
                    if lines.len() == columns * LANES {
                        writer.page(&header, columns, &lines, check)?;
                        lines.clear();
                    }
                    continuation += 1;
                    remaining = &remaining[end..];
                    if remaining.is_empty() {
                        break;
                    }
                }
            }
        }
    }
    if !lines.is_empty() {
        writer.page(&header, columns, &lines, check)?;
    }
    Ok(())
}

#[derive(Default)]
struct Totals {
    sheets: usize,
    rows: usize,
    cells: usize,
}

struct Layout {
    writer: Writer,
    sheet: String,
    rows: Vec<Row>,
    width: usize,
    totals: Totals,
}

impl Layout {
    fn flush(&mut self, check: Check<'_>) -> io::Result<()> {
        if !self.rows.is_empty() {
            write_batch(&mut self.writer, &self.sheet, &self.rows, check)?;
            self.rows.clear();
            self.width = 0;
        }
        Ok(())
    }

    fn cell(
        &mut self,
        sheet: &str,
        item: Option<(u32, u32, String, bool)>,
        check: Check<'_>,
    ) -> io::Result<()> {
        let Some((row, col, value, _)) = item else {
            self.flush(check)?;
            self.sheet = sheet.to_owned();
            self.totals.sheets += 1;
            return Ok(());
        };
        if self.rows.last().is_none_or(|prior| prior.index != row) {
            if let Some(prior) = self.rows.last() {
                self.width += prior.cells.last().map_or(0, |(col, _)| *col as usize);
            }
            if self.rows.len() >= 256 || self.width >= 4096 {
                self.flush(check)?;
            }
            self.rows.push(Row {
                index: row,
                cells: Vec::new(),
            });
            self.totals.rows += 1;
        }
        self.rows.last_mut().unwrap().cells.push((col, value));
        self.totals.cells += 1;
        Ok(())
    }
}

pub fn convert(
    source: &Path,
    target: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut layout = Layout {
        writer: Writer::new(target)?,
        sheet: String::new(),
        rows: Vec::with_capacity(256),
        width: 0,
        totals: Totals::default(),
    };
    crate::sheets::stream_cells(source, check, |sheet, cell| layout.cell(sheet, cell, check))?;
    layout.flush(check)?;
    if layout.writer.pages.is_empty() {
        layout.writer.page(
            "Empty workbook / 1-1 / Cell values and formulas",
            3,
            &["Empty worksheet".to_owned()],
            check,
        )?;
    }
    let pages = layout.writer.finish(check)?;
    if crate::pdfium::page_count(target, check)? as usize != pages {
        return Err(invalid("대형 표 PDF의 쪽수가 완료 기록과 다릅니다."));
    }
    let totals = layout.totals;
    warnings.push(format!("대형 XLSX 전체 셀 내용 출력: {}개 시트, {}행, {}개 셀을 {pages}쪽으로 보존했습니다. 원래 인쇄 배치·서식·도형은 보존하지 않으며 빈 행은 생략하고 수식은 계산하지 않은 텍스트로 출력합니다.", totals.sheets, totals.rows, totals.cells));
    check()
}
