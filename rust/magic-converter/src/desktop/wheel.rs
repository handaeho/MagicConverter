use super::{Ui, wide};
use crate::web;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::f64::consts::{FRAC_PI_2, TAU};
use std::io;
use std::path::PathBuf;
use std::ptr::null_mut;
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::System::Threading::{
    OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, QueryFullProcessImageNameW,
};
use windows_sys::Win32::UI::HiDpi::GetDpiForWindow;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    GetAsyncKeyState, VK_CONTROL, VK_ESCAPE, VK_LBUTTON, VK_LWIN, VK_MENU, VK_RWIN, VK_SHIFT,
};
use windows_sys::Win32::UI::WindowsAndMessaging::*;

struct Slot {
    recipe: Value,
    ready: bool,
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Mode {
    Convert,
    Action,
}

fn modifier_mode(shift: bool, control: bool) -> Option<Mode> {
    if control {
        Some(Mode::Action)
    } else if shift {
        Some(Mode::Convert)
    } else {
        None
    }
}

#[derive(Default)]
pub(super) struct Wheel {
    pub(super) window: HWND,
    origin: Option<(i32, i32)>,
    dragging: bool,
    consumed: bool,
    shortcut: ShortcutGuard,
    mode: Option<Mode>,
    visible: bool,
    paths: Vec<PathBuf>,
    web: Vec<web::Input>,
    refresh_slots: bool,
    hover: Option<usize>,
    inside: bool,
    released: bool,
    hide_at: Option<Instant>,
    profile: Value,
    slots: Vec<Slot>,
    size: i32,
}

fn down(key: u16) -> bool {
    unsafe { GetAsyncKeyState(i32::from(key)) < 0 }
}

fn system_modifier_down() -> bool {
    [VK_LWIN, VK_RWIN, VK_MENU].into_iter().any(down)
}

#[derive(Default)]
struct ShortcutGuard {
    blocked: bool,
}

impl ShortcutGuard {
    fn update(&mut self, system: bool, mouse: bool, drag_modifier: bool) -> bool {
        if system {
            self.blocked = true;
        } else if !mouse && !drag_modifier {
            self.blocked = false;
        }
        self.blocked
    }
}

fn screen_capture_process(path: &str) -> bool {
    let name = path.rsplit(['\\', '/']).next().unwrap_or_default();
    [
        "ScreenClippingHost.exe",
        "SnippingTool.exe",
        "ScreenSketch.exe",
    ]
    .iter()
    .any(|capture| name.eq_ignore_ascii_case(capture))
}

fn screen_capture_active() -> bool {
    unsafe {
        let window = GetForegroundWindow();
        let mut pid = 0;
        if window.is_null() || GetWindowThreadProcessId(window, &mut pid) == 0 {
            return false;
        }
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if process.is_null() {
            return false;
        }
        let mut image = [0_u16; 1024];
        let mut length = image.len() as u32;
        let found = QueryFullProcessImageNameW(process, 0, image.as_mut_ptr(), &mut length);
        CloseHandle(process);
        found != 0 && screen_capture_process(&String::from_utf16_lossy(&image[..length as usize]))
    }
}

impl Wheel {
    fn hide(&mut self) {
        if self.visible {
            unsafe { ShowWindow(self.window, SW_HIDE) };
        }
        self.visible = false;
        self.hover = None;
        self.inside = false;
        self.paths.clear();
        self.web.clear();
        self.slots.clear();
        self.released = false;
        self.hide_at = None;
    }

    fn requires_release(&self) -> bool {
        self.profile["release_modifier_inside"]
            .as_bool()
            .unwrap_or(false)
    }

    fn invalidate(&self) {
        unsafe { InvalidateRect(self.window, std::ptr::null(), 0) };
    }

    fn refresh(&mut self) {
        let selector = if self.mode == Some(Mode::Action) {
            "action_profile"
        } else {
            "convert_profile"
        };
        let profile = self.profile["profiles"]
            .as_array()
            .into_iter()
            .flatten()
            .find(|profile| profile["id"] == self.profile[selector]);
        self.slots = profile
            .and_then(|item| item["preset_ids"].as_array())
            .into_iter()
            .flatten()
            .filter_map(|id| {
                self.profile["presets"]
                    .as_array()?
                    .iter()
                    .find(|preset| preset["id"] == *id)
            })
            .take(12)
            .map(|recipe| Slot {
                recipe: recipe.clone(),
                ready: false,
            })
            .collect();
        self.refresh_slots = true;
        self.invalidate();
    }

    fn automatic(&self) -> bool {
        let selector = if self.mode == Some(Mode::Action) {
            "action_profile"
        } else {
            "convert_profile"
        };
        self.profile[selector]
            .as_str()
            .unwrap_or_default()
            .is_empty()
    }

    pub(super) fn poll(&mut self, profile: &Value) {
        if !profile["enabled"].as_bool().unwrap_or(true) {
            self.hide();
            self.origin = None;
            self.mode = None;
            self.dragging = false;
            return;
        }
        let mouse = down(VK_LBUTTON);
        let mode = modifier_mode(down(VK_SHIFT), down(VK_CONTROL));
        // Reserve the whole gesture for Windows until its keys and the mouse
        // button are released, including mouse selection after Win is lifted.
        if self
            .shortcut
            .update(system_modifier_down(), mouse, mode.is_some())
        {
            self.hide();
            self.origin = None;
            self.mode = None;
            self.dragging = false;
            self.consumed = false;
            return;
        }
        if self
            .hide_at
            .is_some_and(|deadline| Instant::now() >= deadline)
        {
            self.hide();
        }
        if !mouse {
            if self.visible && self.hide_at.is_none() {
                if self.inside {
                    self.hide_at = Some(
                        Instant::now()
                            + Duration::from_millis(if self.requires_release() {
                                30_000
                            } else {
                                100
                            }),
                    );
                } else {
                    self.hide();
                }
            }
            self.origin = None;
            self.mode = None;
            self.dragging = false;
            self.consumed = false;
            return;
        }
        if down(VK_ESCAPE) {
            self.consumed = true;
            self.hide();
            return;
        }
        let mut cursor = POINT::default();
        if unsafe { GetCursorPos(&mut cursor) } == 0 {
            return;
        }
        let (x, y) = *self.origin.get_or_insert((cursor.x, cursor.y));
        let (dx, dy) = (
            i64::from(x) - i64::from(cursor.x),
            i64::from(y) - i64::from(cursor.y),
        );
        let threshold = profile["threshold"].as_i64().unwrap_or(15).clamp(1, 1000);
        self.dragging |= dx * dx + dy * dy >= threshold * threshold;
        if mode.is_none() {
            if self.mode.is_some() && self.visible {
                if self.requires_release() && self.inside && !self.paths.is_empty() {
                    self.released = true;
                    self.invalidate();
                } else {
                    self.hide();
                }
            }
            self.mode = None;
            return;
        }
        if self.dragging && !self.consumed && self.mode != mode {
            if screen_capture_active() {
                self.shortcut.update(true, mouse, true);
                self.hide();
                self.mode = None;
                return;
            }
            self.mode = mode;
            self.released = false;
            if !self.visible {
                self.profile = profile.clone();
                self.size = 320;
                self.move_to(cursor);
                let dpi = unsafe { GetDpiForWindow(self.window) }.max(96);
                self.size = (320 * dpi / 96) as i32;
                self.move_to(cursor);
                let clip = unsafe { CreateEllipticRgn(0, 0, self.size, self.size) };
                if unsafe { SetWindowRgn(self.window, clip, 0) } == 0 {
                    unsafe { DeleteObject(clip) };
                }
                self.visible = true;
                unsafe { ShowWindow(self.window, SW_SHOWNOACTIVATE) };
            }
            self.refresh();
        }
    }

    fn move_to(&self, cursor: POINT) {
        unsafe {
            SetWindowPos(
                self.window,
                HWND_TOPMOST,
                cursor.x - self.size / 2,
                cursor.y - self.size / 2,
                self.size,
                self.size,
                SWP_NOACTIVATE,
            )
        };
    }

    fn slot_at(&self, x: i32, y: i32) -> Option<usize> {
        let mut point = POINT { x, y };
        unsafe { ScreenToClient(self.window, &mut point) };
        self.local_slot(point.x, point.y)
    }

    fn local_slot(&self, x: i32, y: i32) -> Option<usize> {
        let scale = f64::from(self.size) / 320.0;
        let dx = f64::from(x) / scale - 160.0;
        let dy = f64::from(y) / scale - 160.0;
        if self.slots.is_empty() || !(55.0..=149.0).contains(&dx.hypot(dy)) {
            return None;
        }
        let step = TAU / self.slots.len() as f64;
        Some(
            (((dy.atan2(dx) + FRAC_PI_2 + step / 2.0).rem_euclid(TAU) / step) as usize)
                .min(self.slots.len() - 1),
        )
    }

    fn eligible(&self, hover: Option<usize>, modifiers: bool) -> bool {
        self.visible
            && hover.is_some_and(|index| self.slots[index].ready)
            && (!self.requires_release() || (self.released && !modifiers))
    }
}

pub(super) fn poll(ui: &mut Ui) {
    ui.wheel.poll(&ui.profile);
    if std::mem::take(&mut ui.wheel.refresh_slots) && ui.wheel.inside {
        assess_slots(ui);
    }
}

fn assess_slots(ui: &mut Ui) {
    let disabled = ui.disabled();
    if ui.wheel.automatic() {
        let action_mode = ui.wheel.mode == Some(Mode::Action);
        ui.wheel.slots = ui.catalog.actions.iter().filter(|action| {
            let tool = action.key.starts_with("tool:");
            let appropriate = if action_mode {
                tool || matches!(action.key.as_str(), "strip" | "zip" | "copy_path" | "terminal")
            } else {
                !tool && !matches!(action.key.as_str(), "copy_path" | "terminal" | "zip")
            };
            appropriate && ui.available(&ui.wheel.paths, &action.key, &disabled)
        }).filter(|action| ui.wheel.web.is_empty() || (action.key != "tool:pdf_merge" && !super::quick::is_local(&action.key)))
            .take(12)
            .enumerate()
            .map(|(index, action)| Slot {
                recipe: json!({"id": format!("automatic-{index}"), "name": action.label, "action_key": action.key}),
                ready: true,
            })
            .collect();
    }
    let readiness: Vec<_> = ui
        .wheel
        .slots
        .iter()
        .map(|slot| {
            slot.recipe["action_key"].as_str().is_some_and(|key| {
                (ui.wheel.web.is_empty()
                    || (key != "tool:pdf_merge" && !super::quick::is_local(key)))
                    && ui.available(&ui.wheel.paths, key, &disabled)
            })
        })
        .collect();
    for (slot, ready) in ui.wheel.slots.iter_mut().zip(readiness) {
        slot.ready = ready;
    }
    ui.wheel.invalidate();
}

pub(super) fn entered(ui: &mut Ui, paths: Vec<PathBuf>, web: Vec<web::Input>) {
    ui.wheel.paths = if web.is_empty() {
        paths
    } else {
        web.iter().map(web::Input::placeholder).collect()
    };
    ui.wheel.web = web;
    ui.wheel.inside = true;
    assess_slots(ui);
}

pub(super) fn drag(ui: &mut Ui, x: i32, y: i32, modifiers: bool) -> bool {
    let wheel = &mut ui.wheel;
    if wheel.shortcut.blocked || system_modifier_down() {
        wheel.shortcut.update(true, true, modifiers);
        wheel.hide();
        return false;
    }
    let hover = wheel.slot_at(x, y);
    if !modifiers
        && wheel.mode.is_some()
        && wheel.requires_release()
        && wheel.inside
        && !wheel.paths.is_empty()
        && !wheel.released
    {
        wheel.released = true;
        wheel.invalidate();
    }
    if hover != wheel.hover {
        wheel.hover = hover;
        wheel.invalidate();
    }
    wheel.eligible(hover, modifiers)
}

pub(super) fn dropped(
    ui: &mut Ui,
    paths: Vec<PathBuf>,
    web: Vec<web::Input>,
    x: i32,
    y: i32,
    modifiers: bool,
) -> bool {
    let same = if web.is_empty() {
        paths == ui.wheel.paths
    } else {
        web == ui.wheel.web
    };
    if !same || !drag(ui, x, y, modifiers) {
        ui.wheel.hide();
        ui.wheel.consumed = true;
        return false;
    }
    let mut recipe = ui.wheel.slots[ui.wheel.hover.expect("supported selection")]
        .recipe
        .clone();
    if recipe["output"].is_null() {
        recipe["output"] = ui.wheel.profile["output"].clone();
    }
    let paths = std::mem::take(&mut ui.wheel.paths);
    let web = std::mem::take(&mut ui.wheel.web);
    ui.wheel.hide();
    ui.wheel.consumed = true;
    if recipe["action_key"]
        .as_str()
        .is_some_and(super::quick::is_local)
    {
        return ui.submit(paths, web, recipe);
    }
    match super::ole::snapshot(paths, web) {
        Ok((paths, web)) => ui.submit(paths, web, recipe),
        Err(error) => {
            ui.drop_error(&error);
            false
        }
    }
}

pub(super) fn left(ui: &mut Ui) {
    ui.wheel.inside = false;
    ui.wheel.hover = None;
    ui.wheel.paths.clear();
    ui.wheel.web.clear();
    if ui.wheel.automatic() {
        ui.wheel.slots.clear();
    }
    if ui.wheel.released {
        ui.wheel.hide();
    } else {
        ui.wheel.invalidate();
    }
}

fn brush(dc: HDC, color: u32, draw: impl FnOnce()) {
    unsafe {
        let brush = CreateSolidBrush(color);
        let old = SelectObject(dc, brush);
        draw();
        SelectObject(dc, old);
        DeleteObject(brush);
    }
}

fn rgb(r: u32, g: u32, b: u32) -> u32 {
    r | g << 8 | b << 16
}

fn paint(window: HWND, wheel: &Wheel) {
    let mut info = PAINTSTRUCT::default();
    let dc = unsafe { BeginPaint(window, &mut info) };
    if dc.is_null() {
        return;
    }
    let scale = f64::from(wheel.size) / 320.0;
    let p = |n: f64| (n * scale).round() as i32;
    unsafe {
        SelectObject(dc, GetStockObject(NULL_PEN));
        SelectObject(dc, GetStockObject(DEFAULT_GUI_FONT));
        SetBkMode(dc, TRANSPARENT as i32);
        SetArcDirection(dc, AD_CLOCKWISE);
    }
    brush(dc, rgb(36, 39, 44), || unsafe {
        Ellipse(dc, 0, 0, p(320.0), p(320.0));
    });
    if !wheel.slots.is_empty() {
        let step = TAU / wheel.slots.len() as f64;
        for (index, slot) in wheel.slots.iter().enumerate() {
            let middle = -FRAC_PI_2 + index as f64 * step;
            let a = middle - step / 2.0 + 0.012;
            let b = middle + step / 2.0 - 0.012;
            let edge = |angle: f64| {
                (
                    p(160.0 + angle.cos() * 150.0),
                    p(160.0 + angle.sin() * 150.0),
                )
            };
            let (ax, ay) = edge(a);
            let (bx, by) = edge(b);
            let color = if wheel.hover == Some(index) && slot.ready {
                rgb(223, 105, 51)
            } else if slot.ready {
                rgb(149, 72, 43)
            } else {
                rgb(72, 75, 80)
            };
            brush(dc, color, || unsafe {
                Pie(dc, p(11.0), p(11.0), p(309.0), p(309.0), ax, ay, bx, by);
            });
        }
        for (index, slot) in wheel.slots.iter().enumerate() {
            let angle = -FRAC_PI_2 + index as f64 * step;
            let (x, y) = (160.0 + angle.cos() * 106.0, 160.0 + angle.sin() * 106.0);
            let name = wide(slot.recipe["name"].as_str().unwrap_or_default());
            let mut bounds = RECT {
                left: p(x - 45.0),
                top: p(y - 10.0),
                right: p(x + 45.0),
                bottom: p(y + 10.0),
            };
            unsafe {
                SetTextColor(
                    dc,
                    if slot.ready {
                        rgb(255, 249, 244)
                    } else {
                        rgb(186, 187, 190)
                    },
                );
                DrawTextW(
                    dc,
                    name.as_ptr(),
                    name.len() as i32 - 1,
                    &mut bounds,
                    DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_END_ELLIPSIS,
                );
            }
        }
    }
    brush(dc, rgb(36, 39, 44), || unsafe {
        Ellipse(dc, p(104.0), p(104.0), p(216.0), p(216.0));
    });
    let title = wide(
        if wheel.requires_release() && wheel.inside && !wheel.released {
            "보조키 놓기"
        } else if wheel.mode == Some(Mode::Action) {
            "빠른 액션"
        } else {
            "변환"
        },
    );
    let mut title_rect = RECT {
        left: p(107.0),
        right: p(213.0),
        top: p(147.0),
        bottom: p(173.0),
    };
    unsafe {
        SetTextColor(dc, rgb(247, 244, 242));
        DrawTextW(
            dc,
            title.as_ptr(),
            title.len() as i32 - 1,
            &mut title_rect,
            DT_CENTER | DT_VCENTER | DT_SINGLELINE,
        );
        EndPaint(window, &info);
    }
}

unsafe extern "system" fn window_proc(
    window: HWND,
    message: u32,
    wp: WPARAM,
    lp: LPARAM,
) -> LRESULT {
    if message == WM_NCCREATE {
        let create = unsafe { &*(lp as *const CREATESTRUCTW) };
        unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize) };
        return unsafe { DefWindowProcW(window, message, wp, lp) };
    }
    if message == WM_PAINT {
        let pointer = unsafe { GetWindowLongPtrW(window, GWLP_USERDATA) } as *const RefCell<Ui>;
        if let Some(state) = unsafe { pointer.as_ref() }
            && let Ok(ui) = state.try_borrow()
        {
            paint(window, &ui.wheel);
            return 0;
        }
    }
    unsafe { DefWindowProcW(window, message, wp, lp) }
}

pub(super) fn window(state: *const RefCell<Ui>, instance: HINSTANCE) -> io::Result<HWND> {
    let class = wide("MagicConverterRustWheel");
    let definition = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        lpszClassName: class.as_ptr(),
        hCursor: unsafe { LoadCursorW(null_mut(), IDC_ARROW) },
        ..Default::default()
    };
    if unsafe { RegisterClassW(&definition) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let window = unsafe {
        CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            class.as_ptr(),
            wide("MagicConverter 변환 휠").as_ptr(),
            WS_POPUP,
            0,
            0,
            320,
            320,
            null_mut(),
            null_mut(),
            instance,
            state.cast(),
        )
    };
    if window.is_null() {
        Err(io::Error::last_os_error())
    } else {
        Ok(window)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn system_chord_reserves_mouse_gesture_until_all_buttons_are_up() {
        let mut shortcut = ShortcutGuard::default();
        assert!(!shortcut.update(false, true, true));
        assert!(shortcut.update(true, false, true));
        assert!(shortcut.update(false, true, true));
        assert!(shortcut.update(false, true, false));
        assert!(shortcut.update(false, false, true));
        assert!(!shortcut.update(false, false, false));
        assert!(!shortcut.update(false, true, true));
    }

    #[test]
    fn recognizes_windows_capture_hosts_by_executable() {
        assert!(screen_capture_process(
            r"C:\Windows\System32\ScreenClippingHost.exe"
        ));
        assert!(screen_capture_process(r"C:\WindowsApps\SNIPPINGTOOL.EXE"));
        assert!(screen_capture_process("ScreenSketch.exe"));
        assert!(!screen_capture_process(
            r"C:\Program Files\Browser\browser.exe"
        ));
    }

    #[test]
    fn shift_and_control_select_distinct_profiles_and_gate_release() {
        let profile = json!({
            "release_modifier_inside": true,
            "convert_profile": "convert",
            "action_profile": "actions",
            "profiles": [
                {"id": "convert", "preset_ids": ["png", "webp"]},
                {"id": "actions", "preset_ids": ["copy"]}
            ],
            "presets": [
                {"id": "png", "action_key": "image:png"},
                {"id": "webp", "action_key": "image:webp"},
                {"id": "copy", "action_key": "copy"},
            ]
        });
        let mut wheel = Wheel {
            profile,
            visible: true,
            size: 480,
            ..Default::default()
        };
        wheel.mode = modifier_mode(true, false);
        wheel.refresh();
        assert_eq!(wheel.slots[0].recipe["action_key"], "image:png");
        assert_eq!(wheel.slots[1].recipe["action_key"], "image:webp");
        assert_eq!(wheel.local_slot(240, 90), Some(0));
        assert_eq!(wheel.local_slot(240, 390), Some(1));
        assert_eq!(wheel.local_slot(240, 240), None);
        wheel.slots[0].ready = true;
        assert!(!wheel.eligible(Some(0), true));
        assert!(!wheel.eligible(Some(0), false));
        wheel.released = true;
        assert!(wheel.eligible(Some(0), false));
        assert!(!wheel.eligible(Some(1), false));
        wheel.mode = modifier_mode(true, true);
        assert!(wheel.mode == modifier_mode(false, true));
        wheel.refresh();
        assert_eq!(wheel.slots.len(), 1);
        assert_eq!(wheel.slots[0].recipe["action_key"], "copy");
        assert!(!wheel.eligible(Some(0), false));
        assert!(modifier_mode(false, false).is_none());
    }
}
