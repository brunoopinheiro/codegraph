"""Language-neutral facts extracted from one file, and the adapter interface."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class Symbol:
    name: str
    kind: str  # function | class | type | interface | enum | const | component | variable
    line: int
    end_line: int
    exported: bool = False


@dataclass
class Import:
    spec: str
    kind: str  # static | type | dynamic | mock | require
    line: int
    imported: str | None  # exported name in the target; "*" = whole module; None = side effect
    local: str | None


@dataclass
class Export:
    name: str  # name visible to importers ("*" for `export * from`)
    kind: str  # local | reexport | star | type
    line: int
    local: str | None = None  # local binding (local) or imported name (reexport)
    spec: str | None = None


@dataclass
class Ref:
    name: str  # identifier, or "ns.member" for namespace member access
    kind: str  # call | jsx | use | type
    line: int


@dataclass
class Unresolved:
    kind: str
    line: int
    reason: str
    detail: str = ""


@dataclass
class FileFacts:
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)


class Resolver(Protocol):
    def resolve(self, spec: str, from_file: str) -> tuple[str, str]:
        """Return (status, value): ("internal", path) | ("external", package) | ("unresolved", reason)."""


class LanguageAdapter(Protocol):
    name: str
    extensions: tuple[str, ...]
    # True when exporting is a deliberate act (`export` in TS). False when every public
    # top-level name is importable (Python): then a name used only in its own file is not "unused".
    explicit_exports: bool

    def parse(self, path: str, source: bytes) -> FileFacts: ...

    def make_resolver(self, root: Path, all_files: list[str], files: set[str], cfg) -> Resolver: ...
