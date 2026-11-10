"""Pixel operations over every decoded frame, with explicit frame timing."""

import json
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence

from magic_converter.core.conversion.converter import ConversionError, _load_frames
from magic_converter.core.jobs.job_control import checked, check_cancelled


def transform_image(source: Path, destination: Path, key: str, options, progress) -> None:
    poster = False
    if source.suffix.lower() != ".svg":
        with Image.open(source) as image:
            if image.width * image.height * getattr(image, "n_frames", 1) > 128000000:
                raise ConversionError("이미지 도구의 전체 프레임 픽셀 제한을 초과했습니다. 프레임 추출을 사용하세요.")
            poster = bool(image.info.get("default_image", False))
    frames, durations, loop = _load_frames(source, preserve_timing=True)
    transformed = []
    try:
        for index, frame in enumerate(checked(frames)):
            if key == "tool:image_rotate":
                operation = {0: None, 90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                             270: Image.Transpose.ROTATE_90}[options.rotation]
                result = frame.copy() if operation is None else frame.transpose(operation)
            elif key == "tool:image_flip":
                result = frame.transpose(Image.Transpose.FLIP_LEFT_RIGHT if options.flip == "horizontal" else Image.Transpose.FLIP_TOP_BOTTOM)
            elif key == "tool:image_crop":
                x, y = options.crop_x, options.crop_y
                width, height = options.crop_width or frame.width - x, options.crop_height or frame.height - y
                if min(width, height) <= 0 or x + width > frame.width or y + height > frame.height:
                    raise ConversionError("자르기 영역이 이미지 크기를 벗어났습니다.")
                result = frame.crop((x, y, x + width, y + height))
            else:
                result = frame.convert("L").convert("RGBA")
                result.putalpha(frame.getchannel("A"))
            transformed.append(result)
            progress(min(90, (index + 1) * 90 // len(frames)))
        if len(transformed) == 1:
            transformed[0].save(destination, format="PNG", optimize=True, compress_level=9)
        else:
            animation_count = len(transformed) - int(poster)
            # Full-canvas SOURCE frames render independently. Alternating
            # disposal keeps Pillow from merging identical adjacent frames.
            transformed[0].save(destination, format="PNG", save_all=True, append_images=transformed[1:],
                                default_image=poster, duration=durations[1:] if poster else durations, loop=loop,
                                disposal=[index % 2 for index in range(animation_count)], blend=0)
        with Image.open(destination) as decoded:
            if decoded.n_frames != len(transformed) or decoded.size != transformed[0].size:
                raise ConversionError("출력 이미지의 프레임·크기가 올바르지 않습니다.")
        check_cancelled()
    finally:
        for frame in (*frames, *transformed):
            frame.close()


def extract_image_frames(source: Path, folder: Path, options, progress) -> None:
    if source.suffix.lower() != ".svg":
        with Image.open(source) as image:
            count = getattr(image, "n_frames", 1)
            if (count + options.frame_interval - 1) // options.frame_interval > options.max_frames:
                raise ConversionError("최대 프레임 수를 초과했습니다. 프레임 간격을 늘리세요.")
            poster = bool(image.info.get("default_image", False))
            manifest = {"source_frames": count, "loop": int(image.info.get("loop", 0)), "default_image": poster, "frames": []}
            elapsed = 0
            for index, frame in enumerate(checked(ImageSequence.Iterator(image))):
                duration = 0 if poster and index == 0 else frame.info.get("duration", 100)
                if index % options.frame_interval == 0:
                    name = f"frame_{index + 1:06d}.png"
                    with ImageOps.exif_transpose(frame).convert("RGBA") as decoded:
                        decoded.info.clear()
                        decoded.save(folder / name, format="PNG")
                    manifest["frames"].append({"file": name, "source_frame": index + 1,
                                                "start_ms": elapsed, "duration_ms": duration})
                elapsed += duration
                progress(min(99, (index + 1) * 99 // count))
            (folder / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    frames, durations, loop = _load_frames(source)
    try:
        indices = list(range(0, len(frames), options.frame_interval))
        if len(indices) > options.max_frames:
            raise ConversionError("최대 프레임 수를 초과했습니다. 프레임 간격을 늘리세요.")
        manifest = {"source_frames": len(frames), "loop": loop, "frames": []}
        starts, elapsed = [], 0
        for duration in durations:
            starts.append(elapsed)
            elapsed += duration
        for index, number in enumerate(checked(indices), start=1):
            name = f"frame_{number + 1:06d}.png"
            frames[number].save(folder / name, format="PNG")
            manifest["frames"].append({"file": name, "source_frame": number + 1,
                                        "start_ms": starts[number], "duration_ms": durations[number]})
            progress(min(99, index * 99 // len(indices)))
        (folder / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        for frame in frames:
            frame.close()
