use crate::{Catalog, Common, Convert, automation, timeout};
use clap::Parser;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{HashMap, hash_map::Entry};
use std::io::{self, BufRead, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, mpsc};

const PROTOCOLS: [&str; 2] = ["2025-11-25", "2025-06-18"];
const MESSAGE_LIMIT: usize = 2 * 1024 * 1024;

#[derive(Parser)]
#[command(name = "magic_converter-mcp", version)]
struct Cli {
    #[arg(long)]
    data_dir: Option<PathBuf>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Arguments {
    paths: Vec<PathBuf>,
    action: Option<String>,
    to: Option<String>,
    preset: Option<String>,
    output: Option<PathBuf>,
    timeout: Option<f64>,
}

impl Arguments {
    fn convert(self, common: Common) -> Result<Convert, String> {
        if self.paths.is_empty() || self.paths.len() > 1000 {
            return Err("변환할 파일을 1~1,000개 지정하세요.".into());
        }
        if [self.action.as_ref(), self.to.as_ref(), self.preset.as_ref()]
            .into_iter()
            .flatten()
            .count()
            != 1
        {
            return Err("action, to, preset 중 하나를 지정하세요.".into());
        }
        let deadline = timeout(&self.timeout.unwrap_or(3600.0).to_string())?;
        Ok(Convert {
            common,
            action: self.action,
            to: self.to,
            preset: self.preset,
            output: self.output,
            timeout: deadline,
            paths: self.paths,
            recipe: None,
        })
    }
}

fn tools() -> Value {
    json!({"tools": [{
        "name": "convert_files",
        "description": "Convert local files with MagicConverter. Use the CLI action key, a target extension, or an existing preset. Returns the conversion receipt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "minItems": 1, "maxItems": 1000, "items": {"type": "string"}},
                "action": {"type": "string"},
                "to": {"type": "string"},
                "preset": {"type": "string"},
                "output": {"type": "string"},
                "timeout": {"type": "number", "exclusiveMinimum": 0}
            },
            "required": ["paths"],
            "oneOf": [{"required": ["action"]}, {"required": ["to"]}, {"required": ["preset"]}],
            "additionalProperties": false
        }
    }]})
}

fn answer(id: Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn error(id: Value, code: i32, message: impl Into<String>) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message.into()}})
}

fn send(message: &Value) -> io::Result<()> {
    let mut stdout = io::stdout().lock();
    serde_json::to_writer(&mut stdout, message)?;
    stdout.write_all(b"\n")?;
    stdout.flush()
}

struct Job {
    id: Value,
    conversion: Convert,
    cancelled: Arc<AtomicBool>,
}

type Requests = Arc<Mutex<HashMap<String, Arc<AtomicBool>>>>;

fn requests() -> Requests {
    Arc::new(Mutex::new(HashMap::new()))
}

fn work(catalog: &Catalog, incoming: mpsc::Receiver<Job>, requests: Requests) -> io::Result<()> {
    for job in incoming {
        let cancelled = || job.cancelled.load(Ordering::Acquire);
        let result = if cancelled() {
            None
        } else {
            let (receipt, code) =
                automation::convert_with(catalog, &job.conversion, &job.cancelled).unwrap_or_else(
                    |failure| (json!({"ok": false, "error": failure.to_string()}), 1),
                );
            Some(json!({
                "isError": code != 0,
                "structuredContent": receipt,
                "content": [{"type": "text", "text": receipt.to_string()}],
            }))
        };
        let mut active = requests.lock().unwrap();
        active.remove(&job.id.to_string());
        if !cancelled()
            && let Some(result) = result
        {
            send(&answer(job.id, result))?;
        }
    }
    Ok(())
}

fn receive(
    mut input: impl BufRead,
    common: Common,
    queue: mpsc::SyncSender<Job>,
    requests: &Requests,
) -> io::Result<()> {
    let mut initialized = false;
    let mut first = true;
    loop {
        let mut line = Vec::new();
        let length = (&mut input)
            .take(MESSAGE_LIMIT as u64 + 1)
            .read_until(b'\n', &mut line)?;
        if length == 0 {
            return Ok(());
        }
        if length > MESSAGE_LIMIT {
            return send(&error(
                Value::Null,
                -32700,
                "MCP message exceeds the input limit.",
            ));
        }
        let bytes = if std::mem::take(&mut first) {
            line.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&line)
        } else {
            &line
        };
        let message: Value = match serde_json::from_slice(bytes) {
            Ok(message) => message,
            Err(_) => {
                send(&error(Value::Null, -32700, "Invalid JSON."))?;
                continue;
            }
        };
        let id = message.get("id").cloned();
        let method = message["method"].as_str();
        if message["jsonrpc"] != "2.0"
            || method.is_none()
            || id
                .as_ref()
                .is_some_and(|id| !id.is_string() && !id.is_i64() && !id.is_u64())
        {
            send(&error(Value::Null, -32600, "Invalid JSON-RPC request."))?;
            continue;
        }
        let params = &message["params"];
        let method = method.unwrap();
        let Some(id) = id else {
            if method == "notifications/cancelled"
                && let Some(flag) = requests
                    .lock()
                    .unwrap()
                    .get(&params["requestId"].to_string())
            {
                flag.store(true, Ordering::Release);
            }
            continue;
        };
        let response = match method {
            "initialize" if !initialized => {
                let Some(requested) = params["protocolVersion"].as_str() else {
                    send(&error(id, -32602, "protocolVersion is required."))?;
                    continue;
                };
                let version = PROTOCOLS
                    .iter()
                    .copied()
                    .find(|version| *version == requested)
                    .unwrap_or(PROTOCOLS[0]);
                initialized = true;
                answer(
                    id,
                    json!({"protocolVersion": version, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "MagicConverter", "version": env!("CARGO_PKG_VERSION")}}),
                )
            }
            "ping" => answer(id, json!({})),
            _ if !initialized => error(id, -32002, "Initialize the MCP session first."),
            "tools/list" => answer(id, tools()),
            "tools/call" if params["name"] == "convert_files" => {
                let conversion = serde_json::from_value::<Arguments>(params["arguments"].clone())
                    .map_err(|error| error.to_string())
                    .and_then(|arguments| arguments.convert(common.clone()));
                let conversion = match conversion {
                    Ok(conversion) => conversion,
                    Err(message) => {
                        send(&error(id, -32602, message))?;
                        continue;
                    }
                };
                let key = id.to_string();
                let mut active = requests.lock().unwrap();
                if let Entry::Vacant(entry) = active.entry(key) {
                    let cancelled = Arc::new(AtomicBool::new(false));
                    if queue
                        .try_send(Job {
                            id: id.clone(),
                            conversion,
                            cancelled: cancelled.clone(),
                        })
                        .is_ok()
                    {
                        entry.insert(cancelled);
                        continue;
                    }
                    error(id, -32000, "The conversion queue is full.")
                } else {
                    error(id, -32600, "Request ID is already in use.")
                }
            }
            "tools/call" => error(id, -32602, "Unknown MagicConverter tool."),
            _ => error(id, -32601, "Unknown MCP method."),
        };
        send(&response)?;
    }
}

fn run(cli: Cli) -> io::Result<()> {
    let catalog = Catalog::load().map_err(io::Error::other)?;
    let common = Common {
        data_dir: cli.data_dir,
        ..Default::default()
    };
    let requests = requests();
    let (queue, incoming) = mpsc::sync_channel(16);
    std::thread::scope(|thread| {
        let worker = thread.spawn(|| work(&catalog, incoming, requests.clone()));
        let read = receive(io::stdin().lock(), common, queue, &requests);
        for active in requests.lock().unwrap().values() {
            active.store(true, Ordering::Release);
        }
        let write = worker
            .join()
            .map_err(|_| io::Error::other("MCP conversion worker panicked."))?;
        read.and(write)
    })
}

pub fn entry(arguments: impl IntoIterator<Item = std::ffi::OsString>) -> ExitCode {
    match run(Cli::parse_from(arguments)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
