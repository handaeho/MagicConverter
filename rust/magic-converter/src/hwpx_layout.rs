use crate::hwpx_text::{P, child, elements, package, part, section_number};
use roxmltree::{Document, Node};
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{self, Write};
use std::ops::Range;
use std::path::Path;
use zip::{ZipWriter, write::SimpleFileOptions};

type Edit = (Range<usize>, String);

fn number(item: Node<'_, '_>, key: &str) -> Option<i64> {
    let value: i64 = item.attribute(key)?.parse().ok()?;
    (value.abs_diff(0) <= 100_000_000).then_some(value)
}

fn styles<'a>(document: &'a Document<'_>) -> HashSet<&'a str> {
    document
        .descendants()
        .filter(|node| node.tag_name().name() == "paraPr")
        .filter_map(|node| {
            let mut margins = node
                .descendants()
                .filter(|node| matches!(node.tag_name().name(), "prev" | "next"))
                .peekable();
            (margins.peek().is_some()
                && margins.all(|node| {
                    node.attribute("unit") == Some("HWPUNIT")
                        && node.attribute("value") == Some("0")
                }))
            .then(|| node.attribute("id"))
            .flatten()
        })
        .collect()
}

fn lines<'a, 'input>(p: Node<'a, 'input>) -> Vec<Node<'a, 'input>> {
    child(p, "linesegarray").map_or_else(Vec::new, |lines| elements(lines, "lineseg").collect())
}

fn tail(table: Node<'_, '_>, styles: &HashSet<&str>) -> Option<Vec<Edit>> {
    let pos = child(table, "pos")?;
    let size = child(table, "sz")?;
    if pos.attribute("treatAsChar") != Some("0")
        || !matches!(
            table.attribute("textWrap"),
            Some("SQUARE" | "TOP_AND_BOTTOM")
        )
        || table.attribute("noAdjust") != Some("0")
        || table.attribute("cellSpacing") != Some("0")
    {
        return None;
    }
    let rows: Vec<_> = elements(table, "tr").collect();
    let cols = usize::try_from(number(table, "colCnt")?).ok()?;
    if rows.len() < 2 || rows.len() as i64 != number(table, "rowCnt")? || cols == 0 {
        return None;
    }
    let mut total = 0_i64;
    let mut last = 0;
    for (r, row) in rows.iter().copied().enumerate() {
        let mut height = None;
        let mut count = 0;
        for (c, cell) in elements(row, "tc").enumerate() {
            let span = child(cell, "cellSpan")?;
            let addr = child(cell, "cellAddr")?;
            let size = child(cell, "cellSz")?;
            let value = number(size, "height")?;
            if span.attribute("rowSpan") != Some("1")
                || span.attribute("colSpan") != Some("1")
                || number(addr, "rowAddr")? != r as i64
                || number(addr, "colAddr")? != c as i64
                || value <= 0
                || height.is_some_and(|height| value != height)
            {
                return None;
            }
            height = Some(value);
            count += 1;
        }
        if count != cols {
            return None;
        }
        last = height?;
        total += last;
    }
    let excess = total - number(size, "height")?;
    if excess <= 0 || excess * 20 > total {
        return None;
    }
    let height = last - excess;
    let mut edits = Vec::new();
    for cell in elements(*rows.last()?, "tc") {
        let list = child(cell, "subList")?;
        let paragraphs: Vec<_> = elements(list, "p").collect();
        let [p] = paragraphs.as_slice() else {
            return None;
        };
        if !styles.contains(p.attribute("paraPrIDRef")?)
            || elements(*p, "run")
                .flat_map(|run| run.children().filter(Node::is_element))
                .any(|node| !node.has_tag_name((P, "t")))
        {
            return None;
        }
        let lines = lines(*p);
        let mut content = 0;
        if lines.is_empty() {
            return None;
        }
        for line in lines {
            let y = number(line, "vertpos")?;
            if y < 0 {
                return None;
            }
            content = content.max(
                y + number(line, "vertsize")?.max(number(line, "textheight")?)
                    + number(line, "spacing")?.max(0),
            );
        }
        let margin = if cell.attribute("hasMargin") == Some("1") {
            child(cell, "cellMargin")?
        } else {
            child(table, "inMargin")?
        };
        if height < content + number(margin, "top")? + number(margin, "bottom")? {
            return None;
        }
        let field = child(cell, "cellSz")?.attribute_node("height")?;
        edits.push((field.range_value(), height.to_string()));
    }
    Some(edits)
}

fn breaks(root: Node<'_, '_>, check: crate::rhwp::Check<'_>) -> io::Result<Vec<Edit>> {
    let pages: Vec<_> = root
        .descendants()
        .filter(|node| node.has_tag_name((P, "pagePr")))
        .collect();
    let columns: Vec<_> = root
        .descendants()
        .filter(|node| node.has_tag_name((P, "colPr")))
        .collect();
    let ([page], [column]) = (pages.as_slice(), columns.as_slice()) else {
        return Ok(vec![]);
    };
    if column.attribute("colCount") != Some("1") {
        return Ok(vec![]);
    }
    let body = (|| {
        let margin = child(*page, "margin")?;
        Some(
            number(*page, "height")?
                - number(margin, "top")?
                - number(margin, "bottom")?
                - number(margin, "header")?
                - number(margin, "footer")?,
        )
    })();
    let Some(body) = body.filter(|body| *body > 0) else {
        return Ok(vec![]);
    };
    let mut blanks: Vec<(i64, i64)> = Vec::new();
    let mut anchor = false;
    let mut edits = Vec::new();
    for p in elements(root, "p") {
        check()?;
        let nodes: Vec<_> = elements(p, "run")
            .flat_map(|run| run.children().filter(Node::is_element))
            .collect();
        let empty = nodes
            .iter()
            .all(|node| node.has_tag_name((P, "t")) && node.text().unwrap_or_default().is_empty());
        let plain = p
            .children()
            .filter(Node::is_element)
            .all(|node| node.has_tag_name((P, "run")) || node.has_tag_name((P, "linesegarray")));
        let no_break =
            p.attribute("pageBreak") == Some("0") && p.attribute("columnBreak") == Some("0");
        let lines = lines(p);
        let single_y = if let [line] = lines.as_slice() {
            number(*line, "vertpos")
        } else {
            None
        };
        if empty
            && plain
            && no_break
            && let Some(y) = single_y
            && let Some(height) = number(lines[0], "vertsize")
            && height > 0
            && y >= 0
            && y + height <= body
        {
            blanks.push((y, height));
            continue;
        }
        let tables: Vec<_> = nodes
            .iter()
            .copied()
            .filter(|node| node.has_tag_name((P, "tbl")))
            .collect();
        let text = nodes.iter().any(|node| {
            node.has_tag_name((P, "t")) && !node.text().unwrap_or_default().trim().is_empty()
        });
        if anchor
            && blanks.len() >= 2
            && no_break
            && !text
            && let [table] = tables.as_slice()
            && child(*table, "pos").and_then(|pos| pos.attribute("treatAsChar")) == Some("1")
            && table.attribute("rowCnt") == Some("1")
            && table.attribute("textWrap") == Some("TOP_AND_BOTTOM")
            && single_y.is_some_and(|y| y >= 0 && y * 20 <= body)
            && blanks[0].0 * 4 >= body * 3
            && blanks[blanks.len() - 1].0 * 10 >= body * 9
            && blanks
                .windows(2)
                .all(|pair| pair[0].0 + pair[0].1 <= pair[1].0)
        {
            let field = p.attribute_node("pageBreak").expect("no page break");
            edits.push((field.range_value(), "1".to_owned()));
        }
        blanks.clear();
        anchor = no_break
            && tables.len() == 1
            && child(tables[0], "pos").and_then(|pos| pos.attribute("treatAsChar")) == Some("0")
            && matches!(tables[0].attribute("pageBreak"), Some("CELL" | "TABLE"))
            && number(tables[0], "rowCnt").is_some_and(|count| count >= 2)
            && child(tables[0], "sz")
                .and_then(|node| number(node, "height"))
                .is_some_and(|size| size * 2 >= body && size < body);
    }
    Ok(edits)
}

fn edits(
    source: &str,
    styles: &HashSet<&str>,
    check: crate::rhwp::Check<'_>,
) -> io::Result<(Vec<Edit>, usize, usize)> {
    let document = Document::parse(source).map_err(io::Error::other)?;
    let root = document.root_element();
    let mut edits = breaks(root, check)?;
    let page_breaks = edits.len();
    let mut tails = 0;
    for paragraph in elements(root, "p") {
        check()?;
        let nodes: Vec<_> = elements(paragraph, "run")
            .flat_map(|run| run.children().filter(Node::is_element))
            .collect();
        if nodes.iter().any(|node| {
            node.has_tag_name((P, "t")) && !node.text().unwrap_or_default().trim().is_empty()
        }) {
            continue;
        }
        for table in nodes
            .into_iter()
            .filter(|node| node.has_tag_name((P, "tbl")))
        {
            if let Some(changes) = tail(table, styles) {
                tails += 1;
                edits.extend(changes);
            }
        }
    }
    Ok((edits, tails, page_breaks))
}

pub(super) fn prepare(
    source: &Path,
    output: &Path,
    check: crate::rhwp::Check<'_>,
) -> io::Result<(usize, usize)> {
    let mut archive = package(source)?;
    if archive.index_for_name("Contents/header.xml").is_none() {
        return Ok((0, 0));
    }
    let header = part(&mut archive, "Contents/header.xml")?;
    let doc = Document::parse(&header).map_err(io::Error::other)?;
    let styles = styles(&doc);
    let sections: Vec<_> = archive
        .file_names()
        .filter_map(|name| {
            let name = name.ok()?;
            section_number(&name)?;
            Some(name.into_owned())
        })
        .collect();
    let mut replacements = HashMap::new();
    let (mut tails, mut breaks) = (0, 0);
    for name in sections {
        check()?;
        let mut source = part(&mut archive, &name)?;
        let (mut edits, tail, page_breaks) = edits(&source, &styles, check)?;
        if !edits.is_empty() {
            edits.sort_by_key(|(range, _)| range.start);
            for (range, value) in edits.into_iter().rev() {
                source.replace_range(range, &value);
            }
            replacements.insert(name, source);
        }
        tails += tail;
        breaks += page_breaks;
    }
    if replacements.is_empty() {
        return Ok((0, 0));
    }
    let mut target = ZipWriter::new(File::create(output)?);
    for i in 0..archive.len() {
        check()?;
        let mut entry = archive.by_index(i)?;
        let name = entry.name()?.into_owned();
        let options = SimpleFileOptions::default().compression_method(entry.compression());
        if entry.is_dir() {
            target.add_directory(name, options)?;
        } else {
            target.start_file(&name, options)?;
            if let Some(value) = replacements.remove(&name) {
                target.write_all(value.as_bytes())?;
            } else {
                let len = entry.size();
                crate::copy_stream(&mut entry, &mut target, len, check, |_| Ok(()))?;
            }
        }
    }
    target.finish()?;
    Ok((tails, breaks))
}
