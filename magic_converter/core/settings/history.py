"""Local bounded result history. Clearing history never removes converted files."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager

from magic_converter.core.settings.json_store import read_json, write_json


class HistoryStore:
    def __init__(self, path: Path, limit: int = 200):
        self.path, self.limit = path, limit
        self.entries: list[dict] = []
        self.notice = ""
        self.read_only = False
        if path.exists():
            try:
                data = read_json(path, 16 * 1024 * 1024)
                if isinstance(data, dict) and type(data.get("version")) is int and data["version"] > 1:
                    self.read_only = True
                    self.notice = "최신 버전 결과 기록은 보존하며 새 기록은 저장하지 않습니다."
                    return
                if (not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), list)
                        or len(data["entries"]) > 1000):
                    raise ValueError("잘못된 기록 구조")
                for entry in data["entries"]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("files"), list):
                        raise ValueError("잘못된 파일 기록")
                    if (any(not isinstance(entry.get(key), str) for key in
                            ("id", "started_at", "finished_at", "action_key", "label", "preset_id"))
                            or not isinstance(entry.get("options"), dict) or not isinstance(entry.get("output"), dict)):
                        raise ValueError("잘못된 작업 기록")
                    for file in entry["files"]:
                        if (not isinstance(file, dict) or not isinstance(file.get("source"), str)
                                or file.get("state") not in {"done", "failed", "cancelled", "timed_out"}
                                or any(not isinstance(file.get(key), list) or
                                       not all(isinstance(item, str) for item in file[key])
                                       for key in ("outputs", "errors", "warnings"))):
                            raise ValueError("잘못된 파일 결과")
                self.entries = data["entries"][-1000:]
            except (OSError, ValueError):
                self.notice = "결과 기록 파일을 읽지 못했습니다. 기존 파일을 보존했습니다."
                # Keep corrupt data for inspection before a future append replaces it.
                import shutil
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                try:
                    shutil.copy2(path, path.with_name(path.name + ".corrupt-" + stamp))
                except OSError:
                    self.read_only = True
                    self.notice += " 원본 보존을 위해 새 기록 저장을 중지했습니다."

    def append(self, result) -> None:
        if self.limit == 0 or self.read_only:
            return
        with self._lock():
            if self.path.exists():
                latest = HistoryStore(self.path, self.limit)
                if latest.read_only:
                    self.read_only, self.notice = True, latest.notice
                    return
                self.entries = latest.entries
            self._append(result)

    @contextmanager
    def _lock(self):
        from PySide6.QtCore import QLockFile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.path) + ".lock")
        lock.setStaleLockTime(0)
        if not lock.tryLock(1000):
            raise OSError("다른 프로세스가 결과 기록을 저장 중입니다.")
        try:
            yield
        finally:
            lock.unlock()

    def _append(self, result) -> None:
        if self.limit == 0 or self.read_only:
            return
        from dataclasses import asdict
        request = result.request
        entry = {"id": uuid4().hex, "started_at": result.started_at,
                 "finished_at": datetime.now(timezone.utc).isoformat(),
                 "elapsed_seconds": round(result.elapsed_seconds, 3),
                 "action_key": result.action.key, "label": result.action.label,
                 "preset_id": request.preset_id if request else "",
                 "options": asdict(request.options) if request else {},
                 "tools": asdict(request.tools) if request else {},
                 "output": asdict(request.output) if request else {},
                 "files": [{"source": str(file.source), "state": file.state,
                            "outputs": [str(path) for path in file.outputs],
                            "errors": list(file.errors), "warnings": list(file.warnings)} for file in result.files]}
        entries = [*self.entries, entry][-self.limit:]
        # A huge single batch must not make the next startup exceed the read cap.
        import json
        while entries and len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > 12 * 1024 * 1024:
            entries.pop(0)
        write_json(self.path, {"version": 1, "entries": entries})
        self.entries = entries

    def clear(self):
        with self._lock():
            if self.read_only:
                raise ValueError("읽기 전용 결과 기록은 보존합니다.")
            write_json(self.path, {"version": 1, "entries": []})
            self.entries = []
