"""Bounded tracking conversion for verified glyph advances."""
from functools import lru_cache
from hashlib import file_digest
from itertools import chain, pairwise
import math
from string import ascii_uppercase, digits
from magic_converter.core.jobs.job_control import check_cancelled

_MIXED_WIDTH_FACES = {'gulimche': 'GulimChe', '굴림체': 'GulimChe',
                      'dotumche': 'DotumChe', '돋움체': 'DotumChe'}
# Native verification covers this exact regular Tahoma face. Its Latin GSUB
# does not substitute this repertoire. These are all its legacy kern pairs
# within the repertoire, and its Latin GPOS does not add pair adjustments.
_TAHOMA_SHA256 = '9af03d4ad44a3b413d92f7de48b94aa7cc8a1471a75d498406eae837f62ee1d1'
_TAHOMA_CHARACTERS = frozenset(ascii_uppercase + digits + ' bdeghmnoprsu')
_TAHOMA_KERN_PAIRS = frozenset(map(tuple, (
    'AS AT AU AV AW AY BC BG BO BQ BT DJ DT DW DX DY DZ FA FJ FT JA KO LA LC LG LJ LO LQ LT LV LW LY '
    'OJ OT OX OY OZ PA PJ PY RT RY SA SS TA TC TG TJ TO TS TT TY UA VA WA XC XG XO YA YJ YO ZC ZG ZO ZZ '
    'Au Be Bh Bo Br Bu Fe Fo Id Ie Ig Im In Io Ip Ir Is Ke Ko Ku Pe Po Re Ro Ru Se So Te Tg To Tr Ts Tu '
    'Ve Vo Vu We Wo Wr Wu Xe Xo Xu Yd Ye Yg Ym Yn Yo Yp Yr Ys Yu Ze Zo eT rd re rg ro'
).split()))


def _proportional_run(run):
    return (bool(run.font) and run.font.casefold() == 'tahoma' and run.proportional_tracking_allowed
            and not run.bold and not run.italic and run.width_percent == 100
            and 0 < len(run.text) <= 16384 and _TAHOMA_CHARACTERS.issuperset(run.text))


def _kerning_free(characters):
    return all(pair not in _TAHOMA_KERN_PAIRS for pair in pairwise(characters))


@lru_cache(maxsize=4)
def _verified_tahoma(path, size, modified_ns):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest() == _TAHOMA_SHA256


def glyph_advances(run, *, allow_proportional=True):
    # Combining/RTL/contextual shaping and scaled runs still need a richer model.
    text = run.text
    if not text or len(text) > 16384 or run.width_percent != 100 or not math.isfinite(run.size) or run.size <= 0:
        return None
    characters = set(text)
    hangul = {c for c in characters if 0xAC00 <= ord(c) <= 0xD7A3}
    ascii_text = characters - hangul
    if ascii_text and (run.bold or run.italic or any(not ' ' <= c <= '~' for c in ascii_text)):
        return None
    proportional = allow_proportional and bool(ascii_text) and _proportional_run(run) and _kerning_free(text)
    expected_face = 'Tahoma' if proportional else _MIXED_WIDTH_FACES.get(run.font.casefold())
    if ascii_text and expected_face is None:
        return None
    from magic_converter.core.pdf.pdf_fonts import resolve_font, font_metrics
    face = resolve_font(run.font, run.bold, run.italic)
    if face is None or (face.bold, face.italic) != (run.bold, run.italic):
        return None
    if ascii_text and face.family != expected_face:
        return None
    try:
        if proportional:
            stat = face.path.stat()
            if face.offset != 0 or not _verified_tahoma(face.path, stat.st_size, stat.st_mtime_ns):
                return None
        font = font_metrics(face)
        if ascii_text and not proportional and not font.flags.get('mono'):
            return None
        values = {}
        for c in characters:
            check_cancelled()
            if not font.has_glyph(ord(c), fallback=False):
                return None
            advance = font.glyph_advance(ord(c))
            # These installed faces are verified with ASCII halfwidth and
            # precomposed Hangul fullwidth, with no proportional substitution.
            if ascii_text and not proportional and advance != (.5 if c in ascii_text else 1):
                return None
            values[c] = advance * run.size
        if any(not math.isfinite(value) or value <= 0 for value in values.values()):
            return None
        if not ascii_text and max(values.values()) - min(values.values()) > 1e-6:
            return None
        return [values[c] for c in text]
    except (ValueError, RuntimeError, OSError):
        check_cancelled()
        return None


def uniform_advance(run):
    # Proportional fonts need the paragraph-wide proof in metric_segments,
    # even when a particular run contains only one repeated character.
    values=glyph_advances(run, allow_proportional=False)
    return values[0]if values and max(values)-min(values)<=1e-6 else None


def metric_segments(runs,target):
    """Bound cumulative tracking quantization, without shifting glyph outlines.

    Word stores twips; HWPX stores integer percentages of each glyph's advance.
    Carry rounding error across supported runs so long text does not
    accumulate a one-sided rounding loss. Unsupported text stays unchanged
    and reaches the existing explicit loss warning.
    """
    from dataclasses import replace
    from magic_converter.core.documents.hwp_documents import TextRun
    if not isinstance(runs, (list, tuple)):
        runs = tuple(runs)
    # Run splitting can move a kerning or shaping boundary. Require one
    # verified repertoire for the whole paragraph, including adjacent runs.
    proportional = (all(isinstance(run, TextRun) and (not run.text or _proportional_run(run)) for run in runs)
                    and _kerning_free(chain.from_iterable(run.text for run in runs)))
    error=0
    for run in runs:
        if not isinstance(run,TextRun):
            error=0;yield run;continue
        requested=run.spacing_percent if target=='docx'else run.spacing_pt
        if not requested or target=='docx'and run.spacing_pt:
            yield run;continue
        advances=glyph_advances(run, allow_proportional=proportional)
        if advances is None:
            error=0;yield run;continue
        parts=[];pending='';previous=None;balance=error
        for character,advance in zip(run.text,advances):
            check_cancelled()
            desired=advance*run.spacing_percent/100 if target=='docx'else run.spacing_pt
            quantum=.05 if target=='docx'else advance/100
            units=round((desired+balance)/quantum)
            if target=='hwpx'and not -50<=units<=50:
                parts=None;break
            value=units*.05 if target=='docx'else units
            balance+=desired-units*quantum
            if previous is not None and value!=previous:
                parts.append((pending,previous));pending=''
            pending+=character;previous=value
        if parts is None:
            error=0;yield run;continue
        if pending:parts.append((pending,previous))
        error=balance
        for text,value in parts:
            yield replace(run,text=text,spacing_pt=value if target=='docx'else 0,
                          spacing_percent=value if target=='hwpx'else 0)


def metric_characters(run):
    """Comparable per-character tracking, independent of writer run splitting."""
    if run.spacing_pt:
        return [(c,'pt',run.spacing_pt,run.width_percent)for c in run.text]
    if not run.spacing_percent:
        return [(c,'pt',0,run.width_percent)for c in run.text]
    advances=glyph_advances(run)
    if advances is None:return [(c,'percent',run.spacing_percent,run.width_percent)for c in run.text]
    return [(c,'pt',advance*run.spacing_percent/100,run.width_percent)for c,advance in zip(run.text,advances)]


def docx_spacing(run):
    if run.spacing_pt:return run.spacing_pt
    if not run.spacing_percent:return 0
    advance=uniform_advance(run)
    return advance*run.spacing_percent/100 if advance is not None else None


def hwpx_spacing(run,warnings):
    if not run.spacing_pt:return run.spacing_percent
    advance=uniform_advance(run)
    value=round(run.spacing_pt/advance*100)if advance is not None else None
    if value is not None and -50<=value<=50:return value
    warning='DOCX 고정 pt 자간은 이 글꼴·문자 구간에서 HWPX 자간율로 검증할 수 없어 생략됩니다.'
    if warning not in warnings:warnings.append(warning)
    return 0


def spacing_key(run):
    points=docx_spacing(run)
    return ('pt',points)if points is not None else('percent',run.spacing_percent)
