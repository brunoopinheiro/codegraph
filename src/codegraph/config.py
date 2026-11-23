"""Optional codegraph.toml at the target repo root."""
from __future__ import annotations

import functools
import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TESTS = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/__mocks__/**", "test/**", "tests/**", "e2e/**",
                 "**/test_*.py", "**/*_test.py", "**/conftest.py"]


@dataclass
class Config:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    registry: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=lambda: list(DEFAULT_TESTS))
    aliases: dict[str, list[str]] = field(default_factory=dict)
    extensions: list[str] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)  # Python import roots (default: ".", "src", "lib")
    digest: str = ""


def load_config(root: Path) -> Config:
    path = root / "codegraph.toml"
    if not path.is_file():
        return Config()
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    aliases = {k: [v] if isinstance(v, str) else list(v) for k, v in data.get("aliases", {}).items()}
    cfg = Config(aliases=aliases, digest=hashlib.sha1(raw).hexdigest())
    for key in ("include", "exclude", "registry", "entrypoints", "tests", "extensions", "source_roots"):
        if key in data:
            setattr(cfg, key, list(data[key]))
    return cfg


_GLOB_TOKENS = re.compile(r"\*\*/|\*\*|\*|\?|[^*?]+")
_GLOB_REGEX = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}


@functools.cache
def _glob_regex(pattern: str) -> re.Pattern:
    """`**/` matches zero or more directories; `*` and `?` stay inside one path segment."""
    parts = [_GLOB_REGEX.get(tok) or re.escape(tok) for tok in _GLOB_TOKENS.findall(pattern)]
    return re.compile("".join(parts) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(path))


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)
