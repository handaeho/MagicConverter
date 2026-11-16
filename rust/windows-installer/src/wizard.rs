use crate::{
    FOLDER, PRODUCT, Result, VERSION, install, install_folder, platform, remove_installation,
};
use serde_json::{Value, json};
use std::cell::RefCell;
use std::env;
use std::ffi::OsStr;
use std::io;
use std::os::windows::ffi::OsStrExt;
use std::path::PathBuf;
use std::ptr::{null, null_mut};
use std::sync::mpsc::{self, Receiver, TryRecvError};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::SystemServices::SS_NOPREFIX;
use windows_sys::Win32::UI::Controls::*;
use windows_sys::Win32::UI::HiDpi::*;
use windows_sys::Win32::UI::Input::KeyboardAndMouse::{
    EnableWindow, GetFocus, SetFocus, VK_ESCAPE, VK_RETURN,
};
use windows_sys::Win32::UI::WindowsAndMessaging::*;

const WIDTH: i32 = 640;
const HEIGHT: i32 = 450;
const FOOTER: i32 = 396;
const BACK: u16 = 100;
const NEXT: u16 = IDOK as u16;
const CANCEL: u16 = IDCANCEL as u16;
const AGREE: u16 = 101;
const PARENT: u16 = 102;
const DESTINATION: u16 = 103;
const BROWSE: u16 = 104;
const LAUNCH: u16 = 105;
const STATUS: u16 = 106;
const BAR: u16 = 107;

#[derive(Clone, Copy, Eq, PartialEq)]
pub(super) enum Mode {
    Install,
    Remove,
}

impl Mode {
    fn name(self) -> &'static str {
        match self {
            Self::Install => "설치",
            Self::Remove => "제거",
        }
    }
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum Page {
    Welcome,
    License,
    Location,
    Review,
    Progress,
    Finished,
}

enum Work {
    Progress(usize, usize),
    Finished(std::result::Result<Value, String>),
}

struct Wizard {
    window: HWND,
    mode: Mode,
    page: Page,
    folder: PathBuf,
    parent: String,
    agreed: bool,
    children: Vec<HWND>,
    dpi: i32,
    body_font: HFONT,
    title_font: HFONT,
    icon: HICON,
    work: Option<Receiver<Work>>,
    answer: Value,
}

fn wide(text: impl AsRef<OsStr>) -> Vec<u16> {
    text.as_ref().encode_wide().chain([0]).collect()
}

fn text(window: HWND) -> String {
    let length = unsafe { GetWindowTextLengthW(window) }.max(0);
    let mut buffer = vec![0; length as usize + 1];
    let read = unsafe { GetWindowTextW(window, buffer.as_mut_ptr(), length + 1) }.max(0);
    String::from_utf16_lossy(&buffer[..read as usize])
}

fn set_text(window: HWND, value: &str) {
    unsafe { SetWindowTextW(window, wide(value).as_ptr()) };
}

fn brand_icon(size: i32) -> HICON {
    let icon = include_bytes!("../../../assets/magic_converter-tool.ico");
    let count = u16::from_le_bytes([icon[4], icon[5]]) as usize;
    let entry = icon[6..].chunks_exact(16).take(count).min_by_key(|entry| {
        let width = if entry[0] == 0 {
            256
        } else {
            i32::from(entry[0])
        };
        (width - size).abs()
    });
    let Some(entry) = entry else {
        return null_mut();
    };
    let length = u32::from_le_bytes(entry[8..12].try_into().unwrap());
    let offset = u32::from_le_bytes(entry[12..16].try_into().unwrap()) as usize;
    let Some(data) = icon.get(offset..offset + length as usize) else {
        return null_mut();
    };
    unsafe {
        CreateIconFromResourceEx(
            data.as_ptr().cast_mut(),
            length,
            1,
            0x0003_0000,
            size,
            size,
            0,
        )
    }
}

impl Wizard {
    fn scale(&self, value: i32) -> i32 {
        (value * self.dpi + 48) / 96
    }

    fn control(&self, id: u16) -> HWND {
        unsafe { GetDlgItem(self.window, i32::from(id)) }
    }

    fn add(&mut self, class: &str, title: &str, style: u32, id: u16, rect: [i32; 4]) -> HWND {
        let white_background =
            class == "EDIT" || (rect[1] < FOOTER && self.banner()) || rect[1] < 82;
        let [x, y, width, height] = rect.map(|value| self.scale(value));
        let handle = unsafe {
            CreateWindowExW(
                0,
                wide(class).as_ptr(),
                wide(title).as_ptr(),
                WS_CHILD | WS_VISIBLE | style,
                x,
                y,
                width,
                height,
                self.window,
                usize::from(id) as HMENU,
                null_mut(),
                null(),
            )
        };
        if !handle.is_null() {
            let font = if self.body_font.is_null() {
                unsafe { GetStockObject(DEFAULT_GUI_FONT) }
            } else {
                self.body_font
            };
            unsafe {
                SetWindowLongPtrW(handle, GWLP_USERDATA, isize::from(white_background));
                SendMessageW(handle, WM_SETFONT, font as usize, 1);
            }
            self.children.push(handle);
        }
        handle
    }

    fn label(&mut self, title: &str, rect: [i32; 4]) {
        self.add("STATIC", title, SS_NOPREFIX as u32, 0, rect);
    }

    fn heading(&mut self, title: &str, rect: [i32; 4]) {
        let label = self.add("STATIC", title, SS_NOPREFIX as u32, 0, rect);
        if !self.title_font.is_null() {
            unsafe { SendMessageW(label, WM_SETFONT, self.title_font as usize, 1) };
        }
    }

    fn button(&mut self, title: &str, id: u16, rect: [i32; 4]) -> HWND {
        let style = if id == NEXT {
            BS_DEFPUSHBUTTON
        } else {
            BS_PUSHBUTTON
        };
        self.add("BUTTON", title, WS_TABSTOP | style as u32, id, rect)
    }

    fn editor(&mut self, value: &str, id: u16, rect: [i32; 4], multiline: bool, editable: bool) {
        let style = if multiline {
            ES_MULTILINE as u32 | ES_AUTOVSCROLL as u32 | WS_VSCROLL
        } else {
            ES_AUTOHSCROLL as u32
        };
        let readonly = if editable { 0 } else { ES_READONLY as u32 };
        self.add(
            "EDIT",
            value,
            WS_BORDER | WS_TABSTOP | style | readonly,
            id,
            rect,
        );
    }

    fn checked(&self, id: u16) -> bool {
        unsafe { SendMessageW(self.control(id), BM_GETCHECK, 0, 0) != 0 }
    }

    fn check(&mut self, title: &str, id: u16, rect: [i32; 4], checked: bool) {
        let input = self.add(
            "BUTTON",
            title,
            WS_TABSTOP | BS_AUTOCHECKBOX as u32,
            id,
            rect,
        );
        unsafe { SendMessageW(input, BM_SETCHECK, usize::from(checked), 0) };
    }

    fn banner(&self) -> bool {
        self.page == Page::Welcome || (self.page == Page::Finished && self.answer["ok"] == true)
    }

    fn can_retry_removal(&self) -> bool {
        self.mode == Mode::Remove && self.page == Page::Finished && self.answer["ok"] == false
    }

    fn step(&mut self, title: &str, subtitle: &str) {
        self.heading(title, [22, 12, 518, 25]);
        self.label(subtitle, [38, 42, 508, 34]);
    }

    fn render(&mut self) {
        for child in self.children.drain(..) {
            unsafe { DestroyWindow(child) };
        }
        let action = self.mode.name();
        match self.page {
            Page::Welcome => {
                self.heading(
                    &format!("{PRODUCT} {VERSION}\r\n{action} 마법사에 오신 것을 환영합니다"),
                    [222, 30, 394, 70],
                );
                let description = match self.mode {
                    Mode::Install => {
                        "이 마법사는 Windows 64비트용 MagicConverter를 설치합니다. 인터넷 연결 없이 설치할 수 있습니다.\r\n\r\n계속하려면 다음을 누르세요."
                    }
                    Mode::Remove => {
                        "진행 중인 변환을 마치고 MagicConverter를 종료한 뒤 다음을 누르세요."
                    }
                };
                self.label(description, [222, 115, 392, 175]);
            }
            Page::License => {
                self.step("라이선스 계약", "계속하려면 조건을 읽고 동의해 주세요.");
                let license = include_str!("../../../LICENSE")
                    .replace("\r\n", "\n")
                    .replace('\n', "\r\n");
                self.editor(&license, 0, [34, 101, 572, 234], true, false);
                self.check(
                    "라이선스 조건에 동의합니다.",
                    AGREE,
                    [34, 348, 538, 28],
                    self.agreed,
                );
            }
            Page::Location => {
                self.step(
                    "설치 위치 선택",
                    "선택한 상위 폴더에 magic-converter 폴더를 만듭니다.",
                );
                self.label("설치 구성", [34, 101, 572, 23]);
                self.editor(
                    "MagicConverter\r\nFFmpeg·FFprobe 및 rhwp\r\n문서·PDF·이미지 변환 엔진",
                    0,
                    [34, 126, 572, 81],
                    true,
                    false,
                );
                self.label("상위 폴더", [34, 226, 572, 22]);
                self.editor(
                    &self.parent.clone(),
                    PARENT,
                    [34, 252, 453, 27],
                    false,
                    true,
                );
                self.button("찾아보기…", BROWSE, [499, 249, 107, 31]);
                self.label("실제 설치 위치", [34, 301, 572, 23]);
                self.editor("", DESTINATION, [34, 326, 572, 28], false, false);
                self.preview_path();
            }
            Page::Review => {
                self.step(
                    &format!("{action} 준비 완료"),
                    &format!("{action}할 위치를 확인하세요."),
                );
                self.label("설치 위치", [34, 103, 572, 24]);
                self.editor(
                    &self.folder.display().to_string(),
                    0,
                    [34, 131, 572, 28],
                    false,
                    false,
                );
                let description = match self.mode {
                    Mode::Install => {
                        "설치를 시작하려면 설치를 누르세요. 위치를 바꾸려면 이전을 누르세요."
                    }
                    Mode::Remove => {
                        "MagicConverter 설치 파일과 바로가기를 제거합니다. 사용자 설정, 변환 기록, 원본과 변환 결과는 보존합니다."
                    }
                };
                self.label(description, [34, 177, 570, 105]);
            }
            Page::Progress => {
                self.step(&format!("{action} 중"), "작업이 끝날 때까지 기다려 주세요.");
                self.add(
                    "STATIC",
                    "파일 확인 중",
                    SS_NOPREFIX as u32,
                    STATUS,
                    [34, 108, 572, 31],
                );
                self.add("msctls_progress32", "", 0, BAR, [34, 148, 572, 24]);
            }
            Page::Finished => {
                if self.answer["ok"] == true {
                    self.heading(&format!("{action} 완료"), [222, 30, 394, 52]);
                    self.label(&format!("{PRODUCT} {action}를 완료했습니다.\r\n\r\n마침을 눌러 마법사를 닫으세요."), [222, 94, 380, 160]);
                    if self.mode == Mode::Install {
                        self.check("MagicConverter 실행", LAUNCH, [222, 323, 370, 27], true);
                    }
                } else {
                    let guidance = if self.can_retry_removal() {
                        "아래 사유를 해결한 뒤 ‘다시 시도’를 누르세요."
                    } else {
                        "아래 오류 내용을 확인하세요."
                    };
                    self.step(&format!("{action}를 완료하지 못했습니다"), guidance);
                    let error = self.answer["error"]
                        .as_str()
                        .unwrap_or("작업에 실패했습니다.")
                        .to_owned();
                    self.editor(&error, 0, [34, 103, 572, 246], true, false);
                }
            }
        }
        let caption = match self.page {
            Page::Review => action,
            Page::Finished if self.answer["ok"] == true => "마침",
            Page::Finished if self.can_retry_removal() => "다시 시도",
            Page::Finished => "닫기",
            _ => "다음 >",
        };
        let back = self.button("< 이전", BACK, [337, 407, 90, 29]);
        let next = self.button(caption, NEXT, [435, 407, 90, 29]);
        let close = if self.can_retry_removal() {
            "닫기"
        } else {
            "취소"
        };
        let cancel = self.button(close, CANCEL, [533, 407, 90, 29]);
        let can_advance =
            self.page != Page::Progress && (self.page != Page::License || self.agreed);
        unsafe {
            EnableWindow(
                back,
                i32::from(matches!(
                    self.page,
                    Page::License | Page::Location | Page::Review
                )),
            );
            EnableWindow(next, i32::from(can_advance));
            EnableWindow(
                cancel,
                i32::from(
                    !matches!(self.page, Page::Progress | Page::Finished)
                        || self.can_retry_removal(),
                ),
            );
            if can_advance {
                SetFocus(next);
            }
            InvalidateRect(self.window, null(), 1);
        }
    }

    fn preview_path(&self) {
        let path = text(self.control(PARENT));
        let path = path.trim();
        let preview = if path.is_empty() {
            String::new()
        } else {
            PathBuf::from(path).join(FOLDER).display().to_string()
        };
        set_text(self.control(DESTINATION), &preview);
    }

    fn save_parent(&mut self) {
        self.parent = text(self.control(PARENT));
    }

    fn start(&mut self) -> Result<()> {
        let (sender, receiver) = mpsc::channel();
        let folder = self.folder.clone();
        let mode = self.mode;
        let exe = env::current_exe()?;
        if unsafe { SetTimer(self.window, 1, 60, None) } == 0 {
            return Err(io::Error::last_os_error().into());
        }
        if let Err(error) = std::thread::Builder::new().spawn(move || {
            let progress = |done, total| {
                let _ = sender.send(Work::Progress(done, total));
            };
            let result = match mode {
                Mode::Install => install(&exe, &folder, progress),
                Mode::Remove => remove_installation(&folder, progress),
            };
            let _ = sender.send(Work::Finished(result.map_err(|error| error.to_string())));
        }) {
            unsafe { KillTimer(self.window, 1) };
            return Err(error.into());
        }
        self.work = Some(receiver);
        self.page = Page::Progress;
        self.render();
        Ok(())
    }

    fn tick(&mut self) {
        let Some(work) = &self.work else { return };
        while let Ok(event) = work.try_recv().or_else(|error| match error {
            TryRecvError::Empty => Err(error),
            TryRecvError::Disconnected => Ok(Work::Finished(Err("작업이 중단되었습니다.".into()))),
        }) {
            match event {
                Work::Progress(done, total) => {
                    set_text(
                        self.control(STATUS),
                        &format!(
                            "파일 {} 중 ({done}/{total})",
                            if self.mode == Mode::Remove {
                                "제거"
                            } else {
                                "확인"
                            }
                        ),
                    );
                    unsafe { SendMessageW(self.control(BAR), PBM_SETPOS, done * 100 / total, 0) };
                }
                Work::Finished(result) => {
                    unsafe { KillTimer(self.window, 1) };
                    self.work = None;
                    self.answer =
                        result.unwrap_or_else(|error| json!({"ok": false, "error": error}));
                    self.page = Page::Finished;
                    self.render();
                    return;
                }
            }
        }
    }

    fn command(&mut self, id: u16, notification: u16) -> Result<()> {
        match id {
            AGREE if self.page == Page::License => {
                self.agreed = self.checked(AGREE);
                unsafe { EnableWindow(self.control(NEXT), i32::from(self.agreed)) };
                return Ok(());
            }
            PARENT if self.page == Page::Location && notification == EN_CHANGE as u16 => {
                self.preview_path();
                return Ok(());
            }
            BROWSE if self.page == Page::Location => {
                if let Some(path) = platform::browse_install_parent(self.window)? {
                    set_text(self.control(PARENT), &path.display().to_string());
                    self.preview_path();
                }
                return Ok(());
            }
            CANCEL
                if !matches!(self.page, Page::Progress | Page::Finished)
                    || self.can_retry_removal() =>
            {
                unsafe { DestroyWindow(self.window) };
                return Ok(());
            }
            BACK => {
                self.page = match self.page {
                    Page::License => Page::Welcome,
                    Page::Location => {
                        self.save_parent();
                        Page::License
                    }
                    Page::Review if self.mode == Mode::Install => Page::Location,
                    Page::Review => Page::Welcome,
                    _ => return Ok(()),
                }
            }
            NEXT => {
                self.page = match self.page {
                    Page::Welcome if self.mode == Mode::Install => Page::License,
                    Page::Welcome => Page::Review,
                    Page::License if self.agreed => Page::Location,
                    Page::Location => {
                        self.save_parent();
                        self.folder =
                            install_folder(Some(std::path::Path::new(self.parent.trim())))?;
                        Page::Review
                    }
                    Page::Review => return self.start(),
                    Page::Finished if self.can_retry_removal() => return self.start(),
                    Page::Finished => {
                        if self.mode == Mode::Install
                            && self.answer["ok"] == true
                            && self.checked(LAUNCH)
                        {
                            std::process::Command::new(self.folder.join("magic_converter.exe"))
                                .spawn()?;
                        }
                        unsafe { DestroyWindow(self.window) };
                        return Ok(());
                    }
                    _ => return Ok(()),
                }
            }
            _ => return Ok(()),
        }
        self.render();
        Ok(())
    }

    fn paint(&self) {
        let mut info = PAINTSTRUCT::default();
        let dc = unsafe { BeginPaint(self.window, &mut info) };
        let rect = |left, top, right, bottom| RECT {
            left: self.scale(left),
            top: self.scale(top),
            right: self.scale(right),
            bottom: self.scale(bottom),
        };
        unsafe {
            FillRect(
                dc,
                &rect(0, 0, WIDTH, HEIGHT),
                GetSysColorBrush(COLOR_BTNFACE),
            );
            if self.banner() {
                FillRect(
                    dc,
                    &rect(0, 0, WIDTH, FOOTER),
                    GetSysColorBrush(COLOR_WINDOW),
                );
                let vertex = |x, y, r: u16, g: u16, b: u16| TRIVERTEX {
                    x,
                    y,
                    Red: r << 8,
                    Green: g << 8,
                    Blue: b << 8,
                    Alpha: 0,
                };
                let vertices = [
                    vertex(0, 0, 31, 24, 72),
                    vertex(self.scale(202), self.scale(FOOTER), 89, 45, 147),
                ];
                let mesh = GRADIENT_RECT {
                    UpperLeft: 0,
                    LowerRight: 1,
                };
                GdiGradientFill(
                    dc,
                    vertices.as_ptr(),
                    2,
                    (&mesh as *const GRADIENT_RECT).cast(),
                    1,
                    GRADIENT_FILL_RECT_V,
                );
                DrawIconEx(
                    dc,
                    self.scale(98),
                    self.scale(34),
                    self.icon,
                    self.scale(76),
                    self.scale(76),
                    0,
                    null_mut(),
                    DI_NORMAL,
                );
            } else {
                FillRect(dc, &rect(0, 0, WIDTH, 82), GetSysColorBrush(COLOR_WINDOW));
                FillRect(
                    dc,
                    &rect(0, 82, WIDTH, 83),
                    GetSysColorBrush(COLOR_BTNSHADOW),
                );
                DrawIconEx(
                    dc,
                    self.scale(574),
                    self.scale(19),
                    self.icon,
                    self.scale(46),
                    self.scale(46),
                    0,
                    null_mut(),
                    DI_NORMAL,
                );
            }
            FillRect(
                dc,
                &rect(0, FOOTER, WIDTH, FOOTER + 1),
                GetSysColorBrush(COLOR_BTNSHADOW),
            );
            EndPaint(self.window, &info);
        }
    }
}

unsafe extern "system" fn window_proc(
    window: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if message == WM_NCCREATE {
        let create = unsafe { &*(lparam as *const CREATESTRUCTW) };
        unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize) };
    }
    let state = unsafe { GetWindowLongPtrW(window, GWLP_USERDATA) } as *const RefCell<Wizard>;
    let state = unsafe { state.as_ref() };
    if matches!(message, WM_CTLCOLORSTATIC | WM_CTLCOLORBTN) {
        unsafe {
            let white = GetWindowLongPtrW(lparam as HWND, GWLP_USERDATA) != 0;
            let color = if white { COLOR_WINDOW } else { COLOR_BTNFACE };
            SetBkColor(wparam as HDC, GetSysColor(color));
            return GetSysColorBrush(color) as LRESULT;
        }
    }
    if message == WM_DESTROY {
        unsafe { PostQuitMessage(0) };
        return 0;
    }
    if let Some(state) = state
        && let Ok(mut wizard) = state.try_borrow_mut()
    {
        match message {
            WM_PAINT => {
                wizard.paint();
                return 0;
            }
            WM_COMMAND => {
                if let Err(error) = wizard.command(wparam as u16, (wparam >> 16) as u16) {
                    platform::notice(&error.to_string(), true);
                }
                return 0;
            }
            WM_TIMER => {
                wizard.tick();
                return 0;
            }
            WM_CLOSE => {
                if wizard.page != Page::Progress {
                    unsafe { DestroyWindow(window) };
                }
                return 0;
            }
            _ => (),
        }
    }
    unsafe { DefWindowProcW(window, message, wparam, lparam) }
}

fn font(dpi: i32, points: i32, weight: u32) -> HFONT {
    unsafe {
        CreateFontW(
            -(points * dpi + 36) / 72,
            0,
            0,
            0,
            weight as i32,
            0,
            0,
            0,
            DEFAULT_CHARSET as u32,
            OUT_DEFAULT_PRECIS as u32,
            CLIP_DEFAULT_PRECIS as u32,
            CLEARTYPE_QUALITY as u32,
            DEFAULT_PITCH as u32,
            wide("Malgun Gothic").as_ptr(),
        )
    }
}

impl Drop for Wizard {
    fn drop(&mut self) {
        unsafe {
            if IsWindow(self.window) != 0 {
                DestroyWindow(self.window);
            }
            if !self.icon.is_null() {
                DestroyIcon(self.icon);
            }
            DeleteObject(self.body_font);
            DeleteObject(self.title_font);
        }
    }
}

pub(super) fn run(mode: Mode, folder: PathBuf) -> Result<Value> {
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE);
        InitCommonControlsEx(&INITCOMMONCONTROLSEX {
            dwSize: size_of::<INITCOMMONCONTROLSEX>() as u32,
            dwICC: ICC_PROGRESS_CLASS,
        });
    }
    let dpi = unsafe { GetDpiForSystem() }.max(96) as i32;
    let icon = brand_icon(64);
    let instance = unsafe { GetModuleHandleW(null()) };
    let class = wide("MagicConverterInstallerWizard");
    let caption = wide(format!("{PRODUCT} {VERSION} {} 마법사", mode.name()));
    let state = RefCell::new(Wizard {
        window: null_mut(),
        mode,
        page: Page::Welcome,
        parent: folder.parent().unwrap_or(&folder).display().to_string(),
        folder,
        agreed: false,
        children: Vec::new(),
        dpi,
        body_font: font(dpi, 9, FW_NORMAL),
        title_font: font(dpi, 12, FW_SEMIBOLD),
        icon,
        work: None,
        answer: json!({"ok": true, "cancelled": true}),
    });
    let definition = WNDCLASSW {
        lpfnWndProc: Some(window_proc),
        hInstance: instance,
        hIcon: icon,
        hCursor: unsafe { LoadCursorW(null_mut(), IDC_ARROW) },
        hbrBackground: unsafe { GetSysColorBrush(COLOR_BTNFACE) },
        lpszClassName: class.as_ptr(),
        ..Default::default()
    };
    if unsafe { RegisterClassW(&definition) } == 0 {
        return Err(io::Error::last_os_error().into());
    }
    let style = WS_CAPTION | WS_SYSMENU | WS_CLIPCHILDREN;
    let extended = WS_EX_DLGMODALFRAME | WS_EX_CONTROLPARENT;
    let mut bounds = RECT {
        right: state.borrow().scale(WIDTH),
        bottom: state.borrow().scale(HEIGHT),
        ..Default::default()
    };
    let mut area = RECT::default();
    unsafe {
        AdjustWindowRectExForDpi(&mut bounds, style, 0, extended, dpi as u32);
        SystemParametersInfoW(SPI_GETWORKAREA, 0, (&mut area as *mut RECT).cast(), 0);
    }
    let width = bounds.right - bounds.left;
    let height = bounds.bottom - bounds.top;
    let x = area.left + ((area.right - area.left - width) / 2).max(0);
    let y = area.top + ((area.bottom - area.top - height) / 2).max(0);
    let window = unsafe {
        CreateWindowExW(
            extended,
            class.as_ptr(),
            caption.as_ptr(),
            style,
            x,
            y,
            width,
            height,
            null_mut(),
            null_mut(),
            instance,
            (&state as *const RefCell<Wizard>).cast(),
        )
    };
    if window.is_null() {
        return Err(io::Error::last_os_error().into());
    }
    state.borrow_mut().window = window;
    state.borrow_mut().render();
    unsafe { ShowWindow(window, SW_SHOW) };
    let mut message = MSG::default();
    loop {
        let result = unsafe { GetMessageW(&mut message, null_mut(), 0, 0) };
        if result == -1 {
            return Err(io::Error::last_os_error().into());
        }
        if result == 0 {
            break;
        }
        if message.message == WM_KEYDOWN && message.wParam == usize::from(VK_RETURN) {
            let focused = unsafe { GetDlgCtrlID(GetFocus()) } as u16;
            let action = match focused {
                BACK | CANCEL | BROWSE | AGREE | LAUNCH => focused,
                _ => NEXT,
            };
            let button = unsafe { GetDlgItem(window, i32::from(action)) };
            unsafe { SendMessageW(button, BM_CLICK, 0, 0) };
        } else if message.message == WM_KEYDOWN && message.wParam == usize::from(VK_ESCAPE) {
            unsafe { SendMessageW(window, WM_COMMAND, usize::from(CANCEL), 0) };
        } else if unsafe { IsDialogMessageW(window, &message) } == 0 {
            unsafe {
                TranslateMessage(&message);
                DispatchMessageW(&message);
            }
        }
    }
    Ok(state.borrow().answer.clone())
}
