"""Container transcoding preserves SFNT tables; outlines are never approximated."""

from fontTools.ttLib import TTFont
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.special_io import read_bytes
from magic_converter.core.jobs.job_control import check_cancelled
import io
import hashlib
import struct


def signature(font):
    from fontTools.pens.recordingPen import RecordingPen

    outlines = hashlib.sha256()
    glyph_set = font.getGlyphSet()
    for i, name in enumerate(font.getGlyphOrder()):
        if i % 128 == 0:
            check_cancelled()
        pen = RecordingPen()
        glyph_set[name].draw(pen)
        outlines.update(repr((name, pen.value)).encode())
    return {
        "glyphs": font.getGlyphOrder(),
        "cmap": font.getBestCmap(),
        "outlines": "glyf" if "glyf" in font else "CFF2" if "CFF2" in font else "CFF ",
        "tables": sorted(k for k in font.keys() if k not in {"GlyphOrder", "DSIG"}),
        "fsType": font["OS/2"].fsType if "OS/2" in font else None,
        "axes": (
            [
                (a.axisTag, a.minValue, a.defaultValue, a.maxValue)
                for a in font["fvar"].axes
            ]
            if "fvar" in font
            else []
        ),
        "outline_digest": outlines.hexdigest(),
        "names": (
            sorted(
                (n.nameID, n.platformID, n.platEncID, n.langID, n.toUnicode())
                for n in font["name"].names
            )
            if "name" in font
            else []
        ),
        "metrics": font["hmtx"].metrics if "hmtx" in font else {},
        "layout_variations": {
            key: hashlib.sha256(font.getTableData(key)).hexdigest()
            for key in (
                "GSUB",
                "GPOS",
                "GDEF",
                "kern",
                "gvar",
                "HVAR",
                "VVAR",
                "MVAR",
                "avar",
                "STAT",
                "COLR",
                "CPAL",
                "SVG ",
                "vmtx",
            )
            if key in font
        },
    }


def convert(source, destination, target, notice):
    data = read_bytes(source)
    if data[:4] in {b"wOFF", b"wOF2"} and (
        len(data) < 20 or struct.unpack_from(">I", data, 16)[0] > 64 * 1024 * 1024
    ):
        raise ConversionError("웹폰트의 압축 해제 크기는 64 MiB까지 허용합니다.")
    if data[:4] == b"ttcf":
        raise ConversionError("TTC/OTC 글꼴 모음은 개별 폰트로 분리한 뒤 사용하세요.")
    with TTFont(io.BytesIO(data), recalcTimestamp=False) as font:
        before = signature(font)
        if target == "ttf" and "glyf" not in font:
            raise ConversionError(
                "CFF/CFF2 외곽선의 TTF 근사 변환은 지원하지 않습니다. OTF 또는 WOFF를 선택하세요."
            )
        if not any(k in font for k in ("glyf", "CFF ", "CFF2")):
            raise ConversionError("지원하는 외곽선 테이블이 없는 폰트입니다.")
        if "DSIG" in font:
            del font["DSIG"]
            notice(
                "컨테이너 변경으로 무효화되는 폰트 디지털 서명(DSIG)을 제거했습니다."
            )
        notice(
            f"폰트의 OS/2 fsType={before['fsType']} 및 이름/라이선스 테이블을 보존합니다. 변환은 폰트의 임베딩·재배포 권한을 부여하지 않습니다."
        )
        if target == "otf" and "glyf" in font:
            notice(
                "TrueType 외곽선을 가진 OpenType(.otf)으로 저장했습니다. CFF 외곽선으로 바꾸지 않습니다."
            )
        old_flavor = font.flavor
        font.flavor = target if target in {"woff", "woff2"} else None
        if font.flavorData is not None and old_flavor != font.flavor:
            notice(
                "웹폰트 전용 메타데이터/사설 데이터는 다른 컨테이너에 복사하지 않습니다."
            )
            font.flavorData = None
        check_cancelled()
        font.save(destination)
    with TTFont(destination, recalcTimestamp=False) as output:
        after = signature(output)
        if before != after:
            raise ConversionError(
                "폰트 글리프·문자 매핑·테이블·가변 축·fsType 보존 검증에 실패했습니다."
            )
    check_cancelled()
