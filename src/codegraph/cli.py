"""Command line: index the repository and query the graph."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from . import db
from .config import load_config, matches_any
from .languages import ADAPTERS, adapter_for
from .link import link
from .parse import parse_files
from .scan import diff, git_files, source_files

PROJECT_FILES = ("tsconfig", "jsconfig", "package.json", "pnpm-workspace.yaml")


# -- indexing ------------------------------------------------------------------------------
def run_index(root: Path, db_path: Path, full: bool = False) -> dict:
    t0 = time.perf_counter()
    cfg = load_config(root)
    con = db.connect(db_path)
    if full or db.get_meta(con, "config") != cfg.digest + db.SCHEMA_VERSION:
        db.reset(con)
    all_files = git_files(root)
    paths = source_files(all_files, cfg, root)
    changed, same, deleted = diff(root, paths, db.known_files(con))
    project = sorted(p for p in all_files if os.path.basename(p).startswith(PROJECT_FILES))
    stamp = hashlib.sha1(repr([(p, os.stat(root / p).st_mtime) for p in project if (root / p).exists()])
                         .encode()).hexdigest()
    db.touch_files(con, same)
    relinked = bool(changed or deleted or db.get_meta(con, "project") != stamp)
    if changed:
        facts = parse_files(root, [c.path for c in changed])
        for info in changed:
            db.write_facts(con, info, adapter_for(info.path).name, matches_any(info.path, cfg.tests),
                           facts[info.path], matches_any(info.path, cfg.registry))
    db.delete_files(con, deleted)
    if relinked:
        link(con, root, all_files, cfg)
    db.set_meta(con, "config", cfg.digest + db.SCHEMA_VERSION)
    db.set_meta(con, "project", stamp)
    con.commit()
    return {"files": len(paths), "parsed": len(changed), "deleted": len(deleted), "relinked": relinked,
            "seconds": round(time.perf_counter() - t0, 2)}


# -- graph helpers -------------------------------------------------------------------------
class Graph:
    def __init__(self, con, include_tests: bool):
        self.con, self.tests = con, include_tests
        self.paths = {r["id"]: r["path"] for r in con.execute("SELECT id, path FROM files")}
        self.test_files = {r[0] for r in con.execute("SELECT id FROM files WHERE is_test=1")}

    def visible(self, fid: int) -> bool:
        return self.tests or fid not in self.test_files

    def file_edges(self, kinds: set[str] | None = None) -> list[tuple[int, int, str, int]]:
        """(from_file, to_file, kind, line) for imports and re-exports."""
        rows = self.con.execute(
            "SELECT file_id, target_file_id, kind, line FROM imports WHERE target_file_id IS NOT NULL "
            "UNION ALL SELECT file_id, target_file_id, kind, line FROM exports WHERE target_file_id IS NOT NULL")
        out = []
        for a, b, kind, line in rows:
            if kind == "mock" and not self.tests or kinds is not None and kind not in kinds:
                continue
            if self.visible(a) and self.visible(b):
                out.append((a, b, kind, line))
        return out

    def find_symbols(self, query: str) -> list:
        path, _, name = query.rpartition(":") if ":" in query else ("", "", query)
        sql = ("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=?"
               + (" AND f.path=?" if path else "") + " ORDER BY f.path, s.line")
        return self.con.execute(sql, (name, path) if path else (name,)).fetchall()

    def file_id(self, path: str) -> int | None:
        path = path.replace("\\", "/").removeprefix("./")
        return next((fid for fid, p in self.paths.items() if p == path), None)

    def nodes(self, target: str) -> tuple[set[int], list]:
        """Resolve a CLI target (file path or symbol) to (file ids, symbol rows)."""
        fid = self.file_id(target)
        if fid is not None:
            return {fid}, []
        syms = self.find_symbols(target)
        if not syms:
            raise SystemExit(f"codegraph: no file or symbol named {target!r}")
        return {s["file_id"] for s in syms}, syms


def cmd_refs(g: Graph, args) -> dict:
    syms = g.find_symbols(args.symbol)
    if not syms:
        raise SystemExit(f"codegraph: no symbol named {args.symbol!r}")
    ids = [s["id"] for s in syms]
    marks = ",".join("?" * len(ids))
    rows = g.con.execute(
        f"SELECT r.file_id, r.line, r.kind, s.name AS scope FROM refs r LEFT JOIN symbols s ON s.id=r.scope_symbol_id "
        f"WHERE r.target_symbol_id IN ({marks}) ORDER BY r.file_id, r.line", ids).fetchall()
    refs = [{"path": g.paths[r["file_id"]], "line": r["line"], "kind": r["kind"], "scope": r["scope"]}
            for r in rows if g.visible(r["file_id"])]
    if g.tests:
        files = list({s["file_id"] for s in syms})
        for m in g.con.execute(f"SELECT file_id, line FROM imports WHERE kind='mock' AND target_file_id IN "
                               f"({','.join('?' * len(files))})", files):
            refs.append({"path": g.paths[m["file_id"]], "line": m["line"], "kind": "mock", "scope": None})
    defs = [{"path": s["path"], "line": s["line"], "kind": s["kind"]} for s in syms]
    return {"symbol": args.symbol, "definitions": defs, "refs": refs}


def cmd_callers(g: Graph, args) -> dict:
    data = cmd_refs(g, args)
    calls = [r for r in data["refs"] if r["kind"] in ("call", "jsx")]
    grouped = Counter((r["path"], r["scope"] or "<module>") for r in calls)
    return {"symbol": args.symbol, "callers": [{"path": p, "scope": s, "calls": n} for (p, s), n in sorted(grouped.items())]}


def cmd_callees(g: Graph, args) -> dict:
    syms = g.find_symbols(args.symbol)
    if not syms:
        raise SystemExit(f"codegraph: no symbol named {args.symbol!r}")
    ids = [s["id"] for s in syms]
    rows = g.con.execute(
        f"SELECT t.name, t.kind, t.file_id, COUNT(*) AS n FROM refs r JOIN symbols t ON t.id=r.target_symbol_id "
        f"WHERE r.scope_symbol_id IN ({','.join('?' * len(ids))}) AND r.kind IN ('call','jsx') "
        f"AND t.id NOT IN ({','.join('?' * len(ids))}) GROUP BY t.id ORDER BY t.name", ids + ids)
    return {"symbol": args.symbol, "callees": [{"name": r["name"], "kind": r["kind"], "path": g.paths[r["file_id"]],
                                                "calls": r["n"]} for r in rows]}


def cmd_impact(g: Graph, args) -> dict:
    files, syms = g.nodes(args.target)
    depth, symbols = {f: 0 for f in files}, []
    if syms:  # symbol-level closure: who references the symbol, transitively (type refs included)
        depth, seen, queue = {}, {s["id"] for s in syms}, deque(s["id"] for s in syms)
        files = set()  # module-level uses spread to every importer of that module
        while queue:
            for r in g.con.execute("SELECT file_id, scope_symbol_id FROM refs WHERE target_symbol_id=?",
                                   (queue.popleft(),)):
                if not g.visible(r["file_id"]):
                    continue
                depth.setdefault(r["file_id"], 1)
                if r["scope_symbol_id"] is None:
                    files.add(r["file_id"])
                elif r["scope_symbol_id"] not in seen:
                    seen.add(r["scope_symbol_id"])
                    queue.append(r["scope_symbol_id"])
        symbols = sorted({(g.paths[r["file_id"]], r["name"]) for r in g.con.execute(
            f"SELECT file_id, name FROM symbols WHERE id IN ({','.join('?' * len(seen))})", list(seen))})
    reverse = defaultdict(set)
    for a, b, _, _ in g.file_edges():
        reverse[b].add(a)
    queue = deque(files)
    while queue:
        f = queue.popleft()
        for dep in reverse[f]:
            if dep not in depth:
                depth[dep] = depth[f] + 1
                queue.append(dep)
    impacted = sorted((d, g.paths[f]) for f, d in depth.items() if d > 0)
    return {"target": args.target, "files": [{"path": p, "depth": d} for d, p in impacted],
            "symbols": [{"path": p, "name": n} for p, n in symbols]}


def cmd_path(g: Graph, args) -> dict:
    src, _ = g.nodes(args.a)
    dst, _ = g.nodes(args.b)
    forward = defaultdict(list)
    for a, b, kind, line in g.file_edges():
        forward[a].append((b, kind, line))
    prev = {f: None for f in src}
    queue = deque(src)
    while queue:
        f = queue.popleft()
        if f in dst:
            hops = []
            while prev[f] is not None:
                p, kind, line = prev[f]
                hops.append({"from": g.paths[p], "to": g.paths[f], "kind": kind, "line": line})
                f = p
            return {"from": args.a, "to": args.b, "path": hops[::-1]}
        for b, kind, line in forward[f]:
            if b not in prev:
                prev[b] = (f, kind, line)
                queue.append(b)
    return {"from": args.a, "to": args.b, "path": None}


def strongly_connected(nodes, edges: dict[int, list[int]]) -> list[list[int]]:
    """Tarjan's algorithm, iterative so deep import chains don't hit the recursion limit."""
    index: dict[int, int] = {}
    low: dict[int, int] = {}
    stack: list[int] = []
    on_stack: set[int] = set()
    components = []

    def enter(v: int) -> None:
        index[v] = low[v] = len(index)
        stack.append(v)
        on_stack.add(v)

    for start in nodes:
        if start in index:
            continue
        enter(start)
        work = [(start, iter(edges[start]))]
        while work:
            v, children = work[-1]
            for w in children:
                if w not in index:
                    enter(w)
                    work.append((w, iter(edges[w])))
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            else:  # all children done
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    component = []
                    while not component or component[-1] != v:
                        component.append(stack.pop())
                        on_stack.discard(component[-1])
                    components.append(component)
    return components


def cmd_cycles(g: Graph, args) -> dict:
    kinds = set(args.kinds.split(","))
    edges = defaultdict(list)
    for a, b, _, _ in g.file_edges(kinds):
        edges[a].append(b)
    cycles = []
    for comp in strongly_connected(sorted(edges), edges):
        if len(comp) == 1 and comp[0] not in edges[comp[0]]:
            continue
        start = min(comp, key=lambda f: g.paths[f])
        loop = shortest_loop(start, set(comp), edges)
        cycles.append({"files": sorted(g.paths[f] for f in comp), "example": [g.paths[f] for f in loop]})
    cycles.sort(key=lambda c: (-len(c["files"]), c["files"]))
    return {"kinds": sorted(kinds), "cycles": cycles}


def shortest_loop(start: int, members: set[int], edges: dict[int, list[int]]) -> list[int]:
    """BFS inside one strongly connected component: start -> ... -> start."""
    prev: dict[int, int | None] = {start: None}
    queue = deque([start])
    while queue:
        f = queue.popleft()
        if start in edges[f]:
            path = [start]
            while f is not None:
                path.append(f)
                f = prev[f]
            return path[::-1]
        for b in edges[f]:
            if b in members and b not in prev:
                prev[b] = f
                queue.append(b)
    return [start]


def cmd_unused(g: Graph, args) -> dict:
    cfg = load_config(Path(args.root))
    used: set[int] = set()
    whole: set[int] = set()  # modules loaded without a binding: every export counts as used
    for r in g.con.execute("SELECT * FROM imports WHERE target_file_id IS NOT NULL AND kind != 'mock'"):
        if g.visible(r["file_id"]):
            used.add(r["target_symbol_id"])
            if r["imported"] in (None, "*") and r["local"] is None:
                whole.add(r["target_file_id"])
    implicit = {a.name for a in ADAPTERS if not a.explicit_exports}
    for r in g.con.execute("SELECT r.file_id, r.scope_symbol_id, r.target_symbol_id, s.file_id AS def_file, f.lang "
                           "FROM refs r JOIN symbols s ON s.id=r.target_symbol_id JOIN files f ON f.id=s.file_id"):
        same_file_use = r["lang"] in implicit and r["scope_symbol_id"] != r["target_symbol_id"]
        if g.visible(r["file_id"]) and (r["file_id"] != r["def_file"] or same_file_use):
            used.add(r["target_symbol_id"])
    exports = defaultdict(list)
    for e in g.con.execute("SELECT file_id, target_file_id, target_symbol_id, kind FROM exports"):
        exports[e["file_id"]].append(e)
    todo = list(whole)
    while todo:  # follow `export *` chains of modules imported as a whole
        for e in exports[todo.pop()]:
            used.add(e["target_symbol_id"])
            if e["kind"] == "star" and e["target_file_id"] not in whole:
                whole.add(e["target_file_id"])
                todo.append(e["target_file_id"])
    rows = g.con.execute("SELECT DISTINCT e.name, s.id, s.kind, s.line, s.file_id FROM exports e "
                         "JOIN symbols s ON s.id=e.target_symbol_id WHERE e.kind='local' AND e.file_id=s.file_id")
    unused = [{"path": g.paths[r["file_id"]], "name": r["name"], "kind": r["kind"], "line": r["line"]} for r in rows
              if r["id"] not in used and r["file_id"] not in g.test_files
              and not matches_any(g.paths[r["file_id"]], cfg.entrypoints)]
    return {"unused": sorted(unused, key=lambda u: (u["path"], u["line"]))}


def cmd_stats(g: Graph, args) -> dict:
    q = lambda sql: g.con.execute(sql).fetchall()  # noqa: E731
    counts = {t: q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("files", "symbols", "imports", "exports", "refs")}
    edge_status = Counter()
    for t in ("imports", "exports", "refs"):
        for status, n in q(f"SELECT status, COUNT(*) FROM {t} GROUP BY status"):
            edge_status[f"{t}.{status}"] = n
    by_kind = dict(q("SELECT kind, COUNT(*) FROM imports GROUP BY kind ORDER BY 2 DESC"))
    totals = dict(q("SELECT status, COUNT(*) FROM unresolved GROUP BY status"))
    reasons = [dict(r) for r in q("SELECT reason, COUNT(*) AS n FROM unresolved GROUP BY reason ORDER BY n DESC")]
    causes = [dict(r) for r in q("SELECT reason, spec, status, COUNT(*) AS n FROM unresolved "
                                 "GROUP BY reason, spec ORDER BY n DESC, spec LIMIT 10")]
    return {"counts": counts, "import_kinds": by_kind, "edge_status": dict(edge_status),
            "unresolved": totals.get("unresolved", 0), "ambiguous": totals.get("ambiguous", 0),
            "reasons": reasons, "top_causes": causes}


# -- text rendering ------------------------------------------------------------------------
def render(cmd: str, data: dict) -> str:
    lines = []
    if cmd == "index":
        lines.append("indexed {files} files: {parsed} parsed, {deleted} deleted, relinked={relinked} "
                     "in {seconds}s".format(**data))
    elif cmd == "refs":
        lines += [f"def  {d['path']}:{d['line']}  {d['kind']}" for d in data["definitions"]]
        lines += [f"{r['path']}:{r['line']}  {r['kind']}" + (f"  in {r['scope']}" if r["scope"] else "")
                  for r in data["refs"]]
        lines.append(f"{len(data['refs'])} reference(s)")
    elif cmd == "callers":
        lines += [f"{c['path']}  {c['scope']}  ({c['calls']}x)" for c in data["callers"]]
    elif cmd == "callees":
        lines += [f"{c['name']}  {c['kind']}  {c['path']}  ({c['calls']}x)" for c in data["callees"]]
    elif cmd == "impact":
        lines += [f"symbol  {s['path']}  {s['name']}" for s in data["symbols"]]
        lines += [f"{'  ' * (f['depth'] - 1)}{f['path']}" for f in data["files"]]
        lines.append(f"{len(data['files'])} file(s) impacted")
    elif cmd == "path":
        if data["path"] is None:
            lines.append("no path")
        for i, h in enumerate(data["path"] or []):
            lines += [h["from"]] if i == 0 else []
            lines.append(f"  -[{h['kind']} @{h['line']}]-> {h['to']}")
    elif cmd == "cycles":
        for c in data["cycles"]:
            lines.append(f"cycle of {len(c['files'])} file(s): " + " -> ".join(c["example"]))
        lines.append(f"{len(data['cycles'])} cycle(s) (kinds: {', '.join(data['kinds'])})")
    elif cmd == "unused-exports":
        lines += [f"{u['path']}:{u['line']}  {u['name']}  ({u['kind']})" for u in data["unused"]]
        lines.append(f"{len(data['unused'])} unused export(s)")
    elif cmd == "stats":
        lines.append("  ".join(f"{k}={v}" for k, v in data["counts"].items()))
        lines.append("import kinds: " + "  ".join(f"{k}={v}" for k, v in data["import_kinds"].items()))
        lines.append("edge status: " + "  ".join(f"{k}={v}" for k, v in sorted(data["edge_status"].items())))
        lines.append(f"unresolved={data['unresolved']}  ambiguous={data['ambiguous']}")
        lines.append("top causes:")
        lines += [f"  {c['n']:>5}  {c['reason']}  {c['spec']}" for c in data["top_causes"]]
    return "\n".join(lines)


def find_root(start: str | None) -> Path:
    if start:
        return Path(start).resolve()
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(out.stdout.strip() if out.returncode == 0 else os.getcwd()).resolve()


def common_options(defaults: bool) -> argparse.ArgumentParser:
    """Global options, accepted before or after the subcommand.

    Subcommands get SUPPRESSed defaults so they don't overwrite a value given before them.
    """
    def default(value):
        return value if defaults else argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=default(None), help="repository root (default: git toplevel of cwd)")
    common.add_argument("--db", default=default(None), help="database path (default: <root>/.codegraph/graph.db)")
    common.add_argument("--json", action="store_true", default=default(False), help="JSON output")
    common.add_argument("--include-tests", action="store_true", default=default(False),
                        help="include test files and mock edges")
    return common


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codegraph", parents=[common_options(True)])
    common = common_options(False)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", parents=[common]).add_argument("--full", action="store_true")
    for name in ("refs", "callers", "callees"):
        sub.add_parser(name, parents=[common]).add_argument("symbol", help="name or path:name")
    sub.add_parser("impact", parents=[common]).add_argument("target", help="file path or symbol")
    p = sub.add_parser("path", parents=[common])
    p.add_argument("a"), p.add_argument("b")
    sub.add_parser("cycles", parents=[common]).add_argument(
        "--kinds", default="static,reexport,star,registry",
        help="edge kinds to follow (add type,dynamic to widen)")
    sub.add_parser("unused-exports", parents=[common])
    sub.add_parser("stats", parents=[common])
    args = parser.parse_args(argv)
    root = find_root(args.root)
    args.root = str(root)
    db_path = Path(args.db) if args.db else root / ".codegraph" / "graph.db"
    if args.cmd == "index":
        data = run_index(root, db_path, args.full)
    else:
        if not db_path.exists():
            raise SystemExit("codegraph: no index found, run `codegraph index` first")
        handler = {"refs": cmd_refs, "callers": cmd_callers, "callees": cmd_callees, "impact": cmd_impact,
                   "path": cmd_path, "cycles": cmd_cycles, "unused-exports": cmd_unused, "stats": cmd_stats}
        data = handler[args.cmd](Graph(db.connect(db_path), args.include_tests), args)
    print(json.dumps(data, indent=2) if args.json else render(args.cmd, data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
