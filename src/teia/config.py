"""Optional teia.toml at the target repo root."""
from __future__ import annotations

import difflib
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
    present: bool = False  # a teia.toml exists
    explicit: set[str] = field(default_factory=set)  # keys set in the toml
    warnings: list[str] = field(default_factory=list)  # unknown keys, wrong types


LIST_KEYS = ("include", "exclude", "registry", "entrypoints", "tests", "extensions", "source_roots")
KNOWN_KEYS = (*LIST_KEYS, "aliases")


def load_config(root: Path) -> Config:
    path = root / "teia.toml"
    if not path.is_file():
        return Config()
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    cfg = Config(digest=hashlib.sha1(raw).hexdigest(), present=True)
    for key in data:
        if key not in KNOWN_KEYS:
            close = difflib.get_close_matches(key, KNOWN_KEYS, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else ""
            cfg.warnings.append(f"teia.toml: unknown key {key!r}{hint}; it is ignored. "
                                f"Supported keys: {', '.join(KNOWN_KEYS)}")
    aliases = data.get("aliases", {})
    if isinstance(aliases, dict):
        for k, v in aliases.items():
            if isinstance(v, str):
                cfg.aliases[k] = [v]
            elif isinstance(v, list) and all(isinstance(x, str) for x in v):
                cfg.aliases[k] = list(v)
            else:
                cfg.warnings.append(f"teia.toml: [aliases] {k!r} must be a string or a list of strings; ignored")
        if "aliases" in data:
            cfg.explicit.add("aliases")
    else:
        cfg.warnings.append("teia.toml: 'aliases' must be a table ([aliases]); ignored")
    for key in LIST_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, str):
            cfg.warnings.append(f"teia.toml: {key!r} should be a list of strings; treating the string as "
                                f"one entry")
            value = [value]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            cfg.warnings.append(f"teia.toml: {key!r} must be a list of strings; ignored")
            continue
        setattr(cfg, key, list(value))
        cfg.explicit.add(key)
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
