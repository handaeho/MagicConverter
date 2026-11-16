//! The primary data-stream copy used by the pilot worker and standalone CLI.

use std::io::{self, Error, ErrorKind, Read, Write};

const CHUNK_SIZE: usize = 1024 * 1024;

pub fn copy_stream(
    mut source: impl Read,
    mut target: impl Write,
    expected: u64,
    mut check: impl FnMut() -> io::Result<()>,
    mut progress: impl FnMut(u64) -> io::Result<()>,
) -> io::Result<u64> {
    let mut buffer = vec![0; CHUNK_SIZE];
    let mut copied = 0_u64;
    loop {
        check()?;
        let count = source.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        copied += count as u64;
        if copied > expected {
            return Err(Error::new(ErrorKind::InvalidData, "source size changed"));
        }
        target.write_all(&buffer[..count])?;
        progress(copied)?;
    }
    if copied != expected {
        return Err(Error::new(ErrorKind::InvalidData, "source size changed"));
    }
    target.flush()?;
    check()?;
    Ok(copied)
}
