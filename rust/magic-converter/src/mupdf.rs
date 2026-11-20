use libloading::Library;
use std::ffi::{CString, c_char, c_void};
use std::io::{self, ErrorKind, Write};
use std::path::Path;
use std::ptr::{null, null_mut};
use std::sync::OnceLock;

type Handle = *mut c_void;
type Open = unsafe extern "C" fn(*const c_char, *mut Handle, *mut i32) -> i32;
type Close = unsafe extern "C" fn(Handle);
type Render = unsafe extern "C" fn(
    Handle,
    i32,
    i32,
    *mut Handle,
    *mut *const u8,
    *mut usize,
    *mut f32,
    *mut f32,
) -> i32;
type RenderRegion = unsafe extern "C" fn(
    Handle,
    i32,
    *const f32,
    i32,
    *mut Handle,
    *mut *const u8,
    *mut usize,
    *mut i32,
) -> i32;

struct Engine {
    _library: Library,
    open: Open,
    close: Close,
    png: Render,
    drop_png: Close,
    region: Option<RenderRegion>,
}

static ENGINE: OnceLock<Result<Engine, String>> = OnceLock::new();

fn load() -> io::Result<Engine> {
    let exe = std::env::current_exe()?;
    let name = if cfg!(target_os = "macos") {
        "libmagic_mupdf_bridge.dylib"
    } else {
        "magic_mupdf_bridge.dll"
    };
    let path = std::env::var_os("MAGIC_CONVERTER_MUPDF")
        .map(std::path::PathBuf::from)
        .into_iter()
        .chain(
            exe.ancestors()
                .skip(1)
                .map(|root| root.join("vendor/mupdf").join(name)),
        )
        .find(|path| path.is_absolute() && path.is_file())
        .ok_or_else(|| {
            io::Error::new(ErrorKind::NotFound, "MuPDF 렌더링 엔진을 찾을 수 없습니다.")
        })?;
    #[cfg(windows)]
    let library: Library = unsafe {
        libloading::os::windows::Library::load_with_flags(
            path,
            windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
                | windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_SYSTEM32,
        )
    }
    .map_err(io::Error::other)?
    .into();
    #[cfg(not(windows))]
    let library = unsafe { Library::new(path) }.map_err(io::Error::other)?;
    let open = *unsafe { library.get(b"mc_mupdf_open") }.map_err(io::Error::other)?;
    let close = *unsafe { library.get(b"mc_mupdf_close") }.map_err(io::Error::other)?;
    let png = *unsafe { library.get(b"mc_mupdf_png") }.map_err(io::Error::other)?;
    let drop_png = *unsafe { library.get(b"mc_mupdf_drop_png") }.map_err(io::Error::other)?;
    let region = unsafe { library.get(b"mc_mupdf_region") }
        .ok()
        .map(|function| *function);
    Ok(Engine {
        _library: library,
        open,
        close,
        png,
        drop_png,
        region,
    })
}

struct Owned(Handle, Close);
impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { (self.1)(self.0) };
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

pub(crate) struct Regions {
    document: Owned,
    engine: &'static Engine,
    render: RenderRegion,
}

impl Regions {
    pub fn open(source: &Path) -> Option<Self> {
        let engine = ENGINE
            .get_or_init(|| load().map_err(|error| error.to_string()))
            .as_ref()
            .ok()?;
        let render = engine.region?;
        let path = CString::new(source.to_str()?).ok()?;
        let mut document = null_mut();
        let mut count = 0;
        if unsafe { (engine.open)(path.as_ptr(), &mut document, &mut count) } != 0
            || document.is_null()
        {
            return None;
        }
        Some(Self {
            document: Owned(document, engine.close),
            engine,
            render,
        })
    }

    pub fn crop(
        &self,
        page: i32,
        bounds: crate::pdfium::Box2,
    ) -> Option<(crate::pdfium::Box2, Vec<u8>)> {
        const DPI: i32 = 200;
        let [l, t, r, b] = [bounds.x0, bounds.y0, bounds.x1, bounds.y1].map(|v| v as f32);
        let requested = [l - 0.5, t - 0.5, r + 0.5, b + 0.5];
        let (mut buffer, mut data, mut size) = (null_mut(), null(), 0);
        let mut origin = [0; 2];
        if unsafe {
            (self.render)(
                self.document.0,
                page,
                requested.as_ptr(),
                DPI,
                &mut buffer,
                &mut data,
                &mut size,
                origin.as_mut_ptr(),
            )
        } != 0
            || buffer.is_null()
        {
            return None;
        }
        let _owner = Owned(buffer, self.engine.drop_png);
        if data.is_null() || !(1..=256 * 1024 * 1024).contains(&size) {
            return None;
        }
        let pixels = image::load_from_memory_with_format(
            unsafe { std::slice::from_raw_parts(data, size) },
            image::ImageFormat::Png,
        )
        .ok()?
        .into_rgba8();
        let (mut left, mut top, mut right, mut bottom) = (pixels.width(), pixels.height(), 0, 0);
        for (x, y, pixel) in pixels.enumerate_pixels() {
            if pixel.0[3] != 0 {
                left = left.min(x);
                top = top.min(y);
                right = right.max(x + 1);
                bottom = bottom.max(y + 1);
            }
        }
        if right <= left || bottom <= top {
            return None;
        }
        let crop =
            image::imageops::crop_imm(&pixels, left, top, right - left, bottom - top).to_image();
        let mut png = std::io::Cursor::new(Vec::new());
        crop.write_to(&mut png, image::ImageFormat::Png).ok()?;
        let position = |axis: usize, pixel: u32| {
            f64::from(origin[axis] + pixel as i32) * 72.0 / f64::from(DPI)
        };
        let bounds = crate::pdfium::Box2 {
            x0: position(0, left),
            y0: position(1, top),
            x1: position(0, right),
            y1: position(1, bottom),
        };
        Some((bounds, png.into_inner()))
    }
}

pub(crate) fn png_pages(
    source: &Path,
    dpi: i32,
    limit: i32,
    check: &dyn Fn() -> io::Result<()>,
    mut page: impl FnMut(i32, f32, f32, &[u8]) -> io::Result<()>,
) -> io::Result<()> {
    check()?;
    let engine = ENGINE
        .get_or_init(|| load().map_err(|error| error.to_string()))
        .as_ref()
        .map_err(|error| io::Error::other(error.clone()))?;
    let path = CString::new(
        source
            .to_str()
            .ok_or_else(|| invalid("PDF 경로를 읽을 수 없습니다."))?,
    )
    .map_err(io::Error::other)?;
    let mut doc = null_mut();
    let mut pages = 0;
    match unsafe { (engine.open)(path.as_ptr(), &mut doc, &mut pages) } {
        0 if !doc.is_null() => {}
        2 => return Err(invalid("암호로 보호된 PDF는 잠금을 해제한 뒤 변환하세요.")),
        _ => return Err(invalid("PDF를 읽을 수 없습니다.")),
    }
    let doc = Owned(doc, engine.close);
    if !(1..=limit).contains(&pages) {
        return Err(invalid(&format!("PDF 페이지는 1~{limit}쪽이어야 합니다.")));
    }
    for index in 0..pages {
        check()?;
        let (mut buffer, mut data, mut size) = (null_mut(), null(), 0);
        let (mut width, mut height) = (0.0, 0.0);
        let status = unsafe {
            (engine.png)(
                doc.0,
                index,
                dpi,
                &mut buffer,
                &mut data,
                &mut size,
                &mut width,
                &mut height,
            )
        };
        if status != 0 || buffer.is_null() {
            return Err(invalid(if status == 3 {
                "PDF 페이지의 렌더링 크기 제한을 초과했습니다."
            } else {
                "PDF 페이지를 렌더링할 수 없습니다."
            }));
        }
        let _owner = Owned(buffer, engine.drop_png);
        if data.is_null() || !(1..=256 * 1024 * 1024).contains(&size) {
            return Err(invalid(
                "PDF 페이지 이미지 버퍼의 크기가 올바르지 않습니다.",
            ));
        }
        check()?;
        page(index, width, height, unsafe {
            std::slice::from_raw_parts(data, size)
        })?;
    }
    Ok(())
}

pub fn cbz(source: &Path, target: &Path, check: &dyn Fn() -> io::Result<()>) -> io::Result<()> {
    let mut archive = zip::ZipWriter::new(std::fs::File::create(target)?);
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Deflated);
    let mut total = 0_u64;
    png_pages(source, 144, 1000, check, |index, _, _, png| {
        total += png.len() as u64;
        if total > 1024 * 1024 * 1024 {
            return Err(invalid("만화 페이지 이미지의 출력 한도를 초과했습니다."));
        }
        archive.start_file(format!("{:05}.png", index + 1), options)?;
        archive.write_all(png)
    })?;
    archive.finish()?;
    Ok(())
}
