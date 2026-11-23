"""Resolve module specifiers, then follow barrels / re-exports to the real definitions.

An export reachable through several `export *` sources that disagree is left unlinked
and marked `ambiguous` (this mirrors TypeScript, which drops such names).
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from pathlib import Path

from .config import Config
from .languages import adapter_for

OK, AMBIGUOUS, MISSING, EXTERNAL = "resolved", "ambiguous", "unresolved", "external"


def resolve_modules(con, root: Path, all_files: list[str], cfg: Config) -> None:
    """Point every import / re-export at a file (internal), a package (external) or nothing."""
    file_ids = {r["path"]: r["id"] for r in con.execute("SELECT id, path FROM files")}
    resolvers: dict[str, object] = {}
    unresolved = []

    def resolver_for(path: str):
        adapter = adapter_for(path)
        if adapter.name not in resolvers:
            resolvers[adapter.name] = adapter.make_resolver(root, all_files, set(file_ids), cfg)
        return resolvers[adapter.name]

    def resolve(row, spec: str) -> tuple[int | None, str, str]:
        status, value = resolver_for(row["path"]).resolve(spec, row["path"])
        if status == "unresolved":
            unresolved.append((row["file_id"], row["line"], row["kind"], spec, value))
        target = file_ids[value] if status == "internal" else None
        return target, OK if target else status, value

    def rows(table: str):
        return con.execute(f"SELECT t.*, f.path FROM {table} t JOIN files f ON f.id = t.file_id "
                           "WHERE t.spec IS NOT NULL").fetchall()

    imports = []
    for r in rows("imports"):
        spec, imported = r["spec"], r["imported"]
        member_module = getattr(resolver_for(r["path"]), "member_module", None)
        if member_module and imported not in (None, "*"):
            hit = member_module(spec, imported, r["path"])
            if hit:  # Python `from pkg import mod`: the edge points at the submodule itself
                spec, imported = hit[0], "*"
        target, status, value = resolve(r, spec)
        imports.append((spec, imported, target, value if status == EXTERNAL else None, status, r["id"]))
    con.executemany("UPDATE imports SET spec=?, imported=?, target_file_id=?, external=?, status=? WHERE id=?",
                    imports)
    exports = [(*resolve(r, r["spec"])[:2], r["id"]) for r in rows("exports")]
    con.executemany("UPDATE exports SET target_file_id=?, status=? WHERE id=?", exports)
    con.executemany("INSERT INTO unresolved(file_id, line, kind, spec, reason, status, stage) "
                    "VALUES(?,?,?,?,?,'unresolved','link')", unresolved)


class Linker:
    def __init__(self, con):
        self.con = con
        self.syms: dict[int, dict[str, int]] = defaultdict(dict)
        self.ranges: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        for s in con.execute("SELECT id, file_id, name, line, end_line FROM symbols ORDER BY line"):
            self.syms[s["file_id"]].setdefault(s["name"], s["id"])
            self.ranges[s["file_id"]].append((s["line"], s["end_line"], s["id"]))
        self.exports: dict[int, dict[str, dict]] = defaultdict(dict)
        self.stars: dict[int, list[dict]] = defaultdict(list)
        for e in con.execute("SELECT * FROM exports"):
            if e["kind"] == "star":
                self.stars[e["file_id"]].append(dict(e))
            else:
                self.exports[e["file_id"]].setdefault(e["name"], dict(e))
        self.locals: dict[int, dict[str, dict]] = defaultdict(dict)
        for i in con.execute("SELECT * FROM imports WHERE local IS NOT NULL"):
            self.locals[i["file_id"]].setdefault(i["local"], dict(i))
        self.memo: dict[tuple[int, str], tuple] = {}

    def export(self, fid: int, name: str, stack: frozenset = frozenset()) -> tuple[str, int | None, int | None]:
        """(status, symbol_id, file_id) for `name` as exported by file `fid`."""
        key = (fid, name)
        if key in self.memo:
            return self.memo[key]
        if key in stack:
            return MISSING, None, None
        stack |= {key}
        e = self.exports[fid].get(name)
        if e is not None:
            if e["kind"] == "local":
                res = self.local(fid, e["local"], stack)
            elif e["target_file_id"] is None:
                res = (EXTERNAL if e["status"] == EXTERNAL else MISSING, None, None)
            elif e["local"] == "*":
                res = (OK, None, e["target_file_id"])
            else:
                res = self.export(e["target_file_id"], e["local"], stack)
        elif name == "default":
            res = (MISSING, None, None)
        else:
            found, external = {}, False
            for star in self.stars[fid]:
                if star["target_file_id"] is None:
                    external |= star["status"] == EXTERNAL
                    continue
                r = self.export(star["target_file_id"], name, stack)
                if r[0] in (OK, AMBIGUOUS):
                    found[r[1:] if r[0] == OK else ("ambiguous", star["id"])] = r
                external |= r[0] == EXTERNAL
            if len(found) == 1:
                res = next(iter(found.values()))
            elif found:
                res = (AMBIGUOUS, None, None)
            else:
                res = (EXTERNAL if external else MISSING, None, None)
        self.memo[key] = res
        return res

    def local(self, fid: int, name: str, stack: frozenset = frozenset()) -> tuple:
        if name in self.syms[fid]:
            return OK, self.syms[fid][name], fid
        imp = self.locals[fid].get(name)
        if imp is None:
            return MISSING, None, None
        return self.via_import(imp, stack)

    def via_import(self, imp: dict, stack: frozenset = frozenset()) -> tuple:
        if imp["target_file_id"] is None:
            return (EXTERNAL if imp["status"] == EXTERNAL else MISSING), None, None
        if imp["imported"] in (None, "*"):
            return OK, None, imp["target_file_id"]
        return self.export(imp["target_file_id"], imp["imported"], stack)

    def scope(self, fid: int, line: int) -> int | None:
        ranges = self.ranges[fid]
        i = bisect.bisect_right(ranges, (line, 1 << 30, 1 << 62)) - 1
        return ranges[i][2] if i >= 0 and ranges[i][1] >= line else None

    def run(self) -> None:
        con, problems = self.con, []
        imp_updates = []
        for i in con.execute("SELECT * FROM imports WHERE target_file_id IS NOT NULL"):
            status, sym, _ = self.via_import(dict(i))
            imp_updates.append((sym, status, i["id"]))
            if status in (AMBIGUOUS, MISSING):
                problems.append((i["file_id"], i["line"], i["kind"], f"{i['spec']}#{i['imported']}",
                                 "ambiguous-star-export" if status == AMBIGUOUS else "export-not-found", status))
        con.executemany("UPDATE imports SET target_symbol_id=?, status=? WHERE id=?", imp_updates)
        exp_updates = []
        for e in con.execute("SELECT * FROM exports WHERE kind != 'star'"):
            if e["kind"] == "local":
                status, sym, _ = self.local(e["file_id"], e["local"])
            elif e["target_file_id"] is None:
                continue
            else:
                status, sym, _ = self.export(e["file_id"], e["name"])
            exp_updates.append((sym, status, e["id"]))
            if status == AMBIGUOUS:
                problems.append((e["file_id"], e["line"], e["kind"], f"{e['spec']}#{e['local']}",
                                 "ambiguous-star-export", status))
        con.executemany("UPDATE exports SET target_symbol_id=?, status=? WHERE id=?", exp_updates)
        ref_updates = []
        for r in con.execute("SELECT id, file_id, line, name FROM refs"):
            fid, name = r["file_id"], r["name"]
            if "." in name:  # `ns.member`: member of a module binding, or attribute of an imported name
                head, member = name.rsplit(".", 1)
                imp = self.locals[fid].get(head)
                if imp is None:
                    res = self.local(fid, head)
                elif imp["imported"] == "*" and imp["target_file_id"]:
                    res = self.export(imp["target_file_id"], member)
                else:
                    res = self.via_import(imp)
            else:
                res = self.local(fid, name)
            ref_updates.append((self.scope(fid, r["line"]), res[1], res[0], r["id"]))
        con.executemany("UPDATE refs SET scope_symbol_id=?, target_symbol_id=?, status=? WHERE id=?", ref_updates)
        con.executemany("INSERT INTO unresolved(file_id, line, kind, spec, reason, status, stage) "
                        "VALUES(?,?,?,?,?,?,'link')", problems)


def link(con, root: Path, all_files: list[str], cfg: Config) -> None:
    con.execute("DELETE FROM unresolved WHERE stage='link'")
    resolve_modules(con, root, all_files, cfg)
    Linker(con).run()
