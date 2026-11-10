"""Rescan-based watching with stable inputs and atomic result-folder receipts.

Polling runs off the UI thread; it cannot lose a FileSystemWatcher overflow.
Public results live in a deterministic event folder. A crash after rename but
before the journal commit is recovered from the receipt, without another copy.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
from pathlib import Path

from magic_converter.core.automation.automation_store import WatchJournal, WatchRule
from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.jobs.job_control import check_cancelled, copy_file, copy_tree, JobCancelled, JobTimedOut
from magic_converter.core.settings.json_store import read_json, write_json

TEMP_EXTENSIONS = {".tmp", ".temp", ".part", ".partial", ".crdownload", ".download", ".opdownload", ".filepart"}


def fingerprint(path: Path):
    stat = path.stat()
    if not path.is_file() or path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
        raise OSError("링크·재분석 지점은 감시하지 않습니다.")
    return stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino


def digest_file(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancelled()
            digest.update(chunk)
    return digest.hexdigest()


def can_open(path):
    if os.name != "nt":
        return True
    import win32con
    import win32file
    try:
        handle = win32file.CreateFile(str(path), win32con.GENERIC_READ, 0, None, win32con.OPEN_EXISTING, 0, None)
        handle.Close()
        return True
    except Exception:
        return False


class WatchEngine:
    def __init__(self, directory: Path, rules=(), *, rehash_interval=5.0):
        if not 0 < rehash_interval < float("inf"):
            raise ValueError("감시 내용 재검사 간격은 유한한 양수여야 합니다.")
        self.rehash_interval = rehash_interval
        self.directory = Path(directory).resolve()
        self.work_root = self.directory / "watch-work"
        self.journal = WatchJournal(self.directory / "watch-journal.sqlite3")
        self.rules = tuple(rules)
        self.pending = {}
        self.busy_rules = set()
        self.quiescent = False

    def ignored(self, path, rule):
        root = Path(rule.folder).resolve()
        if (path.name.startswith((".", "~$")) or path.name.lower() in {"desktop.ini", "thumbs.db"}
                or path.suffix.lower() in TEMP_EXTENSIONS or not FORMATS.detect(path)):
            return True
        try:
            full = path.resolve()
            if not full.is_relative_to(root) or path.is_symlink():
                return True
            if any(full.is_relative_to(Path(r.output).resolve()) for r in self.rules) or full.is_relative_to(self.work_root):
                return True
            if not rule.recursive and full.parent != root:
                return True
            return not any(fnmatch.fnmatchcase(path.name.lower(), pattern.strip().lower()) for pattern in rule.include.split(";"))
        except OSError:
            return True

    def scan(self, now=None):
        self.quiescent = False
        now = time.monotonic() if now is None else now
        ready, seen = [], set()
        waiting = False
        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.id in self.busy_rules:
                waiting = True
                continue
            root = Path(rule.folder)
            if not root.is_dir() or root.is_symlink():
                continue
            for current, dirs, files in os.walk(root, followlinks=False):
                check_cancelled()
                dirs[:] = [name for name in dirs if not name.startswith(".") and not (Path(current) / name).is_symlink()
                           and not getattr((Path(current) / name).stat(), "st_file_attributes", 0) & 0x400]
                for name in sorted(files):
                    path = Path(current) / name
                    if self.ignored(path, rule):
                        continue
                    candidate = (rule.id, str(path.absolute()).casefold())
                    seen.add(candidate)
                    try:
                        signature = fingerprint(path)
                        previous = self.pending.get(candidate)
                        if previous is None or previous[0] != signature:
                            self.pending[candidate] = signature, now, "", now, False
                            waiting = True
                            continue
                        if now - previous[1] < rule.settle or not can_open(path):
                            waiting = True
                            continue
                        # Some sync/copy tools retain both size and mtime.
                        # Revalidate content periodically, rather than caching
                        # its digest until process restart or hashing every poll.
                        rehash = not previous[2] or previous[4] or now - previous[3] >= self.rehash_interval
                        digest = digest_file(path) if rehash else previous[2]
                        if fingerprint(path) != signature:
                            self.pending.pop(candidate, None)
                            waiting = True
                            continue
                        if previous[2] and digest != previous[2]:
                            self.pending[candidate] = signature, now, digest, now, True
                            waiting = True
                            continue
                        self.pending[candidate] = signature, previous[1], digest, now if rehash else previous[3], False
                        key = hashlib.sha256((rule.signature + "\0" + candidate[1] + "\0" + digest).encode()).hexdigest()
                        work = self.work_root / key
                        # Event directories make folder and multi-page outputs atomic too.
                        target = Path(rule.output) / (path.stem[:80] + "_" + key[:16])
                        if self.journal.claim(key, rule, path, digest, work, target):
                            if work.exists():
                                if work.resolve().parent != self.work_root.resolve() or work.is_symlink():
                                    raise ValueError("감시 작업 폴더가 올바르지 않습니다.")
                                shutil.rmtree(work)
                            (work / "results").mkdir(parents=True)
                            self.busy_rules.add(rule.id)
                            ready.append((rule, self.journal.get(key)))
                            break
                    except OSError:
                        waiting = True
                        continue
                if ready and ready[-1][0].id == rule.id or not rule.recursive:
                    break
        self.pending = {k: v for k, v in self.pending.items() if k in seen or k[0] in self.busy_rules}
        self.quiescent = not waiting and not ready and not self.busy_rules
        return ready

    def receipt_valid(self, folder, key):
        try:
            receipt = read_json(folder / ".magic-receipt.json")
            if receipt.get("key") != key or not isinstance(receipt.get("files"), list) or not (receipt["files"] or receipt.get("directories")):
                return False
            for item in receipt["files"]:
                path = (folder / item["path"]).resolve()
                if not path.is_relative_to(folder.resolve()) or path.is_symlink() or digest_file(path) != item["sha256"]:
                    return False
            for item in receipt.get("directories", []):
                path = (folder / item).resolve()
                if not path.is_relative_to(folder.resolve()) or not path.is_dir() or path.is_symlink():
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def finish(self, event, successful, outputs=(), detail=""):
        key, work, target = event["key"], Path(event["work"]), Path(event["target"])
        try:
            if not successful:
                self.journal.update(key, detail if detail in {"cancelled", "interrupted"} else "failed", detail)
                return None
            if digest_file(Path(event["source"])) != event["digest"]:
                self.journal.update(key, "failed", "변환 중 원본이 변경되었습니다. 새 버전을 다시 감시합니다.")
                self.pending.clear()
                return None
            files = []
            for path in sorted((work / "results").rglob("*")):
                if path.is_file():
                    files.append({"path": str(path.relative_to(work / "results")), "sha256": digest_file(path)})
            directories = [str(path.relative_to(work / "results")) for path in (work / "results").rglob("*") if path.is_dir()]
            if not (files or directories) or not outputs or any(not Path(p).exists() for p in outputs):
                raise ValueError("감시 변환의 결과 파일을 확인하지 못했습니다.")
            write_json(work / "results" / ".magic-receipt.json", {"key": key, "files": files, "directories": directories})
            self.journal.update(key, "publishing")
            return self.publish(event)
        except JobCancelled:
            self.journal.update(key, "cancelled", "감시 결과 게시가 취소되었습니다.")
            raise
        except JobTimedOut as exc:
            self.journal.update(key, "failed", str(exc))
            raise
        finally:
            self.busy_rules.discard(event["rule"])

    def publish(self, event):
        target, results = Path(event["target"]), Path(event["work"]) / "results"
        if target.exists():
            if not self.receipt_valid(target, event["key"]):
                raise ValueError(f"감시 결과 이름이 기존 파일과 충돌합니다: {target}")
        else:
            # Build beside the final directory, including on a different volume.
            target.parent.mkdir(parents=True, exist_ok=True)
            stage = target.with_name(".magic-watch-" + event["key"])
            if stage.exists():
                self.remove_owned_stage(stage, target.parent)
            try:
                copy_tree(results, stage)
                if not self.receipt_valid(stage, event["key"]):
                    raise ValueError("감시 결과의 게시 전 검증에 실패했습니다.")
                check_cancelled()
                stage.rename(target)
            except BaseException:
                if stage.exists():
                    self.remove_owned_stage(stage, target.parent)
                raise
        self.journal.update(event["key"], "done")
        return target

    @staticmethod
    def remove_owned_stage(path, root):
        full, base = path.resolve(), root.resolve()
        if full.parent != base or not full.name.startswith(".magic-watch-") or path.is_symlink():
            raise ValueError("소유하지 않은 감시 임시 폴더입니다.")
        shutil.rmtree(full)

    def recover(self):
        for event in self.journal.rows({"queued", "publishing"}):
            check_cancelled()
            target, results = Path(event["target"]), Path(event["work"]) / "results"
            if self.receipt_valid(target, event["key"]):
                self.journal.update(event["key"], "done")
                self.cleanup(event)
            elif event["state"] == "publishing" and self.receipt_valid(results, event["key"]):
                try:
                    self.publish(event)
                    self.cleanup(event)
                except (OSError, ValueError) as exc:
                    self.journal.update(event["key"], "failed", str(exc))
            else:
                # No public output was committed; a fresh attempt is safe.
                self.journal.update(event["key"], "interrupted", "이전 실행이 중단되어 다시 대기합니다.")
                self.cleanup(event)
                self.journal.retry(event["key"])

    def cleanup(self, event):
        work = Path(event["work"])
        if work.exists():
            if work.resolve().parent != self.work_root.resolve() or work.is_symlink():
                raise ValueError("소유하지 않은 감시 작업 폴더입니다.")
            shutil.rmtree(work)
