#![cfg_attr(windows, windows_subsystem = "windows")]

fn main() -> std::process::ExitCode {
    launcher::run("--desktop", true)
}

#[path = "launcher/mod.rs"]
mod launcher;
