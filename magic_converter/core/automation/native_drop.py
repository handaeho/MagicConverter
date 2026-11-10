"""Bounded snapshots for providers that remove temporary paths after Drop."""
from pathlib import Path
import stat
import tempfile

from magic_converter.core.automation.web_inputs import WebInput


def has_temporary(paths, *, temp_root=None):
    root = Path(temp_root or tempfile.gettempdir()).resolve()
    return any(path.resolve().is_relative_to(root) for path in paths)


def snapshot_temporary(paths, *, temp_root=None, max_bytes=32 * 1024**2):
    """Read only temporary files, after acceptance and before returning to OLE.

    This is not source authentication. Immutable bytes use the same limits and
    worker/publication path as native FileContents, without a source-side move.
    """
    root = Path(temp_root or tempfile.gettempdir()).resolve()
    resolved = tuple(path.resolve() for path in paths)
    temporary = [path.is_relative_to(root) for path in resolved]
    if not any(temporary):
        return paths, ()
    if not all(temporary) or not 1 <= len(paths) <= 100:
        raise ValueError('임시 드래그 파일만 한 번에 100개까지 선택하세요.')
    sizes = []
    for path in resolved:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise ValueError('임시 드래그 입력은 비어 있지 않은 파일이어야 합니다.')
        sizes.append(info.st_size)
    if sum(sizes) > max_bytes:
        raise ValueError('임시 드래그 파일은 합계 32 MiB까지 지원합니다.')
    items = []
    for path, size in zip(resolved, sizes):
        with path.open('rb') as stream:
            data = stream.read(size + 1)
        if len(data) != size:
            raise ValueError('드래그 도중 임시 파일 크기가 변경되었습니다.')
        items.append(WebInput(path.name, data=data))
    items = tuple(items)
    return tuple(item.placeholder for item in items), items
