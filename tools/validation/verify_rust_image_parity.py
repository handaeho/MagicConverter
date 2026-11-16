"""Compare decoded images, frame timelines and SVG PDF content on the native CLI."""

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import time

from PIL import Image, ImageCms, ImageSequence
from pillow_heif import register_heif_opener
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run

register_heif_opener()

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 240 120">
<defs><linearGradient id="g"><stop stop-color="#237fa1"/><stop offset="1" stop-color="#6a3dca"/></linearGradient></defs>
<rect width="240" height="120" fill="url(#g)"/>
<path d="M5 90 C80 10 160 120 220 25" fill="none" stroke="white"/>
<text x="20" y="50" font-family="Segoe UI" font-size="16">Vector sample</text></svg>"""


def fixtures(root):
    root.mkdir()
    image = Image.new("RGBA", (64, 48))
    image.putdata([(x * 4, y * 5, (7 * x + 13 * y) % 256, 255 if (x // 9 + y // 8) % 2 else (x * y) % 256)
                   for y in range(image.height) for x in range(image.width)])
    sources = {kind: root / f"한글 image.{kind}" for kind in
               ("png", "jpg", "bmp", "webp", "tiff", "gif", "svg", "heic", "avif")}
    image.save(sources["png"], compress_level=0)
    image.convert("RGB").save(sources["jpg"], quality=94, dpi=(143, 91))
    image.convert("RGB").save(sources["bmp"])
    image.save(sources["webp"], lossless=True)
    image.save(sources["tiff"], compression="raw")
    image.save(sources["heic"], format="HEIF", quality=90)
    image.save(sources["avif"], format="AVIF", quality=90)
    frames = [Image.new("RGBA", (64, 48), color) for color in ("red", (0, 0, 0, 0), "blue")]
    frames[1].putpixel((3, 4), (30, 200, 64, 255))
    frames[0].save(sources["gif"], save_all=True, append_images=frames[1:],
                   duration=[0, 90, 140], loop=2, disposal=[1, 2, 1])
    sources["svg"].write_text(SVG, encoding="utf-8")
    return sources


def native_routes(worker, matrix, root):
    output = root / "rust-plans.jsonl"
    with matrix.open("rb") as source, output.open("wb") as target:
        subprocess.run([worker, "compare-plans"], stdin=source, stdout=target,
                       check=True, timeout=30, env=native_environment())
    routes = defaultdict(list)
    with matrix.open("rb") as expected, output.open("rb") as actual:
        for left, right in zip(expected, actual, strict=True):
            row, result = json.loads(left), json.loads(right)
            if row["profile"] != "all" or not row["path"].startswith("input.") or not result["native"]:
                continue
            steps = result["route"]["steps"]
            if len(steps) != 1:
                continue
            edge = steps[0]
            if edge["engine_id"] in {"images", "image_tools"} and (row["action"] != "document:pdf" or edge["source"] == "svg"):
                routes[row["action"]].append(edge["source"])
    return routes


def png_delays(path):
    data = path.read_bytes()
    position = 8
    delays = []
    while position + 8 <= len(data):
        length, kind = struct.unpack_from(">I4s", data, position)
        if kind == b"fcTL":
            delays.append(struct.unpack_from(">HH", data, position + 28))
        position += 12 + length
    return delays


def image_model(path):
    with Image.open(path) as image:
        frames = []
        for frame in ImageSequence.Iterator(image):
            rgba = frame.convert("RGBA")
            frames.append((rgba.size, frame.info.get("duration"), rgba.tobytes()))
        metadata = {key: image.info[key] for key in ("icc_profile", "dpi", "exif", "xmp") if key in image.info}
        return (image.info.get("loop", 0), bool(image.info.get("default_image")), metadata, frames)


def pixel_metrics(a, b):
    pairs = list(zip(a, b, strict=True))
    count = sum(len(left) for left, _ in pairs)
    deviations = [abs(p - q) for left, right in pairs for p, q in zip(left, right, strict=True)]
    square_error = sum(delta * delta for delta in deviations)
    return {"mean_absolute": sum(deviations) / count, "max": max(deviations),
            "psnr_db": 20 * math.log10(255 / math.sqrt(square_error / count)) if square_error else None}


def compare_image(left, right):
    loop_a, poster_a, metadata_a, frames_a = image_model(left)
    loop_b, poster_b, metadata_b, frames_b = image_model(right)
    geometry_a = [frame[0] for frame in frames_a]
    geometry_b = [frame[0] for frame in frames_b]
    timing = (loop_a, poster_a, [item[1] for item in frames_a]) == (loop_b, poster_b, [item[1] for item in frames_b])
    result = {"geometry_equal": geometry_a == geometry_b, "timing_equal": timing,
              "metadata_equal": metadata_a == metadata_b, "bytes_equal": digest(left) == digest(right),
              "python_bytes": left.stat().st_size, "rust_bytes": right.stat().st_size}
    if geometry_a == geometry_b:
        result["pixels"] = pixel_metrics([item[2] for item in frames_a], [item[2] for item in frames_b])
    else:
        result.update(python_geometry=geometry_a, rust_geometry=geometry_b)
    if not timing:
        result.update(python_timing=[loop_a, poster_a, [item[1] for item in frames_a]],
                      rust_timing=[loop_b, poster_b, [item[1] for item in frames_b]])
    if metadata_a != metadata_b:
        result.update(python_metadata=sorted(metadata_a), rust_metadata=sorted(metadata_b))
    result["equal"] = (geometry_a == geometry_b and timing and metadata_a == metadata_b
                       and result["pixels"]["max"] == 0)
    return result


def pdf_model(path):
    with pymupdf.open(path) as document:
        page, = document
        assert abs(page.rect.width - 100 * 72 / 25.4) < .001
        assert abs(page.rect.height - 50 * 72 / 25.4) < .001
        assert "Vector sample" in page.get_text() and page.get_drawings() and not page.get_images()
        image = page.get_pixmap()
        return {"size_pt": [page.rect.width, page.rect.height], "text": page.get_text(),
                "drawing_count": len(page.get_drawings()), "image_count": len(page.get_images())}, image.samples


def compare(left, right):
    if left.is_dir():
        manifest_a = json.loads((left / "frames.json").read_bytes())
        manifest_b = json.loads((right / "frames.json").read_bytes())
        frames = [compare_image(left / item["file"], right / item["file"]) for item in manifest_a["frames"]
                  if (right / item["file"]).is_file()]
        return {"equal": manifest_a == manifest_b and len(frames) == len(manifest_a["frames"])
                and all(item["equal"] for item in frames), "manifest_equal": manifest_a == manifest_b,
                "frames": frames, "python_manifest": manifest_a, "rust_manifest": manifest_b}
    if left.suffix == ".pdf":
        a, pixels_a = pdf_model(left)
        b, pixels_b = pdf_model(right)
        metrics = pixel_metrics([pixels_a], [pixels_b])
        return {"equal": metrics["max"] == 0, "same_vector_text_contract": True,
                "python": a, "rust": b, "pixels": metrics}
    return compare_image(left, right)


class Compare:
    def __init__(self, baseline, worker, root):
        self.commands = ([sys.executable, "-B", baseline / "run_magic_converter.py"], [worker])
        self.root = root

    def job(self, key, action, paths, settings=None, profile=None):
        path = self.root / key
        outputs = []
        for label, command, env in zip(("python", "rust"), self.commands, (None, native_environment()), strict=True):
            output = path / label
            output.mkdir(parents=True)
            profile = profile or path / "profile"
            if settings:
                profile.mkdir(parents=True, exist_ok=True)
                (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            start = time.perf_counter()
            code, report = run(command, ("convert", "--data-dir", profile, *action, "--output", output, *paths), environment=env)
            files = report.get("jobs", [{}])[0].get("files", []) if report.get("jobs") else []
            assert len(files) == len(paths), (action, label, report)
            outputs.append((code, files, time.perf_counter() - start))
        py, rs = outputs
        compared = []
        for a, b in zip(py[1], rs[1], strict=True):
            entry = {"source": Path(a["source"]).name, "python_state": a["state"], "rust_state": b["state"],
                     "python_warnings": a["warnings"], "rust_warnings": b["warnings"]}
            if a["state"] == b["state"] == "done":
                entry["output"] = compare(Path(a["outputs"][0]), Path(b["outputs"][0]))
                entry["output"]["equal"] &= a["warnings"] == b["warnings"]
            elif a["state"] == b["state"] == "failed":
                entry["errors"] = [a["errors"], b["errors"]]
            else:
                entry["errors"] = [a["errors"], b["errors"]]
            compared.append(entry)
        return {"files": compared, "python_s": py[2], "rust_s": rs[2], "codes": [py[0], rs[0]]}


def animation(compare_job, root):
    source = root / "poster.apng"
    frames = [Image.new("RGBA", (32, 20), color) for color in ("red", "green", "blue")]
    frames[0].save(source, format="PNG", save_all=True, append_images=frames[1:], default_image=True,
                   duration=[16.5, 33.5], loop=3)
    settings = {"version": 3, "presets": [{"id": "small", "name": "Small", "action_key": "image:png",
                                         "options": {"image_max_edge": 16}}]}
    result = {
        "resize": compare_job.job("apng-resize", ("--preset", "small"), [source], settings),
        "flip": compare_job.job("apng-flip", ("--action", "tool:image_flip"), [source]),
        "webp": compare_job.job("apng-webp", ("--action", "image:webp"), [source]),
        "frames": compare_job.job("apng-frames", ("--action", "tool:image_frames"), [source]),
    }
    for key in ("resize", "flip"):
        files = [next((compare_job.root / f"apng-{key}" / label).glob("*.png")) for label in ("python", "rust")]
        assert png_delays(source) == png_delays(files[0]) == png_delays(files[1]), (key, list(map(png_delays, files)))
    for key, item in result.items():
        assert item["files"][0]["output"]["equal"], (key, item)
    return result


def container_metadata(compare_job, root):
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    exif = Image.Exif()
    exif[270] = "Synthetic test image"
    evidence = {}
    for suffix, image_format in (("heic", "HEIF"), ("avif", "AVIF")):
        source = root / f"metadata.{suffix}"
        Image.new("RGB", (72, 48), (30, 130, 225)).save(
            source, image_format, icc_profile=profile, exif=exif,
        )
        key = f"metadata-{suffix}"
        evidence[suffix] = compare_job.job(key, ("--action", "image:png"), [source])
        for side in ("python", "rust"):
            file, = (compare_job.root / key / side).glob("*.png")
            with Image.open(file) as image:
                assert image.mode == "RGB" and image.info.get("icc_profile") == profile
                assert image.getexif().get(270) == exif[270]
        assert evidence[suffix]["files"][0]["output"]["equal"], evidence[suffix]
    return evidence


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    evidence = {"status": "running", "worker_sha256": digest(args.worker), "actions": {}}
    inputs = fixtures(root / "inputs")
    comparator = Compare(args.baseline.resolve(), args.worker.resolve(), root)
    try:
        for action, sources in native_routes(args.worker.resolve(), args.matrix.resolve(), root).items():
            print(action, len(sources), flush=True)
            evidence["actions"][action] = comparator.job(action.replace(":", "-"), ("--action", action),
                                                         [inputs[source] for source in sources])
        counts = Counter()
        for record in evidence["actions"].values():
            for file in record["files"]:
                if "output" in file:
                    counts["equal" if file["output"]["equal"] else "different_output"] += 1
                elif file["python_state"] == file["rust_state"]:
                    counts["both_rejected"] += 1
                else:
                    counts["different_state"] += 1
        evidence["counts"] = dict(counts)
        evidence["animation"] = animation(comparator, root)
        evidence["container_metadata"] = container_metadata(comparator, root)
        evidence["status"] = "complete"
    finally:
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
