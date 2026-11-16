use super::{
    ActiveJobs, JobGuard, ProfileLock, Result, database, directory, history, new_id, now, pipe,
    record, signature, stamp, valid_id, watch,
};
use crate::{
    Catalog, Common, Convert, INTERRUPTED, Planned, execute, media, plan_inputs, preferences,
    resolved, selected_recipe, timeout, web,
};
use clap::Args;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Args)]
pub struct Serve {
    #[command(flatten)]
    pub common: Common,
    #[arg(long, default_value = "3600", value_parser = timeout)]
    pub(crate) timeout: Duration,
    #[arg(long, value_parser = timeout)]
    pub(crate) duration: Option<Duration>,
}

pub(crate) enum DesktopEvent {
    Settings,
    Open(Vec<PathBuf>),
    Feedback(String),
    Status(Value),
}

pub(crate) struct DesktopHost {
    pub(crate) events: mpsc::Sender<DesktopEvent>,
    pub(crate) stopping: Arc<AtomicBool>,
    pub(crate) submissions: Mutex<mpsc::Receiver<Submission>>,
}

pub(crate) struct Submission {
    pub(crate) paths: Vec<PathBuf>,
    pub(crate) web: Vec<web::Input>,
    pub(crate) recipe: Value,
}

#[derive(Args)]
pub struct Send {
    #[command(flatten)]
    pub common: Common,
    #[arg(long, group = "selection", required_unless_present_any = ["to", "preset"])]
    action: Option<String>,
    #[arg(long, group = "selection")]
    to: Option<String>,
    #[arg(long, group = "selection")]
    preset: Option<String>,
    #[arg(long)]
    output: Option<PathBuf>,
    #[arg(long)]
    request_id: Option<String>,
    #[arg(required = true, num_args = 1..)]
    paths: Vec<PathBuf>,
}

#[derive(Args)]
pub struct Cancel {
    #[command(flatten)]
    pub common: Common,
    pub job_id: Option<i64>,
    #[arg(long)]
    pub instance: String,
}

#[derive(Args)]
pub struct Stop {
    #[command(flatten)]
    pub common: Common,
    #[arg(long)]
    pub instance: String,
}

pub fn call(common: &Common, mut request: Value) -> Result<Value> {
    let directory = directory(common)?;
    request["version"] = json!(1);
    if request["id"].is_null() {
        request["id"] = json!(new_id());
    }
    Ok(pipe::call(&directory, &request)?)
}

pub fn send(catalog: &Catalog, args: &Send) -> Result<Value> {
    let request = Convert {
        common: args.common.clone(),
        action: args.action.clone(),
        to: args.to.clone(),
        preset: args.preset.clone(),
        output: args.output.clone(),
        timeout: Duration::from_secs(3600),
        paths: Vec::new(),
        recipe: None,
    };
    let paths = args
        .paths
        .iter()
        .map(|path| resolved(path))
        .collect::<std::io::Result<Vec<_>>>()?;
    let profile = preferences(catalog, &args.common)?;
    let disabled =
        if media::Tools::discover(profile["ffmpeg_path"].as_str().unwrap_or_default()).is_some() {
            Vec::new()
        } else {
            vec!["ffmpeg".to_owned()]
        };
    let recipe = selected_recipe(catalog, &request, &paths, &profile, &disabled)?;
    call(
        &args.common,
        json!({"command": "send", "id": args.request_id, "recipe": recipe, "paths": paths}),
    )
}

struct Queued<'a> {
    id: u64,
    plan: Planned<'a>,
    cancelled: Arc<AtomicBool>,
    guard: JobGuard,
}

struct Service<'a> {
    catalog: &'a Catalog,
    common: &'a Common,
    directory: &'a Path,
    timeout: Duration,
    ids: AtomicU64,
    active: Arc<ActiveJobs>,
    jobs: Mutex<Vec<Value>>,
    errors: Mutex<Vec<Value>>,
    queue: mpsc::SyncSender<Queued<'a>>,
    stopping: AtomicBool,
    watch_stop: AtomicBool,
    watch_running: AtomicBool,
    desktop: Option<&'a DesktopHost>,
}

impl<'a> Service<'a> {
    fn handle(&self, request: &Value, instance: &str) -> Result<Value> {
        if self.stopping.load(Ordering::Relaxed) {
            return Err("프로그램이 종료 중입니다.".into());
        }
        let command = request["command"].as_str().unwrap_or_default();
        match command {
            "status" => return Ok(self.status()),
            "cancel" | "stop" => {
                if request["instance"] != instance {
                    return Err(
                        "앱이 재시작되었습니다. status의 새 인스턴스 ID를 사용하세요.".into(),
                    );
                }
                if command == "stop" {
                    self.stopping.store(true, Ordering::Relaxed);
                    self.cancel_all();
                    return Ok(json!({"stopping": true}));
                }
                if request["job_id"].is_null() {
                    self.cancel_all();
                    return Ok(json!({"cancelled": true}));
                }
                let id = request["job_id"]
                    .as_u64()
                    .filter(|&id| id > 0)
                    .ok_or("작업 ID가 올바르지 않습니다.")?;
                return Ok(json!({"cancelled": self.active.cancel(id)}));
            }
            "settings" => {
                let desktop = self.desktop.ok_or("이 인스턴스에는 설정 창이 없습니다.")?;
                desktop.events.send(DesktopEvent::Settings)?;
                return Ok(json!({}));
            }
            _ => (),
        }
        let paths = request["paths"]
            .as_array()
            .filter(|paths| (1..=256).contains(&paths.len()))
            .ok_or("절대 파일 경로 1~256개를 지정하세요.")?;
        let paths: Vec<_> = paths
            .iter()
            .map(|path| {
                path.as_str()
                    .filter(|path| path.chars().count() <= 4096 && Path::new(path).is_absolute())
                    .map(PathBuf::from)
                    .ok_or("절대 파일 경로 1~256개를 지정하세요.")
            })
            .collect::<std::result::Result<_, _>>()?;
        match command {
            "open" => {
                let desktop = self
                    .desktop
                    .ok_or("이 인스턴스에는 변환 선택 창이 없습니다.")?;
                let count = paths.len();
                desktop.events.send(DesktopEvent::Open(paths))?;
                Ok(json!({"files": count}))
            }
            "send" => {
                if matches!(
                    request["recipe"]["action_key"].as_str(),
                    Some("copy_path" | "terminal")
                ) {
                    return Err("IPC에는 파일 변환 작업을 지정하세요.".into());
                }
                let id = self.submit(Submission {
                    paths,
                    web: Vec::new(),
                    recipe: request["recipe"].clone(),
                })?;
                Ok(json!({"job_id": id}))
            }
            _ => Err("지원하지 않는 IPC 명령입니다.".into()),
        }
    }

    fn submit(&self, submission: Submission) -> Result<u64> {
        let input = Convert {
            common: self.common.clone(),
            action: None,
            to: None,
            preset: None,
            output: None,
            timeout: self.timeout,
            paths: submission.paths,
            recipe: Some(submission.recipe),
        };
        let planned = plan_inputs(self.catalog, &input, submission.web)?;
        let id = self.ids.fetch_add(1, Ordering::Relaxed);
        let cancelled = Arc::new(AtomicBool::new(false));
        let guard = self.active.register(id, &planned, cancelled.clone());
        if self.stopping.load(Ordering::Relaxed) {
            return Err("프로그램이 종료 중입니다.".into());
        }
        self.queue
            .try_send(Queued {
                id,
                plan: planned,
                cancelled,
                guard,
            })
            .map_err(|_| "대기 중인 변환 작업이 너무 많습니다.")?;
        Ok(id)
    }

    fn status(&self) -> Value {
        let jobs = self.active.snapshots();
        let recent = history(self.directory).unwrap_or_default();
        let offset = recent.len().saturating_sub(20);
        let recent: Vec<_> = recent[offset..].iter().map(|entry| json!({
            "id": entry["id"], "action": entry["action_key"], "files": entry["files"].as_array().map_or(0, Vec::len),
        })).collect();
        json!({"jobs": jobs, "watch_running": self.watch_running.load(Ordering::Relaxed), "recent": recent})
    }

    fn cancel_all(&self) {
        self.watch_stop.store(true, Ordering::Relaxed);
        self.watch_running.store(false, Ordering::Relaxed);
        self.active.cancel_all();
    }

    fn work(&self, receiver: &Mutex<mpsc::Receiver<Queued<'a>>>) {
        loop {
            let item = receiver
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_millis(50));
            let Queued {
                id,
                plan,
                cancelled,
                guard,
            } = match item {
                Ok(item) => item,
                Err(mpsc::RecvTimeoutError::Timeout) if !self.stopping.load(Ordering::Relaxed) => {
                    continue;
                }
                Err(_) => break,
            };
            if self.stopping.load(Ordering::Relaxed) {
                cancelled.store(true, Ordering::Relaxed);
            }
            let recipe = plan.recipe.clone();
            let started = stamp();
            let clock = Instant::now();
            let (report, _) = execute(self.catalog, plan, &cancelled, id, |index, state| {
                guard.update(index, state);
            });
            let job = report["jobs"][0].clone();
            let recorded = preferences(self.catalog, self.common)
                .map_err(super::Error::from)
                .and_then(|profile| {
                    record(
                        self.directory,
                        &profile,
                        self.catalog,
                        &job,
                        &recipe,
                        &started,
                        clock.elapsed(),
                    )
                });
            if let Err(error) = recorded {
                self.errors.lock().unwrap().push(json!(error.to_string()));
            }
            self.jobs.lock().unwrap().push(job);
        }
    }
}

struct Receipts {
    db: Connection,
    instance: String,
}

impl Receipts {
    fn open(directory: &Path) -> Result<Self> {
        let db = database(
            &directory.join("ipc-receipts.sqlite3"),
            "CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY,digest TEXT NOT NULL,instance TEXT NOT NULL,response TEXT,created REAL NOT NULL)",
        )?;
        Ok(Self {
            db,
            instance: new_id(),
        })
    }

    fn dispatch(&self, service: &Service<'_>, request: &Value) -> Result<Value> {
        if request["version"] != 1 {
            return Err("IPC 프로토콜 버전이 올바르지 않습니다.".into());
        }
        let id = request["id"]
            .as_str()
            .filter(|id| valid_id(id))
            .ok_or("IPC 요청 ID가 올바르지 않습니다.")?;
        let digest = signature(request);
        let previous = self
            .db
            .query_row(
                "SELECT digest,instance,response FROM receipts WHERE id=?",
                [id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()?;
        if let Some((old_digest, instance, response)) = previous {
            if old_digest != digest {
                return Err("동일한 IPC 요청 ID에 다른 내용이 전달되었습니다.".into());
            }
            let mut response: Value = if let Some(response) = response {
                serde_json::from_str(&response)?
            } else {
                json!({"ok": false, "error": "이전 요청이 중단됐습니다. 상태를 확인한 후 새 ID로 재시도하세요."})
            };
            response["replayed"] = json!(true);
            response["previous_instance"] = json!(instance != self.instance);
            return Ok(response);
        }
        self.db.execute(
            "INSERT INTO receipts VALUES (?,?,?,?,?)",
            params![id, digest, self.instance, Option::<&str>::None, now()],
        )?;
        let mut response = match service.handle(request, &self.instance) {
            Ok(mut value) => {
                value["ok"] = json!(true);
                value
            }
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        response["instance"] = json!(self.instance);
        self.db.execute(
            "UPDATE receipts SET response=? WHERE id=?",
            params![response.to_string(), id],
        )?;
        self.db.execute("DELETE FROM receipts WHERE id IN (SELECT id FROM receipts ORDER BY created DESC LIMIT -1 OFFSET 10000)", [])?;
        Ok(response)
    }
}

pub fn serve(catalog: &Catalog, input: &Serve) -> Result<(Value, u8)> {
    serve_with(catalog, input, None)
}

pub(crate) fn serve_with(
    catalog: &Catalog,
    input: &Serve,
    desktop: Option<&DesktopHost>,
) -> Result<(Value, u8)> {
    super::ctrlc()?;
    let directory = directory(&input.common)?;
    let _lock = ProfileLock::acquire(directory.join("magic_converter.lock"), Duration::ZERO)
        .map_err(|_| "이 설정 폴더의 인스턴스가 이미 실행 중입니다.")?;
    let profile = preferences(catalog, &input.common)?;
    let rules = watch::rules(catalog, &directory)?;
    let watching = watch::running_rules(&rules);
    let receipts = Mutex::new(Receipts::open(&directory)?);
    let (queue, receiver) = mpsc::sync_channel(100);
    let receiver = Mutex::new(receiver);
    let service = Service {
        catalog,
        common: &input.common,
        directory: &directory,
        timeout: input.timeout,
        ids: AtomicU64::new(1),
        active: Arc::new(ActiveJobs::default()),
        jobs: Mutex::new(Vec::new()),
        errors: Mutex::new(Vec::new()),
        queue,
        stopping: AtomicBool::new(false),
        watch_stop: AtomicBool::new(false),
        watch_running: AtomicBool::new(watching),
        desktop,
    };
    thread::scope(|scope| {
        for _ in 0..2 {
            scope.spawn(|| service.work(&receiver));
        }
        if watching {
            scope.spawn(|| {
                let watch = watch::Session {
                    catalog,
                    common: &input.common,
                    profile: &profile,
                    directory: &directory,
                    timeout: input.timeout,
                    cancelled: &service.watch_stop,
                    ids: &service.ids,
                    active: Some(&service.active),
                };
                let result = watch.run(&rules, None, false, |value| {
                    if value.is_string() {
                        service.errors.lock().unwrap().push(value);
                    } else {
                        service.jobs.lock().unwrap().push(value);
                    }
                });
                if let Err(error) = result
                    && !service.watch_stop.load(Ordering::Relaxed)
                {
                    service
                        .errors
                        .lock()
                        .unwrap()
                        .push(json!(error.to_string()));
                }
                service.watch_running.store(false, Ordering::Relaxed);
            });
        }
        scope.spawn(|| {
            let started = Instant::now();
            while !service.stopping.load(Ordering::Relaxed) {
                if INTERRUPTED.load(Ordering::Relaxed)
                    || desktop.is_some_and(|host| host.stopping.load(Ordering::Relaxed))
                    || input
                        .duration
                        .is_some_and(|duration| started.elapsed() >= duration)
                {
                    service.stopping.store(true, Ordering::Relaxed);
                    service.cancel_all();
                    break;
                }
                if let Some(host) = desktop
                    && let Ok(submission) = host.submissions.lock().unwrap().try_recv()
                {
                    let message = match service.submit(submission) {
                        Ok(id) => format!("변환 작업 {id}번이 대기 중입니다."),
                        Err(error) => error.to_string(),
                    };
                    let _ = host.events.send(DesktopEvent::Feedback(message));
                }
                thread::sleep(Duration::from_millis(20));
            }
        });
        let result = pipe::serve(&directory, &service.stopping, |request| {
            receipts
                .lock()
                .unwrap()
                .dispatch(&service, &request)
                .unwrap_or_else(|error| json!({"ok": false, "error": error.to_string()}))
        });
        service.stopping.store(true, Ordering::Relaxed);
        service.cancel_all();
        result
    })?;
    if let Some(desktop) = desktop {
        desktop.stopping.store(true, Ordering::Relaxed);
    }
    Ok(watch::summary(
        service.jobs.into_inner().unwrap(),
        service.errors.into_inner().unwrap(),
        INTERRUPTED.load(Ordering::Relaxed),
    ))
}
