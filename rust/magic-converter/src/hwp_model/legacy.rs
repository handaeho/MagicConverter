use super::*;
use std::io::Read;

const LIMIT: u64 = 32 * 1024 * 1024;

fn inflate(bytes: &[u8]) -> io::Result<Vec<u8>> {
    let mut output = Vec::new();
    flate2::read::DeflateDecoder::new(bytes)
        .take(LIMIT + 1)
        .read_to_end(&mut output)?;
    if output.len() as u64 > LIMIT {
        return Err(invalid("HWP 스트림의 압축 해제 한도를 초과했습니다."));
    }
    Ok(output)
}

fn word(bytes: &[u8], start: usize) -> Option<u16> {
    Some(u16::from_le_bytes(
        bytes.get(start..start + 2)?.try_into().ok()?,
    ))
}

fn dword(bytes: &[u8], start: usize) -> Option<u32> {
    Some(u32::from_le_bytes(
        bytes.get(start..start + 4)?.try_into().ok()?,
    ))
}

struct Record<'a> {
    tag: u32,
    data: &'a [u8],
    children: Vec<usize>,
}

fn records(mut bytes: &[u8]) -> io::Result<(Vec<Record<'_>>, Vec<usize>)> {
    let mut records: Vec<Record<'_>> = Vec::new();
    let mut stack = Vec::<(u32, usize)>::new();
    let mut roots = Vec::new();
    while let Some(header) = dword(bytes, 0) {
        let tag = header & 1023;
        let level = (header >> 10) & 1023;
        let mut size = header >> 20;
        bytes = &bytes[4..];
        if size == 4095 {
            size = dword(bytes, 0).ok_or_else(|| invalid("HWP 레코드 크기가 잘렸습니다."))?;
            bytes = &bytes[4..];
        }
        if records.len() >= 200_000 || level > 64 {
            return Err(invalid("HWP 레코드의 크기 또는 중첩 한도를 초과했습니다."));
        }
        let (data, tail) = bytes
            .split_at_checked(size as usize)
            .ok_or_else(|| invalid("HWP 레코드 본문이 잘렸습니다."))?;
        bytes = tail;
        while stack.last().is_some_and(|(before, _)| *before >= level) {
            stack.pop();
        }
        let next = records.len();
        if let Some((_, parent)) = stack.last() {
            records[*parent].children.push(next);
        } else {
            roots.push(next);
        }
        records.push(Record {
            tag,
            data,
            children: Vec::new(),
        });
        stack.push((level, next));
    }
    Ok((records, roots))
}

fn decode_text(bytes: &[u8]) -> String {
    let mut units = Vec::new();
    let mut offset = 0;
    while let Some(unit) = word(bytes, offset) {
        if (1..=23).contains(&unit) && !matches!(unit, 10 | 13) {
            if unit == 9 {
                units.push(9);
            }
            offset += 16;
        } else {
            match unit {
                10 | 32.. => units.push(unit),
                24 => units.push(45),
                30 | 31 => units.push(32),
                _ => {}
            }
            offset += 2;
        }
    }
    String::from_utf16_lossy(&units)
}

struct Reader<'a> {
    records: Vec<Record<'a>>,
    assets: &'a BTreeAssets,
    check: Check<'a>,
    tables: usize,
    pictures: usize,
}

type BTreeAssets = std::collections::BTreeMap<u16, Vec<u8>>;

impl Reader<'_> {
    fn picture(&mut self, index: usize, output: &mut String) -> io::Result<()> {
        let data = self.records[index].data;
        let id = word(data, 71).ok_or_else(|| invalid("HWP 그림 레코드가 잘렸습니다."))?;
        let bytes = self
            .assets
            .get(&id)
            .ok_or_else(|| invalid("HWP 그림의 데이터를 찾을 수 없습니다."))?;
        let picture = image_html(bytes, None, None, [0; 4])
            .or_else(|_| image_html(&inflate(bytes)?, None, None, [0; 4]))?;
        output.push_str(&picture);
        self.pictures += 1;
        Ok(())
    }

    fn table(
        &mut self,
        control: usize,
        table: usize,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        let table = self.records[table].data;
        let (rows, cols) = (
            usize::from(word(table, 4).unwrap_or(0)),
            usize::from(word(table, 6).unwrap_or(0)),
        );
        if rows == 0 || cols == 0 || rows.saturating_mul(cols) > 100_000 {
            return Err(invalid("HWP 표의 크기가 허용 범위를 벗어납니다."));
        }
        let mut groups = vec![Vec::new(); rows];
        let children = self.records[control].children.clone();
        for (position, &index) in children.iter().enumerate() {
            let child = &self.records[index];
            if child.tag != 72 || child.data.len() < 34 {
                continue;
            }
            let [c, r, cs, rs] =
                [8, 10, 12, 14].map(|offset| usize::from(word(child.data, offset).unwrap_or(0)));
            if cs == 0 || rs == 0 {
                continue;
            }
            if c + cs > cols || r + rs > rows {
                return Err(invalid("HWP 표 병합 범위가 올바르지 않습니다."));
            }
            let mut contents = child.children.clone();
            contents.extend(
                children[position + 1..]
                    .iter()
                    .copied()
                    .take_while(|index| self.records[*index].tag != 72),
            );
            groups[r].push((c, cs, rs, contents));
        }
        output.push_str("<table>");
        for mut group in groups {
            output.push_str("<tr>");
            group.sort_by_key(|cell| cell.0);
            for (c, cs, rs, contents) in group {
                write!(
                    output,
                    r#"<td data-col="{c}" colspan="{cs}" rowspan="{rs}">"#
                )
                .unwrap();
                for index in contents
                    .into_iter()
                    .filter(|index| self.records[*index].tag == 66)
                    .collect::<Vec<_>>()
                {
                    self.paragraph(index, output, depth + 1)?;
                }
                output.push_str("</td>");
            }
            output.push_str("</tr>");
        }
        output.push_str("</table>");
        self.tables += 1;
        Ok(())
    }

    fn paragraphs_in(&mut self, index: usize, output: &mut String, depth: usize) -> io::Result<()> {
        if depth > 64 {
            return Err(invalid("HWP 개체의 중첩 한도를 초과했습니다."));
        }
        for child in self.records[index].children.clone() {
            match self.records[child].tag {
                66 => self.paragraph(child, output, depth + 1)?,
                85 => {
                    output.push_str("<p>");
                    self.picture(child, output)?;
                    output.push_str("</p>");
                }
                _ => self.paragraphs_in(child, output, depth + 1)?,
            }
        }
        Ok(())
    }

    fn paragraph(&mut self, index: usize, output: &mut String, depth: usize) -> io::Result<()> {
        if depth > 64 {
            return Err(invalid("HWP 문단의 중첩 한도를 초과했습니다."));
        }
        (self.check)()?;
        let record = &self.records[index];
        let text = if record.tag == 67 {
            record.data
        } else {
            record
                .children
                .iter()
                .find(|index| self.records[**index].tag == 67)
                .map(|index| self.records[*index].data)
                .unwrap_or_default()
        };
        let text = decode_text(text);
        let new_page = record.tag == 66 && record.data.get(11).is_some_and(|flag| flag & 4 != 0);
        let controls: Vec<_> = record
            .children
            .iter()
            .copied()
            .filter(|index| self.records[*index].tag == 71)
            .collect();
        if !text.is_empty() || controls.is_empty() || new_page {
            write!(
                output,
                r#"<p data-page="{}">{}</p>"#,
                u8::from(new_page),
                escape(&text).replace('\n', "<br>")
            )
            .unwrap();
        }
        for control in controls {
            if let Some(table) = self.records[control]
                .children
                .iter()
                .copied()
                .find(|index| self.records[*index].tag == 77)
            {
                self.table(control, table, output, depth + 1)?;
            } else {
                self.paragraphs_in(control, output, depth + 1)?;
            }
            if output.len() > 128 * 1024 * 1024 {
                return Err(invalid("HWP 문서 모델의 크기 한도를 초과했습니다."));
            }
        }
        Ok(())
    }
}

pub(super) fn read(source: &Path, check: Check<'_>) -> io::Result<String> {
    let archive = compound::Compound::read(source, check)?;
    let header = archive.stream(archive.find(0, "FileHeader", 2)?, check)?;
    if !header.starts_with(b"HWP Document File") {
        return Err(invalid("HWP 5.x 문서가 아닙니다."));
    }
    let flags = dword(&header, 36).ok_or_else(|| invalid("HWP 파일 헤더가 잘렸습니다."))?;
    if flags & 6 != 0 {
        return Err(invalid(
            "암호/배포용 HWP는 보호가 해제된 문서로 변환하세요.",
        ));
    }
    if archive.find(0, "DocInfo", 2).is_ok() {
        return Err(invalid(
            "문서 정보를 가진 HWP는 rhwp 엔진에서 열어야 합니다.",
        ));
    }
    let body = archive.find(0, "BodyText", 1)?;
    let mut sections: Vec<_> = archive
        .children(body)?
        .into_iter()
        .filter(|(_, _, kind)| *kind == 2)
        .filter_map(|(entry, name, _)| {
            let index = name.strip_prefix("Section")?.parse::<usize>().ok()?;
            Some((index, entry))
        })
        .collect();
    sections.sort();
    if sections.is_empty() {
        return Err(invalid("HWP 본문 스트림이 없습니다."));
    }
    let mut assets = BTreeAssets::new();
    let mut bytes = 0;
    if let Ok(data) = archive.find(0, "BinData", 1) {
        for (entry, name, kind) in archive.children(data)? {
            check()?;
            if kind == 2
                && let Some((stem, _)) = name.split_once('.')
                && let Some(id) = stem
                    .to_ascii_uppercase()
                    .strip_prefix("BIN")
                    .and_then(|hex| u16::from_str_radix(hex, 16).ok())
            {
                let content = archive.stream(entry, check)?;
                bytes += content.len();
                if bytes > 128 * 1024 * 1024 {
                    return Err(invalid("HWP 포함 그림의 용량 한도를 초과했습니다."));
                }
                assets.insert(id, content);
            }
        }
    }
    let mut geometry = GEOMETRY;
    let mut result = String::new();
    for (index, (_, section)) in sections.into_iter().enumerate() {
        check()?;
        let data = archive.stream(section, check)?;
        let data = if flags & 1 != 0 {
            inflate(&data)?
        } else {
            data
        };
        let (records, roots) = records(&data)?;
        if index == 0
            && let Some(record) = records
                .iter()
                .find(|record| record.tag == 73 && record.data.len() >= 24)
        {
            for (i, metric) in geometry[..6].iter_mut().enumerate() {
                *metric = dword(record.data, i * 4).unwrap_or_default().min(500_000) as i32 / 5;
            }
        }
        let expected =
            [77, 85].map(|tag| records.iter().filter(|record| record.tag == tag).count());
        let mut reader = Reader {
            records,
            assets: &assets,
            check,
            tables: 0,
            pictures: 0,
        };
        for index in roots {
            if matches!(reader.records[index].tag, 66 | 67) {
                reader.paragraph(index, &mut result, 0)?;
            }
        }
        if expected != [reader.tables, reader.pictures] {
            return Err(invalid("HWP 표 또는 그림을 모두 복원하지 못했습니다."));
        }
    }
    let geometry = geometry.map(|value| value.to_string()).join(" ");
    Ok(format!(
        r#"<main data-geometry="{geometry}">{result}</main>"#
    ))
}
