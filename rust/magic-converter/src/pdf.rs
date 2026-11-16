//! PDF object-level page operations and image-page construction.
use lopdf::{Dictionary, Document, Object, ObjectId, Stream, dictionary};
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{self, ErrorKind, Write};
use std::path::{Path, PathBuf};

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
const MAX_PDF: u64 = 1024 * 1024 * 1024;

fn error(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

fn read(path: &Path, check: Check<'_>) -> io::Result<Document> {
    check()?;
    if path.metadata()?.len() > MAX_PDF {
        return Err(error("PDF의 입력 크기 제한을 초과했습니다."));
    }
    let doc = Document::load(path).map_err(io::Error::other)?;
    if doc.is_encrypted() {
        return Err(error("암호로 보호된 PDF는 잠금을 해제한 뒤 사용하세요."));
    }
    if doc.get_pages().is_empty() {
        return Err(error("PDF에 페이지가 없습니다."));
    }
    check()?;
    Ok(doc)
}

fn pages(spec: &str, count: usize) -> io::Result<Vec<usize>> {
    if spec.trim().is_empty() {
        return Ok((0..count).collect());
    }
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    for part in spec.split(',') {
        let number = |value: &str| {
            value
                .trim()
                .parse::<usize>()
                .ok()
                .filter(|page| *page > 0)
                .ok_or_else(|| error("PDF 페이지는 1-3,5처럼 지정하세요."))
        };
        let (first, last) = if let Some((first, last)) = part.split_once('-') {
            (number(first)?, number(last)?)
        } else {
            let page = number(part)?;
            (page, page)
        };
        if first > count || last > count {
            return Err(error(format!(
                "PDF 페이지 범위가 전체 {count}페이지를 벗어났습니다."
            )));
        }
        let order: Box<dyn Iterator<Item = usize>> = if first <= last {
            Box::new(first..=last)
        } else {
            Box::new((last..=first).rev())
        };
        result.extend(order.filter(|page| seen.insert(*page)).map(|page| page - 1));
    }
    Ok(result)
}

fn inherited(doc: &Document, start: ObjectId, name: &[u8]) -> Option<Object> {
    let mut id = start;
    let mut visited = HashSet::new();
    while visited.insert(id) {
        let current = doc.get_dictionary(id).ok()?;
        if let Ok(value) = current.get(name) {
            return Some(value.clone());
        }
        id = current.get(b"Parent").ok()?.as_reference().ok()?;
    }
    None
}

struct PageTree {
    doc: Document,
    root: ObjectId,
    children: Vec<ObjectId>,
}

impl PageTree {
    fn new() -> Self {
        let mut doc = Document::with_version("1.7");
        let root = doc.new_object_id();
        Self {
            doc,
            root,
            children: Vec::new(),
        }
    }

    fn append(
        &mut self,
        mut source: Document,
        selection: &[usize],
        check: Check<'_>,
    ) -> io::Result<()> {
        source.renumber_objects_with(self.doc.max_id + 1);
        let pages: Vec<_> = source.get_pages().into_values().collect();
        for &index in selection {
            check()?;
            let id = pages[index];
            let entries: Vec<_> = [b"Resources".as_slice(), b"MediaBox", b"CropBox", b"Rotate"]
                .iter()
                .filter_map(|&key| inherited(&source, id, key).map(|value| (key, value)))
                .collect();
            let page = source.get_dictionary_mut(id).map_err(io::Error::other)?;
            for (name, value) in entries {
                page.set(name.to_vec(), value);
            }
            page.set("Parent", self.root);
            self.children.push(id);
        }
        self.doc.max_id = source.max_id;
        self.doc.objects.extend(source.objects);
        Ok(())
    }

    fn image(
        &mut self,
        bytes: Vec<u8>,
        width: u32,
        height: u32,
        dpi: u32,
        filter: &str,
    ) -> io::Result<()> {
        use lopdf::content::{Content, Operation};
        let xobject = self.doc.add_object(Stream::new(dictionary! {
            "Type" => "XObject", "Subtype" => "Image", "Width" => i64::from(width), "Height" => i64::from(height),
            "BitsPerComponent" => 8, "ColorSpace" => "DeviceRGB", "Filter" => filter,
        }, bytes));
        let width = width as f32 * 72.0 / dpi as f32;
        let height = height as f32 * 72.0 / dpi as f32;
        let content = Content {
            operations: vec![
                Operation::new("q", vec![]),
                Operation::new(
                    "cm",
                    vec![
                        width.into(),
                        0.into(),
                        0.into(),
                        height.into(),
                        0.into(),
                        0.into(),
                    ],
                ),
                Operation::new("Do", vec![Object::Name(b"image".to_vec())]),
                Operation::new("Q", vec![]),
            ],
        }
        .encode()
        .map_err(io::Error::other)?;
        let content = self.doc.add_object(Stream::new(Dictionary::new(), content));
        let page = self.doc.add_object(dictionary! {
            "Type" => "Page", "Parent" => self.root, "MediaBox" => vec![0.into(), 0.into(), width.into(), height.into()],
            "Resources" => dictionary! { "XObject" => dictionary! { "image" => xobject } }, "Contents" => content,
        });
        self.children.push(page);
        Ok(())
    }

    fn finish(mut self) -> Document {
        self.doc.objects.insert(
            self.root,
            Object::Dictionary(dictionary! {
                "Type" => "Pages", "Count" => self.children.len() as i64,
                "Kids" => self.children.into_iter().map(Object::Reference).collect::<Vec<_>>(),
            }),
        );
        let catalog = self
            .doc
            .add_object(dictionary! { "Type" => "Catalog", "Pages" => self.root });
        self.doc.trailer.set("Root", catalog);
        self.doc.prune_objects();
        self.doc
    }
}

fn write(doc: &mut Document, output: &mut impl Write, check: Check<'_>) -> io::Result<()> {
    check()?;
    doc.save_to(output).map_err(io::Error::other)?;
    check()
}

pub fn images(frames: &[image::RgbaImage], output: &Path, check: Check<'_>) -> io::Result<()> {
    let mut tree = PageTree::new();
    for frame in frames {
        check()?;
        let rgb = crate::pictures::white(frame);
        let mut jpeg = Vec::new();
        crate::pictures::write_pdf_jpeg(&rgb, &mut jpeg)?;
        tree.image(jpeg, frame.width(), frame.height(), 300, "DCTDecode")?;
    }
    write(&mut tree.finish(), &mut File::create(output)?, check)
}

pub(crate) fn comic_pages(
    output: &Path,
    count: usize,
    check: Check<'_>,
    mut page: impl FnMut(usize) -> io::Result<image::RgbImage>,
) -> io::Result<()> {
    use flate2::{Compression, write::ZlibEncoder};
    let mut tree = PageTree::new();
    let mut bytes = 0_usize;
    for index in 0..count {
        check()?;
        let image = page(index)?;
        let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
        for chunk in image.as_raw().chunks(256 * 1024) {
            check()?;
            encoder.write_all(chunk)?;
        }
        let stream = encoder.finish()?;
        bytes = bytes.saturating_add(stream.len());
        if bytes > 256 * 1024 * 1024 {
            return Err(error("만화 PDF 출력의 이미지 용량 한도를 초과했습니다."));
        }
        tree.image(stream, image.width(), image.height(), 96, "FlateDecode")?;
    }
    write(&mut tree.finish(), &mut File::create(output)?, check)
}

fn compress_pages(doc: &mut Document, ids: &[ObjectId], check: Check<'_>) -> io::Result<()> {
    use flate2::{Compression, write::ZlibEncoder};
    let mut streams = HashMap::<Vec<u8>, ObjectId>::new();
    for id in ids {
        check()?;
        let original = doc.get_page_content(*id).map_err(io::Error::other)?;
        if original.is_empty() {
            continue;
        }
        let stream = if let Some(id) = streams.get(&original) {
            *id
        } else {
            let mut encoder = ZlibEncoder::new(Vec::new(), Compression::best());
            encoder.write_all(&original)?;
            let stream = doc.add_object(Stream::new(
                dictionary! {"Filter" => "FlateDecode"},
                encoder.finish()?,
            ));
            streams.insert(original, stream);
            stream
        };
        doc.get_dictionary_mut(*id)
            .map_err(io::Error::other)?
            .set("Contents", stream);
    }
    doc.prune_objects();
    Ok(())
}

pub fn transform(
    path: &Path,
    output: &Path,
    key: &str,
    spec: &str,
    rotation: i64,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut doc = read(path, check)?;
    let ids: Vec<_> = doc.get_pages().into_values().collect();
    let spec = if matches!(key, "tool:pdf_compress" | "strip") {
        ""
    } else {
        spec
    };
    let selected = pages(spec, ids.len())?;
    match key {
        "tool:pdf_select" => {
            let mut tree = PageTree::new();
            tree.append(doc, &selected, check)?;
            doc = tree.finish();
        }
        "tool:pdf_rotate" => {
            for index in selected {
                check()?;
                let id = ids[index];
                let before = inherited(&doc, id, b"Rotate")
                    .and_then(|value| value.as_i64().ok())
                    .unwrap_or(0);
                doc.get_dictionary_mut(id)
                    .map_err(io::Error::other)?
                    .set("Rotate", (before + rotation).rem_euclid(360));
            }
        }
        "tool:pdf_compress" => compress_pages(&mut doc, &ids, check)?,
        "strip" => {
            doc.trailer.remove(b"Info");
            if let Ok(root) = doc.trailer.get(b"Root").and_then(Object::as_reference) {
                doc.get_dictionary_mut(root)
                    .map_err(io::Error::other)?
                    .remove(b"Metadata");
            }
            doc.prune_objects();
        }
        _ => return Err(error("지원하는 PDF 작업이 아닙니다.")),
    }
    write(&mut doc, &mut File::create(output)?, check)?;
    if key == "tool:pdf_compress" && output.metadata()?.len() >= path.metadata()?.len() {
        let input = File::open(path)?;
        let size = input.metadata()?.len();
        magic_copy_worker::copy_stream(input, File::create(output)?, size, check, |_| Ok(()))?;
        warnings.push("이미 최적화된 PDF여서 원본 내용을 복사했습니다.".to_owned());
    }
    if read(output, check)?.get_pages().len() != doc.get_pages().len() {
        return Err(error("출력 PDF의 페이지 수가 올바르지 않습니다."));
    }
    Ok(())
}

pub fn split(path: &Path, folder: &Path, spec: &str, check: Check<'_>) -> io::Result<()> {
    let doc = read(path, check)?;
    let count = doc.get_pages().len();
    for (index, page) in pages(spec, count)?.into_iter().enumerate() {
        let mut tree = PageTree::new();
        tree.append(doc.clone(), &[page], check)?;
        let mut output =
            File::create(folder.join(format!("{:04}_page_{:04}.pdf", index + 1, page + 1)))?;
        write(&mut tree.finish(), &mut output, check)?;
    }
    Ok(())
}

pub fn merge(
    catalog: &crate::Catalog,
    sources: &[PathBuf],
    output: &Path,
    check: Check<'_>,
) -> io::Result<()> {
    let mut tree = PageTree::new();
    let workspace = tempfile::tempdir()?;
    for (index, source) in sources.iter().enumerate() {
        check()?;
        let (format, _) = catalog
            .detect(source)
            .ok_or_else(|| error("지원하는 PDF 또는 이미지 파일을 지정하세요."))?;
        let path = if format.id == "pdf" {
            source.clone()
        } else {
            let output = workspace.path().join(format!("{index}.pdf"));
            crate::pictures::to_pdf(source, &output, &format.id, check, &mut Vec::new())?;
            output
        };
        let doc = read(&path, check)?;
        let selection = (0..doc.get_pages().len()).collect::<Vec<_>>();
        tree.append(doc, &selection, check)?;
    }
    write(&mut tree.finish(), &mut File::create(output)?, check)
}
