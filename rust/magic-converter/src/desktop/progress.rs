use super::{Ui, wide};
use crate::automation::ipc::{JobProgress, ProgressOutcome};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::ptr::{null, null_mut};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::*;
use windows_sys::Win32::UI::HiDpi::GetDpiForSystem;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

const WIDTH: i32 = 348;
const ROW: i32 = 56;
const MAX_ROWS: usize = 3;

struct Entry {
    progress: JobProgress,
    result: Option<(ProgressOutcome, Instant)>,
}

#[derive(Default)]
pub(super) struct Panel {
    pub(super) window: HWND,
    rows: BTreeMap<u64, Entry>,
    frame: u32,
    last_frame: Option<Instant>,
    dirty: bool,
    height: i32,
}

impl Panel {
    pub(super) fn update(&mut self, id: u64, progress: JobProgress) {
        self.rows.insert(
            id,
            Entry {
                progress,
                result: None,
            },
        );
        self.dirty = true;
    }

    pub(super) fn finish(&mut self, id: u64, result: ProgressOutcome) {
        if let Some(row) = self.rows.get_mut(&id) {
            let linger = match result {
                ProgressOutcome::Done => 2,
                ProgressOutcome::Warning => 6,
                ProgressOutcome::Failed | ProgressOutcome::Cancelled => 4,
            };
            row.result = Some((result, Instant::now() + Duration::from_secs(linger)));
            self.dirty = true;
        }
    }

    pub(super) fn tick(&mut self) {
        if self.window.is_null() || self.rows.is_empty() && !self.dirty {
            return;
        }
        let now = Instant::now();
        let before = self.rows.len();
        self.rows
            .retain(|_, row| row.result.is_none_or(|(_, end)| end > now));
        let repaint = self.dirty || before != self.rows.len();
        self.dirty = false;
        if self.rows.is_empty() {
            unsafe { ShowWindow(self.window, SW_HIDE) };
            return;
        }
        let scale = scale();
        let height = 41
            + ROW * self.rows.len().min(MAX_ROWS) as i32
            + if self.rows.len() > MAX_ROWS { 19 } else { 0 };
        let mut work = RECT::default();
        unsafe { SystemParametersInfoW(SPI_GETWORKAREA, 0, (&mut work as *mut RECT).cast(), 0) };
        if repaint || self.height != scale(height) {
            self.height = scale(height);
            unsafe {
                SetWindowPos(
                    self.window,
                    HWND_TOPMOST,
                    work.right - scale(WIDTH + 15),
                    work.bottom - self.height - scale(15),
                    scale(WIDTH),
                    self.height,
                    SWP_NOACTIVATE | SWP_SHOWWINDOW,
                );
            }
        }
        let animate = self.rows.values().any(|row| row.result.is_none())
            && self
                .last_frame
                .is_none_or(|last| now.duration_since(last).as_millis() >= 90);
        if animate {
            self.frame = self.frame.wrapping_add(11);
            self.last_frame = Some(now);
        }
        if repaint || animate {
            unsafe { InvalidateRect(self.window, null(), 0) };
        }
    }
}

fn scale() -> impl Fn(i32) -> i32 {
    let dpi = unsafe { GetDpiForSystem() }.max(96) as i32;
    move |length| (length * dpi + 48) / 96
}

fn rgb(r: u32, g: u32, b: u32) -> u32 {
    r | g << 8 | b << 16
}

fn fill(dc: HDC, rect: RECT, color: u32) {
    unsafe {
        let brush = CreateSolidBrush(color);
        FillRect(dc, &rect, brush);
        DeleteObject(brush);
    }
}

fn label(dc: HDC, text: &str, mut rect: RECT, color: u32) {
    let text = wide(text);
    unsafe {
        SetTextColor(dc, color);
        DrawTextW(
            dc,
            text.as_ptr(),
            text.len() as i32 - 1,
            &mut rect,
            DT_SINGLELINE | DT_VCENTER | DT_END_ELLIPSIS | DT_NOPREFIX,
        );
    }
}

fn paint(window: HWND, panel: &Panel) {
    let mut info = PAINTSTRUCT::default();
    let target = unsafe { BeginPaint(window, &mut info) };
    let p = scale();
    let rect = |x, y, right, bottom| RECT {
        left: p(x),
        top: p(y),
        right: p(right),
        bottom: p(bottom),
    };
    let bitmap = unsafe { CreateCompatibleBitmap(target, p(WIDTH), panel.height) };
    let memory = unsafe { CreateCompatibleDC(target) };
    let buffered = !memory.is_null() && !bitmap.is_null();
    let dc = if buffered { memory } else { target };
    let old_bitmap = if buffered {
        unsafe { SelectObject(dc, bitmap) }
    } else {
        null_mut()
    };
    let font = unsafe {
        CreateFontW(
            -p(14),
            0,
            0,
            0,
            FW_NORMAL as i32,
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
    };
    let old_font = unsafe {
        SetBkMode(dc, TRANSPARENT as i32);
        SelectObject(
            dc,
            if font.is_null() {
                GetStockObject(DEFAULT_GUI_FONT)
            } else {
                font
            },
        )
    };
    fill(
        dc,
        RECT {
            left: 0,
            top: 0,
            right: p(WIDTH),
            bottom: panel.height,
        },
        rgb(32, 32, 40),
    );
    label(
        dc,
        "MagicConverter · 작업 진행",
        rect(16, 9, 332, 32),
        rgb(255, 197, 122),
    );
    let entries = panel
        .rows
        .values()
        .filter(|row| row.result.is_none())
        .chain(panel.rows.values().filter(|row| row.result.is_some()));
    for (index, row) in entries.take(MAX_ROWS).enumerate() {
        let y = 34 + index as i32 * ROW;
        let progress = &row.progress;
        let title = format!(
            "{} · {} / {}",
            progress.label, progress.finished, progress.total
        );
        label(dc, &title, rect(16, y, 332, y + 21), rgb(249, 249, 252));
        let (phase, color) = match row.result.map(|(outcome, _)| outcome) {
            None => ("처리 중", rgb(238, 151, 72)),
            Some(ProgressOutcome::Done) => ("완료", rgb(89, 185, 128)),
            Some(ProgressOutcome::Warning) => ("완료 · 결과 기록 확인", rgb(238, 183, 93)),
            Some(ProgressOutcome::Failed) => ("일부 또는 전체 실패", rgb(228, 108, 110)),
            Some(ProgressOutcome::Cancelled) => ("취소됨", rgb(149, 151, 166)),
        };
        label(
            dc,
            &format!("{phase} · {}", progress.file),
            rect(16, y + 21, 332, y + 40),
            rgb(214, 215, 224),
        );
        fill(dc, rect(16, y + 44, 332, y + 50), rgb(65, 65, 81));
        let length = (316 * progress.finished / progress.total.max(1)).min(316) as i32;
        if length > 0 {
            fill(dc, rect(16, y + 44, 16 + length, y + 50), color);
        }
        if row.result.is_none() && length < 316 {
            let remaining = 316 - length;
            let offset = (panel.frame % remaining as u32) as i32;
            fill(
                dc,
                rect(
                    16 + length + offset,
                    y + 44,
                    16 + length + (offset + 36).min(remaining),
                    y + 50,
                ),
                color,
            );
        }
    }
    if panel.rows.len() > MAX_ROWS {
        label(
            dc,
            &format!("외 {}개 작업", panel.rows.len() - MAX_ROWS),
            rect(
                16,
                34 + ROW * MAX_ROWS as i32,
                332,
                53 + ROW * MAX_ROWS as i32,
            ),
            rgb(214, 215, 224),
        );
    }
    unsafe {
        SelectObject(dc, old_font);
        DeleteObject(font);
        if buffered {
            BitBlt(target, 0, 0, p(WIDTH), panel.height, dc, 0, 0, SRCCOPY);
            SelectObject(dc, old_bitmap);
        }
        DeleteDC(memory);
        DeleteObject(bitmap);
        EndPaint(window, &info);
    }
}

unsafe extern "system" fn procedure(window: HWND, message: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match message {
        WM_NCCREATE => {
            let create = unsafe { &*(lp as *const CREATESTRUCTW) };
            unsafe { SetWindowLongPtrW(window, GWLP_USERDATA, create.lpCreateParams as isize) };
        }
        WM_NCHITTEST => return HTTRANSPARENT as LRESULT,
        WM_MOUSEACTIVATE => return MA_NOACTIVATE as LRESULT,
        WM_PAINT => {
            let state = unsafe { GetWindowLongPtrW(window, GWLP_USERDATA) } as *const RefCell<Ui>;
            if let Some(state) = unsafe { state.as_ref() }
                && let Ok(ui) = state.try_borrow()
            {
                paint(window, &ui.progress);
                return 0;
            }
        }
        _ => {}
    }
    unsafe { DefWindowProcW(window, message, wp, lp) }
}

pub(super) fn window(state: *const RefCell<Ui>, instance: HINSTANCE) -> HWND {
    let class = wide("MagicConverterRustProgress");
    let definition = WNDCLASSW {
        lpfnWndProc: Some(procedure),
        hInstance: instance,
        lpszClassName: class.as_ptr(),
        ..Default::default()
    };
    unsafe {
        if RegisterClassW(&definition) == 0 {
            return null_mut();
        }
        let window = CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT,
            class.as_ptr(),
            wide("MagicConverter · 작업 진행").as_ptr(),
            WS_POPUP,
            0,
            0,
            0,
            0,
            null_mut(),
            null_mut(),
            instance,
            state.cast(),
        );
        if !window.is_null() {
            SetLayeredWindowAttributes(window, 0, 246, LWA_ALPHA);
        }
        window
    }
}
