"""Bounded JSON reads and same-directory atomic publication."""

import json
import os
from pathlib import Path
import tempfile


def read_json(path: Path, limit: int = 2 * 1024 * 1024):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("JSON 파일 크기가 제한을 초과했습니다.")
    return json.loads(raw.decode("utf-8-sig"))


def write_json(path: Path, data) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".magic-json-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
