use super::{
    Check, LIMIT, Limits, MAX_BYTES, MAX_ENTRIES, MAX_HEADERS, Member, Notices, Visitor, invalid,
    safe_name,
};
use rars::{ArchiveReadOptions, ArchiveReader, AttrSource};
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs::{File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::Duration;

#[derive(Serialize, Deserialize)]
struct Record {
    name: String,
    size: u64,
    directory: bool,
}

struct Bounded {
    file: File,
    remaining: u64,
}

impl Write for Bounded {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        if bytes.len() as u64 > self.remaining {
            return Err(invalid("RAR 항목의 실제 크기가 선언한 크기를 초과합니다."));
        }
        let written = self.file.write(bytes)?;
        self.remaining -= written as u64;
        Ok(written)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

fn spool(source: &Path, folder: &Path) -> io::Result<Vec<Record>> {
    let options = ArchiveReadOptions::default()
        .with_max_header_count(MAX_ENTRIES as u64 * 2)
        .with_max_header_bytes(MAX_HEADERS)
        .with_max_member_output_bytes(MAX_BYTES)
        .with_max_total_output_bytes(MAX_BYTES);
    let archive =
        ArchiveReader::read_path_with_options(source, options).map_err(io::Error::other)?;
    let mut limits = Limits::default();
    let mut records = Vec::new();
    let mut raw_names = Vec::new();
    for entry in archive.members() {
        let meta = &entry.meta;
        if meta.is_encrypted {
            return Err(invalid("암호화 RAR은 지원하지 않습니다."));
        }
        if meta.is_split_before || meta.is_split_after {
            return Err(invalid("분할 RAR은 지원하지 않습니다."));
        }
        if meta.is_redirection
            || (meta.attr_source() == AttrSource::Unix
                && !matches!(meta.file_attr & 0o170000, 0 | 0o100000 | 0o040000))
        {
            return Err(invalid("링크/장치 압축 항목은 지원하지 않습니다."));
        }
        let decoded = entry.decoded_name(None).map_err(io::Error::other)?;
        let decoded = std::str::from_utf8(&decoded).map_err(io::Error::other)?;
        let mut member = Member {
            name: safe_name(decoded)?,
            size: if meta.is_directory {
                0
            } else {
                meta.unpacked_size
            },
            directory: meta.is_directory,
            mode: None,
            modified: None,
        };
        limits.accept(&mut member)?;
        raw_names.push(meta.name.clone());
        records.push(Record {
            name: member.name,
            size: member.size,
            directory: member.directory,
        });
    }
    let spool = folder.join("members.bin");
    File::create_new(&spool)?;
    let mut offset = 0_u64;
    let mut next = 0;
    archive
        .extract_with_links(
            options,
            |meta| {
                let record = records
                    .get(next)
                    .ok_or_else(|| invalid("RAR 항목 수가 바뀌었습니다."))?;
                if record.directory != meta.is_directory || raw_names[next] != meta.name {
                    return Err(invalid("RAR 항목 순서가 바뀌었습니다.").into());
                }
                next += 1;
                if record.directory {
                    return Ok(Box::new(io::sink()));
                }
                let mut file = OpenOptions::new().write(true).open(&spool)?;
                file.seek(SeekFrom::Start(offset))?;
                offset += record.size;
                Ok(Box::new(Bounded {
                    file,
                    remaining: record.size,
                }))
            },
            |_, _| Err(invalid("링크/장치 압축 항목은 지원하지 않습니다.").into()),
        )
        .map_err(io::Error::other)?;
    if next != records.len() || spool.metadata()?.len() != offset {
        return Err(invalid("RAR 항목 수 또는 크기가 올바르지 않습니다."));
    }
    Ok(records)
}

pub(crate) fn worker(source: &Path, folder: &Path) -> io::Result<u8> {
    let report = folder.join("members.json");
    let result = spool(source, folder);
    let code = if result.is_ok() { 0 } else { 1 };
    let report_data = match result {
        Ok(records) => serde_json::json!({"records": records}),
        Err(error) => serde_json::json!({"error": error.to_string()}),
    };
    crate::write_report(&report, &report_data)?;
    Ok(code)
}

pub(super) fn visit(
    source: &Path,
    check: Check<'_>,
    visitor: &mut Visitor<'_>,
    notices: &mut Notices,
) -> io::Result<()> {
    let directory = tempfile::Builder::new().prefix("magic-rar-").tempdir()?;
    let args = [
        OsString::from("rar-spool"),
        source.as_os_str().to_owned(),
        directory.path().as_os_str().to_owned(),
    ];
    let process = crate::engine_process::run(
        &std::env::current_exe()?,
        &args,
        Some(Duration::from_secs(3600)),
        None,
        check,
        &|| Ok(()),
    )?;
    let report: serde_json::Value = serde_json::from_reader(
        File::open(directory.path().join("members.json"))?.take(MAX_HEADERS),
    )?;
    if process.code != 0 {
        return Err(invalid(
            report["error"]
                .as_str()
                .unwrap_or("RAR 변환 작업이 실패했습니다."),
        ));
    }
    let records: Vec<Record> = serde_json::from_value(report["records"].clone())?;
    if records.len() > MAX_ENTRIES {
        return Err(invalid(LIMIT));
    }
    let mut spool = File::open(directory.path().join("members.bin"))?;
    let mut offset = 0_u64;
    notices.insert("RAR 항목의 수정 시각·권한은 이 변환 경로에서 보존하지 않습니다.");
    for record in records {
        check()?;
        let item = Member {
            name: record.name,
            size: record.size,
            directory: record.directory,
            mode: None,
            modified: None,
        };
        if item.directory {
            visitor(&item, &mut io::empty(), notices)?;
        } else {
            let size = item.size;
            spool.seek(SeekFrom::Start(offset))?;
            visitor(&item, &mut (&mut spool).take(size), notices)?;
            offset += size;
        }
    }
    Ok(())
}
