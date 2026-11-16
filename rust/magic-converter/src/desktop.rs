mod form;
mod ole;
mod presets;
mod quick;
mod settings;
pub mod shell;
mod wheel;

use crate::{
    Catalog, Common, Convert, INTERRUPTED, automation, media, preferences, resolved,
    selected_recipe, supports_steps, web, write_report,
};
use automation::ipc::{DesktopEvent, DesktopHost, Serve, Submission};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::env;
use std::ffi::{OsStr, OsString};
use std::io;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::PathBuf;
use std::ptr::{null, null_mut};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender, SyncSender};
use std::thread;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::UI::Controls::Dialogs::*;
use windows_sys::Win32::UI::HiDpi::*;
use windows_sys::Win32::UI::Shell::*;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

const TRAY: u32 = WM_APP + 3;
const INPUTS: u16 = 101;
const PICK: u16 = 102;
const ACTION: u16 = 103;
const OUTPUT: u16 = 104;
const START: u16 = 105;
const JOBS: u16 = 106;
const HISTORY: u16 = 107;
const ENABLED: u16 = 108;
const RELEASE: u16 = 109;
const SAVE: u16 = 110;
const QUIT: u16 = 111;
const CANCEL: u16 = 112;
const SHOW: u16 = 113;
const FORMATS: u16 = 114;
const HELP: u16 = 115;
const STATUS: u16 = 116;
const CONVERT_PROFILE: u16 = 117;
const ACTION_PROFILE: u16 = 118;
const PRESETS: u16 = 119;
const SETTINGS: u16 = 120;

pub fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain([0]).collect()
}

struct Ui {
    catalog: Catalog,
    common: Common,
    profile: Value,
    paths: Vec<PathBuf>,
    web: Vec<web::Input>,
    actions: Vec<Selection>,
    wheel_profiles: [Vec<String>; 2],
    main: HWND,
    wheel: wheel::Wheel,
    events: Receiver<DesktopEvent>,
    sender: Sender<DesktopEvent>,
    submissions: SyncSender<Submission>,
    stopping: Arc<AtomicBool>,
    status_inflight: bool,
    status: Value,
    last_status: Instant,
    icon: HICON,
    tray_added: bool,
}

enum Selection {
    Action(String),
    Preset(String),
}

fn control(window: HWND, id: u16) -> HWND {
    unsafe { GetDlgItem(window, i32::from(id)) }
}

fn text(window: HWND, value: &str) {
    unsafe { SetWindowTextW(window, wide(value).as_ptr()) };
}

fn read_text(window: HWND) -> String {
    let length = unsafe { GetWindowTextLengthW(window) }.clamp(0, 32_767);
    let mut result = vec![0_u16; length as usize + 1];
    let count = unsafe { GetWindowTextW(window, result.as_mut_ptr(), length + 1) };
    String::from_utf16_lossy(&result[..count as usize])
}

fn msg(window: HWND, message: &str) {
    unsafe {
        MessageBoxW(
            window,
            wide(message).as_ptr(),
            wide("MagicConverter").as_ptr(),
            MB_OK | MB_ICONINFORMATION,
        )
    };
}

fn handle(id: u16) -> HMENU {
    usize::from(id) as HMENU
}

fn controls(window: HWND) {
    let normal = WS_CHILD | WS_VISIBLE;
    let tab = normal | WS_TABSTOP;
    let specs = [
        (
            "STATIC",
            concat!("MagicConverter ", env!("CARGO_PKG_VERSION")),
            normal,
            0,
            20,
            16,
            595,
            22,
        ),
        (
            "STATIC",
            "Shift+드래그로 변환 · Ctrl+드래그로 빠른 액션",
            normal,
            0,
            20,
            44,
            595,
            22,
        ),
        ("STATIC", "변환할 원본", normal, 0, 20, 82, 125, 21),
        ("BUTTON", "변환할 파일 고르기…", tab, PICK, 437, 74, 173, 27),
        (
            "LISTBOX",
            "",
            tab | WS_VSCROLL | LBS_NOINTEGRALHEIGHT as u32 | WS_BORDER,
            INPUTS,
            20,
            105,
            590,
            114,
        ),
        ("STATIC", "작업", normal, 0, 20, 232, 80, 22),
        (
            "COMBOBOX",
            "",
            tab | WS_VSCROLL | CBS_DROPDOWNLIST as u32,
            ACTION,
            104,
            228,
            506,
            260,
        ),
        ("STATIC", "출력 폴더", normal, 0, 20, 270, 84, 22),
        (
            "EDIT",
            "",
            tab | WS_BORDER | ES_AUTOHSCROLL as u32,
            OUTPUT,
            104,
            267,
            384,
            25,
        ),
        (
            "BUTTON",
            "변환 시작",
            tab | BS_DEFPUSHBUTTON as u32,
            START,
            497,
            265,
            113,
            29,
        ),
        (
            "BUTTON",
            "드래그 감지 사용",
            tab | BS_AUTOCHECKBOX as u32,
            ENABLED,
            20,
            313,
            145,
            23,
        ),
        (
            "BUTTON",
            "휠 안에서 보조키를 놓아야 실행",
            tab | BS_AUTOCHECKBOX as u32,
            RELEASE,
            174,
            313,
            252,
            23,
        ),
        ("BUTTON", "설정 저장", tab, SAVE, 497, 309, 113, 28),
        ("STATIC", "Shift 변환 휠", normal, 0, 20, 356, 95, 22),
        (
            "COMBOBOX",
            "",
            tab | WS_VSCROLL | CBS_DROPDOWNLIST as u32,
            CONVERT_PROFILE,
            118,
            352,
            180,
            145,
        ),
        ("STATIC", "Ctrl 액션 휠", normal, 0, 327, 356, 95, 22),
        (
            "COMBOBOX",
            "",
            tab | WS_VSCROLL | CBS_DROPDOWNLIST as u32,
            ACTION_PROFILE,
            427,
            352,
            183,
            145,
        ),
        ("STATIC", "대기 중", normal, STATUS, 20, 394, 455, 22),
        ("BUTTON", "프리셋 편집…", tab, PRESETS, 497, 388, 113, 29),
        ("BUTTON", "작업 관리…", tab, JOBS, 20, 425, 112, 29),
        ("BUTTON", "결과 기록…", tab, HISTORY, 144, 425, 112, 29),
        ("BUTTON", "모두 취소", tab, CANCEL, 268, 425, 112, 29),
        ("BUTTON", "고급 설정…", tab, SETTINGS, 382, 425, 108, 29),
        ("BUTTON", "프로그램 종료", tab, QUIT, 497, 425, 113, 29),
    ];
    form::install(window, (630, 474), &specs, general_help);
}

fn general_help(id: u16, label: &str) -> &'static str {
    match (id, label) {
        (INPUTS | PICK, _) | (0, "변환할 원본") => {
            "이번 작업에 사용할 원본 파일 목록. 여러 파일 선택 또는 끌어다 놓기 가능"
        }
        (ACTION, _) | (0, "작업") => "선택한 원본에 적용할 변환 형식, 도구 또는 프리셋",
        (OUTPUT, _) | (0, "출력 폴더") => "결과를 저장할 폴더. 빈칸이면 원본 파일의 폴더",
        (START, _) => "표시된 파일에 선택한 작업 실행",
        (ENABLED, _) => "파일을 끌면서 Shift 또는 Ctrl을 눌렀을 때 휠 표시 여부",
        (RELEASE, _) => "휠 안에서 Shift 또는 Ctrl을 먼저 놓았을 때만 작업 실행",
        (SAVE, _) => "현재 일반 설정을 다음 실행에도 사용할 기본값으로 저장",
        (CONVERT_PROFILE, _) | (0, "Shift 변환 휠") => {
            "Shift로 열 변환 목록. 자동은 원본에 가능한 변환 형식 표시"
        }
        (ACTION_PROFILE, _) | (0, "Ctrl 액션 휠") => {
            "Ctrl로 열 도구 목록. 자동은 원본에 사용할 수 있는 빠른 도구 표시"
        }
        (PRESETS, _) => "자주 쓰는 작업과 휠에 표시할 항목의 편집",
        (JOBS, _) => "진행 중이거나 대기 중인 작업의 확인",
        (HISTORY, _) => "최근 작업 결과와 저장 위치의 확인",
        (CANCEL, _) => "진행 중이거나 대기 중인 작업의 취소",
        (SETTINGS, _) => "드래그 감도, 결과 이름, 저장 위치와 기록 보관 수의 설정",
        (QUIT, _) => "트레이를 포함한 MagicConverter 종료",
        _ => "",
    }
}

impl Ui {
    const WHEEL_SELECTORS: [(u16, &'static str, &'static str); 2] = [
        (CONVERT_PROFILE, "convert_profile", "convert"),
        (ACTION_PROFILE, "action_profile", "action"),
    ];
    fn show(&self) {
        unsafe {
            ShowWindow(self.main, SW_RESTORE);
            SetForegroundWindow(self.main);
        }
    }

    fn update_settings(&mut self) {
        for (id, field, fallback) in [
            (ENABLED, "enabled", true),
            (RELEASE, "release_modifier_inside", false),
        ] {
            let checked = self.profile[field].as_bool().unwrap_or(fallback);
            unsafe { SendMessageW(control(self.main, id), BM_SETCHECK, usize::from(checked), 0) };
        }
        text(
            control(self.main, OUTPUT),
            self.profile["output"]["directory"]
                .as_str()
                .unwrap_or_default(),
        );
        for (index, (id, field, mode)) in Self::WHEEL_SELECTORS.into_iter().enumerate() {
            let combo = control(self.main, id);
            let mut choices = vec![String::new()];
            let mut labels = vec!["자동"];
            for profile in self.profile["profiles"].as_array().into_iter().flatten() {
                if profile["mode"] == mode
                    && let (Some(id), Some(name)) =
                        (profile["id"].as_str(), profile["name"].as_str())
                {
                    choices.push(id.to_owned());
                    labels.push(name);
                }
            }
            let selected = choices
                .iter()
                .position(|id| self.profile[field].as_str() == Some(id))
                .unwrap_or(0);
            unsafe { SendMessageW(combo, CB_RESETCONTENT, 0, 0) };
            for label in labels {
                unsafe { SendMessageW(combo, CB_ADDSTRING, 0, wide(label).as_ptr() as isize) };
            }
            unsafe { SendMessageW(combo, CB_SETCURSEL, selected, 0) };
            self.wheel_profiles[index] = choices;
        }
    }

    fn save(&mut self) -> automation::Result<()> {
        let profile = self.draft_profile()?;
        self.save_profile(profile)?;
        text(control(self.main, STATUS), "설정을 저장했습니다.");
        Ok(())
    }

    fn draft_profile(&self) -> automation::Result<Value> {
        let mut profile = self.profile.clone();
        for (id, field) in [(ENABLED, "enabled"), (RELEASE, "release_modifier_inside")] {
            let state = unsafe { SendMessageW(control(self.main, id), BM_GETCHECK, 0, 0) };
            profile[field] = json!(state != 0);
        }
        for (index, (id, field, _)) in Self::WHEEL_SELECTORS.into_iter().enumerate() {
            let choice = unsafe { SendMessageW(control(self.main, id), CB_GETCURSEL, 0, 0) };
            if let Some(selected) = self.wheel_profiles[index].get(choice as usize) {
                profile[field] = json!(selected);
            }
        }
        let directory = read_text(control(self.main, OUTPUT));
        if directory.trim().is_empty() {
            profile["output"]["location"] = json!("source");
            profile["output"]["directory"] = json!("");
        } else {
            profile["output"]["location"] = json!("folder");
            profile["output"]["directory"] = json!(resolved(&PathBuf::from(directory))?);
        }
        Ok(profile)
    }

    fn save_profile(&mut self, profile: Value) -> automation::Result<()> {
        let path = automation::directory(&self.common)?.join("settings.json");
        if path.is_file() {
            let current = automation::read_json(&path, 2 * 1024 * 1024)?;
            if !matches!(current["version"].as_u64(), Some(1..=3)) {
                return Err("지원하지 않는 버전의 설정 파일을 보존했습니다.".into());
            }
            if current != profile {
                write_report(&path.with_file_name("settings.json.bak"), &current)?;
            }
        }
        write_report(&path, &profile)?;
        self.profile = profile;
        self.update_interval();
        Ok(())
    }

    fn update_interval(&self) {
        let interval = self.profile["interval_ms"]
            .as_u64()
            .unwrap_or(35)
            .clamp(30, 50) as u32;
        unsafe { SetTimer(self.main, 1, interval, None) };
    }

    fn available(&self, paths: &[PathBuf], key: &str, disabled: &[String]) -> bool {
        if quick::is_local(key) {
            return !paths.is_empty() && paths.iter().all(|path| path.is_file() || path.is_dir());
        }
        !paths.is_empty()
            && paths.iter().all(|path| {
                self.catalog
                    .assess(path, key, disabled)
                    .route
                    .is_some_and(|route| supports_steps(&route.steps, Some(path)))
            })
    }

    fn disabled(&self) -> Vec<String> {
        if media::Tools::discover(self.profile["ffmpeg_path"].as_str().unwrap_or_default())
            .is_none()
        {
            vec!["ffmpeg".to_owned()]
        } else {
            Vec::new()
        }
    }

    fn set_inputs(&mut self, paths: Vec<PathBuf>) {
        self.set_selection(paths, Vec::new());
    }

    fn set_selection(&mut self, paths: Vec<PathBuf>, web: Vec<web::Input>) {
        self.web = web;
        let mut seen = std::collections::HashSet::new();
        self.paths = if self.web.is_empty() {
            paths
                .iter()
                .filter_map(|path| resolved(path).ok())
                .filter(|path| seen.insert(path.to_string_lossy().to_lowercase()))
                .take(256)
                .collect()
        } else {
            self.web.iter().map(web::Input::placeholder).collect()
        };
        let list = control(self.main, INPUTS);
        let actions = control(self.main, ACTION);
        unsafe {
            SendMessageW(list, LB_RESETCONTENT, 0, 0);
            SendMessageW(actions, CB_RESETCONTENT, 0, 0);
        }
        for path in &self.paths {
            unsafe { SendMessageW(list, LB_ADDSTRING, 0, wide(path).as_ptr() as isize) };
        }
        self.actions.clear();
        let disabled = self.disabled();
        for action in &self.catalog.actions {
            if (self.web.is_empty()
                || (action.key != "tool:pdf_merge" && !quick::is_local(&action.key)))
                && self.available(&self.paths, &action.key, &disabled)
            {
                unsafe {
                    SendMessageW(
                        actions,
                        CB_ADDSTRING,
                        0,
                        wide(&action.label).as_ptr() as isize,
                    )
                };
                self.actions.push(Selection::Action(action.key.clone()));
            }
        }
        for preset in self.profile["presets"].as_array().into_iter().flatten() {
            if let (Some(key), Some(name), Some(id)) = (
                preset["action_key"].as_str(),
                preset["name"].as_str(),
                preset["id"].as_str(),
            ) && (self.web.is_empty() || (key != "tool:pdf_merge" && !quick::is_local(key)))
                && self.available(&self.paths, key, &disabled)
            {
                let label = wide(format!("프리셋 · {name}"));
                unsafe { SendMessageW(actions, CB_ADDSTRING, 0, label.as_ptr() as isize) };
                self.actions.push(Selection::Preset(id.to_owned()));
            }
        }
        if !self.actions.is_empty() {
            unsafe { SendMessageW(actions, CB_SETCURSEL, 0, 0) };
        }
        unsafe {
            windows_sys::Win32::UI::Input::KeyboardAndMouse::EnableWindow(
                control(self.main, START),
                i32::from(!self.actions.is_empty()),
            )
        };
        if !self.paths.is_empty() {
            text(
                control(self.main, STATUS),
                if self.actions.is_empty() {
                    "모든 입력에 사용할 수 있는 작업이 없습니다."
                } else {
                    "실행할 작업을 선택하세요."
                },
            );
        }
        self.show();
    }

    fn select(&self) -> Vec<PathBuf> {
        let mut buffer = vec![0_u16; 32_768];
        let filter = wide("모든 파일\0*.*\0");
        let mut dialog = OPENFILENAMEW {
            lStructSize: size_of::<OPENFILENAMEW>() as u32,
            hwndOwner: self.main,
            lpstrFilter: filter.as_ptr(),
            lpstrFile: buffer.as_mut_ptr(),
            nMaxFile: buffer.len() as u32,
            Flags: OFN_EXPLORER
                | OFN_ALLOWMULTISELECT
                | OFN_FILEMUSTEXIST
                | OFN_PATHMUSTEXIST
                | OFN_NOCHANGEDIR,
            ..Default::default()
        };
        if unsafe { GetOpenFileNameW(&mut dialog) } == 0 {
            return Vec::new();
        }
        let mut paths = buffer
            .split(|&character| character == 0)
            .take_while(|path| !path.is_empty())
            .map(|path| PathBuf::from(OsString::from_wide(path)));
        let Some(first) = paths.next() else {
            return Vec::new();
        };
        let relative: Vec<_> = paths.collect();
        if relative.is_empty() {
            vec![first]
        } else {
            relative.into_iter().map(|file| first.join(file)).collect()
        }
    }

    fn submit(&self, paths: Vec<PathBuf>, web: Vec<web::Input>, mut recipe: Value) -> bool {
        let action = recipe["action_key"].as_str().unwrap_or_default();
        if quick::is_local(action) {
            return match quick::perform(self.main, action, &paths, web.is_empty()) {
                Ok(message) => {
                    text(control(self.main, STATUS), &message);
                    true
                }
                Err(error) => {
                    self.drop_error(&error);
                    false
                }
            };
        }
        if !web.is_empty() && recipe["output"]["location"] == "source" {
            if let Ok(directory) = automation::directory(&self.common)
                && let Ok(settings) =
                    automation::read_json(&directory.join("automation.json"), 2 * 1024 * 1024)
                && let Some(folder) = settings["web_output"]
                    .as_str()
                    .filter(|value| std::path::Path::new(value).is_absolute())
            {
                recipe["output"]["location"] = json!("folder");
                recipe["output"]["directory"] = json!(folder);
            }
        }
        let sent = self
            .submissions
            .try_send(Submission { paths, web, recipe })
            .is_ok();
        text(
            control(self.main, STATUS),
            if sent {
                "변환 작업을 요청했습니다."
            } else {
                "변환 작업을 시작하지 못했습니다."
            },
        );
        sent
    }

    fn drop_error(&self, error: &io::Error) {
        text(control(self.main, STATUS), &error.to_string());
    }

    fn start(&self) {
        let index = unsafe { SendMessageW(control(self.main, ACTION), CB_GETCURSEL, 0, 0) };
        let Some(action) = usize::try_from(index)
            .ok()
            .and_then(|index| self.actions.get(index))
        else {
            return;
        };
        let output = read_text(control(self.main, OUTPUT));
        let (action, preset) = match action {
            Selection::Action(key) => (Some(key.clone()), None),
            Selection::Preset(id) => (None, Some(id.clone())),
        };
        let input = Convert {
            common: self.common.clone(),
            action,
            to: None,
            preset,
            output: (!output.trim().is_empty()).then(|| PathBuf::from(output)),
            timeout: Duration::from_secs(3600),
            paths: self.paths.clone(),
            recipe: None,
        };
        match selected_recipe(
            &self.catalog,
            &input,
            &self.paths,
            &self.profile,
            &self.disabled(),
        ) {
            Ok(recipe) => {
                self.submit(self.paths.clone(), self.web.clone(), recipe);
            }
            Err(error) => msg(self.main, &error),
        }
    }

    fn summary(&self) {
        let jobs = self.status["jobs"]
            .as_array()
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        if jobs.is_empty() {
            msg(self.main, "진행하거나 대기 중인 작업이 없습니다.");
            return;
        }
        let mut text = String::new();
        for job in jobs {
            text.push_str(&format!(
                "{} · {}\n",
                job["job_id"],
                job["action"].as_str().unwrap_or("")
            ));
            for file in job["files"].as_array().into_iter().flatten() {
                text.push_str(&format!(
                    "  {} · {}\n",
                    file["name"].as_str().unwrap_or(""),
                    file["state"].as_str().unwrap_or("")
                ));
            }
        }
        msg(self.main, &text);
    }

    fn history(&self) {
        let history = automation::directory(&self.common)
            .map_err(Into::into)
            .and_then(|path| automation::history(&path));
        let Ok(history) = history else {
            msg(self.main, "결과 기록을 읽지 못했습니다.");
            return;
        };
        let mut summary = String::new();
        for entry in history.iter().rev().take(10) {
            summary.push_str(&format!("{}\n", entry["label"].as_str().unwrap_or("")));
            for file in entry["files"].as_array().into_iter().flatten().take(6) {
                let path = file["source"].as_str().unwrap_or("");
                summary.push_str(&format!(
                    "  {} · {}\n",
                    PathBuf::from(path)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy(),
                    file["state"].as_str().unwrap_or("")
                ));
            }
        }
        msg(
            self.main,
            if summary.is_empty() {
                "결과 기록이 없습니다."
            } else {
                &summary
            },
        );
    }

    fn command(&mut self, id: u16) {
        match id {
            PICK => {
                let paths = self.select();
                if !paths.is_empty() {
                    self.set_inputs(paths);
                }
            }
            START => self.start(),
            SAVE | ENABLED => {
                if let Err(error) = self.save() {
                    msg(self.main, &error.to_string());
                }
            }
            HISTORY => self.history(),
            PRESETS => {
                if let Err(error) = presets::open(self) {
                    msg(self.main, &error.to_string());
                }
            }
            SETTINGS => {
                if let Err(error) = settings::open(self) {
                    msg(self.main, &error.to_string());
                }
            }
            JOBS => self.summary(),
            CANCEL => {
                let common = self.common.clone();
                let sender = self.sender.clone();
                thread::spawn(move || {
                    let result = automation::ipc::call(&common, json!({"command": "status"})).and_then(|status|
                        automation::ipc::call(&common, json!({"command": "cancel", "instance": status["instance"], "job_id": null})));
                    let message = match result {
                        Ok(result) if result["ok"] == true => {
                            "작업 취소를 요청했습니다.".to_owned()
                        }
                        Ok(result) => result["error"]
                            .as_str()
                            .unwrap_or("작업을 취소하지 못했습니다.")
                            .to_owned(),
                        Err(error) => error.to_string(),
                    };
                    let _ = sender.send(DesktopEvent::Feedback(message));
                });
            }
            SHOW => self.show(),
            QUIT => {
                self.stopping.store(true, Ordering::Relaxed);
            }
            HELP => msg(
                self.main,
                "파일을 끌 때 Shift를 누르면 변환 휠, Ctrl을 누르면 작업 휠을 엽니다.\n\n휠의 작업 위로 이동한 뒤 파일을 놓으세요.",
            ),
            FORMATS => {
                let formats = self
                    .catalog
                    .formats
                    .iter()
                    .map(|format| format.extensions.join("/"))
                    .collect::<Vec<_>>()
                    .join(" · ");
                msg(self.main, &formats);
            }
            _ => (),
        }
    }

    fn notify(&self, command: NOTIFY_ICON_MESSAGE) -> bool {
        let mut data = NOTIFYICONDATAW {
            cbSize: size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: self.main,
            uID: 1,
            uFlags: NIF_ICON | NIF_TIP | NIF_MESSAGE,
            uCallbackMessage: TRAY,
            hIcon: self.icon,
            ..Default::default()
        };
        let tip = wide("MagicConverter · Shift 변환 / Ctrl 액션");
        data.szTip[..tip.len()].copy_from_slice(&tip);
        unsafe { Shell_NotifyIconW(command, &data) != 0 }
    }

    fn tray_menu(&mut self) {
        let menu = unsafe { CreatePopupMenu() };
        if menu.is_null() {
            return;
        }
        for (id, label) in [
            (SHOW, "실행 상태 / 설정…"),
            (PICK, "변환할 파일 고르기…"),
            (JOBS, "작업 관리…"),
            (HISTORY, "결과 기록…"),
            (HELP, "사용 방법"),
            (FORMATS, "지원 형식…"),
            (CANCEL, "진행/대기 변환 모두 취소"),
            (QUIT, "종료"),
        ] {
            unsafe { AppendMenuW(menu, MF_STRING, usize::from(id), wide(label).as_ptr()) };
        }
        let mut cursor = POINT::default();
        unsafe {
            GetCursorPos(&mut cursor);
            SetForegroundWindow(self.main);
        }
        let id = unsafe {
            TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_RIGHTBUTTON,
                cursor.x,
                cursor.y,
                0,
                self.main,
                null(),
            )
        };
        unsafe { DestroyMenu(menu) };
        if id != 0 {
            self.command(id as u16);
        }
    }

    fn tick(&mut self) {
        if self.stopping.load(Ordering::Relaxed) || INTERRUPTED.load(Ordering::Relaxed) {
            if self.tray_added {
                self.notify(NIM_DELETE);
                self.tray_added = false;
            }
            unsafe { PostQuitMessage(0) };
            return;
        }
        while let Ok(event) = self.events.try_recv() {
            match event {
                DesktopEvent::Settings => self.show(),
                DesktopEvent::Open(paths) => self.set_inputs(paths),
                DesktopEvent::Feedback(message) => text(control(self.main, STATUS), &message),
                DesktopEvent::Status(status) => {
                    self.status_inflight = false;
                    let count = status["jobs"].as_array().map_or(0, Vec::len);
                    let old = self.status["jobs"].as_array().map_or(0, Vec::len);
                    let last = |status: &Value| {
                        status["recent"]
                            .as_array()
                            .and_then(|entries| entries.last())
                            .and_then(|last| last["id"].as_str())
                            .map(str::to_owned)
                    };
                    let completed = self.status.is_object()
                        && last(&status).is_some()
                        && last(&status) != last(&self.status);
                    if count == 0 && completed {
                        text(
                            control(self.main, STATUS),
                            "작업이 끝났습니다. 결과 기록에서 상세 내용을 확인하세요.",
                        );
                    } else if count != old {
                        text(
                            control(self.main, STATUS),
                            &if count == 0 {
                                "대기 중".to_owned()
                            } else {
                                format!("변환 중 · 작업 {count}개")
                            },
                        );
                    }
                    self.status = status;
                }
            }
        }
        if !self.status_inflight && self.last_status.elapsed() >= Duration::from_secs(1) {
            self.last_status = Instant::now();
            self.status_inflight = true;
            let common = self.common.clone();
            let sender = self.sender.clone();
            thread::spawn(move || {
                let result = automation::ipc::call(&common, json!({"command": "status"}))
                    .unwrap_or(Value::Null);
                let _ = sender.send(DesktopEvent::Status(result));
            });
        }
        wheel::poll(self);
    }
}

unsafe extern "system" fn window_proc(
    window: HWND,
    message: u32,
    wp: WPARAM,
    lp: LPARAM,
) -> LRESULT {
    if let Some(result) = form::message(window, message, wp, lp) {
        return result;
    }
    if message == WM_NCCREATE {
        let create = unsafe { &*(lp as *const CREATESTRUCTW) };
        unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize) };
        return unsafe { DefWindowProcW(window, message, wp, lp) };
    }
    let pointer = unsafe { GetWindowLongPtrW(window, GWLP_USERDATA) } as *const RefCell<Ui>;
    if let Some(state) = unsafe { pointer.as_ref() }
        && let Ok(mut ui) = state.try_borrow_mut()
    {
        match message {
            WM_TIMER => {
                ui.tick();
                return 0;
            }
            WM_COMMAND => {
                ui.command((wp & 0xffff) as u16);
                return 0;
            }
            WM_CLOSE => {
                unsafe { ShowWindow(window, SW_HIDE) };
                return 0;
            }
            TRAY if lp as u32 == WM_RBUTTONUP || lp as u32 == WM_CONTEXTMENU => {
                ui.tray_menu();
                return 0;
            }
            TRAY if lp as u32 == WM_LBUTTONDBLCLK => {
                ui.show();
                return 0;
            }
            _ => (),
        }
    }
    unsafe { DefWindowProcW(window, message, wp, lp) }
}

fn load_icon() -> HICON {
    let path = env::current_exe().ok().and_then(|exe| {
        exe.parent()?
            .ancestors()
            .take(5)
            .map(|folder| folder.join("assets/magic_converter-tool.ico"))
            .find(|path| path.is_file())
    });
    if let Some(path) = path {
        let icon = unsafe {
            LoadImageW(
                null_mut(),
                wide(path).as_ptr(),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
        };
        if !icon.is_null() {
            return icon;
        }
    }
    unsafe { LoadIconW(null_mut(), IDI_APPLICATION) }
}

pub fn run(
    catalog: Catalog,
    common: Common,
    paths: Vec<PathBuf>,
    tray: bool,
) -> automation::Result<u8> {
    let directory = automation::directory(&common)?;
    let paths = paths
        .iter()
        .map(|path| resolved(path))
        .collect::<io::Result<Vec<_>>>()?;
    if directory.join("magic_converter.lock").exists() {
        let message = if paths.is_empty() {
            json!({"command": "settings"})
        } else {
            json!({"command": "open", "paths": paths})
        };
        if automation::ipc::call(&common, message).is_ok_and(|result| result["ok"] == true) {
            return Ok(0);
        }
    }
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    }
    let _ole = ole::Apartment::new()?;
    let (sender, receiver) = mpsc::channel();
    let (submissions, requests) = mpsc::sync_channel(8);
    let stopping = Arc::new(AtomicBool::new(false));
    let profile = preferences(&catalog, &common)?;
    let icon = load_icon();
    let instance = unsafe { GetModuleHandleW(null()) };
    let class = wide("MagicConverterRustMain");
    let window_class = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        hIcon: icon,
        hCursor: unsafe { LoadCursorW(null_mut(), IDC_ARROW) },
        hbrBackground: (COLOR_WINDOW + 1) as HBRUSH,
        lpszClassName: class.as_ptr(),
        ..Default::default()
    };
    if unsafe { RegisterClassW(&window_class) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    let ui = Box::new(RefCell::new(Ui {
        catalog,
        common: common.clone(),
        profile,
        paths: Vec::new(),
        web: Vec::new(),
        actions: Vec::new(),
        wheel_profiles: Default::default(),
        main: null_mut(),
        wheel: wheel::Wheel::default(),
        events: receiver,
        sender: sender.clone(),
        submissions,
        stopping: stopping.clone(),
        status_inflight: false,
        status: Value::Null,
        last_status: Instant::now(),
        icon,
        tray_added: false,
    }));
    let state = &*ui as *const RefCell<Ui>;
    let window = unsafe {
        CreateWindowExW(
            0,
            class.as_ptr(),
            wide("MagicConverter").as_ptr(),
            WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            650,
            515,
            null_mut(),
            null_mut(),
            instance,
            state.cast(),
        )
    };
    if window.is_null() {
        return Err(io::Error::last_os_error().into());
    }
    let mut gui = ui.borrow_mut();
    gui.main = window;
    controls(window);
    gui.update_settings();
    let wheel_window = wheel::window(state, instance)?;
    gui.wheel.window = wheel_window;
    drop(gui);
    let _main_drop = ole::Registration::new(state, window, false)?;
    let _wheel_drop = ole::Registration::new(state, wheel_window, true)?;
    {
        let mut gui = ui.borrow_mut();
        gui.tray_added = gui.notify(NIM_ADD);
        if !paths.is_empty() {
            gui.set_inputs(paths);
        } else if !tray || !gui.tray_added {
            gui.show();
        }
    }
    let host = DesktopHost {
        events: sender.clone(),
        stopping: stopping.clone(),
        submissions: std::sync::Mutex::new(requests),
    };
    let server = thread::spawn(move || -> automation::Result<(Value, u8)> {
        let catalog = Catalog::load()?;
        let input = Serve {
            common,
            timeout: Duration::from_secs(3600),
            duration: None,
        };
        let result = automation::ipc::serve_with(&catalog, &input, Some(&host));
        if let Err(error) = &result {
            let _ = sender.send(DesktopEvent::Feedback(error.to_string()));
        }
        result
    });
    ui.borrow().update_interval();
    let mut message = MSG::default();
    while unsafe { GetMessageW(&mut message, null_mut(), 0, 0) } > 0 {
        unsafe {
            TranslateMessage(&message);
            DispatchMessageW(&message);
        }
    }
    stopping.store(true, Ordering::Relaxed);
    drop(_wheel_drop);
    drop(_main_drop);
    unsafe {
        DestroyWindow(wheel_window);
        DestroyWindow(window);
    }
    let _ = server.join();
    Ok(0)
}
