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
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import glob_match, load_config, matches_any
from .languages import ADAPTERS, adapter_for
from .link import link
from .parse import parse_files
from .scan import diff, git_files, source_files

PROJECT_FILES = ("tsconfig", "jsconfig", "package.json", "pnpm-workspace.yaml")


# -- indexing ------------------------------------------------------------------------------
def head_commit(root: Path) -> str | None:
    """HEAD commit of the repo, or None when git has no commit (or cannot be run)."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    except OSError:
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def human_age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def index_info(con, root: Path) -> dict:
    """When the stored graph last changed and at which commit; None fields when unknown."""
    updated = db.get_meta(con, "updated_at")
    head = head_commit(root)
    commit = db.get_meta(con, "commit") or None
    return {"updated_at": iso_utc(float(updated)) if updated else None,
            "age_seconds": round(time.time() - float(updated)) if updated else None,
            "commit": commit, "head": head,
            "commit_matches_head": (commit == head) if commit and head else None}


def run_index(root: Path, db_path: Path, full: bool = False) -> dict:
    t0 = time.perf_counter()
    cfg = load_config(root)
    for warning in cfg.warnings:
        print(f"codegraph: warning: {warning}", file=sys.stderr)
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
    up_to_date = not (changed or deleted or relinked)
    if not up_to_date or not db.get_meta(con, "updated_at"):
        db.set_meta(con, "updated_at", repr(time.time()))
        db.set_meta(con, "commit", head_commit(root) or "")
    con.commit()
    return {"files": len(paths), "parsed": len(changed), "deleted": len(deleted), "relinked": relinked,
            "seconds": round(time.perf_counter() - t0, 2), "up_to_date": up_to_date, **index_info(con, root)}


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
    limit = getattr(args, "depth", None)
    through_registry = getattr(args, "through_registry", False)
    depth, symbols = {f: 0 for f in files}, []
    via: dict[int, tuple[str, int | None]] = {}  # file -> (edge kind it was reached by, file it came from)
    if syms:  # symbol-level closure: who references the symbol, transitively (type refs included)
        depth, seen, queue = {}, {s["id"] for s in syms}, deque(s["id"] for s in syms)
        files = set()  # module-level uses spread to every importer of that module
        while queue:
            for r in g.con.execute("SELECT file_id, scope_symbol_id FROM refs WHERE target_symbol_id=?",
                                   (queue.popleft(),)):
                if not g.visible(r["file_id"]):
                    continue
                if r["file_id"] not in depth:
                    depth[r["file_id"]] = 1
                    via[r["file_id"]] = ("ref", None)
                if r["scope_symbol_id"] is None:
                    files.add(r["file_id"])
                elif r["scope_symbol_id"] not in seen:
                    seen.add(r["scope_symbol_id"])
                    queue.append(r["scope_symbol_id"])
        symbols = sorted({(g.paths[r["file_id"]], r["name"]) for r in g.con.execute(
            f"SELECT file_id, name FROM symbols WHERE id IN ({','.join('?' * len(seen))})", list(seen))})
    reverse = defaultdict(list)  # imported file -> [(importer, kind)], registry edges last
    for a, b, kind, _ in sorted(g.file_edges(), key=lambda e: (e[2] == "registry", g.paths[e[0]], e[2])):
        if (a, kind) not in reverse[b]:
            reverse[b].append((a, kind))
    # Pass 1 expands everything except registry edges (all edges with --through-registry).
    expandable = set(files)  # nodes whose importers count (in symbol mode, only module-level users and below)
    queue = deque(sorted(files, key=lambda f: depth[f]))
    while queue:
        f = queue.popleft()
        if limit is not None and depth[f] >= limit:
            continue
        for dep, kind in reverse[f]:
            if dep not in depth and (through_registry or kind != "registry"):
                depth[dep] = depth[f] + 1
                via[dep] = (kind, f)
                expandable.add(dep)
                queue.append(dep)
    # Pass 2: a registry file that imports something already impacted is reported but never expanded.
    stopped: set[int] = set()
    if not through_registry:
        for f in sorted(expandable, key=lambda f: depth[f]):
            if limit is not None and depth[f] >= limit:
                continue
            for dep, kind in reverse[f]:
                if kind == "registry" and dep not in depth:
                    depth[dep] = depth[f] + 1
                    via[dep] = (kind, f)
                    stopped.add(dep)
    impacted = sorted((d, g.paths[f], f) for f, d in depth.items() if d > 0)
    out_files = [{"path": p, "depth": d, "via": via[f][0],
                  "from": g.paths[via[f][1]] if via[f][1] is not None else None} for d, p, f in impacted]
    counts = Counter(f["depth"] for f in out_files)
    return {"target": args.target, "files": out_files,
            "symbols": [{"path": p, "name": n} for p, n in symbols],
            "depth_counts": {str(d): counts[d] for d in sorted(counts)},
            "depth_limit": limit, "through_registry": through_registry,
            "registry_stopped": sorted(g.paths[f] for f in stopped)}


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
    all_edges = g.file_edges(kinds)
    edges = defaultdict(list)
    for a, b, _, _ in all_edges:
        edges[a].append(b)
    cycles, comps = [], []
    for comp in strongly_connected(sorted(edges), edges):
        if len(comp) == 1 and comp[0] not in edges[comp[0]]:
            continue
        start = min(comp, key=lambda f: g.paths[f])
        loop = shortest_loop(start, set(comp), edges)
        cycles.append({"files": sorted(g.paths[f] for f in comp), "example": [g.paths[f] for f in loop]})
        comps.append(set(comp))
    inside = {f: i for i, comp in enumerate(comps) for f in comp}  # file -> index in `comps`
    members: list[list[tuple[int, int, str, int]]] = [[] for _ in comps]
    for edge in all_edges:  # edges that sit inside one component
        if edge[0] in inside and inside.get(edge[1]) == inside[edge[0]]:
            members[inside[edge[0]]].append(edge)
    for cycle, comp, inner in zip(cycles, comps, members):
        kind_counts = Counter(kind for _, _, kind, _ in inner)
        # Removing every edge of one kind: if that breaks the cycle, the kind is needed to hold it together.
        held_by = sorted(k for k in kind_counts if not has_cycle(comp, [e for e in inner if e[2] != k]))
        cycle["edge_kinds"] = dict(sorted(kind_counts.items()))
        cycle["held_by"] = held_by
        cycle["needs_registry"] = "registry" in held_by
        if getattr(args, "edges", False):
            cycle["edges"] = [{"from": g.paths[a], "to": g.paths[b], "kind": kind, "line": line}
                              for a, b, kind, line in sorted(inner, key=lambda e: (g.paths[e[0]], g.paths[e[1]], e[2]))]
    order = sorted(range(len(cycles)), key=lambda i: (-len(cycles[i]["files"]), cycles[i]["files"]))
    return {"kinds": sorted(kinds), "members": bool(getattr(args, "members", False)),
            "cycles": [cycles[i] for i in order]}


def has_cycle(nodes: set[int], edge_list: list[tuple[int, int, str, int]]) -> bool:
    """True when the subgraph over `nodes` still contains a cycle (a loop, or a component of 2+ files)."""
    adj = defaultdict(list)
    for a, b, _, _ in edge_list:
        adj[a].append(b)
    return any(len(c) > 1 or c[0] in adj[c[0]] for c in strongly_connected(sorted(nodes), adj))


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
    classes = parse_csv(getattr(args, "klass", None), UNUSED_CLASSES, "--class")
    kinds = parse_csv(getattr(args, "kind", None), None, "--kind")
    globs = getattr(args, "path", None) or []
    limit = getattr(args, "limit", None)
    # Who uses each symbol. Tests are always read, so `test-only` can be told apart from `dead`;
    # `--include-tests` then decides whether test usage counts as real usage.
    prod: set[int] = set()  # used from non-test files
    tests: set[int] = set()  # used from test files only (not in `prod`)
    local: set[int] = set()  # used inside its own file
    whole = {"prod": set(), "test": set()}  # modules loaded without a binding: every export counts as used
    for r in g.con.execute("SELECT * FROM imports WHERE target_file_id IS NOT NULL AND kind != 'mock'"):
        side = "test" if r["file_id"] in g.test_files else "prod"
        (tests if side == "test" else prod).add(r["target_symbol_id"])
        if r["imported"] in (None, "*") and r["local"] is None:
            whole[side].add(r["target_file_id"])
    implicit = {a.name for a in ADAPTERS if not a.explicit_exports}
    for r in g.con.execute("SELECT r.file_id, r.scope_symbol_id, r.target_symbol_id, s.file_id AS def_file, f.lang "
                           "FROM refs r JOIN symbols s ON s.id=r.target_symbol_id JOIN files f ON f.id=s.file_id"):
        if r["file_id"] != r["def_file"]:
            (tests if r["file_id"] in g.test_files else prod).add(r["target_symbol_id"])
        elif r["scope_symbol_id"] != r["target_symbol_id"]:
            # Python has no `export` keyword, so a use inside the file counts as a real use there
            (prod if r["lang"] in implicit else local).add(r["target_symbol_id"])
    exports = defaultdict(list)
    for e in g.con.execute("SELECT file_id, target_file_id, target_symbol_id, kind FROM exports"):
        exports[e["file_id"]].append(e)
    for side, target in (("prod", prod), ("test", tests)):
        seen, todo = set(whole[side]), list(whole[side])
        while todo:  # follow `export *` chains of modules imported as a whole
            for e in exports[todo.pop()]:
                target.add(e["target_symbol_id"])
                if e["kind"] == "star" and e["target_file_id"] not in seen:
                    seen.add(e["target_file_id"])
                    todo.append(e["target_file_id"])
    rows = g.con.execute("SELECT DISTINCT e.name, s.id, s.kind, s.line, s.file_id FROM exports e "
                         "JOIN symbols s ON s.id=e.target_symbol_id WHERE e.kind='local' AND e.file_id=s.file_id")
    unused = []
    for r in rows:
        path, sid = g.paths[r["file_id"]], r["id"]
        if sid in prod or r["file_id"] in g.test_files or matches_any(path, cfg.entrypoints):
            continue
        if sid in tests:
            if g.tests:  # --include-tests: a test using it is a use
                continue
            cls = "test-only"
        else:
            cls = "local-only" if sid in local else "dead"
        unused.append({"path": path, "name": r["name"], "kind": r["kind"], "line": r["line"], "class": cls})
    if classes:
        unused = [u for u in unused if u["class"] in classes]
    if kinds:
        unused = [u for u in unused if u["kind"] in kinds]
    if globs:
        unused = [u for u in unused if matches_any(u["path"], globs)]
    unused.sort(key=lambda u: (u["path"], u["line"]))
    group_depth = max(getattr(args, "group_depth", 3), 1)
    out = {"total": len(unused), "unused": unused}
    if getattr(args, "summary", False):
        out["summary"] = {"by_class": dict(Counter(u["class"] for u in unused).most_common()),
                          "by_kind": dict(Counter(u["kind"] for u in unused).most_common()),
                          "by_directory": dict(Counter(group_dir(u["path"], group_depth) for u in unused).most_common()),
                          "group_depth": group_depth}
        out["unused"] = []
    elif limit is not None:
        out["unused"] = unused[:max(limit, 0)]
    out["shown"] = len(out["unused"])
    return out


UNUSED_CLASSES = ("dead", "local-only", "test-only")


def parse_csv(value: str | None, allowed: tuple[str, ...] | None, flag: str) -> set[str]:
    items = {v.strip() for v in (value or "").split(",") if v.strip()}
    if allowed is not None and items - set(allowed):
        raise SystemExit(f"codegraph: {flag} takes {', '.join(allowed)} (comma separated), got {value!r}")
    return items


def group_dir(path: str, depth: int) -> str:
    """The first `depth` directory components of a file's path ('.' for files at the root)."""
    return "/".join(path.split("/")[:-1][:depth]) or "."


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
            "reasons": reasons, "top_causes": causes,
            "config": config_in_effect(g, Path(args.root)), "index": index_info(g.con, Path(args.root))}


def config_in_effect(g: Graph, root: Path) -> dict:
    """How many indexed files each pattern of registry / entrypoints / tests matches (0 = probably a typo)."""
    cfg = load_config(root)
    out: dict = {"file": "codegraph.toml" if cfg.present else None, "warnings": list(cfg.warnings)}
    paths = list(g.paths.values())
    for key in ("registry", "entrypoints", "tests"):
        patterns = getattr(cfg, key)
        out[key] = {"source": "toml" if key in cfg.explicit else "default" if patterns else "unset",
                    "files": sum(1 for p in paths if matches_any(p, patterns)),
                    "patterns": [{"pattern": pat, "files": sum(1 for p in paths if glob_match(p, pat))}
                                 for pat in patterns]}
    return out


# -- text rendering ------------------------------------------------------------------------
def short(commit: str | None) -> str:
    return commit[:7] if commit else "(none)"


def render(cmd: str, data: dict) -> str:
    lines = []
    if cmd == "index":
        lines.append("indexed {files} files: {parsed} parsed, {deleted} deleted, relinked={relinked} "
                     "in {seconds}s".format(**data))
        if data["up_to_date"]:
            lines.append("nothing changed since the last index: it is up to date with the working tree")
        if data["updated_at"]:
            lines.append(f"index last changed {data['updated_at']} ({human_age(data['age_seconds'])} ago), "
                         f"built at commit {short(data['commit'])}")
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
        lines += [f"{'  ' * (f['depth'] - 1)}{f['path']}" + ("  [registry]" if f["via"] == "registry" else "")
                  for f in data["files"]]
        per_depth = ", ".join(f"depth {d}: {n}" for d, n in data["depth_counts"].items())
        lines.append(f"{len(data['files'])} file(s) impacted" + (f" ({per_depth})" if per_depth else ""))
        if data["registry_stopped"]:
            lines.append(f"stopped at {len(data['registry_stopped'])} registry file(s), not followed to their "
                         f"importers (use --through-registry to follow them)")
        if data["depth_limit"] is not None:
            lines.append(f"depth limited to {data['depth_limit']} (--depth)")
    elif cmd == "path":
        if data["path"] is None:
            lines.append("no path")
        for i, h in enumerate(data["path"] or []):
            lines += [h["from"]] if i == 0 else []
            lines.append(f"  -[{h['kind']} @{h['line']}]-> {h['to']}")
    elif cmd == "cycles":
        for c in data["cycles"]:
            lines.append(f"cycle of {len(c['files'])} file(s): " + " -> ".join(c["example"]))
            kinds = ", ".join(f"{k}={n}" for k, n in c["edge_kinds"].items())
            held = f"; broken by removing: {', '.join(c['held_by'])}" if c["held_by"] else "; no single kind holds it"
            lines.append(f"  edges: {kinds}{held}" + ("  [needs registry edges]" if c["needs_registry"] else ""))
            if data.get("members"):
                lines += [f"  member {f}" for f in c["files"]]
            lines += [f"  edge {e['from']}:{e['line']} -[{e['kind']}]-> {e['to']}" for e in c.get("edges", [])]
        lines.append(f"{len(data['cycles'])} cycle(s) (kinds: {', '.join(data['kinds'])})")
    elif cmd == "unused-exports":
        lines += [f"{u['path']}:{u['line']}  {u['name']}  ({u['kind']})  [{u['class']}]" for u in data["unused"]]
        if "summary" in data:
            s = data["summary"]
            lines.append("by class: " + "  ".join(f"{k}={v}" for k, v in s["by_class"].items()))
            lines.append("by kind: " + "  ".join(f"{k}={v}" for k, v in s["by_kind"].items()))
            lines.append(f"by directory (first {s['group_depth']} levels):")
            lines += [f"  {n:>5}  {d}" for d, n in s["by_directory"].items()]
        lines.append(f"{data['total']} unused export(s)"
                     + (f", showing {data['shown']}" if data["shown"] < data["total"] and "summary" not in data else ""))
    elif cmd == "stats":
        lines.append("  ".join(f"{k}={v}" for k, v in data["counts"].items()))
        lines.append("import kinds: " + "  ".join(f"{k}={v}" for k, v in data["import_kinds"].items()))
        lines.append("edge status: " + "  ".join(f"{k}={v}" for k, v in sorted(data["edge_status"].items())))
        lines.append(f"unresolved={data['unresolved']}  ambiguous={data['ambiguous']}")
        lines.append("top causes:")
        lines += [f"  {c['n']:>5}  {c['reason']}  {c['spec']}" for c in data["top_causes"]]
        cfg = data["config"]
        lines.append(f"config ({cfg['file'] or 'no codegraph.toml, defaults in effect'}):")
        for key in ("registry", "entrypoints", "tests"):
            c = cfg[key]
            lines.append(f"  {key}: {c['files']} file(s) matched [{c['source']}]")
            # Built-in defaults cover several ecosystems, so most of them match nothing in any
            # one repo; only a pattern the user wrote is worth flagging.
            flag = "  <- matches nothing" if c["source"] == "toml" else ""
            lines += [f"    {p['files']:>5}  {p['pattern']}" + ("" if p["files"] else flag) for p in c["patterns"]]
        lines += [f"  warning: {w}" for w in cfg["warnings"]]
        ix = data["index"]
        if ix["updated_at"]:
            lines.append(f"index: last changed {ix['updated_at']} ({human_age(ix['age_seconds'])} ago), "
                         f"built at commit {short(ix['commit'])}, HEAD is {short(ix['head'])}"
                         + ("" if ix["commit_matches_head"] is not False else "  <- index is from another commit"))
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
    p = sub.add_parser("impact", parents=[common])
    p.add_argument("target", help="file path or symbol")
    p.add_argument("--depth", type=int, default=None, metavar="N",
                   help="follow importers at most N levels (default: unlimited)")
    p.add_argument("--through-registry", action="store_true",
                   help="also follow importers of registry files (default: report the registry file, "
                        "but stop there)")
    p = sub.add_parser("path", parents=[common])
    p.add_argument("a"), p.add_argument("b")
    p = sub.add_parser("cycles", parents=[common])
    p.add_argument("--kinds", default="static,reexport,star,registry",
                   help="edge kinds to follow (add type,dynamic to widen)")
    p.add_argument("--members", action="store_true", help="text output: list every file of each cycle")
    p.add_argument("--edges", action="store_true",
                   help="also list the edges (from, to, kind, line) inside each cycle")
    p = sub.add_parser("unused-exports", parents=[common])
    p.add_argument("--path", action="append", metavar="GLOB",
                   help="only exports in files matching this glob (repeatable), e.g. 'src/modules/guest/**'")
    p.add_argument("--kind", metavar="KINDS", help="only these symbol kinds, comma separated "
                                                   "(function, component, type, interface, const, class, ...)")
    p.add_argument("--class", dest="klass", metavar="CLASSES",
                   help=f"only these classes, comma separated ({', '.join(UNUSED_CLASSES)})")
    p.add_argument("--limit", type=int, default=None, metavar="N", help="list at most N entries (total is kept)")
    p.add_argument("--summary", action="store_true", help="counts per class, kind and directory; no entries")
    p.add_argument("--group-depth", type=int, default=3, metavar="N",
                   help="directory levels used by --summary (default: 3)")
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
