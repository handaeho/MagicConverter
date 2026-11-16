//! FFmpeg recipes, media inspection and validation at the native process boundary.

use crate::engine_process::{self, ResultData};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::env;
use std::ffi::{OsStr, OsString};
use std::fs::{self, File};
use std::io::{self, BufReader, ErrorKind};
use std::path::{Path, PathBuf};
use std::time::Duration;

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
const SCALE: &str = "scale=ceil(iw/2)*2:ceil(ih/2)*2";

#[derive(Clone, Default, Deserialize)]
#[serde(default)]
pub struct EncodeOptions {
    video_crf: Option<u32>,
    audio_bitrate: Option<u32>,
}

#[derive(Clone, Deserialize)]
#[serde(default)]
pub struct ToolOptions {
    start_ms: u64,
    duration_ms: u64,
    frame_interval: u32,
    max_frames: usize,
}

impl Default for ToolOptions {
    fn default() -> Self {
        Self {
            start_ms: 0,
            duration_ms: 0,
            frame_interval: 1,
            max_frames: 10_000,
        }
    }
}

impl ToolOptions {
    fn validate(&self) -> io::Result<()> {
        if self.start_ms > 604_800_000
            || self.duration_ms > 604_800_000
            || !(1..=10_000).contains(&self.frame_interval)
            || !(1..=100_000).contains(&self.max_frames)
        {
            return Err(invalid("미디어 도구 옵션이 허용 범위를 벗어났습니다."));
        }
        Ok(())
    }
}

pub struct Tools {
    encoder: PathBuf,
    probe: Option<PathBuf>,
}

fn invalid(message: impl Into<String>) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message.into())
}

fn find(executable: &OsStr) -> Option<PathBuf> {
    let path = Path::new(executable);
    if path.is_file() {
        return fs::canonicalize(path).ok();
    }
    if path.components().count() > 1 {
        return None;
    }
    env::split_paths(&env::var_os("PATH").unwrap_or_default()).find_map(|directory| {
        let candidate = directory.join(path);
        let candidate = if candidate.extension().is_none() {
            candidate.with_extension("exe")
        } else {
            candidate
        };
        candidate
            .is_file()
            .then(|| fs::canonicalize(candidate).ok())
            .flatten()
    })
}

fn bundled(name: &str) -> Option<PathBuf> {
    env::current_exe()
        .ok()?
        .parent()?
        .ancestors()
        .take(5)
        .map(|directory| directory.join("vendor").join("ffmpeg").join(name))
        .find(|path| path.is_file())
}

impl Tools {
    pub fn discover(preferred: &str) -> Option<Self> {
        let configured = if preferred.trim().is_empty() {
            env::var_os("MAGIC_CONVERTER_FFMPEG").unwrap_or_default()
        } else {
            preferred.trim().into()
        };
        let encoder = (!configured.is_empty())
            .then(|| find(&configured))
            .flatten()
            .or_else(|| bundled("ffmpeg.exe"))
            .or_else(|| find(OsStr::new("ffmpeg")))?;
        let sibling = encoder.with_file_name("ffprobe.exe");
        let probe = sibling
            .is_file()
            .then_some(sibling)
            .or_else(|| bundled("ffprobe.exe"))
            .or_else(|| find(OsStr::new("ffprobe")));
        Some(Self { encoder, probe })
    }

    fn inspect(&self, source: &Path, args: &[&str], check: Check<'_>) -> io::Result<ResultData> {
        let probe = self
            .probe
            .as_deref()
            .ok_or_else(|| invalid("미디어 도구에는 FFprobe가 필요합니다."))?;
        let mut command = command(&["-v", "error"]);
        extend(&mut command, args);
        extend(&mut command, &["-of", "json"]);
        command.push(source.into());
        engine_process::run(
            probe,
            &command,
            Some(Duration::from_secs(30)),
            None,
            check,
            &|| Ok(()),
        )
    }

    fn media_info(&self, source: &Path, check: Check<'_>) -> io::Result<Value> {
        let result = self.inspect(source, &["-show_entries",
            "format=duration:stream=duration,codec_type,codec_name,bits_per_sample,bits_per_raw_sample:stream_disposition=attached_pic"], check);
        let result = match result {
            Ok(result) if result.code == 0 => result,
            Err(error) if matches!(error.kind(), ErrorKind::Interrupted | ErrorKind::TimedOut) => {
                return Err(error);
            }
            _ => return Ok(Value::Null),
        };
        Ok(serde_json::from_str(&result.stdout).unwrap_or(Value::Null))
    }

    fn streams(&self, source: &Path, check: Check<'_>) -> io::Result<Vec<Value>> {
        Ok(array(&self.media_info(source, check)?, "streams").to_vec())
    }

    fn probe(&self, source: &Path, chapters: bool, check: Check<'_>) -> io::Result<Value> {
        let mut args = vec!["-show_streams", "-show_format"];
        if chapters {
            args.push("-show_chapters");
        }
        let result = self.inspect(source, &args, check)?;
        if result.code != 0 {
            let message = tail(&result.stderr, 1200);
            return Err(invalid(if message.is_empty() {
                "미디어 정보를 읽지 못했습니다."
            } else {
                message
            }));
        }
        let value: Value = serde_json::from_str(&result.stdout)?;
        if array(&value, "streams").is_empty() {
            return Err(invalid("미디어 스트림이 없습니다."));
        }
        Ok(value)
    }

    fn run(&self, args: &[OsString], check: Check<'_>) -> io::Result<()> {
        let result = engine_process::run(
            &self.encoder,
            args,
            None,
            Some(Duration::from_secs(600)),
            check,
            &|| Ok(()),
        )?;
        if result.code == 0 {
            Ok(())
        } else {
            let message = tail(&result.stderr, 2400).trim();
            Err(invalid(if message.is_empty() {
                format!("FFmpeg 종료 코드: {}", result.code)
            } else {
                message.to_owned()
            }))
        }
    }

    pub fn convert(
        &self,
        request: MediaFile<'_>,
        output: &Path,
        check: Check<'_>,
        notices: &mut Vec<String>,
    ) -> io::Result<()> {
        let MediaFile {
            path,
            key,
            input_format,
            input_is_audio,
            encoding,
            tools,
        } = request;
        if key.starts_with("tool:") {
            tools.validate()?;
            return self.convert_tool(&request, output, check, notices);
        }
        let info = self.media_info(path, check)?;
        let streams = array(&info, "streams");
        let audio = streams.iter().find(|value| value["codec_type"] == "audio");
        if matches!(key, "audio:wav" | "audio:flac")
            && matches!(input_format, "wav" | "flac")
            && audio.is_none()
        {
            return Err(invalid(
                "WAV·FLAC의 샘플 정밀도를 확인할 수 없습니다. FFprobe와 원본 오디오를 확인하세요.",
            ));
        }
        let mut args = recipe(path, output, key, input_format, input_is_audio, audio)?;
        if input_is_audio && video_target(key) {
            let duration = audio
                .and_then(|stream| number(&stream["duration"]))
                .filter(|duration| *duration > 0.0)
                .or_else(|| number(&info["format"]["duration"]).filter(|duration| *duration > 0.0));
            if let Some(duration) = duration {
                // End the synthetic video independently of muxer buffering.
                // Preserve all audio samples, including the last partial frame.
                args[START.len() + 3] =
                    format!("color=c=0x1e1e23:s=640x360:r=24:d={duration:.6}").into();
                args.retain(|arg| arg != "-shortest");
            } else {
                notices
                    .push("오디오 길이를 읽을 수 없어 정지 화면의 끝은 FFmpeg가 정합니다.".into());
            }
        }
        apply_encoding(&mut args, encoding)?;
        self.run(&args, check)?;
        if key == "audio:flac"
            && let Some(audio) = audio
            && audio["codec_name"] != "flac"
            && bit_depth(audio) > 16
        {
            let result = self.streams(output, check)?;
            if result
                .first()
                .is_none_or(|value| bit_depth(value) < bit_depth(audio))
            {
                return Err(invalid(
                    "FLAC 출력의 비트 깊이가 원본보다 낮거나 확인되지 않습니다.",
                ));
            }
        }
        describe_loss(key, streams, notices);
        check()
    }

    fn convert_tool(
        &self,
        request: &MediaFile<'_>,
        output: &Path,
        check: Check<'_>,
        notices: &mut Vec<String>,
    ) -> io::Result<()> {
        if request.key == "tool:media_remux_mkv" {
            let info = self.probe(request.path, true, check)?;
            let mut args = command(START);
            extend(&mut args, &["-copyts", "-i"]);
            args.push(request.path.into());
            extend(
                &mut args,
                &[
                    "-map",
                    "0",
                    "-map_metadata",
                    "0",
                    "-map_chapters",
                    "0",
                    "-c",
                    "copy",
                    "-default_mode",
                    "passthrough",
                    "-avoid_negative_ts",
                    "disabled",
                ],
            );
            args.push(output.into());
            self.run(&args, check)?;
            return check_remux(&info, &self.probe(output, true, check)?, notices);
        }
        let info = self.probe(request.path, false, check)?;
        check_range(&info, request.tools)?;
        let key = if request.key == "tool:media_mute" {
            "media:mp4".to_owned()
        } else if !request.input_is_audio || request.input_format == "mp3" {
            format!("media:{}", request.input_format)
        } else {
            format!("audio:{}", request.input_format)
        };
        let mut args = recipe(
            request.path,
            output,
            &key,
            request.input_format,
            request.input_is_audio,
            None,
        )?;
        args.pop();
        if request.key == "tool:media_trim" {
            extend(&mut args, &["-ss", &seconds(request.tools.start_ms)]);
            if request.tools.duration_ms > 0 {
                extend(&mut args, &["-t", &seconds(request.tools.duration_ms)]);
            }
        } else {
            args.push("-an".into());
        }
        args.push(output.into());
        self.run(&args, check)?;
        let result = self.probe(output, false, check)?;
        let streams = array(&result, "streams");
        if request.key == "tool:media_mute"
            && (!streams.iter().any(|stream| stream["codec_type"] == "video")
                || streams.iter().any(|stream| stream["codec_type"] == "audio"))
        {
            return Err(invalid("음소거 출력의 영상·음성 구조가 올바르지 않습니다."));
        }
        check()
    }

    pub fn frames(
        &self,
        source: &Path,
        folder: &Path,
        options: &ToolOptions,
        check: Check<'_>,
    ) -> io::Result<()> {
        options.validate()?;
        let info = self.probe(source, false, check)?;
        if !array(&info, "streams")
            .iter()
            .any(|stream| stream["codec_type"] == "video")
        {
            return Err(invalid("프레임을 추출할 영상 스트림이 없습니다."));
        }
        check_range(&info, options)?;
        let mut args = command(&[
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-progress",
            "pipe:1",
            "-nostats",
            "-ss",
            &seconds(options.start_ms),
            "-i",
        ]);
        args.push(source.into());
        if options.duration_ms > 0 {
            extend(&mut args, &["-t", &seconds(options.duration_ms)]);
        }
        extend(
            &mut args,
            &[
                "-map",
                "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf",
                &format!("select=not(mod(n\\,{}))", options.frame_interval),
                "-fps_mode",
                "vfr",
                "-frames:v",
                &(options.max_frames + 1).to_string(),
                "-threads",
                "2",
            ],
        );
        args.push(folder.join("frame_%06d.png").into_os_string());
        self.run(&args, check)?;
        let mut frames = Vec::new();
        for path in fs::read_dir(folder)? {
            check()?;
            let path = path?;
            let name = path.file_name().to_string_lossy().into_owned();
            if name.starts_with("frame_") && name.ends_with(".png") {
                frames.push(name);
            }
        }
        frames.sort_unstable();
        if frames.is_empty() {
            return Err(invalid("선택 구간에 추출할 프레임이 없습니다."));
        }
        if frames.len() > options.max_frames {
            return Err(invalid(
                "최대 프레임 수를 초과했습니다. 구간을 줄이거나 프레임 간격을 늘리세요.",
            ));
        }
        let mut dimensions = None;
        for frame in &frames {
            check()?;
            let file = BufReader::new(File::open(folder.join(frame))?);
            let mut reader = png::Decoder::new(file)
                .read_info()
                .map_err(io::Error::other)?;
            let mut pixels = vec![
                0;
                reader
                    .output_buffer_size()
                    .ok_or_else(|| invalid("PNG 프레임이 너무 큽니다."))?
            ];
            let info = reader.next_frame(&mut pixels).map_err(io::Error::other)?;
            let size = (info.width, info.height);
            if dimensions.is_some_and(|expected| expected != size) {
                return Err(invalid("추출된 프레임의 크기가 일정하지 않습니다."));
            }
            dimensions = Some(size);
        }
        let (width, height) = dimensions.expect("at least one frame");
        #[derive(Serialize)]
        struct Manifest<'a> {
            source: &'a str,
            start_ms: u64,
            duration_ms: u64,
            frame_interval: u32,
            width: u32,
            height: u32,
            frames: Vec<String>,
        }
        let name = source
            .file_name()
            .unwrap_or(source.as_os_str())
            .to_string_lossy();
        let data = Manifest {
            source: &name,
            start_ms: options.start_ms,
            duration_ms: options.duration_ms,
            frame_interval: options.frame_interval,
            width,
            height,
            frames,
        };
        let mut manifest = serde_json::to_string_pretty(&data)?;
        if cfg!(windows) {
            manifest = manifest.replace('\n', "\r\n");
        }
        fs::write(folder.join("frames.json"), manifest)?;
        check()
    }
}

pub struct MediaFile<'a> {
    pub path: &'a Path,
    pub key: &'a str,
    pub input_format: &'a str,
    pub input_is_audio: bool,
    pub encoding: &'a EncodeOptions,
    pub tools: &'a ToolOptions,
}

const START: &[&str] = &[
    "-hide_banner",
    "-loglevel",
    "error",
    "-nostdin",
    "-y",
    "-nostats",
    "-stats_period",
    "0.2",
    "-progress",
    "pipe:1",
];

fn command(args: &[&str]) -> Vec<OsString> {
    args.iter().map(OsString::from).collect()
}

fn extend(command: &mut Vec<OsString>, args: &[&str]) {
    command.extend(args.iter().map(OsString::from));
}

fn tail(text: &str, count: usize) -> &str {
    text.char_indices()
        .rev()
        .nth(count - 1)
        .map_or(text, |(index, _)| &text[index..])
}

fn array<'a>(value: &'a Value, key: &str) -> &'a [Value] {
    value[key].as_array().map_or(&[], Vec::as_slice)
}

fn integer(value: &Value) -> u32 {
    value
        .as_u64()
        .and_then(|number| u32::try_from(number).ok())
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
        .unwrap_or(0)
}

fn number(value: &Value) -> Option<f64> {
    value
        .as_f64()
        .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
        .filter(|value| value.is_finite())
}

fn bit_depth(stream: &Value) -> u32 {
    ["bits_per_raw_sample", "bits_per_sample"]
        .iter()
        .map(|field| integer(&stream[field]))
        .find(|&value| value > 0)
        .unwrap_or(0)
}

fn check_range(value: &Value, options: &ToolOptions) -> io::Result<()> {
    if number(&value["format"]["duration"])
        .is_some_and(|duration| duration > 0.0 && options.start_ms as f64 / 1000.0 >= duration)
    {
        return Err(invalid("시작 시간이 미디어 길이를 벗어났습니다."));
    }
    Ok(())
}

fn seconds(milliseconds: u64) -> String {
    format!("{}.{:03}", milliseconds / 1000, milliseconds % 1000)
}

fn video_target(key: &str) -> bool {
    matches!(
        key,
        "media:mp4" | "media:mov" | "media:mkv" | "media:webm" | "media:avi" | "media:wmv"
    )
}

fn recipe(
    source: &Path,
    output: &Path,
    key: &str,
    input_format: &str,
    input_is_audio: bool,
    audio: Option<&Value>,
) -> io::Result<Vec<OsString>> {
    let mut args = command(START);
    let video = video_target(key);
    if video && input_is_audio {
        extend(
            &mut args,
            &["-f", "lavfi", "-i", "color=c=0x1e1e23:s=640x360:r=24", "-i"],
        );
        args.push(source.into());
        extend(&mut args, &["-map", "0:v:0", "-map", "1:a:0", "-shortest"]);
    } else {
        args.push("-i".into());
        args.push(source.into());
        if video {
            extend(&mut args, &["-map", "0:v:0", "-map", "0:a:0?"]);
        }
    }
    match key {
        "media:mp4" | "media:mov" | "media:mkv" => {
            extend(
                &mut args,
                &[
                    "-vf", SCALE, "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt",
                    "yuv420p", "-c:a", "aac", "-b:a", "192k",
                ],
            );
            if key != "media:mkv" {
                extend(&mut args, &["-movflags", "+faststart"]);
            }
        }
        "media:avi" | "media:wmv" => {
            let (video, audio) = if key == "media:avi" {
                ("mpeg4", "libmp3lame")
            } else {
                ("wmv2", "wmav2")
            };
            extend(
                &mut args,
                &[
                    "-vf", SCALE, "-c:v", video, "-q:v", "4", "-pix_fmt", "yuv420p", "-c:a", audio,
                    "-b:a", "128k",
                ],
            );
        }
        "media:webm" => extend(
            &mut args,
            &[
                "-c:v",
                "libvpx-vp9",
                "-crf",
                "32",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "libopus",
                "-b:a",
                "128k",
            ],
        ),
        "media:mp3" => extend(
            &mut args,
            &["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "2"],
        ),
        "media:gif" => extend(
            &mut args,
            &[
                "-filter_complex",
                "[0:v:0]fps=15,scale=w='min(960,iw)':h=-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a[v]",
                "-map",
                "[v]",
                "-an",
                "-loop",
                "0",
            ],
        ),
        "strip" => {
            extend(
                &mut args,
                &[
                    "-map",
                    "0",
                    "-c",
                    "copy",
                    "-map_metadata",
                    "-1",
                    "-map_metadata:s",
                    "-1",
                    "-map_chapters",
                    "-1",
                    "-metadata",
                    "encoder=",
                    "-metadata:s",
                    "encoder=",
                ],
            );
            if input_format == "mp3" {
                extend(&mut args, &["-id3v2_version", "0", "-write_id3v1", "0"]);
            }
        }
        key if key.starts_with("audio:") => {
            let target = key.strip_prefix("audio:").unwrap();
            let depth = audio.map_or(0, bit_depth);
            let input = audio
                .and_then(|stream| stream["codec_name"].as_str())
                .unwrap_or("");
            let floating = input.starts_with("pcm_f32") || input.starts_with("pcm_f64");
            if matches!(target, "wav" | "flac") && depth > 32 && !floating {
                return Err(invalid(
                    "32비트를 넘는 정수 샘플의 정밀도를 이 출력에서 보존할 수 없습니다.",
                ));
            }
            let codec = match target {
                "m4a" => "aac",
                "wav" if input.starts_with("pcm_f64") => "pcm_f64le",
                "wav" if floating => "pcm_f32le",
                "wav" if depth > 24 => "pcm_s32le",
                "wav" if depth > 16 => "pcm_s24le",
                "wav" if depth == 8 && (input.starts_with("pcm_") || input == "flac") => "pcm_u8",
                "wav" => "pcm_s16le",
                "flac" if floating => {
                    return Err(invalid(
                        "부동소수점 PCM은 FLAC에서 정확히 보존할 수 없습니다. WAV를 선택하세요.",
                    ));
                }
                "flac" if input == "flac" => "copy",
                "flac" => "flac",
                "ogg" => "libvorbis",
                "opus" => "libopus",
                "aiff" => "pcm_s16be",
                "wma" => "wmav2",
                _ => return Err(invalid(format!("알 수 없는 미디어 작업: {key}"))),
            };
            extend(&mut args, &["-map", "0:a:0", "-vn", "-c:a", codec]);
            if target == "flac" && codec != "copy" && depth > 24 {
                extend(
                    &mut args,
                    &[
                        "-sample_fmt",
                        "s32",
                        "-bits_per_raw_sample",
                        "32",
                        "-strict",
                        "experimental",
                    ],
                );
            }
            if matches!(target, "m4a" | "opus" | "wma") {
                extend(&mut args, &["-b:a", "128k"]);
            } else if target == "ogg" {
                extend(&mut args, &["-q:a", "5"]);
            }
        }
        _ => return Err(invalid(format!("알 수 없는 미디어 작업: {key}"))),
    }
    extend(&mut args, &["-threads", "2"]);
    args.push(output.into());
    Ok(args)
}

fn apply_encoding(args: &mut Vec<OsString>, options: &EncodeOptions) -> io::Result<()> {
    if let Some(value) = options.video_crf {
        let index = args
            .iter()
            .position(|arg| arg == "-crf")
            .filter(|_| value <= 51)
            .ok_or_else(|| invalid("영상 CRF: 0~51 범위의 정수가 필요합니다."))?;
        args[index + 1] = value.to_string().into();
    }
    if let Some(value) = options.audio_bitrate {
        if !(32..=512).contains(&value) {
            return Err(invalid(
                "오디오 비트레이트: 32~512 범위의 정수가 필요합니다.",
            ));
        }
        let value = format!("{value}k").into();
        if let Some(index) = args.iter().position(|arg| arg == "-q:a" || arg == "-b:a") {
            args[index] = "-b:a".into();
            args[index + 1] = value;
        } else {
            let position = args.len() - 1;
            args.splice(position..position, ["-b:a".into(), value]);
        }
    }
    Ok(())
}

fn describe_loss(key: &str, streams: &[Value], notices: &mut Vec<String>) {
    if key == "strip" {
        return;
    }
    let audio: Vec<_> = streams
        .iter()
        .filter(|stream| stream["codec_type"] == "audio")
        .collect();
    let video = streams
        .iter()
        .filter(|stream| {
            stream["codec_type"] == "video" && integer(&stream["disposition"]["attached_pic"]) == 0
        })
        .count();
    if key == "media:gif" {
        notices.push("GIF에는 영상 한 트랙을 최대 폭 960·15fps·팔레트 색상으로 저장하며 음성·자막을 제외합니다.".to_owned());
    } else if key == "media:mp3" || key.starts_with("audio:") {
        if audio.len() > 1 {
            notices.push(format!(
                "오디오 출력은 첫 음성 트랙만 사용합니다. 나머지 {}개 음성 트랙은 제외합니다.",
                audio.len() - 1
            ));
        }
        if key == "audio:aiff"
            && audio.first().is_some_and(|stream| {
                integer(&stream["bits_per_sample"]) > 16
                    || integer(&stream["bits_per_raw_sample"]) > 16
            })
        {
            notices.push("이 출력은 원본 음성의 비트 깊이를 16비트 PCM으로 줄입니다.".to_owned());
        }
    } else if audio.len() > 1 || video > 1 || audio.len() + video != streams.len() {
        notices.push(
            "영상 재인코딩은 영상·음성의 첫 트랙만 사용하며 추가 트랙·자막·첨부를 제외합니다."
                .to_owned(),
        );
    }
}

fn track(stream: &Value) -> Value {
    let tags: BTreeMap<_, _> = stream["tags"]
        .as_object()
        .into_iter()
        .flatten()
        .map(|(key, value)| (key.to_ascii_lowercase(), value))
        .collect();
    let mut disposition: Vec<_> = stream["disposition"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(key, enabled)| (integer(enabled) != 0).then_some(key))
        .collect();
    disposition.sort_unstable();
    let language = tags.get("language").filter(|value| ***value != "und");
    serde_json::json!([
        stream["codec_type"],
        stream["codec_name"],
        language,
        tags.get("title"),
        tags.get("filename"),
        tags.get("mimetype"),
        disposition
    ])
}

fn check_remux(before: &Value, after: &Value, notices: &mut Vec<String>) -> io::Result<()> {
    let source = array(before, "streams");
    let output = array(after, "streams");
    if !source.iter().map(track).eq(output.iter().map(track)) {
        return Err(invalid(
            "MKV에 모든 트랙의 코덱·이름·언어·기본 설정을 유지할 수 없습니다.",
        ));
    }
    let chapters = array(before, "chapters");
    let actual = array(after, "chapters");
    if chapters.len() != actual.len() {
        return Err(invalid("MKV에 모든 챕터를 유지할 수 없습니다."));
    }
    if chapters.iter().zip(actual).any(|(a, b)| {
        a["tags"]["title"] != b["tags"]["title"]
            || ["start_time", "end_time"].iter().any(|key| {
                number(&a[key])
                    .zip(number(&b[key]))
                    .is_none_or(|(a, b)| (a - b).abs() > 0.001)
            })
    }) {
        return Err(invalid("MKV에 챕터 이름 또는 시간을 유지할 수 없습니다."));
    }
    if source
        .iter()
        .zip(output)
        .any(|(a, b)| a["time_base"] != b["time_base"])
    {
        notices.push("트랙을 재인코딩하지 않았습니다. MKV의 시간 단위에 맞춰 일부 재생 시각이 반올림될 수 있습니다.".to_owned());
    }
    Ok(())
}
