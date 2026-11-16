use super::{Ui, wheel};
use crate::web::{self, Input};
use std::cell::RefCell;
use std::ffi::OsString;
use std::fs::File;
use std::io::Read;
use std::os::windows::ffi::OsStringExt;
use std::path::PathBuf;
use std::ptr::null_mut;
use windows::Win32::Foundation::{HWND, POINTL};
use windows::Win32::System::Com::{
    DVASPECT_CONTENT, FORMATETC, IDataObject, STGMEDIUM, TYMED_HGLOBAL, TYMED_ISTREAM,
};
use windows::Win32::System::DataExchange::RegisterClipboardFormatW;
use windows::Win32::System::Memory::{GlobalLock, GlobalSize, GlobalUnlock};
use windows::Win32::System::Ole::{
    CF_HDROP, CF_UNICODETEXT, DROPEFFECT, DROPEFFECT_COPY, DROPEFFECT_NONE, IDropTarget,
    IDropTarget_Impl, OleInitialize, OleUninitialize, RegisterDragDrop, ReleaseStgMedium,
    RevokeDragDrop,
};
use windows::Win32::System::SystemServices::{MK_CONTROL, MK_SHIFT, MODIFIERKEYS_FLAGS};
use windows::Win32::UI::Shell::{DragQueryFileW, HDROP};
use windows::core::{PCWSTR, Ref, Result, implement};

pub(super) struct Apartment;

impl Apartment {
    pub(super) fn new() -> Result<Self> {
        unsafe { OleInitialize(None)? };
        Ok(Self)
    }
}

impl Drop for Apartment {
    fn drop(&mut self) {
        unsafe { OleUninitialize() };
    }
}

pub(super) struct Registration {
    window: HWND,
    _target: IDropTarget,
}

impl Registration {
    pub(super) fn new(
        state: *const RefCell<Ui>,
        window: windows_sys::Win32::Foundation::HWND,
        is_wheel: bool,
    ) -> Result<Self> {
        let target: IDropTarget = DropTarget {
            state,
            is_wheel,
            accepts: RefCell::new(false),
        }
        .into();
        let window = HWND(window);
        unsafe { RegisterDragDrop(window, &target)? };
        Ok(Self {
            window,
            _target: target,
        })
    }
}

impl Drop for Registration {
    fn drop(&mut self) {
        let _ = unsafe { RevokeDragDrop(self.window) };
    }
}

struct Medium(STGMEDIUM);

impl Drop for Medium {
    fn drop(&mut self) {
        unsafe { ReleaseStgMedium(&mut self.0) };
    }
}

fn files(object: Option<&IDataObject>) -> Vec<PathBuf> {
    let format = FORMATETC {
        cfFormat: CF_HDROP.0,
        ptd: null_mut(),
        dwAspect: DVASPECT_CONTENT.0,
        lindex: -1,
        tymed: TYMED_HGLOBAL.0 as u32,
    };
    let Some(medium) = object
        .and_then(|object| unsafe { object.GetData(&format) }.ok())
        .map(Medium)
    else {
        return Vec::new();
    };
    if medium.0.tymed != TYMED_HGLOBAL.0 as u32 {
        return Vec::new();
    }
    let handle = HDROP(unsafe { medium.0.u.hGlobal }.0);
    let count = unsafe { DragQueryFileW(handle, u32::MAX, None) };
    if !(1..=256).contains(&count) {
        return Vec::new();
    }
    let mut paths = Vec::new();
    for index in 0..count {
        let length = unsafe { DragQueryFileW(handle, index, None) };
        if !(1..=4096).contains(&length) {
            return Vec::new();
        }
        let mut buffer = vec![0_u16; length as usize + 1];
        if unsafe { DragQueryFileW(handle, index, Some(&mut buffer)) } != length {
            return Vec::new();
        }
        let path = PathBuf::from(OsString::from_wide(&buffer[..length as usize]));
        if !path.is_absolute() {
            return Vec::new();
        }
        paths.push(path);
    }
    paths
}

fn registered(name: &str) -> u16 {
    let name = super::wide(name);
    unsafe { RegisterClipboardFormatW(PCWSTR(name.as_ptr())) as u16 }
}

fn format(kind: u16, index: i32, stream: bool) -> FORMATETC {
    FORMATETC {
        cfFormat: kind,
        ptd: null_mut(),
        dwAspect: DVASPECT_CONTENT.0,
        lindex: index,
        tymed: (TYMED_HGLOBAL.0 | if stream { TYMED_ISTREAM.0 } else { 0 }) as u32,
    }
}

fn payload(object: &IDataObject, format: &FORMATETC, limit: usize) -> Option<Vec<u8>> {
    let medium = Medium(unsafe { object.GetData(format) }.ok()?);
    if medium.0.tymed == TYMED_HGLOBAL.0 as u32 {
        let handle = unsafe { medium.0.u.hGlobal };
        let size = unsafe { GlobalSize(handle) };
        if !(1..=limit).contains(&size) {
            return None;
        }
        let ptr = unsafe { GlobalLock(handle) }.cast::<u8>();
        if ptr.is_null() {
            return None;
        }
        let bytes = unsafe { std::slice::from_raw_parts(ptr, size) }.to_vec();
        let _ = unsafe { GlobalUnlock(handle) };
        return Some(bytes);
    }
    if medium.0.tymed == TYMED_ISTREAM.0 as u32 {
        let stream = unsafe { &*medium.0.u.pstm }.as_ref()?;
        let mut data = Vec::new();
        let mut buffer = [0_u8; 32768];
        loop {
            let mut count = 0;
            let request = buffer.len().min(limit.saturating_sub(data.len()) + 1);
            let result = unsafe {
                stream.Read(buffer.as_mut_ptr().cast(), request as u32, Some(&mut count))
            };
            let count = count as usize;
            if result.is_err() || count > request || data.len() + count > limit {
                return None;
            }
            data.extend_from_slice(&buffer[..count]);
            if count < request {
                return (!data.is_empty()).then_some(data);
            }
        }
    }
    None
}

fn decoded(bytes: &[u8], wide: bool) -> Option<String> {
    let (bytes, big) = if let Some(rest) = bytes.strip_prefix(&[255, 254]) {
        (rest, Some(false))
    } else if let Some(rest) = bytes.strip_prefix(&[254, 255]) {
        (rest, Some(true))
    } else if wide || bytes.get(1) == Some(&0) {
        (bytes, Some(false))
    } else {
        (bytes.strip_prefix(&[239, 187, 191]).unwrap_or(bytes), None)
    };
    let value = if let Some(big) = big {
        if !bytes.len().is_multiple_of(2) {
            return None;
        }
        let words: Vec<_> = bytes
            .chunks_exact(2)
            .map(|part| {
                let pair = [part[0], part[1]];
                if big {
                    u16::from_be_bytes(pair)
                } else {
                    u16::from_le_bytes(pair)
                }
            })
            .collect();
        String::from_utf16(&words).ok()?
    } else {
        std::str::from_utf8(bytes).ok()?.to_owned()
    };
    let value = value.trim_end_matches('\0');
    (!value.contains('\0')).then(|| value.to_owned())
}

fn browser_urls(kind: &str, bytes: &[u8]) -> Option<Vec<Input>> {
    let value = decoded(bytes, kind == "UniformResourceLocatorW")?;
    if kind == "DownloadURL" {
        let (media, rest) = value.split_once(':')?;
        let (name, url) = rest.split_once(':')?;
        if media.is_empty() || name.is_empty() {
            return None;
        }
        return Some(vec![Input::url(url, Some(name), false).ok()?]);
    }
    let skip = if kind == "text/x-moz-url" { 2 } else { 1 };
    let values = value
        .lines()
        .step_by(skip)
        .map(str::trim)
        .filter(|url| !url.is_empty());
    let items: Vec<_> = values
        .map(|url| Input::url(url, None, false).ok())
        .collect::<Option<_>>()?;
    (!items.is_empty() && items.len() <= 100).then_some(items)
}

fn descriptors(bytes: &[u8]) -> Option<Vec<(String, usize)>> {
    let number = |bytes: &[u8], start: usize| {
        Some(u32::from_le_bytes(
            bytes.get(start..start + 4)?.try_into().ok()?,
        ))
    };
    let count = number(bytes, 0)? as usize;
    if !(1..=100).contains(&count) || bytes.len() != 4 + count * 592 {
        return None;
    }
    let mut total = 0;
    bytes[4..]
        .chunks_exact(592)
        .map(|record| {
            let flags = number(record, 0)?;
            let attributes = number(record, 36)?;
            let size = number(record, 68)? as usize;
            if flags & 64 == 0
                || flags & 4 != 0 && attributes & 16 != 0
                || number(record, 64)? != 0
                || !(1..=web::INLINE_LIMIT).contains(&size)
            {
                return None;
            }
            total += size;
            let name: Vec<_> = record[72..]
                .chunks_exact(2)
                .map(|p| u16::from_le_bytes([p[0], p[1]]))
                .take_while(|word| *word != 0)
                .collect();
            let name = String::from_utf16(&name).ok()?;
            (total <= web::INLINE_LIMIT && name == web::safe_name(&name)).then_some((name, size))
        })
        .collect()
}

fn internet_shortcut(data: &[u8]) -> Option<Input> {
    let text = decoded(data, false)?;
    let mut in_section = false;
    let mut found = None;
    for line in text.lines().map(str::trim) {
        if line.starts_with('[') {
            in_section = line == "[InternetShortcut]";
        } else if in_section
            && let Some((key, value)) = line.split_once('=')
            && key.trim().eq_ignore_ascii_case("URL")
        {
            if found.is_some() {
                return None;
            }
            found = Some(Input::url(value.trim(), None, false).ok()?);
        }
    }
    found
}

fn input(object: Option<&IDataObject>) -> (Vec<PathBuf>, Vec<Input>) {
    let paths = files(object);
    if !paths.is_empty() {
        return (paths, Vec::new());
    }
    (paths, object.and_then(browser).unwrap_or_default())
}

pub(super) fn snapshot(
    paths: Vec<PathBuf>,
    web: Vec<Input>,
) -> std::io::Result<(Vec<PathBuf>, Vec<Input>)> {
    if !web.is_empty() || paths.is_empty() {
        return Ok((paths, web));
    }
    let root = crate::resolved(&std::env::temp_dir())?;
    let resolved = paths
        .iter()
        .map(|path| crate::resolved(path))
        .collect::<std::io::Result<Vec<_>>>()?;
    let under_temp = resolved
        .iter()
        .filter(|path| path.starts_with(&root))
        .count();
    if under_temp == 0 {
        return Ok((paths, web));
    }
    if under_temp != paths.len() || under_temp > 100 {
        return Err(std::io::Error::other(
            "임시 드래그 파일만 한 번에 100개까지 선택하세요.",
        ));
    }
    let mut total = 0_u64;
    let items = resolved
        .iter()
        .map(|path| {
            let file = File::open(path)?;
            let info = file.metadata()?;
            if !info.is_file() || info.len() == 0 {
                return Err(std::io::Error::other(
                    "임시 드래그 입력은 비어 있지 않은 파일이어야 합니다.",
                ));
            }
            total = total.saturating_add(info.len());
            if total > web::INLINE_LIMIT as u64 {
                return Err(std::io::Error::other(
                    "임시 드래그 파일은 합계 32 MiB까지 지원합니다.",
                ));
            }
            let mut data = Vec::new();
            file.take(info.len() + 1).read_to_end(&mut data)?;
            if data.len() as u64 != info.len() {
                return Err(std::io::Error::other(
                    "드래그 도중 임시 파일 크기가 변경되었습니다.",
                ));
            }
            Input::bytes(
                &path.file_name().unwrap_or_default().to_string_lossy(),
                data,
            )
        })
        .collect::<std::io::Result<Vec<_>>>()?;
    Ok((items.iter().map(Input::placeholder).collect(), items))
}

fn browser(object: &IDataObject) -> Option<Vec<Input>> {
    let available = |format: &FORMATETC| unsafe { object.QueryGetData(format) }.is_ok();
    for kind in [
        "text/x-moz-url",
        "text/x-moz-url-data",
        "UniformResourceLocatorW",
        "UniformResourceLocator",
        "DownloadURL",
        "application/x-moz-file-promise-url",
    ] {
        let format = format(registered(kind), -1, false);
        if available(&format) {
            return browser_urls(kind, &payload(object, &format, 64_000)?);
        }
    }
    let group = format(registered("FileGroupDescriptorW"), -1, false);
    if available(&group) {
        let items = descriptors(&payload(object, &group, 4 + 100 * 592)?)?;
        let contents = registered("FileContents");
        return items
            .iter()
            .enumerate()
            .map(|(index, (name, size))| {
                let indexed = format(contents, index as i32, true);
                let data = payload(object, &indexed, *size).or_else(|| {
                    (items.len() == 1)
                        .then(|| payload(object, &format(contents, -1, true), *size))
                        .flatten()
                })?;
                if data.len() != *size {
                    return None;
                }
                if name.to_ascii_lowercase().ends_with(".url") {
                    internet_shortcut(&data)
                } else {
                    Input::bytes(name, data).ok()
                }
            })
            .collect();
    }
    for (kind, name) in [
        ("PNG", "image.png"),
        ("image/png", "image.png"),
        ("image/jpeg", "image.jpg"),
        ("image/webp", "image.webp"),
    ] {
        let format = format(registered(kind), -1, false);
        if available(&format) {
            return Some(vec![
                Input::bytes(name, payload(object, &format, web::INLINE_LIMIT)?).ok()?,
            ]);
        }
    }
    let html = format(registered("HTML Format"), -1, false);
    if available(&html)
        && let Some(url) = html_image(&decoded(&payload(object, &html, 1_000_000)?, false)?)?
    {
        return Some(vec![Input::url(&url, None, true).ok()?]);
    }
    let plain = format(CF_UNICODETEXT.0, -1, false);
    let text = decoded(&payload(object, &plain, 64_000)?, true)?;
    Some(vec![Input::url(text.trim(), None, false).ok()?])
}

fn html_image(html: &str) -> Option<Option<String>> {
    use std::sync::LazyLock;
    static TAG: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(
            r#"(?is)<!--[\s\S]*?-->|<([/]?[a-z][a-z0-9]*)\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>"#,
        )
        .unwrap()
    });
    static SRC: LazyLock<regex::Regex> = LazyLock::new(|| {
        regex::Regex::new(r#"(?is)\s+src\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s>]+))"#).unwrap()
    });
    let html = html
        .split_once("<!--StartFragment-->")
        .and_then(|(_, rest)| rest.split_once("<!--EndFragment-->"))
        .map_or(html, |(value, _)| value);
    let mut end = 0;
    let mut image = None;
    let mut anchor = 0;
    let mut inside_anchor = false;
    let mut text = false;
    for capture in TAG.captures_iter(html) {
        let tag = capture.get(0)?;
        if !html_escape::decode_html_entities(&html[end..tag.start()])
            .trim()
            .is_empty()
        {
            if !inside_anchor {
                return None;
            }
            text = true;
        }
        end = tag.end();
        let kind = capture
            .get(1)
            .map(|name| name.as_str().to_ascii_lowercase());
        match kind.as_deref() {
            Some("a") if anchor == 0 => {
                anchor += 1;
                inside_anchor = true;
            }
            Some("/a") if inside_anchor => inside_anchor = false,
            Some("a" | "/a" | "script" | "style" | "iframe" | "object") => return None,
            Some("img") if image.is_none() => {
                let source = SRC.captures(tag.as_str())?;
                let value = (1..=3).find_map(|index| source.get(index))?;
                image = Some(html_escape::decode_html_entities(value.as_str()).into_owned());
            }
            Some("img") => return None,
            _ => {}
        }
    }
    if !html[end..].trim().is_empty() || inside_anchor || (text && image.is_some()) {
        return None;
    }
    (anchor == 1 || image.is_some()).then_some(image)
}

#[implement(IDropTarget)]
struct DropTarget {
    state: *const RefCell<Ui>,
    is_wheel: bool,
    accepts: RefCell<bool>,
}

fn modifiers(keys: MODIFIERKEYS_FLAGS) -> bool {
    keys.0 & (MK_SHIFT.0 | MK_CONTROL.0) != 0
}

fn copy_allowed(effect: *mut DROPEFFECT) -> bool {
    unsafe { effect.as_ref() }.is_some_and(|allowed| allowed.0 & DROPEFFECT_COPY.0 != 0)
}

fn set_effect(effect: *mut DROPEFFECT, allowed: bool) {
    if let Some(effect) = unsafe { effect.as_mut() } {
        *effect = if allowed {
            DROPEFFECT_COPY
        } else {
            DROPEFFECT_NONE
        };
    }
}

impl IDropTarget_Impl for DropTarget_Impl {
    fn DragEnter(
        &self,
        data: Ref<IDataObject>,
        keys: MODIFIERKEYS_FLAGS,
        point: &POINTL,
        effect: *mut DROPEFFECT,
    ) -> Result<()> {
        let (paths, web) = input(data.as_ref());
        *self.accepts.borrow_mut() = !paths.is_empty() || !web.is_empty();
        let mut allowed = *self.accepts.borrow() && copy_allowed(effect);
        if self.is_wheel {
            allowed = unsafe { self.state.as_ref() }
                .and_then(|state| state.try_borrow_mut().ok())
                .is_some_and(|mut ui| {
                    wheel::entered(&mut ui, paths, web);
                    allowed && wheel::drag(&mut ui, point.x, point.y, modifiers(keys))
                });
        }
        set_effect(effect, allowed);
        Ok(())
    }

    fn DragOver(
        &self,
        keys: MODIFIERKEYS_FLAGS,
        point: &POINTL,
        effect: *mut DROPEFFECT,
    ) -> Result<()> {
        let mut allowed = *self.accepts.borrow() && copy_allowed(effect);
        if self.is_wheel {
            allowed = unsafe { self.state.as_ref() }
                .and_then(|state| state.try_borrow_mut().ok())
                .is_some_and(|mut ui| {
                    allowed && wheel::drag(&mut ui, point.x, point.y, modifiers(keys))
                });
        }
        set_effect(effect, allowed);
        Ok(())
    }

    fn DragLeave(&self) -> Result<()> {
        *self.accepts.borrow_mut() = false;
        if self.is_wheel
            && let Some(mut ui) =
                unsafe { self.state.as_ref() }.and_then(|state| state.try_borrow_mut().ok())
        {
            wheel::left(&mut ui);
        }
        Ok(())
    }

    fn Drop(
        &self,
        data: Ref<IDataObject>,
        keys: MODIFIERKEYS_FLAGS,
        point: &POINTL,
        effect: *mut DROPEFFECT,
    ) -> Result<()> {
        let (paths, web) = input(data.as_ref());
        let allowed = (!paths.is_empty() || !web.is_empty())
            && *self.accepts.borrow()
            && copy_allowed(effect)
            && unsafe { self.state.as_ref() }
                .and_then(|state| state.try_borrow_mut().ok())
                .is_some_and(|mut ui| {
                    if self.is_wheel {
                        wheel::dropped(&mut ui, paths, web, point.x, point.y, modifiers(keys))
                    } else {
                        match snapshot(paths, web) {
                            Ok((paths, web)) => {
                                ui.set_selection(paths, web);
                                true
                            }
                            Err(error) => {
                                ui.drop_error(&error);
                                false
                            }
                        }
                    }
                });
        *self.accepts.borrow_mut() = false;
        set_effect(effect, allowed);
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_modifier_bits() {
        assert!(modifiers(MK_SHIFT));
        assert!(modifiers(MK_CONTROL));
        assert!(modifiers(MODIFIERKEYS_FLAGS(MK_SHIFT.0 | MK_CONTROL.0)));
        assert!(!modifiers(MODIFIERKEYS_FLAGS(0)));
    }

    #[test]
    fn browser_drag_formats_and_invalid_selections() {
        let wide: Vec<_> = "https://example.com/한글.png\0"
            .encode_utf16()
            .flat_map(u16::to_le_bytes)
            .collect();
        let items = browser_urls("UniformResourceLocatorW", &wide).unwrap();
        assert_eq!(items[0].placeholder().file_name().unwrap(), "한글.png");
        let mozilla = b"https://example.com/a.jpg\nTitle\nhttps://example.com/b.png\nOther";
        assert_eq!(browser_urls("text/x-moz-url", mozilla).unwrap().len(), 2);
        let chrome = b"image/png:new name.png:https://example.com/content";
        assert_eq!(
            browser_urls("DownloadURL", chrome).unwrap()[0]
                .placeholder()
                .file_name()
                .unwrap(),
            "new name.png"
        );
        assert!(browser_urls("UniformResourceLocator", b"file:///C:/x").is_none());
        assert!(
            internet_shortcut(b"[InternetShortcut]\nURL=https://example.com/a.jpg\n").is_some()
        );
        assert!(
            internet_shortcut(
                b"[InternetShortcut]\nURL=https://example.com/a.jpg\nURL=file:///C:/x"
            )
            .is_none()
        );
        let html = "Version:1.0\r\n<!--StartFragment--><a href='page'><img src='https://example.com/x.png?a=1&amp;b=2'></a><!--EndFragment-->";
        assert_eq!(
            html_image(html),
            Some(Some("https://example.com/x.png?a=1&b=2".to_owned()))
        );
        assert_eq!(
            html_image("<a href='page'><span>link text</span></a>"),
            Some(None)
        );
        assert!(
            html_image("<span>selected text</span><img src='https://example.com/p.png'>").is_none()
        );
        assert!(html_image("<img src='a.png'><img src='b.png'>").is_none());
    }

    #[test]
    fn virtual_file_descriptors_require_safe_name_and_size() {
        let mut data = vec![0_u8; 4 + 592];
        data[..4].copy_from_slice(&1_u32.to_le_bytes());
        data[4..8].copy_from_slice(&64_u32.to_le_bytes());
        data[72..76].copy_from_slice(&42_u32.to_le_bytes());
        for (bytes, word) in data[76..]
            .chunks_exact_mut(2)
            .zip("image.png".encode_utf16())
        {
            bytes.copy_from_slice(&word.to_le_bytes());
        }
        assert_eq!(descriptors(&data), Some(vec![("image.png".to_owned(), 42)]));
        data[76] = b'/';
        assert!(descriptors(&data).is_none());
        assert!(descriptors(&data[..80]).is_none());
    }
}
