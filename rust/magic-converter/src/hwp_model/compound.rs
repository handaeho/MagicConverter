use super::{Check, invalid};
use std::collections::HashSet;
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;

const END: u32 = 0xffff_fffe;
const FREE: u32 = 0xffff_ffff;
const STREAM_LIMIT: usize = 32 * 1024 * 1024;
const FILE_LIMIT: u64 = 256 * 1024 * 1024;

fn short(data: &[u8], offset: usize) -> u16 {
    u16::from_le_bytes(data[offset..offset + 2].try_into().unwrap())
}

fn long(data: &[u8], offset: usize) -> u32 {
    u32::from_le_bytes(data[offset..offset + 4].try_into().unwrap())
}

fn sector(data: &[u8], id: u32, width: usize) -> io::Result<&[u8]> {
    let start = (id as usize)
        .checked_mul(width)
        .ok_or_else(|| invalid("HWP 섹터 위치가 올바르지 않습니다."))?;
    data.get(start..start + width)
        .ok_or_else(|| invalid("HWP 섹터가 파일 밖을 가리킵니다."))
}

fn words(data: &[u8]) -> impl Iterator<Item = u32> + '_ {
    data.chunks_exact(4).map(|chunk| long(chunk, 0))
}

fn chain(
    data: &[u8],
    table: &[u32],
    mut id: u32,
    width: usize,
    length: Option<usize>,
    check: Check<'_>,
) -> io::Result<Vec<u8>> {
    let cap = length.unwrap_or(STREAM_LIMIT);
    if cap > STREAM_LIMIT {
        return Err(invalid("HWP 스트림의 크기 한도를 초과했습니다."));
    }
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    while id != END && result.len() < cap {
        if seen.len() % 256 == 0 {
            check()?;
        }
        if !seen.insert(id) {
            return Err(invalid("HWP 섹터 연결이 순환합니다."));
        }
        let content = sector(data, id, width)?;
        result.extend_from_slice(&content[..content.len().min(cap - result.len())]);
        id = *table
            .get(id as usize)
            .ok_or_else(|| invalid("HWP 섹터 할당 정보가 없습니다."))?;
    }
    if length.is_some_and(|expected| result.len() != expected) || id != END && length.is_none() {
        return Err(invalid("HWP 섹터 연결의 길이가 올바르지 않습니다."));
    }
    Ok(result)
}

struct Entry {
    name: String,
    kind: u8,
    left: u32,
    right: u32,
    child: u32,
    sector: u32,
    size: usize,
}

pub(super) struct Compound {
    file: Vec<u8>,
    width: usize,
    fat: Vec<u32>,
    mini: Vec<u8>,
    mini_fat: Vec<u32>,
    entries: Vec<Entry>,
}

impl Compound {
    pub fn read(path: &Path, check: Check<'_>) -> io::Result<Self> {
        check()?;
        let file = File::open(path)?;
        if file.metadata()?.len() > FILE_LIMIT {
            return Err(invalid("기본 HWP 리더의 파일 크기 한도를 초과했습니다."));
        }
        let mut data = Vec::new();
        file.take(FILE_LIMIT + 1).read_to_end(&mut data)?;
        if data.len() as u64 > FILE_LIMIT
            || data.len() < 512
            || !data.starts_with(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            || short(&data, 28) != 0xfffe
            || short(&data, 32) != 6
            || long(&data, 56) != 4096
        {
            return Err(invalid("HWP 복합 파일 헤더가 올바르지 않습니다."));
        }
        let width = match (short(&data, 26), short(&data, 30)) {
            (3, 9) => 512,
            (4, 12) => 4096,
            _ => return Err(invalid("HWP 복합 파일 버전을 읽을 수 없습니다.")),
        };
        let file = data
            .get(width..)
            .ok_or_else(|| invalid("HWP 복합 파일이 잘렸습니다."))?;
        let sector_count = file.len() / width;
        let fat_count = long(&data, 44) as usize;
        let difat_count = long(&data, 72) as usize;
        if fat_count > sector_count.div_ceil(width / 4) + 1
            || difat_count > sector_count.div_ceil(width / 4)
        {
            return Err(invalid("HWP 섹터 할당 테이블 크기가 올바르지 않습니다."));
        }
        let mut fat_ids: Vec<_> = words(&data[76..512]).filter(|id| *id != FREE).collect();
        let mut difat = long(&data, 68);
        let mut seen = HashSet::new();
        for _ in 0..difat_count {
            check()?;
            if !seen.insert(difat) {
                return Err(invalid("HWP 확장 할당 테이블이 순환합니다."));
            }
            let part = sector(file, difat, width)?;
            fat_ids.extend(words(&part[..width - 4]).filter(|id| *id != FREE));
            difat = long(part, width - 4);
        }
        if fat_ids.len() != fat_count {
            return Err(invalid("HWP 섹터 할당 테이블 수가 올바르지 않습니다."));
        }
        let mut fat = Vec::new();
        let mut seen = HashSet::new();
        for id in fat_ids {
            check()?;
            if !seen.insert(id) {
                return Err(invalid("HWP 섹터 할당 테이블이 중복됩니다."));
            }
            fat.extend(words(sector(file, id, width)?));
        }
        let directory = chain(file, &fat, long(&data, 48), width, None, check)?;
        if directory.len() > 128 * 10_000 {
            return Err(invalid("HWP 스트림 개수 한도를 초과했습니다."));
        }
        let mut entries = Vec::new();
        for row in directory.chunks_exact(128) {
            let kind = row[66];
            let length = usize::from(short(row, 64));
            let name = if kind == 0 {
                String::new()
            } else {
                if !(2..=64).contains(&length) || length % 2 != 0 {
                    return Err(invalid("HWP 스트림 이름 길이가 올바르지 않습니다."));
                }
                let name: Vec<_> = row[..length - 2]
                    .chunks_exact(2)
                    .map(|chunk| short(chunk, 0))
                    .collect();
                String::from_utf16(&name).map_err(io::Error::other)?
            };
            let size = if width == 512 {
                long(row, 120) as usize
            } else {
                usize::try_from(u64::from_le_bytes(row[120..128].try_into().unwrap()))
                    .map_err(io::Error::other)?
            };
            entries.push(Entry {
                name,
                kind,
                left: long(row, 68),
                right: long(row, 72),
                child: long(row, 76),
                sector: long(row, 116),
                size,
            });
        }
        let root = entries
            .first()
            .filter(|root| root.kind == 5)
            .ok_or_else(|| invalid("HWP 복합 파일의 루트 스트림이 없습니다."))?;
        let mini_length = (long(&data, 64) as usize)
            .checked_mul(width)
            .ok_or_else(|| invalid("HWP 미니 섹터 테이블이 너무 큽니다."))?;
        let mini = chain(file, &fat, root.sector, width, Some(root.size), check)?;
        let mini_fat = words(&chain(
            file,
            &fat,
            long(&data, 60),
            width,
            Some(mini_length),
            check,
        )?)
        .collect();
        Ok(Self {
            file: data,
            width,
            fat,
            mini,
            mini_fat,
            entries,
        })
    }

    pub fn children(&self, parent: usize) -> io::Result<Vec<(usize, &str, u8)>> {
        let mut stack = vec![
            self.entries
                .get(parent)
                .filter(|entry| matches!(entry.kind, 1 | 5))
                .ok_or_else(|| invalid("HWP 하위 저장소가 없습니다."))?
                .child,
        ];
        let mut seen = HashSet::new();
        let mut names = HashSet::new();
        let mut result = Vec::new();
        while let Some(id) = stack.pop() {
            if id == FREE {
                continue;
            }
            let entry = self
                .entries
                .get(id as usize)
                .filter(|entry| matches!(entry.kind, 1 | 2))
                .ok_or_else(|| invalid("HWP 디렉터리 연결이 올바르지 않습니다."))?;
            if !seen.insert(id) || !names.insert(entry.name.to_ascii_lowercase()) {
                return Err(invalid("HWP 디렉터리에 순환 또는 중복된 항목이 있습니다."));
            }
            stack.extend([entry.left, entry.right]);
            result.push((id as usize, entry.name.as_str(), entry.kind));
        }
        Ok(result)
    }

    pub fn find(&self, parent: usize, name: &str, kind: u8) -> io::Result<usize> {
        self.children(parent)?
            .into_iter()
            .find(|(_, entry, ty)| *ty == kind && entry.eq_ignore_ascii_case(name))
            .map(|(index, _, _)| index)
            .ok_or_else(|| invalid("HWP 스트림 또는 저장소를 찾을 수 없습니다."))
    }

    pub fn stream(&self, id: usize, check: Check<'_>) -> io::Result<Vec<u8>> {
        let entry = self
            .entries
            .get(id)
            .filter(|entry| entry.kind == 2)
            .ok_or_else(|| invalid("HWP 스트림 항목이 아닙니다."))?;
        let (data, table, width) = if entry.size < 4096 {
            (self.mini.as_slice(), self.mini_fat.as_slice(), 64)
        } else {
            (&self.file[self.width..], self.fat.as_slice(), self.width)
        };
        chain(data, table, entry.sector, width, Some(entry.size), check)
    }
}
