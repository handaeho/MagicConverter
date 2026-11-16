use super::*;
use crate::OutputRule;
use std::collections::HashSet;
use std::path::Path;
use windows_sys::Win32::System::SystemServices::SS_CENTERIMAGE;
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
const BROWSE_FOLDER: u16 = 313;
const USE_SOURCE: u16 = 314;
const USE_AUTO: u16 = 315;
const INTEGER_FIELDS: [(u16, &str, u64, u64); 3] = [
    (INTERVAL, "interval_ms", 30, 50),
    (THRESHOLD, "threshold", 5, 80),
    (HISTORY_LIMIT, "history_limit", 0, 1000),
];
const NAME_STYLES: [(&str, &str); 5] = [
    ("{stem}_{tag}", "원본 이름_변환 종류"),
    ("{stem}", "원본 이름 그대로"),
    ("{stem}-{tag}", "원본 이름-변환 종류"),
    ("{tag}_{stem}", "변환 종류_원본 이름"),
    ("{stem}_{preset}", "원본 이름_프리셋 이름"),
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

fn folder_dialog(window: HWND) -> Option<PathBuf> {
    use windows::Win32::Foundation::HWND as ComWindow;
    use windows::Win32::System::Com::{CLSCTX_INPROC_SERVER, CoCreateInstance, CoTaskMemFree};
    use windows::Win32::UI::Shell::{
        FOS_FORCEFILESYSTEM, FOS_PICKFOLDERS, FileOpenDialog, IFileOpenDialog, SIGDN_FILESYSPATH,
    };
    use windows::core::w;

    unsafe {
        let dialog: IFileOpenDialog =
            CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER).ok()?;
        dialog
            .SetOptions(dialog.GetOptions().ok()? | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM)
            .ok()?;
        dialog.SetTitle(w!("결과를 저장할 폴더 고르기")).ok()?;
        dialog.Show(Some(ComWindow(window))).ok()?;
        let path = dialog
            .GetResult()
            .ok()?
            .GetDisplayName(SIGDN_FILESYSPATH)
            .ok()?;
        let result = PathBuf::from(OsString::from_wide(path.as_wide()));
        CoTaskMemFree(Some(path.0.cast()));
        Some(result)
    }
}

fn add_choice(window: HWND, label: &str) {
    unsafe { SendMessageW(window, CB_ADDSTRING, 0, wide(label).as_ptr() as isize) };
}

fn select_choice(window: HWND, index: usize) {
    unsafe { SendMessageW(window, CB_SETCURSEL, index, 0) };
}

fn choice(window: HWND) -> usize {
    unsafe { SendMessageW(window, CB_GETCURSEL, 0, 0) as usize }
}

fn populate_choices(window: HWND) {
    for (id, _, min, max) in INTEGER_FIELDS {
        let combo = control(window, id);
        for value in min..=max {
            let label = match id {
                INTERVAL => format!("{value} ms"),
                THRESHOLD => format!("{value} px"),
                _ if value == 0 => "보관 안 함".to_owned(),
                _ => format!("{value}개"),
            };
            add_choice(combo, &label);
        }
    }
    for label in ["번호를 붙여 저장", "저장하지 않고 오류 표시"] {
        add_choice(control(window, COLLISION), label);
    }
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
        for (id, key, min, max) in INTEGER_FIELDS {
            let current = self.draft[key].as_u64().unwrap_or(min).clamp(min, max);
            select_choice(control(window, id), (current - min) as usize);
        }
        self.display_paths(window);
        let template = control(window, TEMPLATE);
        unsafe { SendMessageW(template, CB_RESETCONTENT, 0, 0) };
        for (_, label) in NAME_STYLES {
            add_choice(template, label);
        }
        let current = self.draft["output"]["template"]
            .as_str()
            .unwrap_or_default();
        let selected = NAME_STYLES.iter().position(|(style, _)| *style == current);
        if selected.is_none() {
            add_choice(template, "기존 사용자 지정 이름 유지");
        }
        select_choice(template, selected.unwrap_or(NAME_STYLES.len()));
        select_choice(
            control(window, COLLISION),
            usize::from(self.draft["output"]["collision"] == "error"),
        );
    }

    fn display_paths(&self, window: HWND) {
        for (id, value, fallback) in [
            (FFMPEG, &self.draft["ffmpeg_path"], "자동 찾기"),
            (
                DESTINATION,
                &self.draft["output"]["directory"],
                "원본 파일의 폴더",
            ),
        ] {
            let path = value.as_str().filter(|path| !path.is_empty());
            text(control(window, id), path.unwrap_or(fallback));
        }
    }

    fn read(&self, window: HWND) -> automation::Result<Value> {
        let mut draft = self.draft.clone();
        for (id, key, min, max) in INTEGER_FIELDS {
            let index = choice(control(window, id)) as u64;
            if index <= max - min {
                draft[key] = json!(min + index);
            }
        }
        if let Some((template, _)) = NAME_STYLES.get(choice(control(window, TEMPLATE))) {
            draft["output"]["template"] = json!(template);
        }
        draft["output"]["collision"] = json!(if choice(control(window, COLLISION)) == 1 {
            "error"
        } else {
            "rename"
        });
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
                        self.draft["ffmpeg_path"] = json!(path.to_string_lossy());
                        self.display_paths(window);
                    }
                }
                USE_AUTO => {
                    self.draft["ffmpeg_path"] = json!("");
                    self.display_paths(window);
                }
                BROWSE_FOLDER => {
                    if let Some(path) = folder_dialog(window) {
                        self.draft["output"]["directory"] = json!(path.to_string_lossy());
                        self.draft["output"]["location"] = json!("folder");
                        self.display_paths(window);
                    }
                }
                USE_SOURCE => {
                    self.draft["output"]["directory"] = json!("");
                    self.draft["output"]["location"] = json!("source");
                    self.display_paths(window);
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
    if let Some(result) = form::message(window, message, wp, lp) {
        return result;
    }
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

fn advanced_help(id: u16, label: &str) -> &'static str {
    match (id, label) {
        (INTERVAL, _) | (0, "드래그 확인 간격") => {
            "드래그 중인지를 확인하는 시간 간격. 숫자가 작을수록 더 자주 확인"
        }
        (THRESHOLD, _) | (0, "휠 표시 거리") => {
            "왼쪽 버튼을 누른 뒤 변환 휠이 나타나기까지 마우스를 움직일 최소 거리"
        }
        (FFMPEG, _) | (0, "FFmpeg") => {
            "동영상과 오디오 변환에 사용할 FFmpeg. 자동 찾기는 내장 도구 우선 사용"
        }
        (BROWSE, _) => "별도로 설치한 FFmpeg 실행 파일의 선택",
        (USE_AUTO, _) => "FFmpeg 실행 파일의 자동 찾기",
        (DESTINATION, _) | (0, "결과 저장 폴더") => "변환한 파일을 저장할 기본 폴더",
        (BROWSE_FOLDER, _) => "변환 결과를 모아 둘 폴더의 선택",
        (USE_SOURCE, _) => "각 결과를 원본 파일과 같은 폴더에 저장",
        (TEMPLATE, _) | (0, "결과 파일 이름") => {
            "원본 이름, 변환 종류, 프리셋 이름을 사용해 결과 이름을 만드는 방식"
        }
        (COLLISION, _) | (0, "같은 이름의 파일") => {
            "저장할 곳에 같은 이름의 파일이 있을 때 번호를 붙이거나 저장을 중단하는 방식"
        }
        (HISTORY_LIMIT, _) | (0, "보관할 결과 기록") => {
            "최근 변환 결과를 최대 몇 건까지 보관할지 결정. 보관 안 함은 새 기록 미보관"
        }
        (IMPORT, _) => "파일에 저장된 설정을 이 창으로 불러오기",
        (EXPORT, _) => "이 창의 설정을 파일로 저장. 결과 기록 제외",
        (RESTORE, _) => "직전에 저장한 설정의 백업을 이 창으로 불러오기",
        (RESET, _) => "이 창의 설정을 처음 설치했을 때의 값으로 되돌리기",
        (id, _) if id == IDOK as u16 => "변경한 설정의 저장과 적용",
        (id, _) if id == IDCANCEL as u16 => "이 창에서 바꾼 설정을 적용하지 않고 닫기",
        _ => "",
    }
}

pub(super) fn open(ui: &mut Ui) -> automation::Result<()> {
    let class = wide("MagicConverterAdvancedSettings");
    let instance = unsafe { GetModuleHandleW(null()) };
    let registration = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        hCursor: unsafe { LoadCursorW(null_mut(), IDC_ARROW) },
        hbrBackground: (COLOR_WINDOW + 1) as HBRUSH,
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
    let mut owner_position = RECT::default();
    unsafe { GetWindowRect(ui.main, &mut owner_position) };
    let window = unsafe {
        CreateWindowExW(
            WS_EX_DLGMODALFRAME | WS_EX_CONTROLPARENT,
            class.as_ptr(),
            wide("MagicConverter 고급 설정").as_ptr(),
            WS_CAPTION | WS_SYSMENU,
            owner_position.left,
            owner_position.top,
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
    let label = WS_CHILD | WS_VISIBLE | SS_CENTERIMAGE;
    let tab = WS_CHILD | WS_VISIBLE | WS_TABSTOP;
    let combo = tab | WS_VSCROLL | CBS_DROPDOWNLIST as u32;
    let display = WS_CHILD | WS_VISIBLE | WS_BORDER | ES_AUTOHSCROLL as u32 | ES_READONLY as u32;
    let fields = [
        ("STATIC", "드래그 확인 간격", label, 0, 20, 18, 147, 28),
        ("COMBOBOX", "", combo, INTERVAL, 178, 18, 125, 230),
        ("STATIC", "휠 표시 거리", label, 0, 337, 18, 128, 28),
        ("COMBOBOX", "", combo, THRESHOLD, 487, 18, 155, 230),
        ("STATIC", "FFmpeg", label, 0, 20, 66, 147, 28),
        ("EDIT", "", display, FFMPEG, 178, 66, 234, 26),
        ("BUTTON", "파일 고르기…", tab, BROWSE, 422, 64, 112, 30),
        ("BUTTON", "자동 찾기", tab, USE_AUTO, 542, 64, 100, 30),
        ("STATIC", "결과 저장 폴더", label, 0, 20, 110, 147, 28),
        ("EDIT", "", display, DESTINATION, 178, 110, 234, 26),
        (
            "BUTTON",
            "폴더 고르기…",
            tab,
            BROWSE_FOLDER,
            422,
            108,
            112,
            30,
        ),
        ("BUTTON", "원본 폴더", tab, USE_SOURCE, 542, 108, 100, 30),
        ("STATIC", "결과 파일 이름", label, 0, 20, 154, 147, 28),
        ("COMBOBOX", "", combo, TEMPLATE, 178, 154, 464, 170),
        ("STATIC", "같은 이름의 파일", label, 0, 20, 198, 147, 28),
        ("COMBOBOX", "", combo, COLLISION, 178, 198, 464, 100),
        ("STATIC", "보관할 결과 기록", label, 0, 20, 242, 147, 28),
        ("COMBOBOX", "", combo, HISTORY_LIMIT, 178, 242, 154, 230),
        ("BUTTON", "설정 가져오기…", tab, IMPORT, 20, 297, 145, 32),
        ("BUTTON", "설정 내보내기…", tab, EXPORT, 181, 297, 145, 32),
        ("BUTTON", "백업 복구", tab, RESTORE, 342, 297, 145, 32),
        ("BUTTON", "기본값 복원", tab, RESET, 497, 297, 145, 32),
        (
            "BUTTON",
            "저장",
            tab | BS_DEFPUSHBUTTON as u32,
            IDOK as u16,
            434,
            350,
            100,
            32,
        ),
        ("BUTTON", "취소", tab, IDCANCEL as u16, 542, 350, 100, 32),
    ];
    form::install(window, (662, 400), &fields, advanced_help);
    populate_choices(window);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_choices_save_and_preserve_an_existing_custom_name() {
        let catalog = Catalog::load().unwrap();
        let mut editor = Editor {
            catalog: &catalog,
            directory: PathBuf::new(),
            draft: catalog.default_preferences.clone(),
            saved: false,
            closed: false,
        };
        editor.draft["output"]["template"] = json!("완료-{stem}-{tag}");
        editor.draft["output"]["location"] = json!("folder");
        editor.draft["output"]["directory"] = json!(r"C:\test");
        let instance = unsafe { GetModuleHandleW(null()) };
        let window = unsafe {
            CreateWindowExW(
                0,
                wide("STATIC").as_ptr(),
                null(),
                WS_OVERLAPPED,
                0,
                0,
                350,
                250,
                null_mut(),
                null_mut(),
                instance,
                null(),
            )
        };
        assert!(!window.is_null());
        for (index, id) in [INTERVAL, THRESHOLD, HISTORY_LIMIT, TEMPLATE, COLLISION]
            .into_iter()
            .enumerate()
        {
            let item = unsafe {
                CreateWindowExW(
                    0,
                    wide("COMBOBOX").as_ptr(),
                    null(),
                    WS_CHILD | CBS_DROPDOWNLIST as u32,
                    0,
                    index as i32 * 34,
                    200,
                    150,
                    window,
                    handle(id),
                    instance,
                    null(),
                )
            };
            assert!(!item.is_null());
        }
        populate_choices(window);
        editor.display(window);
        assert_eq!(
            editor.read(window).unwrap()["output"]["template"],
            "완료-{stem}-{tag}"
        );
        select_choice(control(window, INTERVAL), 10);
        select_choice(control(window, HISTORY_LIMIT), 0);
        select_choice(control(window, TEMPLATE), 1);
        editor.command(window, USE_SOURCE).unwrap();
        editor.command(window, IDOK as u16).unwrap();
        unsafe { DestroyWindow(window) };
        assert!(editor.saved);
        assert_eq!(editor.draft["interval_ms"], 40);
        assert_eq!(editor.draft["history_limit"], 0);
        assert_eq!(editor.draft["output"]["template"], "{stem}");
        assert_eq!(editor.draft["output"]["location"], "source");
        assert_eq!(editor.draft["output"]["directory"], "");
    }
}
