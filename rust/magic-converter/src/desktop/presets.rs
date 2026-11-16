use super::*;
use std::collections::HashSet;

const LIST: u16 = 201;
const NAME: u16 = 202;
const ACTION: u16 = 203;
const ADD: u16 = 205;
const DUPLICATE: u16 = 206;
const SAVE: u16 = 208;
const CANCEL: u16 = 209;
const DELETE: u16 = 210;
const PROFILES: u16 = 211;
const PROFILE_NAME: u16 = 212;
const MODE: u16 = 213;
const PROFILE_ADD: u16 = 214;
const PROFILE_DELETE: u16 = 215;
const SLOTS: u16 = 216;
const AVAILABLE: u16 = 217;
const SLOT_ADD: u16 = 218;
const SLOT_DELETE: u16 = 219;
const SLOT_UP: u16 = 220;
const SLOT_DOWN: u16 = 221;
const LOCATION: u16 = 222;
const DIRECTORY: u16 = 223;
const TEMPLATE: u16 = 224;
const COLLISION: u16 = 225;
const OPTION: u16 = 230;
const OPTION_LABEL: u16 = 240;

type Field = (&'static str, &'static str, &'static str);

fn fields(action: &str) -> Vec<Field> {
    let [image, video, audio] = automation::recipe_encoder_fields(action);
    let mut fields = Vec::new();
    if image {
        fields.extend([
            ("options", "image_quality", "이미지 품질 (1~100)"),
            ("options", "image_max_edge", "최대 변 길이 (px, 0은 원본)"),
        ]);
    }
    if video {
        fields.push(("options", "video_crf", "영상 CRF (빈칸은 기본값)"));
    }
    if audio {
        fields.push(("options", "audio_bitrate", "음성 kbps (빈칸은 기본값)"));
    }
    fields.extend(automation::recipe_tool_fields(action).iter().map(|&key| {
        let label = match key {
            "pdf_pages" => "PDF 페이지 (예: 1,3-5)",
            "rotation" => "회전 (0/90/180/270°)",
            "flip" => "반전 (가로/세로)",
            "crop_x" => "자르기 X (px)",
            "crop_y" => "자르기 Y (px)",
            "crop_width" => "자르기 너비 (px)",
            "crop_height" => "자르기 높이 (px)",
            "start_ms" => "시작 (ms)",
            "duration_ms" => "길이 (ms)",
            "frame_interval" => "프레임 간격",
            "max_frames" => "최대 프레임 수",
            _ => "자막 이동 (ms)",
        };
        ("tools", key, label)
    }));
    fields
}

struct Editor<'a> {
    catalog: &'a Catalog,
    presets: Vec<Value>,
    profiles: Vec<Value>,
    selected: Option<usize>,
    profile: Option<usize>,
    saved: bool,
    closed: bool,
}

fn send(window: HWND, id: u16, message: u32, wp: usize, lp: isize) -> isize {
    unsafe { SendMessageW(control(window, id), message, wp, lp) }
}

fn add(window: HWND, id: u16, message: u32, label: &str) {
    send(window, id, message, 0, wide(label).as_ptr() as isize);
}

fn selection(window: HWND, id: u16, message: u32) -> Option<usize> {
    usize::try_from(send(window, id, message, 0, 0)).ok()
}

fn field_text(value: &Value) -> String {
    match value {
        Value::String(text) => match text.as_str() {
            "horizontal" => "가로".into(),
            "vertical" => "세로".into(),
            _ => text.clone(),
        },
        Value::Number(number) => number.to_string(),
        _ => String::new(),
    }
}

impl Editor<'_> {
    fn list(&mut self, window: HWND, selected: usize) {
        send(window, LIST, LB_RESETCONTENT, 0, 0);
        for preset in &self.presets {
            add(
                window,
                LIST,
                LB_ADDSTRING,
                preset["name"].as_str().unwrap_or("프리셋"),
            );
        }
        send(window, LIST, LB_SETCURSEL, selected, 0);
        self.select(window);
        self.available(window);
    }

    fn action(&self, window: HWND) -> Option<&str> {
        let index = selection(window, ACTION, CB_GETCURSEL)?;
        Some(&self.catalog.actions.get(index)?.key)
    }

    fn options(&self, window: HWND, recipe: &Value) {
        let active = self.action(window).map_or_else(Vec::new, fields);
        let defaults = &self.catalog.default_preferences["presets"][0];
        for index in 0..4 {
            let edit = control(window, OPTION + index as u16);
            let label = control(window, OPTION_LABEL + index as u16);
            let field = active.get(index);
            unsafe {
                ShowWindow(edit, if field.is_some() { SW_SHOW } else { SW_HIDE });
                ShowWindow(label, if field.is_some() { SW_SHOW } else { SW_HIDE });
            }
            if let Some(&(group, key, title)) = field {
                let value = recipe[group].get(key).unwrap_or(&defaults[group][key]);
                text(label, title);
                text(edit, &field_text(value));
            }
        }
    }

    fn select(&mut self, window: HWND) {
        self.selected =
            selection(window, LIST, LB_GETCURSEL).filter(|&index| index < self.presets.len());
        let preset = self
            .selected
            .map(|index| &self.presets[index])
            .unwrap_or(&Value::Null);
        text(
            control(window, NAME),
            preset["name"].as_str().unwrap_or_default(),
        );
        let action = self
            .catalog
            .actions
            .iter()
            .position(|a| preset["action_key"] == a.key);
        send(
            window,
            ACTION,
            CB_SETCURSEL,
            action.unwrap_or(usize::MAX),
            0,
        );
        self.options(window, preset);
        let rule = &preset["output"];
        let defaults = &self.catalog.default_preferences["output"];
        let location = if rule.is_null() {
            0
        } else if rule["location"] == "folder" {
            2
        } else {
            1
        };
        send(window, LOCATION, CB_SETCURSEL, location, 0);
        text(
            control(window, DIRECTORY),
            rule["directory"].as_str().unwrap_or_default(),
        );
        text(
            control(window, TEMPLATE),
            rule["template"]
                .as_str()
                .or_else(|| defaults["template"].as_str())
                .unwrap_or_default(),
        );
        send(
            window,
            COLLISION,
            CB_SETCURSEL,
            usize::from(rule["collision"] == "error"),
            0,
        );
        Self::output_enabled(window);
    }

    fn output_enabled(window: HWND) {
        let choice = selection(window, LOCATION, CB_GETCURSEL).unwrap_or(0);
        use windows_sys::Win32::UI::Input::KeyboardAndMouse::EnableWindow;
        unsafe {
            EnableWindow(control(window, DIRECTORY), i32::from(choice == 2));
            EnableWindow(control(window, TEMPLATE), i32::from(choice != 0));
            EnableWindow(control(window, COLLISION), i32::from(choice != 0));
        }
    }

    fn apply(&mut self, window: HWND) -> Result<(), String> {
        let Some(index) = self.selected else {
            return Ok(());
        };
        let action = self.action(window).ok_or("변환 작업을 선택하세요.")?;
        let mut recipe = json!({
            "id": self.presets[index]["id"],
            "name": read_text(control(window, NAME)).trim(),
            "action_key": action,
            "options": {},
            "tools": {},
        });
        for (index, (group, key, label)) in fields(action).into_iter().enumerate() {
            let entered = read_text(control(window, OPTION + index as u16));
            let entered = entered.trim();
            recipe[group][key] = match key {
                "pdf_pages" => json!(entered),
                "flip" => match entered {
                    "가로" | "horizontal" => json!("horizontal"),
                    "세로" | "vertical" => json!("vertical"),
                    _ => return Err("반전은 가로 또는 세로로 입력하세요.".into()),
                },
                "video_crf" | "audio_bitrate" if entered.is_empty() => Value::Null,
                _ => json!(
                    entered
                        .parse::<i64>()
                        .map_err(|_| format!("{label}에 숫자를 입력하세요."))?
                ),
            };
        }
        let location = selection(window, LOCATION, CB_GETCURSEL).unwrap_or(0);
        if location != 0 {
            recipe["output"] = json!({
                "location": if location == 2 { "folder" } else { "source" },
                "directory": read_text(control(window, DIRECTORY)).trim(),
                "template": read_text(control(window, TEMPLATE)).trim(),
                "collision": if selection(window, COLLISION, CB_GETCURSEL) == Some(1) { "error" } else { "rename" },
            });
        }
        self.presets[index] = automation::normalize_recipe(self.catalog, recipe)?;
        Ok(())
    }

    fn available(&self, window: HWND) {
        send(window, AVAILABLE, CB_RESETCONTENT, 0, 0);
        for preset in &self.presets {
            add(
                window,
                AVAILABLE,
                CB_ADDSTRING,
                preset["name"].as_str().unwrap_or("프리셋"),
            );
        }
        send(window, AVAILABLE, CB_SETCURSEL, 0, 0);
    }

    fn profile_list(&mut self, window: HWND, index: usize) {
        send(window, PROFILES, LB_RESETCONTENT, 0, 0);
        for profile in &self.profiles {
            add(
                window,
                PROFILES,
                LB_ADDSTRING,
                profile["name"].as_str().unwrap_or("휠 프로필"),
            );
        }
        send(window, PROFILES, LB_SETCURSEL, index, 0);
        self.select_profile(window);
    }

    fn select_profile(&mut self, window: HWND) {
        self.profile =
            selection(window, PROFILES, LB_GETCURSEL).filter(|&index| index < self.profiles.len());
        let profile = self
            .profile
            .map(|index| &self.profiles[index])
            .unwrap_or(&Value::Null);
        text(
            control(window, PROFILE_NAME),
            profile["name"].as_str().unwrap_or_default(),
        );
        send(
            window,
            MODE,
            CB_SETCURSEL,
            usize::from(profile["mode"] == "action"),
            0,
        );
        self.slots(window, 0);
    }

    fn slots(&self, window: HWND, selected: usize) {
        send(window, SLOTS, LB_RESETCONTENT, 0, 0);
        if let Some(ids) = self
            .profile
            .and_then(|index| self.profiles[index]["preset_ids"].as_array())
        {
            for id in ids {
                let name = self
                    .presets
                    .iter()
                    .find(|p| p["id"] == *id)
                    .and_then(|p| p["name"].as_str())
                    .unwrap_or("삭제된 프리셋");
                add(window, SLOTS, LB_ADDSTRING, name);
            }
        }
        send(window, SLOTS, LB_SETCURSEL, selected, 0);
    }

    fn apply_profile(&mut self, window: HWND) {
        if let Some(index) = self.profile {
            self.profiles[index]["name"] = json!(read_text(control(window, PROFILE_NAME)).trim());
            self.profiles[index]["mode"] =
                json!(if selection(window, MODE, CB_GETCURSEL) == Some(1) {
                    "action"
                } else {
                    "convert"
                });
        }
    }

    fn change_slot(&mut self, window: HWND, id: u16) -> Result<(), String> {
        let index = self.profile.ok_or("휠 프로필을 선택하거나 추가하세요.")?;
        let slots = self.profiles[index]["preset_ids"]
            .as_array_mut()
            .ok_or("휠 프로필에 배치할 프리셋 목록이 없습니다.")?;
        let mut selected = selection(window, SLOTS, LB_GETCURSEL).filter(|&i| i < slots.len());
        if id == SLOT_ADD {
            let preset = selection(window, AVAILABLE, CB_GETCURSEL)
                .and_then(|i| self.presets.get(i))
                .ok_or("추가할 프리셋을 선택하세요.")?;
            if slots.contains(&preset["id"]) || slots.len() == 12 {
                return Err("휠에는 서로 다른 프리셋을 최대 12개 배치할 수 있습니다.".into());
            }
            selected = Some(slots.len());
            slots.push(preset["id"].clone());
        } else if let Some(i) = selected {
            match id {
                SLOT_DELETE => {
                    slots.remove(i);
                    selected = Some(i.min(slots.len().saturating_sub(1)));
                }
                SLOT_UP if i > 0 => {
                    slots.swap(i, i - 1);
                    selected = Some(i - 1);
                }
                SLOT_DOWN if i + 1 < slots.len() => {
                    slots.swap(i, i + 1);
                    selected = Some(i + 1);
                }
                _ => {}
            }
        }
        self.slots(window, selected.unwrap_or(0));
        Ok(())
    }

    fn command(&mut self, window: HWND, id: u16, notification: u16) -> Result<(), String> {
        match id {
            LIST if notification == LBN_SELCHANGE as u16 => {
                if let Err(error) = self.apply(window) {
                    send(
                        window,
                        LIST,
                        LB_SETCURSEL,
                        self.selected.unwrap_or(usize::MAX),
                        0,
                    );
                    return Err(error);
                }
                let index = selection(window, LIST, LB_GETCURSEL).unwrap_or(0);
                self.list(window, index);
                self.slots(window, 0);
            }
            ACTION if notification == CBN_SELCHANGE as u16 => {
                self.options(window, &Value::Null);
            }
            LOCATION if notification == CBN_SELCHANGE as u16 => Self::output_enabled(window),
            ADD | DUPLICATE => {
                self.apply(window)?;
                if self.presets.len() >= 200 {
                    return Err("프리셋은 최대 200개까지 만들 수 있습니다.".into());
                }
                let mut preset = if id == DUPLICATE {
                    let index = self.selected.ok_or("복제할 프리셋을 선택하세요.")?;
                    let mut preset = self.presets[index].clone();
                    let name = preset["name"]
                        .as_str()
                        .unwrap_or_default()
                        .chars()
                        .take(55)
                        .collect::<String>();
                    preset["name"] = json!(format!("{name} 복제"));
                    preset
                } else {
                    json!({"name":"새 프리셋", "action_key":"image:png"})
                };
                preset["id"] = json!(uuid::Uuid::new_v4().simple().to_string());
                self.presets.push(preset);
                self.list(window, self.presets.len() - 1);
            }
            DELETE => {
                if let Some(index) = self.selected {
                    let id = self.presets.remove(index)["id"].clone();
                    for profile in &mut self.profiles {
                        if let Some(ids) = profile["preset_ids"].as_array_mut() {
                            ids.retain(|item| *item != id);
                        }
                    }
                    self.list(window, index.min(self.presets.len().saturating_sub(1)));
                    self.slots(window, 0);
                }
            }
            PROFILES if notification == LBN_SELCHANGE as u16 => {
                self.apply_profile(window);
                self.profile_list(
                    window,
                    selection(window, PROFILES, LB_GETCURSEL).unwrap_or(0),
                );
            }
            PROFILE_ADD => {
                self.apply_profile(window);
                if self.profiles.len() >= 50 {
                    return Err("휠 프로필은 최대 50개까지 만들 수 있습니다.".into());
                }
                self.profiles
                    .push(json!({"id":uuid::Uuid::new_v4().simple().to_string(),
                    "name":"새 휠 프로필", "mode":"convert", "preset_ids":[]}));
                self.profile_list(window, self.profiles.len() - 1);
            }
            PROFILE_DELETE => {
                if let Some(index) = self.profile {
                    self.profiles.remove(index);
                    self.profile_list(window, index.min(self.profiles.len().saturating_sub(1)));
                }
            }
            SLOT_ADD | SLOT_DELETE | SLOT_UP | SLOT_DOWN => self.change_slot(window, id)?,
            SAVE => {
                self.apply(window)?;
                self.apply_profile(window);
                validate_profiles(&self.profiles, &self.presets)?;
                self.saved = true;
                self.closed = true;
            }
            CANCEL => self.closed = true,
            _ => {}
        }
        Ok(())
    }
}

pub(super) fn validate_profiles(profiles: &[Value], presets: &[Value]) -> Result<(), String> {
    let available: HashSet<_> = presets.iter().filter_map(|p| p["id"].as_str()).collect();
    let mut profile_ids = HashSet::new();
    for profile in profiles {
        let id = profile["id"]
            .as_str()
            .filter(|value| automation::valid_id(value))
            .ok_or("휠 프로필 ID가 올바르지 않습니다.")?;
        if !profile_ids.insert(id) {
            return Err("휠 프로필 ID가 중복되었습니다.".into());
        }
        if !profile["name"].as_str().is_some_and(|name| {
            !name.trim().is_empty() && name.chars().count() <= 60 && !name.chars().any(|c| c < ' ')
        }) || !matches!(profile["mode"].as_str(), Some("convert" | "action"))
        {
            return Err("휠 프로필의 이름과 모드를 지정하세요.".into());
        }
        let ids = profile["preset_ids"]
            .as_array()
            .filter(|items| (1..=12).contains(&items.len()))
            .ok_or("휠 프로필에 프리셋을 1~12개 배치하세요.")?;
        let mut seen = HashSet::new();
        if !ids.iter().all(|item| {
            item.as_str()
                .is_some_and(|id| available.contains(id) && seen.insert(id))
        }) {
            return Err("휠 프로필에 없거나 중복된 프리셋 ID가 있습니다.".into());
        }
    }
    Ok(())
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
                let result = state
                    .try_borrow_mut()
                    .ok()
                    .map(|mut editor| editor.command(window, wp as u16, (wp >> 16) as u16));
                if let Some(Err(error)) = result {
                    msg(window, &error);
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
    let class = wide("MagicConverterPresetEditor");
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
        presets: ui.profile["presets"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
        profiles: ui.profile["profiles"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
        selected: None,
        profile: None,
        saved: false,
        closed: false,
    });
    let window = unsafe {
        CreateWindowExW(
            WS_EX_DLGMODALFRAME | WS_EX_CONTROLPARENT | WS_EX_APPWINDOW,
            class.as_ptr(),
            wide("MagicConverter 프리셋 및 휠 편집").as_ptr(),
            WS_CAPTION | WS_SYSMENU,
            CW_USEDEFAULT,
            CW_USEDEFAULT,
            748,
            640,
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
    let combo = tab | WS_VSCROLL | CBS_DROPDOWNLIST as u32;
    let list = tab | WS_BORDER | WS_VSCROLL | LBS_NOTIFY as u32;
    let mut fields = vec![
        ("STATIC", "프리셋", child, 0, 16, 12, 190, 21),
        ("LISTBOX", "", list, LIST, 16, 37, 193, 226),
        ("BUTTON", "추가", tab, ADD, 16, 268, 58, 27),
        ("BUTTON", "복제", tab, DUPLICATE, 81, 268, 58, 27),
        ("BUTTON", "삭제", tab, DELETE, 146, 268, 63, 27),
        ("STATIC", "이름", child, 0, 226, 14, 75, 21),
        ("EDIT", "", edit, NAME, 306, 10, 416, 24),
        ("STATIC", "작업", child, 0, 226, 44, 75, 21),
        ("COMBOBOX", "", combo, ACTION, 306, 40, 416, 230),
        ("STATIC", "저장 위치", child, 0, 226, 198, 111, 20),
        ("COMBOBOX", "", combo, LOCATION, 340, 194, 382, 130),
        ("STATIC", "출력 폴더", child, 0, 226, 226, 111, 20),
        ("EDIT", "", edit, DIRECTORY, 340, 222, 382, 23),
        ("STATIC", "파일 이름 형식", child, 0, 226, 254, 111, 20),
        ("EDIT", "", edit, TEMPLATE, 340, 250, 382, 23),
        ("STATIC", "이름이 겹칠 때", child, 0, 226, 282, 111, 20),
        ("COMBOBOX", "", combo, COLLISION, 340, 278, 382, 100),
        ("STATIC", "휠 프로필", child, 0, 16, 324, 190, 20),
        ("LISTBOX", "", list, PROFILES, 16, 348, 193, 181),
        ("BUTTON", "프로필 추가", tab, PROFILE_ADD, 16, 532, 93, 27),
        (
            "BUTTON",
            "프로필 삭제",
            tab,
            PROFILE_DELETE,
            116,
            532,
            93,
            27,
        ),
        ("STATIC", "프로필 이름", child, 0, 226, 329, 101, 21),
        ("EDIT", "", edit, PROFILE_NAME, 330, 325, 392, 24),
        ("STATIC", "휠 모드", child, 0, 226, 357, 101, 21),
        ("COMBOBOX", "", combo, MODE, 330, 353, 392, 100),
        (
            "STATIC",
            "배치 순서 (위부터 시계 방향)",
            child,
            0,
            226,
            382,
            300,
            22,
        ),
        ("LISTBOX", "", list, SLOTS, 226, 406, 379, 113),
        ("BUTTON", "위로", tab, SLOT_UP, 615, 406, 107, 27),
        ("BUTTON", "아래로", tab, SLOT_DOWN, 615, 439, 107, 27),
        (
            "BUTTON",
            "배치에서 제거",
            tab,
            SLOT_DELETE,
            615,
            472,
            107,
            27,
        ),
        ("COMBOBOX", "", combo, AVAILABLE, 226, 530, 379, 173),
        ("BUTTON", "휠에 추가", tab, SLOT_ADD, 615, 530, 107, 27),
        ("BUTTON", "저장", tab, SAVE, 522, 567, 92, 27),
        ("BUTTON", "닫기", tab, CANCEL, 630, 567, 92, 27),
    ];
    for index in 0..4 {
        let y = 73 + i32::from(index) * 28;
        fields.push((
            "STATIC",
            "",
            child,
            OPTION_LABEL + index,
            226,
            y + 4,
            241,
            20,
        ));
        fields.push(("EDIT", "", edit, OPTION + index, 469, y, 253, 23));
    }
    for (class, label, style, id, x, y, width, height) in fields {
        let handle = unsafe {
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
                super::handle(id),
                instance,
                null(),
            )
        };
        unsafe {
            SendMessageW(
                handle,
                WM_SETFONT,
                GetStockObject(DEFAULT_GUI_FONT) as usize,
                1,
            )
        };
    }
    let mut editor = state.borrow_mut();
    for action in &editor.catalog.actions {
        let label = wide(format!("{}  ·  {}", action.label, action.key));
        send(window, ACTION, CB_ADDSTRING, 0, label.as_ptr() as isize);
    }
    for (id, labels) in [
        (
            LOCATION,
            &["기본 설정 사용", "원본과 같은 폴더", "지정한 폴더"][..],
        ),
        (COLLISION, &["번호를 붙여 저장", "오류 표시"][..]),
        (MODE, &["Shift 변환", "Ctrl 빠른 액션"][..]),
    ] {
        for label in labels {
            add(window, id, CB_ADDSTRING, label);
        }
    }
    editor.list(window, 0);
    editor.profile_list(window, 0);
    drop(editor);
    use windows_sys::Win32::UI::Input::KeyboardAndMouse::EnableWindow;
    unsafe {
        EnableWindow(ui.main, 0);
        ShowWindow(window, SW_SHOW);
    }
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
    unsafe { DestroyWindow(window) };
    let editor = state.into_inner();
    unsafe {
        EnableWindow(ui.main, 1);
        SetForegroundWindow(ui.main);
    }
    if editor.saved {
        let mut profile = ui.profile.clone();
        profile["presets"] = json!(editor.presets);
        profile["profiles"] = json!(editor.profiles);
        for (_, field, mode) in Ui::WHEEL_SELECTORS {
            if !profile["profiles"]
                .as_array()
                .unwrap()
                .iter()
                .any(|item| item["mode"] == mode && item["id"] == profile[field])
            {
                profile[field] = json!("");
            }
        }
        ui.save_profile(profile)?;
        let unsaved_output = read_text(control(ui.main, OUTPUT));
        let checkboxes = [ENABLED, RELEASE].map(|id| send(ui.main, id, BM_GETCHECK, 0, 0));
        ui.update_settings();
        text(control(ui.main, OUTPUT), &unsaved_output);
        for (id, value) in [ENABLED, RELEASE].into_iter().zip(checkboxes) {
            send(ui.main, id, BM_SETCHECK, value as usize, 0);
        }
        if !ui.paths.is_empty() {
            ui.set_selection(ui.paths.clone(), ui.web.clone());
        }
        text(
            control(ui.main, STATUS),
            "프리셋과 휠 프로필을 저장했습니다.",
        );
    }
    Ok(())
}
