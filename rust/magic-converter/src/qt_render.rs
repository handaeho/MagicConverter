use std::ffi::OsString;
use std::fs;
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};

type Check<'a> = &'a dyn Fn() -> io::Result<()>;

pub(crate) fn engine() -> Option<PathBuf> {
    let executable = std::env::current_exe().ok()?;
    std::env::var_os("MAGIC_CONVERTER_QT")
        .map(PathBuf::from)
        .into_iter()
        .chain(
            executable
                .ancestors()
                .skip(1)
                .map(|root| root.join("vendor/qt/magic_qt_render.exe")),
        )
        .find(|path| path.is_absolute() && path.is_file())
}

pub(crate) fn render(
    engine: &Path,
    mode: &str,
    content: &str,
    output: &Path,
    options: &[OsString],
    check: Check<'_>,
) -> io::Result<()> {
    check()?;
    let temporary = tempfile::Builder::new()
        .prefix("magic-qt-render-")
        .tempdir()?;
    let source = temporary.path().join(if mode.starts_with("svg") {
        "input.svg"
    } else {
        "input.html"
    });
    fs::write(&source, content)?;
    let mut args: Vec<OsString> = vec![
        mode.into(),
        source.into_os_string(),
        output.as_os_str().to_owned(),
    ];
    args.extend_from_slice(options);
    let result = crate::engine_process::run(engine, &args, None, None, check, &|| Ok(()))?;
    if result.code != 0 {
        return Err(io::Error::other(format!(
            "Qt 변환 실패 ({}): {}",
            result.code,
            result.stderr.trim()
        )));
    }
    if fs::metadata(output)?.len() == 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "Qt 변환 결과가 비어 있습니다.",
        ));
    }
    Ok(())
}

pub(crate) fn html_pdf(
    content: &str,
    output: &Path,
    landscape: bool,
    check: Check<'_>,
) -> io::Result<()> {
    let engine = engine().ok_or_else(|| {
        io::Error::new(
            ErrorKind::NotFound,
            "Qt PDF 렌더링 엔진을 찾을 수 없습니다.",
        )
    })?;
    render(
        &engine,
        "html-pdf",
        content,
        output,
        &[if landscape { "landscape" } else { "portrait" }.into()],
        check,
    )
}
