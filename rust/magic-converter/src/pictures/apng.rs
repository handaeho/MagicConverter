use super::{Decoded, Timeline, invalid, pixels};
use image::{Rgba, RgbaImage, metadata::Orientation};
use std::fs::File;
use std::io::{self, BufReader, BufWriter, Write};
use std::path::Path;

pub fn reader(
    source: &Path,
    transformations: png::Transformations,
) -> io::Result<png::Reader<BufReader<File>>> {
    let mut decoder = png::Decoder::new_with_limits(
        BufReader::new(File::open(source)?),
        png::Limits {
            bytes: 512 * 1024 * 1024,
        },
    );
    decoder.set_transformations(transformations);
    decoder.read_info().map_err(io::Error::other)
}

fn clear(image: &mut RgbaImage, control: png::FrameControl) {
    for y in control.y_offset..control.y_offset + control.height {
        for x in control.x_offset..control.x_offset + control.width {
            image.put_pixel(x, y, Rgba([0, 0, 0, 0]));
        }
    }
}

fn decoded_pixel(samples: &[u8], color: png::ColorType) -> Rgba<u8> {
    match color {
        png::ColorType::Grayscale => Rgba([samples[0], samples[0], samples[0], 255]),
        png::ColorType::GrayscaleAlpha => Rgba([samples[0], samples[0], samples[0], samples[1]]),
        png::ColorType::Rgb => Rgba([samples[0], samples[1], samples[2], 255]),
        png::ColorType::Rgba => Rgba([samples[0], samples[1], samples[2], samples[3]]),
        png::ColorType::Indexed => unreachable!("EXPAND resolves palettes"),
    }
}

pub fn load(
    mut reader: png::Reader<BufReader<File>>,
    check: &dyn Fn() -> io::Result<()>,
    output_limit: u64,
) -> io::Result<Decoded> {
    let info = reader.info();
    let animation = info.animation_control;
    let poster = animation.is_some() && info.frame_control.is_none();
    let count = animation.map_or(1, |ctl| u64::from(ctl.num_frames) + u64::from(poster));
    pixels(info.width, info.height, count, output_limit)?;
    let mut canvas = RgbaImage::new(info.width, info.height);
    let high_depth = info.bit_depth == png::BitDepth::Sixteen;
    let metadata = info.icc_profile.is_some()
        || info.exif_metadata.is_some()
        || info
            .pixel_dims
            .is_some_and(|dims| dims.unit == png::Unit::Meter)
        || info
            .utf8_text
            .iter()
            .any(|text| text.keyword == "XML:com.adobe.xmp");
    let orientation = info
        .exif_metadata
        .as_deref()
        .and_then(Orientation::from_exif_chunk)
        .unwrap_or(Orientation::NoTransforms);
    let mut decoded = Decoded::still(metadata, high_depth);
    if let Some(animation) = animation {
        decoded.loop_count = animation.num_plays;
        decoded.timeline = Some(Timeline {
            poster,
            delays: Vec::new(),
        });
    }
    let mut bytes = vec![
        0_u8;
        reader
            .output_buffer_size()
            .ok_or_else(|| invalid("PNG 프레임 크기가 너무 큽니다."))?
    ];
    for index in 0..count {
        check()?;
        let frame = reader.next_frame(&mut bytes).map_err(io::Error::other)?;
        let control = reader.info().frame_control;
        let (left, top) = control.map_or((0, 0), |control| (control.x_offset, control.y_offset));
        let previous = control
            .filter(|control| control.dispose_op == png::DisposeOp::Previous && index != 0)
            .map(|_| canvas.clone());
        let blend = index != 0 && control.is_some_and(|ctl| ctl.blend_op == png::BlendOp::Over);
        for row in 0..frame.height {
            check()?;
            let samples = &bytes[row as usize * frame.line_size..][..frame.line_size];
            for (column, samples) in samples.chunks_exact(frame.color_type.samples()).enumerate() {
                let new = decoded_pixel(samples, frame.color_type);
                let pixel = canvas.get_pixel_mut(left + column as u32, top + row);
                if blend {
                    // The Python decoder pastes the update using its own alpha mask.
                    let alpha = u32::from(new.0[3]);
                    for (current, next) in pixel.0.iter_mut().zip(new.0) {
                        *current =
                            ((u32::from(*current) * (255 - alpha) + u32::from(next) * alpha + 127)
                                / 255) as u8;
                    }
                } else {
                    *pixel = new;
                }
            }
        }
        let mut image = image::DynamicImage::ImageRgba8(canvas.clone());
        image.apply_orientation(orientation);
        decoded.frames.push(image.into_rgba8());
        if let Some(timeline) = &mut decoded.timeline {
            if poster && index == 0 {
                decoded.durations.push(0.0);
            } else {
                let ctl = control
                    .ok_or_else(|| invalid("APNG 재생 시간과 프레임 수가 일치하지 않습니다."))?;
                timeline.delays.push((ctl.delay_num, ctl.delay_den));
                let denominator = if ctl.delay_den == 0 {
                    100
                } else {
                    ctl.delay_den
                };
                decoded
                    .durations
                    .push(f64::from(ctl.delay_num) * 1000.0 / f64::from(denominator));
            }
        } else {
            decoded.durations.push(100.0);
        }
        if let Some(ctl) = control {
            match ctl.dispose_op {
                png::DisposeOp::None => (),
                png::DisposeOp::Background => clear(&mut canvas, ctl),
                png::DisposeOp::Previous => {
                    if let Some(previous) = previous {
                        canvas = previous;
                    } else {
                        clear(&mut canvas, ctl);
                    }
                }
            }
        }
    }
    reader.finish().map_err(io::Error::other)?;
    Ok(decoded)
}

pub fn encode<W: Write>(
    writer: W,
    frames: &[RgbaImage],
    timeline: Option<&Timeline>,
    loops: u32,
    check: &dyn Fn() -> io::Result<()>,
) -> io::Result<()> {
    let first = &frames[0];
    let mut encoder = png::Encoder::new(BufWriter::new(writer), first.width(), first.height());
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_compression(png::Compression::High);
    encoder.validate_sequence(true);
    if let Some(timeline) = timeline {
        encoder
            .set_animated(timeline.delays.len() as u32, loops)
            .map_err(io::Error::other)?;
        encoder
            .set_sep_def_img(timeline.poster)
            .map_err(io::Error::other)?;
    }
    let mut out = encoder.write_header().map_err(io::Error::other)?;
    for (index, frame) in frames.iter().enumerate() {
        check()?;
        if frame.dimensions() != first.dimensions() {
            return Err(invalid("출력 이미지의 프레임·크기가 올바르지 않습니다."));
        }
        if let Some(timeline) = timeline {
            let position = index.checked_sub(usize::from(timeline.poster));
            if let Some(position) = position {
                let (n, d) = timeline.delays[position];
                out.set_frame_delay(n, d).map_err(io::Error::other)?;
            }
        }
        out.write_image_data(frame.as_raw())
            .map_err(io::Error::other)?;
    }
    out.finish().map_err(io::Error::other)
}
