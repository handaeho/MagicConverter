use super::{Decoded, StillMetadata, invalid, pixels};
use image::RgbaImage;
use image::metadata::Orientation;
use libloading::Library;
use std::ffi::{CStr, c_char, c_int, c_void};
use std::fs::File;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::ptr::{null, null_mut};
use std::sync::OnceLock;

const RGB: c_int = 1;
const RGB24: c_int = 10;
const RGBA32: c_int = 11;
const INTERLEAVED: c_int = 10;
const MAX_FILE: u64 = 512 * 1024 * 1024;
const MAX_FRAMES: usize = 10_000;
type Handle = *mut c_void;
type Close = unsafe extern "C" fn(Handle);
type Check<'a> = &'a dyn Fn() -> io::Result<()>;

#[repr(C)]
struct Error {
    code: c_int,
    subcode: c_int,
    message: *const c_char,
}

impl Error {
    fn result(self) -> io::Result<()> {
        if self.code == 0 {
            return Ok(());
        }
        let message = if self.message.is_null() {
            format!("HEIF 변환 오류 {} / {}", self.code, self.subcode)
        } else {
            unsafe { CStr::from_ptr(self.message) }
                .to_string_lossy()
                .into_owned()
        };
        Err(invalid(&message))
    }
}

#[repr(C)]
struct ChromaOptions {
    version: u8,
    downsample: c_int,
    upsample: c_int,
    only_preferred: u8,
}

#[repr(C)]
struct Options {
    version: u8,
    ignore_transformations: u8,
    start_progress: Option<unsafe extern "C" fn(c_int, c_int, Handle)>,
    on_progress: Option<unsafe extern "C" fn(c_int, c_int, Handle)>,
    end_progress: Option<unsafe extern "C" fn(c_int, Handle)>,
    userdata: Handle,
    hdr_to_8bit: u8,
    strict: u8,
    decoder_id: *const c_char,
    chroma: ChromaOptions,
    cancel: Option<unsafe extern "C" fn(Handle) -> c_int>,
    extended_chroma: Handle,
    ignore_editlist: c_int,
    output_profile: Handle,
    library_threads: c_int,
    codec_threads: c_int,
}

#[repr(C)]
struct Writer {
    version: c_int,
    write: unsafe extern "C" fn(Handle, *const c_void, usize, Handle) -> Error,
}

macro_rules! api {
    ($($name:ident = $symbol:literal : $ty:ty),+ $(,)?) => {
        struct Engine { _library: Library, $($name: $ty),+ }
        impl Engine {
            fn load() -> io::Result<Self> {
                let path = library_path()?;
                #[cfg(windows)]
                let library: Library = unsafe {
                    libloading::os::windows::Library::load_with_flags(
                        path,
                        windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR
                            | windows_sys::Win32::System::LibraryLoader::LOAD_LIBRARY_SEARCH_SYSTEM32,
                    )
                }.map_err(io::Error::other)?.into();
                #[cfg(not(windows))]
                let library = unsafe { Library::new(path) }.map_err(io::Error::other)?;
                $(let $name: $ty = *unsafe { library.get($symbol) }.map_err(io::Error::other)?;)+
                Ok(Self { _library: library, $($name),+ })
            }
        }
    };
}

api! {
    context = b"heif_context_alloc": unsafe extern "C" fn() -> Handle,
    drop_context = b"heif_context_free": Close,
    max_width = b"heif_context_set_maximum_image_size_limit": unsafe extern "C" fn(Handle, c_int),
    read = b"heif_context_read_from_memory_without_copy": unsafe extern "C" fn(Handle, *const c_void, usize, Handle) -> Error,
    count = b"heif_context_get_number_of_top_level_images": unsafe extern "C" fn(Handle) -> c_int,
    ids = b"heif_context_get_list_of_top_level_image_IDs": unsafe extern "C" fn(Handle, *mut u32, c_int) -> c_int,
    image_handle = b"heif_context_get_image_handle": unsafe extern "C" fn(Handle, u32, *mut Handle) -> Error,
    handle_drop = b"heif_image_handle_release": Close,
    handle_width = b"heif_image_handle_get_width": unsafe extern "C" fn(Handle) -> c_int,
    handle_height = b"heif_image_handle_get_height": unsafe extern "C" fn(Handle) -> c_int,
    handle_depth = b"heif_image_handle_get_luma_bits_per_pixel": unsafe extern "C" fn(Handle) -> c_int,
    metadata = b"heif_image_handle_get_number_of_metadata_blocks": unsafe extern "C" fn(Handle, *const c_char) -> c_int,
    metadata_ids = b"heif_image_handle_get_list_of_metadata_block_IDs": unsafe extern "C" fn(Handle, *const c_char, *mut u32, c_int) -> c_int,
    metadata_size = b"heif_image_handle_get_metadata_size": unsafe extern "C" fn(Handle, u32) -> usize,
    read_metadata = b"heif_image_handle_get_metadata": unsafe extern "C" fn(Handle, u32, *mut c_void) -> Error,
    color_profile = b"heif_image_handle_get_raw_color_profile_size": unsafe extern "C" fn(Handle) -> usize,
    read_profile = b"heif_image_handle_get_raw_color_profile": unsafe extern "C" fn(Handle, *mut c_void) -> Error,
    alpha = b"heif_image_handle_has_alpha_channel": unsafe extern "C" fn(Handle) -> c_int,
    options = b"heif_decoding_options_alloc": unsafe extern "C" fn() -> Handle,
    options_drop = b"heif_decoding_options_free": Close,
    decode = b"heif_decode_image": unsafe extern "C" fn(Handle, *mut Handle, c_int, c_int, *const Options) -> Error,
    image_drop = b"heif_image_release": Close,
    width = b"heif_image_get_primary_width": unsafe extern "C" fn(Handle) -> c_int,
    height = b"heif_image_get_primary_height": unsafe extern "C" fn(Handle) -> c_int,
    plane = b"heif_image_get_plane_readonly2": unsafe extern "C" fn(Handle, c_int, *mut usize) -> *const u8,
    has_sequence = b"heif_context_has_sequence": unsafe extern "C" fn(Handle) -> c_int,
    track = b"heif_context_get_track": unsafe extern "C" fn(Handle, u32) -> Handle,
    track_drop = b"heif_track_release": Close,
    track_size = b"heif_track_get_image_resolution": unsafe extern "C" fn(Handle, *mut u16, *mut u16) -> Error,
    track_loop = b"heif_track_get_number_of_repetitions": unsafe extern "C" fn(Handle) -> u32,
    timescale = b"heif_track_get_timescale": unsafe extern "C" fn(Handle) -> u32,
    duration = b"heif_image_get_duration": unsafe extern "C" fn(Handle) -> u32,
    decode_next = b"heif_track_decode_next_image": unsafe extern "C" fn(Handle, *mut Handle, c_int, c_int, *const Options) -> Error,
    encoder = b"heif_context_get_encoder_for_format": unsafe extern "C" fn(Handle, c_int, *mut Handle) -> Error,
    encoder_drop = b"heif_encoder_release": Close,
    quality = b"heif_encoder_set_lossy_quality": unsafe extern "C" fn(Handle, c_int) -> Error,
    create = b"heif_image_create": unsafe extern "C" fn(c_int, c_int, c_int, c_int, *mut Handle) -> Error,
    add_plane = b"heif_image_add_plane": unsafe extern "C" fn(Handle, c_int, c_int, c_int, c_int) -> Error,
    plane_mut = b"heif_image_get_plane2": unsafe extern "C" fn(Handle, c_int, *mut usize) -> *mut u8,
    encode = b"heif_context_encode_image": unsafe extern "C" fn(Handle, Handle, Handle, Handle, *mut Handle) -> Error,
    sequence_clock = b"heif_context_set_sequence_timescale": unsafe extern "C" fn(Handle, u32),
    sequence_loop = b"heif_context_set_number_of_sequence_repetitions": unsafe extern "C" fn(Handle, u32),
    add_track = b"heif_context_add_visual_sequence_track": unsafe extern "C" fn(Handle, u16, u16, u32, Handle, Handle, *mut Handle) -> Error,
    set_duration = b"heif_image_set_duration": unsafe extern "C" fn(Handle, u32),
    encode_next = b"heif_track_encode_sequence_image": unsafe extern "C" fn(Handle, Handle, Handle, Handle) -> Error,
    encode_end = b"heif_track_encode_end_of_sequence": unsafe extern "C" fn(Handle, Handle) -> Error,
    write = b"heif_context_write": unsafe extern "C" fn(Handle, *mut Writer, Handle) -> Error,
}

static ENGINE: OnceLock<Result<Engine, String>> = OnceLock::new();

fn engine() -> io::Result<&'static Engine> {
    ENGINE
        .get_or_init(|| Engine::load().map_err(|err| err.to_string()))
        .as_ref()
        .map_err(|error| io::Error::other(error.clone()))
}

fn library_path() -> io::Result<PathBuf> {
    let name = if cfg!(windows) {
        "libheif.dll"
    } else if cfg!(target_os = "macos") {
        "libheif.dylib"
    } else {
        "libheif.so"
    };
    if let Some(path) = std::env::var_os("MAGIC_CONVERTER_HEIF").map(PathBuf::from)
        && path.is_absolute()
        && path.is_file()
    {
        return Ok(path);
    }
    if cfg!(target_os = "macos") {
        for library in [
            "/opt/homebrew/lib/libheif.dylib",
            "/usr/local/lib/libheif.dylib",
        ] {
            let path = PathBuf::from(library);
            if path.is_file() {
                return Ok(path);
            }
        }
    }
    runtime_library(name)
}

#[cfg(target_os = "macos")]
pub(super) fn available() -> bool {
    engine().is_ok()
}

pub(super) fn runtime_library(name: &str) -> io::Result<PathBuf> {
    let executable = std::env::current_exe()?;
    std::env::var_os("MAGIC_CONVERTER_HEIF")
        .map(PathBuf::from)
        .and_then(|path| path.parent().map(|parent| parent.join(name)))
        .into_iter()
        .chain(
            executable
                .ancestors()
                .skip(1)
                .take(6)
                .map(|root| root.join("vendor").join("heif").join(name)),
        )
        .find(|path| path.is_absolute() && path.is_file())
        .ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "HEIF/AVIF 변환 엔진을 찾을 수 없습니다.",
            )
        })
}

struct Owned {
    raw: Handle,
    close: Close,
}

impl Owned {
    fn new(raw: Handle, close: Close) -> io::Result<Self> {
        if raw.is_null() {
            Err(invalid("HEIF 리소스를 만들 수 없습니다."))
        } else {
            Ok(Self { raw, close })
        }
    }

    fn call(close: Close, call: impl FnOnce(*mut Handle) -> Error) -> io::Result<Self> {
        let mut raw = null_mut();
        let result = call(&mut raw);
        if result.code != 0 && !raw.is_null() {
            unsafe { close(raw) };
        }
        result.result()?;
        Self::new(raw, close)
    }
}

impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { (self.close)(self.raw) };
    }
}

fn decoded_frame(
    engine: &Engine,
    raw: Handle,
    total: &mut u64,
    limit: u64,
    check: Check<'_>,
) -> io::Result<RgbaImage> {
    let width = unsafe { (engine.width)(raw) }.max(0) as u32;
    let height = unsafe { (engine.height)(raw) }.max(0) as u32;
    *total = total.saturating_add(pixels(width, height, 1, 80_000_000)?);
    if *total > limit {
        return Err(invalid("이미지의 전체 프레임 픽셀 제한을 초과했습니다."));
    }
    let mut stride = 0;
    let source = unsafe { (engine.plane)(raw, INTERLEAVED, &mut stride) };
    let row_bytes = width as usize * 4;
    if source.is_null() || stride < row_bytes || stride > row_bytes + 65536 {
        return Err(invalid("HEIF 픽셀 행 크기가 올바르지 않습니다."));
    }
    let mut image = Vec::with_capacity(row_bytes * height as usize);
    for y in 0..height as usize {
        if y.is_multiple_of(128) {
            check()?;
        }
        image.extend_from_slice(unsafe {
            std::slice::from_raw_parts(source.add(y * stride), row_bytes)
        });
    }
    RgbaImage::from_raw(width, height, image)
        .ok_or_else(|| invalid("HEIF 픽셀 버퍼가 올바르지 않습니다."))
}

fn still_metadata(engine: &Engine, handle: Handle) -> io::Result<StillMetadata> {
    fn read(size: usize, fill: impl FnOnce(*mut c_void) -> Error) -> io::Result<Option<Vec<u8>>> {
        if size == 0 {
            return Ok(None);
        }
        if size > 16 * 1024 * 1024 {
            return Err(invalid("HEIF 메타데이터가 너무 큽니다."));
        }
        let mut data = vec![0; size];
        fill(data.as_mut_ptr().cast()).result()?;
        Ok(Some(data))
    }
    let icc = read(unsafe { (engine.color_profile)(handle) }, |out| unsafe {
        (engine.read_profile)(handle, out)
    })?;
    let mut id = 0;
    let exif = if unsafe { (engine.metadata_ids)(handle, c"Exif".as_ptr(), &mut id, 1) } == 1 {
        read(
            unsafe { (engine.metadata_size)(handle, id) },
            |out| unsafe { (engine.read_metadata)(handle, id, out) },
        )?
        .and_then(|data| {
            let field = data.get(..4)?.try_into().ok()?;
            let offset = 4_usize.checked_add(u32::from_be_bytes(field) as usize)?;
            let tail = data
                .get(offset..)
                .filter(|tail| tail.len() > 4)
                .or_else(|| data.get(4..))?;
            let mut exif = tail.strip_prefix(b"Exif\0\0").unwrap_or(tail).to_vec();
            let _ = Orientation::remove_from_exif_chunk(&mut exif);
            Some(exif)
        })
    } else {
        None
    };
    let xmp = unsafe { (engine.metadata)(handle, c"mime".as_ptr()) > 0 };
    let opaque = unsafe { (engine.alpha)(handle) == 0 };
    Ok(StillMetadata {
        icc,
        exif,
        xmp,
        opaque,
    })
}

pub(super) fn load(path: &Path, limit: u64, check: Check<'_>) -> io::Result<Decoded> {
    let mut bytes = Vec::new();
    let file = File::open(path)?;
    if file.metadata()?.len() > MAX_FILE {
        return Err(invalid("HEIF 입력 파일이 너무 큽니다."));
    }
    file.take(MAX_FILE + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_FILE {
        return Err(invalid("HEIF 입력 파일이 너무 큽니다."));
    }
    check()?;
    let engine = engine()?;
    let context = Owned::new(unsafe { (engine.context)() }, engine.drop_context)?;
    unsafe {
        (engine.max_width)(context.raw, 40_000);
        (engine.read)(context.raw, bytes.as_ptr().cast(), bytes.len(), null_mut()).result()?;
    }
    let options = Owned::new(unsafe { (engine.options)() }, engine.options_drop)?;
    let options_ptr = options.raw.cast::<Options>();
    unsafe {
        if (*options_ptr).version < 8 {
            return Err(invalid("HEIF 엔진 버전이 너무 낮습니다."));
        }
        (*options_ptr).hdr_to_8bit = 1;
        (*options_ptr).strict = 1;
        (*options_ptr).ignore_editlist = 1;
        (*options_ptr).codec_threads = 4;
    }
    let mut result = Decoded::still(false, false);
    let mut total = 0;
    if unsafe { (engine.has_sequence)(context.raw) } != 0 {
        let track = Owned::new(unsafe { (engine.track)(context.raw, 0) }, engine.track_drop)?;
        let mut width = 0_u16;
        let mut height = 0_u16;
        unsafe { (engine.track_size)(track.raw, &mut width, &mut height) }.result()?;
        pixels(u32::from(width), u32::from(height), 1, 80_000_000)?;
        let scale = f64::from(unsafe { (engine.timescale)(track.raw) });
        if scale <= 0.0 {
            return Err(invalid("HEIF 재생 시간 단위가 올바르지 않습니다."));
        }
        result.loop_count = match unsafe { (engine.track_loop)(track.raw) } {
            u32::MAX => 0,
            n => n.max(1),
        };
        loop {
            check()?;
            let mut raw = null_mut();
            let status =
                unsafe { (engine.decode_next)(track.raw, &mut raw, RGB, RGBA32, options_ptr) };
            if status.code == 13 {
                break;
            }
            status.result()?;
            let image = Owned::new(raw, engine.image_drop)?;
            if result.frames.len() >= MAX_FRAMES {
                return Err(invalid("HEIF 프레임 수 제한을 초과했습니다."));
            }
            result
                .frames
                .push(decoded_frame(engine, image.raw, &mut total, limit, check)?);
            result
                .durations
                .push(f64::from(unsafe { (engine.duration)(image.raw) }) * 1000.0 / scale);
        }
    } else {
        let count = unsafe { (engine.count)(context.raw) };
        if !(1..=MAX_FRAMES as i32).contains(&count) {
            return Err(invalid("HEIF 이미지 수가 올바르지 않습니다."));
        }
        let mut ids = vec![0_u32; count as usize];
        if unsafe { (engine.ids)(context.raw, ids.as_mut_ptr(), count) } != count {
            return Err(invalid("HEIF 이미지 목록을 읽을 수 없습니다."));
        }
        for id in ids {
            check()?;
            let handle = Owned::call(engine.handle_drop, |out| unsafe {
                (engine.image_handle)(context.raw, id, out)
            })?;
            let width = unsafe { (engine.handle_width)(handle.raw) }.max(0) as u32;
            let height = unsafe { (engine.handle_height)(handle.raw) }.max(0) as u32;
            pixels(width, height, 1, 80_000_000)?;
            result.metadata |= unsafe {
                (engine.metadata)(handle.raw, null()) > 0 || (engine.color_profile)(handle.raw) > 0
            };
            result.high_depth |= unsafe { (engine.handle_depth)(handle.raw) > 8 };
            if count == 1 {
                result.still_metadata = still_metadata(engine, handle.raw)?;
            }
            let image = Owned::call(engine.image_drop, |out| unsafe {
                (engine.decode)(handle.raw, out, RGB, RGBA32, options_ptr)
            })?;
            result
                .frames
                .push(decoded_frame(engine, image.raw, &mut total, limit, check)?);
            result.durations.push(100.0);
        }
    }
    if result.frames.is_empty() {
        return Err(invalid("HEIF 이미지 프레임이 없습니다."));
    }
    Ok(result)
}

fn encode_frame(engine: &Engine, frame: &RgbaImage, check: Check<'_>) -> io::Result<Owned> {
    let (width, height) = (frame.width() as i32, frame.height() as i32);
    let alpha = frame.pixels().any(|pixel| pixel.0[3] != 255);
    let format = if alpha { RGBA32 } else { RGB24 };
    let image = Owned::call(engine.image_drop, |out| unsafe {
        (engine.create)(width, height, RGB, format, out)
    })?;
    unsafe { (engine.add_plane)(image.raw, INTERLEAVED, width, height, 8) }.result()?;
    let mut stride = 0;
    let target = unsafe { (engine.plane_mut)(image.raw, INTERLEAVED, &mut stride) };
    let channels = if alpha { 4 } else { 3 };
    let row_bytes = width as usize * channels;
    if target.is_null() || stride < row_bytes || stride > row_bytes + 65536 {
        return Err(invalid("HEIF 출력 픽셀 행 크기가 올바르지 않습니다."));
    }
    for (y, row) in frame.as_raw().chunks_exact(width as usize * 4).enumerate() {
        if y.is_multiple_of(128) {
            check()?;
        }
        let target = unsafe { std::slice::from_raw_parts_mut(target.add(y * stride), row_bytes) };
        if alpha {
            target.copy_from_slice(row);
        } else {
            for (pixel, output) in row.chunks_exact(4).zip(target.chunks_exact_mut(3)) {
                output.copy_from_slice(&pixel[..3]);
            }
        }
    }
    Ok(image)
}

struct Output<'a> {
    file: File,
    error: Option<io::Error>,
    check: Check<'a>,
}

unsafe extern "C" fn write_callback(
    _: Handle,
    bytes: *const c_void,
    size: usize,
    userdata: Handle,
) -> Error {
    let output = unsafe { &mut *userdata.cast::<Output<'_>>() };
    if size == 0 {
        return Error {
            code: 0,
            subcode: 0,
            message: null(),
        };
    }
    let result = (output.check)().and_then(|()| {
        output
            .file
            .write_all(unsafe { std::slice::from_raw_parts(bytes.cast::<u8>(), size) })
    });
    if let Err(error) = result {
        output.error = Some(error);
        Error {
            code: 9,
            subcode: 0,
            message: c"Output write failed".as_ptr(),
        }
    } else {
        Error {
            code: 0,
            subcode: 0,
            message: null(),
        }
    }
}

pub(super) fn write(
    file: File,
    frames: &[RgbaImage],
    durations: &[f64],
    loops: u32,
    kind: &str,
    quality: u8,
    check: Check<'_>,
) -> io::Result<()> {
    let engine = engine()?;
    let context = Owned::new(unsafe { (engine.context)() }, engine.drop_context)?;
    let codec = if kind == "avif" { 4 } else { 1 };
    let encoder = Owned::call(engine.encoder_drop, |out| unsafe {
        (engine.encoder)(context.raw, codec, out)
    })?;
    unsafe { (engine.quality)(encoder.raw, i32::from(quality)) }.result()?;
    let sequence = if kind == "avif" && frames.len() > 1 {
        let (width, height) = frames[0].dimensions();
        if frames
            .iter()
            .any(|frame| frame.dimensions() != (width, height))
        {
            return Err(invalid("AVIF 재생 프레임의 크기가 서로 다릅니다."));
        }
        let width = u16::try_from(width).map_err(io::Error::other)?;
        let height = u16::try_from(height).map_err(io::Error::other)?;
        unsafe {
            (engine.sequence_clock)(context.raw, 1000);
            (engine.sequence_loop)(context.raw, if loops == 0 { u32::MAX } else { loops });
        }
        Some(Owned::call(engine.track_drop, |out| unsafe {
            (engine.add_track)(
                context.raw,
                width,
                height,
                u32::from_be_bytes(*b"pict"),
                null_mut(),
                null_mut(),
                out,
            )
        })?)
    } else {
        None
    };
    for (index, frame) in frames.iter().enumerate() {
        check()?;
        let image = encode_frame(engine, frame, check)?;
        if let Some(track) = &sequence {
            let duration = durations
                .get(index)
                .copied()
                .unwrap_or(100.0)
                .round()
                .clamp(1.0, u32::MAX as f64);
            unsafe {
                (engine.set_duration)(image.raw, duration as u32);
                (engine.encode_next)(track.raw, image.raw, encoder.raw, null_mut()).result()?;
            }
        } else {
            let _handle = Owned::call(engine.handle_drop, |out| unsafe {
                (engine.encode)(context.raw, image.raw, encoder.raw, null_mut(), out)
            })?;
        }
    }
    if let Some(track) = &sequence {
        unsafe { (engine.encode_end)(track.raw, encoder.raw) }.result()?;
    }
    let mut output = Output {
        file,
        error: None,
        check,
    };
    let mut writer = Writer {
        version: 1,
        write: write_callback,
    };
    let status = unsafe {
        (engine.write)(
            context.raw,
            &mut writer,
            (&mut output as *mut Output<'_>).cast(),
        )
    };
    if let Some(error) = output.error {
        return Err(error);
    }
    status.result()?;
    output.file.flush()?;
    check()
}
