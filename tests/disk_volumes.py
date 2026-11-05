"""A fake disk layout for disk-space preflight tests."""

import os
import shutil
from pathlib import Path

import pytest

GIB = 1024**3


class Volumes:
    """The OS boundary of a disk preflight: folders on their own fake volumes
    with a set number of free GiB, and inputs that look bigger than they are."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.free: dict[Path, float] = {}
        self.sizes: dict[Path, int] = {}
        real_stat, real_usage = Path.stat, shutil.disk_usage

        def stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            result = real_stat(path, follow_symlinks=follow_symlinks)
            volume, size = self._volume(path), self.sizes.get(Path(path).absolute())
            if volume is None and size is None:
                return result
            fields = list(result)
            if volume is not None:
                fields[2] = result.st_dev + 1 + list(self.free).index(volume)
            if size is not None:
                fields[6] = size
            extra = ("st_atime", "st_mtime", "st_ctime", "st_mtime_ns")
            return os.stat_result(fields, {k: getattr(result, k) for k in extra})

        def disk_usage(path):
            volume = self._volume(path)
            if volume is None:
                return real_usage(path)
            free = int(self.free[volume] * GIB)
            return type(real_usage(volume))(free + GIB, GIB, free)

        monkeypatch.setattr(Path, "stat", stat)
        monkeypatch.setattr(shutil, "disk_usage", disk_usage)

    def _volume(self, path) -> Path | None:
        absolute = Path(os.path.abspath(path))
        return next((v for v in self.free if absolute.is_relative_to(v)), None)
