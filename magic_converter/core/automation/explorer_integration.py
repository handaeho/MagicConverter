"""Explicit per-user classic Explorer menu; no elevation or shell DLL required."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

KEY = r"Software\Classes\*\shell\MagicConverter"


def command(executable=None):
    if executable is None and (launcher := os.environ.get("MAGIC_CONVERTER_PUBLIC_GUI")):
        executable = Path(launcher)
        if not executable.is_absolute() or not executable.is_file():
            raise OSError("탐색기 메뉴를 등록하려면 같은 폴더에 magic_converter.exe가 필요합니다.")
    executable = Path(executable or sys.executable).resolve()
    if getattr(sys, "frozen", False) and executable.stem.endswith("-cli"):
        sibling = executable.with_name("magic_converter.exe")
        if sibling.is_file():
            executable = sibling
    args = [str(executable)]
    if not getattr(sys, "frozen", False) and executable == Path(sys.executable).resolve():
        args += [str(Path(__file__).resolve().parents[3] / "run_magic_converter.py")]
    return subprocess.list2cmdline(args) + ' --open "%1"'


def install(executable=None):
    if sys.platform != "win32":
        raise OSError("탐색기 메뉴는 Windows에서 지원합니다.")
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "MagicConverter로 변환…")
        winreg.SetValueEx(key, "MultiSelectModel", 0, winreg.REG_SZ, "Document")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, KEY + r"\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command(executable))


def remove():
    import winreg
    # Only these two exact app-owned keys are removed.
    for key in (KEY + r"\command", KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def installed_command():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY + r"\command") as key:
            return winreg.QueryValueEx(key, "")[0]
    except FileNotFoundError:
        return ""
