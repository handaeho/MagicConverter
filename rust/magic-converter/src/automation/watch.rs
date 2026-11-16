use super::{
    ActiveJobs, ProfileLock, Result, check, database, directory, normalize_recipe, now, read_json,
    record, sha256, signature, stamp, valid_id,
};
use crate::{
    Catalog, Common, Convert, execute, media, output::move_folder, plan, preferences, resolved,
    selected_recipe, timeout, write_report,
};
use clap::Args;
use glob::Pattern;
use rusqlite::{Connection, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::{self, ErrorKind, Read};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime};
use unicode_casefold::UnicodeCaseFold;

#[derive(Args)]
pub struct Watch {
    #[command(flatten)]
    request: Convert,
    #[arg(long)]
    recursive: bool,
    #[arg(long, default_value = "*")]
    include: String,
    #[arg(long)]
    settle: Option<f64>,
    #[arg(long)]
    once: bool,
    #[arg(long, value_parser = timeout)]
    duration: Option<Duration>,
    #[arg(long)]
    retry: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Rule {
    id: String,
    folder: PathBuf,
    output: PathBuf,
    recipe: Value,
    #[serde(default)]
    recursive: bool,
    #[serde(default = "include_all")]
    include: String,
    #[serde(default = "enabled")]
    enabled: bool,
    #[serde(default = "settle_default")]
    settle: Value,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    target: String,
}

fn include_all() -> String {
    "*".to_owned()
}
fn enabled() -> bool {
    true
}
fn settle_default() -> Value {
    json!(2.0)
}

pub(super) struct ValidRule {
    rule: Rule,
    hash: String,
    folder: PathBuf,
    output: PathBuf,
    patterns: Vec<Pattern>,
    settle: Duration,
}

impl Rule {
    fn validate(mut self, catalog: &Catalog) -> Result<ValidRule> {
        if !valid_id(&self.id) {
            return Err("감시 규칙 ID가 올바르지 않습니다.".into());
        }
        if !self.folder.is_absolute() || !self.output.is_absolute() {
            return Err("감시·출력 폴더는 절대 경로여야 합니다.".into());
        }
        let folder = resolved(&self.folder)?;
        let output = resolved(&self.output)?;
        if path_within(&folder, &output) {
            return Err("감시 출력은 별도 하위 폴더 또는 감시 폴더 밖에 지정하세요.".into());
        }
        if self.enabled
            && (!folder.is_dir()
                || fs::symlink_metadata(&self.folder).is_ok_and(|data| reparse(&data)))
        {
            return Err("사용 중인 감시 폴더가 존재하지 않습니다.".into());
        }
        self.recipe = normalize_recipe(catalog, self.recipe)?;
        if matches!(
            self.recipe["action_key"].as_str(),
            Some("zip" | "copy_path" | "terminal" | "tool:pdf_merge")
        ) {
            return Err("감시 규칙에는 파일별 변환 작업을 지정하세요.".into());
        }
        if !self.target.is_empty()
            && !catalog
                .formats
                .iter()
                .any(|format| format.id == self.target && format.writable)
        {
            return Err("감시 출력 형식이 올바르지 않습니다.".into());
        }
        let settle = self
            .settle
            .as_f64()
            .filter(|value| (0.1..=300.0).contains(value))
            .map(Duration::from_secs_f64)
            .ok_or("감시 안정화 시간은 0.1~300초여야 합니다.")?;
        if self.include.chars().count() > 1000 || self.include.trim().is_empty() {
            return Err("포함 패턴을 지정하세요 (예: *.png;*.jpg).".into());
        }
        let patterns = self
            .include
            .to_lowercase()
            .split(';')
            .filter_map(|pattern| Pattern::new(pattern.trim()).ok())
            .collect();
        let hash = signature(&serde_json::to_value(&self)?);
        Ok(ValidRule {
            rule: self,
            hash,
            folder,
            output,
            patterns,
            settle,
        })
    }
}

pub(super) fn rules(catalog: &Catalog, directory: &Path) -> Result<Vec<ValidRule>> {
    let path = directory.join("automation.json");
    if !path.exists() {
        return Ok(Vec::new());
    }
    let value = read_json(&path, 2 * 1024 * 1024)?;
    let list = value["rules"]
        .as_array()
        .filter(|rules| value["version"] == 1 && rules.len() <= 100)
        .ok_or("자동화 설정 버전을 읽을 수 없습니다. 기존 파일을 보존했습니다.")?;
    if let Some(output) = value.get("web_output") {
        output
            .as_str()
            .filter(|output| {
                output.chars().count() <= 2000
                    && (output.is_empty() || Path::new(output).is_absolute())
            })
            .ok_or("웹 출력 폴더는 절대 경로여야 합니다.")?;
    }
    let rules: Vec<_> = list
        .iter()
        .map(|item| serde_json::from_value::<Rule>(item.clone())?.validate(catalog))
        .collect::<Result<_>>()?;
    let ids: HashSet<_> = rules.iter().map(|rule| &rule.rule.id).collect();
    if ids.len() != rules.len() {
        return Err("감시 규칙이 중복되거나 100개를 초과했습니다.".into());
    }
    Ok(rules)
}

#[cfg(windows)]
pub(super) fn running_rules(rules: &[ValidRule]) -> bool {
    rules.iter().any(|item| item.rule.enabled)
}

fn path_within(path: &Path, base: &Path) -> bool {
    let mut parts = path.components();
    base.components().all(|part| {
        parts.next().is_some_and(|item| {
            item.as_os_str()
                .to_string_lossy()
                .case_fold()
                .eq(part.as_os_str().to_string_lossy().case_fold())
        })
    })
}

#[cfg(windows)]
fn reparse(data: &Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    data.file_attributes() & 0x400 != 0
}
#[cfg(not(windows))]
fn reparse(data: &Metadata) -> bool {
    data.is_symlink()
}

fn exclusive(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT;
        options
            .share_mode(0)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    if !file
        .metadata()
        .is_ok_and(|data| data.is_file() && !reparse(&data))
    {
        return Err(io::Error::new(
            ErrorKind::InvalidInput,
            "링크·재분석 지점은 감시하지 않습니다.",
        ));
    }
    Ok(file)
}

fn digest(mut file: File, cancelled: &AtomicBool) -> io::Result<String> {
    let mut hash = Sha256::new();
    let mut buffer = vec![0_u8; 1024 * 1024];
    loop {
        check(cancelled)?;
        let count = file.read(&mut buffer)?;
        if count == 0 {
            return Ok(format!("{:x}", hash.finalize()));
        }
        hash.update(&buffer[..count]);
    }
}

struct Event {
    key: String,
    source: PathBuf,
    digest: String,
    state: String,
    work: PathBuf,
    target: PathBuf,
}

type Fingerprint = (u64, SystemTime, Option<SystemTime>);

fn fingerprint(data: &Metadata) -> io::Result<Fingerprint> {
    Ok((data.len(), data.modified()?, data.created().ok()))
}

struct Candidate {
    fingerprint: Fingerprint,
    first: Instant,
    hash: String,
    checked: Instant,
    changed: bool,
}

struct Journal {
    db: Connection,
    work_root: PathBuf,
}

impl Journal {
    fn open(directory: &Path) -> Result<Self> {
        let db = database(
            &directory.join("watch-journal.sqlite3"),
            "CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, rule TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, work TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)",
        )?;
        Ok(Self {
            db,
            work_root: directory.join("watch-work"),
        })
    }

    fn update(&self, event: &Event, state: &str, detail: &str) -> Result<()> {
        let detail: String = detail.chars().take(4000).collect();
        self.db.execute(
            "UPDATE events SET state=?,detail=?,updated=? WHERE key=?",
            params![state, detail, now(), event.key],
        )?;
        Ok(())
    }

    fn claim(&self, event: &Event, rule: &str) -> Result<bool> {
        Ok(self.db.execute("INSERT OR IGNORE INTO events (key,rule,source,digest,state,work,target,updated) VALUES (?,?,?,?,'queued',?,?,?)",
            params![event.key, rule, event.source.to_string_lossy(), event.digest, event.work.to_string_lossy(), event.target.to_string_lossy(), now()])? == 1)
    }

    fn recover(&self, cancelled: &AtomicBool) -> Result<()> {
        let mut query = self.db.prepare("SELECT key,source,digest,state,work,target FROM events WHERE state IN ('queued','publishing') ORDER BY updated DESC LIMIT 10000")?;
        let events = query
            .query_map([], |row| {
                Ok(Event {
                    key: row.get(0)?,
                    source: PathBuf::from(row.get::<_, String>(1)?),
                    digest: row.get(2)?,
                    state: row.get(3)?,
                    work: PathBuf::from(row.get::<_, String>(4)?),
                    target: PathBuf::from(row.get::<_, String>(5)?),
                })
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        drop(query);
        for event in events {
            check(cancelled)?;
            self.validate_work(&event)?;
            if receipt_valid(&event.target, &event.key, cancelled) {
                self.update(&event, "done", "")?;
            } else if event.state == "publishing"
                && receipt_valid(&event.work.join("results"), &event.key, cancelled)
            {
                if let Err(error) = self.publish(&event, cancelled) {
                    check(cancelled)?;
                    self.update(&event, "failed", &error.to_string())?;
                }
            } else {
                self.cleanup(&event)?;
                self.db.execute(
                    "DELETE FROM events WHERE key=? AND state IN ('queued','publishing')",
                    [&event.key],
                )?;
                continue;
            }
            self.cleanup(&event)?;
        }
        Ok(())
    }

    fn validate_work(&self, event: &Event) -> Result<()> {
        if event.key.len() != 64
            || !event.key.bytes().all(|ch| ch.is_ascii_hexdigit())
            || event.work != self.work_root.join(&event.key)
            || !event.target.is_absolute()
            || fs::symlink_metadata(&self.work_root)
                .is_ok_and(|data| !data.is_dir() || reparse(&data))
        {
            return Err("소유하지 않은 감시 작업 폴더입니다.".into());
        }
        Ok(())
    }

    fn cleanup(&self, event: &Event) -> Result<()> {
        self.validate_work(event)?;
        remove_directory(&event.work)
    }

    fn publish(&self, event: &Event, cancelled: &AtomicBool) -> Result<()> {
        let target = &event.target;
        if fs::symlink_metadata(target).is_ok() {
            if !receipt_valid(target, &event.key, cancelled) {
                return Err(format!(
                    "감시 결과 이름이 기존 파일과 충돌합니다: {}",
                    target.display()
                )
                .into());
            }
        } else {
            let base = target
                .parent()
                .ok_or("감시 출력 폴더가 올바르지 않습니다.")?;
            fs::create_dir_all(base)?;
            let stage = base.join(format!(".magic-watch-{}", event.key));
            remove_directory(&stage)?;
            fs::create_dir(&stage)?;
            let publish = || -> Result<()> {
                copy_contents(&event.work.join("results"), &stage, cancelled)?;
                if !receipt_valid(&stage, &event.key, cancelled) {
                    check(cancelled)?;
                    return Err("감시 결과의 게시 전 검증에 실패했습니다.".into());
                }
                let started = Instant::now();
                loop {
                    check(cancelled)?;
                    match move_folder(&stage, target) {
                        Ok(()) => return Ok(()),
                        Err(error)
                            if matches!(error.raw_os_error(), Some(5 | 32 | 33))
                                && !target.exists()
                                && started.elapsed() < Duration::from_secs(1) =>
                        {
                            thread::sleep(Duration::from_millis(20))
                        }
                        Err(error) => return Err(error.into()),
                    }
                }
            };
            if let Err(error) = publish() {
                remove_directory(&stage)?;
                return Err(error);
            }
        }
        self.update(event, "done", "")
    }
}

fn remove_directory(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(data) if data.is_dir() && !reparse(&data) => Ok(fs::remove_dir_all(path)?),
        Ok(_) => Err("소유하지 않은 감시 임시 폴더입니다.".into()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn tree(root: &Path, cancelled: &AtomicBool) -> Result<(Vec<PathBuf>, Vec<PathBuf>)> {
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    let mut pending = vec![root.to_owned()];
    while let Some(parent) = pending.pop() {
        check(cancelled)?;
        for entry in fs::read_dir(parent)? {
            let entry = entry?;
            let path = entry.path();
            let data = fs::symlink_metadata(&path)?;
            if reparse(&data) {
                return Err("링크·재분석 지점은 감시 결과에 포함할 수 없습니다.".into());
            }
            let relative = path.strip_prefix(root)?.to_owned();
            if data.is_dir() {
                pending.push(path);
                dirs.push(relative);
            } else if data.is_file() {
                files.push(relative);
            }
        }
    }
    dirs.sort();
    files.sort();
    Ok((dirs, files))
}

fn copy_contents(source: &Path, stage: &Path, cancelled: &AtomicBool) -> Result<()> {
    let (dirs, files) = tree(source, cancelled)?;
    for directory in dirs {
        check(cancelled)?;
        fs::create_dir(stage.join(directory))?;
    }
    for path in files {
        let mut from = exclusive(&source.join(&path))?;
        let mut to = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(stage.join(&path))?;
        let size = from.metadata()?.len();
        magic_copy_worker::copy_stream(&mut from, &mut to, size, || check(cancelled), |_| Ok(()))?;
    }
    Ok(())
}

fn receipt_valid(root: &Path, key: &str, cancelled: &AtomicBool) -> bool {
    let validate = || -> Result<bool> {
        if !fs::symlink_metadata(root).is_ok_and(|data| data.is_dir() && !reparse(&data)) {
            return Ok(false);
        }
        let receipt = read_json(&root.join(".magic-receipt.json"), 2 * 1024 * 1024)?;
        let files = receipt["files"]
            .as_array()
            .ok_or("감시 영수증의 파일 목록이 잘못되었습니다.")?;
        let directories = receipt["directories"].as_array();
        if receipt["key"] != key || (files.is_empty() && directories.is_none_or(Vec::is_empty)) {
            return Ok(false);
        }
        for item in files {
            let Some(path) = safe_member(root, &item["path"]) else {
                return Ok(false);
            };
            if digest(exclusive(&path)?, cancelled)? != item["sha256"].as_str().unwrap_or_default()
            {
                return Ok(false);
            }
        }
        Ok(directories
            .into_iter()
            .flatten()
            .all(|item| safe_member(root, item).is_some_and(|path| path.is_dir())))
    };
    validate().unwrap_or(false)
}

fn safe_member(root: &Path, value: &Value) -> Option<PathBuf> {
    let relative = Path::new(value.as_str()?);
    let mut path = root.to_owned();
    for part in relative.components() {
        let Component::Normal(name) = part else {
            return None;
        };
        path.push(name);
        if reparse(&fs::symlink_metadata(&path).ok()?) {
            return None;
        }
    }
    (path != root).then_some(path)
}

struct Scan<'a> {
    rules: &'a [ValidRule],
    journal: Journal,
    pending: HashMap<(String, String), Candidate>,
    seen: HashSet<(String, String)>,
    quiescent: bool,
}

impl Scan<'_> {
    fn next(
        &mut self,
        catalog: &Catalog,
        rule: &ValidRule,
        cancelled: &AtomicBool,
    ) -> Result<Option<Event>> {
        let mut directories = vec![rule.folder.clone()];
        while let Some(folder) = directories.pop() {
            check(cancelled)?;
            let mut entries = match fs::read_dir(&folder) {
                Ok(entries) => entries.collect::<io::Result<Vec<_>>>()?,
                Err(error) if error.kind() == ErrorKind::NotFound => continue,
                Err(error) => return Err(error.into()),
            };
            entries.sort_by_key(|entry| entry.file_name());
            for entry in entries {
                check(cancelled)?;
                let path = entry.path();
                let name = entry.file_name().to_string_lossy().into_owned();
                let lower = name.to_lowercase();
                if name.starts_with('.')
                    || self
                        .rules
                        .iter()
                        .any(|item| path_within(&path, &item.output))
                    || path_within(&path, &self.journal.work_root)
                {
                    continue;
                }
                let data = match fs::symlink_metadata(&path) {
                    Ok(data) if !reparse(&data) => data,
                    _ => continue,
                };
                if data.is_dir() && rule.rule.recursive {
                    directories.push(path);
                    continue;
                }
                if !data.is_file()
                    || name.starts_with("~$")
                    || matches!(lower.as_str(), "desktop.ini" | "thumbs.db")
                    || catalog.detect(&path).is_none()
                    || !rule.patterns.iter().any(|pattern| pattern.matches(&lower))
                {
                    continue;
                }
                let candidate = (
                    rule.rule.id.clone(),
                    path.to_string_lossy().case_fold().collect::<String>(),
                );
                self.seen.insert(candidate.clone());
                let sample = fingerprint(&data)?;
                let current = Instant::now();
                let entry = self
                    .pending
                    .entry(candidate.clone())
                    .or_insert_with(|| Candidate {
                        fingerprint: sample,
                        first: current,
                        hash: String::new(),
                        checked: current,
                        changed: false,
                    });
                if entry.fingerprint != sample {
                    *entry = Candidate {
                        fingerprint: sample,
                        first: current,
                        hash: String::new(),
                        checked: current,
                        changed: false,
                    };
                }
                if current - entry.first < rule.settle {
                    self.quiescent = false;
                    continue;
                }
                let Ok(file) = exclusive(&path) else {
                    self.quiescent = false;
                    continue;
                };
                if file.metadata().and_then(|data| fingerprint(&data))? != sample {
                    self.pending.remove(&candidate);
                    self.quiescent = false;
                    continue;
                }
                if entry.hash.is_empty()
                    || entry.changed
                    || current - entry.checked >= Duration::from_secs(5)
                {
                    let hash = digest(file, cancelled)?;
                    entry.checked = current;
                    entry.changed = !entry.hash.is_empty() && entry.hash != hash;
                    entry.hash = hash;
                    if entry.changed {
                        entry.first = current;
                        self.quiescent = false;
                        continue;
                    }
                }
                let key = sha256(format!("{}\0{}\0{}", rule.hash, candidate.1, entry.hash));
                let stem: String = path
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .chars()
                    .take(80)
                    .collect();
                let event = Event {
                    source: path,
                    state: "queued".to_owned(),
                    digest: entry.hash.clone(),
                    target: rule.output.join(format!("{stem}_{}", &key[..16])),
                    work: self.journal.work_root.join(&key),
                    key,
                };
                if self.journal.claim(&event, &rule.rule.id)? {
                    self.quiescent = false;
                    return Ok(Some(event));
                }
            }
        }
        Ok(None)
    }
}

pub(super) struct Session<'a> {
    pub(super) catalog: &'a Catalog,
    pub(super) common: &'a Common,
    pub(super) profile: &'a Value,
    pub(super) directory: &'a Path,
    pub(super) timeout: Duration,
    pub(super) cancelled: &'a AtomicBool,
    pub(super) ids: &'a AtomicU64,
    pub(super) active: Option<&'a Arc<ActiveJobs>>,
}

impl Session<'_> {
    fn event(&self, journal: &Journal, rule: &ValidRule, event: &Event) -> Result<Value> {
        journal.cleanup(event)?;
        let results = event.work.join("results");
        fs::create_dir_all(&results)?;
        let mut recipe = rule.rule.recipe.clone();
        if !rule.rule.target.is_empty() {
            let disabled =
                if media::Tools::discover(self.profile["ffmpeg_path"].as_str().unwrap_or_default())
                    .is_some()
                {
                    Vec::new()
                } else {
                    vec!["ffmpeg".to_owned()]
                };
            let action = self
                .catalog
                .target_action(
                    &rule.rule.target,
                    std::slice::from_ref(&event.source),
                    &disabled,
                )
                .ok_or("이 파일은 지정한 감시 출력 형식으로 변환할 수 없습니다.")?;
            recipe["action_key"] = json!(action.key);
        }
        recipe["output"] = json!({"location": "folder", "directory": results,
            "template": recipe["output"]["template"].as_str().unwrap_or("{stem}_{tag}"), "collision": "rename"});
        let input = Convert {
            common: self.common.clone(),
            action: None,
            to: None,
            preset: None,
            output: None,
            paths: vec![event.source.clone()],
            timeout: self.timeout,
            recipe: Some(recipe.clone()),
        };
        let planned = plan(self.catalog, &input)?;
        let started = stamp();
        let clock = Instant::now();
        let job_id = self.ids.fetch_add(1, Ordering::Relaxed);
        let job_cancelled = Arc::new(AtomicBool::new(false));
        let guard = self
            .active
            .map(|active| active.register(job_id, &planned, job_cancelled.clone()));
        let cancelled = if guard.is_some() {
            if self.cancelled.load(Ordering::Relaxed) {
                job_cancelled.store(true, Ordering::Relaxed);
            }
            job_cancelled.as_ref()
        } else {
            self.cancelled
        };
        let (report, code) = execute(self.catalog, planned, cancelled, job_id, |index, state| {
            if let Some(guard) = &guard {
                guard.update(index, state);
            }
        });
        let mut job = report["jobs"][0].clone();
        if code == 0 {
            let publish = Self::finish_event(journal, event, &results, cancelled);
            if let Err(error) = publish {
                let cancelled = cancelled.load(Ordering::Relaxed);
                job["completed"] = json!(0);
                job["cancelled"] = json!(cancelled);
                job["outputs"] = json!([]);
                job["errors"] = if cancelled {
                    json!([])
                } else {
                    json!([error.to_string()])
                };
                job["files"][0]["outputs"] = json!([]);
                job["files"][0]["errors"] = job["errors"].clone();
                job["files"][0]["state"] = json!(if cancelled { "cancelled" } else { "failed" });
                journal.update(
                    event,
                    if cancelled { "cancelled" } else { "failed" },
                    &error.to_string(),
                )?;
            } else {
                job["outputs"] = json!([event.target]);
                job["files"][0]["outputs"] = json!([event.target]);
                recipe["output"] = json!({"location": "folder", "directory": rule.output, "template": "{stem}_{tag}", "collision": "rename"});
            }
        } else {
            let errors = job["errors"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join("\n");
            journal.update(
                event,
                if code == 130 { "cancelled" } else { "failed" },
                &errors,
            )?;
        }
        if let Err(error) = journal.cleanup(event)
            && let Some(warnings) = job["warnings"].as_array_mut()
        {
            warnings.push(json!(format!(
                "감시 임시 작업 폴더를 정리하지 못했습니다: {error}"
            )));
        }
        record(
            self.directory,
            self.profile,
            self.catalog,
            &job,
            &recipe,
            &started,
            clock.elapsed(),
        )?;
        Ok(job)
    }

    fn finish_event(
        journal: &Journal,
        event: &Event,
        results: &Path,
        cancelled: &AtomicBool,
    ) -> Result<()> {
        if digest(exclusive(&event.source)?, cancelled)? != event.digest {
            return Err("변환 중 원본이 변경되었습니다. 새 버전을 다시 감시합니다.".into());
        }
        let (directories, files) = tree(results, cancelled)?;
        if directories.is_empty() && files.is_empty() {
            return Err("감시 변환의 결과 파일을 확인하지 못했습니다.".into());
        }
        let mut hashes = Vec::new();
        for path in files {
            let hash = digest(exclusive(&results.join(&path))?, cancelled)?;
            hashes.push(json!({"path": path, "sha256": hash}));
        }
        write_report(
            &results.join(".magic-receipt.json"),
            &json!({"key": event.key, "files": hashes, "directories": directories}),
        )?;
        journal.update(event, "publishing", "")?;
        journal.publish(event, cancelled)
    }

    pub(super) fn run(
        &self,
        rules: &[ValidRule],
        once: Option<Duration>,
        retry: bool,
        mut submit: impl FnMut(Value),
    ) -> Result<()> {
        let _lock = ProfileLock::acquire(self.directory.join("watch.lock"), Duration::ZERO)?;
        let journal = Journal::open(self.directory)?;
        if retry {
            journal.db.execute(
                "DELETE FROM events WHERE state IN ('failed','cancelled','interrupted')",
                [],
            )?;
        }
        journal.recover(self.cancelled)?;
        let mut scan = Scan {
            journal,
            rules,
            pending: HashMap::new(),
            seen: HashSet::new(),
            quiescent: false,
        };
        let started = Instant::now();
        while !self.cancelled.load(Ordering::Relaxed) {
            scan.quiescent = true;
            scan.seen.clear();
            for rule in rules.iter().filter(|rule| rule.rule.enabled) {
                if let Some(event) = scan.next(self.catalog, rule, self.cancelled)? {
                    match self.event(&scan.journal, rule, &event) {
                        Ok(job) => submit(job),
                        Err(error) => {
                            let state = if self.cancelled.load(Ordering::Relaxed) {
                                "cancelled"
                            } else {
                                "failed"
                            };
                            scan.journal.update(&event, state, &error.to_string())?;
                            scan.journal.cleanup(&event)?;
                            if state == "failed" {
                                submit(json!(error.to_string()));
                            }
                        }
                    }
                    // This scan ended early for this rule. Keep candidates below the
                    // current file so each completes its own settling period.
                    scan.seen.extend(
                        scan.pending
                            .keys()
                            .filter(|key| key.0 == rule.rule.id)
                            .cloned(),
                    );
                }
            }
            scan.pending.retain(|key, _| scan.seen.contains(key));
            if scan.quiescent && once.is_some_and(|minimum| started.elapsed() > minimum) {
                break;
            }
            for _ in 0..10 {
                if self.cancelled.load(Ordering::Relaxed) {
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
        Ok(())
    }
}

pub fn run(catalog: &Catalog, input: &Watch) -> Result<(Value, u8)> {
    if input.request.paths.len() != 1 {
        return Err("감시 폴더 하나를 지정하세요.".into());
    }
    let common = &input.request.common;
    let folder = resolved(&input.request.paths[0])?;
    if !folder.is_dir() {
        return Err("감시 폴더가 존재하지 않습니다.".into());
    }
    let profile = preferences(catalog, common)?;
    let mut request = input.request.clone();
    request.paths.clear();
    let target = if let Some(target) = &request.to {
        let name = format!("file.{}", target.trim_start_matches('.'));
        catalog
            .detect(Path::new(&name))
            .filter(|(format, _)| format.writable)
            .map(|(format, _)| format.id.clone())
            .ok_or("지원하는 감시 출력 형식을 지정하세요.")?
    } else {
        String::new()
    };
    let recipe = selected_recipe(catalog, &request, &[], &profile, &[])?;
    let output = request
        .output
        .as_deref()
        .map(resolved)
        .transpose()?
        .unwrap_or_else(|| folder.join("_converted"));
    let rule = Rule {
        id: "cli".to_owned(),
        folder,
        output,
        recipe,
        recursive: input.recursive,
        include: input.include.clone(),
        enabled: true,
        settle: input.settle.map_or(json!(2), |seconds| json!(seconds)),
        target,
    }
    .validate(catalog)?;
    let directory = directory(common)?;
    // The command validates any stored rules before it substitutes its own.
    rules(catalog, &directory)?;
    let minimum = input.once.then(|| rule.settle + Duration::from_secs(2));
    let ids = AtomicU64::new(1);
    let cancelled = AtomicBool::new(false);
    let stop_clock = AtomicBool::new(false);
    let started = Instant::now();
    let session = Session {
        catalog,
        common,
        profile: &profile,
        directory: &directory,
        timeout: input.request.timeout,
        cancelled: &cancelled,
        ids: &ids,
        active: None,
    };
    let mut jobs = Vec::new();
    let mut errors = Vec::new();
    let result = thread::scope(|scope| {
        scope.spawn(|| {
            while !stop_clock.load(Ordering::Relaxed) {
                if crate::INTERRUPTED.load(Ordering::Relaxed)
                    || input
                        .duration
                        .is_some_and(|duration| started.elapsed() >= duration)
                {
                    cancelled.store(true, Ordering::Relaxed);
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
        });
        let result = session.run(&[rule], minimum, input.retry, |value| {
            if value.is_string() {
                errors.push(value);
            } else {
                jobs.push(value);
            }
        });
        stop_clock.store(true, Ordering::Relaxed);
        result
    });
    if let Err(error) = result
        && !cancelled.load(Ordering::Relaxed)
    {
        errors.push(json!(error.to_string()));
    }
    Ok(summary(
        jobs,
        errors,
        crate::INTERRUPTED.load(Ordering::Relaxed),
    ))
}

pub fn common(input: &Watch) -> &Common {
    &input.request.common
}

pub(super) fn summary(jobs: Vec<Value>, errors: Vec<Value>, interrupted: bool) -> (Value, u8) {
    let ok = !interrupted
        && errors.is_empty()
        && jobs.iter().all(|job| {
            job["errors"].as_array().is_some_and(Vec::is_empty)
                && job["cancelled"] == false
                && job["timed_out"] == false
        });
    (
        json!({"ok": ok, "interrupted": interrupted, "jobs": jobs, "errors": errors}),
        if interrupted { 130 } else { u8::from(!ok) },
    )
}
