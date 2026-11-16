mod apng;
mod avif;
mod codecs;
mod heif;

use crate::{Edge, svg};
use image::{Rgb, RgbImage, Rgba, RgbaImage, imageops};
use serde::Deserialize;
use serde_json::json;
use std::fs::{self, File};
use std::io::{self, ErrorKind};
use std::path::Path;

const TOOL_PIXELS: u64 = 128_000_000;
const DECODE_PIXELS: u64 = 240_000_000;
const METADATA: &str =
    "이 이미지 출력 경로는 원본의 색상 프로필·해상도·EXIF/XMP 메타데이터를 보존하지 않습니다.";
const DEPTH: &str = "이 이미지 출력 경로는 원본의 높은 비트 깊이를 8비트 픽셀로 변환합니다.";

#[derive(Clone, Deserialize)]
#[serde(default)]
pub struct Options {
    image_quality: u8,
    image_max_edge: u32,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            image_quality: 90,
            image_max_edge: 0,
        }
    }
}

#[derive(Clone, Deserialize)]
#[serde(default)]
pub struct ToolOptions {
    rotation: u16,
    flip: String,
    crop_x: u32,
    crop_y: u32,
    crop_width: u32,
    crop_height: u32,
    frame_interval: usize,
    max_frames: usize,
}

impl Default for ToolOptions {
    fn default() -> Self {
        Self {
            rotation: 90,
            flip: "horizontal".to_owned(),
            crop_x: 0,
            crop_y: 0,
            crop_width: 0,
            crop_height: 0,
            frame_interval: 1,
            max_frames: 10_000,
        }
    }
}

#[derive(Clone)]
struct Timeline {
    poster: bool,
    delays: Vec<(u16, u16)>,
}

struct Decoded {
    frames: Vec<RgbaImage>,
    durations: Vec<f64>,
    loop_count: u32,
    gif_loops: Option<u32>,
    timeline: Option<Timeline>,
    metadata: bool,
    high_depth: bool,
    still_metadata: StillMetadata,
}

#[derive(Default)]
struct StillMetadata {
    icc: Option<Vec<u8>>,
    exif: Option<Vec<u8>>,
    xmp: bool,
    opaque: bool,
}

impl Decoded {
    fn still(metadata: bool, high_depth: bool) -> Self {
        Self {
            frames: Vec::new(),
            durations: Vec::new(),
            loop_count: 0,
            gif_loops: None,
            timeline: None,
            metadata,
            high_depth,
            still_metadata: StillMetadata::default(),
        }
    }

    fn warn(&self, notices: &mut Vec<String>) {
        if self.metadata {
            notices.push(METADATA.to_owned());
        }
        if self.high_depth {
            notices.push(DEPTH.to_owned());
        }
    }
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn pixels(width: u32, height: u32, frames: u64, limit: u64) -> io::Result<u64> {
    u64::from(width)
        .checked_mul(u64::from(height))
        .and_then(|pixels| pixels.checked_mul(frames))
        .filter(|&pixels| pixels != 0 && pixels <= limit)
        .ok_or_else(|| {
            invalid(if limit == TOOL_PIXELS {
                "이미지 도구의 전체 프레임 픽셀 제한을 초과했습니다. 프레임 추출을 사용하세요."
            } else {
                "이미지의 전체 프레임 픽셀 제한을 초과했습니다."
            })
        })
}

pub fn supports(edge: &Edge) -> bool {
    let input = readable(&edge.source);
    let output = matches!(
        edge.target.as_str(),
        "png" | "jpg" | "bmp" | "tiff" | "webp" | "heic" | "avif"
    ) || edge.target == "pdf";
    input && output
}

pub(crate) fn readable(kind: &str) -> bool {
    matches!(
        kind,
        "png" | "jpg" | "bmp" | "tiff" | "webp" | "gif" | "svg" | "heic" | "avif"
    )
}

fn resize(frame: &RgbaImage, width: u32, height: u32) -> RgbaImage {
    if frame.dimensions() == (width, height) {
        return frame.clone();
    }
    let mut premultiplied = frame.clone();
    for pixel in premultiplied.pixels_mut() {
        let alpha = u32::from(pixel.0[3]);
        for value in &mut pixel.0[..3] {
            *value = ((u32::from(*value) * alpha + 127) / 255) as u8;
        }
    }
    // Match Pillow's separable 8-bit Lanczos, including the rounding between passes.
    if width != frame.width() {
        premultiplied = resample_axis(&premultiplied, width, true);
    }
    let mut resized = if height != frame.height() {
        resample_axis(&premultiplied, height, false)
    } else {
        premultiplied
    };
    for pixel in resized.pixels_mut() {
        let alpha = u32::from(pixel.0[3]);
        if alpha != 0 {
            for value in &mut pixel.0[..3] {
                *value = (u32::from(*value) * 255 / alpha).min(255) as u8;
            }
        }
    }
    resized
}

fn resample_axis(frame: &RgbaImage, length: u32, horizontal: bool) -> RgbaImage {
    const PRECISION: i32 = 22;
    let input = if horizontal {
        frame.width()
    } else {
        frame.height()
    };
    let scale = f64::from(input) / f64::from(length);
    let support = 3.0 * scale.max(1.0);
    let inverse = 1.0 / scale.max(1.0);
    let sinc = |x: f64| {
        let angle = x * std::f64::consts::PI;
        if angle == 0.0 {
            1.0
        } else {
            angle.sin() / angle
        }
    };
    let coefficients: Vec<_> = (0..length)
        .map(|index| {
            let center = (f64::from(index) + 0.5) * scale;
            let first = (center - support + 0.5) as u32;
            let end = ((center + support + 0.5) as u32).min(input);
            let weights: Vec<_> = (first..end)
                .map(|sample| {
                    let x = (f64::from(sample) - center + 0.5) * inverse;
                    if (-3.0..3.0).contains(&x) {
                        sinc(x) * sinc(x / 3.0)
                    } else {
                        0.0
                    }
                })
                .collect();
            let sum: f64 = weights.iter().sum();
            let fixed: Vec<_> = weights
                .iter()
                .map(|weight| ((weight / sum) * f64::from(1 << PRECISION)).round() as i32)
                .collect();
            (first, fixed)
        })
        .collect();
    let (width, height) = if horizontal {
        (length, frame.height())
    } else {
        (frame.width(), length)
    };
    RgbaImage::from_fn(width, height, |x, y| {
        let index = if horizontal { x } else { y } as usize;
        let (first, weights) = &coefficients[index];
        let mut sums = [1 << (PRECISION - 1); 4];
        for (offset, weight) in weights.iter().enumerate() {
            let position = first + offset as u32;
            let pixel = if horizontal {
                frame.get_pixel(position, y)
            } else {
                frame.get_pixel(x, position)
            };
            for (sum, channel) in sums.iter_mut().zip(pixel.0) {
                *sum += weight * i32::from(channel);
            }
        }
        image::Rgba(sums.map(|sum| (sum >> PRECISION).clamp(0, 255) as u8))
    })
}

fn thumbnail((width, height): (u32, u32), max_edge: u32) -> (u32, u32) {
    if max_edge == 0 || width.max(height) <= max_edge {
        return (width, height);
    }
    let scaled = |minor, major| {
        (f64::from(minor) * f64::from(max_edge) / f64::from(major))
            .round_ties_even()
            .max(1.0) as u32
    };
    if width > height {
        (max_edge, scaled(height, width))
    } else {
        (scaled(width, height), max_edge)
    }
}

pub(crate) fn white(frame: &RgbaImage) -> RgbImage {
    RgbImage::from_fn(frame.width(), frame.height(), |x, y| {
        let [r, g, b, a] = frame.get_pixel(x, y).0;
        let a = u32::from(a);
        Rgb([r, g, b]
            .map(|component| ((u32::from(component) * a + 255 * (255 - a) + 127) / 255) as u8))
    })
}

pub(crate) fn write_pdf_jpeg(image: &RgbImage, writer: &mut impl std::io::Write) -> io::Result<()> {
    codecs::encode_jpg(writer, image, 75, false)
}

pub(crate) fn write_page_jpeg(
    image: &RgbImage,
    writer: &mut impl std::io::Write,
) -> io::Result<()> {
    codecs::encode_jpg_density(writer, image, 94, false, Some(300))
}

pub(crate) fn to_pdf(
    source: &Path,
    output: &Path,
    format: &str,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    if format == "svg" {
        return svg::pdf(source, &mut File::create(output)?, check, notices);
    }
    let decoded = codecs::load(source, format, DECODE_PIXELS, check)?;
    decoded.warn(notices);
    if decoded.timeline.is_some() {
        notices
            .push("이 출력 경로는 APNG의 프레임 재생 시간과 반복을 보존하지 않습니다.".to_owned());
    }
    crate::pdf::images(&decoded.frames, output, check)
}

fn durations_for_png(durations: &[f64]) -> io::Result<Timeline> {
    let mut delays = Vec::new();
    for delay in durations {
        let value = delay.round_ties_even();
        if !(0.0..=65535.0).contains(&value) {
            return Err(invalid("APNG 프레임의 재생 시간이 너무 깁니다."));
        }
        delays.push((value as u16, 1000));
    }
    Ok(Timeline {
        poster: false,
        delays,
    })
}

fn save(
    decoded: &Decoded,
    frames: &[RgbaImage],
    target: &Path,
    kind: &str,
    quality: u8,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    if frames.len() > 1 && !matches!(kind, "png" | "tiff" | "webp" | "heic" | "avif") {
        return Err(invalid(
            "다중 프레임은 PNG/WebP/TIFF/HEIC/AVIF/PDF로 변환하세요.",
        ));
    }
    let file = File::create(target)?;
    match kind {
        "png" => {
            let derived;
            let timeline = if let Some(timeline) = &decoded.timeline {
                Some(timeline)
            } else if frames.len() > 1 {
                derived = durations_for_png(&decoded.durations)?;
                Some(&derived)
            } else {
                None
            };
            apng::encode(file, frames, timeline, decoded.loop_count, check)
        }
        "tiff" => codecs::write_tiff(file, frames, check),
        "webp" => codecs::write_webp(file, frames, decoded, quality, check),
        "avif" if cfg!(windows) => avif::write(
            file,
            frames,
            &decoded.durations,
            decoded.loop_count,
            quality,
            check,
        ),
        "heic" | "avif" => heif::write(
            file,
            frames,
            &decoded.durations,
            decoded.loop_count,
            kind,
            quality,
            check,
        ),
        "jpg" => codecs::write_jpg(file, &white(&frames[0]), quality),
        "bmp" => codecs::write_bmp(file, &white(&frames[0])),
        _ => Err(invalid("이미지 출력 형식을 지원하지 않습니다.")),
    }
}

pub fn convert(
    path: &Path,
    target: &Path,
    edge: &Edge,
    options: &Options,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    if edge.target == "pdf" {
        return to_pdf(path, target, &edge.source, check, notices);
    }
    let compress = edge.action_key == "compress";
    let strip = edge.action_key == "strip";
    if edge.target == "png"
        && !compress
        && !strip
        && !matches!(edge.source.as_str(), "svg" | "heic" | "avif")
        && codecs::native_png(path, target, options.image_max_edge, check, notices)?
    {
        return Ok(());
    }
    let mut decoded = codecs::load(path, &edge.source, DECODE_PIXELS, check)?;
    if edge.target == "png"
        && !compress
        && !strip
        && matches!(edge.source.as_str(), "heic" | "avif")
        && decoded.frames.len() == 1
    {
        if decoded.high_depth {
            return Err(invalid(
                "이 PNG 변환 경로는 원본의 높은 비트 깊이를 보존할 수 없습니다.",
            ));
        }
        let frame = image::DynamicImage::ImageRgba8(decoded.frames.pop().unwrap());
        let metadata = decoded.still_metadata;
        let image = if metadata.opaque {
            image::DynamicImage::ImageRgb8(frame.into_rgb8())
        } else {
            frame
        };
        return codecs::write_native_png(
            image,
            metadata,
            target,
            options.image_max_edge,
            check,
            notices,
        );
    }
    if !strip {
        // The Python HEIF reader exposes an EXIF slot even without an EXIF block.
        decoded.metadata |= edge.source == "heic";
        decoded.warn(notices);
    }
    if let Some(loops) = decoded.gif_loops {
        decoded.loop_count = loops;
    }
    if let Some(timeline) = &mut decoded.timeline {
        if edge.target == "webp" {
            if timeline.poster {
                decoded.frames.remove(0);
                decoded.durations.remove(0);
                timeline.poster = false;
                notices.push(
                    "WebP에는 APNG의 별도 기본 이미지를 저장할 수 없어 재생 프레임만 변환합니다."
                        .to_owned(),
                );
            }
            if decoded.frames.len() == 1 {
                notices.push(
                    "WebP의 단일 프레임은 정지 이미지로 저장하며 재생 시간과 반복을 제외합니다."
                        .to_owned(),
                );
            } else {
                if decoded.loop_count > 65535 {
                    return Err(invalid(
                        "WebP의 반복 횟수 한도를 초과했습니다. PNG로 변환하세요.",
                    ));
                }
                let (durations, rounded) = codecs::webp_delays(&timeline.delays)?;
                decoded.durations = durations;
                if rounded {
                    notices.push(
                        "WebP의 1ms 단위에 맞춰 APNG 프레임 경계 시간을 반올림합니다.".to_owned(),
                    );
                }
            }
        } else if edge.target != "png" {
            notices.push(
                "이 출력 경로는 APNG의 프레임 재생 시간과 반복을 보존하지 않습니다.".to_owned(),
            );
        }
    }
    for frame in &mut decoded.frames {
        check()?;
        let (width, height) = thumbnail(frame.dimensions(), options.image_max_edge);
        if frame.dimensions() != (width, height) {
            *frame = resize(frame, width, height);
        }
    }
    if !compress {
        return save(
            &decoded,
            &decoded.frames,
            target,
            &edge.target,
            options.image_quality,
            check,
        );
    }
    let goal = path.metadata()?.len() / 2;
    let original = decoded.frames[0].dimensions();
    let (mut width, mut height) = original;
    let qualities = if matches!(edge.target.as_str(), "jpg" | "webp" | "heic" | "avif") {
        &[88, 72, 55, 38, 22][..]
    } else {
        &[90]
    };
    loop {
        check()?;
        let scaled = decoded
            .frames
            .iter()
            .map(|frame| {
                check()?;
                let ratio = |from, to, base| {
                    (f64::from(from) * f64::from(to) / f64::from(base))
                        .round_ties_even()
                        .max(1.0) as u32
                };
                Ok(resize(
                    frame,
                    ratio(frame.width(), width, original.0),
                    ratio(frame.height(), height, original.1),
                ))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let mut size = 0;
        for &quality in qualities {
            save(&decoded, &scaled, target, &edge.target, quality, check)?;
            size = target.metadata()?.len();
            if size <= goal {
                return Ok(());
            }
        }
        if (width, height) == (1, 1) {
            return Err(invalid(
                "포맷의 최소 파일 크기 때문에 50% 이하로 줄일 수 없습니다.",
            ));
        }
        let ratio = 0.85_f64.min((goal as f64 / size.max(1) as f64).sqrt() * 0.9);
        width = ((f64::from(width) * ratio) as u32).max(1);
        height = ((f64::from(height) * ratio) as u32).max(1);
    }
}

pub fn transform(
    source: &Path,
    target: &Path,
    edge: &Edge,
    options: &ToolOptions,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    let mut decoded = codecs::load(source, &edge.source, TOOL_PIXELS, check)?;
    for frame in &mut decoded.frames {
        check()?;
        *frame = match edge.action_key.as_str() {
            "tool:image_rotate" => match options.rotation {
                90 => imageops::rotate90(frame),
                180 => imageops::rotate180(frame),
                270 => imageops::rotate270(frame),
                _ => continue,
            },
            "tool:image_flip" if options.flip == "horizontal" => imageops::flip_horizontal(frame),
            "tool:image_flip" => imageops::flip_vertical(frame),
            "tool:image_crop" => {
                let (x, y) = (options.crop_x, options.crop_y);
                let w = if options.crop_width == 0 {
                    frame.width().saturating_sub(x)
                } else {
                    options.crop_width
                };
                let h = if options.crop_height == 0 {
                    frame.height().saturating_sub(y)
                } else {
                    options.crop_height
                };
                if w == 0
                    || h == 0
                    || x.saturating_add(w) > frame.width()
                    || y.saturating_add(h) > frame.height()
                {
                    return Err(invalid("자르기 영역이 이미지 크기를 벗어났습니다."));
                }
                imageops::crop_imm(frame, x, y, w, h).to_image()
            }
            _ => RgbaImage::from_fn(frame.width(), frame.height(), |x, y| {
                let [r, g, b, a] = frame.get_pixel(x, y).0;
                let value =
                    ((19595 * u32::from(r) + 38470 * u32::from(g) + 7471 * u32::from(b) + 32768)
                        >> 16) as u8;
                Rgba([value, value, value, a])
            }),
        };
    }
    save(&decoded, &decoded.frames, target, "png", 90, check)
}

pub fn extract(
    source: &Path,
    folder: &Path,
    kind: &str,
    options: &ToolOptions,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    let decoded = codecs::load(source, kind, DECODE_PIXELS, check)?;
    let count = decoded.frames.len();
    if count.div_ceil(options.frame_interval) > options.max_frames {
        return Err(invalid(
            "최대 프레임 수를 초과했습니다. 프레임 간격을 늘리세요.",
        ));
    }
    let poster = decoded
        .timeline
        .as_ref()
        .is_some_and(|timeline| timeline.poster);
    let mut frames = Vec::new();
    let mut elapsed = 0.0;
    for (index, (frame, duration)) in decoded.frames.iter().zip(decoded.durations).enumerate() {
        check()?;
        if index % options.frame_interval == 0 {
            let name = format!("frame_{:06}.png", index + 1);
            apng::encode(
                File::create(folder.join(&name))?,
                std::slice::from_ref(frame),
                None,
                0,
                check,
            )?;
            frames.push(json!({"file": name, "source_frame": index + 1, "start_ms": elapsed, "duration_ms": duration}));
        }
        elapsed += duration;
    }
    let mut manifest = json!({"source_frames": count, "loop": decoded.loop_count});
    if kind != "svg" {
        manifest["default_image"] = json!(poster);
    }
    manifest["frames"] = json!(frames);
    let bytes = serde_json::to_vec_pretty(&manifest).map_err(io::Error::other)?;
    fs::write(folder.join("frames.json"), bytes)
}
