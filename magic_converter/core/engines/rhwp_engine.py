"""Run rhwp under the same cancellation and deadline policy as encoders."""
from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled, report_stage
from magic_converter.core.jobs.process_runner import run_process
from magic_converter.core.jobs.process_runner import _ProcessDirectory

VERSION = "0.8.7"


def locate() -> Path | None:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    path = root / "vendor" / "rhwp" / "rhwp.exe"
    if path.is_file():
        return path
    configured = os.environ.get("MAGIC_CONVERTER_RHWP")
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute() and path.is_file():
            return path
    return None


def time_limit(source: Path) -> float:
    size = source.stat().st_size * 3
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            size = sum(i.file_size for i in archive.infolist()
                       if re.fullmatch(r"Contents/section\d+\.xml", i.filename))
    return min(3600, 120 + 1800 * size / 1048576)


def diagnostics(stderr: str) -> list[str]:
    """Surface bounded engine warnings without treating normal output as loss."""
    messages = []
    for line in stderr.splitlines():
        if line.startswith(('WARN:', 'LAYOUT_OVERFLOW:', 'LAYOUT_OVERFLOW_DRAW:')):
            message = 'rhwp 진단: ' + line[:500]
            if message in messages:
                continue
            if len(messages) == 10:
                messages.append('rhwp 진단: 추가 경고는 생략했습니다. 출력의 글꼴·줄 넘침을 확인하세요.')
                break
            messages.append(message)
    return messages


def fallback_mono():
    """Use an installed Korean fixed-width face; never install/extract fonts."""
    from magic_converter.core.pdf.pdf_fonts import resolve_font, font_metrics
    for family in ('D2Coding', 'GulimChe', 'DotumChe'):
        face = resolve_font(family)
        if face is None:
            continue
        try:
            font = font_metrics(face)
            if all(font.has_glyph(ord(c)) for c in 'Az09가나다') and abs(font.glyph_advance(ord('i')) - font.glyph_advance(ord('W'))) < .001:
                return face
        except (ValueError, RuntimeError, OSError):
            continue
    return None


def font_directories() -> list[Path]:
    candidates = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts']
    if os.environ.get('LOCALAPPDATA'):
        candidates.append(Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Windows/Fonts')
    return list(dict.fromkeys(path for path in candidates if path.is_dir()))


def convert(source: Path, destination: Path, warning=lambda _: None) -> None:
    from magic_converter.core.engines.rhwp_hwpx import prepare
    with _ProcessDirectory(prefix='magic-rhwp-layout-') as folder:
        rendered_source, changes = prepare(source, Path(folder)) if destination.suffix.lower() == '.pdf' else (source, [])
        _convert(rendered_source, destination, warning, timeout=time_limit(source))
        if changes:
            tails = sum(c['operation'] == 'table_tail_height' for c in changes)
            breaks = sum(c['operation'] == 'stored_tail_page_break' for c in changes)
            if tails or breaks:
                warning(f'rhwp 조판 호환: 표 끝 빈 여유 {tails}개, 저장된 쪽 경계 {breaks}개를 임시 입력에 반영했습니다. '
                        '저장 줄·원본 파일은 유지했습니다. 최종 배치는 확인하세요.')
            romans = sum(c['operation'] == 'roman_missing_glyph' for c in changes)
            if romans:
                warning(f'rhwp 글꼴 호환: 원래 글꼴에 없는 로마숫자 Ⅺ·Ⅻ의 텍스트 구간 {romans}개에 시스템 또는 사용자 영역에 설치된 대체 글꼴을 사용했습니다. 원본 파일은 유지했습니다.')


def _convert(source: Path, destination: Path, warning, *, timeout) -> None:
    executable = locate()
    if executable is None:
        raise ConversionError("rhwp 엔진이 없습니다. 포함된 배포본을 복구하세요.")
    target = destination.suffix.lower()
    mono = None
    if target == ".pdf":
        arguments = ["export-pdf", str(source.resolve()), "-o", str(destination.resolve()),
                     "--fallback-sans", "Malgun Gothic", "--fallback-serif", "Batang"]
        directories = font_directories()
        for folder in directories:
            arguments += ["--font-path", str(folder)]
        mono = fallback_mono()
        if mono is not None:
            arguments += ['--fallback-mono', mono.family]
            if mono.path.parent not in directories:
                arguments += ['--font-path', str(mono.path.parent)]
    elif target in {".hwp", ".hwpx"}:
        arguments = ["convert" if target == ".hwp" else "export-hwpx", str(source.resolve()), str(destination.resolve())]
    else:
        raise ConversionError(f"rhwp 출력 형식이 아닙니다: {target}")
    report_stage(f"rhwp · {target[1:].upper()} 변환 중")
    result = run_process([str(executable), *arguments], timeout=timeout)
    check_cancelled()
    if result.returncode:
        destination.unlink(missing_ok=True)
        raise ConversionError(f"rhwp 변환 실패 ({result.returncode}): {(result.stderr or result.stdout)[-1200:]}")
    from magic_converter.core.engines.hwp_converter import validate_output
    validate_output(destination)
    for message in diagnostics(result.stderr):
        warning(message)
    if mono is not None and mono.family != 'D2Coding':
        warning(f'rhwp 고정폭 대체 글꼴: D2Coding 대신 설치된 {mono.family}을 사용합니다. 원본 글꼴과 배치는 다를 수 있습니다.')


def shaping_required(text: str) -> bool:
    # rhwp 0.8.7 places individual glyphs without complex-script shaping.
    return bool(re.search(r"[\u0590-\u0dff\u0e00-\u0eff\u1780-\u17ff]", text))
