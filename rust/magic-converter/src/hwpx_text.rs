use roxmltree::{Document, Node};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use zip::ZipArchive;

pub(super) const P: &str = "http://www.hancom.co.kr/hwpml/2011/paragraph";
pub(crate) const WORD_TEXTBOX_COMMENT: &str = "Word textbox: ";
const PART_LIMIT: u64 = 16 * 1024 * 1024;
const ARCHIVE_LIMIT: u64 = 256 * 1024 * 1024;
type Zip = ZipArchive<File>;

pub(super) fn invalid(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

pub(super) fn elements<'a, 'input>(
    parent: Node<'a, 'input>,
    name: &'a str,
) -> impl Iterator<Item = Node<'a, 'input>> + 'a {
    parent
        .children()
        .filter(move |node| node.has_tag_name((P, name)))
}

pub(super) fn child<'a, 'input>(node: Node<'a, 'input>, name: &'a str) -> Option<Node<'a, 'input>> {
    elements(node, name).next()
}

pub(super) fn section_number(name: &str) -> Option<usize> {
    name.strip_prefix("Contents/section")?
        .strip_suffix(".xml")?
        .parse()
        .ok()
}

pub(super) fn part(archive: &mut Zip, name: &str) -> io::Result<String> {
    let member = archive.by_name(name)?;
    if member.size() > PART_LIMIT {
        return Err(invalid("HWPX XML이 너무 큽니다."));
    }
    let mut value = String::new();
    member.take(PART_LIMIT + 1).read_to_string(&mut value)?;
    if value.len() as u64 > PART_LIMIT {
        return Err(invalid("HWPX XML이 너무 큽니다."));
    }
    Ok(value)
}

pub(super) fn package(source: &Path) -> io::Result<Zip> {
    let mut archive = Zip::new(File::open(source)?)?;
    let mut names = HashSet::new();
    let mut total = 0_u64;
    if archive.len() > 10_000 {
        return Err(invalid("HWPX 항목이 너무 많습니다."));
    }
    for index in 0..archive.len() {
        let entry = archive.by_index(index)?;
        total = total.saturating_add(entry.size());
        if !names.insert(entry.name()?.into_owned()) || total > ARCHIVE_LIMIT || entry.encrypted() {
            return Err(invalid("HWPX 패키지의 항목 또는 크기가 유효하지 않습니다."));
        }
    }
    Ok(archive)
}

pub(super) fn order(archive: &mut Zip) -> io::Result<Vec<String>> {
    let mut numbered: Vec<_> = archive
        .file_names()
        .filter_map(|name| {
            let name = name.ok()?;
            Some((section_number(&name)?, name.into_owned()))
        })
        .collect();
    numbered.sort();
    if numbered.is_empty() {
        return Err(invalid("HWPX 본문 구역이 없습니다."));
    }
    let mut order = Vec::new();
    if archive.index_for_name("Contents/content.hpf").is_some() {
        let index = part(archive, "Contents/content.hpf")?;
        let content = Document::parse(&index).map_err(io::Error::other)?;
        let items: HashMap<_, _> = content
            .descendants()
            .filter(|node| node.tag_name().name() == "item")
            .filter_map(|item| item.attribute("id").zip(item.attribute("href")))
            .collect();
        for item in content
            .descendants()
            .filter(|node| node.tag_name().name() == "itemref")
        {
            if let Some(href) = item.attribute("idref").and_then(|id| items.get(id)) {
                let href = href.trim_start_matches(['.', '/']);
                let name = if href.starts_with("Contents/") {
                    href.to_owned()
                } else {
                    format!("Contents/{href}")
                };
                if numbered.iter().any(|(_, entry)| *entry == name) && !order.contains(&name) {
                    order.push(name);
                }
            }
        }
    }
    for (_, name) in numbered {
        if !order.contains(&name) {
            order.push(name);
        }
    }
    Ok(order)
}

#[derive(Default)]
struct Text {
    headers: Vec<String>,
    footers: Vec<String>,
    include_picture_text: bool,
}

#[derive(Default)]
struct Paragraph {
    blocks: Vec<String>,
    pending: String,
    drawable: bool,
    page_number: bool,
}

impl Paragraph {
    fn flush(&mut self) {
        if !self.pending.is_empty() || self.drawable {
            self.blocks.push(std::mem::take(&mut self.pending));
        }
        self.drawable = false;
        self.page_number = false;
    }
}

impl Text {
    fn paragraph(&mut self, item: Node<'_, '_>, depth: usize) -> io::Result<Vec<String>> {
        let mut p = Paragraph::default();
        for run in elements(item, "run") {
            for node in run.children().filter(Node::is_element) {
                self.visit(node, &mut p, depth + 1)?;
            }
        }
        p.flush();
        if p.blocks.is_empty()
            || (p.blocks.len() == 1
                && item.attribute("pageBreak") == Some("1")
                && elements(item, "run").any(|node| child(node, "tbl").is_some())
                && p.blocks[0].contains('\t'))
        {
            p.blocks.insert(0, String::new());
        }
        Ok(p.blocks)
    }

    fn visit(&mut self, node: Node<'_, '_>, p: &mut Paragraph, depth: usize) -> io::Result<()> {
        if depth > 64 {
            return Err(invalid("HWPX 문단 중첩이 너무 깊습니다."));
        }
        match node.tag_name().name() {
            "tbl" => {
                let inline =
                    child(node, "pos").and_then(|pos| pos.attribute("treatAsChar")) == Some("1");
                if inline && !p.drawable && !p.page_number && p.pending.trim().is_empty() {
                    p.pending.clear();
                } else {
                    p.flush();
                }
                if p.blocks.is_empty()
                    && node
                        .ancestors()
                        .find(|node| node.has_tag_name((P, "p")))
                        .is_some_and(|node| node.attribute("pageBreak") == Some("1"))
                {
                    p.blocks.push(String::new());
                }
                p.blocks.push(self.table(node, depth + 1)?);
            }
            "textart" => {
                p.drawable = true;
                if self.include_picture_text
                    && let Some(text) = node.attribute("text")
                {
                    p.pending.push_str(text);
                }
            }
            "pic" | "line" => {
                p.drawable = true;
                if self.include_picture_text
                    && let Some(text) = child(node, "shapeComment")
                        .and_then(|comment| comment.text())
                        .and_then(|text| text.strip_prefix(WORD_TEXTBOX_COMMENT))
                {
                    p.pending.push_str(text);
                }
            }
            "equation" => {
                let expression = node
                    .children()
                    .find(|node| node.tag_name().name() == "script")
                    .and_then(|node| node.text())
                    .unwrap_or_default();
                p.pending.push_str(expression);
            }
            "t" => {
                for child in node.children() {
                    if child.is_text() {
                        p.pending.push_str(child.text().unwrap_or_default());
                    } else {
                        p.pending.push_str(match child.tag_name().name() {
                            "lineBreak" => "\n",
                            "tab" => "\t",
                            "nbSpace" => " ",
                            _ => "",
                        });
                    }
                }
            }
            "autoNum" if node.attribute("numType") == Some("PAGE") => {
                p.pending.push_str(
                    node.attribute("num")
                        .filter(|num| !num.is_empty())
                        .unwrap_or("1"),
                );
                p.page_number = true;
            }
            "header" | "footer" => {
                if let Some(body) = child(node, "subList") {
                    let mut blocks = Vec::new();
                    for item in elements(body, "p") {
                        blocks.extend(self.paragraph(item, depth + 1)?);
                    }
                    if node.tag_name().name() == "header" {
                        self.headers.extend(blocks);
                    } else {
                        self.footers.extend(blocks);
                    }
                }
            }
            "switch" => {
                let branch = node
                    .children()
                    .find(|node| node.tag_name().name() == "case")
                    .or_else(|| {
                        node.children()
                            .find(|node| node.tag_name().name() == "default")
                    });
                if let Some(branch) = branch {
                    for node in branch.children().filter(Node::is_element) {
                        self.visit(node, p, depth + 1)?;
                    }
                }
            }
            "secPr" | "linesegarray" | "lineSegArray" => {}
            _ => {
                for node in node.children().filter(Node::is_element) {
                    self.visit(node, p, depth + 1)?;
                }
            }
        }
        Ok(())
    }

    fn table(&mut self, table: Node<'_, '_>, depth: usize) -> io::Result<String> {
        let rows = table
            .attribute("rowCnt")
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(1);
        let cols = table
            .attribute("colCnt")
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(1);
        if rows == 0 || cols == 0 || rows.saturating_mul(cols) > 100_000 {
            return Err(invalid("HWPX 표 크기가 유효하지 않습니다."));
        }
        let mut occupied = HashSet::new();
        let mut grid = BTreeMap::new();
        for (r, row) in elements(table, "tr").enumerate() {
            for (c, item) in elements(row, "tc").enumerate() {
                let address = child(item, "cellAddr");
                let spans = child(item, "cellSpan");
                let number = |node: Option<Node<'_, '_>>, name, default| {
                    node.and_then(|node| node.attribute(name))
                        .and_then(|value| value.parse::<usize>().ok())
                        .unwrap_or(default)
                };
                let row = number(address, "rowAddr", r);
                let col = number(address, "colAddr", c);
                let end_row = row.saturating_add(number(spans, "rowSpan", 1));
                let end_col = col.saturating_add(number(spans, "colSpan", 1));
                if row >= end_row || col >= end_col || end_row > rows || end_col > cols {
                    return Err(invalid("HWPX 셀 범위가 유효하지 않습니다."));
                }
                for r in row..end_row {
                    for c in col..end_col {
                        if !occupied.insert((r, c)) {
                            return Err(invalid("HWPX 표의 셀 범위가 겹칩니다."));
                        }
                    }
                }
                let mut blocks = Vec::new();
                if let Some(body) = child(item, "subList") {
                    for item in elements(body, "p") {
                        blocks.extend(self.paragraph(item, depth + 1)?);
                    }
                }
                grid.insert((row, col), blocks.join("\n"));
            }
        }
        let mut result = vec![Vec::new(); rows];
        for ((row, _), value) in grid {
            result[row].push(value);
        }
        Ok(result
            .into_iter()
            .map(|row| row.join("\t"))
            .collect::<Vec<_>>()
            .join("\n"))
    }
}

pub(super) fn extract(
    source: &Path,
    include_picture_text: bool,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<String> {
    let mut archive = package(source)?;
    let mut model = Text {
        include_picture_text,
        ..Text::default()
    };
    let mut body = Vec::new();
    for name in order(&mut archive)? {
        let source = part(&mut archive, &name)?;
        let xml = Document::parse(&source).map_err(io::Error::other)?;
        for paragraph in elements(xml.root_element(), "p") {
            check()?;
            body.extend(model.paragraph(paragraph, 0)?);
        }
    }
    model.headers.append(&mut body);
    model.headers.append(&mut model.footers);
    Ok(model.headers.join("\n"))
}
