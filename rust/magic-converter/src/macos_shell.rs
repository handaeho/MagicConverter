use crate::Common;
use clap::{Args, ValueEnum};
use serde_json::{Value, json};
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

#[derive(Args)]
pub struct Shell {
    #[command(flatten)]
    pub common: Common,
    operation: Operation,
}

#[derive(Clone, ValueEnum)]
enum Operation {
    Install,
    Remove,
    Status,
}

pub(crate) fn app() -> io::Result<PathBuf> {
    std::env::current_exe()?
        .ancestors()
        .find(|path| path.extension().is_some_and(|extension| extension == "app"))
        .map(Path::to_path_buf)
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "MagicConverter.app 번들을 찾지 못했습니다.",
            )
        })
}

pub(crate) fn open(paths: &[PathBuf]) -> io::Result<()> {
    let result = Command::new("/usr/bin/open")
        .arg("-a")
        .arg(app()?)
        .args(paths)
        .status()?;
    if result.success() {
        Ok(())
    } else {
        Err(io::Error::other("MagicConverter 창을 열지 못했습니다."))
    }
}

pub fn run(input: &Shell) -> io::Result<Value> {
    let app = app()?;
    let tool = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister";
    if !matches!(input.operation, Operation::Status) {
        let flag = if matches!(input.operation, Operation::Install) {
            "-f"
        } else {
            "-u"
        };
        if !Command::new(tool).arg(flag).arg(&app).status()?.success() {
            return Err(io::Error::other(
                "Finder의 연결 프로그램 등록을 변경하지 못했습니다.",
            ));
        }
    }
    let dump = Command::new(tool).arg("-dump").output()?;
    if !dump.status.success() {
        return Err(io::Error::other(
            "Finder의 연결 프로그램 상태를 읽지 못했습니다.",
        ));
    }
    let registered = String::from_utf8_lossy(&dump.stdout).lines().any(|line| {
        line.trim()
            .strip_prefix("path:")
            .is_some_and(|path| path.trim() == app.to_string_lossy())
    });
    let command = if registered {
        format!("open -a '{}'", app.to_string_lossy().replace('\'', "'\\''"))
    } else {
        String::new()
    };
    Ok(json!({"ok": true, "registered": registered, "command": command, "application": app}))
}
