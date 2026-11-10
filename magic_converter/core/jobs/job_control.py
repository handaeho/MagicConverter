"""Cooperative cancellation and stage messages across converter modules."""

from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Iterable, Iterator, TypeVar
import time


class JobCancelled(Exception):
    pass


class JobTimedOut(TimeoutError):
    pass


@dataclass
class Control:
    event: Event
    stage: Callable[[str], None]
    deadline: float | None = None


current: ContextVar[Control | None] = ContextVar("magic_converter_job", default=None)
DEFAULT_API_TIMEOUT = 3600


@contextmanager
def supervised_api():
    """Give direct registered encoders the same finite default as job entry."""
    token = None
    if current.get() is None:
        token = current.set(Control(Event(), lambda _: None, time.monotonic() + DEFAULT_API_TIMEOUT))
    try:
        yield
    finally:
        if token is not None:
            current.reset(token)


def check_cancelled() -> None:
    control = current.get()
    if control and control.event.is_set():
        raise JobCancelled()
    if control and control.deadline is not None and time.monotonic() >= control.deadline:
        raise JobTimedOut("파일 처리 제한 시간이 초과되었습니다.")


def report_stage(message: str) -> None:
    check_cancelled()
    control = current.get()
    if control:
        control.stage(message)


T = TypeVar("T")


def checked(items: Iterable[T]) -> Iterator[T]:
    for item in items:
        check_cancelled()
        yield item


@contextmanager
def cancellable_lock(lock):
    while not lock.acquire(timeout=0.1):
        check_cancelled()
    try:
        check_cancelled()
        yield
    finally:
        lock.release()


class CancellableRLock:
    """A reentrant renderer lock whose wait observes the file's control."""
    def __init__(self) -> None:
        self._lock = RLock()

    def __enter__(self):
        while not self._lock.acquire(timeout=0.1):
            check_cancelled()
        try:
            check_cancelled()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, *_):
        self._lock.release()


def copy_stream(source, destination, *, chunk_size: int = 1024 * 1024) -> None:
    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    while True:
        check_cancelled()
        chunk = source.read(chunk_size)
        if not chunk:
            break
        destination.write(chunk)
    check_cancelled()


def copy_file(source: Path, destination: Path) -> None:
    check_cancelled()
    with source.open("rb") as incoming, destination.open("wb") as outgoing:
        copy_stream(incoming, outgoing)


def copy_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for member in checked(source.rglob("*")):
        if member.is_symlink():
            raise ValueError("변환 결과 폴더의 심볼릭 링크는 복사하지 않습니다.")
        target = destination / member.relative_to(source)
        if member.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_file(member, target)


class CheckedReader:
    def __init__(self, stream) -> None:
        self.stream = stream

    def read(self, size=-1):
        check_cancelled()
        return self.stream.read(size)
