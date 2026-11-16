use libloading::Library;
use std::ffi::{CString, c_char, c_int, c_ulong, c_void};
use std::fs::File;
use std::io::{self, BufWriter, ErrorKind, Write};
use std::path::{Path, PathBuf};
use std::ptr::null;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

mod layout;
pub(crate) use layout::{Box2, Drawing, Page, PathCommand, Span, pages};

type Handle = *mut c_void;
type Close = unsafe extern "system" fn(Handle);
type Check<'a> = &'a dyn Fn() -> io::Result<()>;
const MAX_PIXELS: u64 = 80_000_000;
const MAX_TEXT: i32 = 16_000_000;
static SESSION: Mutex<()> = Mutex::new(());
static ENGINE: OnceLock<Result<Engine, String>> = OnceLock::new();

macro_rules! api {
    ($($name:ident = $symbol:literal : $ty:ty),+ $(,)?) => {
        struct Engine {
            _library: Library,
            $($name: $ty),+
        }
        impl Engine {
            fn load() -> io::Result<Self> {
                let path = library_path()?;
                #[cfg(windows)]
                let library: Library = unsafe {
                    libloading::os::windows::Library::load_with_flags(
                        &path,
                        windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
                            | windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_SYSTEM32,
                    )
                }.map_err(io::Error::other)?.into();
                #[cfg(not(windows))]
                let library = unsafe { Library::new(&path) }.map_err(io::Error::other)?;
                $(let $name: $ty = *unsafe { library.get($symbol) }.map_err(io::Error::other)?;)+
                let engine = Self { _library: library, $($name),+ };
                unsafe { (engine.init)() };
                Ok(engine)
            }
        }
    };
}

api! {
    init = b"FPDF_InitLibrary": unsafe extern "system" fn(),
    open = b"FPDF_LoadDocument": unsafe extern "system" fn(*const c_char, *const c_char) -> Handle,
    close_document = b"FPDF_CloseDocument": Close,
    page_count = b"FPDF_GetPageCount": unsafe extern "system" fn(Handle) -> c_int,
    load_page = b"FPDF_LoadPage": unsafe extern "system" fn(Handle, c_int) -> Handle,
    close_page = b"FPDF_ClosePage": Close,
    last_error = b"FPDF_GetLastError": unsafe extern "system" fn() -> c_ulong,
    width = b"FPDF_GetPageWidthF": unsafe extern "system" fn(Handle) -> f32,
    height = b"FPDF_GetPageHeightF": unsafe extern "system" fn(Handle) -> f32,
    create_bitmap = b"FPDFBitmap_CreateEx": unsafe extern "system" fn(c_int, c_int, c_int, Handle, c_int) -> Handle,
    free_bitmap = b"FPDFBitmap_Destroy": Close,
    render = b"FPDF_RenderPageBitmap": unsafe extern "system" fn(Handle, Handle, c_int, c_int, c_int, c_int, c_int, c_int),
    load_text = b"FPDFText_LoadPage": unsafe extern "system" fn(Handle) -> Handle,
    close_text = b"FPDFText_ClosePage": Close,
    chars = b"FPDFText_CountChars": unsafe extern "system" fn(Handle) -> c_int,
    text_index = b"FPDFText_GetTextIndexFromCharIndex": unsafe extern "system" fn(Handle, c_int) -> c_int,
    text = b"FPDFText_GetText": unsafe extern "system" fn(Handle, c_int, c_int, *mut u16) -> c_int,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

fn library_path() -> io::Result<PathBuf> {
    let name = if cfg!(windows) {
        "pdfium.dll"
    } else {
        "libpdfium.so"
    };
    let executable = std::env::current_exe()?;
    std::env::var_os("MAGIC_CONVERTER_PDFIUM")
        .map(PathBuf::from)
        .into_iter()
        .chain(
            executable
                .ancestors()
                .skip(1)
                .take(6)
                .map(|root| root.join("vendor").join("pdfium").join(name)),
        )
        .find(|path| path.is_absolute() && path.is_file())
        .ok_or_else(|| io::Error::new(ErrorKind::NotFound, "PDFium 변환 엔진을 찾을 수 없습니다."))
}

struct Owned {
    value: Handle,
    close: Close,
}

impl Owned {
    fn new(value: Handle, close: Close, error: impl FnOnce() -> io::Error) -> io::Result<Self> {
        if value.is_null() {
            Err(error())
        } else {
            Ok(Self { value, close })
        }
    }
}

impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { (self.close)(self.value) };
    }
}

fn read<T>(
    source: &Path,
    check: Check<'_>,
    run: impl FnOnce(&Engine, Handle, i32) -> io::Result<T>,
) -> io::Result<T> {
    let _session = loop {
        check()?;
        match SESSION.try_lock() {
            Ok(session) => break session,
            Err(std::sync::TryLockError::WouldBlock) => {
                std::thread::sleep(Duration::from_millis(40))
            }
            Err(error) => return Err(io::Error::other(error.to_string())),
        }
    };
    let engine = ENGINE
        .get_or_init(|| Engine::load().map_err(|err| err.to_string()))
        .as_ref()
        .map_err(|error| io::Error::other(error.clone()))?;
    let name = CString::new(
        source
            .to_str()
            .ok_or_else(|| invalid("PDF 경로를 UTF-8로 표현할 수 없습니다."))?,
    )
    .map_err(io::Error::other)?;
    let document = Owned::new(
        unsafe { (engine.open)(name.as_ptr(), null()) },
        engine.close_document,
        || {
            if unsafe { (engine.last_error)() } == 4 {
                invalid("암호로 보호된 PDF는 잠금을 해제한 뒤 변환하세요.")
            } else {
                invalid("PDF를 읽을 수 없습니다.")
            }
        },
    )?;
    let count = unsafe { (engine.page_count)(document.value) };
    if count < 1 {
        return Err(invalid("PDF에 페이지가 없습니다."));
    }
    run(engine, document.value, count)
}

fn with_pages<T>(
    path: &Path,
    check: Check<'_>,
    mut visit: impl FnMut(&Engine, Handle, i32) -> io::Result<T>,
) -> io::Result<Vec<T>> {
    read(path, check, |engine, document, count| {
        (0..count)
            .map(|index| {
                check()?;
                let page = Owned::new(
                    unsafe { (engine.load_page)(document, index) },
                    engine.close_page,
                    || invalid("PDF 페이지를 읽을 수 없습니다."),
                )?;
                visit(engine, page.value, index)
            })
            .collect()
    })
}

pub fn page_count(source: &Path, check: Check<'_>) -> io::Result<i32> {
    read(source, check, |_, _, count| Ok(count))
}

pub fn text(source: &Path, check: Check<'_>) -> io::Result<String> {
    let pages = with_pages(source, check, |engine, page, _| {
        let text = Owned::new(
            unsafe { (engine.load_text)(page) },
            engine.close_text,
            || invalid("PDF 페이지에서 텍스트를 읽을 수 없습니다."),
        )?;
        let count = unsafe { (engine.chars)(text.value) };
        if !(0..=MAX_TEXT).contains(&count) {
            return Err(invalid("PDF 페이지의 텍스트가 너무 많거나 잘못되었습니다."));
        }
        let position = |index| unsafe { (engine.text_index)(text.value, index) };
        let mut first = 0;
        while first < count && position(first) < 0 {
            first += 1;
        }
        if first == count {
            return Ok(String::new());
        }
        let mut last = count - 1;
        while last > first && position(last) < 0 {
            last -= 1;
        }
        let length = position(last) - position(first) + 2;
        if !(2..=2 * MAX_TEXT + 1).contains(&length) {
            return Err(invalid("PDF 텍스트 인덱스가 올바르지 않습니다."));
        }
        let mut output = vec![0_u16; length.max(last - first + 2) as usize];
        let written =
            unsafe { (engine.text)(text.value, first, last - first + 1, output.as_mut_ptr()) };
        let output = output
            .get(..written.saturating_sub(1) as usize)
            .ok_or_else(|| invalid("PDF 텍스트 길이가 올바르지 않습니다."))?;
        let text: String = char::decode_utf16(output.iter().copied())
            .filter_map(Result::ok)
            .collect();
        Ok(text.replace("\r\n", "\n"))
    })?;
    let body = pages.join("\n\n");
    if body.trim().is_empty() {
        return Err(invalid(
            "추출 가능한 텍스트가 없습니다. 스캔 문서 OCR은 지원하지 않습니다.",
        ));
    }
    Ok(body)
}

fn rgb(engine: &Engine, page: Handle) -> io::Result<image::RgbImage> {
    let scale = 300.0 / 72.0;
    let width = (f64::from(unsafe { (engine.width)(page) }) * scale).ceil();
    let height = (f64::from(unsafe { (engine.height)(page) }) * scale).ceil();
    if !(1.0..=MAX_PIXELS as f64).contains(&width)
        || !(1.0..=MAX_PIXELS as f64).contains(&height)
        || width * height > MAX_PIXELS as f64
    {
        return Err(invalid("PDF 페이지의 렌더링 크기 제한을 초과했습니다."));
    }
    let (width, height) = (width as u32, height as u32);
    let stride = width * 3;
    let mut bytes = vec![255; (u64::from(stride) * u64::from(height)) as usize];
    {
        let bitmap = Owned::new(
            unsafe {
                (engine.create_bitmap)(
                    width as i32,
                    height as i32,
                    2,
                    bytes.as_mut_ptr().cast(),
                    stride as i32,
                )
            },
            engine.free_bitmap,
            || invalid("PDF 비트맵을 만들 수 없습니다."),
        )?;
        unsafe { (engine.render)(bitmap.value, page, 0, 0, width as i32, height as i32, 0, 1) };
    }
    for pixel in bytes.chunks_exact_mut(3) {
        pixel.swap(0, 2);
    }
    image::RgbImage::from_raw(width, height, bytes)
        .ok_or_else(|| invalid("PDF 픽셀 버퍼 크기가 잘못되었습니다."))
}

pub fn render(source: &Path, folder: &Path, suffix: &str, check: Check<'_>) -> io::Result<()> {
    with_pages(source, check, |engine, page, index| {
        let image = rgb(engine, page)?;
        check()?;
        let path = folder.join(format!("page_{:04}.{suffix}", index + 1));
        let mut file = BufWriter::new(File::create(path)?);
        if suffix == "jpg" {
            crate::pictures::write_page_jpeg(&image, &mut file)?;
        } else {
            let mut encoder = png::Encoder::new(&mut file, image.width(), image.height());
            encoder.set_color(png::ColorType::Rgb);
            encoder.set_depth(png::BitDepth::Eight);
            encoder.set_pixel_dims(Some(png::PixelDimensions {
                xppu: 11811,
                yppu: 11811,
                unit: png::Unit::Meter,
            }));
            let mut png = encoder.write_header().map_err(io::Error::other)?;
            png.write_image_data(image.as_raw())
                .map_err(io::Error::other)?;
            png.finish().map_err(io::Error::other)?;
        }
        file.flush()?;
        check()
    })?;
    Ok(())
}
