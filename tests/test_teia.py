import json
import os
import subprocess

import pytest
from conftest import TS_FILES, make_repo, runner
from conftest import query as edges

from teia.cli import main
from teia.config import glob_match
from teia.resolve import match_paths


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
    assert len(cycles) == 1
    assert cycles[0]["files"] == ["src/cycle/x.ts", "src/cycle/y.ts"]
    assert cycles[0]["example"] == ["src/cycle/x.ts", "src/cycle/y.ts", "src/cycle/x.ts"]
    assert cycles[0]["edge_kinds"] == {"static": 2} and not cycles[0]["needs_registry"]
    assert "edges" not in cycles[0]


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


def test_existing_assets_are_not_unresolved(cg, repo):
    rows = edges(repo, "SELECT spec, status FROM imports WHERE spec LIKE '%.css' ORDER BY spec")
    assert rows == [{"spec": "./missing.css", "status": "unresolved"}, {"spec": "./styles.css", "status": "asset"}]


def test_root_before_subcommand(repo, capsys):
    main(["--root", str(repo), "--json", "index"])
    assert json.loads(capsys.readouterr().out)["files"] == len([p for p in TS_FILES if p.endswith((".ts", ".tsx"))])


def test_helpers():
    assert match_paths("@/lib/x", {"@/*": ["src/*"], "@/lib/*": ["lib/*"]}, "") == ["lib/x"]
    assert glob_match("src/a/b.test.ts", "**/*.test.*") and glob_match("x.test.ts", "**/*.test.*")
    assert not glob_match("src/a.ts", "**/*.test.*")


# -- impact: registry boundary and depth ---------------------------------------------------
REGISTRY_FILES = {
    "teia.toml": 'registry = ["src/registry.ts"]\n',
    "src/lib/util.ts": "export const util = 1;\n",
    "src/mod/a.ts": "import { util } from '../lib/util';\nexport const a = util;\n",
    "src/mod/b.ts": "import { a } from './a';\nexport const b = a;\n",
    "src/registry.ts": "export const registry = [() => import('./mod/a')];\n",
    "src/main.ts": "import { registry } from './registry';\nconsole.log(registry);\n",
    "src/other.ts": "import { registry } from './registry';\nexport const other = registry;\n",
}


def test_impact_stops_at_registry_by_default(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, REGISTRY_FILES), capsys)
    data = cg("impact", "src/lib/util.ts")
    got = {f["path"]: (f["depth"], f["via"], f["from"]) for f in data["files"]}
    assert got == {"src/mod/a.ts": (1, "static", "src/lib/util.ts"), "src/mod/b.ts": (2, "static", "src/mod/a.ts"),
                   "src/registry.ts": (2, "registry", "src/mod/a.ts")}
    assert data["registry_stopped"] == ["src/registry.ts"]
    assert data["depth_counts"] == {"1": 1, "2": 2}


def test_impact_through_registry_opts_back_in(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, REGISTRY_FILES), capsys)
    data = cg("impact", "src/lib/util.ts", "--through-registry")
    assert {f["path"]: f["depth"] for f in data["files"]} == {
        "src/mod/a.ts": 1, "src/mod/b.ts": 2, "src/registry.ts": 2, "src/main.ts": 3, "src/other.ts": 3}
    assert data["registry_stopped"] == [] and data["through_registry"] is True


def test_impact_depth_limit(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, REGISTRY_FILES), capsys)
    assert [f["path"] for f in cg("impact", "src/lib/util.ts", "--depth", "1")["files"]] == ["src/mod/a.ts"]
    two = cg("impact", "src/lib/util.ts", "--depth", "2", "--through-registry")
    assert {f["path"] for f in two["files"]} == {"src/mod/a.ts", "src/mod/b.ts", "src/registry.ts"}
    assert two["depth_limit"] == 2


def test_impact_text_summary_counts_per_depth(tmp_path, capsys):
    repo = make_repo(tmp_path, REGISTRY_FILES)
    runner(repo, capsys)
    capsys.readouterr()
    main(["impact", "src/lib/util.ts", "--root", str(repo)])
    out = capsys.readouterr().out
    assert "3 file(s) impacted (depth 1: 1, depth 2: 2)" in out
    assert "src/registry.ts  [registry]" in out and "--through-registry" in out
    assert "src/main.ts" not in out


# -- unused-exports: classes and filters ---------------------------------------------------
UNUSED_FILES = {
    "src/lib.ts": "export function dead() {}\nexport function localOnly() {}\nexport function testOnly() {}\n"
                  "export function used() {}\nexport function wrapper() {\n  return localOnly();\n}\n",
    "src/main.ts": "import { used, wrapper } from './lib';\nused();\nwrapper();\n",
    "src/lib.test.ts": "import { testOnly } from './lib';\ntestOnly();\n",
    "src/types.ts": "export interface Shape { a: number }\n",
    "src/deep/thing.ts": "export function viaBarrel() {}\n",
    "src/deep/index.ts": "export * from './thing';\n",
    "src/deep/thing.test.ts": "import { viaBarrel } from './index';\nviaBarrel();\n",
}


def test_unused_exports_are_classified(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, UNUSED_FILES), capsys)
    data = cg("unused-exports")
    assert {u["name"]: u["class"] for u in data["unused"]} == {
        "dead": "dead", "Shape": "dead", "localOnly": "local-only", "testOnly": "test-only",
        "viaBarrel": "test-only"}
    assert data["total"] == 5 and data["shown"] == 5


def test_unused_exports_include_tests_counts_test_use(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, UNUSED_FILES), capsys)
    names = {u["name"]: u["class"] for u in cg("unused-exports", "--include-tests")["unused"]}
    assert names == {"dead": "dead", "Shape": "dead", "localOnly": "local-only"}


def test_unused_exports_filters(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, UNUSED_FILES), capsys)
    assert {u["name"] for u in cg("unused-exports", "--class", "dead")["unused"]} == {"dead", "Shape"}
    assert {u["name"] for u in cg("unused-exports", "--class", "dead,local-only")["unused"]} == {
        "dead", "Shape", "localOnly"}
    assert [u["name"] for u in cg("unused-exports", "--kind", "interface")["unused"]] == ["Shape"]
    assert [u["name"] for u in cg("unused-exports", "--path", "src/deep/**")["unused"]] == ["viaBarrel"]
    limited = cg("unused-exports", "--limit", "2")
    assert len(limited["unused"]) == 2 and limited["total"] == 5 and limited["shown"] == 2
    with pytest.raises(SystemExit):
        cg("unused-exports", "--class", "bogus")


def test_unused_exports_summary(tmp_path, capsys):
    repo = make_repo(tmp_path, UNUSED_FILES)
    cg = runner(repo, capsys)
    data = cg("unused-exports", "--summary")
    assert data["unused"] == [] and data["total"] == 5
    assert data["summary"]["by_class"] == {"dead": 2, "local-only": 1, "test-only": 2}
    assert data["summary"]["by_kind"] == {"function": 4, "interface": 1}
    assert data["summary"]["by_directory"] == {"src": 4, "src/deep": 1}
    capsys.readouterr()
    main(["unused-exports", "--summary", "--root", str(repo)])
    out = capsys.readouterr().out
    assert "by class:" in out and "src/deep" in out and "5 unused export(s)" in out
    assert "src/lib.ts" not in out


def test_unused_exports_text_shows_class(tmp_path, capsys):
    repo = make_repo(tmp_path, UNUSED_FILES)
    runner(repo, capsys)
    capsys.readouterr()
    main(["unused-exports", "--limit", "1", "--root", str(repo)])
    out = capsys.readouterr().out
    assert "[test-only]" in out or "[dead]" in out or "[local-only]" in out
    assert "5 unused export(s), showing 1" in out


# -- teia.toml: validation and stats --------------------------------------------------
def test_index_warns_on_unknown_toml_keys(tmp_path, capsys):
    repo = make_repo(tmp_path, {"teia.toml": 'registy = ["src/r.ts"]\nregistry = "src/r.ts"\n',
                                "src/r.ts": "export const r = 1;\n"})
    main(["index", "--root", str(repo), "--json"])
    err = capsys.readouterr().err
    assert "unknown key 'registy'" in err and "did you mean 'registry'" in err
    assert "'registry' should be a list" in err


def test_stats_reports_config_in_effect(tmp_path, capsys):
    cg = runner(make_repo(tmp_path, {
        "teia.toml": 'entrypoints = ["src/nope.ts", "src/main.ts"]\nregistry = ["src/reg.ts"]\n',
        "src/main.ts": "export const m = 1;\n", "src/reg.ts": "export const r = () => import('./main');\n",
        "src/main.test.ts": "export const t = 1;\n"}), capsys)
    cfg = cg("stats")["config"]
    assert cfg["file"] == "teia.toml" and cfg["warnings"] == []
    assert cfg["entrypoints"]["files"] == 1 and cfg["entrypoints"]["source"] == "toml"
    assert cfg["entrypoints"]["patterns"] == [{"pattern": "src/nope.ts", "files": 0},
                                              {"pattern": "src/main.ts", "files": 1}]
    assert cfg["registry"]["files"] == 1
    assert cfg["tests"]["source"] == "default" and cfg["tests"]["files"] == 1


def test_stats_text_flags_patterns_matching_nothing(tmp_path, capsys):
    repo = make_repo(tmp_path, {"teia.toml": 'entrypoints = ["src/nope.ts"]\n', "src/a.ts": "export const a = 1;\n"})
    runner(repo, capsys)
    capsys.readouterr()
    main(["stats", "--root", str(repo)])
    out = capsys.readouterr().out
    assert "src/nope.ts  <- matches nothing" in out
    # The default test globs match nothing here either, but they are not the user's patterns.
    assert "**/conftest.py\n" in out and out.count("<- matches nothing") == 1


# -- cycles: edge kinds --------------------------------------------------------------------
def test_cycle_edge_kinds_show_registry_dependency(tmp_path, capsys):
    repo = make_repo(tmp_path, {
        "teia.toml": 'registry = ["src/registry.ts"]\n',
        "src/page.ts": "import { r } from './registry';\nexport const p = r;\n",
        "src/registry.ts": "export const r = () => import('./page');\n"})
    cg = runner(repo, capsys)
    (cycle,) = cg("cycles", "--edges")["cycles"]
    assert cycle["edge_kinds"] == {"registry": 1, "static": 1}
    assert cycle["needs_registry"] is True and cycle["held_by"] == ["registry", "static"]
    assert {(e["from"], e["to"], e["kind"]) for e in cycle["edges"]} == {
        ("src/page.ts", "src/registry.ts", "static"), ("src/registry.ts", "src/page.ts", "registry")}
    assert cg("cycles", "--kinds", "static,reexport,star")["cycles"] == []
    capsys.readouterr()
    main(["cycles", "--members", "--root", str(repo)])
    out = capsys.readouterr().out
    assert "member src/page.ts" in out and "[needs registry edges]" in out


# -- index: age and commit -----------------------------------------------------------------
def test_index_reports_commit_and_up_to_date(tmp_path, capsys):
    repo = make_repo(tmp_path, {"src/a.ts": "export const a = 1;\n"})
    # Commit without the machine's git setup: no global or system config (signing, templates,
    # identity) and no hooks, so the test behaves the same everywhere.
    hooks = tmp_path / "no-hooks"
    hooks.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    git = ["git", "-c", f"core.hooksPath={hooks.as_posix()}", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "add", "-A"], cwd=repo, check=True, env=env)
    subprocess.run([*git, "commit", "-q", "-m", "init"], cwd=repo, check=True, env=env)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                          check=True).stdout.strip()
    cg = runner(repo, capsys)
    again = cg("index")
    assert again["parsed"] == 0 and again["up_to_date"] is True
    assert again["commit"] == head and again["commit_matches_head"] is True
    assert again["updated_at"] and again["age_seconds"] >= 0
    (repo / "src/b.ts").write_text("export const b = 1;\n", encoding="utf-8")
    assert cg("index")["up_to_date"] is False
    index = cg("stats")["index"]
    assert index["commit"] == head and index["head"] == head
    capsys.readouterr()
    main(["index", "--root", str(repo)])
    assert "up to date" in capsys.readouterr().out
