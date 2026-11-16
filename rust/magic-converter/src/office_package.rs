use roxmltree::{Document, Node};
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{self, ErrorKind, Write};
use std::ops::Range;
use std::path::Path;
use zip::write::SimpleFileOptions;
use zip::{CompressionMethod, ZipArchive, ZipWriter};

const MAX_PACKAGE: u64 = 512 * 1024 * 1024;
const MAX_XML: u64 = 128 * 1024 * 1024;
const RELATIONSHIP_NS: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
type Edit<'a> = (Range<usize>, &'a str);

#[derive(Clone, Copy, PartialEq)]
pub enum Action {
    StripMetadata,
    Standardize,
    RewriteWorkbook,
}

fn copy(
    source: &mut zip::read::ZipFile<'_, File>,
    destination: &mut impl Write,
    check: Check<'_>,
) -> io::Result<()> {
    let length = source.size();
    crate::copy_stream(source, destination, length, check, |_| Ok(()))?;
    Ok(())
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn blocked(value: &str, action: Action) -> bool {
    let value = value.to_ascii_lowercase();
    (action == Action::RewriteWorkbook && value.contains("externallink"))
        || [
            "vbaproject",
            "vbadata",
            "activex",
            "embeddings/",
            "_xmlsignatures/",
        ]
        .iter()
        .any(|pattern| value.contains(pattern))
}

fn load_xml(archive: &mut ZipArchive<File>, name: &str, check: Check<'_>) -> io::Result<String> {
    let mut source = archive.by_name(name)?;
    if source.size() > MAX_XML {
        return Err(invalid("Office XML의 크기가 너무 큽니다."));
    }
    let mut data = Vec::new();
    copy(&mut source, &mut data, check)?;
    let decoded = if data.starts_with(&[255, 254]) {
        encoding_rs::UTF_16LE.decode_with_bom_removal(&data)
    } else if data.starts_with(&[254, 255]) {
        encoding_rs::UTF_16BE.decode_with_bom_removal(&data)
    } else {
        encoding_rs::UTF_8.decode_with_bom_removal(&data)
    };
    if decoded.1 {
        return Err(invalid("Office XML의 인코딩을 읽을 수 없습니다."));
    }
    let mut xml = decoded.0.into_owned();
    if xml.starts_with("<?xml")
        && let Some(end) = xml.find("?>")
    {
        xml.replace_range(..end + 2, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
    }
    Ok(xml)
}

fn document(xml: &str) -> io::Result<Document<'_>> {
    if xml.contains("<!DOCTYPE") || xml.contains("<!ENTITY") {
        return Err(invalid("외부 엔터티를 포함한 Office XML은 읽지 않습니다."));
    }
    Document::parse(xml).map_err(io::Error::other)
}

fn apply(mut xml: String, mut edits: Vec<Edit<'_>>) -> String {
    edits.sort_unstable_by_key(|(range, _)| range.start);
    for (range, replacement) in edits.into_iter().rev() {
        xml.replace_range(range, replacement);
    }
    xml
}

fn affected(node: Node<'_, '_>, action: Action) -> bool {
    node.attributes()
        .any(|attribute| blocked(attribute.value(), action))
}

fn relationships(xml: &str, action: Action) -> io::Result<(Vec<Edit<'static>>, HashSet<String>)> {
    let parsed = document(xml)?;
    let mut edits = Vec::new();
    let mut ids = HashSet::new();
    for child in parsed.root_element().children().filter(Node::is_element) {
        if affected(child, action) {
            if let Some(id) = child.attribute("Id") {
                ids.insert(id.to_owned());
            }
            edits.push((child.range(), ""));
        }
    }
    Ok((edits, ids))
}

fn relationship_owner(name: &str) -> Option<String> {
    if let Some(name) = name.strip_prefix("_rels/") {
        return Some(name.strip_suffix(".rels")?.to_owned());
    }
    let (parent, name) = name.rsplit_once("/_rels/")?;
    Some(format!("{parent}/{}", name.strip_suffix(".rels")?))
}

fn types(
    xml: &str,
    part: &str,
    main_type: &'static str,
    action: Action,
) -> io::Result<Vec<Edit<'static>>> {
    let parsed = document(xml)?;
    let mut edits = Vec::new();
    let mut found = false;
    for child in parsed.root_element().children().filter(Node::is_element) {
        if affected(child, action) {
            edits.push((child.range(), ""));
        } else if child.attribute("PartName") == Some(part)
            && let Some(kind) = child.attribute_node("ContentType")
        {
            edits.push((kind.range_value(), main_type));
            found = true;
        }
    }
    if !found {
        return Err(invalid(
            "Office 패키지의 기본 콘텐츠 유형을 찾을 수 없습니다.",
        ));
    }
    Ok(edits)
}

fn remove_references(
    parent: Node<'_, '_>,
    ids: &HashSet<String>,
    edits: &mut Vec<Edit<'static>>,
    depth: usize,
) -> io::Result<()> {
    if depth > 256 {
        return Err(invalid("Office XML 요소의 중첩이 너무 깊습니다."));
    }
    let references = |node: Node<'_, '_>| {
        node.attributes().any(|attribute| {
            attribute.namespace() == Some(RELATIONSHIP_NS) && ids.contains(attribute.value())
        })
    };
    for child in parent.children().filter(Node::is_element) {
        let tag = child.tag_name().name();
        if references(child)
            || matches!(
                tag,
                "object" | "oleObj" | "oleObject" | "control" | "graphicFrame"
            ) && child.descendants().any(references)
        {
            edits.push((child.range(), ""));
        } else {
            let before = edits.len();
            remove_references(child, ids, edits, depth + 1)?;
            if matches!(tag, "oleObjects" | "controls" | "externalReferences")
                && child.children().filter(Node::is_element).all(|node| {
                    edits[before..]
                        .iter()
                        .any(|(range, _)| *range == node.range())
                })
            {
                edits.truncate(before);
                edits.push((child.range(), ""));
            }
        }
    }
    Ok(())
}

fn properties(xml: &str, name: &str) -> io::Result<Vec<Edit<'static>>> {
    let parsed = document(xml)?;
    let mut edits = Vec::new();
    for child in parsed.root_element().children().filter(Node::is_element) {
        if name != "docProps/app.xml" {
            edits.push((child.range(), ""));
        } else if matches!(child.tag_name().name(), "Company" | "Manager") {
            for value in child.children().filter(Node::is_text) {
                edits.push((value.range(), ""));
            }
        }
    }
    Ok(edits)
}

pub fn convert(source: &Path, output: &Path, action: Action, check: Check<'_>) -> io::Result<()> {
    let normalize = action != Action::StripMetadata;
    let layout = match source
        .extension()
        .and_then(|ext| ext.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "xlsm" | "xltm" | "xltx" | "xlsx" => (
            "/xl/workbook.xml",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
        "docm" | "dotm" | "dotx" | "docx" => (
            "/word/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        ),
        _ => (
            "/ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        ),
    };
    let mut archive = ZipArchive::new(File::open(source)?)?;
    let mut names = Vec::new();
    let mut unique = HashSet::new();
    let mut bytes = 0_u64;
    for index in 0..archive.len() {
        check()?;
        let entry = archive.by_index(index)?;
        let name = entry.name()?.into_owned();
        if !unique.insert(name.clone()) {
            return Err(invalid("Office 패키지에 중복 항목이 있습니다."));
        }
        bytes = bytes.saturating_add(entry.size());
        if bytes > MAX_PACKAGE {
            return Err(invalid("Office 패키지의 압축 해제 크기가 너무 큽니다."));
        }
        names.push(name);
    }
    if normalize && (!unique.contains(&layout.0[1..]) || !unique.contains("[Content_Types].xml")) {
        return Err(invalid("Office 패키지의 필수 구성 요소가 없습니다."));
    }
    let mut removed = HashMap::new();
    if normalize {
        for name in names
            .iter()
            .filter(|name| name.ends_with(".rels") && !blocked(name, action))
        {
            check()?;
            let xml = load_xml(&mut archive, name, check)?;
            let (_, ids) = relationships(&xml, action)?;
            if !ids.is_empty()
                && let Some(owner) = relationship_owner(name)
            {
                removed.insert(owner, ids);
            }
        }
    }
    let mut target = ZipWriter::new(File::create(output)?);
    let options = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    for name in names {
        check()?;
        if normalize && blocked(&name, action) {
            continue;
        }
        if name.ends_with('/') {
            target.add_directory(name, options)?;
            continue;
        }
        target.start_file(&name, options)?;
        if normalize
            && (name == "[Content_Types].xml"
                || name.ends_with(".rels")
                || removed.contains_key(&name))
        {
            let xml = load_xml(&mut archive, &name, check)?;
            let edits = if name == "[Content_Types].xml" {
                types(&xml, layout.0, layout.1, action)?
            } else if name.ends_with(".rels") {
                relationships(&xml, action)?.0
            } else {
                let parsed = document(&xml)?;
                let mut edits = Vec::new();
                remove_references(parsed.root_element(), &removed[&name], &mut edits, 0)?;
                edits
            };
            target.write_all(apply(xml, edits).as_bytes())?;
        } else if !normalize
            && matches!(
                name.as_str(),
                "docProps/core.xml" | "docProps/custom.xml" | "docProps/app.xml"
            )
        {
            let xml = load_xml(&mut archive, &name, check)?;
            let edits = properties(&xml, &name)?;
            target.write_all(apply(xml, edits).as_bytes())?;
        } else {
            copy(&mut archive.by_name(&name)?, &mut target, check)?;
        }
    }
    target.finish()?.sync_all()?;
    check()
}
