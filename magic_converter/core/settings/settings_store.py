"""Migrate legacy preferences once, keep a last-good backup and recover corruption."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import shutil

from magic_converter.core.settings.json_store import read_json, write_json
from magic_converter.core.settings.preferences import Preferences, SettingsError, SCHEMA_VERSION, default_preferences


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path
        self.backup = path.with_suffix(path.suffix + ".bak")
        self.read_only = False
        self.notice = ""

    def load(self, legacy: dict | None = None) -> Preferences:
        if not self.path.exists():
            if self.backup.is_file():
                try:
                    settings = self.read_backup()
                    write_json(self.path, settings.to_dict())
                    self.notice = "설정 파일이 없어 마지막 정상 백업으로 복구했습니다."
                    return settings
                except (OSError, ValueError):
                    pass
            settings = default_preferences()
            for key, value in (legacy or {}).items():
                if key not in settings.base_values():
                    continue
                try:
                    settings = replace(settings, **{key: value})
                except SettingsError:
                    self.notice = "이전 설정의 잘못된 값은 기본값으로 복구했습니다."
            self.save(settings)
            return settings
        try:
            data = read_json(self.path)
            if isinstance(data, dict) and type(data.get("version")) is int and data["version"] > SCHEMA_VERSION:
                self.read_only = True
                self.notice = "더 최신 버전의 설정입니다. 기본값으로 실행하며 기존 파일은 보존합니다."
                return default_preferences()
            preferences = Preferences.from_dict(data)
            if data.get("version") != SCHEMA_VERSION:
                try:
                    self.save(preferences)
                    self.notice = "이전 설정을 새 버전으로 이전했습니다."
                except OSError:
                    self.read_only = True
                    self.notice = "설정 버전 이전을 저장하지 못했습니다. 기존 설정을 읽기 전용으로 사용합니다."
            return preferences
        except (OSError, ValueError):
            try:
                settings = self.read_backup()
                self.notice = "설정 파일 손상을 감지해 마지막 정상 백업으로 복구했습니다."
            except (OSError, ValueError):
                settings = default_preferences()
                self.notice = "설정 파일을 읽지 못해 기본값으로 복구했습니다."
            if self.path.is_file():
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                shutil.copy2(self.path, self.path.with_name(self.path.name + ".corrupt-" + stamp))
            write_json(self.path, settings.to_dict())
            return settings

    def read_backup(self) -> Preferences:
        return Preferences.from_dict(read_json(self.backup))

    def save(self, preferences: Preferences) -> None:
        if self.read_only:
            raise SettingsError("설정 파일을 보존하는 읽기 전용 상태입니다. 파일 버전과 쓰기 권한을 확인하세요.")
        if self.path.is_file():
            try:
                previous = Preferences.from_dict(read_json(self.path))
            except (OSError, ValueError):
                previous = None
            if previous is not None:
                write_json(self.backup, previous.to_dict())
        write_json(self.path, preferences.to_dict())
