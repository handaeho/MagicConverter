use super::{Decoded, apng, invalid, pixels, resize, thumbnail};
use crate::svg;
use image::{
    AnimationDecoder, ColorType, DynamicImage, GenericImageView, GrayImage, ImageDecoder,
    ImageReader, RgbImage, Rgba, RgbaImage,
    codecs::{bmp::BmpEncoder, gif::GifDecoder, webp::WebPDecoder},
    metadata::{LoopCount, Orientation},
};
use num_bigint::BigUint;
use std::borrow::Cow;
use std::fs::File;
use std::io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write};
use std::path::Path;
use tiff::{
    decoder::DecodingResult,
    tags::{ExtraSamples, Tag},
};

const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";

fn png(source: &Path) -> io::Result<bool> {
    let mut prefix = [0_u8; 8];
    Ok(File::open(source)?.read(&mut prefix)? == prefix.len() && prefix == PNG_SIGNATURE)
}

fn apply_orientation(mut image: DynamicImage, orientation: Orientation) -> DynamicImage {
    image.apply_orientation(orientation);
    image
}

struct Still {
    image: DynamicImage,
    icc: Option<Vec<u8>>,
    exif: Option<Vec<u8>>,
    dpi: Option<png::PixelDimensions>,
    xmp: bool,
}

pub(super) fn write_native_png(
    image: DynamicImage,
    metadata: super::StillMetadata,
    target: &Path,
    max_edge: u32,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    write_still_png(
        Still {
            image,
            icc: metadata.icc,
            exif: metadata.exif,
            dpi: None,
            xmp: metadata.xmp,
        },
        target,
        max_edge,
        check,
        notices,
    )
}

fn jfif(source: &Path) -> io::Result<Option<png::PixelDimensions>> {
    let mut reader = BufReader::new(File::open(source)?);
    let mut bytes = [0_u8; 16];
    let count = reader.read(&mut bytes)?;
    if count != bytes.len() || !bytes.starts_with(b"\xff\xd8\xff\xe0") || &bytes[6..11] != b"JFIF\0"
    {
        return Ok(None);
    }
    let mut density_y = [0_u8; 2];
    reader.read_exact(&mut density_y)?;
    let scale = match bytes[13] {
        1 => 1.0 / 0.0254,
        2 => 100.0,
        _ => return Ok(None),
    };
    let convert = |value| (f64::from(u16::from_be_bytes(value)) * scale).round() as u32;
    Ok(Some(png::PixelDimensions {
        xppu: convert([bytes[14], bytes[15]]),
        yppu: convert(density_y),
        unit: png::Unit::Meter,
    }))
}

fn bmp_density(source: &Path) -> io::Result<Option<png::PixelDimensions>> {
    let mut data = [0_u8; 46];
    if File::open(source)?.read(&mut data)? != data.len()
        || u32::from_le_bytes(data[14..18].try_into().unwrap()) < 40
    {
        return Ok(None);
    }
    Ok(Some(png::PixelDimensions {
        xppu: u32::from_le_bytes(data[38..42].try_into().unwrap()),
        yppu: u32::from_le_bytes(data[42..46].try_into().unwrap()),
        unit: png::Unit::Meter,
    }))
}

fn tiff_density(
    reader: &mut tiff::decoder::Decoder<BufReader<File>>,
) -> Option<png::PixelDimensions> {
    use tiff::decoder::ifd::Value;
    let scale = match reader.get_tag_unsigned::<u16>(Tag::ResolutionUnit).ok() {
        None | Some(2) => 1.0 / 0.0254,
        Some(3) => 100.0,
        _ => return None,
    };
    let mut read = |key| match reader.get_tag(key) {
        Ok(Value::Rational(n, d)) if d > 0 => f64::from(n) / f64::from(d),
        _ => 1.0,
    };
    let (x, y) = (read(Tag::XResolution), read(Tag::YResolution));
    (x > 0.0 && y > 0.0).then(|| png::PixelDimensions {
        xppu: (x * scale).round() as u32,
        yppu: (y * scale).round() as u32,
        unit: png::Unit::Meter,
    })
}

fn still(source: &Path, limit: u64) -> io::Result<Still> {
    let decoder = ImageReader::open(source)?.with_guessed_format()?;
    let format = decoder.format();
    let mut decoder = decoder.into_decoder().map_err(io::Error::other)?;
    let size = decoder.dimensions();
    pixels(size.0, size.1, 1, limit)?;
    let mut exif = decoder.exif_metadata().map_err(io::Error::other)?;
    let icc = decoder.icc_profile().map_err(io::Error::other)?;
    let mut xmp = decoder.xmp_metadata().map_err(io::Error::other)?.is_some();
    let orientation = decoder.orientation().map_err(io::Error::other)?;
    let mut dpi = None;
    if format == Some(image::ImageFormat::Png) {
        let png = apng::reader(source, png::Transformations::IDENTITY)?;
        dpi = png
            .info()
            .pixel_dims
            .filter(|item| item.unit == png::Unit::Meter);
        xmp |= png
            .info()
            .utf8_text
            .iter()
            .any(|chunk| chunk.keyword == "XML:com.adobe.xmp");
    } else if format == Some(image::ImageFormat::Jpeg) {
        dpi = jfif(source)?;
    } else if format == Some(image::ImageFormat::Bmp) {
        dpi = bmp_density(source)?;
    } else if format == Some(image::ImageFormat::Tiff) {
        dpi = tiff_density(&mut tiff_decoder(source)?);
    }
    let image = if format == Some(image::ImageFormat::Jpeg)
        && matches!(decoder.color_type(), ColorType::L8 | ColorType::Rgb8)
    {
        read_jpg(source, decoder.color_type() == ColorType::L8, size)?
    } else {
        DynamicImage::from_decoder(decoder).map_err(io::Error::other)?
    };
    if orientation != Orientation::NoTransforms {
        if let Some(bytes) = &mut exif {
            let _ = Orientation::remove_from_exif_chunk(bytes);
        }
        if matches!(
            orientation,
            Orientation::Rotate90
                | Orientation::Rotate270
                | Orientation::Rotate90FlipH
                | Orientation::Rotate270FlipH
        ) && let Some(dpi) = &mut dpi
        {
            std::mem::swap(&mut dpi.xppu, &mut dpi.yppu);
        }
    }
    Ok(Still {
        image: apply_orientation(image, orientation),
        exif,
        icc,
        dpi,
        xmp,
    })
}

fn jpeg<T>(run: impl FnOnce() -> io::Result<T>) -> io::Result<T> {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(run))
        .map_err(|_| invalid("JPEG 이미지를 처리하지 못했습니다."))?
}

fn read_jpg(source: &Path, gray: bool, size: (u32, u32)) -> io::Result<DynamicImage> {
    jpeg(|| {
        let decoder = mozjpeg::Decompress::new_path(source)?;
        let mut decoder = if gray {
            decoder.grayscale()?
        } else {
            decoder.rgb()?
        };
        let bytes = decoder.read_scanlines::<u8>()?;
        decoder.finish()?;
        if gray {
            GrayImage::from_raw(size.0, size.1, bytes).map(DynamicImage::ImageLuma8)
        } else {
            RgbImage::from_raw(size.0, size.1, bytes).map(DynamicImage::ImageRgb8)
        }
        .ok_or_else(|| invalid("JPEG 크기가 올바르지 않습니다."))
    })
}

pub fn write_jpg(target: File, image: &RgbImage, quality: u8) -> io::Result<()> {
    encode_jpg(BufWriter::new(target), image, quality, true)
}

pub fn encode_jpg(
    target: impl Write,
    image: &RgbImage,
    quality: u8,
    progressive: bool,
) -> io::Result<()> {
    encode_jpg_density(target, image, quality, progressive, None)
}

pub fn encode_jpg_density(
    target: impl Write,
    image: &RgbImage,
    quality: u8,
    progressive: bool,
    dpi: Option<u16>,
) -> io::Result<()> {
    jpeg(|| {
        let mut encoder = mozjpeg::Compress::new(mozjpeg::ColorSpace::JCS_RGB);
        encoder.set_fastest_defaults();
        encoder.set_size(image.width() as usize, image.height() as usize);
        encoder.set_quality(f32::from(quality));
        encoder.set_optimize_coding(progressive);
        if let Some(dpi) = dpi {
            encoder.set_pixel_density(mozjpeg::PixelDensity {
                unit: mozjpeg::PixelDensityUnit::Inches,
                x: dpi,
                y: dpi,
            });
        }
        if progressive {
            encoder.set_progressive_mode();
        }
        let mut writer = encoder.start_compress(target)?;
        writer.write_scanlines(image.as_raw())?;
        writer.finish()?.flush()
    })
}

pub fn write_bmp(target: File, image: &RgbImage) -> io::Result<()> {
    let mut target = BufWriter::new(target);
    BmpEncoder::new(&mut target)
        .encode(
            image.as_raw(),
            image.width(),
            image.height(),
            image::ExtendedColorType::Rgb8,
        )
        .map_err(io::Error::other)?;
    target.seek(SeekFrom::Start(38))?;
    target.write_all(&3780_i32.to_le_bytes())?;
    target.write_all(&3780_i32.to_le_bytes())?;
    target.flush()
}

fn rgba(image: DynamicImage) -> RgbaImage {
    if let DynamicImage::ImageLuma16(gray) = image {
        RgbaImage::from_fn(gray.width(), gray.height(), |x, y| {
            let value = gray.get_pixel(x, y).0[0].min(255) as u8;
            Rgba([value, value, value, 255])
        })
    } else {
        image.into_rgba8()
    }
}

fn tiff_decoder(source: &Path) -> io::Result<tiff::decoder::Decoder<BufReader<File>>> {
    tiff::decoder::Decoder::new(BufReader::new(File::open(source)?)).map_err(io::Error::other)
}

fn read_tiff(source: &Path, limit: u64, check: &dyn Fn() -> io::Result<()>) -> io::Result<Decoded> {
    let mut reader = tiff_decoder(source)?;
    let mut result = Decoded::still(false, false);
    let mut total = 0_u64;
    loop {
        check()?;
        let (width, height) = reader.dimensions().map_err(io::Error::other)?;
        total += pixels(width, height, 1, limit)?;
        if total > limit {
            pixels(width, height, u64::MAX, limit)?;
        }
        let color = reader.colortype().map_err(io::Error::other)?;
        result.high_depth |= color.bit_depth() > 8;
        result.metadata |= tiff_density(&mut reader).is_some()
            || reader.get_tag(Tag::IccProfile).is_ok()
            || reader.get_tag(Tag::ExifDirectory).is_ok();
        let orientation = reader
            .get_tag_unsigned::<u8>(Tag::Orientation)
            .ok()
            .and_then(Orientation::from_exif)
            .unwrap_or(Orientation::NoTransforms);
        let buffer = reader.read_image().map_err(io::Error::other)?;
        let invalid_size = || invalid("TIFF 프레임의 픽셀 형식이 올바르지 않습니다.");
        use tiff::ColorType as Tiff;
        let decoded = match (color, buffer) {
            (Tiff::Gray(8), DecodingResult::U8(bytes)) => {
                GrayImage::from_raw(width, height, bytes).map(DynamicImage::ImageLuma8)
            }
            (Tiff::GrayA(8), DecodingResult::U8(bytes)) => {
                image::ImageBuffer::from_raw(width, height, bytes).map(DynamicImage::ImageLumaA8)
            }
            (Tiff::RGB(8) | Tiff::YCbCr(8), DecodingResult::U8(bytes)) => {
                RgbImage::from_raw(width, height, bytes).map(DynamicImage::ImageRgb8)
            }
            (Tiff::RGBA(8), DecodingResult::U8(bytes)) => {
                RgbaImage::from_raw(width, height, bytes).map(DynamicImage::ImageRgba8)
            }
            (Tiff::Gray(16), DecodingResult::U16(samples)) => {
                image::ImageBuffer::from_raw(width, height, samples).map(DynamicImage::ImageLuma16)
            }
            (Tiff::RGB(16), DecodingResult::U16(samples)) => {
                image::ImageBuffer::from_raw(width, height, samples).map(DynamicImage::ImageRgb16)
            }
            (Tiff::RGBA(16), DecodingResult::U16(samples)) => {
                image::ImageBuffer::from_raw(width, height, samples).map(DynamicImage::ImageRgba16)
            }
            _ => return Err(invalid("지원하지 않는 TIFF 픽셀 형식입니다.")),
        }
        .ok_or_else(invalid_size)?;
        result
            .frames
            .push(rgba(apply_orientation(decoded, orientation)));
        result.durations.push(100.0);
        if !reader.more_images() {
            break;
        }
        reader.next_image().map_err(io::Error::other)?;
    }
    Ok(result)
}

pub fn write_tiff(
    target: File,
    frames: &[RgbaImage],
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    use tiff::encoder::{Compression, DeflateLevel, TiffEncoder, colortype::RGB8};
    let mut encoder = TiffEncoder::new(BufWriter::new(target))
        .map_err(io::Error::other)?
        .with_compression(Compression::Deflate(DeflateLevel::Balanced));
    for frame in frames {
        check()?;
        let mut image = encoder
            .new_image::<RGB8>(frame.width(), frame.height())
            .map_err(io::Error::other)?;
        image
            .extra_samples(&[ExtraSamples::UnassociatedAlpha])
            .map_err(io::Error::other)?;
        image.resolution_unit(tiff::tags::ResolutionUnit::Inch);
        image.write_data(frame.as_raw()).map_err(io::Error::other)?;
    }
    Ok(())
}

fn animated<'a>(
    decoder: impl AnimationDecoder<'a>,
    limit: u64,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<Decoded> {
    let mut result = Decoded::still(false, false);
    let loops = decoder.loop_count();
    result.loop_count = match loops {
        LoopCount::Infinite => 0,
        LoopCount::Finite(count) => count.get(),
    };
    let mut total = 0_u64;
    for frame in decoder.into_frames() {
        check()?;
        let frame = frame.map_err(io::Error::other)?;
        let (numerator, denominator) = frame.delay().numer_denom_ms();
        result
            .durations
            .push(f64::from(numerator) / f64::from(denominator));
        let frame = frame.into_buffer();
        total += pixels(frame.width(), frame.height(), 1, limit)?;
        if total > limit {
            pixels(frame.width(), frame.height(), u64::MAX, limit)?;
        }
        result.frames.push(frame);
    }
    if result.frames.is_empty() {
        return Err(invalid("이미지 프레임이 없습니다."));
    }
    Ok(result)
}

pub fn load(
    source: &Path,
    kind: &str,
    limit: u64,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<Decoded> {
    check()?;
    if kind == "avif" && cfg!(windows) {
        return super::avif::load(source, limit, check);
    }
    if matches!(kind, "heic" | "avif") {
        return super::heif::load(source, limit, check);
    }
    if kind == "svg" {
        let mut result = Decoded::still(false, false);
        result.frames.push(svg::raster(source, check)?);
        result.durations.push(100.0);
        return Ok(result);
    }
    if png(source)? {
        let reader = apng::reader(source, png::Transformations::normalize_to_color8())?;
        if reader.info().animation_control.is_some() {
            return apng::load(reader, check, limit);
        }
    }
    let format = ImageReader::open(source)?.with_guessed_format()?.format();
    match format {
        Some(image::ImageFormat::Tiff) => return read_tiff(source, limit, check),
        Some(image::ImageFormat::Gif) => {
            let decoder =
                GifDecoder::new(BufReader::new(File::open(source)?)).map_err(io::Error::other)?;
            let (width, height) = decoder.dimensions();
            pixels(width, height, 1, limit)?;
            let mut result = animated(decoder, limit, check)?;
            let header = gif::DecodeOptions::new()
                .read_info(BufReader::new(File::open(source)?))
                .map_err(io::Error::other)?;
            result.gif_loops = Some(match header.repeat() {
                gif::Repeat::Finite(n) => u32::from(n) + 1,
                gif::Repeat::Infinite => 0,
            });
            return Ok(result);
        }
        Some(image::ImageFormat::WebP) => {
            let mut decoder =
                WebPDecoder::new(BufReader::new(File::open(source)?)).map_err(io::Error::other)?;
            if decoder.has_animation() {
                let (width, height) = decoder.dimensions();
                pixels(width, height, 1, limit)?;
                let metadata = decoder.icc_profile().map_err(io::Error::other)?.is_some()
                    || decoder.exif_metadata().map_err(io::Error::other)?.is_some()
                    || decoder.xmp_metadata().map_err(io::Error::other)?.is_some();
                let mut result = animated(decoder, limit, check)?;
                result.metadata = metadata;
                return Ok(result);
            }
        }
        _ => (),
    }
    let still = still(source, limit)?;
    let high_depth =
        still.image.color().bytes_per_pixel() / still.image.color().channel_count() > 1;
    let mut result = Decoded::still(
        still.icc.is_some() || still.exif.is_some() || still.dpi.is_some() || still.xmp,
        high_depth,
    );
    if format == Some(image::ImageFormat::WebP) {
        result.loop_count = 1;
    }
    result.frames.push(rgba(still.image));
    result.durations.push(100.0);
    Ok(result)
}

pub fn native_png(
    source: &Path,
    target: &Path,
    max_edge: u32,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<bool> {
    let is_png = png(source)?;
    if is_png {
        let mut reader = apng::reader(source, png::Transformations::IDENTITY)?;
        let size = reader.info().size();
        if max_edge == 0 || size.0.max(size.1) <= max_edge {
            reader.finish().map_err(io::Error::other)?;
            let input = File::open(source)?;
            let size = input.metadata()?.len();
            magic_copy_worker::copy_stream(input, File::create(target)?, size, check, |_| Ok(()))?;
            return Ok(true);
        }
        if reader.info().animation_control.is_some() {
            return Ok(false);
        }
    } else {
        let format = ImageReader::open(source)?.with_guessed_format()?.format();
        if matches!(format, Some(image::ImageFormat::Gif)) {
            return Ok(false);
        }
        if format == Some(image::ImageFormat::Tiff) && tiff_decoder(source)?.more_images() {
            return Ok(false);
        }
        if format == Some(image::ImageFormat::WebP)
            && WebPDecoder::new(BufReader::new(File::open(source)?))
                .map_err(io::Error::other)?
                .has_animation()
        {
            return Ok(false);
        }
    }
    let still = still(source, super::DECODE_PIXELS)?;
    write_still_png(still, target, max_edge, check, notices)?;
    Ok(true)
}

fn write_still_png(
    still: Still,
    target: &Path,
    max_edge: u32,
    check: &dyn Fn() -> io::Result<()>,
    notices: &mut Vec<String>,
) -> io::Result<()> {
    let color = still.image.color();
    if matches!(
        color,
        ColorType::Rgb16
            | ColorType::Rgba16
            | ColorType::La16
            | ColorType::Rgb32F
            | ColorType::Rgba32F
    ) {
        return Err(invalid(
            "이 PNG 변환 경로는 원본의 높은 비트 깊이를 보존할 수 없습니다.",
        ));
    }
    if still.xmp {
        notices.push("이 PNG 인코딩 경로는 원본 XMP 메타데이터를 보존하지 않습니다.".to_owned());
    }
    let (width, height) = thumbnail(still.image.dimensions(), max_edge);
    let image = if (width, height) != still.image.dimensions() {
        if color == ColorType::Rgba8 {
            DynamicImage::ImageRgba8(resize(&still.image.into_rgba8(), width, height))
        } else {
            still
                .image
                .resize_exact(width, height, image::imageops::FilterType::Lanczos3)
        }
    } else {
        still.image
    };
    let mut info = png::Info::with_size(width, height);
    info.icc_profile = still.icc.map(Cow::Owned);
    info.exif_metadata = still.exif.map(Cow::Owned);
    info.pixel_dims = still.dpi;
    info.color_type = match color {
        ColorType::L8 | ColorType::L16 => png::ColorType::Grayscale,
        ColorType::La8 => png::ColorType::GrayscaleAlpha,
        ColorType::Rgb8 => png::ColorType::Rgb,
        _ => png::ColorType::Rgba,
    };
    let bytes = if let DynamicImage::ImageLuma16(gray) = image {
        info.bit_depth = png::BitDepth::Sixteen;
        gray.into_raw()
            .into_iter()
            .flat_map(u16::to_be_bytes)
            .collect()
    } else {
        image.into_bytes()
    };
    let mut encoder = png::Encoder::with_info(BufWriter::new(File::create(target)?), info)
        .map_err(io::Error::other)?;
    encoder.set_compression(png::Compression::High);
    check()?;
    let mut writer = encoder.write_header().map_err(io::Error::other)?;
    writer.write_image_data(&bytes).map_err(io::Error::other)?;
    writer.finish().map_err(io::Error::other)?;
    Ok(())
}

pub fn webp_delays(delays: &[(u16, u16)]) -> io::Result<(Vec<f64>, bool)> {
    let mut numerator = BigUint::from(0_u32);
    let mut denominator = BigUint::from(1_u32);
    let mut previous = 0_u32;
    let mut rounded = false;
    let mut result = Vec::new();
    for &(ticks, unit) in delays {
        let unit = u32::from(if unit == 0 { 100 } else { unit });
        let ticks = u32::from(ticks) * 1000;
        rounded |= ticks % unit != 0;
        let mut left = unit;
        let mut right = u32::try_from(&denominator % unit).expect("remainder below u16");
        while right != 0 {
            (left, right) = (right, left % right);
        }
        numerator *= unit / left;
        numerator += (&denominator / left) * ticks;
        denominator *= unit / left;
        let mut boundary = u32::try_from(&numerator / &denominator).map_err(|_| {
            invalid("WebP가 저장할 수 있는 프레임 재생 시간을 초과했습니다. PNG로 변환하세요.")
        })?;
        let doubled = (&numerator % &denominator) * 2_u32;
        if doubled > denominator || (doubled == denominator && boundary % 2 != 0) {
            boundary += 1;
        }
        if boundary > i32::MAX as u32 || boundary - previous > 0xff_ffff {
            return Err(invalid(
                "WebP가 저장할 수 있는 프레임 재생 시간을 초과했습니다. PNG로 변환하세요.",
            ));
        }
        result.push(f64::from(boundary - previous));
        previous = boundary;
    }
    Ok((result, rounded))
}

fn encoded_webp(frame: &RgbaImage, quality: u8, lossless: bool) -> io::Result<webp::WebPMemory> {
    let encoder = webp::Encoder::from_rgba(frame.as_raw(), frame.width(), frame.height());
    let mut options =
        webp::WebPConfig::new().map_err(|()| invalid("WebP 인코더를 열지 못했습니다."))?;
    options.quality = f32::from(quality);
    options.method = 6;
    options.lossless = i32::from(lossless);
    encoder
        .encode_advanced(&options)
        .map_err(|error| invalid(&format!("WebP 인코딩 실패: {error:?}")))
}

fn riff_chunk(writer: &mut impl Write, name: &[u8; 4], bytes: &[u8]) -> io::Result<()> {
    writer.write_all(name)?;
    writer.write_all(
        &u32::try_from(bytes.len())
            .map_err(io::Error::other)?
            .to_le_bytes(),
    )?;
    writer.write_all(bytes)?;
    if !bytes.len().is_multiple_of(2) {
        writer.write_all(&[0])?;
    }
    Ok(())
}

pub fn write_webp(
    file: File,
    frames: &[RgbaImage],
    decoded: &Decoded,
    quality: u8,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    let mut file = BufWriter::new(file);
    let first = &frames[0];
    let lossless = decoded.timeline.is_some() || decoded.gif_loops.is_some();
    if frames.len() == 1 {
        file.write_all(&encoded_webp(first, quality, lossless)?)?;
        return file.flush();
    }
    if decoded.loop_count > u16::MAX.into() {
        return Err(invalid(
            "WebP의 반복 횟수 한도를 초과했습니다. PNG로 변환하세요.",
        ));
    }
    file.write_all(b"RIFF\0\0\0\0WEBP")?;
    let u24 = |number: u32| {
        let raw = number.to_le_bytes();
        [raw[0], raw[1], raw[2]]
    };
    let mut canvas = vec![0x12, 0, 0, 0];
    canvas.extend(u24(first.width() - 1));
    canvas.extend(u24(first.height() - 1));
    riff_chunk(&mut file, b"VP8X", &canvas)?;
    let mut animation = vec![0, 0, 0, 0];
    animation.extend((decoded.loop_count as u16).to_le_bytes());
    riff_chunk(&mut file, b"ANIM", &animation)?;
    for (frame, &duration) in frames.iter().zip(&decoded.durations) {
        check()?;
        if frame.dimensions() != first.dimensions() || !(0.0..=16777215.0).contains(&duration) {
            return Err(invalid(
                "WebP 프레임의 크기 또는 재생 시간이 올바르지 않습니다.",
            ));
        }
        let encoded = encoded_webp(frame, quality, lossless)?;
        let mut chunk = vec![0; 6];
        chunk.extend(u24(frame.width() - 1));
        chunk.extend(u24(frame.height() - 1));
        chunk.extend(u24(duration.round_ties_even() as u32));
        chunk.push(2); // This full-canvas frame replaces the preceding canvas.
        let mut payload = encoded
            .get(12..)
            .ok_or_else(|| invalid("WebP 인코딩 결과가 올바르지 않습니다."))?;
        while payload.len() >= 8 {
            let size = u32::from_le_bytes(payload[4..8].try_into().expect("RIFF size")) as usize;
            let padded = 8 + size + size % 2;
            let (part, remainder) = payload
                .split_at_checked(padded)
                .ok_or_else(|| invalid("WebP 인코딩 결과가 올바르지 않습니다."))?;
            if matches!(&part[..4], b"ALPH" | b"VP8L" | b"VP8 ") {
                chunk.extend(part);
            }
            payload = remainder;
        }
        riff_chunk(&mut file, b"ANMF", &chunk)?;
    }
    let size = u32::try_from(file.stream_position()? - 8).map_err(io::Error::other)?;
    file.seek(SeekFrom::Start(4))?;
    file.write_all(&size.to_le_bytes())?;
    file.flush()
}
