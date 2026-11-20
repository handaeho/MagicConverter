mod archives;
mod automation;
mod catalog;
mod data;
#[cfg(windows)]
mod desktop;
mod ebook;
mod engine_process;
mod fonts;
mod geo;
mod hwp_model;
mod hwpx_graphics;
mod hwpx_layout;
mod hwpx_text;
#[cfg(target_os = "macos")]
mod macos_shell;
mod mail;
mod mcp;
mod media;
mod mesh;
mod mupdf;
#[cfg(windows)]
mod native_office;
#[cfg(target_os = "macos")]
#[path = "native_office_macos.rs"]
mod native_office;
mod office_package;
mod output;
mod pdf;
mod pdf_office;
mod pdfium;
mod pictures;
mod pptx_image;
mod profile;
mod qt_render;
mod rhwp;
mod safe_html;
mod sheets;
mod subtitles;
mod svg;
mod text_documents;
mod web;
mod word_model;
mod workbook_pdf;

use catalog::{Action, Catalog, Edge};
use clap::{Args, CommandFactory, Parser, Subcommand};
use magic_copy_worker::copy_stream;
use output::OutputRule;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::{self, File};
use std::io::{self, BufRead, BufWriter, ErrorKind, Write};
use std::path::{self, Path, PathBuf};
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use tempfile::NamedTempFile;

static INTERRUPTED: AtomicBool = AtomicBool::new(false);

#[derive(Parser)]
#[command(name = "magic_converter-cli", version)]
struct Cli {
    #[arg(long, hide = true)]
    desktop: bool,
    #[arg(long)]
    tray: bool,
    #[arg(long, num_args = 1..)]
    open: Vec<PathBuf>,
    #[arg(long)]
    data_dir: Option<PathBuf>,
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    Formats(Common),
    Actions(Common),
    #[cfg(target_os = "macos")]
    Available(Available),
    Presets(Common),
    Convert(Convert),
    Watch(automation::watch::Watch),
    #[cfg(any(windows, target_os = "macos"))]
    Serve(automation::ipc::Serve),
    #[cfg(any(windows, target_os = "macos"))]
    Send(automation::ipc::Send),
    #[cfg(any(windows, target_os = "macos"))]
    Status(Common),
    #[cfg(any(windows, target_os = "macos"))]
    Cancel(automation::ipc::Cancel),
    #[cfg(any(windows, target_os = "macos"))]
    Stop(automation::ipc::Stop),
    #[cfg(windows)]
    Shell(desktop::shell::Shell),
    #[cfg(target_os = "macos")]
    Shell(macos_shell::Shell),
    #[command(hide = true)]
    ComparePlans,
    #[command(hide = true)]
    ArchiveEngine {
        source: PathBuf,
        target: String,
        output: PathBuf,
    },
    #[command(hide = true)]
    RarSpool {
        source: PathBuf,
        folder: PathBuf,
    },
    #[cfg(windows)]
    #[command(hide = true)]
    OfficeWorker {
        kind: native_office::Kind,
        source: PathBuf,
        output: PathBuf,
        #[arg(long)]
        print_area: bool,
    },
    #[command(hide = true)]
    WebFetch {
        request: PathBuf,
        report: PathBuf,
        folder: PathBuf,
    },
}

#[derive(Args, Clone, Default)]
struct Common {
    #[cfg(target_os = "macos")]
    #[arg(long, hide = true)]
    recipe_file: Option<PathBuf>,
    #[arg(long)]
    data_dir: Option<PathBuf>,
    #[arg(long)]
    report: Option<PathBuf>,
    #[arg(long)]
    json: bool,
}

#[cfg(target_os = "macos")]
#[derive(Args)]
struct Available {
    #[command(flatten)]
    common: Common,
    #[arg(required = true, num_args = 1..)]
    paths: Vec<PathBuf>,
}

#[derive(Args, Clone)]
struct Convert {
    #[command(flatten)]
    common: Common,
    #[arg(long, group = "selection", required_unless_present_any = ["to", "preset"])]
    action: Option<String>,
    #[arg(long, group = "selection")]
    to: Option<String>,
    #[arg(long, group = "selection")]
    preset: Option<String>,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long, default_value = "3600", value_parser = timeout)]
    timeout: Duration,
    #[arg(required = true, num_args = 1..)]
    paths: Vec<PathBuf>,
    #[arg(skip)]
    recipe: Option<Value>,
}

fn timeout(value: &str) -> Result<Duration, String> {
    value
        .parse::<f64>()
        .ok()
        .and_then(|number| Duration::try_from_secs_f64(number).ok())
        .filter(|duration| !duration.is_zero())
        .ok_or_else(|| "시간 제한은 유한한 양수로 지정하세요.".to_owned())
}

#[derive(Deserialize)]
struct Query {
    path: PathBuf,
    action: String,
    disabled: Vec<String>,
}

#[derive(Serialize)]
struct Outcome {
    source: PathBuf,
    state: &'static str,
    outputs: Vec<PathBuf>,
    errors: Vec<String>,
    warnings: Vec<String>,
    cancel_requested: bool,
}

fn compare_plans(catalog: &Catalog) -> io::Result<()> {
    let mut stdout = BufWriter::new(io::stdout().lock());
    for line in io::stdin().lock().lines() {
        let query: Query = serde_json::from_str(&line?)?;
        let result = catalog.assess(&query.path, &query.action, &query.disabled);
        let native = result
            .route
            .as_ref()
            .is_some_and(|route| supports_steps(&route.steps, None));
        serde_json::to_writer(
            &mut stdout,
            &json!({
                "route": result.route, "reason": result.reason, "native": native,
            }),
        )?;
        stdout.write_all(b"\n")?;
    }
    stdout.flush()
}

fn resolved(path: &Path) -> io::Result<PathBuf> {
    let absolute = path::absolute(path)?;
    let Ok(actual) = absolute.canonicalize() else {
        return Ok(absolute);
    };
    if cfg!(windows)
        && let Some(name) = actual.to_str()
    {
        if let Some(unc) = name.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{unc}")));
        }
        if let Some(local) = name.strip_prefix(r"\\?\") {
            return Ok(PathBuf::from(local));
        }
    }
    Ok(actual)
}

fn preferences(catalog: &Catalog, common: &Common) -> io::Result<Value> {
    let directory = profile::directory(common)?;
    Ok(["settings.json", "settings.json.bak"]
        .iter()
        .find_map(|name| {
            let values = automation::read_json(&directory.join(name), 2 * 1024 * 1024).ok()?;
            matches!(values.get("version")?.as_u64(), Some(1..=3)).then_some(values)
        })
        .unwrap_or_else(|| catalog.default_preferences.clone()))
}

#[derive(Clone)]
struct Selection<'a> {
    action: &'a Action,
    name: String,
    output: OutputRule,
    subtitle_offset_ms: i64,
    encoding: media::EncodeOptions,
    media_tools: media::ToolOptions,
    image_options: pictures::Options,
    image_tools: pictures::ToolOptions,
    pdf_pages: String,
    pdf_rotation: i64,
}

fn selected_recipe(
    catalog: &Catalog,
    input: &Convert,
    paths: &[PathBuf],
    profile: &Value,
    disabled: &[String],
) -> Result<Value, String> {
    let mut recipe = if let Some(recipe) = &input.recipe {
        recipe.clone()
    } else if let Some(recipe) = gui_recipe(&input.common)? {
        recipe
    } else if let Some(selector) = &input.preset {
        let selected: Vec<_> = profile["presets"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|preset| {
                preset["id"].as_str() == Some(selector) || preset["name"].as_str() == Some(selector)
            })
            .collect();
        let [preset] = selected.as_slice() else {
            return Err("이름 또는 ID가 정확히 일치하는 프리셋 하나를 지정하세요.".to_owned());
        };
        (*preset).clone()
    } else {
        let action = if let Some(target) = &input.to {
            catalog.target_action(target, paths, disabled)
        } else {
            catalog
                .actions
                .iter()
                .find(|action| input.action.as_deref() == Some(&action.key))
        };
        let action = action.ok_or("지원하는 변환 작업/출력 형식/프리셋을 지정하세요. actions 명령으로 확인할 수 있습니다.")?;
        json!({"id": "cli", "name": action.label, "action_key": action.key})
    };
    if recipe["output"].is_null() {
        recipe["output"] = profile
            .get("output")
            .unwrap_or(&catalog.default_preferences["output"])
            .clone();
    }
    if let Some(folder) = &input.output {
        let folder = resolved(folder).map_err(|error| error.to_string())?;
        recipe["output"]["location"] = json!("folder");
        recipe["output"]["directory"] = json!(folder);
    }
    automation::normalize_recipe(catalog, recipe)
}

fn gui_recipe(common: &Common) -> Result<Option<Value>, String> {
    #[cfg(target_os = "macos")]
    if let Some(path) = &common.recipe_file {
        return automation::read_json(path, 2 * 1024 * 1024)
            .map(Some)
            .map_err(|error| error.to_string());
    }
    let _ = common;
    Ok(None)
}

fn selection_for<'a>(catalog: &'a Catalog, recipe: &Value) -> Result<Selection<'a>, String> {
    let action = catalog.actions.iter()
        .find(|action| recipe["action_key"].as_str() == Some(&action.key))
        .filter(|action| !matches!(action.key.as_str(), "copy_path" | "terminal"))
        .ok_or("지원하는 변환 작업/출력 형식/프리셋을 지정하세요. actions 명령으로 확인할 수 있습니다.")?;
    Ok(Selection {
        action,
        name: recipe["name"].as_str().unwrap_or(&action.label).to_owned(),
        output: serde_json::from_value(recipe["output"].clone()).unwrap_or_default(),
        subtitle_offset_ms: recipe["tools"]["subtitle_offset_ms"]
            .as_i64()
            .unwrap_or(1_000),
        encoding: serde_json::from_value(recipe["options"].clone()).unwrap_or_default(),
        media_tools: serde_json::from_value(recipe["tools"].clone()).unwrap_or_default(),
        image_options: serde_json::from_value(recipe["options"].clone()).unwrap_or_default(),
        image_tools: serde_json::from_value(recipe["tools"].clone()).unwrap_or_default(),
        pdf_pages: recipe["tools"]["pdf_pages"]
            .as_str()
            .unwrap_or_default()
            .to_owned(),
        pdf_rotation: recipe["tools"]["rotation"].as_i64().unwrap_or(90),
    })
}

fn copy_original(
    catalog: &Catalog,
    path: &Path,
    preset: &str,
    rule: &OutputRule,
    action_key: &str,
    check: impl Fn() -> io::Result<()>,
) -> io::Result<PathBuf> {
    if !path.is_file() {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            format!(
                "일반 파일이 아니거나 접근할 수 없습니다: {}",
                path.display()
            ),
        ));
    }
    let (_, extension) = catalog.detect(path).expect("planned copy source");
    let source_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .expect("detected file name");
    let (suffix, tag) = if action_key == "tool:copy_original" {
        (
            &source_name[source_name.len() - extension.len() - 1..],
            "copy",
        )
    } else {
        (".pdf", "converted")
    };
    rule.publish(path, suffix, tag, preset, &check, |target| {
        let incoming = File::open(path)?;
        let length = incoming.metadata()?.len();
        if length == 0 && tag != "copy" {
            return Err(io::Error::new(
                ErrorKind::InvalidData,
                "변환 결과가 비어 있습니다.",
            ));
        }
        copy_stream(incoming, target, length, &check, |_| Ok(()))?;
        Ok(())
    })
}

fn supported(edge: &Edge) -> bool {
    matches!(
        edge.engine_id.as_str(),
        "copy" | "subtitles" | "subtitle_tools" | "ffmpeg" | "mesh" | "geo" | "font" | "email"
    ) || (matches!(edge.engine_id.as_str(), "archives" | "archive_tools")
        && archives::supports(&edge.source))
        || (edge.engine_id == "data" && data::supports(&edge.source, &edge.target))
        || (matches!(edge.engine_id.as_str(), "images" | "image_tools") && pictures::supports(edge))
        || (edge.engine_id == "pdf_tools"
            && (edge.source == "pdf"
                || (edge.action_key == "tool:pdf_merge" && pictures::readable(&edge.source))))
        || (edge.engine_id == "pdf"
            && matches!(
                edge.action_key.as_str(),
                "strip"
                    | "document:txt"
                    | "document:md"
                    | "document:html"
                    | "document:epub"
                    | "document:png"
                    | "document:jpg"
            ))
        || ((cfg!(windows) || cfg!(target_os = "macos"))
            && ((edge.engine_id == "pdf" && edge.action_key == "pptx_image")
                || (edge.engine_id == "ebook" && edge.action_key == "ebook:cbz")))
        || (edge.engine_id == "metadata" && edge.action_key == "strip")
        || (edge.engine_id == "documents"
            && (edge.action_key == "office:pptx"
                || text_documents::supports(&edge.source, &edge.target)))
        || (edge.engine_id == "spreadsheets" && sheets::supports(&edge.source, &edge.target))
        || native_office_edge(edge)
        || pdf_office::supports(edge)
        || rhwp::supports(edge)
        || hwp_model::supports(edge)
        || word_model::supports(edge)
        || ebook::supports(edge)
}

fn native_office_edge(edge: &Edge) -> bool {
    #[cfg(any(windows, target_os = "macos"))]
    {
        native_office::supports(edge)
    }
    #[cfg(not(any(windows, target_os = "macos")))]
    {
        let _ = edge;
        false
    }
}

fn workbook_pdf_edge(edge: &Edge, path: &Path) -> bool {
    edge.engine_id == "spreadsheets"
        && edge.source == "xlsx"
        && edge.action_key == "document:pdf"
        && workbook_pdf::large(path, &|| Ok(())).unwrap_or(false)
}

fn archive_action(
    path: &Path,
    preset: &str,
    rule: &OutputRule,
    edge: &Edge,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut archives::Notices,
) -> io::Result<PathBuf> {
    if edge.engine_id == "archive_tools" {
        return rule.publish_folder(path, preset, check, |target| {
            archives::extract(path, &edge.source, target, check, notices)
        });
    }
    let suffix = format!(".{}", edge.target);
    rule.publish(path, &suffix, "converted", preset, check, |target| {
        archives::convert(path, &edge.source, &edge.target, target, check, notices)
    })
}

fn media_action(
    catalog: &Catalog,
    path: &Path,
    selection: &Selection<'_>,
    edge: &Edge,
    engine: &media::Tools,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<PathBuf> {
    if edge.action_key == "tool:media_frames" {
        return selection
            .output
            .publish_folder(path, &selection.name, check, |target| {
                engine.frames(path, target, &selection.media_tools, check)
            });
    }
    let (format, _) = catalog.detect(path).expect("planned media source");
    selection.output.publish_external(
        path,
        &format!(".{}", edge.target),
        &selection.name,
        check,
        |target| {
            engine.convert(
                media::MediaFile {
                    path,
                    key: &edge.action_key,
                    input_format: &edge.source,
                    input_is_audio: format.category == "audio",
                    encoding: &selection.encoding,
                    tools: &selection.media_tools,
                },
                target,
                check,
                notices,
            )
        },
    )
}

fn image_action(
    path: &Path,
    selection: &Selection<'_>,
    edge: &Edge,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<PathBuf> {
    if edge.action_key == "tool:image_frames" {
        return selection
            .output
            .publish_folder(path, &selection.name, check, |target| {
                pictures::extract(path, target, &edge.source, &selection.image_tools, check)
            });
    }
    selection.output.publish_external(
        path,
        &format!(".{}", edge.target),
        &selection.name,
        check,
        |target| {
            if edge.engine_id == "image_tools" {
                pictures::transform(path, target, edge, &selection.image_tools, check)
            } else {
                pictures::convert(path, target, edge, &selection.image_options, check, notices)
            }
        },
    )
}

struct Planned<'a> {
    paths: Vec<PathBuf>,
    engines: Vec<Vec<&'a Edge>>,
    selection: Selection<'a>,
    recipe: Value,
    ffmpeg: Option<media::Tools>,
    timeout: Duration,
    web: HashMap<PathBuf, web::Input>,
}

#[derive(Debug)]
enum PlanError {
    Request(String),
    Selection(String),
}

impl std::fmt::Display for PlanError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Request(message) | Self::Selection(message) => f.write_str(message),
        }
    }
}

impl std::error::Error for PlanError {}

fn plan<'a>(catalog: &'a Catalog, input: &Convert) -> Result<Planned<'a>, PlanError> {
    plan_inputs(catalog, input, Vec::new())
}

fn plan_inputs<'a>(
    catalog: &'a Catalog,
    input: &Convert,
    inline: Vec<web::Input>,
) -> Result<Planned<'a>, PlanError> {
    let mut seen = HashSet::new();
    let mut web: HashMap<_, _> = inline
        .into_iter()
        .map(|item| (item.placeholder(), item))
        .collect();
    let paths: Vec<_> = input
        .paths
        .iter()
        .map(|path| {
            if web.contains_key(path) {
                Ok(path.clone())
            } else if let Some(url) = path.to_str().filter(|value| web::Input::is_argument(value)) {
                let item = web::Input::url(url, None, false)?;
                let placeholder = item.placeholder();
                web.insert(placeholder.clone(), item);
                Ok(placeholder)
            } else {
                resolved(path)
            }
        })
        .collect::<io::Result<Vec<_>>>()
        .map_err(|error| PlanError::Request(error.to_string()))?
        .into_iter()
        .filter(|path| seen.insert(path.to_string_lossy().to_lowercase()))
        .collect();
    if !web.is_empty()
        && (paths.len() != web.len() || paths.iter().any(|path| !web.contains_key(path)))
    {
        return Err(PlanError::Request(
            "웹 주소와 로컬 파일은 별도 요청으로 변환하세요.".to_owned(),
        ));
    }
    let profile = preferences(catalog, &input.common)
        .map_err(|error| PlanError::Request(error.to_string()))?;
    let ffmpeg = media::Tools::discover(profile["ffmpeg_path"].as_str().unwrap_or_default());
    let disabled = if ffmpeg.is_none() {
        vec!["ffmpeg".to_owned()]
    } else {
        Vec::new()
    };
    let mut recipe =
        selected_recipe(catalog, input, &paths, &profile, &disabled).map_err(PlanError::Request)?;
    if !web.is_empty() && recipe["output"]["location"] == "source" {
        let root = env::var_os("USERPROFILE")
            .or_else(|| env::var_os("HOME"))
            .map(PathBuf::from)
            .ok_or_else(|| PlanError::Request("다운로드 폴더를 찾지 못했습니다.".to_owned()))?;
        recipe["output"]["location"] = json!("folder");
        recipe["output"]["directory"] = json!(root.join("Downloads").join("MagicConverter"));
    }
    let selection = selection_for(catalog, &recipe).map_err(PlanError::Request)?;
    let action = selection.action;
    if !web.is_empty() && action.key == "tool:pdf_merge" {
        return Err(PlanError::Selection(
            "웹 입력은 파일별 변환 작업을 선택하세요.".to_owned(),
        ));
    }
    let mut engines = Vec::new();
    for path in &paths {
        let assessment = catalog.assess(path, &action.key, &disabled);
        let name = path
            .file_name()
            .unwrap_or(path.as_os_str())
            .to_string_lossy();
        let route = assessment
            .route
            .ok_or_else(|| PlanError::Selection(format!("{name}: {}", assessment.reason)))?;
        if supports_steps(&route.steps, Some(path)) {
            engines.push(route.steps);
        } else {
            return Err(PlanError::Selection(format!(
                "{name}: 이 변환의 Rust 실행 경로는 아직 구현되지 않았습니다."
            )));
        }
    }
    Ok(Planned {
        paths,
        engines,
        selection,
        recipe,
        ffmpeg,
        timeout: input.timeout,
        web,
    })
}

fn supports_steps(steps: &[&Edge], path: Option<&Path>) -> bool {
    !steps.is_empty()
        && steps.iter().enumerate().all(|(index, edge)| {
            supported(edge)
                || (index == 0 && path.is_some_and(|path| workbook_pdf_edge(edge, path)))
        })
}

struct FileConversion<'a> {
    catalog: &'a Catalog,
    ffmpeg: Option<&'a media::Tools>,
    check: &'a dyn Fn() -> io::Result<()>,
    file_warnings: &'a mut Vec<String>,
    notices: &'a mut archives::Notices,
}

impl FileConversion<'_> {
    fn run(
        &mut self,
        path: &Path,
        selection: &Selection<'_>,
        steps: &[&Edge],
    ) -> io::Result<PathBuf> {
        if let [step] = steps {
            return self.step(path, selection, step);
        }
        let stage = tempfile::Builder::new().prefix("magic-route-").tempdir()?;
        let mut source = path.to_owned();
        let mut staged_selection = selection.clone();
        for (index, edge) in steps.iter().enumerate() {
            (self.check)()?;
            let directory = stage.path().join(index.to_string());
            staged_selection.output = OutputRule::staging(directory, path);
            source = self.step(&source, &staged_selection, edge)?;
            if index + 1 < steps.len() && !source.is_file() {
                return Err(io::Error::other(
                    "여러 파일로 나뉜 중간 결과는 다음 변환에 사용할 수 없습니다.",
                ));
            }
        }
        let check = self.check;
        if source.is_dir() {
            selection
                .output
                .publish_folder(path, &selection.name, check, |target| {
                    for entry in fs::read_dir(source)? {
                        let entry = entry?;
                        if !entry.file_type()?.is_file() {
                            return Err(io::Error::other(
                                "여러 단계 변환의 출력 폴더에 일반 파일이 아닌 항목이 있습니다.",
                            ));
                        }
                        publish_stage_file(
                            &entry.path(),
                            &mut File::create(target.join(entry.file_name()))?,
                            check,
                        )?;
                    }
                    Ok(())
                })
        } else {
            let suffix = self.catalog.detect(&source).map_or_else(
                || {
                    source
                        .extension()
                        .and_then(|extension| extension.to_str())
                        .unwrap_or("")
                },
                |(_, extension)| extension,
            );
            let tag = match selection.action.key.as_str() {
                "pptx_image" => "slide",
                "pptx_editable" => "editable",
                _ => "converted",
            };
            selection.output.publish(
                path,
                &format!(".{suffix}"),
                tag,
                &selection.name,
                check,
                |target| publish_stage_file(&source, target, check),
            )
        }
    }

    fn step(&mut self, path: &Path, selection: &Selection<'_>, edge: &Edge) -> io::Result<PathBuf> {
        let Self {
            catalog,
            ffmpeg,
            check,
            file_warnings,
            notices,
        } = self;
        let check = *check;
        match edge.engine_id.as_str() {
            "copy" => copy_original(
                catalog,
                path,
                &selection.name,
                &selection.output,
                &edge.action_key,
                check,
            ),
            "subtitles" | "subtitle_tools" => {
                let suffix = format!(".{}", edge.target);
                let offset = if edge.engine_id == "subtitle_tools" {
                    selection.subtitle_offset_ms
                } else {
                    0
                };
                selection.output.publish(
                    path,
                    &suffix,
                    "converted",
                    &selection.name,
                    check,
                    |target| {
                        subtitles::convert(path, target, &edge.target, offset, check, file_warnings)
                    },
                )
            }
            "ffmpeg" => media_action(
                catalog,
                path,
                selection,
                edge,
                ffmpeg.as_ref().expect("planned available media engine"),
                check,
                file_warnings,
            ),
            "images" | "image_tools" => image_action(path, selection, edge, check, file_warnings),
            "spreadsheets" if workbook_pdf_edge(edge, path) => {
                selection
                    .output
                    .publish_external(path, ".pdf", &selection.name, check, |target| {
                        workbook_pdf::convert(path, target, check, file_warnings)
                    })
            }
            #[cfg(any(windows, target_os = "macos"))]
            _ if native_office_edge(edge) => {
                selection
                    .output
                    .publish_external(path, ".pdf", &selection.name, check, |target| {
                        hwp_model::office_pdf(path, target, edge, check, file_warnings)
                    })
            }
            "spreadsheets" => sheets::convert(path, edge, selection, check, file_warnings),
            _ if hwp_model::supports(edge) => selection.output.publish_external(
                path,
                &format!(".{}", edge.target),
                &selection.name,
                check,
                |target| hwp_model::convert(path, target, edge, check, file_warnings),
            ),
            "rhwp" | "hwp_documents" => selection.output.publish_external(
                path,
                &format!(".{}", edge.target),
                &selection.name,
                check,
                |target| rhwp::convert(path, target, edge, check, file_warnings),
            ),
            "font" => selection.output.publish_external(
                path,
                &format!(".{}", edge.target),
                &selection.name,
                check,
                |target| fonts::convert(path, target, &edge.target, check, file_warnings),
            ),
            "documents" if text_documents::supports(&edge.source, &edge.target) => {
                selection.output.publish_path(
                    path,
                    &format!(".{}", edge.target),
                    &selection.name,
                    check,
                    |target| {
                        text_documents::convert(path, target, &edge.source, &edge.target, check)
                    },
                )
            }
            "documents" if word_model::supports(edge) => selection.output.publish_external(
                path, &format!(".{}", edge.target), &selection.name, check,
                |target| word_model::convert(path, target, edge, check, file_warnings),
            ),
            "metadata" | "documents" => {
                let normalize = edge.action_key == "office:pptx";
                selection.output.publish_external(
            path,
            &format!(".{}", edge.target),
            &selection.name,
            check,
            |target| {
                let operation = if normalize {
                    office_package::Action::Standardize
                } else {
                    office_package::Action::StripMetadata
                };
                office_package::convert(path, target, operation, check)?;
                if normalize && edge.source != "pptx" {
                    file_warnings.push("Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.".to_owned());
                }
                Ok(())
            },
        )
            }
            "mesh" | "geo" | "email" => selection.output.publish_external(
                path,
                &format!(".{}", edge.target),
                &selection.name,
                check,
                |target| {
                    if edge.engine_id == "email" {
                        return mail::convert(
                            path,
                            target,
                            &edge.source,
                            &edge.target,
                            check,
                            file_warnings,
                        );
                    }
                    let convert = if edge.engine_id == "mesh" {
                        mesh::convert
                    } else {
                        geo::convert
                    };
                    convert(
                        path,
                        target,
                        &edge.source,
                        &edge.target,
                        check,
                        file_warnings,
                    )
                },
            ),
            "pdf_tools" | "pdf" if edge.action_key == "tool:pdf_split" => selection
                .output
                .publish_folder(path, &selection.name, check, |folder| {
                    pdf::split(path, folder, &selection.pdf_pages, check)
                }),
            "pdf" if matches!(edge.action_key.as_str(), "document:png" | "document:jpg") => {
                selection
                    .output
                    .publish_folder(path, &selection.name, check, |folder| {
                        pdfium::render(path, folder, &edge.target, check)
                    })
            }
            "pdf" if edge.action_key == "pptx_image" => selection.output.publish_path_with_tag(
                path, ".pptx", "slide", &selection.name, check,
                |target| pptx_image::convert(path, target, check),
            ),
            "pdf" if pdf_office::supports(edge) => selection.output.publish_external(
                path, &format!(".{}", edge.target), &selection.name, check,
                |target| pdf_office::convert(path, target, edge, check, file_warnings),
            ),
            "ebook" if edge.action_key == "ebook:cbz" => selection.output.publish_external(
                path, ".cbz", &selection.name, check, |target| {
                    mupdf::cbz(path, target, check)?;
                    file_warnings.push("PDF를 144 DPI 페이지 이미지로 래스터화했습니다. 검색 텍스트·링크·양식·주석 편집 기능은 포함되지 않습니다.".to_owned());
                    Ok(())
                },
            ),
            "ebook" => selection.output.publish_external(
                path, &format!(".{}", edge.target), &selection.name, check,
                |target| ebook::convert(path, target, edge, check, file_warnings),
            ),
            "pdf"
                if matches!(
                    edge.action_key.as_str(),
                    "document:txt" | "document:md" | "document:html" | "document:epub"
                ) =>
            {
                selection.output.publish_external(
                    path,
                    &format!(".{}", edge.target),
                    &selection.name,
                    check,
                    |target| {
                        let text = pdfium::text(path, check)?;
                        text_documents::write_pdf_text(path, target, &text, &edge.target)
                    },
                )
            }
            "pdf_tools" | "pdf" => {
                selection
                    .output
                    .publish_external(path, ".pdf", &selection.name, check, |target| {
                        pdf::transform(
                            path,
                            target,
                            &edge.action_key,
                            &selection.pdf_pages,
                            selection.pdf_rotation,
                            check,
                            file_warnings,
                        )
                    })
            }
            "data" => selection.output.publish(
                path,
                &format!(".{}", edge.target),
                "converted",
                &selection.name,
                check,
                |target| {
                    data::convert(
                        path,
                        &edge.source,
                        &edge.target,
                        target,
                        check,
                        file_warnings,
                    )
                },
            ),
            _ => archive_action(
                path,
                &selection.name,
                &selection.output,
                edge,
                check,
                notices,
            ),
        }
    }
}

fn publish_stage_file(
    source: &Path,
    output: &mut File,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    let mut file = File::open(source)?;
    let length = file.metadata()?.len();
    copy_stream(&mut file, output, length, check, |_| Ok(())).map(|_| ())
}

fn execute(
    catalog: &Catalog,
    planned: Planned<'_>,
    cancelled: &AtomicBool,
    job_id: u64,
    mut update: impl FnMut(usize, &'static str),
) -> (Value, u8) {
    let Planned {
        paths,
        engines,
        selection,
        ffmpeg,
        timeout,
        web,
        ..
    } = planned;
    let action = selection.action;
    let mut files = Vec::new();
    let mut outputs = Vec::new();
    let mut errors = Vec::new();
    let mut warnings = Vec::new();
    let mut timed_out = false;
    let merged = if action.key == "tool:pdf_merge" {
        for index in 0..paths.len() {
            update(index, "running");
        }
        let started = Instant::now();
        let check = || {
            if cancelled.load(Ordering::Relaxed) {
                Err(io::Error::new(ErrorKind::Interrupted, "취소되었습니다."))
            } else if started.elapsed() >= timeout {
                Err(io::Error::new(
                    ErrorKind::TimedOut,
                    "파일 처리 제한 시간이 초과되었습니다.",
                ))
            } else {
                Ok(())
            }
        };
        Some(
            selection
                .output
                .publish_external(&paths[0], ".pdf", &selection.name, check, |target| {
                    pdf::merge(catalog, &paths, target, &check)
                })
                .map_err(|error| (error.kind(), error.to_string())),
        )
    } else {
        None
    };
    for (index, (path, steps)) in paths.into_iter().zip(engines).enumerate() {
        if merged.is_none() {
            update(index, "running");
        }
        let start = Instant::now();
        let check = || {
            if cancelled.load(Ordering::Relaxed) {
                Err(io::Error::new(ErrorKind::Interrupted, "취소되었습니다."))
            } else if start.elapsed() >= timeout {
                Err(io::Error::new(
                    ErrorKind::TimedOut,
                    "파일 처리 제한 시간이 초과되었습니다.",
                ))
            } else {
                Ok(())
            }
        };
        let mut notices = archives::Notices::new();
        let mut file_warnings = Vec::new();
        let source = path.clone();
        let result = (|| {
            if let Some(result) = &merged {
                return result
                    .clone()
                    .map_err(|(kind, error)| io::Error::new(kind, error));
            }
            let stage;
            let (path, steps) = if let Some(item) = web.get(&source) {
                stage = tempfile::Builder::new()
                    .prefix("magic-web-job-")
                    .tempdir()?;
                let actual = item.materialize(catalog, stage.path(), &check)?;
                let disabled = if ffmpeg.is_some() {
                    vec![]
                } else {
                    vec!["ffmpeg".to_owned()]
                };
                let assessment = catalog.assess(&actual, &action.key, &disabled);
                let route = assessment
                    .route
                    .ok_or_else(|| io::Error::other(assessment.reason))?;
                if !supports_steps(&route.steps, Some(&actual))
                    || route.steps.iter().any(|edge| {
                        !steps
                            .iter()
                            .any(|expected| edge.engine_id == expected.engine_id)
                    })
                {
                    return Err(io::Error::other(
                        "웹 파일의 실제 형식이 예상과 달라 필요한 엔진이 바뀌었습니다. 파일을 저장한 뒤 다시 선택하세요.",
                    ));
                }
                (actual, route.steps)
            } else {
                (path, steps)
            };
            FileConversion {
                catalog,
                ffmpeg: ffmpeg.as_ref(),
                check: &check,
                file_warnings: &mut file_warnings,
                notices: &mut notices,
            }
            .run(&path, &selection, &steps)
        })();
        let mut file = Outcome {
            source,
            state: "done",
            outputs: Vec::new(),
            errors: Vec::new(),
            warnings: Vec::new(),
            cancel_requested: false,
        };
        match result {
            Ok(path) => {
                outputs.push(path.clone());
                file.outputs.push(path);
            }
            Err(_) if cancelled.load(Ordering::Relaxed) => {
                file.state = "cancelled";
                file.cancel_requested = true;
            }
            Err(error) => {
                let name = file
                    .source
                    .file_name()
                    .unwrap_or(file.source.as_os_str())
                    .to_string_lossy();
                let message = format!("{name}: {error}");
                errors.push(message.clone());
                file.errors.push(message);
                file.state = if error.kind() == ErrorKind::TimedOut {
                    timed_out = true;
                    "timed_out"
                } else {
                    "failed"
                };
            }
        }
        file_warnings.extend(notices.into_iter().map(str::to_owned));
        file.warnings = file_warnings;
        warnings.extend(file.warnings.iter().cloned());
        update(index, file.state);
        files.push(file);
    }
    let mut unique_warnings = HashSet::new();
    warnings.retain(|warning| unique_warnings.insert(warning.clone()));
    let mut unique_errors = HashSet::new();
    errors.retain(|error| unique_errors.insert(error.clone()));
    let completed = files.iter().filter(|file| file.state == "done").count();
    let mut unique_outputs = HashSet::new();
    outputs.retain(|output| unique_outputs.insert(output.clone()));
    let interrupted = cancelled.load(Ordering::Relaxed);
    let ok = !interrupted && errors.is_empty();
    let report = json!({"ok": ok, "interrupted": interrupted, "errors": [], "jobs": [{
        "job_id": job_id, "action": action.key, "total": files.len(), "completed": completed,
        "cancelled": interrupted, "timed_out": timed_out, "outputs": outputs,
        "errors": errors, "warnings": warnings, "files": files,
    }]});
    (report, if interrupted { 130 } else { u8::from(!ok) })
}

fn write_report(path: &Path, report: &Value) -> io::Result<()> {
    let path = path::absolute(path)?;
    let parent = path.parent().ok_or(ErrorKind::InvalidInput)?;
    fs::create_dir_all(parent)?;
    let mut staged = NamedTempFile::new_in(parent)?;
    let mut output = BufWriter::new(staged.as_file_mut());
    serde_json::to_writer_pretty(&mut output, report)?;
    output.write_all(b"\n")?;
    output.flush()?;
    drop(output);
    staged.into_temp_path().persist(path)?;
    Ok(())
}

fn measure_archive(
    catalog: &Catalog,
    source: &Path,
    target: &str,
    output: &Path,
) -> io::Result<Value> {
    let input = catalog
        .detect(source)
        .map(|(format, _)| format.id.as_str())
        .filter(|format| archives::supports(format))
        .ok_or(ErrorKind::InvalidInput)?;
    if !matches!(target, "zip" | "tar" | "tar.gz") {
        return Err(ErrorKind::InvalidInput.into());
    }
    let output = path::absolute(output)?;
    let mut staged = NamedTempFile::new_in(output.parent().ok_or(ErrorKind::InvalidInput)?)?;
    let start = Instant::now();
    archives::convert(
        source,
        input,
        target,
        staged.as_file_mut(),
        &|| Ok(()),
        &mut archives::Notices::new(),
    )?;
    let elapsed = start.elapsed().as_secs_f64();
    staged.into_temp_path().persist_noclobber(&output)?;
    Ok(json!({"ok": true, "jobs": [{"outputs": [output], "engine_s": elapsed}]}))
}

#[cfg(any(windows, target_os = "macos"))]
fn ipc_result(result: automation::Result<Value>, common: Common) -> (Value, u8, Common) {
    let report = result.unwrap_or_else(|error| json!({"ok": false, "error": error.to_string()}));
    let code = u8::from(report["ok"] != true);
    (report, code, common)
}

fn run(command: Command, catalog: &Catalog) -> Result<u8, Box<dyn std::error::Error>> {
    let (mut report, mut code, common) = match command {
        Command::ComparePlans => {
            compare_plans(catalog)?;
            return Ok(0);
        }
        Command::WebFetch {
            request,
            report,
            folder,
        } => {
            return Ok(web::worker(catalog, &request, &report, &folder)?);
        }
        Command::RarSpool { source, folder } => {
            return Ok(archives::rar::worker(&source, &folder)?);
        }
        #[cfg(windows)]
        Command::OfficeWorker {
            kind,
            source,
            output,
            print_area,
        } => {
            native_office::worker(kind, &source, &output, print_area)?;
            return Ok(0);
        }
        Command::ArchiveEngine {
            source,
            target,
            output,
        } => {
            let report = measure_archive(catalog, &source, &target, &output)?;
            serde_json::to_writer(io::stdout().lock(), &report)?;
            return Ok(0);
        }
        Command::Formats(common) => (json!({"ok": true, "formats": catalog.formats}), 0, common),
        Command::Actions(common) => (json!({"ok": true, "actions": catalog.actions}), 0, common),
        #[cfg(target_os = "macos")]
        Command::Available(input) => {
            let settings = preferences(catalog, &input.common)?;
            let ffmpeg =
                media::Tools::discover(settings["ffmpeg_path"].as_str().unwrap_or_default());
            let disabled = ffmpeg
                .is_none()
                .then(|| "ffmpeg".to_owned())
                .into_iter()
                .collect::<Vec<_>>();
            let qt = qt_render::engine().is_some();
            let paths = input
                .paths
                .iter()
                .map(|path| {
                    if let Some(url) = path.to_str().filter(|value| web::Input::is_argument(value))
                    {
                        web::Input::url(url, None, false).map(|input| input.placeholder())
                    } else {
                        Ok(path.clone())
                    }
                })
                .collect::<io::Result<Vec<_>>>()?;
            let actions: Vec<_> = catalog
                .actions
                .iter()
                .filter(|action| {
                    paths.iter().all(|path| {
                        catalog
                            .assess(path, &action.key, &disabled)
                            .route
                            .is_some_and(|route| {
                                supports_steps(&route.steps, Some(path))
                                    && route.steps.iter().all(|edge| {
                                        qt || !(edge.target == "pdf"
                                            && matches!(
                                                edge.engine_id.as_str(),
                                                "documents" | "spreadsheets"
                                            ))
                                    })
                            })
                    })
                })
                .collect();
            (
                json!({"ok": true, "actions": actions, "snapshot_recipes": true}),
                0,
                input.common,
            )
        }
        Command::Presets(common) => {
            let profile = preferences(catalog, &common)?;
            let presets = profile
                .get("presets")
                .unwrap_or(&catalog.default_preferences["presets"]);
            (json!({"ok": true, "presets": presets}), 0, common)
        }
        Command::Convert(input) => {
            let (report, code) = automation::convert(catalog, &input)
                .unwrap_or_else(|error| (json!({"ok": false, "error": error.to_string()}), 1));
            (report, code, input.common)
        }
        Command::Watch(input) => {
            let (report, code) = automation::ctrlc()
                .map_err(|error| error.to_string())
                .and_then(|()| {
                    automation::watch::run(catalog, &input).map_err(|error| error.to_string())
                })
                .unwrap_or_else(|error| (json!({"ok": false, "error": error}), 1));
            (report, code, automation::watch::common(&input).clone())
        }
        #[cfg(any(windows, target_os = "macos"))]
        Command::Serve(input) => {
            let (report, code) = automation::ipc::serve(catalog, &input)
                .unwrap_or_else(|error| (json!({"ok": false, "error": error.to_string()}), 1));
            (report, code, input.common)
        }
        #[cfg(any(windows, target_os = "macos"))]
        Command::Send(input) => ipc_result(automation::ipc::send(catalog, &input), input.common),
        #[cfg(any(windows, target_os = "macos"))]
        Command::Status(common) => ipc_result(
            automation::ipc::call(&common, json!({"command": "status"})),
            common,
        ),
        #[cfg(any(windows, target_os = "macos"))]
        Command::Cancel(input) => ipc_result(
            automation::ipc::call(
                &input.common,
                json!({"command": "cancel", "job_id": input.job_id, "instance": input.instance}),
            ),
            input.common,
        ),
        #[cfg(any(windows, target_os = "macos"))]
        Command::Stop(input) => ipc_result(
            automation::ipc::call(
                &input.common,
                json!({"command": "stop", "instance": input.instance}),
            ),
            input.common,
        ),
        #[cfg(windows)]
        Command::Shell(input) => ipc_result(
            desktop::shell::run(&input).map_err(Into::into),
            input.common,
        ),
        #[cfg(target_os = "macos")]
        Command::Shell(input) => {
            ipc_result(macos_shell::run(&input).map_err(Into::into), input.common)
        }
    };
    if let Some(path) = &common.report
        && let Err(error) = write_report(path, &report)
    {
        report["ok"] = json!(false);
        report["report_error"] = json!(error.to_string());
        code = 1;
    }
    let mut output = io::stdout().lock();
    serde_json::to_writer_pretty(&mut output, &report)?;
    output.write_all(b"\n")?;
    Ok(code)
}

pub fn entry() -> ExitCode {
    let mut arguments = env::args_os();
    let program = arguments.next().unwrap_or_default();
    if arguments.next().is_some_and(|value| value == "mcp") {
        return mcp::entry(std::iter::once(program).chain(arguments));
    }
    let cli = Cli::parse();
    let result: automation::Result<u8> = Catalog::load().map_err(Into::into).and_then(|catalog| {
        debug_assert!(!catalog.baseline.is_empty());
        if let Some(command) = cli.command {
            return run(command, &catalog).map_err(|error| error.to_string().into());
        }
        #[cfg(windows)]
        if cli.desktop
            || cli.tray
            || !cli.open.is_empty()
            || env::current_exe().ok().is_some_and(|exe| {
                exe.file_stem()
                    .is_some_and(|name| name == "magic_converter")
            })
        {
            let common = Common {
                data_dir: cli.data_dir,
                ..Default::default()
            };
            return desktop::run(catalog, common, cli.open, cli.tray);
        }
        Cli::command().print_help()?;
        Ok(0)
    });
    match result {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
