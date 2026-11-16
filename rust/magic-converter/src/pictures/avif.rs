use super::{Decoded, invalid, pixels};
use image::{DynamicImage, RgbImage, RgbaImage, metadata::Orientation};
use libloading::Library;
use std::ffi::{CStr, c_char, c_int, c_void};
use std::fs::File;
use std::io::{self, Read, Write};
use std::ptr::{null_mut, slice_from_raw_parts};
use std::sync::OnceLock;

type Handle = *mut c_void;
type Check<'a> = &'a dyn Fn() -> io::Result<()>;
type Close = unsafe extern "C" fn(Handle);
const MAX_FILE: u64 = 512 * 1024 * 1024;

#[repr(C)]
#[derive(Default)]
struct Info {
    width: u32,
    height: u32,
    count: u32,
    high_depth: u32,
    alpha: u32,
    orientation: u32,
    metadata: u32,
    repetitions: i32,
}

#[repr(C)]
struct Buffer {
    data: *mut u8,
    len: usize,
}

#[repr(C)]
struct MetadataBlock {
    data: *const u8,
    len: usize,
}

impl MetadataBlock {
    fn copy(&self) -> io::Result<Option<Vec<u8>>> {
        if self.len > 16 * 1024 * 1024 {
            return Err(invalid("AVIF 메타데이터가 너무 큽니다."));
        }
        Ok((self.len != 0)
            .then(|| unsafe { std::slice::from_raw_parts(self.data, self.len).to_vec() }))
    }
}

macro_rules! api {
    ($($name:ident: $ty:ty),+ $(,)?) => {
        struct Engine { _library: Library, $($name: $ty),+ }
        impl Engine {
            fn load() -> io::Result<Self> {
                let path = super::heif::runtime_library("magic_avif_bridge.dll")?;
                #[cfg(windows)]
                let library: Library = unsafe {
                    use windows_sys::Win32::System::LibraryLoader::*;
                    libloading::os::windows::Library::load_with_flags(
                        path, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32,
                    )
                }.map_err(io::Error::other)?.into();
                #[cfg(not(windows))]
                let library = unsafe { Library::new(path) }.map_err(io::Error::other)?;
                $(let $name: $ty = *unsafe {
                    library.get(concat!("magic_avif_", stringify!($name), "\0").as_bytes())
                }.map_err(io::Error::other)?;)+
                Ok(Self { _library: library, $($name),+ })
            }
        }
    }
}

api! {
    open: unsafe extern "C" fn(*const u8, usize, u32, *mut Handle, *mut Info) -> c_int,
    read: unsafe extern "C" fn(Handle, *mut u8, usize, *mut f64) -> c_int,
    close: Close,
    metadata: unsafe extern "C" fn(Handle, *mut MetadataBlock),
    encoder: unsafe extern "C" fn(c_int, c_int) -> Handle,
    add: unsafe extern "C" fn(Handle, u32, u32, *const u8, c_int, u64, c_int) -> c_int,
    finish: unsafe extern "C" fn(Handle, *mut Buffer) -> c_int,
    free: unsafe extern "C" fn(*mut Buffer),
    drop_encoder: Close,
    error: unsafe extern "C" fn(c_int) -> *const c_char,
}

impl Engine {
    fn result(&self, code: c_int) -> io::Result<()> {
        if code == 0 {
            return Ok(());
        }
        let message = unsafe { CStr::from_ptr((self.error)(code)) }.to_string_lossy();
        Err(invalid(&format!("AVIF: {message}")))
    }
}

fn engine() -> io::Result<&'static Engine> {
    static ENGINE: OnceLock<Result<Engine, String>> = OnceLock::new();
    ENGINE
        .get_or_init(|| Engine::load().map_err(|err| err.to_string()))
        .as_ref()
        .map_err(|error| io::Error::other(error.clone()))
}

struct Owned(Handle, Close);

impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { (self.1)(self.0) }
    }
}

pub(super) fn load(path: &std::path::Path, limit: u64, check: Check<'_>) -> io::Result<Decoded> {
    let file = File::open(path)?;
    let mut data = Vec::new();
    file.take(MAX_FILE + 1).read_to_end(&mut data)?;
    if data.len() as u64 > MAX_FILE {
        return Err(invalid("AVIF 입력 파일이 너무 큽니다."));
    }
    check()?;
    let engine = engine()?;
    let mut info = Info::default();
    let mut handle = null_mut();
    engine.result(unsafe {
        (engine.open)(
            data.as_ptr(),
            data.len(),
            limit.min(u64::from(u32::MAX)) as u32,
            &mut handle,
            &mut info,
        )
    })?;
    let decoder = Owned(handle, engine.close);
    pixels(info.width, info.height, u64::from(info.count), limit)?;
    let orientation =
        Orientation::from_exif(info.orientation as u8).unwrap_or(Orientation::NoTransforms);
    let mut decoded = Decoded::still(
        info.metadata != 0 || orientation != Orientation::NoTransforms,
        info.high_depth != 0,
    );
    if info.count == 1 {
        let mut blocks = std::array::from_fn::<_, 3, _>(|_| MetadataBlock {
            data: null_mut(),
            len: 0,
        });
        unsafe { (engine.metadata)(decoder.0, blocks.as_mut_ptr()) };
        decoded.still_metadata.icc = blocks[0].copy()?;
        decoded.still_metadata.exif = blocks[1].copy()?.map(|mut bytes| {
            if bytes.starts_with(b"Exif\0\0") {
                bytes.drain(..6);
            }
            let _ = Orientation::remove_from_exif_chunk(&mut bytes);
            bytes
        });
        decoded.still_metadata.xmp = blocks[2].len > 0;
        decoded.still_metadata.opaque = info.alpha == 0;
    }
    decoded.loop_count = info.repetitions.max(0) as u32;
    let channels = if info.alpha != 0 { 4 } else { 3 };
    let frame_size = (u64::from(info.width) * u64::from(info.height) * channels) as usize;
    for _ in 0..info.count {
        check()?;
        let mut bytes = vec![0; frame_size];
        let mut duration = 0.0;
        engine.result(unsafe {
            (engine.read)(decoder.0, bytes.as_mut_ptr(), bytes.len(), &mut duration)
        })?;
        let mut image = if channels == 4 {
            RgbaImage::from_raw(info.width, info.height, bytes).map(DynamicImage::ImageRgba8)
        } else {
            RgbImage::from_raw(info.width, info.height, bytes).map(DynamicImage::ImageRgb8)
        }
        .ok_or_else(|| invalid("AVIF 픽셀 버퍼가 올바르지 않습니다."))?;
        image.apply_orientation(orientation);
        decoded.frames.push(image.into_rgba8());
        let duration = duration.round_ties_even();
        decoded.durations.push(if info.count > 1 && duration > 0.0 {
            duration
        } else {
            100.0
        });
    }
    Ok(decoded)
}

pub(super) fn write(
    mut file: File,
    frames: &[RgbaImage],
    durations: &[f64],
    loop_count: u32,
    quality: u8,
    check: Check<'_>,
) -> io::Result<()> {
    let engine = engine()?;
    let repetitions = if loop_count == 0 {
        -1
    } else {
        loop_count.min(i32::MAX as u32) as i32
    };
    let handle = unsafe { (engine.encoder)(c_int::from(quality), repetitions) };
    if handle.is_null() {
        return Err(invalid("AVIF 인코더를 만들 수 없습니다."));
    }
    let encoder = Owned(handle, engine.drop_encoder);
    for (index, frame) in frames.iter().enumerate() {
        check()?;
        let duration = durations
            .get(index)
            .copied()
            .unwrap_or(100.0)
            .round_ties_even() as u64;
        engine.result(unsafe {
            (engine.add)(
                encoder.0,
                frame.width(),
                frame.height(),
                frame.as_raw().as_ptr(),
                1,
                duration,
                c_int::from(frames.len() == 1),
            )
        })?;
    }
    let mut output = Buffer {
        data: null_mut(),
        len: 0,
    };
    let result = engine
        .result(unsafe { (engine.finish)(encoder.0, &mut output) })
        .and_then(|()| check())
        .and_then(|()| {
            let bytes = unsafe { slice_from_raw_parts(output.data, output.len).as_ref() }
                .filter(|data| !data.is_empty())
                .ok_or_else(|| invalid("AVIF 출력이 비어 있습니다."))?;
            file.write_all(bytes)
        });
    unsafe { (engine.free)(&mut output) };
    result
}
