use super::*;
use windows_sys::Win32::System::SystemServices::SS_NOTIFY;
use windows_sys::Win32::UI::Controls::*;

pub(super) type Field = (&'static str, &'static str, u32, u16, i32, i32, i32, i32);

const PROPERTY: windows_sys::core::PCWSTR = windows_sys::core::w!("MagicConverter.Form");
// TOOLINFO V2 is accepted by both classic and themed common controls.
const TOOL_INFO_SIZE: u32 = std::mem::offset_of!(TTTOOLINFOW, lpReserved) as u32;

struct Form {
    children: Vec<(HWND, RECT)>,
    tooltip: HWND,
    tips: Vec<Vec<u16>>,
    font: HFONT,
}

fn scale(value: i32, dpi: u32) -> i32 {
    (value * dpi as i32 + 48) / 96
}

impl Form {
    fn layout(&mut self, dpi: u32) {
        let mut metrics = NONCLIENTMETRICSW {
            cbSize: size_of::<NONCLIENTMETRICSW>() as u32,
            ..Default::default()
        };
        let font = unsafe {
            if SystemParametersInfoForDpi(
                SPI_GETNONCLIENTMETRICS,
                metrics.cbSize,
                (&mut metrics as *mut NONCLIENTMETRICSW).cast(),
                0,
                dpi,
            ) != 0
            {
                CreateFontIndirectW(&metrics.lfMessageFont)
            } else {
                null_mut()
            }
        };
        let active_font = if font.is_null() {
            unsafe { GetStockObject(DEFAULT_GUI_FONT) }
        } else {
            font
        };
        for &(child, rect) in &self.children {
            unsafe {
                SendMessageW(child, WM_SETFONT, active_font as usize, 1);
                SetWindowPos(
                    child,
                    null_mut(),
                    scale(rect.left, dpi),
                    scale(rect.top, dpi),
                    scale(rect.right, dpi),
                    scale(rect.bottom, dpi),
                    SWP_NOZORDER | SWP_NOACTIVATE,
                );
            }
        }
        if !self.tooltip.is_null() {
            unsafe {
                SendMessageW(self.tooltip, WM_SETFONT, active_font as usize, 1);
                SendMessageW(
                    self.tooltip,
                    TTM_SETMAXTIPWIDTH,
                    0,
                    scale(365, dpi) as isize,
                );
            }
        }
        if !self.font.is_null() {
            unsafe { DeleteObject(self.font) };
        }
        self.font = font;
    }

    fn tip(&mut self, owner: HWND, child: HWND, description: &str) {
        if description.is_empty() || self.tooltip.is_null() {
            return;
        }
        let mut tip = wide(description);
        let tool = TTTOOLINFOW {
            cbSize: TOOL_INFO_SIZE,
            uFlags: TTF_IDISHWND | TTF_SUBCLASS,
            hwnd: owner,
            uId: child as usize,
            lpszText: tip.as_mut_ptr(),
            ..Default::default()
        };
        unsafe {
            SendMessageW(
                self.tooltip,
                TTM_ADDTOOLW,
                0,
                (&tool as *const TTTOOLINFOW) as isize,
            )
        };
        self.tips.push(tip);
    }
}

pub(super) fn install(
    window: HWND,
    client: (i32, i32),
    fields: &[Field],
    help: fn(u16, &str) -> &'static str,
) {
    let instance = unsafe { GetModuleHandleW(null()) };
    unsafe {
        InitCommonControlsEx(&INITCOMMONCONTROLSEX {
            dwSize: size_of::<INITCOMMONCONTROLSEX>() as u32,
            dwICC: ICC_WIN95_CLASSES,
        });
    }
    let tooltip = unsafe {
        CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_NOACTIVATE,
            TOOLTIPS_CLASSW,
            null(),
            WS_POPUP | TTS_NOPREFIX | TTS_ALWAYSTIP,
            0,
            0,
            0,
            0,
            window,
            null_mut(),
            instance,
            null(),
        )
    };
    let mut form = Box::new(Form {
        children: Vec::new(),
        tooltip,
        tips: Vec::new(),
        font: null_mut(),
    });
    for &(class, label, mut style, id, x, y, w, h) in fields {
        let description = help(id, label);
        if class == "STATIC" && !description.is_empty() {
            style |= SS_NOTIFY;
        }
        let child = unsafe {
            CreateWindowExW(
                0,
                wide(class).as_ptr(),
                wide(label).as_ptr(),
                style,
                x,
                y,
                w,
                h,
                window,
                handle(id),
                instance,
                null(),
            )
        };
        if !child.is_null() {
            form.tip(window, child, description);
            form.children.push((
                child,
                RECT {
                    left: x,
                    top: y,
                    right: w,
                    bottom: h,
                },
            ));
        }
    }
    let dpi = unsafe { GetDpiForWindow(window) }.max(96);
    let mut bounds = RECT {
        right: scale(client.0, dpi),
        bottom: scale(client.1, dpi),
        ..Default::default()
    };
    unsafe {
        AdjustWindowRectExForDpi(
            &mut bounds,
            GetWindowLongW(window, GWL_STYLE) as u32,
            0,
            GetWindowLongW(window, GWL_EXSTYLE) as u32,
            dpi,
        );
        let (width, height) = (bounds.right - bounds.left, bounds.bottom - bounds.top);
        let owner = GetWindow(window, GW_OWNER);
        let anchor = if owner.is_null() { window } else { owner };
        let mut position = RECT::default();
        GetWindowRect(anchor, &mut position);
        let mut x = position.left;
        let mut y = position.top;
        if !owner.is_null() {
            x += (position.right - position.left - width) / 2;
            y += (position.bottom - position.top - height) / 2;
        }
        let monitor = MonitorFromWindow(anchor, MONITOR_DEFAULTTONEAREST);
        let mut monitor_info = MONITORINFO {
            cbSize: size_of::<MONITORINFO>() as u32,
            ..Default::default()
        };
        if GetMonitorInfoW(monitor, &mut monitor_info) != 0 {
            let area = monitor_info.rcWork;
            x = x.clamp(area.left, (area.right - width).max(area.left));
            y = y.clamp(area.top, (area.bottom - height).max(area.top));
        }
        SetWindowPos(
            window,
            null_mut(),
            x,
            y,
            width,
            height,
            SWP_NOZORDER | SWP_NOACTIVATE,
        );
    }
    form.layout(dpi);
    let raw = Box::into_raw(form);
    if unsafe { SetPropW(window, PROPERTY, raw.cast()) } == 0 {
        drop(unsafe { Box::from_raw(raw) });
    }
}

impl Drop for Form {
    fn drop(&mut self) {
        unsafe {
            if IsWindow(self.tooltip) != 0 {
                DestroyWindow(self.tooltip);
            }
            if !self.font.is_null() {
                DeleteObject(self.font);
            }
        }
    }
}

pub(super) fn message(window: HWND, message: u32, wp: WPARAM, lp: LPARAM) -> Option<LRESULT> {
    match message {
        WM_CTLCOLORSTATIC | WM_CTLCOLORBTN => unsafe {
            SetBkMode(wp as HDC, TRANSPARENT as i32);
            SetBkColor(wp as HDC, GetSysColor(COLOR_WINDOW));
            Some(GetSysColorBrush(COLOR_WINDOW) as LRESULT)
        },
        WM_DPICHANGED => {
            let form = unsafe { GetPropW(window, PROPERTY) } as *mut Form;
            let Some(form) = (unsafe { form.as_mut() }) else {
                return None;
            };
            let bounds = unsafe { &*(lp as *const RECT) };
            unsafe {
                SetWindowPos(
                    window,
                    null_mut(),
                    bounds.left,
                    bounds.top,
                    bounds.right - bounds.left,
                    bounds.bottom - bounds.top,
                    SWP_NOZORDER | SWP_NOACTIVATE,
                );
            }
            form.layout((wp & 0xffff) as u32);
            Some(0)
        }
        WM_NCDESTROY => {
            let form = unsafe { RemovePropW(window, PROPERTY) } as *mut Form;
            if !form.is_null() {
                drop(unsafe { Box::from_raw(form) });
            }
            None
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registers_tooltips_on_native_controls_and_labels() {
        unsafe {
            let window = CreateWindowExW(
                0,
                wide("STATIC").as_ptr(),
                null(),
                WS_OVERLAPPED,
                0,
                0,
                220,
                100,
                null_mut(),
                null_mut(),
                GetModuleHandleW(null()),
                null(),
            );
            assert!(!window.is_null());
            install(
                window,
                (220, 100),
                &[
                    (
                        "STATIC",
                        "도움말 레이블",
                        WS_CHILD | WS_VISIBLE,
                        11,
                        10,
                        10,
                        130,
                        24,
                    ),
                    ("BUTTON", "선택", WS_CHILD | WS_VISIBLE, 12, 10, 40, 130, 24),
                ],
                |_, _| "마우스를 올렸을 때 표시할 한국어 도움말",
            );
            let form = RemovePropW(window, PROPERTY) as *mut Form;
            assert!(!form.is_null());
            let form = Box::from_raw(form);
            let registered = SendMessageW(form.tooltip, TTM_GETTOOLCOUNT, 0, 0);
            let mut tip = [0_u16; 128];
            let mut tool = TTTOOLINFOW {
                cbSize: TOOL_INFO_SIZE,
                uFlags: TTF_IDISHWND,
                hwnd: window,
                uId: control(window, 11) as usize,
                lpszText: tip.as_mut_ptr(),
                ..Default::default()
            };
            SendMessageW(
                form.tooltip,
                TTM_GETTEXTW,
                tip.len(),
                (&mut tool as *mut TTTOOLINFOW) as isize,
            );
            DestroyWindow(window);
            drop(form);
            assert_eq!(registered, 2);
            let end = tip.iter().position(|&ch| ch == 0).unwrap();
            assert_eq!(
                String::from_utf16_lossy(&tip[..end]),
                "마우스를 올렸을 때 표시할 한국어 도움말"
            );
        }
    }
}
