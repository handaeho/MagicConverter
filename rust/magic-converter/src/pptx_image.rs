use std::fs::File;
use std::io::{self, Cursor, Read, Write};
use std::path::Path;
use zip::{CompressionMethod, ZipArchive, ZipWriter, write::SimpleFileOptions};

const EMPTY_DECK: &[u8] = include_bytes!("../assets/empty-presentation.pptx");
const PRESENTATION: &str = "ppt/presentation.xml";
const RELATIONS: &str = "ppt/_rels/presentation.xml.rels";
const CONTENT_TYPES: &str = "[Content_Types].xml";
const P: &str = "http://schemas.openxmlformats.org/presentationml/2006/main";
const A: &str = "http://schemas.openxmlformats.org/drawingml/2006/main";
const R: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PACKAGE: &str = "http://schemas.openxmlformats.org/package/2006/relationships";
pub(crate) const CHART_NS: &str = "http://schemas.openxmlformats.org/drawingml/2006/chart";

pub(crate) struct ChartPart {
    pub xml: String,
    pub workbook: Vec<u8>,
}

type Deck = ZipWriter<File>;

fn xml(archive: &mut ZipArchive<Cursor<&[u8]>>, name: &str) -> io::Result<String> {
    let mut value = String::new();
    archive.by_name(name)?.read_to_string(&mut value)?;
    Ok(value)
}

fn text(deck: &mut Deck, name: &str, value: &str, options: SimpleFileOptions) -> io::Result<()> {
    deck.start_file(name, options)?;
    deck.write_all(value.as_bytes())
}

pub(crate) fn picture(picture: [u64; 4], id: usize, relationship: usize) -> String {
    let [x, y, cx, cy] = picture;
    format!(
        r#"<p:pic><p:nvPicPr><p:cNvPr id="{id}" name="PDF page" descr="PDF page"/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>
<p:blipFill><a:blip r:embed="rId{relationship}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
</p:pic>"#
    )
}

pub(crate) fn emu(value: f64) -> u64 {
    (value * 12_700.0) as u64
}

type Seed = ZipArchive<Cursor<&'static [u8]>>;

pub(crate) struct Presentation {
    input: Seed,
    deck: Deck,
    compressed: SimpleFileOptions,
    canvas: Option<(f64, f64)>,
    ids: String,
    rels: String,
    types: String,
    image_bytes: usize,
    slides: usize,
    charts: usize,
}

impl Presentation {
    pub fn new(output: &Path) -> io::Result<Self> {
        let mut input = ZipArchive::new(Cursor::new(EMPTY_DECK))?;
        let mut deck = ZipWriter::new(File::create(output)?);
        let compressed =
            SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
        for index in 0..input.len() {
            let mut file = input.by_index(index)?;
            let name = file.name()?.into_owned();
            if ![PRESENTATION, RELATIONS, CONTENT_TYPES].contains(&name.as_str()) {
                deck.start_file(name, compressed)?;
                io::copy(&mut file, &mut deck)?;
            }
        }
        Ok(Self {
            input,
            deck,
            compressed,
            canvas: None,
            ids: "<p:sldIdLst>".into(),
            rels: String::new(),
            types: r#"<Default Extension="png" ContentType="image/png"/>"#.into(),
            image_bytes: 0,
            slides: 0,
            charts: 0,
        })
    }

    pub fn fit(&mut self, width: f64, height: f64) -> io::Result<(f64, f64, f64)> {
        let (deck_width, deck_height) = *self.canvas.get_or_insert((width, height));
        if [deck_width, deck_height]
            .iter()
            .any(|size| !(72.0..=4032.0).contains(size))
        {
            return Err(io::Error::other(
                "첫 페이지 크기가 PowerPoint의 슬라이드 크기 범위를 벗어납니다.",
            ));
        }
        let ratio = (deck_width / width).min(deck_height / height);
        let (w, h) = (width * ratio, height * ratio);
        Ok((ratio, (deck_width - w) / 2.0, (deck_height - h) / 2.0))
    }

    pub fn slide<'a>(
        &mut self,
        shapes: &str,
        images: impl IntoIterator<Item = &'a [u8]>,
    ) -> io::Result<()> {
        self.slide_with_charts(shapes, images, &[])
    }

    pub fn slide_with_charts<'a>(
        &mut self,
        shapes: &str,
        images: impl IntoIterator<Item = &'a [u8]>,
        charts: &[ChartPart],
    ) -> io::Result<()> {
        use std::fmt::Write as _;
        self.slides += 1;
        let number = self.slides;
        let mut relationships = String::new();
        let mut next_relationship = 2;
        for (index, png) in images.into_iter().enumerate() {
            self.image_bytes = self.image_bytes.saturating_add(png.len());
            if self.image_bytes > 1024 * 1024 * 1024 {
                return Err(io::Error::other(
                    "슬라이드 이미지 출력 한도를 초과했습니다.",
                ));
            }
            let name = if index == 0 {
                format!("image{number}.png")
            } else {
                format!("image{number}-{}.png", index + 1)
            };
            self.deck
                .start_file(format!("ppt/media/{name}"), SimpleFileOptions::default())?;
            self.deck.write_all(png)?;
            write!(
                relationships,
                r#"<Relationship Id="rId{}" Type="{R}/image" Target="../media/{name}"/>"#,
                next_relationship
            )
            .unwrap();
            next_relationship += 1;
        }
        for chart in charts {
            self.charts += 1;
            let name = format!("chart{}", self.charts);
            text(
                &mut self.deck,
                &format!("ppt/charts/{name}.xml"),
                &chart.xml,
                self.compressed,
            )?;
            text(
                &mut self.deck,
                &format!("ppt/charts/_rels/{name}.xml.rels"),
                &format!(
                    r#"<Relationships xmlns="{PACKAGE}"><Relationship Id="rId1" Type="{R}/package" Target="../embeddings/{name}.xlsx"/></Relationships>"#
                ),
                self.compressed,
            )?;
            self.deck.start_file(
                format!("ppt/embeddings/{name}.xlsx"),
                SimpleFileOptions::default(),
            )?;
            self.deck.write_all(&chart.workbook)?;
            write!(relationships, r#"<Relationship Id="rId{next_relationship}" Type="{R}/chart" Target="../charts/{name}.xml"/>"#).unwrap();
            write!(self.types, r#"<Override PartName="/ppt/charts/{name}.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>"#).unwrap();
            next_relationship += 1;
        }
        let slide = format!(
            r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"><p:cSld>
<p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
<p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
<p:grpSpPr/>{shapes}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>"#
        );
        text(
            &mut self.deck,
            &format!("ppt/slides/slide{number}.xml"),
            &slide,
            self.compressed,
        )?;
        text(
            &mut self.deck,
            &format!("ppt/slides/_rels/slide{number}.xml.rels"),
            &format!(
                r#"<Relationships xmlns="{PACKAGE}"><Relationship Id="rId1" Type="{R}/slideLayout" Target="../slideLayouts/slideLayout7.xml"/>{relationships}</Relationships>"#
            ),
            self.compressed,
        )?;
        let relationship = number + 6;
        write!(
            self.ids,
            r#"<p:sldId id="{}" r:id="rId{relationship}"/>"#,
            number + 255
        )
        .unwrap();
        write!(self.rels, r#"<Relationship Id="rId{relationship}" Type="{R}/slide" Target="slides/slide{number}.xml"/>"#).unwrap();
        write!(self.types, r#"<Override PartName="/ppt/slides/slide{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>"#).unwrap();
        Ok(())
    }

    pub fn finish(mut self) -> io::Result<()> {
        self.ids.push_str("</p:sldIdLst>");
        let (width, height) = self
            .canvas
            .ok_or_else(|| io::Error::other("PDF에 페이지가 없습니다."))?;
        let mut presentation = xml(&mut self.input, PRESENTATION)?;
        let size = presentation.find("<p:sldSz ").expect("template slide size");
        let end = size
            + presentation[size..]
                .find("/>")
                .expect("template slide size end")
            + 2;
        presentation.replace_range(
            size..end,
            &format!(
                r#"{}<p:sldSz cx="{}" cy="{}"/>"#,
                self.ids,
                emu(width),
                emu(height)
            ),
        );
        text(&mut self.deck, PRESENTATION, &presentation, self.compressed)?;
        let relations = xml(&mut self.input, RELATIONS)?.replace(
            "</Relationships>",
            &format!("{}</Relationships>", self.rels),
        );
        text(&mut self.deck, RELATIONS, &relations, self.compressed)?;
        if self.charts != 0 {
            self.types.push_str(r#"<Default Extension="xlsx" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"/>"#);
        }
        let types = xml(&mut self.input, CONTENT_TYPES)?
            .replace("</Types>", &format!("{}</Types>", self.types));
        text(&mut self.deck, CONTENT_TYPES, &types, self.compressed)?;
        self.deck.finish()?;
        Ok(())
    }
}

pub fn convert(source: &Path, output: &Path, check: &dyn Fn() -> io::Result<()>) -> io::Result<()> {
    let mut deck = Presentation::new(output)?;
    crate::mupdf::png_pages(source, 200, 1000, check, |_, width, height, png| {
        let (width, height) = (f64::from(width), f64::from(height));
        let (scale, x, y) = deck.fit(width, height)?;
        let frame = [emu(x), emu(y), emu(width * scale), emu(height * scale)];
        deck.slide(&picture(frame, 2, 2), [png])
    })?;
    deck.finish()
}
