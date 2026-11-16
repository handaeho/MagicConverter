use flate2::{Compression, write::ZlibEncoder};
use std::collections::BTreeMap;
use std::fs;
use std::io::{self, ErrorKind, Write};
use std::path::Path;

const LIMIT: usize = 64 * 1024 * 1024;
const CHECKSUM: u32 = 0xB1B0AFBA;
const TAGS: [&[u8; 4]; 63] = [
    b"cmap", b"head", b"hhea", b"hmtx", b"maxp", b"name", b"OS/2", b"post", b"cvt ", b"fpgm",
    b"glyf", b"loca", b"prep", b"CFF ", b"VORG", b"EBDT", b"EBLC", b"gasp", b"hdmx", b"kern",
    b"LTSH", b"PCLT", b"VDMX", b"vhea", b"vmtx", b"BASE", b"GDEF", b"GPOS", b"GSUB", b"EBSC",
    b"JSTF", b"MATH", b"CBDT", b"CBLC", b"COLR", b"CPAL", b"SVG ", b"sbix", b"acnt", b"avar",
    b"bdat", b"bloc", b"bsln", b"cvar", b"fdsc", b"feat", b"fmtx", b"fvar", b"gvar", b"hsty",
    b"just", b"lcar", b"mort", b"morx", b"opbd", b"prop", b"trak", b"Zapf", b"Silf", b"Glat",
    b"Gloc", b"Feat", b"Sill",
];
type Check<'a> = &'a dyn Fn() -> io::Result<()>;

#[derive(PartialEq)]
struct Font {
    flavor: [u8; 4],
    tables: BTreeMap<[u8; 4], Vec<u8>>,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

fn at<const N: usize>(data: &[u8], offset: usize) -> io::Result<[u8; N]> {
    data.get(offset..offset + N)
        .and_then(|part| part.try_into().ok())
        .ok_or_else(|| invalid("글꼴 테이블이 잘렸습니다."))
}

fn number(data: &[u8], offset: usize) -> io::Result<usize> {
    Ok(u32::from_be_bytes(at(data, offset)?) as usize)
}

fn parse(raw: &[u8], check: Check<'_>) -> io::Result<Font> {
    let flavor = at(raw, 0)?;
    if flavor == *b"ttcf" {
        return Err(invalid(
            "TTC/OTC 글꼴 모음은 개별 폰트로 분리한 뒤 사용하세요.",
        ));
    }
    let decoded;
    let data = if matches!(&flavor, b"wOFF" | b"wOF2") {
        if number(raw, 16)? > LIMIT || number(raw, 8)? != raw.len() {
            return Err(invalid(
                "웹폰트의 압축 해제 크기나 헤더가 올바르지 않습니다.",
            ));
        }
        decoded = if &flavor == b"wOFF" {
            wuff::decompress_woff1(raw)
        } else {
            wuff::decompress_woff2(raw)
        }
        .map_err(|error| invalid(format!("웹폰트의 압축을 읽을 수 없습니다: {error:?}")))?;
        &decoded
    } else {
        raw
    };
    let flavor = at(data, 0)?;
    if !matches!(&flavor, b"\0\x01\0\0" | b"OTTO" | b"true") {
        return Err(invalid("지원하지 않는 OpenType 글꼴 컨테이너입니다."));
    }
    let count = u16::from_be_bytes(at(data, 4)?) as usize;
    if !(1..=4095).contains(&count) || 12 + count * 16 > data.len() || data.len() > LIMIT {
        return Err(invalid("글꼴 테이블 디렉터리 크기가 올바르지 않습니다."));
    }
    let mut tables = BTreeMap::new();
    let mut occupied = Vec::new();
    let mut bytes = 0;
    for index in 0..count {
        check()?;
        let record = 12 + index * 16;
        let tag = at(data, record)?;
        let offset = number(data, record + 8)?;
        let length = number(data, record + 12)?;
        let end = offset
            .checked_add(length)
            .ok_or_else(|| invalid("글꼴 테이블 길이가 올바르지 않습니다."))?;
        let value = data
            .get(offset..end)
            .ok_or_else(|| invalid("글꼴 테이블이 잘렸습니다."))?;
        bytes += length;
        if bytes > LIMIT || length != 0 && (offset < 12 + count * 16 || !offset.is_multiple_of(4)) {
            return Err(invalid("글꼴 테이블 오프셋 또는 크기가 올바르지 않습니다."));
        }
        if length > 0 {
            occupied.push((offset, end));
        }
        if tables.insert(tag, value.to_vec()).is_some() {
            return Err(invalid("중복 글꼴 테이블을 읽을 수 없습니다."));
        }
    }
    occupied.sort_unstable();
    if occupied.windows(2).any(|pair| pair[0].1 > pair[1].0) {
        return Err(invalid("글꼴 테이블이 겹칩니다."));
    }
    Ok(Font { flavor, tables })
}

fn checksum(data: &[u8]) -> u32 {
    data.chunks(4).fold(0_u32, |sum, part| {
        let mut bytes = [0_u8; 4];
        bytes[..part.len()].copy_from_slice(part);
        sum.wrapping_add(u32::from_be_bytes(bytes))
    })
}

fn put16(output: &mut Vec<u8>, value: u16) {
    output.extend_from_slice(&value.to_be_bytes());
}
fn put32(output: &mut Vec<u8>, value: usize) {
    output.extend_from_slice(&(value as u32).to_be_bytes());
}
fn update32(output: &mut [u8], offset: usize, value: u32) {
    output[offset..offset + 4].copy_from_slice(&value.to_be_bytes());
}
fn padded(length: usize) -> usize {
    (length + 3) & !3
}
fn align(output: &mut Vec<u8>) {
    output.resize(padded(output.len()), 0);
}

fn sfnt(font: &Font, check: Check<'_>) -> io::Result<Vec<u8>> {
    let count = font.tables.len() as u16;
    let selector = count.ilog2() as u16;
    let search = 16 << selector;
    let mut output = font.flavor.to_vec();
    for value in [count, search, selector, count * 16 - search] {
        put16(&mut output, value);
    }
    let mut offset = 12 + font.tables.len() * 16;
    let mut adjustment = 0;
    for (tag, table) in &font.tables {
        check()?;
        output.extend_from_slice(tag);
        put32(&mut output, checksum(table) as usize);
        put32(&mut output, offset);
        put32(&mut output, table.len());
        if tag == b"head" {
            adjustment = offset + 8;
        }
        offset += padded(table.len());
    }
    for table in font.tables.values() {
        check()?;
        output.extend_from_slice(table);
        align(&mut output);
    }
    let value = CHECKSUM.wrapping_sub(checksum(&output));
    update32(&mut output, adjustment, value);
    Ok(output)
}

fn woff_header(font: &Font, format: &[u8; 4], sfnt_size: usize) -> Vec<u8> {
    let mut output = format.to_vec();
    output.extend_from_slice(&font.flavor);
    put32(&mut output, 0);
    put16(&mut output, font.tables.len() as u16);
    put16(&mut output, 0);
    put32(&mut output, sfnt_size);
    if format == b"wOF2" {
        put32(&mut output, 0);
    }
    output.extend_from_slice(&font.tables[b"head"][4..8]);
    output.extend_from_slice(&[0; 20]);
    output
}

fn woff1(font: &Font, sfnt_size: usize, check: Check<'_>) -> io::Result<Vec<u8>> {
    let mut output = woff_header(font, b"wOFF", sfnt_size);
    output.resize(44 + 20 * font.tables.len(), 0);
    for (index, (tag, table)) in font.tables.iter().enumerate() {
        check()?;
        let mut zip = ZlibEncoder::new(Vec::new(), Compression::best());
        zip.write_all(table)?;
        let zipped = zip.finish()?;
        let data = if zipped.len() < table.len() {
            &zipped
        } else {
            table
        };
        let offset = output.len();
        let directory = 44 + index * 20;
        output[directory..directory + 4].copy_from_slice(tag);
        for (field, value) in [
            offset as u32,
            data.len() as u32,
            table.len() as u32,
            checksum(table),
        ]
        .into_iter()
        .enumerate()
        {
            update32(&mut output, directory + 4 + field * 4, value);
        }
        output.extend_from_slice(data);
        align(&mut output);
    }
    let length = output.len() as u32;
    update32(&mut output, 8, length);
    Ok(output)
}

fn base128(output: &mut Vec<u8>, length: usize) {
    let mut started = false;
    for shift in [28, 21, 14, 7, 0] {
        let digit = ((length >> shift) & 127) as u8;
        if started || digit != 0 || shift == 0 {
            output.push(digit | if shift == 0 { 0 } else { 128 });
            started = true;
        }
    }
}

fn woff2(font: &Font, sfnt_size: usize, check: Check<'_>) -> io::Result<Vec<u8>> {
    let mut output = woff_header(font, b"wOF2", sfnt_size);
    let mut zipped = Vec::new();
    {
        let mut encoder = brotli::CompressorWriter::new(&mut zipped, 64 * 1024, 9, 22);
        for (tag, table) in &font.tables {
            check()?;
            let index = TAGS.iter().position(|known| *known == tag).unwrap_or(63) as u8;
            let transform = if matches!(tag, b"glyf" | b"loca") {
                0xC0
            } else {
                0
            };
            output.push(index | transform);
            if index == 63 {
                output.extend_from_slice(tag);
            }
            base128(&mut output, table.len());
            for part in table.chunks(64 * 1024) {
                check()?;
                encoder.write_all(part)?;
            }
        }
        encoder.flush()?;
    }
    update32(&mut output, 20, zipped.len() as u32);
    output.extend_from_slice(&zipped);
    align(&mut output);
    let length = output.len() as u32;
    update32(&mut output, 8, length);
    Ok(output)
}

fn canonical(font: &mut Font) -> io::Result<()> {
    let head = font
        .tables
        .get_mut(b"head")
        .ok_or_else(|| invalid("글꼴에 head 테이블이 없습니다."))?;
    if head.len() < 54 {
        return Err(invalid("글꼴의 head 테이블이 잘렸습니다."));
    }
    head[8..12].fill(0);
    Ok(())
}

pub fn convert(
    source: &Path,
    output: &Path,
    target: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let source_bytes = crate::data::bytes(source)?;
    let mut font = parse(&source_bytes, check)?;
    let truetype = font.tables.contains_key(b"glyf");
    if target == "ttf" && !truetype {
        return Err(invalid(
            "CFF/CFF2 외곽선의 TTF 근사 변환은 지원하지 않습니다. OTF 또는 WOFF를 선택하세요.",
        ));
    }
    if !truetype && !font.tables.contains_key(b"CFF ") && !font.tables.contains_key(b"CFF2") {
        return Err(invalid("지원하는 외곽선 테이블이 없는 폰트입니다."));
    }
    if font.tables.remove(b"DSIG").is_some() {
        warnings
            .push("컨테이너 변경으로 무효화되는 폰트 디지털 서명(DSIG)을 제거했습니다.".to_owned());
    }
    let fs_type = font
        .tables
        .get(b"OS/2")
        .and_then(|table| at(table, 8).ok())
        .map(u16::from_be_bytes)
        .map_or_else(|| "None".to_owned(), |flag| flag.to_string());
    warnings.push(format!("폰트의 OS/2 fsType={fs_type} 및 이름/라이선스 테이블을 보존합니다. 변환은 폰트의 임베딩·재배포 권한을 부여하지 않습니다."));
    if target == "otf" && truetype {
        warnings.push("TrueType 외곽선을 가진 OpenType(.otf)으로 저장했습니다. CFF 외곽선으로 바꾸지 않습니다.".to_owned());
    }
    if source_bytes.starts_with(b"wOFF") || source_bytes.starts_with(b"wOF2") {
        warnings.push(
            "웹폰트 전용 메타데이터/사설 데이터는 다른 컨테이너에 복사하지 않습니다.".to_owned(),
        );
    }
    canonical(&mut font)?;
    let standard = sfnt(&font, check)?;
    let bytes = match target {
        "woff" => woff1(&font, standard.len(), check)?,
        "woff2" => woff2(&font, standard.len(), check)?,
        "ttf" | "otf" => standard,
        _ => return Err(invalid("지원하지 않는 글꼴 출력 컨테이너입니다.")),
    };
    let mut verification = parse(&bytes, check)?;
    canonical(&mut verification)?;
    if verification != font {
        return Err(invalid(
            "폰트 글리프·문자 매핑·테이블·가변 축·fsType 보존 검증에 실패했습니다.",
        ));
    }
    check()?;
    fs::write(output, bytes)?;
    check()
}
