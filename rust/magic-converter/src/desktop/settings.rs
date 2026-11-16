use super::*;
use crate::OutputRule;
use std::collections::HashSet;
use std::path::Path;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::EnableWindow;

const INTERVAL: u16 = 301;
const THRESHOLD: u16 = 302;
const FFMPEG: u16 = 303;
const TEMPLATE: u16 = 304;
const COLLISION: u16 = 305;
const HISTORY_LIMIT: u16 = 306;
const BROWSE: u16 = 307;
const EXPORT: u16 = 308;
const IMPORT: u16 = 309;
const RESTORE: u16 = 310;
const RESET: u16 = 311;
const DESTINATION: u16 = 312;
const INTEGER_FIELDS: [(u16, &str, u64, u64); 3] = [
    (INTERVAL, "interval_ms", 30, 50),
    (THRESHOLD, "threshold", 5, 80),
    (HISTORY_LIMIT, "history_limit", 0, 1000),
];

struct Editor<'a> {
    catalog: &'a Catalog,
    directory: PathBuf,
    draft: Value,
    saved: bool,
    closed: bool,
}

fn file_dialog(window: HWND, save: bool, executable: bool) -> Option<PathBuf> {
    let filter = wide(if executable {
        "실행 파일\0*.exe\0"
    } else {
        "JSON 설정 파일\0*.json\0"
    });
    let ext = wide("json");
    let mut buffer = vec![0_u16; 32_768];
    if save {
        let name = wide("magic-converter-settings.json");
        buffer[..name.len()].copy_from_slice(&name);
    }
    let mut dialog = OPENFILENAMEW {
        lStructSize: size_of::<OPENFILENAMEW>() as u32,
        hwndOwner: window,
        lpstrFilter: filter.as_ptr(),
        lpstrFile: buffer.as_mut_ptr(),
        nMaxFile: buffer.len() as u32,
        lpstrDefExt: if executable { null() } else { ext.as_ptr() },
        Flags: OFN_EXPLORER
            | OFN_NOCHANGEDIR
            | OFN_PATHMUSTEXIST
            | if save {
                OFN_OVERWRITEPROMPT
            } else {
                OFN_FILEMUSTEXIST
            },
        ..Default::default()
    };
    let result = unsafe {
        if save {
            GetSaveFileNameW(&mut dialog)
        } else {
            GetOpenFileNameW(&mut dialog)
        }
    };
    let end = buffer.iter().position(|&ch| ch == 0)?;
    (result != 0).then(|| PathBuf::from(OsString::from_wide(&buffer[..end])))
}

fn normalize(catalog: &Catalog, imported: Value) -> automation::Result<Value> {
    if !matches!(imported["version"].as_u64(), Some(1..=3)) {
        return Err("지원하지 않는 설정 파일 버전입니다.".into());
    }
    let mut profile = catalog.default_preferences.clone();
    for (key, value) in profile.as_object_mut().expect("default settings object") {
        if let Some(field) = imported.get(key) {
            *value = field.clone();
        }
    }
    for (_, key, min, max) in INTEGER_FIELDS {
        if !profile[key]
            .as_u64()
            .is_some_and(|n| (min..=max).contains(&n))
        {
            return Err(format!("{key} 값의 범위는 {min}~{max}입니다.").into());
        }
    }
    if !profile["enabled"].is_boolean()
        || !profile["release_modifier_inside"].is_boolean()
        || !profile["ffmpeg_path"]
            .as_str()
            .is_some_and(|path| path.chars().count() <= 2000 && !path.chars().any(|ch| ch < ' '))
    {
        return Err("드래그 옵션 또는 FFmpeg 경로가 올바르지 않습니다.".into());
    }
    let rule: OutputRule = serde_json::from_value(profile["output"].clone())?;
    rule.validate_recipe()?;
    let mut ids = HashSet::new();
    let presets = profile["presets"]
        .as_array_mut()
        .filter(|items| items.len() <= 200)
        .ok_or("프리셋 목록이 올바르지 않습니다.")?;
    for preset in presets.iter_mut() {
        *preset = automation::normalize_recipe(catalog, preset.take())?;
        if !ids.insert(preset["id"].as_str().unwrap().to_owned()) {
            return Err("프리셋 ID가 중복되었습니다.".into());
        }
    }
    let profiles = profile["profiles"]
        .as_array()
        .filter(|items| items.len() <= 50)
        .ok_or("휠 프로필 목록이 올바르지 않습니다.")?;
    presets::validate_profiles(profiles, profile["presets"].as_array().unwrap())?;
    for (_, key, mode) in Ui::WHEEL_SELECTORS {
        if !profile[key].as_str().is_some_and(|id| {
            id.is_empty() || profiles.iter().any(|p| p["id"] == id && p["mode"] == mode)
        }) {
            return Err("선택한 휠 프로필을 찾을 수 없습니다.".into());
        }
    }
    profile["version"] = catalog.default_preferences["version"].clone();
    Ok(profile)
}

impl Editor<'_> {
    fn display(&self, window: HWND) {
        for (id, key, _, _) in INTEGER_FIELDS {
            text(control(window, id), &self.draft[key].to_string());
        }
        for (id, value) in [
            (FFMPEG, &self.draft["ffmpeg_path"]),
            (TEMPLATE, &self.draft["output"]["template"]),
            (DESTINATION, &self.draft["output"]["directory"]),
        ] {
            text(control(window, id), value.as_str().unwrap_or_default());
        }
        unsafe {
            SendMessageW(
                control(window, COLLISION),
                CB_SETCURSEL,
                usize::from(self.draft["output"]["collision"] == "error"),
                0,
            )
        };
    }

    fn read(&self, window: HWND) -> automation::Result<Value> {
        let mut draft = self.draft.clone();
        for (id, key, _, _) in INTEGER_FIELDS {
            draft[key] = json!(read_text(control(window, id)).trim().parse::<u64>()?);
        }
        draft["ffmpeg_path"] = json!(read_text(control(window, FFMPEG)).trim());
        draft["output"]["template"] = json!(read_text(control(window, TEMPLATE)));
        let directory = read_text(control(window, DESTINATION));
        let directory = directory.trim();
        draft["output"]["directory"] = json!(directory);
        draft["output"]["location"] = json!(if directory.is_empty() {
            "source"
        } else {
            "folder"
        });
        let collision = unsafe { SendMessageW(control(window, COLLISION), CB_GETCURSEL, 0, 0) };
        draft["output"]["collision"] = json!(if collision == 1 { "error" } else { "rename" });
        normalize(self.catalog, draft)
    }

    fn import(&mut self, window: HWND, path: &Path) -> automation::Result<()> {
        self.draft = normalize(self.catalog, automation::read_json(path, 2 * 1024 * 1024)?)?;
        self.display(window);
        Ok(())
    }

    fn command(&mut self, window: HWND, id: u16) -> automation::Result<()> {
        match i32::from(id) {
            IDOK => {
                self.draft = self.read(window)?;
                self.saved = true;
                self.closed = true;
            }
            IDCANCEL => self.closed = true,
            _ => match id {
                BROWSE => {
                    if let Some(path) = file_dialog(window, false, true) {
                        text(control(window, FFMPEG), &path.to_string_lossy());
                    }
                }
                EXPORT => {
                    let draft = self.read(window)?;
                    if let Some(path) = file_dialog(window, true, false) {
                        write_report(&path, &draft)?;
                        msg(
                            window,
                            "설정을 내보냈습니다. 결과 기록은 포함하지 않습니다.",
                        );
                    }
                }
                IMPORT => {
                    if let Some(path) = file_dialog(window, false, false) {
                        self.import(window, &path)?;
                    }
                }
                RESTORE => self.import(window, &self.directory.join("settings.json.bak"))?,
                RESET => {
                    self.draft = self.catalog.default_preferences.clone();
                    self.display(window);
                }
                _ => {}
            },
        }
        Ok(())
    }
}

unsafe extern "system" fn window_proc(
    window: HWND,
    message: u32,
    wp: WPARAM,
    lp: LPARAM,
) -> LRESULT {
    if message == WM_NCCREATE {
        let params = unsafe { &*(lp as *const CREATESTRUCTW) };
        unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, params.lpCreateParams as isize) };
    }
    let pointer = unsafe { GetWindowLongPtrW(window, GWLP_USERDATA) } as *const RefCell<Editor<'_>>;
    if let Some(state) = unsafe { pointer.as_ref() } {
        match message {
            WM_COMMAND => {
                if let Some(Err(error)) = state
                    .try_borrow_mut()
                    .ok()
                    .map(|mut editor| editor.command(window, wp as u16))
                {
                    msg(window, &error.to_string());
                }
                return 0;
            }
            WM_CLOSE => {
                if let Ok(mut editor) = state.try_borrow_mut() {
                    editor.closed = true;
                }
                return 0;
            }
            WM_NCDESTROY => {
                unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, 0) };
            }
            _ => {}
        }
    }
    unsafe { DefWindowProcW(window, message, wp, lp) }
}

pub(super) fn open(ui: &mut Ui) -> automation::Result<()> {
    let class = wide("MagicConverterAdvancedSettings");
    let instance = unsafe { GetModuleHandleW(null()) };
    let registration = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        hCursor: unsafe { LoadCursorW(null_mut(), IDC_ARROW) },
        hbrBackground: (COLOR_BTNFACE + 1) as HBRUSH,
        lpszClassName: class.as_ptr(),
        ..Default::default()
    };
    unsafe { RegisterClassW(&registration) };
    let state = RefCell::new(Editor {
        catalog: &ui.catalog,
        directory: automation::directory(&ui.common)?,
        draft: ui.draft_profile()?,
        saved: false,
        closed: false,
    });
    let window = unsafe {
        CreateWindowExW(
            WS_EX_DLGMODALFRAME | WS_EX_CONTROLPARENT,
            class.as_ptr(),
            wide("MagicConverter 고급 설정").as_ptr(),
            WS_CAPTION | WS_SYSMENU,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            630,
            392,
            ui.main,
            null_mut(),
            instance,
            (&state as *const RefCell<Editor<'_>>).cast(),
        )
    };
    if window.is_null() {
        return Err(io::Error::last_os_error().into());
    }
    let child = WS_CHILD | WS_VISIBLE;
    let tab = child | WS_TABSTOP;
    let edit = tab | WS_BORDER | ES_AUTOHSCROLL as u32;
    let fields = [
        ("STATIC", "감지 주기 (30~50 ms)", child, 0, 16, 20, 180, 22),
        ("EDIT", "", edit, INTERVAL, 198, 17, 84, 25),
        (
            "STATIC",
            "드래그 거리 (5~80 px)",
            child,
            0,
            310,
            20,
            175,
            22,
        ),
        ("EDIT", "", edit, THRESHOLD, 489, 17, 107, 25),
        (
            "STATIC",
            "FFmpeg 경로 (빈칸은 자동)",
            child,
            0,
            16,
            60,
            180,
            22,
        ),
        ("EDIT", "", edit, FFMPEG, 198, 57, 300, 25),
        ("BUTTON", "찾기…", tab, BROWSE, 510, 56, 86, 27),
        (
            "STATIC",
            "출력 폴더 (빈칸은 원본 위치)",
            child,
            0,
            16,
            100,
            180,
            22,
        ),
        ("EDIT", "", edit, DESTINATION, 198, 97, 398, 25),
        ("STATIC", "출력 이름", child, 0, 16, 140, 180, 22),
        ("EDIT", "", edit, TEMPLATE, 198, 137, 398, 25),
        (
            "STATIC",
            "{stem} 원본 이름 · {tag} 변환 종류 · {preset} 프리셋",
            child,
            0,
            198,
            167,
            397,
            20,
        ),
        ("STATIC", "이름 충돌", child, 0, 16, 200, 180, 22),
        (
            "COMBOBOX",
            "",
            tab | CBS_DROPDOWNLIST as u32,
            COLLISION,
            198,
            197,
            398,
            99,
        ),
        (
            "STATIC",
            "결과 기록 보관 수 (0~1,000)",
            child,
            0,
            16,
            239,
            180,
            22,
        ),
        ("EDIT", "", edit, HISTORY_LIMIT, 198, 236, 84, 25),
        ("BUTTON", "가져오기…", tab, IMPORT, 16, 275, 130, 29),
        ("BUTTON", "내보내기…", tab, EXPORT, 164, 275, 130, 29),
        ("BUTTON", "백업 복구", tab, RESTORE, 312, 275, 130, 29),
        ("BUTTON", "기본값 복원", tab, RESET, 466, 275, 130, 29),
        (
            "BUTTON",
            "저장",
            tab | BS_DEFPUSHBUTTON as u32,
            IDOK as u16,
            400,
            315,
            94,
            29,
        ),
        ("BUTTON", "취소", tab, IDCANCEL as u16, 502, 315, 94, 29),
    ];
    for (class, label, style, id, x, y, width, height) in fields {
        let item = unsafe {
            CreateWindowExW(
                0,
                wide(class).as_ptr(),
                wide(label).as_ptr(),
                style,
                x,
                y,
                width,
                height,
                window,
                handle(id),
                instance,
                null(),
            )
        };
        unsafe {
            SendMessageW(
                item,
                WM_SETFONT,
                GetStockObject(DEFAULT_GUI_FONT) as usize,
                1,
            )
        };
    }
    for label in ["번호를 붙여 저장", "오류 표시"] {
        unsafe {
            SendMessageW(
                control(window, COLLISION),
                CB_ADDSTRING,
                0,
                wide(label).as_ptr() as isize,
            )
        };
    }
    let editor = state.borrow();
    editor.display(window);
    unsafe {
        EnableWindow(
            control(window, RESTORE),
            i32::from(editor.directory.join("settings.json.bak").is_file()),
        );
        EnableWindow(ui.main, 0);
        ShowWindow(window, SW_SHOW);
    }
    drop(editor);
    while !state.borrow().closed {
        let mut message = MSG::default();
        let received = unsafe { GetMessageW(&mut message, null_mut(), 0, 0) };
        if received <= 0 {
            if received == 0 {
                unsafe { PostQuitMessage(message.wParam as i32) };
            }
            break;
        }
        if unsafe { IsDialogMessageW(window, &message) } == 0 {
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
    unsafe {
        DestroyWindow(window);
        EnableWindow(ui.main, 1);
        SetForegroundWindow(ui.main);
    }
    let editor = state.into_inner();
    if editor.saved {
        ui.save_profile(editor.draft)?;
        ui.update_settings();
        if !ui.paths.is_empty() {
            ui.set_selection(ui.paths.clone(), ui.web.clone());
        }
        text(control(ui.main, STATUS), "고급 설정을 저장했습니다.");
    }
    Ok(())
}
