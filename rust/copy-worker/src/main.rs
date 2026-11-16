use magic_copy_worker::copy_stream;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Error, ErrorKind, Write};
use std::path::Path;
use std::process::ExitCode;
use std::time::Instant;

fn copy(source: &Path, target: &Path, output: &mut impl Write) -> io::Result<()> {
    let started = Instant::now();
    let source = File::open(source)?;
    let metadata = source.metadata()?;
    if !metadata.is_file() {
        return Err(Error::new(ErrorKind::InvalidInput, "source is not a file"));
    }

    // The supervisor owns the private directory. Exclusive creation also
    // protects existing files and aliases if the worker is invoked directly.
    let destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)?;
    let expected = metadata.len();
    let mut last_percent = 0;
    let result = copy_stream(
        source,
        destination,
        expected,
        || Ok(()),
        |copied| {
            let percent = (u128::from(copied) * 100 / u128::from(expected)).min(99);
            if percent > last_percent {
                writeln!(output, "progress {percent}")?;
                last_percent = percent;
            }
            Ok(())
        },
    )
    .and_then(|copied| writeln!(output, "copied {copied} {}", started.elapsed().as_nanos()));
    if result.is_err() {
        let _ = fs::remove_file(target);
    }
    result
}

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let (Some(source), Some(target), None) = (args.next(), args.next(), args.next()) else {
        eprintln!("usage: magic-copy-worker SOURCE PRIVATE_TARGET");
        return ExitCode::from(2);
    };
    match copy(
        Path::new(&source),
        Path::new(&target),
        &mut io::stdout().lock(),
    ) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
