"""Read and write APNG frame delays without rounding them to milliseconds."""

from fractions import Fraction
import struct
import zlib

from PIL.PngImagePlugin import PngInfo

from magic_converter.core.jobs.job_control import check_cancelled


def _frame_controls(stream):
    stream.seek(8)  # PNG signature, already checked by Pillow.
    while header := stream.read(8):
        check_cancelled()
        if len(header) != 8:
            raise ValueError("APNG 청크 헤더가 잘렸습니다.")
        size, kind = struct.unpack(">I4s", header)
        if kind == b"IEND":
            break
        if kind == b"fcTL":
            if size != 26:
                raise ValueError("잘못된 APNG 프레임 제어 정보입니다.")
            offset = stream.tell()
            data = stream.read(size)
            crc = stream.read(4)
            if len(data) != size or crc != struct.pack(">I", zlib.crc32(kind + data)):
                raise ValueError("APNG 프레임 제어 정보가 손상되었습니다.")
            yield offset, data
        else:
            stream.seek(size + 4, 1)


def read_delays(source, frame_count):
    with source.open("rb") as stream:
        delays = [struct.unpack_from(">HH", data, 20) for _, data in _frame_controls(stream)]
    if len(delays) != frame_count:
        raise ValueError("APNG 재생 시간과 프레임 수가 일치하지 않습니다.")
    return delays


def save_apng(frames, destination, poster, delays, loop):
    options = {"format": "PNG", "optimize": True, "compress_level": 9}
    if len(frames) == 1:
        # Pillow writes a static PNG for a one-frame animation without a poster.
        info = PngInfo()
        info.add(b"acTL", struct.pack(">II", 1, loop))
        info.add(b"fcTL", struct.pack(">IIIIIHHBB", 0, *frames[0].size, 0, 0, *delays[0], 0, 0))
        frames[0].save(destination, pnginfo=info, **options)
    else:
        # Full-canvas SOURCE frames reproduce the decoded canvases. Alternating
        # disposal prevents Pillow from merging identical adjacent canvases.
        frames[0].save(destination, save_all=True, append_images=frames[1:],
                       default_image=poster, loop=loop, duration=1, blend=0,
                       disposal=[index % 2 for index in range(len(delays))], **options)
        with destination.open("r+b") as stream:
            controls = _frame_controls(stream)
            for (offset, data), delay in zip(controls, delays, strict=True):
                data = data[:20] + struct.pack(">HH", *delay) + data[24:]
                stream.seek(offset)
                stream.write(data + struct.pack(">I", zlib.crc32(b"fcTL" + data)))


def webp_delays(delays):
    """Round cumulative frame boundaries so the error cannot accumulate."""
    elapsed = Fraction(0)
    previous = 0
    durations = []
    rounded = False
    for numerator, denominator in delays:
        check_cancelled()
        duration = Fraction(numerator * 1000, denominator or 100)
        rounded |= duration.denominator != 1
        elapsed += duration
        boundary = round(elapsed)
        milliseconds = boundary - previous
        if milliseconds > 0xffffff or boundary > 0x7fffffff:
            raise ValueError("WebP가 저장할 수 있는 프레임 재생 시간을 초과했습니다. PNG로 변환하세요.")
        durations.append(milliseconds)
        previous = boundary
    return durations, rounded
