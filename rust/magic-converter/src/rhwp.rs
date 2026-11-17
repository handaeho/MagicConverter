use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{self, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::Edge;

const ENGINE_VERSION: &str = "0.8.7";
pub(crate) type Check<'a> = &'a dyn Fn() -> io::Result<()>;

fn executable() -> io::Result<PathBuf> {
    let current = std::env::current_exe()?;
    let name = if cfg!(windows) { "rhwp.exe" } else { "rhwp" };
    current
        .ancestors()
        .skip(1)
        .map(|root| root.join("vendor/rhwp").join(name))
        .find(|path| path.is_file())
        .or_else(|| {
            std::env::var_os("MAGIC_CONVERTER_RHWP")
                .map(PathBuf::from)
                .filter(|path| path.is_absolute() && path.is_file())
        })
        .ok_or_else(|| {
            io::Error::new(
                ErrorKind::NotFound,
                "rhwp 엔진이 없습니다. 포함된 배포본을 복구하세요.",
            )
        })
}

pub(crate) fn available() -> bool {
    cfg!(target_os = "macos") && executable().is_ok()
}

pub fn supports(edge: &Edge) -> bool {
    (cfg!(windows) || available())
        && matches!(
            (
                edge.engine_id.as_str(),
                edge.source.as_str(),
                edge.target.as_str()
            ),
            ("rhwp", "hwpx", "hwp")
                | ("hwp_documents", "hwp", "hwpx")
                | ("hwp_documents", "hwp" | "hwpx", "txt" | "pdf")
        )
}

fn sections(path: &Path) -> io::Result<u64> {
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    let mut total = 0_u64;
    let mut count = 0;
    for index in 0..archive.len() {
        let entry = archive.by_index(index)?;
        if entry
            .name()?
            .strip_prefix("Contents/section")
            .and_then(|name| name.strip_suffix(".xml"))
            .is_some_and(|number| {
                !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
            })
        {
            count += 1;
            total = total.saturating_add(entry.size());
        }
    }
    if count == 0 {
        return Err(io::Error::new(
            ErrorKind::InvalidData,
            "HWPX 본문 구역이 없습니다.",
        ));
    }
    Ok(total)
}

fn font_directories() -> Vec<PathBuf> {
    let system = std::env::var_os("WINDIR").unwrap_or_else(|| "C:/Windows".into());
    std::iter::once(PathBuf::from(system).join("Fonts"))
        .chain(
            std::env::var_os("LOCALAPPDATA")
                .map(|root| PathBuf::from(root).join("Microsoft/Windows/Fonts")),
        )
        .filter(|path| path.is_dir())
        .collect()
}

fn mono(directories: &[PathBuf], check: Check<'_>) -> io::Result<Option<&'static str>> {
    for (family, starts_with) in [
        ("D2Coding", "d2coding"),
        ("GulimChe", "gulim"),
        ("DotumChe", "batang"),
    ] {
        for directory in directories {
            for entry in fs::read_dir(directory)? {
                check()?;
                let Ok(entry) = entry else { continue };
                let name = entry.file_name().to_string_lossy().to_ascii_lowercase();
                if !name.starts_with(starts_with)
                    || ![".ttf", ".ttc", ".otf"]
                        .iter()
                        .any(|suffix| name.ends_with(suffix))
                {
                    continue;
                }
                let Ok(data) = fs::read(entry.path()) else {
                    continue;
                };
                for index in 0..ttf_parser::fonts_in_collection(&data).unwrap_or(1) {
                    let Ok(face) = ttf_parser::Face::parse(&data, index) else {
                        continue;
                    };
                    if !face.names().into_iter().any(|name| {
                        name.name_id == ttf_parser::name_id::FAMILY
                            && name
                                .to_string()
                                .is_some_and(|name| name.eq_ignore_ascii_case(family))
                    }) {
                        continue;
                    }
                    let widths = |ch| {
                        face.glyph_index(ch)
                            .and_then(|id| face.glyph_hor_advance(id))
                    };
                    if "Az09가나다".chars().all(|ch| widths(ch).is_some())
                        && widths('i') == widths('W')
                    {
                        return Ok(Some(family));
                    }
                }
            }
        }
    }
    Ok(None)
}

pub(crate) fn invoke(
    source: &Path,
    output: &Path,
    source_kind: &str,
    target: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let graphics = if source_kind == "hwpx" && matches!(target, "pdf" | "hwp") {
        crate::hwpx_graphics::prepare(source, target == "hwp", check)?
    } else {
        None
    };
    invoke_prepared(
        source,
        output,
        source_kind,
        target,
        check,
        warnings,
        graphics,
    )
}

fn invoke_prepared(
    source: &Path,
    output: &Path,
    source_kind: &str,
    target: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
    graphics: Option<crate::hwpx_graphics::Stage>,
) -> io::Result<()> {
    let executable = executable()?;
    let source = graphics
        .as_ref()
        .map_or(source, |stage| stage.source.as_path());
    let stage_output = graphics
        .as_ref()
        .map(|stage| stage.source.with_extension(target));
    let engine_output = stage_output.as_deref().unwrap_or(output);
    let bytes = if source_kind == "hwpx" {
        sections(source)?
    } else {
        fs::metadata(source)?.len().saturating_mul(3)
    };
    let seconds = (120.0 + 1800.0 * bytes as f64 / 1048576.0).min(3600.0);
    let command = match target {
        "hwp" => "convert",
        "pdf" => "export-pdf",
        _ => "export-hwpx",
    };
    let mut arguments: Vec<OsString> = vec![command.into(), source.as_os_str().to_owned()];
    if target == "pdf" {
        arguments.push("-o".into());
    }
    arguments.push(engine_output.as_os_str().to_owned());
    let mut mono_font = None;
    if target == "pdf" {
        arguments.extend(
            [
                "--fallback-sans",
                "Malgun Gothic",
                "--fallback-serif",
                "Batang",
            ]
            .into_iter()
            .map(OsString::from),
        );
        let directories = font_directories();
        for directory in &directories {
            arguments.push("--font-path".into());
            arguments.push(directory.as_os_str().to_owned());
        }
        mono_font = mono(&directories, check)?;
        if let Some(font) = mono_font {
            arguments.push("--fallback-mono".into());
            arguments.push(font.into());
        }
    }
    let result = crate::engine_process::run(
        &executable,
        &arguments,
        Some(Duration::from_secs_f64(seconds)),
        None,
        check,
        &|| Ok(()),
    )?;
    if result.code != 0 {
        let message = if result.stderr.is_empty() {
            result.stdout
        } else {
            result.stderr
        };
        let end = message
            .chars()
            .rev()
            .take(1200)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<String>();
        return Err(io::Error::other(format!(
            "rhwp 변환 실패 ({}): {end}",
            result.code
        )));
    }
    let mut diagnostics = Vec::new();
    for line in result.stderr.lines().filter(|line| {
        ["WARN:", "LAYOUT_OVERFLOW:", "LAYOUT_OVERFLOW_DRAW:"]
            .iter()
            .any(|prefix| line.starts_with(prefix))
    }) {
        let message = format!("rhwp 진단: {}", line.chars().take(500).collect::<String>());
        if diagnostics.contains(&message) {
            continue;
        }
        if diagnostics.len() == 10 {
            diagnostics.push(
                "rhwp 진단: 추가 경고는 생략했습니다. 출력의 글꼴·줄 넘침을 확인하세요.".to_owned(),
            );
            break;
        }
        diagnostics.push(message);
    }
    warnings.extend(diagnostics);
    if let Some(font) = mono_font.filter(|name| *name != "D2Coding") {
        warnings.push(format!("rhwp 고정폭 대체 글꼴: D2Coding 대신 설치된 {font}을 사용합니다. 원본 글꼴과 배치는 다를 수 있습니다."));
    }
    if let Some(graphics) = graphics {
        graphics.finish(engine_output, output, check)?;
    }
    Ok(())
}

pub(crate) fn shaping_required(body: &str) -> bool {
    body.chars()
        .any(|ch| matches!(u32::from(ch), 0x0590..=0x0dff | 0x0e00..=0x0eff | 0x1780..=0x17ff))
}

pub fn convert(
    source: &Path,
    destination: &Path,
    edge: &Edge,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    use std::io::{Read, Write};
    let directory = tempfile::Builder::new().prefix("magic-rhwp-").tempdir()?;
    let model = directory.path().join("model.hwpx");
    let mut model_source = None;
    let mut legacy_graphics = None;
    if matches!(edge.target.as_str(), "txt" | "pdf") {
        let input = if edge.source == "hwp" {
            crate::hwp_model::to_hwpx(source, &model, check, warnings)?;
            model.as_path()
        } else {
            source
        };
        model_source = Some(input);
        let body = crate::hwpx_text::extract(input, edge.target == "txt", check)?;
        if edge.target == "txt" {
            let mut output = File::create(destination)?;
            output.write_all(&[239, 187, 191])?;
            output.write_all(body.replace('\n', "\r\n").as_bytes())?;
            return Ok(());
        }
        if edge.source == "hwp" && edge.target == "pdf" {
            legacy_graphics = crate::hwpx_graphics::prepare(input, false, check)?
                .filter(crate::hwpx_graphics::Stage::contains_text);
        }
        if shaping_required(&body) && legacy_graphics.is_none() {
            return crate::hwp_model::pdf(input, destination, edge, check, warnings);
        }
    }
    let output = directory.path().join(format!("document.{}", edge.target));
    let layout = directory.path().join("layout.hwpx");
    let (tails, breaks) = if edge.source == "hwpx" && edge.target == "pdf" {
        crate::hwpx_layout::prepare(source, &layout, check)?
    } else {
        (0, 0)
    };
    let render_source = if tails + breaks > 0 {
        layout.as_path()
    } else {
        source
    };
    let result = if edge.source == "hwp" && edge.target == "hwpx" {
        crate::hwp_model::to_hwpx(render_source, &output, check, warnings)
    } else if legacy_graphics.is_some() {
        invoke_prepared(
            &model,
            &output,
            "hwpx",
            "pdf",
            check,
            warnings,
            legacy_graphics,
        )
        .map(|()| true)
    } else {
        invoke(
            render_source,
            &output,
            &edge.source,
            &edge.target,
            check,
            warnings,
        )
        .map(|()| true)
    };
    let used_rhwp = match result {
        Ok(used_rhwp) => used_rhwp,
        Err(error) => {
            if let Some(model_source) = model_source.filter(|_| edge.target == "pdf") {
                check()?;
                warnings.push(format!(
                    "rhwp PDF 처리 오류: {}",
                    error.to_string().chars().take(180).collect::<String>()
                ));
                return crate::hwp_model::pdf(model_source, destination, edge, check, warnings);
            }
            return Err(error);
        }
    };
    if tails + breaks > 0 {
        warnings.push(format!("rhwp 조판 호환: 표 끝 빈 여유 {tails}개, 저장된 쪽 경계 {breaks}개를 임시 입력에 반영했습니다. 저장 줄·원본 파일은 유지했습니다. 최종 배치는 확인하세요."));
    }
    let mut incoming = File::open(&output)?;
    if edge.target == "pdf" {
        crate::pdfium::page_count(&output, check)?;
    } else if edge.target == "hwpx" {
        sections(&output)?;
    } else {
        let mut signature = [0_u8; 8];
        incoming.read_exact(&mut signature)?;
        if signature != [208, 207, 17, 224, 161, 177, 26, 225] {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "HWP 결과의 저장 형식이 올바르지 않습니다.",
            ));
        }
        use std::io::{Seek, SeekFrom};
        incoming.seek(SeekFrom::Start(0))?;
    }
    let length = incoming.metadata()?.len();
    crate::copy_stream(
        &mut incoming,
        &mut File::create(destination)?,
        length,
        check,
        |_| Ok(()),
    )?;
    if used_rhwp {
        warnings.push(format!(
            "rhwp {ENGINE_VERSION}로 변환했습니다. 글꼴·쪽 나눔은 달라질 수 있습니다."
        ));
    }
    Ok(())
}
