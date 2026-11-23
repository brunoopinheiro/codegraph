import json
import re
import webbrowser
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from conftest import make_repo, runner

from codegraph import export, view
from codegraph.cli import main

EXPORT_FILES = {
    "codegraph.toml": 'registry = ["src/registry.ts"]\nentrypoints = ["src/main.ts"]\n',
    "src/core/util.ts": "export const util = 1;\nexport type T = number;\n",
    "src/core/math.ts": "import { util } from './util';\nimport type { T } from './util';\n"
                        "export const two: T = util + 1;\n",
    "src/core/index.ts": "export * from './math';\n",
    "src/ui/view.ts": "import { two } from '../core';\nimport { util } from '../core/util';\n"
                      "export const view = two + util;\n",
    "src/ui/lazy.ts": "export const lazy = () => import('./view');\n",
    "src/registry.ts": "export const registry = [() => import('./ui/view')];\n",
    "src/main.ts": "import { registry } from './registry';\nconsole.log(registry);\n",
    "src/ui/view.test.ts": "import { view } from './view';\nconsole.log(view);\n",
}

DEFAULT_EDGES = {
    ("src/core/math.ts", "src/core/util.ts", "static", 1),
    ("src/core/index.ts", "src/core/math.ts", "star", 1),
    ("src/ui/view.ts", "src/core/index.ts", "static", 1),
    ("src/ui/view.ts", "src/core/util.ts", "static", 1),
    ("src/registry.ts", "src/ui/view.ts", "registry", 1),
    ("src/main.ts", "src/registry.ts", "static", 1),
}


@pytest.fixture
def ex_repo(tmp_path):
    return make_repo(tmp_path, EXPORT_FILES)


@pytest.fixture
def ex(ex_repo, capsys):
    return runner(ex_repo, capsys)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a browser")
    monkeypatch.setattr(webbrowser, "open", refuse)


def raw(repo, capsys, *args):
    """Run the CLI and return its stdout as text."""
    capsys.readouterr()
    main([*args, "--root", str(repo)])
    return capsys.readouterr().out


def edge_set(data):
    return {(e["from"], e["to"], e["kind"], e["count"]) for e in data["edges"]}


# -- export json ---------------------------------------------------------------------------
def test_export_json_shape_and_counts(ex):
    data = ex("export")
    assert data["kinds"] == ["reexport", "registry", "star", "static"]
    assert data["collapsed"] is False and data["group_depth"] == 3 and data["include_tests"] is False
    nodes = {n["id"]: n for n in data["nodes"]}
    assert set(nodes) == {"src/core/util.ts", "src/core/math.ts", "src/core/index.ts", "src/ui/view.ts",
                          "src/ui/lazy.ts", "src/registry.ts", "src/main.ts"}
    assert edge_set(data) == DEFAULT_EDGES
    assert data["counts"] == {"nodes": 7, "edges": 6, "imports": 6}
    util = nodes["src/core/util.ts"]
    assert util == {"id": "src/core/util.ts", "path": "src/core/util.ts", "group": "src/core", "lang": "typescript",
                    "is_test": False, "is_registry": False, "is_entrypoint": False, "in_degree": 2,
                    "out_degree": 0}
    assert nodes["src/registry.ts"]["is_registry"] and nodes["src/registry.ts"]["group"] == "src"
    assert nodes["src/main.ts"]["is_entrypoint"]
    assert (nodes["src/ui/view.ts"]["in_degree"], nodes["src/ui/view.ts"]["out_degree"]) == (1, 2)
    assert (nodes["src/ui/lazy.ts"]["in_degree"], nodes["src/ui/lazy.ts"]["out_degree"]) == (0, 0)


def test_export_kinds(ex):
    data = ex("export", "--kinds", "static,type,dynamic,reexport,star,registry")
    assert edge_set(data) == DEFAULT_EDGES | {("src/core/math.ts", "src/core/util.ts", "type", 1),
                                              ("src/ui/lazy.ts", "src/ui/view.ts", "dynamic", 1)}
    util = next(n for n in data["nodes"] if n["id"] == "src/core/util.ts")
    assert util["in_degree"] == 2  # distinct importers, not edges per kind
    only_static = ex("export", "--kinds", "static")
    assert {e["kind"] for e in only_static["edges"]} == {"static"} and len(only_static["edges"]) == 4
    with pytest.raises(SystemExit):
        ex("export", "--kinds", "static,bogus")


def test_export_path_filter(ex):
    data = ex("export", "--path", "src/core/**")
    assert {n["id"] for n in data["nodes"]} == {"src/core/util.ts", "src/core/math.ts", "src/core/index.ts"}
    assert edge_set(data) == {("src/core/math.ts", "src/core/util.ts", "static", 1),
                              ("src/core/index.ts", "src/core/math.ts", "star", 1)}
    two = ex("export", "--path", "src/core/util.ts", "--path", "src/ui/*")
    assert {n["id"] for n in two["nodes"]} == {"src/core/util.ts", "src/ui/view.ts", "src/ui/lazy.ts"}
    assert edge_set(two) == {("src/ui/view.ts", "src/core/util.ts", "static", 1)}


def test_export_collapse(ex):
    data = ex("export", "--collapse")
    nodes = {n["id"]: n for n in data["nodes"]}
    assert set(nodes) == {"src", "src/core", "src/ui"}
    assert nodes["src/core"]["files"] == 3 and nodes["src/ui"]["files"] == 2 and nodes["src"]["files"] == 2
    assert nodes["src"]["is_registry"] and nodes["src"]["is_entrypoint"] and not nodes["src/core"]["is_registry"]
    # Edges inside a group are dropped; the two ui -> core imports are summed.
    assert edge_set(data) == {("src/ui", "src/core", "static", 2), ("src", "src/ui", "registry", 1)}
    assert nodes["src/core"]["in_degree"] == 1 and nodes["src/ui"]["out_degree"] == 1
    flat = ex("export", "--collapse", "--group-depth", "1")
    assert [n["id"] for n in flat["nodes"]] == ["src"] and flat["edges"] == []


def test_export_hides_tests_unless_included(ex):
    assert "src/ui/view.test.ts" not in {n["id"] for n in ex("export")["nodes"]}
    data = ex("export", "--include-tests")
    nodes = {n["id"]: n for n in data["nodes"]}
    assert nodes["src/ui/view.test.ts"]["is_test"] is True
    assert ("src/ui/view.test.ts", "src/ui/view.ts", "static", 1) in edge_set(data)
    assert data["include_tests"] is True and data["counts"]["nodes"] == 8


def test_export_out_file(ex_repo, ex, capsys):
    target = ex_repo / "out" / "graph.json"
    capsys.readouterr()
    main(["export", "--out", str(target), "--root", str(ex_repo)])
    captured = capsys.readouterr()
    assert captured.out == "" and "wrote 7 node(s) and 6 edge(s)" in captured.err
    assert edge_set(json.loads(target.read_text(encoding="utf-8"))) == DEFAULT_EDGES


# -- gexf, mermaid, dot --------------------------------------------------------------------
def test_export_gexf_parses(ex_repo, ex, capsys):
    text = raw(ex_repo, capsys, "export", "--format", "gexf", "--kinds", "static,type")
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(text.encode("utf-8"))
    ns = {"g": export.GEXF_NS}
    assert root.tag == f"{{{export.GEXF_NS}}}gexf" and root.get("version") == "1.2"
    nodes = root.findall("g:graph/g:nodes/g:node", ns)
    edges = root.findall("g:graph/g:edges/g:edge", ns)
    assert len(nodes) == 7
    # static + type between math.ts and util.ts are merged into one edge of weight 2.
    assert len(edges) == 4
    labels = {n.get("id"): n.get("label") for n in nodes}
    merged = [e for e in edges if labels[e.get("source")] == "src/core/math.ts"]
    assert len(merged) == 1 and merged[0].get("weight") == "2"
    assert merged[0].find("g:attvalues/g:attvalue", ns).get("value") == "static,type"
    titles = {a.get("title") for a in root.findall("g:graph/g:attributes[@class='node']/g:attribute", ns)}
    assert {"group", "in_degree", "out_degree", "is_test"} <= titles and "files" not in titles


def test_export_mermaid_and_dot(ex_repo, ex, capsys):
    mermaid = raw(ex_repo, capsys, "export", "--format", "mermaid", "--collapse")
    assert mermaid.splitlines()[0] == "flowchart LR"
    assert '["src/core"]' in mermaid and "-->|static x2|" in mermaid and "-->|registry|" in mermaid
    dot = raw(ex_repo, capsys, "export", "--format", "dot", "--path", "src/core/**")
    assert dot.startswith("digraph codegraph {") and dot.rstrip().endswith("}")
    assert '[label="src/core/util.ts"]' in dot and dot.count(" -> ") == 2


def test_diagram_text_is_escaped():
    data = {"collapsed": False, "nodes": [{"id": 'a"b<c>#d.ts'}, {"id": "x\\y.ts"}],
            "edges": [{"from": 'a"b<c>#d.ts', "to": "x\\y.ts", "kind": "static", "count": 1}]}
    mermaid = export.to_mermaid(data)
    assert 'n0["a#quot;b#lt;c#gt;#35;d.ts"]' in mermaid
    dot = export.to_dot(data)
    assert 'n0 [label="a\\"b<c>#d.ts"];' in dot and 'n1 [label="x\\\\y.ts"];' in dot


def test_diagram_refuses_large_graphs(ex_repo, ex, capsys, monkeypatch):
    monkeypatch.setattr(export, "MAX_DIAGRAM_NODES", 3)
    for fmt in ("mermaid", "dot"):
        with pytest.raises(SystemExit) as exc:
            raw(ex_repo, capsys, "export", "--format", fmt)
        assert "too many" in str(exc.value) and "--collapse" in str(exc.value)
    assert "flowchart LR" in raw(ex_repo, capsys, "export", "--format", "mermaid", "--collapse")
    assert raw(ex_repo, capsys, "export", "--format", "gexf").startswith("<?xml")  # gexf has no limit


# -- view ----------------------------------------------------------------------------------
def embedded(page):
    match = re.search(r'<script id="codegraph-data" type="application/json">(.*?)</script>', page, re.S)
    assert match, "no embedded data"
    return match.group(1)


def test_view_writes_html_with_embedded_data(ex_repo, ex, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(view, "VENDOR", tmp_path / "no-vendor")
    out = tmp_path / "page" / "graph.html"
    data = ex("view", "--no-open", "--out", str(out))
    assert data["out"] == str(out) and data["opened"] is False and data["libraries"] == "cdn"
    assert (data["files"], data["groups"], data["edges"]) == (7, 3, 6)
    page = out.read_text(encoding="utf-8")
    assert "<!--CG:" not in page
    payload = json.loads(embedded(page))
    assert payload["files"] == ex("export") and payload["groups"] == ex("export", "--collapse")
    for _, url in view.LIBRARIES:
        assert f'<script src="{url}"' in page
    assert "renderer" in page  # app.js is inlined


def test_view_default_out_and_filters(ex_repo, ex, monkeypatch, tmp_path):
    monkeypatch.setattr(view, "VENDOR", tmp_path / "no-vendor")
    data = ex("view", "--no-open", "--path", "src/core/**", "--kinds", "static")
    assert Path(data["out"]).resolve() == (ex_repo / ".codegraph" / "view.html").resolve()
    payload = json.loads(embedded((ex_repo / ".codegraph" / "view.html").read_text(encoding="utf-8")))
    assert payload["files"]["kinds"] == ["static"] and payload["files"]["counts"]["nodes"] == 3


def test_view_reports_library_mode_on_stderr(ex_repo, ex, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(view, "VENDOR", tmp_path / "no-vendor")
    capsys.readouterr()
    main(["view", "--no-open", "--root", str(ex_repo)])
    captured = capsys.readouterr()
    assert "CDN" in captured.err and "libraries: cdn (not opened)" in captured.out


def test_view_inlines_vendored_libraries(ex_repo, ex, capsys, tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    for name, _ in view.LIBRARIES:
        (vendor / name).write_text(f"window.marker_{name.split('.')[0].replace('-', '_')} = '</script>';\n",
                                   encoding="utf-8")
    monkeypatch.setattr(view, "VENDOR", vendor)
    out = tmp_path / "inline.html"
    capsys.readouterr()
    main(["view", "--no-open", "--out", str(out), "--root", str(ex_repo)])
    assert "offline" in capsys.readouterr().err
    page = out.read_text(encoding="utf-8")
    assert "window.marker_graphology = '<\\/script>';" in page
    assert "window.marker_sigma" in page and "window.marker_graphology_library" in page
    assert "cdn.jsdelivr.net" not in page


def test_view_cdn_when_vendor_incomplete(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / view.LIBRARIES[0][0]).write_text("window.x = 1;\n", encoding="utf-8")
    tags, mode = view.library_tags(vendor)
    assert mode == "cdn" and tags.count("<script src=") == len(view.LIBRARIES)


def test_view_embedding_is_script_safe(tmp_path):
    nasty = 'src/</script><script>alert(1)</script>&<!--x.ts'
    data = {"title": "t</title>", "files": {"nodes": [{"id": nasty, "label": "<b>"}], "edges": []}, "groups": {}}
    page, _ = view.render_html(data, vendor=tmp_path)
    raw_json = embedded(page)
    assert "<" not in raw_json and ">" not in raw_json
    assert json.loads(raw_json) == data
    assert "<title>codegraph: t&lt;/title&gt;</title>" in page


def test_view_assets_ship_with_the_package():
    for name in ("template.html", "app.js", "app.css", "vendor/README.md"):
        assert (view.ASSETS / name).is_file(), name
    readme = (view.ASSETS / "vendor" / "README.md").read_text(encoding="utf-8")
    for name, url in view.LIBRARIES:
        assert name in readme and url in readme
