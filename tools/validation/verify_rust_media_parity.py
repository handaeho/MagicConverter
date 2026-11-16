"""Exercise every reference route whose execution is owned by FFmpeg."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run


VIDEO = {
    "mp4": ("libx264", "aac"), "mov": ("libx264", "aac"), "mkv": ("libx264", "aac"),
    "webm": ("libvpx-vp9", "libopus"), "avi": ("mpeg4", "libmp3lame"), "wmv": ("wmv2", "wmav2"),
}
AUDIO = {
    "mp3": "libmp3lame", "m4a": "aac", "wav": "pcm_s24le", "flac": "flac",
    "ogg": "libvorbis", "opus": "libopus", "aiff": "pcm_s16be", "wma": "wmav2",
}


def ffmpeg(engine, *arguments):
    result = subprocess.run([engine, "-hide_banner", "-loglevel", "error", "-nostdin", *map(str, arguments)],
                            capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout


def fixtures(root, engine):
    root.mkdir()
    inputs = {}
    video = "testsrc2=size=64x48:rate=12:duration=0.5"
    audio = "sine=frequency=740:sample_rate=48000:duration=0.5"
    for extension, (picture, sound) in VIDEO.items():
        source = root / f"한글 영상 {extension}.{extension}"
        ffmpeg(engine, "-f", "lavfi", "-i", video, "-f", "lavfi", "-i", audio, "-c:v", picture,
               "-pix_fmt", "yuv420p", "-c:a", sound, "-threads", "2", "-shortest", source)
        inputs[extension] = source
    gif = root / "한글 애니.gif"
    ffmpeg(engine, "-f", "lavfi", "-i", video, "-threads", "2", gif)
    inputs["gif"] = gif
    for extension, codec in AUDIO.items():
        source = root / f"한글 음성 {extension}.{extension}"
        ffmpeg(engine, "-f", "lavfi", "-i", audio, "-c:a", codec, "-metadata", "title=한국어",
               "-threads", "2", source)
        inputs[extension] = source
    return inputs


def routes(matrix):
    groups = {}
    with matrix.open("rb") as rows:
        for line in rows:
            row = json.loads(line)
            if row["profile"] != "all" or not row["path"].startswith("input.") or not row["route"]:
                continue
            steps = row["route"]["steps"]
            if len(steps) == 1 and steps[0]["engine_id"] == "ffmpeg":
                groups.setdefault(row["action"], []).append(steps[0]["source"])
    return groups


def ffprobe(probe, path):
    data = json.loads(subprocess.check_output([probe, "-v", "error", "-show_streams", "-show_chapters",
                                               "-show_format", "-of", "json", path], timeout=30))
    for key in ("filename", "size", "bit_rate", "probe_score"):
        data["format"].pop(key, None)
    return data


def still_video(left, right, before, after, engine, source=None, probe=None):
    def without_video_length(data):
        streams = data["streams"]
        video, = (stream for stream in streams if stream["codec_type"] == "video")
        length = data["format"].pop("duration", None)
        if data["format"]["format_name"] == "asf":
            # ASF can report the same container-derived duration for both
            # streams. Compare the actual audio samples and timestamps below.
            audio, = (stream for stream in streams if stream["codec_type"] == "audio")
            if audio.get("duration_ts") == video.get("duration_ts"):
                for key in ("duration", "duration_ts"):
                    audio.pop(key, None)
        # A different count of identical video frames changes the reported
        # average video bitrate, even though the audio and video frames agree.
        for key in ("duration", "duration_ts", "nb_frames", "bit_rate"):
            video.pop(key, None)
        video.get("tags", {}).pop("DURATION", None)
        return length

    lengths = (without_video_length(before), without_video_length(after))
    assert before == after, (left, right, before, after)
    def audio(path):
        return ffmpeg(engine, "-i", path, "-map", "0:a:0", "-c:a", "pcm_s32le", "-f", "framemd5", "-")
    assert audio(left) == audio(right), (left, right)

    def frames(path):
        result = ffmpeg(engine, "-i", path, "-map", "0:v:0", "-c:v", "rawvideo", "-threads:v", "1",
                        "-pix_fmt", "rgb24", "-f", "framemd5", "-")
        hashes = [line.rsplit(b",", 1)[1].strip() for line in result.splitlines() if not line.startswith(b"#")]
        assert hashes and len(set(hashes)) == 1
        return hashes[0], len(hashes)

    left_hash, left_count = frames(left)
    right_hash, right_count = frames(right)
    assert left_hash == right_hash
    if source is not None:
        original = ffprobe(probe, source)
        stream, *_ = [stream for stream in original["streams"] if stream["codec_type"] == "audio"]
        duration = float(stream.get("duration") or original["format"]["duration"])
        # The synthetic picture is sampled at 24 fps, so its duration is
        # quantized to frame boundaries. Compare audio against Python and
        # video against the source duration instead of Python's variable tail.
        assert abs(right_count / 24 - duration) <= 1 / 24 + .001, (source, duration, right_count)
        return {"comparison": "still-tail-bounded", "input_audio_s": duration,
                "python_duration": lengths[0], "rust_duration": lengths[1],
                "python_frames": left_count, "rust_frames": right_count}
    return {"comparison": "still-tail-differs", "python_duration": lengths[0], "rust_duration": lengths[1],
            "python_frames": left_count, "rust_frames": right_count}


def equivalent(left, right, engine, probe, *, compare_still=False, fixed_tail_source=None):
    assert left.name == right.name and left.is_dir() == right.is_dir(), (left, right)
    if left.is_dir():
        names = [file.relative_to(left) for file in sorted(left.rglob("*"))]
        assert names == [file.relative_to(right) for file in sorted(right.rglob("*"))]
        assert all(digest(left / name) == digest(right / name) for name in names)
        return {"comparison": "folder-bytes", "members": len(names)}
    hashes = (digest(left), digest(right))
    if hashes[0] == hashes[1] and fixed_tail_source is None:
        return {"comparison": "file-bytes", "sha256": hashes[0]}
    before, after = ffprobe(probe, left), ffprobe(probe, right)
    if fixed_tail_source is not None or (before != after and compare_still):
        return still_video(left, right, before, after, engine, fixed_tail_source, probe)
    # Ogg muxers choose a random serial number. Compare decoded frames and timestamps
    # plus all stream/container fields except physical file size and bitrate.
    assert before == after, (left, right, before, after)
    decode = lambda path: ffmpeg(engine, "-i", path, "-map", "0:v?", "-map", "0:a?", "-c:v", "rawvideo",
                                 "-threads:v", "1", "-pix_fmt", "rgb24", "-c:a", "pcm_s32le", "-f", "framemd5", "-")
    assert decode(left) == decode(right), (left, right)
    return {"comparison": "decoded-frames-and-probe", "python_sha256": hashes[0], "rust_sha256": hashes[1]}


def error_lines(messages):
    """Keep diagnostics while removing engine-generated addresses and stage paths."""
    cleaned = []
    for message in messages:
        message = re.sub(r" @ [0-9A-Fa-f]{8,}(?=\])", " @ <addr>", message)
        message = re.sub(r"(?i)[a-z]:[\\/][^\r\n]*?[\\/]\.magic_converter-[^ \r\n]+", "<stage>", message)
        cleaned.append(message.strip())
    return cleaned


def batch(prefix, sources, action, out, profile, environment=None, timeout=None, *, preset=False):
    out.mkdir(parents=True, exist_ok=True)
    option = "--preset" if preset else "--action"
    args = ["convert", *sources, option, action, "--output", out, "--data-dir", profile, "--json"]
    if timeout is not None:
        args.extend(("--timeout", str(timeout)))
    start = time.perf_counter()
    status, result = run(prefix, args, environment=environment)
    return status, result, time.perf_counter() - start


def compare_batch(results, engine, probe):
    (status, reference, py_s), (actual_status, actual, rust_s) = results
    assert status == actual_status and reference["ok"] == actual["ok"], (
        status, actual_status, [job["errors"] for job in reference["jobs"]], [job["errors"] for job in actual["jobs"]],
    )
    expected, = reference["jobs"]
    candidate, = actual["jobs"]
    assert all(expected[key] == candidate[key] for key in ("action", "total", "completed", "cancelled", "timed_out", "warnings"))
    records = []
    pairs = []
    for left, right in zip(expected["files"], candidate["files"], strict=True):
        source = Path(left["source"])
        assert left["source"] == right["source"] and left["state"] == right["state"], (left, right)
        assert left["warnings"] == right["warnings"], (left, right)
        assert len(left["outputs"]) == len(right["outputs"])
        item = {"source": source.name, "state": left["state"], "warnings": left["warnings"]}
        if left["state"] != "done":
            assert left["errors"] and right["errors"] and not left["outputs"] and not right["outputs"]
            errors = (error_lines(left["errors"]), error_lines(right["errors"]))
            item["errors"] = errors[0] if errors[0] == errors[1] else {"python": errors[0], "rust": errors[1]}
        for a, b in zip(left["outputs"], right["outputs"], strict=True):
            compare_still = source.suffix.lower()[1:] in AUDIO and expected["action"] in {f"media:{name}" for name in VIDEO}
            fixed_tail = source if compare_still else None
            pairs.append((item, Path(a), Path(b), compare_still, fixed_tail))
        records.append(item)
    with ThreadPoolExecutor(max_workers=3) as pool:
        checks = [(item, pool.submit(equivalent, left, right, engine, probe,
                                    compare_still=compare_still, fixed_tail_source=fixed_tail))
                  for item, left, right, compare_still, fixed_tail in pairs]
        for item, result in checks:
            item["output"] = result.result()
    return {"python_s": py_s, "rust_s": rust_s, "files": records}


def tool_preset(directory, presets):
    directory.mkdir()
    (directory / "settings.json").write_text(json.dumps({"version": 3, "presets": presets}), encoding="utf-8")


def precision(root, commands, engine, probe, profiles):
    output = {}
    for width in (2, 3, 4):
        source = root / f"precision-{width * 8}.wav"
        limit = 2 ** (8 * width - 1)
        original = b"".join(sample.to_bytes(width, "little", signed=True)
                            for sample in (-limit, -limit + 7, -127, 0, 1, 375, limit - 2, limit - 1)) * 256
        with wave.open(str(source), "wb") as wavefile:
            wavefile.setparams((1, width, 48000, 0, "NONE", "not compressed"))
            wavefile.writeframes(original)
        paths = []
        for label, prefix, environment in commands:
            _, data, _ = batch(prefix, [source], "audio:flac", root / label / f"flac-{width}", profiles[label], environment)
            path, = data["jobs"][0]["outputs"]
            paths.append(Path(path))
        compare = equivalent(*paths, engine, probe)
        for (label, prefix, environment), path in zip(commands, paths, strict=True):
            _, data, _ = batch(prefix, [path], "audio:wav", root / label / f"wav-{width}", profiles[label], environment)
            wav, = data["jobs"][0]["outputs"]
            with wave.open(wav, "rb") as wavefile:
                assert wavefile.getsampwidth() == width and wavefile.readframes(wavefile.getnframes()) == original
        output[str(width * 8)] = {"source_samples_sha256": hashlib.sha256(original).hexdigest(), **compare}
    source = root / "float.wav"
    ffmpeg(engine, "-f", "lavfi", "-i", "sine=duration=0.2", "-c:a", "pcm_f32le", source)
    comparisons = [batch(prefix, [source], "audio:flac", root / label / "float", profiles[label], environment)
                   for label, prefix, environment in commands]
    assert all(item[0] == 1 and "부동소수점 PCM" in str(item[1]) for item in comparisons)
    output["float_flac"] = compare_batch(comparisons, engine, probe)
    return output


def main():
    parser = argparse.ArgumentParser()
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--baseline", type=Path)
    reference.add_argument("--python-cli", type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    worker = args.worker.resolve(strict=True)
    engine = args.ffmpeg.resolve(strict=True)
    probe = engine.with_name("ffprobe.exe")
    environment = native_environment()
    environment["MAGIC_CONVERTER_FFMPEG"] = str(engine)
    python_env = {**os.environ, "MAGIC_CONVERTER_FFMPEG": str(engine), "PYTHONUTF8": "1"}
    python = ([args.python_cli.resolve(strict=True)] if args.python_cli else
              [sys.executable, "-B", args.baseline.resolve(strict=True) / "run_magic_converter.py"])
    commands = (("python", python, python_env),
                ("rust", [worker], environment))
    presets = [
        {"id": "crf", "name": "CRF 시험", "action_key": "media:mp4",
         "options": {"video_crf": 31, "audio_bitrate": 96}},
        {"id": "trim", "name": "구간 시험", "action_key": "tool:media_trim",
         "tools": {"start_ms": 100, "duration_ms": 300}},
        {"id": "frames", "name": "프레임 시험", "action_key": "tool:media_frames",
         "tools": {"start_ms": 0, "duration_ms": 300, "frame_interval": 2, "max_frames": 4}},
        {"id": "limit", "name": "프레임 상한", "action_key": "tool:media_frames", "tools": {"max_frames": 1}},
    ]
    profiles = {label: root / f"profile-{label}" for label, _, _ in commands}
    for profile in profiles.values():
        tool_preset(profile, presets)
    inputs = fixtures(root / "inputs", engine)
    fingerprints = {name: digest(path) for name, path in inputs.items()}
    report = {"status": "running", "worker_sha256": digest(worker), "ffmpeg_sha256": digest(engine),
              "ffprobe_sha256": digest(probe), "inputs": fingerprints, "actions": {}, "extras": {}}
    if args.python_cli:
        report["python_cli_sha256"] = digest(python[0])

    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def compare(label, sources, action, *, preset=False, timeout=None, expect=None):
        results = [batch(prefix, sources, action, root / label / edition, profiles[edition], environment, timeout, preset=preset)
                   for edition, prefix, environment in commands]
        if expect is not None:
            assert all(status == expect for status, _, _ in results), (label, results)
        return compare_batch(results, engine, probe)

    save()
    try:
        for action, formats in routes(args.matrix).items():
            report["actions"][action] = compare(action.replace(":", "-"), [inputs[name] for name in formats], action)
            save()
            print(action, len(formats), flush=True)
        for preset in presets:
            key = preset["id"]
            report["extras"][key] = compare(f"preset-{key}", [inputs["mp4"]], key,
                                             preset=True, expect=int(key == "limit"))
        report["extras"]["pcm"] = precision(root / "inputs", commands, engine, probe, profiles)
        report["extras"]["deadline"] = compare("deadline", [inputs["mp4"]], "media:mp4", timeout=0.000001, expect=1)
        assert all(digest(path) == fingerprints[name] for name, path in inputs.items())
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
