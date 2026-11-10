"""Optional local Office renderers, never a cloud conversion service."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
from functools import lru_cache
from pathlib import Path

from magic_converter.core.jobs.job_control import check_cancelled, cancellable_lock, copy_file
from magic_converter.core.jobs.process_runner import run_process

_office_lock = threading.Lock()


@lru_cache(maxsize=1)
def find_libreoffice() -> str | None:
    roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3])) / "vendor" / "libreoffice"]
    roots += [Path(os.environ.get(name, "C:/Program Files")) / "LibreOffice" for name in ("ProgramFiles", "ProgramFiles(x86)")]
    configured = os.environ.get("MAGIC_CONVERTER_LIBREOFFICE", "")
    candidates = ([Path(configured)] if configured else []) + [root / "program" / "soffice.exe" for root in roots]
    return next((str(path) for path in candidates if path.is_file()), None) or shutil.which("soffice")


@lru_cache(maxsize=3)
def find_office(application: str) -> str | None:
    executable = {"PowerPoint": "POWERPNT.EXE", "Word": "WINWORD.EXE", "Excel": "EXCEL.EXE"}[application]
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{executable}") as key:
                    path = Path(winreg.QueryValue(key, None).strip('"'))
                    if path.is_file():
                        return str(path)
            except OSError:
                continue
    for root_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = Path(os.environ.get(root_name, "C:/Program Files")) / "Microsoft Office"
        for relative in (f"root/Office16/{executable}", f"Office16/{executable}", f"Office15/{executable}"):
            path = root / relative
            if path.is_file():
                return str(path)
    return None


def presentation_available() -> bool:
    return bool(find_libreoffice() or find_office("PowerPoint"))


def office_pdf(source: Path, destination: Path, application: str = "PowerPoint") -> None:
    from magic_converter.core.conversion.converter import ConversionError
    check_cancelled()
    libreoffice = find_libreoffice()
    if libreoffice:
        with tempfile.TemporaryDirectory(prefix="magic-office-") as directory:
            root = Path(directory)
            command = [libreoffice, f"-env:UserInstallation={(root / 'profile').as_uri()}",
                       "--headless", "--convert-to", "pdf", "--outdir", str(root), str(source)]
            result = run_process(command, timeout=180)
            generated = root / f"{source.stem}.pdf"
            if result.returncode or not generated.is_file():
                raise ConversionError("LibreOffice PDF 변환에 실패했습니다: " + result.stderr[-500:])
            copy_file(generated, destination)
            return
    if not find_office(application):
        raise ConversionError(f"{application} 또는 LibreOffice가 설치되어 있어야 합니다.")
    # Use the same bounded, identity-tracked automation helper for legacy PPT
    # as for DOCX/PPTX/XLSX; a blocked dialog cannot strand a PowerShell server.
    from magic_converter.core.engines.hwp_converter import run_com
    with cancellable_lock(_office_lock):
        run_com(source, destination, "office_pdf")
