#[cfg(windows)]
pub mod ipc;
#[cfg(windows)]
mod pipe;
pub mod watch;

use crate::{Catalog, Common, resolved, write_report};
use rusqlite::Connection;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use uuid::Uuid;

type Error = Box<dyn std::error::Error + Send + Sync>;
pub(crate) type Result<T> = std::result::Result<T, Error>;

struct Active {
    action: String,
    files: Vec<(String, &'static str)>,
    cancelled: Arc<AtomicBool>,
    #[cfg(windows)]
    label: String,
    #[cfg(windows)]
    showing_progress: bool,
}

#[derive(Default)]
struct ActiveJobs {
    jobs: Mutex<BTreeMap<u64, Active>>,
    #[cfg(windows)]
    events: Option<std::sync::mpsc::Sender<ipc::DesktopEvent>>,
}

impl ActiveJobs {
    #[cfg(windows)]
    fn with_desktop(desktop: Option<&ipc::DesktopHost>) -> Self {
        Self {
            events: desktop.map(|host| host.events.clone()),
            ..Self::default()
        }
    }

    fn register(
        self: &Arc<Self>,
        id: u64,
        plan: &crate::Planned<'_>,
        cancelled: Arc<AtomicBool>,
    ) -> JobGuard {
        let files = plan
            .paths
            .iter()
            .map(|path| {
                (
                    path.file_name()
                        .unwrap_or(path.as_os_str())
                        .to_string_lossy()
                        .into_owned(),
                    "queued",
                )
            })
            .collect();
        self.jobs.lock().unwrap().insert(
            id,
            Active {
                action: plan.selection.action.key.clone(),
                files,
                cancelled,
                #[cfg(windows)]
                label: plan.selection.name.clone(),
                #[cfg(windows)]
                showing_progress: false,
            },
        );
        JobGuard {
            jobs: self.clone(),
            id,
        }
    }

    #[cfg(windows)]
    fn snapshots(&self) -> Vec<Value> {
        self.jobs.lock().unwrap().iter().take(100).map(|(&id, job)| {
            let files: Vec<_> = job.files.iter().take(64).map(|(name, state)| json!({"name": name, "state": state})).collect();
            json!({"job_id": id, "action": job.action, "total": job.files.len(), "files": files})
        }).collect()
    }

    #[cfg(windows)]
    fn cancel(&self, id: u64) -> bool {
        self.jobs.lock().unwrap().get(&id).is_some_and(|job| {
            job.cancelled.store(true, Ordering::Relaxed);
            true
        })
    }

    #[cfg(windows)]
    fn cancel_all(&self) {
        for job in self.jobs.lock().unwrap().values() {
            job.cancelled.store(true, Ordering::Relaxed);
        }
    }
}

struct JobGuard {
    jobs: Arc<ActiveJobs>,
    id: u64,
}

impl JobGuard {
    fn update(&self, index: usize, state: &'static str) {
        if let Some(job) = self.jobs.jobs.lock().unwrap().get_mut(&self.id) {
            job.files[index].1 = state;
            #[cfg(windows)]
            if let Some(events) = &self.jobs.events {
                job.showing_progress = true;
                let finished = job
                    .files
                    .iter()
                    .filter(|(_, state)| !matches!(*state, "queued" | "running"))
                    .count();
                let _ = events.send(ipc::DesktopEvent::Progress(
                    self.id,
                    ipc::JobProgress {
                        label: job.label.clone(),
                        file: job.files[index].0.clone(),
                        finished,
                        total: job.files.len(),
                    },
                ));
            }
        }
    }

    #[cfg(windows)]
    fn finish(&self, result: &Value) {
        use ipc::ProgressOutcome;
        let outcome = if result["cancelled"] == true {
            ProgressOutcome::Cancelled
        } else if result["completed"] != result["total"] {
            ProgressOutcome::Failed
        } else if result["warnings"]
            .as_array()
            .is_some_and(|warnings| !warnings.is_empty())
        {
            ProgressOutcome::Warning
        } else {
            ProgressOutcome::Done
        };
        let mut jobs = self.jobs.jobs.lock().unwrap();
        if let Some(job) = jobs.get_mut(&self.id)
            && let Some(events) = &self.jobs.events
        {
            job.showing_progress = false;
            let _ = events.send(ipc::DesktopEvent::ProgressFinished(self.id, outcome));
        }
    }
}

impl Drop for JobGuard {
    fn drop(&mut self) {
        let _job = self.jobs.jobs.lock().unwrap().remove(&self.id);
        #[cfg(windows)]
        if _job.is_some_and(|job| job.showing_progress)
            && let Some(events) = &self.jobs.events
        {
            let _ = events.send(ipc::DesktopEvent::ProgressFinished(
                self.id,
                ipc::ProgressOutcome::Failed,
            ));
        }
    }
}

pub fn read_json(path: &Path, limit: usize) -> Result<Value> {
    let mut raw = Vec::new();
    File::open(path)?
        .take(limit as u64 + 1)
        .read_to_end(&mut raw)?;
    if raw.len() > limit {
        return Err("JSON 파일 크기가 제한을 초과했습니다.".into());
    }
    Ok(serde_json::from_slice(
        raw.strip_prefix(b"\xef\xbb\xbf").unwrap_or(&raw),
    )?)
}

pub(crate) fn directory(common: &Common) -> io::Result<PathBuf> {
    let directory = crate::profile::directory(common)?;
    fs::create_dir_all(&directory)?;
    resolved(&directory)
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn check(cancelled: &AtomicBool) -> io::Result<()> {
    if cancelled.load(Ordering::Relaxed) {
        Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "취소되었습니다.",
        ))
    } else {
        Ok(())
    }
}

pub fn ctrlc() -> std::result::Result<(), ctrlc::Error> {
    ctrlc::set_handler(|| crate::INTERRUPTED.store(true, Ordering::Relaxed))
}

fn sha256(bytes: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

pub(crate) fn valid_id(value: &str) -> bool {
    (1..=64).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

// Match the JSON that Python hashes for the durable watch and IPC keys.
fn sorted_json(value: &Value, output: &mut String) {
    match value {
        Value::Object(values) => {
            let mut pairs: Vec<_> = values.iter().collect();
            pairs.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
            output.push('{');
            for (index, (key, value)) in pairs.into_iter().enumerate() {
                if index != 0 {
                    output.push_str(", ");
                }
                output.push_str(&Value::String(key.clone()).to_string());
                output.push_str(": ");
                sorted_json(value, output);
            }
            output.push('}');
        }
        Value::Array(values) => {
            output.push('[');
            for (index, value) in values.iter().enumerate() {
                if index != 0 {
                    output.push_str(", ");
                }
                sorted_json(value, output);
            }
            output.push(']');
        }
        _ => output.push_str(&value.to_string()),
    }
}

fn signature(value: &Value) -> String {
    let mut encoded = String::new();
    sorted_json(value, &mut encoded);
    sha256(encoded)
}

fn fields(value: &mut Value, defaults: &Value, key: &str) -> std::result::Result<(), String> {
    if value.get(key).is_none() {
        value[key] = defaults.clone();
    }
    let options = value[key]
        .as_object_mut()
        .ok_or("프리셋 옵션이 올바르지 않습니다.")?;
    let defaults = defaults.as_object().expect("built-in option defaults");
    if options.keys().any(|name| !defaults.contains_key(name)) {
        return Err("알 수 없는 프리셋 옵션입니다.".to_owned());
    }
    for (name, default) in defaults {
        options
            .entry(name.clone())
            .or_insert_with(|| default.clone());
    }
    Ok(())
}

pub(crate) fn recipe_tool_fields(action: &str) -> &'static [&'static str] {
    match action {
        "tool:pdf_select" | "tool:pdf_split" => &["pdf_pages"],
        "tool:pdf_rotate" => &["pdf_pages", "rotation"],
        "tool:image_rotate" => &["rotation"],
        "tool:image_flip" => &["flip"],
        "tool:image_crop" => &["crop_x", "crop_y", "crop_width", "crop_height"],
        "tool:image_frames" => &["frame_interval", "max_frames"],
        "tool:media_trim" => &["start_ms", "duration_ms"],
        "tool:media_frames" => &["start_ms", "duration_ms", "frame_interval", "max_frames"],
        "tool:subtitle_shift" => &["subtitle_offset_ms"],
        _ => &[],
    }
}

pub(crate) fn recipe_encoder_fields(action: &str) -> [bool; 3] {
    [
        action.starts_with("image:"),
        matches!(
            action,
            "media:mp4" | "media:mov" | "media:mkv" | "media:webm"
        ),
        matches!(
            action,
            "media:mp4"
                | "media:mov"
                | "media:mkv"
                | "media:webm"
                | "media:avi"
                | "media:wmv"
                | "media:mp3"
                | "audio:m4a"
                | "audio:ogg"
                | "audio:opus"
                | "audio:wma"
        ),
    ]
}

pub fn normalize_recipe(catalog: &Catalog, mut value: Value) -> std::result::Result<Value, String> {
    let id = value["id"]
        .as_str()
        .filter(|id| valid_id(id))
        .ok_or("프리셋 ID가 올바르지 않습니다.")?;
    let name = value["name"]
        .as_str()
        .filter(|name| {
            !name.trim().is_empty()
                && name.chars().count() <= 60
                && !name.chars().any(|ch| ch < ' ')
        })
        .ok_or("프리셋 이름과 지원 작업을 지정하세요.")?;
    let action = value["action_key"]
        .as_str()
        .filter(|key| catalog.actions.iter().any(|action| action.key == *key))
        .ok_or("프리셋 이름과 지원 작업을 지정하세요.")?;
    let mut recipe = json!({"id": id, "name": name, "action_key": action});
    let defaults = &catalog.default_preferences["presets"][0];
    for key in ["options", "tools", "output"] {
        if let Some(item) = value.get_mut(key) {
            recipe[key] = item.take();
        }
    }
    fields(&mut recipe, &defaults["options"], "options")?;
    fields(&mut recipe, &defaults["tools"], "tools")?;
    if recipe["output"].is_null() {
        recipe["output"] = Value::Null;
    } else {
        fields(
            &mut recipe,
            &catalog.default_preferences["output"],
            "output",
        )?;
        let rule: crate::OutputRule =
            serde_json::from_value(recipe["output"].clone()).map_err(|error| error.to_string())?;
        rule.validate_recipe().map_err(|error| error.to_string())?;
    }
    let options = &recipe["options"];
    let tools = &recipe["tools"];
    let integer =
        |value: &Value, min, max| value.as_i64().is_some_and(|n| (min..=max).contains(&n));
    let action = recipe["action_key"].as_str().expect("validated action key");
    let [image, video, audio] = recipe_encoder_fields(action);
    if !integer(&options["image_quality"], 1, 100)
        || !integer(&options["image_max_edge"], 0, 16384)
        || (!options["video_crf"].is_null() && !integer(&options["video_crf"], 0, 51))
        || (!options["audio_bitrate"].is_null() && !integer(&options["audio_bitrate"], 32, 512))
        || (!image && (options["image_quality"] != 90 || options["image_max_edge"] != 0))
        || (!options["video_crf"].is_null() && !video)
        || (!options["audio_bitrate"].is_null() && !audio)
    {
        return Err("이 작업의 인코더 옵션이 올바르지 않습니다.".to_owned());
    }
    let allowed = recipe_tool_fields(action);
    if tools
        .as_object()
        .expect("tool options")
        .iter()
        .any(|(key, value)| !allowed.contains(&key.as_str()) && value != &defaults["tools"][key])
        || !matches!(tools["rotation"].as_i64(), Some(0 | 90 | 180 | 270))
        || !matches!(tools["flip"].as_str(), Some("horizontal" | "vertical"))
        || ["crop_x", "crop_y", "crop_width", "crop_height"]
            .iter()
            .any(|key| !integer(&tools[*key], 0, 100000))
        || ["start_ms", "duration_ms"]
            .iter()
            .any(|key| !integer(&tools[*key], 0, 604800000))
        || !integer(&tools["frame_interval"], 1, 10000)
        || !integer(&tools["max_frames"], 1, 100000)
        || !integer(&tools["subtitle_offset_ms"], -604800000, 604800000)
        || !tools["pdf_pages"].as_str().is_some_and(valid_pdf_range)
    {
        return Err("이 작업의 도구 옵션이 올바르지 않습니다.".to_owned());
    }
    Ok(recipe)
}

fn valid_pdf_range(text: &str) -> bool {
    use std::sync::OnceLock;
    static RANGE: OnceLock<regex::Regex> = OnceLock::new();
    text.is_empty() || (text.chars().count() <= 1000
        && !text.chars().any(|ch| ch < ' ')
        && RANGE.get_or_init(|| regex::Regex::new(r"^\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?(?:\s*,\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?)*\s*$").expect("PDF range expression")).is_match(text))
}

fn database(path: &Path, schema: &str) -> Result<Connection> {
    let db = Connection::open(path)?;
    db.busy_timeout(Duration::from_secs(5))?;
    let version: u64 = db.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version > 1 {
        return Err("최신 버전 자동화 기록은 보존합니다.".into());
    }
    db.execute_batch(schema)?;
    db.pragma_update(None, "user_version", 1)?;
    Ok(db)
}

fn new_id() -> String {
    Uuid::new_v4().simple().to_string()
}

struct ProfileLock {
    _file: File,
    #[cfg(not(windows))]
    path: PathBuf,
}

impl ProfileLock {
    fn acquire(path: PathBuf, timeout: Duration) -> Result<Self> {
        let started = std::time::Instant::now();
        let host = std::env::var("COMPUTERNAME").unwrap_or_default();
        let exe = std::env::current_exe()?;
        let name = exe.file_stem().unwrap_or_default().to_string_lossy();
        let mut stale_checked = false;
        loop {
            let mut options = OpenOptions::new();
            options.write(true).create_new(true);
            #[cfg(windows)]
            {
                use std::os::windows::fs::OpenOptionsExt;
                use windows_sys::Win32::Storage::FileSystem::{
                    FILE_FLAG_DELETE_ON_CLOSE, FILE_SHARE_READ,
                };
                options
                    .custom_flags(FILE_FLAG_DELETE_ON_CLOSE)
                    .share_mode(FILE_SHARE_READ);
            }
            match options.open(&path) {
                Ok(mut file) => {
                    writeln!(file, "{}\n{name}\n{host}\n\n", std::process::id())?;
                    return Ok(Self {
                        _file: file,
                        #[cfg(not(windows))]
                        path,
                    });
                }
                Err(error)
                    if error.kind() == io::ErrorKind::AlreadyExists
                        || (matches!(error.raw_os_error(), Some(5 | 32)) && path.exists()) =>
                {
                    if !stale_checked && remove_stale_lock(&path, &host) {
                        stale_checked = true;
                        continue;
                    }
                    if started.elapsed() >= timeout {
                        return Err("이 설정 폴더가 다른 프로세스에서 사용 중입니다.".into());
                    }
                    std::thread::sleep(Duration::from_millis(25));
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
}

#[cfg(not(windows))]
impl Drop for ProfileLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(windows)]
fn remove_stale_lock(path: &Path, host: &str) -> bool {
    use std::mem::size_of;
    use std::os::windows::{
        fs::{MetadataExt, OpenOptionsExt},
        io::AsRawHandle,
    };
    use windows_sys::Win32::Foundation::{CloseHandle, GENERIC_READ};
    use windows_sys::Win32::Storage::FileSystem::{
        DELETE, FILE_DISPOSITION_INFO, FILE_FLAG_OPEN_REPARSE_POINT, FileDispositionInfo,
        SetFileInformationByHandle,
    };
    use windows_sys::Win32::System::Threading::{OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION};
    let Ok(file) = OpenOptions::new()
        .access_mode(GENERIC_READ | DELETE)
        .share_mode(0)
        .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
        .open(path)
    else {
        return false;
    };
    if !file
        .metadata()
        .is_ok_and(|metadata| metadata.is_file() && metadata.file_attributes() & 0x400 == 0)
    {
        return false;
    }
    let mut content = String::new();
    if (&file).take(4096).read_to_string(&mut content).is_err() {
        return false;
    }
    let mut lines = content.lines();
    let Some(pid) = lines
        .next()
        .and_then(|line| line.parse::<u32>().ok())
        .filter(|&pid| pid > 0)
    else {
        return false;
    };
    if lines.next().is_none()
        || !lines
            .next()
            .is_some_and(|saved| saved.eq_ignore_ascii_case(host))
    {
        return false;
    }
    // An inaccessible or running process can still own this lock.
    let process = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if !process.is_null() {
        unsafe { CloseHandle(process) };
        return false;
    }
    if io::Error::last_os_error().raw_os_error() != Some(87) {
        return false;
    }
    let information = FILE_DISPOSITION_INFO { DeleteFile: true };
    unsafe {
        SetFileInformationByHandle(
            file.as_raw_handle(),
            FileDispositionInfo,
            (&raw const information).cast(),
            size_of::<FILE_DISPOSITION_INFO>() as u32,
        ) != 0
    }
}

#[cfg(not(windows))]
fn remove_stale_lock(_: &Path, _: &str) -> bool {
    false
}

pub(crate) fn history(directory: &Path) -> Result<Vec<Value>> {
    let path = directory.join("history.json");
    if !path.exists() {
        return Ok(Vec::new());
    }
    let mut value = read_json(&path, 16 * 1024 * 1024)?;
    if value["version"] != 1 {
        return Err("읽을 수 없는 결과 기록은 보존합니다.".into());
    }
    match value["entries"].take() {
        Value::Array(entries) if entries.len() <= 1000 => Ok(entries),
        _ => Err("잘못된 기록 구조".into()),
    }
}

fn stamp() -> String {
    use time::format_description::well_known::Rfc3339;
    time::OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .expect("current UTC timestamp")
}

fn record(
    directory: &Path,
    profile: &Value,
    catalog: &Catalog,
    job: &Value,
    recipe: &Value,
    started: &str,
    elapsed: Duration,
) -> Result<()> {
    let limit = profile["history_limit"].as_u64().unwrap_or(200).min(1000) as usize;
    if limit == 0 {
        return Ok(());
    }
    let _lock = ProfileLock::acquire(directory.join("history.json.lock"), Duration::from_secs(1))?;
    let mut entries = match history(directory) {
        Ok(entries) => entries,
        Err(_) => return Ok(()),
    };
    let label = catalog
        .actions
        .iter()
        .find(|item| job["action"].as_str() == Some(&item.key))
        .map(|item| item.label.as_str())
        .unwrap_or_default();
    entries.push(json!({
        "id": new_id(), "started_at": started, "finished_at": stamp(),
        "elapsed_seconds": (elapsed.as_secs_f64() * 1000.0).round() / 1000.0,
        "action_key": job["action"], "label": label, "preset_id": recipe["id"],
        "options": recipe["options"], "tools": recipe["tools"], "output": recipe["output"],
        "files": job["files"],
    }));
    if entries.len() > limit {
        entries.drain(..entries.len() - limit);
    }
    let sizes: Vec<_> = entries
        .iter()
        .map(|item| item.to_string().len() + 2)
        .collect();
    let mut size: usize = sizes.iter().sum();
    let remove = sizes
        .iter()
        .take_while(|item| {
            let oversized = size > 12 * 1024 * 1024;
            if oversized {
                size -= **item;
            }
            oversized
        })
        .count();
    entries.drain(..remove);
    write_report(
        &directory.join("history.json"),
        &json!({"version": 1, "entries": entries}),
    )?;
    Ok(())
}

pub fn convert(catalog: &Catalog, input: &crate::Convert) -> Result<(Value, u8)> {
    ctrlc()?;
    convert_with(catalog, input, &crate::INTERRUPTED)
}

pub(crate) fn convert_with(
    catalog: &Catalog,
    input: &crate::Convert,
    cancelled: &AtomicBool,
) -> Result<(Value, u8)> {
    let planned = match crate::plan(catalog, input) {
        Err(crate::PlanError::Selection(message)) => {
            return Ok((
                json!({"ok": false, "interrupted": false, "jobs": [], "errors": [message]}),
                1,
            ));
        }
        result => result?,
    };
    let recipe = planned.recipe.clone();
    let started = stamp();
    let clock = std::time::Instant::now();
    let result = crate::execute(catalog, planned, cancelled, 1, |_, _| {});
    let profile = crate::preferences(catalog, &input.common)?;
    let directory = directory(&input.common)?;
    record(
        &directory,
        &profile,
        catalog,
        &result.0["jobs"][0],
        &recipe,
        &started,
        clock.elapsed(),
    )?;
    Ok(result)
}
