//! Optional installed Office renderer with the shared document-model fallback.
use crate::Edge;
use std::ffi::OsString;
use std::io;
use std::path::{Path, PathBuf};

pub fn supports(edge: &Edge) -> bool {
    edge.action_key == "office:xlsx_print_pdf"
        || (edge.action_key == "document:pdf"
            && matches!(
                edge.source.as_str(),
                "docx"
                    | "docm"
                    | "dotx"
                    | "dotm"
                    | "xlsx"
                    | "xlsm"
                    | "xltx"
                    | "xltm"
                    | "ppt"
                    | "pptx"
                    | "pptm"
                    | "potx"
                    | "potm"
                    | "ppsx"
                    | "ppsm"
            ))
}

pub fn convert(
    source: &Path,
    target: &Path,
    edge: &Edge,
    check: &dyn Fn() -> io::Result<()>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let bundled = std::env::current_exe().ok().and_then(|executable| {
        executable.ancestors().skip(1).map(|root| {
            root.join("vendor/office/LibreOffice.app/Contents/MacOS/soffice")
        }).find(|path| path.is_file())
    });
    let needs_office = edge.source == "ppt" || edge.action_key == "office:xlsx_print_pdf";
    let renderer = bundled.filter(|_| needs_office).into_iter().chain([
        PathBuf::from("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        PathBuf::from("/usr/local/bin/soffice"),
        PathBuf::from("/opt/homebrew/bin/soffice"),
    ])
    .find(|path| path.is_file())
    .ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::NotFound,
            "설치된 Office PDF 출력 프로그램을 찾지 못했습니다.",
        )
    })?;
    let stage = tempfile::tempdir()?;
    let generated = stage.path().join("output");
    std::fs::create_dir(&generated)?;
    let configuration = url::Url::from_directory_path(stage.path().join("office-profile"))
        .map_err(|()| io::Error::other("Office 임시 경로를 만들 수 없습니다."))?;
    let args: Vec<OsString> = vec![
        format!("-env:UserInstallation={configuration}").into(),
        "--headless".into(),
        "--convert-to".into(),
        "pdf".into(),
        "--outdir".into(),
        generated.as_os_str().to_owned(),
        source.as_os_str().to_owned(),
    ];
    let result = crate::engine_process::run(&renderer, &args, None, None, check, &|| Ok(()))?;
    if result.code != 0 {
        return Err(io::Error::other(result.stderr));
    }
    let stem = source
        .file_stem()
        .ok_or_else(|| io::Error::other("Office 파일 이름을 확인하세요."))?;
    let output = generated.join(format!("{}.pdf", stem.to_string_lossy()));
    crate::pdfium::page_count(&output, check)?;
    std::fs::copy(output, target)?;
    warnings.push("LibreOffice 인쇄 엔진으로 PDF를 출력했습니다.".to_owned());
    Ok(())
}
