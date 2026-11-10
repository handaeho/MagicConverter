"""Resolve PDF PostScript names to installed Office faces, without a GUI.

Only read local font metadata. In particular, never install an extracted PDF
subset as a system font: it generally cannot support subsequent text edits.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pymupdf as fitz


def font_key(name: str) -> str:
    name = re.sub(r"^[A-Z]{6}\+", "", name)
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return "".join(c for c in name.casefold() if c.isalnum())


@dataclass(frozen=True)
class OfficeFont:
    path: Path
    offset: int
    family: str
    bold: bool
    italic: bool
    names: tuple[tuple[str, int], ...]


def _font_paths() -> list[Path]:
    roots = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Windows/Fonts")
    paths = set()
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts") as key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        value = Path(os.path.expandvars(winreg.EnumValue(key, i)[1]))
                        if value.is_absolute():
                            paths.add(value)
                        else:
                            paths.update(root / value for root in roots)
            except OSError:
                continue
    else:
        roots += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
                  Path.home() / ".fonts", Path.home() / ".local/share/fonts",
                  Path("/System/Library/Fonts"), Path("/Library/Fonts")]
    for root in roots:
        if root.is_dir():
            paths.update(p for p in root.rglob("*") if p.suffix.lower() in {".ttf", ".otf", ".ttc", ".otc"})
    return sorted(paths)


def _tables(data: bytes, offset: int) -> dict[bytes, tuple[int, int]]:
    count = struct.unpack_from(">H", data, offset + 4)[0]
    if count > 512:
        raise ValueError("Invalid sfnt table count")
    result = {}
    for start in range(offset + 12, offset + 12 + count * 16, 16):
        location, size = struct.unpack_from(">II", data, start + 8)
        if location + size > len(data):
            raise ValueError("Invalid sfnt table bounds")
        result[data[start:start + 4]] = location, size
    return result


def _faces(path: Path) -> list[OfficeFont]:
    data = path.read_bytes()
    offsets = [0]
    if data[:4] == b"ttcf":
        count = struct.unpack_from(">I", data, 8)[0]
        if count > 256:
            return []
        offsets = list(struct.unpack_from(f">{count}I", data, 12))
    result = []
    for offset in offsets:
        tables = _tables(data, offset)
        if b"name" not in tables:
            continue
        start, length = tables[b"name"]
        block = data[start:start + length]
        count, strings = struct.unpack_from(">HH", block, 2)
        records = []
        for i in range(count):
            platform, encoding, language, kind, size, position = struct.unpack_from(">6H", block, 6 + i * 12)
            if kind not in {1, 2, 4, 6, 16, 17} or platform not in {0, 1, 3}:
                continue
            raw = block[strings + position:strings + position + size]
            text = raw.decode("utf-16-be" if platform in {0, 3} else "mac_roman", errors="replace").strip("\0 ")
            if text:
                records.append((kind, language, text))
        family = next((text for kind, lang, text in records if kind == 1 and lang == 0x409), None)
        family = family or next((text for kind, _, text in records if kind == 1), None)
        if not family:
            continue
        os2 = tables.get(b"OS/2")
        selection = struct.unpack_from(">H", data, os2[0] + 62)[0] if os2 and os2[1] >= 64 else 0
        names = tuple((font_key(text), 2 if kind in {4, 6} else 1 if kind == 1 else 0)
                      for kind, _, text in records if kind in {1, 4, 6, 16})
        result.append(OfficeFont(path, offset, family, bool(selection & 32), bool(selection & 1), names))
    return result


@lru_cache(maxsize=1)
def _font_index() -> dict[str, list[tuple[OfficeFont, int]]]:
    result: dict[str, list[tuple[OfficeFont, int]]] = {}
    for path in _font_paths():
        try:
            for face in _faces(path):
                for name, priority in set(face.names):
                    result.setdefault(name, []).append((face, priority))
        except (OSError, ValueError, struct.error):
            continue  # A corrupt or unsupported local font must not abort a job.
    return result


@lru_cache(maxsize=512)
def resolve_font(name: str, bold: bool = False, italic: bool = False) -> OfficeFont | None:
    key = font_key(name)
    aliases = {"helvetica": "arial", "helveticabold": "arialbold",
               "helveticaoblique": "arialitalic", "helveticaboldoblique": "arialbolditalic",
               "timesroman": "timesnewroman", "timesbold": "timesnewromanbold",
               "timesitalic": "timesnewromanitalic", "timesbolditalic": "timesnewromanbolditalic",
               "courier": "couriernew", "courierbold": "couriernewbold",
               "courieroblique": "couriernewitalic", "courierboldoblique": "couriernewbolditalic"}
    choices = _font_index().get(aliases.get(key, key), [])
    if not choices:
        return None
    return max(choices, key=lambda item: (item[0].bold == bold, item[0].italic == italic, item[1]))[0]


@lru_cache(maxsize=32)
def font_metrics(face: OfficeFont) -> fitz.Font:
    data = face.path.read_bytes()
    if data[:4] == b"ttcf":
        # MuPDF opens the first TTC face by default. Repackage the requested
        # member in memory so measurements agree with the selected Office face.
        tables = _tables(data, face.offset)
        count = len(tables)
        body = bytearray(data[face.offset:face.offset + 12] + b"\0" * (count * 16))
        for i, (tag, (offset, size)) in enumerate(tables.items()):
            body.extend(b"\0" * (-len(body) % 4))
            position = len(body)
            body.extend(data[offset:offset + size])
            struct.pack_into(">4sIII", body, 12 + i * 16, tag, 0, position, size)
        data = bytes(body)
    return fitz.Font(fontbuffer=data)
