"""SQLite storage. Every edge row carries a `kind`; link results carry a `status`."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .languages.base import FileFacts
from .scan import FileInfo

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, lang TEXT, hash TEXT, mtime REAL, size INTEGER,
  is_test INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  name TEXT, kind TEXT, line INTEGER, end_line INTEGER, exported INTEGER);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line INTEGER, spec TEXT, kind TEXT, imported TEXT, local TEXT,
  target_file_id INTEGER, target_symbol_id INTEGER, external TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS exports(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line INTEGER, name TEXT, kind TEXT, local TEXT, spec TEXT,
  target_file_id INTEGER, target_symbol_id INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS refs(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line INTEGER, name TEXT, kind TEXT, scope_symbol_id INTEGER, target_symbol_id INTEGER, status TEXT);
CREATE TABLE IF NOT EXISTS unresolved(
  id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  line INTEGER, kind TEXT, spec TEXT, reason TEXT, status TEXT, stage TEXT);
CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS ix_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS ix_imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS ix_imports_target ON imports(target_file_id);
CREATE INDEX IF NOT EXISTS ix_exports_file ON exports(file_id);
CREATE INDEX IF NOT EXISTS ix_refs_target ON refs(target_symbol_id);
CREATE INDEX IF NOT EXISTS ix_refs_scope ON refs(scope_symbol_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA journal_mode=MEMORY")
    con.executescript(SCHEMA)
    return con


def get_meta(con, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con, key: str, value: str) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def reset(con) -> None:
    con.execute("DELETE FROM files")


def known_files(con) -> dict[str, tuple[float, int, str]]:
    return {r["path"]: (r["mtime"], r["size"], r["hash"]) for r in con.execute("SELECT * FROM files")}


def delete_files(con, paths: list[str]) -> None:
    con.executemany("DELETE FROM files WHERE path=?", [(p,) for p in paths])


def touch_files(con, infos: list[FileInfo]) -> None:
    con.executemany("UPDATE files SET mtime=?, size=? WHERE path=?", [(i.mtime, i.size, i.path) for i in infos])


def write_facts(con, info: FileInfo, lang: str, is_test: bool, facts: FileFacts, registry: bool) -> None:
    con.execute("DELETE FROM files WHERE path=?", (info.path,))
    fid = con.execute("INSERT INTO files(path, lang, hash, mtime, size, is_test) VALUES(?,?,?,?,?,?)",
                      (info.path, lang, info.hash, info.mtime, info.size, int(is_test))).lastrowid
    con.executemany("INSERT INTO symbols(file_id, name, kind, line, end_line, exported) VALUES(?,?,?,?,?,?)",
                    [(fid, s.name, s.kind, s.line, s.end_line, int(s.exported)) for s in facts.symbols])
    con.executemany("INSERT INTO imports(file_id, line, spec, kind, imported, local) VALUES(?,?,?,?,?,?)",
                    [(fid, i.line, i.spec, "registry" if registry and i.kind in ("static", "dynamic") else i.kind,
                      i.imported, i.local) for i in facts.imports])
    con.executemany("INSERT INTO exports(file_id, line, name, kind, local, spec) VALUES(?,?,?,?,?,?)",
                    [(fid, e.line, e.name, e.kind, e.local, e.spec) for e in facts.exports])
    con.executemany("INSERT INTO refs(file_id, line, name, kind) VALUES(?,?,?,?)",
                    [(fid, r.line, r.name, r.kind) for r in facts.refs])
    con.executemany("INSERT INTO unresolved(file_id, line, kind, spec, reason, status, stage) "
                    "VALUES(?,?,?,?,?,'unresolved','parse')",
                    [(fid, u.line, u.kind, u.detail, u.reason) for u in facts.unresolved])
