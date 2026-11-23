from conftest import query as edges

from codegraph.config import glob_match
from codegraph.resolve import match_paths


def test_alias_and_barrel_chain_reach_definition(cg, repo):
    data = cg("refs", "isoDate")
    assert [d["path"] for d in data["definitions"]] == ["src/lib/date.ts"]
    assert [(r["path"], r["line"], r["kind"], r["scope"]) for r in data["refs"]] == [
        ("src/app.tsx", 4, "call", "App")]
    imp = edges(repo, "SELECT i.status, s.name, f.path FROM imports i JOIN symbols s ON s.id=i.target_symbol_id "
                      "JOIN files f ON f.id=s.file_id WHERE i.spec='@/index'")
    assert imp == [{"status": "resolved", "name": "isoDate", "path": "src/lib/date.ts"}]


def test_ambiguous_reexport_is_not_linked(cg, repo):
    imp = edges(repo, "SELECT status, target_symbol_id FROM imports WHERE spec='./amb'")
    assert imp == [{"status": "ambiguous", "target_symbol_id": None}]
    assert cg("stats")["ambiguous"] >= 1


def test_cycle_detected(cg):
    cycles = cg("cycles")["cycles"]
    assert {"files": ["src/cycle/x.ts", "src/cycle/y.ts"], "example": ["src/cycle/x.ts", "src/cycle/y.ts",
                                                                     "src/cycle/x.ts"]} in cycles
    assert len(cycles) == 1


def test_dynamic_import_literal_and_non_literal(cg, repo):
    dyn = edges(repo, "SELECT i.kind, t.path FROM imports i JOIN files f ON f.id=i.file_id "
                      "JOIN files t ON t.id=i.target_file_id WHERE f.path='src/app.tsx' AND i.kind='dynamic'")
    assert dyn == [{"kind": "dynamic", "path": "src/pages/page.tsx"}]
    bad = edges(repo, "SELECT u.line, u.reason FROM unresolved u JOIN files f ON f.id=u.file_id "
                      "WHERE f.path='src/app.tsx'")
    assert bad == [{"line": 7, "reason": "dynamic-non-literal"}]


def test_registry_imports_are_marked(cg, repo):
    reg = edges(repo, "SELECT i.kind FROM imports i JOIN files f ON f.id=i.file_id WHERE f.path='src/registry.ts'")
    assert reg == [{"kind": "registry"}]


def test_mocks_hidden_unless_include_tests(cg, repo):
    assert edges(repo, "SELECT kind, status FROM imports WHERE kind='mock'") == [{"kind": "mock", "status": "resolved"}]
    with_tests = cg("refs", "isoDate", "--include-tests")["refs"]
    assert {(r["path"], r["kind"]) for r in with_tests} == {
        ("src/app.tsx", "call"), ("src/app.test.ts", "call"), ("src/app.test.ts", "mock")}
    assert "src/app.test.ts" not in [f["path"] for f in cg("impact", "src/lib/date.ts")["files"]]
    assert "src/app.test.ts" in [f["path"] for f in cg("impact", "src/lib/date.ts", "--include-tests")["files"]]


def test_import_type_counts_for_impact(cg, repo):
    assert edges(repo, "SELECT kind FROM imports WHERE spec='@/types'") == [{"kind": "type"}]
    assert [f["path"] for f in cg("impact", "Model")["files"]] == ["src/app.tsx"]
    assert [f["path"] for f in cg("impact", "src/types.ts")["files"]] == ["src/app.tsx"]


def test_impact_of_file_is_transitive_through_barrels(cg):
    files = {f["path"]: f["depth"] for f in cg("impact", "src/lib/date.ts")["files"]}
    assert files == {"src/lib/index.ts": 1, "src/index.ts": 2, "src/app.tsx": 3}


def test_callers_callees_path(cg):
    assert cg("callers", "isoDate")["callers"] == [{"path": "src/app.tsx", "scope": "App", "calls": 1}]
    assert [c["name"] for c in cg("callees", "App")["callees"]] == ["isoDate"]
    hops = cg("path", "src/app.tsx", "src/lib/date.ts")["path"]
    assert [h["to"] for h in hops] == ["src/index.ts", "src/lib/index.ts", "src/lib/date.ts"]


def test_unused_exports(cg):
    names = {u["name"] for u in cg("unused-exports")["unused"]}
    assert "unusedThing" in names
    assert "isoDate" not in names and "Page" not in names  # Page is used through dynamic import


def test_incremental_reindex(cg, repo):
    assert cg("index")["parsed"] == 0
    (repo / "src/b.ts").write_text("export const other = 2;\n", encoding="utf-8")
    assert cg("index")["parsed"] == 1
    assert edges(repo, "SELECT status FROM imports WHERE spec='./amb'") == [{"status": "resolved"}]


def test_helpers():
    assert match_paths("@/lib/x", {"@/*": ["src/*"], "@/lib/*": ["lib/*"]}, "") == ["lib/x"]
    assert glob_match("src/a/b.test.ts", "**/*.test.*") and glob_match("x.test.ts", "**/*.test.*")
    assert not glob_match("src/a.ts", "**/*.test.*")
