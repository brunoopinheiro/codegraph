"""List repository files through git and detect which ones changed."""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config, matches_any
from .languages import adapter_for


@dataclass
class FileInfo:
    path: str  # posix, relative to root
    mtime: float
    size: int
    hash: str = ""


def git_files(root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files (respects .gitignore)."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, check=True,
    ).stdout
    seen, files = set(), []
    for raw in out.split(b"\0"):
        if raw and raw not in seen:
            seen.add(raw)
            files.append(raw.decode("utf-8"))
    return files


def source_files(all_files: list[str], cfg: Config, root: Path) -> list[str]:
    result = []
    for p in all_files:
        if adapter_for(p) is None:
            continue
        if cfg.include and not matches_any(p, cfg.include):
            continue
        if cfg.exclude and matches_any(p, cfg.exclude):
            continue
        if os.path.isfile(root / p):  # deleted-but-tracked files are skipped
            result.append(p)
    return result


def file_hash(root: Path, path: str) -> str:
    return hashlib.sha1((root / path).read_bytes()).hexdigest()


def diff(root: Path, paths: list[str], known: dict[str, tuple[float, int, str]]):
    """Return (changed FileInfo list, unchanged FileInfo list, deleted paths).

    `known` maps path -> (mtime, size, hash) from the database. The hash is only
    recomputed when mtime or size moved, which keeps no-op runs cheap.
    """
    changed, same = [], []
    for p in paths:
        st = os.stat(root / p)
        info = FileInfo(p, st.st_mtime, st.st_size)
        old = known.get(p)
        if old and old[0] == info.mtime and old[1] == info.size:
            info.hash = old[2]
            same.append(info)
            continue
        info.hash = file_hash(root, p)
        (same if old and old[2] == info.hash else changed).append(info)
    deleted = sorted(set(known) - set(paths))
    return changed, same, deleted
