use std::env;
use std::io;
use std::process::{Command, ExitCode};

fn launch(mode: &str, hidden: bool) -> io::Result<std::process::ExitStatus> {
    let executable = env::current_exe()?;
    let cli = executable.with_file_name(if cfg!(windows) {
        "magic_converter-cli.exe"
    } else {
        "magic_converter-cli"
    });
    let cli = if cli.is_file() {
        cli
    } else {
        executable.with_file_name(if cfg!(windows) {
            "magic-converter-rust.exe"
        } else {
            "magic-converter-rust"
        })
    };
    let mut command = Command::new(cli);
    command.arg(mode).args(env::args_os().skip(1));
    #[cfg(windows)]
    if hidden {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    #[cfg(not(windows))]
    let _ = hidden;
    command.status()
}

pub(super) fn run(mode: &str, hidden: bool) -> ExitCode {
    match launch(mode, hidden) {
        Ok(status) => ExitCode::from(
            status
                .code()
                .and_then(|code| u8::try_from(code).ok())
                .unwrap_or(1),
        ),
        Err(error) => {
            eprintln!("MagicConverter: {error}");
            ExitCode::FAILURE
        }
    }
}
