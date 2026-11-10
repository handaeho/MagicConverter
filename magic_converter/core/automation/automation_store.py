"""Watch recipes and durable receipts, separate from UI preferences."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from magic_converter.core.settings.json_store import read_json, write_json
from magic_converter.core.settings.preferences import EncoderOptions, OutputRule, Preset, ToolOptions


def recipe_from_dict(data):
    return Preset(data["id"], data["name"], data["action_key"], EncoderOptions(**data.get("options", {})),
                  OutputRule(**data["output"]) if data.get("output") else None, ToolOptions(**data.get("tools", {})))


@dataclass(frozen=True)
class WatchRule:
    id: str
    folder: str
    output: str
    recipe: Preset
    recursive: bool = False
    include: str = "*"
    enabled: bool = True
    settle: float = 2.0
    target: str = ""  # CLI --to: choose the route from each settled file.

    def __post_init__(self):
        import re
        from magic_converter.core.actions.tool_definitions import COMBINED_ACTIONS
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id):
            raise ValueError("감시 규칙 ID가 올바르지 않습니다.")
        if not Path(self.folder).is_absolute() or not Path(self.output).is_absolute():
            raise ValueError("감시·출력 폴더는 절대 경로여야 합니다.")
        root, output = Path(self.folder).resolve(), Path(self.output).resolve()
        if root == output or root.is_relative_to(output):
            raise ValueError("감시 출력은 별도 하위 폴더 또는 감시 폴더 밖에 지정하세요.")
        if self.recipe.action_key in {*COMBINED_ACTIONS, "copy_path", "terminal"}:
            raise ValueError("감시 규칙에는 파일별 변환 작업을 지정하세요.")
        if self.target:
            from magic_converter.core.conversion.formats import FORMATS
            if not isinstance(self.target, str) or self.target not in {f.id for f in FORMATS.known if f.writable}:
                raise ValueError("감시 출력 형식이 올바르지 않습니다.")
        if type(self.recursive) is not bool or type(self.enabled) is not bool or not .1 <= self.settle <= 300:
            raise ValueError("감시 안정화 시간은 0.1~300초여야 합니다.")
        if not isinstance(self.include, str) or len(self.include) > 1000 or not self.include.strip():
            raise ValueError("포함 패턴을 지정하세요 (예: *.png;*.jpg).")

    @property
    def signature(self):
        data = asdict(self)
        if not self.target:
            data.pop("target")  # Preserve stage-6 receipts for existing recipes.
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_dict(cls, data):
        return cls(**{**data, "recipe": recipe_from_dict(data["recipe"])})


class AutomationStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / "automation.json"
        self.rules = ()
        self.web_output = ""
        if self.path.exists():
            data = read_json(self.path)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("rules"), list) or len(data["rules"]) > 100:
                raise ValueError("자동화 설정 버전을 읽을 수 없습니다. 기존 파일을 보존했습니다.")
            self.rules = tuple(WatchRule.from_dict(item) for item in data["rules"])
            self.web_output = data.get("web_output", "")
        self._validate(self.rules, self.web_output)

    @staticmethod
    def _validate(rules, web_output):
        if len(rules) > 100 or len({rule.id for rule in rules}) != len(rules):
            raise ValueError("감시 규칙이 중복되거나 100개를 초과했습니다.")
        if not isinstance(web_output, str) or len(web_output) > 2000 or web_output and not Path(web_output).is_absolute():
            raise ValueError("웹 출력 폴더는 절대 경로여야 합니다.")

    def save(self, rules, web_output):
        self._validate(rules, web_output)
        if self.path.exists():
            current = read_json(self.path)
            if not isinstance(current, dict) or current.get("version") != 1:
                raise ValueError("읽을 수 없는 자동화 설정은 덮어쓰지 않습니다.")
        write_json(self.path, {"version": 1, "rules": [asdict(rule) for rule in rules], "web_output": web_output})
        self.rules, self.web_output = tuple(rules), web_output


class WatchJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise ValueError("최신 버전 감시 기록은 보존합니다.")
            db.execute("CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, rule TEXT NOT NULL, source TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, work TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', updated REAL NOT NULL)")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def rows(self, states=None):
        with self.connect() as db:
            if states is None:
                rows = db.execute("SELECT * FROM events ORDER BY updated DESC LIMIT 10000").fetchall()
            else:
                values = tuple(states)
                rows = db.execute("SELECT * FROM events WHERE state IN (" + ",".join("?" for _ in values) + ") ORDER BY updated DESC LIMIT 10000", values).fetchall()
            return [dict(row) for row in rows]

    def claim(self, key, rule, source, digest, work, target):
        import time
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO events (key,rule,source,digest,state,work,target,updated) VALUES (?,?,?,?,'queued',?,?,?)",
                                (key, rule.id, str(source), digest, str(work), str(target), time.time()))
            return cursor.rowcount == 1

    def update(self, key, state, detail=""):
        import time
        with self.connect() as db:
            db.execute("UPDATE events SET state=?,detail=?,updated=? WHERE key=?", (state, str(detail)[:4000], time.time(), key))

    def retry(self, key):
        with self.connect() as db:
            db.execute("DELETE FROM events WHERE key=? AND state IN ('failed','cancelled','interrupted')", (key,))
