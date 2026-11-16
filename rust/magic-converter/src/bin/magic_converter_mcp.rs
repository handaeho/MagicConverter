fn main() -> std::process::ExitCode {
    launcher::run("mcp", false)
}

#[path = "launcher/mod.rs"]
mod launcher;
