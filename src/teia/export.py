"""File-level import graph export (`teia export`): JSON, GEXF, Mermaid and DOT."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from .config import Config, matches_any

# Edge kinds that exist at runtime; `cycles`, `export` and `view` follow these by default.
RUNTIME_KINDS = ("static", "reexport", "star", "registry")
EDGE_KINDS = ("static", "type", "dynamic", "registry", "reexport", "star", "mock")
FORMATS = ("json", "gexf", "mermaid", "dot")
# Mermaid and DOT become unreadable well before this; read at call time so tests can lower it.
MAX_DIAGRAM_NODES = 250
GEXF_NS = "http://www.gexf.net/1.2draft"


def group_dir(path: str, depth: int) -> str:
    """The first `depth` directory components of a file's path ('.' for files at the root)."""
    return "/".join(path.split("/")[:-1][:depth]) or "."


def build_export(g, cfg: Config, kinds: set[str], globs: list[str] | None = None, group_depth: int = 3,
                 collapse: bool = False) -> dict:
    """Nodes are indexed files (or groups with `collapse`), edges resolved imports per (from, to, kind)."""
    depth = max(group_depth, 1)
    globs = list(globs or [])
    files = {fid: path for fid, path in g.paths.items()
             if g.visible(fid) and (not globs or matches_any(path, globs))}
    langs = {r[0]: r[1] for r in g.con.execute("SELECT id, lang FROM files")}
    imports: Counter = Counter()  # (from file, to file, kind) -> import statements
    for a, b, kind, _ in g.file_edges(set(kinds)):
        if a in files and b in files:
            imports[(a, b, kind)] += 1

    def node_of(fid: int) -> str:
        return group_dir(files[fid], depth) if collapse else files[fid]

    members: dict[str, list[int]] = defaultdict(list)
    for fid in files:
        members[node_of(fid)].append(fid)
    edges: Counter = Counter()
    for (a, b, kind), n in imports.items():
        na, nb = node_of(a), node_of(b)
        if collapse and na == nb:
            continue
        edges[(na, nb, kind)] += n
    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for na, nb, _ in edges:
        if na != nb:
            succ[na].add(nb)
            pred[nb].add(na)

    nodes = []
    for nid in sorted(members):
        paths = sorted(files[f] for f in members[nid])
        flags = [(p, matches_any(p, cfg.registry), matches_any(p, cfg.entrypoints)) for p in paths]
        node_langs = {langs.get(f) for f in members[nid]} - {None}
        tests = [f in g.test_files for f in members[nid]]
        node = {"id": nid, "path": nid, "group": nid if collapse else group_dir(nid, depth),
                "lang": next(iter(node_langs)) if len(node_langs) == 1 else None,
                "is_test": all(tests), "is_registry": any(r for _, r, _ in flags),
                "is_entrypoint": any(e for _, _, e in flags),
                "in_degree": len(pred[nid]), "out_degree": len(succ[nid])}
        if collapse:
            node["files"] = len(paths)
        nodes.append(node)
    edge_list = [{"from": a, "to": b, "kind": kind, "count": n} for (a, b, kind), n in sorted(edges.items())]
    return {"format_version": 1, "kinds": sorted(kinds), "paths": globs, "group_depth": depth,
            "collapsed": collapse, "include_tests": bool(g.tests),
            "counts": {"nodes": len(nodes), "edges": len(edge_list), "imports": sum(imports.values())},
            "nodes": nodes, "edges": edge_list}


def render_export(data: dict, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(data, indent=2)
    if fmt == "gexf":
        return to_gexf(data)
    check_diagram_size(data, fmt)
    return to_mermaid(data) if fmt == "mermaid" else to_dot(data)


def check_diagram_size(data: dict, fmt: str) -> None:
    n, limit = len(data["nodes"]), MAX_DIAGRAM_NODES
    if n > limit:
        raise SystemExit(f"teia: {n} nodes is too many for {fmt} output (limit {limit}); "
                         f"narrow it with --collapse (and a smaller --group-depth) or --path GLOB, "
                         f"or use --format json / gexf")


# -- GEXF ----------------------------------------------------------------------------------
NODE_ATTRS = (("group", "string"), ("path", "string"), ("lang", "string"), ("is_test", "boolean"),
              ("is_registry", "boolean"), ("is_entrypoint", "boolean"), ("in_degree", "integer"),
              ("out_degree", "integer"), ("files", "integer"))


def _gexf_value(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def to_gexf(data: dict) -> str:
    """GEXF 1.2 for Gephi. Parallel edges of different kinds are merged into one edge per pair
    (Gephi merges them anyway): `kind` lists the kinds, `weight` sums the import counts."""
    root = ET.Element("gexf", {"xmlns": GEXF_NS, "version": "1.2"})
    meta = ET.SubElement(root, "meta")
    ET.SubElement(meta, "creator").text = "teia"
    ET.SubElement(meta, "description").text = (
        f"File-level import graph ({'groups' if data['collapsed'] else 'files'}; kinds: {', '.join(data['kinds'])})")
    graph = ET.SubElement(root, "graph", {"defaultedgetype": "directed", "mode": "static"})
    attrs = [(i, name, kind) for i, (name, kind) in enumerate(NODE_ATTRS)
             if name != "files" or data["collapsed"]]
    node_attrs = ET.SubElement(graph, "attributes", {"class": "node"})
    for i, name, kind in attrs:
        ET.SubElement(node_attrs, "attribute", {"id": str(i), "title": name, "type": kind})
    edge_attrs = ET.SubElement(graph, "attributes", {"class": "edge"})
    ET.SubElement(edge_attrs, "attribute", {"id": "0", "title": "kind", "type": "string"})
    ids = {n["id"]: f"n{i}" for i, n in enumerate(data["nodes"])}
    nodes = ET.SubElement(graph, "nodes")
    for n in data["nodes"]:
        node = ET.SubElement(nodes, "node", {"id": ids[n["id"]], "label": n["id"]})
        values = ET.SubElement(node, "attvalues")
        for i, name, _ in attrs:
            if n.get(name) is not None:
                ET.SubElement(values, "attvalue", {"for": str(i), "value": _gexf_value(n[name])})
    merged: dict[tuple[str, str], list] = {}
    for e in data["edges"]:
        entry = merged.setdefault((e["from"], e["to"]), [set(), 0])
        entry[0].add(e["kind"])
        entry[1] += e["count"]
    edges = ET.SubElement(graph, "edges")
    for i, ((a, b), (kinds, count)) in enumerate(sorted(merged.items())):
        edge = ET.SubElement(edges, "edge", {"id": f"e{i}", "source": ids[a], "target": ids[b],
                                             "weight": str(count)})
        values = ET.SubElement(edge, "attvalues")
        ET.SubElement(values, "attvalue", {"for": "0", "value": ",".join(sorted(kinds))})
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# -- Mermaid and DOT -----------------------------------------------------------------------
def _mermaid_text(text: str) -> str:
    """Mermaid entity codes, `#` first so the others are not escaped twice."""
    for raw, code in (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;"), ("\n", " ")):
        text = text.replace(raw, code)
    return text


def _edge_label(e: dict) -> str:
    return e["kind"] if e["count"] == 1 else f"{e['kind']} x{e['count']}"


def to_mermaid(data: dict) -> str:
    ids = {n["id"]: f"n{i}" for i, n in enumerate(data["nodes"])}
    lines = ["flowchart LR"]
    lines += [f'  {ids[n["id"]]}["{_mermaid_text(n["id"])}"]' for n in data["nodes"]]
    lines += [f'  {ids[e["from"]]} -->|{_edge_label(e)}| {ids[e["to"]]}' for e in data["edges"]]
    return "\n".join(lines) + "\n"


def _dot_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def to_dot(data: dict) -> str:
    ids = {n["id"]: f"n{i}" for i, n in enumerate(data["nodes"])}
    lines = ["digraph teia {", "  rankdir=LR;", "  node [shape=box];"]
    lines += [f"  {ids[n['id']]} [label={_dot_string(n['id'])}];" for n in data["nodes"]]
    lines += [f"  {ids[e['from']]} -> {ids[e['to']]} [label={_dot_string(_edge_label(e))}, weight={e['count']}];"
              for e in data["edges"]]
    lines.append("}")
    return "\n".join(lines) + "\n"
