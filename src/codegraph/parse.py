"""Extract facts (symbols, imports, exports, refs) from files through their language adapter."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from .languages import adapter_for
from .languages.base import FileFacts, Unresolved

PARALLEL_THRESHOLD = 64


def parse_file(root: str, path: str) -> tuple[str, FileFacts]:
    adapter = adapter_for(path)
    try:
        source = (Path(root) / path).read_bytes()
        return path, adapter.parse(path, source)
    except Exception as exc:  # a broken file must not stop the whole index
        return path, FileFacts(unresolved=[Unresolved("parse", 0, "parse-error", repr(exc)[:120])])


def _batch(args: tuple[str, list[str]]) -> list[tuple[str, FileFacts]]:
    root, paths = args
    return [parse_file(root, p) for p in paths]


def parse_files(root: Path, paths: list[str]) -> dict[str, FileFacts]:
    if len(paths) < PARALLEL_THRESHOLD:
        return dict(parse_file(str(root), p) for p in paths)
    workers = max(1, min(os.cpu_count() or 1, 8))
    chunks = [paths[i::workers * 4] for i in range(workers * 4)]
    out: dict[str, FileFacts] = {}
    with ProcessPoolExecutor(workers) as pool:
        for result in pool.map(_batch, [(str(root), c) for c in chunks if c]):
            out.update(result)
    return out
