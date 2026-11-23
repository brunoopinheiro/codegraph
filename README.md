# teia

**T**racing **E**xports, **I**mports and **A**liases. *Teia* is Portuguese for web.

`teia` indexes a repository into a SQLite symbol graph and answers structural
questions about it: who references a symbol, what a change can break, how two files
are connected, where the import cycles are and which exports nobody uses.

It is deterministic: no network calls and no LLM. It supports TypeScript / TSX /
JavaScript and Python, and is built to take more languages without changing the core.

## Install

```sh
uv tool install <path-or-git-url-of-this-repo>   # puts `teia` on PATH
# or, without installing:
uvx --from <path-or-git-url-of-this-repo> teia --help
```

Or install the wheel from a release:

download `teia-<version>-py3-none-any.whl` from the release page and point pipx at it:

```sh
pipx install ~/Downloads/teia-0.1.0-py3-none-any.whl
```

To upgrade, download the new wheel and run `pipx install --force <wheel>`.

Requires Python 3.11+ and `git` on PATH (files are listed with `git ls-files`).

### Releasing

Bump `version` in `pyproject.toml`, commit, then push a matching tag (`v0.1.0` for
version `0.1.0`). The `Release` workflow checks that the tag matches the version, runs the
tests, builds the wheel and sdist with `uv build` and attaches them to a GitHub release.

## Quick start

```sh
cd path/to/repo
teia index          # first run parses everything; later runs only what changed
teia stats          # check resolution health before trusting answers
teia refs isoDate
teia impact src/lib/date.ts
```

The database is `<repo>/.teia/graph.db`. The folder carries its own `.gitignore`,
so nothing needs to be added to the repo's ignore rules.

## Commands

Every command accepts `--root <repo>` (default: the git top level of the current
directory), `--db <path>`, `--json` and `--include-tests`, before or after the
subcommand.

| Command | What it answers |
|---|---|
| `index [--full]` | Build or update the graph. `--full` discards the database first. Reports whether anything changed (`up_to_date`), when the index last changed and the commit it was built at. |
| `refs <symbol>` | Definitions and every reference (`call`, `jsx`, `use`, `type`) with the enclosing top-level symbol. |
| `callers <symbol>` | Who calls or renders the symbol, grouped by caller. |
| `callees <symbol>` | What the symbol calls or renders. |
| `impact <file\|symbol> [--depth N] [--through-registry]` | Reverse transitive closure: files that may break if the target changes, with depth and the edge kind each was reached by. |
| `path <a> <b>` | Shortest import path from `a` to `b` (files or symbols), with edge kinds and lines. |
| `cycles [--kinds k1,k2] [--members] [--edges]` | Import cycles (strongly connected components), a shortest example loop and the edge kinds holding each one together. |
| `unused-exports [--path GLOB] [--kind K] [--class C] [--limit N] [--summary]` | Exported symbols that nothing outside their file (or outside tests) uses, classified `dead`, `local-only` or `test-only`. |
| `stats` | Counts, edge status, the top 10 causes of `unresolved` / `ambiguous` edges, the `teia.toml` in effect (files matched by each `registry` / `entrypoints` / `tests` pattern) and the age/commit of the index. |
| `export [--format json\|gexf\|mermaid\|dot] [--out FILE] [--kinds K] [--path GLOB] [--group-depth N] [--collapse]` | The file-level import graph (files and resolved imports) for other tools: JSON, GEXF for Gephi, Mermaid or Graphviz DOT. |
| `view [--out FILE] [--no-open] [--kinds K] [--path GLOB] [--group-depth N]` | Writes one self-contained HTML page with an interactive view of the file-level import graph and opens it in the browser. |

A symbol is a bare name (`isoDate`) or `path:name` (`src/lib/format.ts:isoDate`)
when the name is defined in more than one file. Paths are relative to the repo root.

By default test files and `mock` edges are left out of `refs`, `callers`, `impact`,
`path`, `cycles` and `unused-exports`. `--include-tests` adds them.

`cycles` follows `static`, `reexport`, `star` and `registry` edges by default.
Type-only imports and lazy dynamic imports don't create runtime cycles; add them
with `--kinds static,reexport,star,registry,type,dynamic`.

### `impact`

- `--depth N` follows importers at most N levels.
- **Registry boundary.** A file listed under `registry` imports many modules lazily, so
  walking past it reaches everything the app wires together. By default `impact`
  reports a registry file that imports something impacted (`via: "registry"`) but does
  not continue to what imports the registry. The text output says how many registry
  files it stopped at, and `--json` lists them in `registry_stopped`. Pass
  `--through-registry` for the full traversal.
- `--json` files look like `{"path", "depth", "via", "from"}`: `via` is the edge kind it
  was reached by (`static`, `type`, `dynamic`, `registry`, `reexport`, `star`, `mock`, or
  `ref` when a file references a target symbol directly) and `from` the file it was
  reached from. `depth_counts` has the number of files per depth; the text output ends
  with the same counts, e.g. `12 file(s) impacted (depth 1: 3, depth 2: 9)`.

### `cycles`

Each cycle in `--json` has `files` (every member), `example` (one loop), `edge_kinds`
(edges inside the component, per kind), `held_by` (kinds whose removal breaks the cycle
entirely, so those kinds are needed to hold it together) and `needs_registry`. A cycle
that exists only because of `registry` edges is not a static import cycle. `--members`
prints every member in text output; `--edges` adds the `from` / `to` / `kind` / `line` of
each edge inside the component (text and JSON).

### `unused-exports`

An export is not reported when a non-test file uses it from another file, when a
module that imports it as a whole (side-effect, dynamic or registry import) can reach it,
or when it is in an `entrypoints` file. Everything else is classified:

| Class | Meaning |
|---|---|
| `dead` | No reference at all. |
| `local-only` | Used only inside its own file; the `export` keyword is unnecessary. (Python has no `export`, so a use in the same file counts as a use and is not reported.) |
| `test-only` | Used from test files only (also through a barrel). Not dead code. |

A symbol used both locally and from tests is `test-only`. Tests are always read, which is
what separates `test-only` from `dead`. By default `test-only` entries are listed. With
`--include-tests` a test using an export counts as a use, so `test-only` entries disappear.

Filters: `--path GLOB` (repeatable, matched against the repo-relative path, e.g.
`'src/modules/guest/**'`), `--kind function,component` (comma separated: `function`,
`component`, `type`, `interface`, `const`, `class`, `enum`, `variable`, ...) and
`--class dead,local-only` (comma separated). `--limit N` lists the first N entries by path.
`--summary` prints counts per class, kind and directory (the first `--group-depth`
levels, default 3) without listing entries. `--json` always has `total` (after filters,
before `--limit`), `shown` and `unused` entries `{"path", "name", "kind", "line", "class"}`;
with `--summary` it adds `summary` and `unused` is empty.

### `export` and `view`

Both work on the **file-level** import graph, not on symbols: nodes are indexed files,
edges are resolved file-to-file imports and re-exports, aggregated per (from, to, kind)
with a `count` of import statements. `view` is for humans; the `--json` commands above
remain the interface for scripts and LLM agents.

Shared options:
- `--kinds a,b` edge kinds to include (default `static,reexport,star,registry`, the same
  runtime kinds `cycles` follows; add `type`, `dynamic`, or `mock` with `--include-tests`).
- `--path GLOB` (repeatable) keeps only matching files and the edges among them.
- `--group-depth N` (default 3) the leading directory levels that form a node's `group`
  (`src/modules/guest/x.ts` is in `src/modules/guest`; root files are in `.`).
- Test files are left out unless `--include-tests` is given.

`export --format` (default `json`) prints to stdout or writes `--out FILE`:
- `json`: `nodes` (`id`, `path`, `group`, `lang`, `is_test`, `is_registry`,
  `is_entrypoint`, `in_degree`, `out_degree`; degrees count distinct neighbours),
  `edges` (`from`, `to`, `kind`, `count`), `counts`, and the options used.
- `gexf`: GEXF 1.2 for Gephi, with node attributes (group, flags, degrees) and edge
  `kind` and `weight`. Edges of several kinds between the same two files become one edge
  (Gephi would merge them anyway): `kind` lists the kinds, `weight` sums the counts.
- `mermaid` / `dot`: diagram text. These are unreadable for large graphs, so they refuse
  more than 250 nodes; narrow with `--collapse` (and a smaller `--group-depth`) or `--path`.
- `--collapse` makes one node per group (with a `files` count) and aggregates the edges
  between groups; edges inside a group are dropped.

`view` writes `<root>/.teia/view.html` (or `--out FILE`) and opens it unless
`--no-open`. The page is a single file that works from `file://`: the graph data is
embedded, rendering uses sigma.js, graphology and ForceAtlas2. Nodes are coloured by
group (click a legend entry to hide or show a group) and sized by in-degree. You can search
a file by substring, toggle edge kinds, click a node to highlight it and its direct
neighbours and see its imports and importers, and switch between one node per group and
all files (the page starts collapsed above 400 files). The libraries are inlined when
their files are in the package's `teia/view/vendor/` (works offline; see the README
there for the pinned versions and URLs); otherwise the page loads them from jsDelivr and
needs a network connection. `view` says on stderr which mode it used.

## Configuration: `teia.toml` (optional, repo root)

Complete example, with every supported key. All paths are relative to the repo root and
use `/`. Keys marked "globs" take glob patterns: `*` and `?` stay inside one path segment,
`**/` matches zero or more directories, `**` matches anything.

```toml
include = ["src/**"]                   # globs. Only index files matching one. Default: every file git lists
exclude = ["**/*.generated.ts"]        # globs. Skip files matching one
registry = ["src/modules/registry.ts"] # globs. Imports in these files get kind=registry (lazy wiring files)
entrypoints = ["src/main.tsx"]         # globs. Exports in these files are never reported by unused-exports
tests = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test_*.py"]  # globs. Replaces the defaults
source_roots = ["src"]                 # plain directories (no globs). Python import roots, default ".", "src", "lib"
extensions = [".ts", ".tsx", ".js"]    # plain suffixes (no globs). TS resolution order, default covers ts/tsx/d.ts/js/jsx/mjs/cjs

[aliases]                              # keys are tsconfig-style patterns ("@/*"), values a string or list of targets
"~/*" = ["src/*"]
```

`include`, `exclude`, `registry`, `entrypoints` and `tests` are globs; `source_roots`,
`extensions` and `[aliases]` are not. The default `tests` are `**/*.test.*`, `**/*.spec.*`,
`**/__tests__/**`, `**/__mocks__/**`, `test/**`, `tests/**`, `e2e/**`, `**/test_*.py`,
`**/*_test.py` and `**/conftest.py`. Setting `tests` replaces them.

`teia index` prints a warning on stderr for an unknown top-level key (with a
"did you mean" hint) and for a value of the wrong type; the offending entry is ignored.
`teia stats` shows the configuration in effect: how many indexed files each
`registry`, `entrypoints` and `tests` pattern matched (`config` in `--json`). A pattern
from `teia.toml` that matches 0 files is flagged `<- matches nothing`, which usually
means a typo or a wrong path. The built-in default patterns are not flagged.

Changing this file triggers a full re-index on the next `teia index`.
Changes to `tsconfig*.json`, `jsconfig.json`, `package.json` or
`pnpm-workspace.yaml` trigger a re-link without re-parsing.

## Is the index current?

`teia index` reports `parsed=0` when nothing changed. `up_to_date` says so
explicitly, and `updated_at` (UTC), `age_seconds` and `commit` give the time the stored
graph last changed and the git HEAD at that time; `head` and `commit_matches_head`
compare it with the current HEAD (`null` when the repo has no commit). The index is
built from the working tree, so uncommitted edits are included once you run `index`;
`commit` is the base commit, not a snapshot of it. `stats` shows the same `index` block.

## How resolution works

**TypeScript / JavaScript** (tree-sitter)
- Uses the nearest `tsconfig.json` / `jsconfig.json` with its `paths` and `baseUrl`. It follows `extends` chains (relative or from `node_modules`). For solution-style configs it falls back to the `references`.
- Tries the configured extension order, then `index.*`. An ESM `./x.js` specifier also matches `x.ts`.
- Workspace packages come from `package.json` `workspaces` or `pnpm-workspace.yaml`. They resolve through `source`/`types`/`module`/`main`/`exports`, with `dist/` mapped to `src/`.
- Bare specifiers that are not workspaces are `external` (`node_modules`).
- Existing non-code files (CSS, SVG, JSON…) are `asset`. Bundler queries such as `?raw` are ignored.

**Python** (stdlib `ast`)
- Absolute imports resolve from the source roots, relative imports from the package.
- `from pkg import mod` points at the submodule when one exists.
- `__all__` defines the exports when present. Otherwise every public top-level name is exported, including names imported into the module (re-exports).

**Barrels and re-exports**: `export {…} from` and `export * from` (and their Python
equivalents) are followed to the real definition. When two `export *` sources provide
the same name, the edge is left unlinked and marked `ambiguous`, as TypeScript does.

## Data model

Tables: `files`, `symbols`, `imports`, `exports`, `refs`, `unresolved`.

Edge `kind`:
- `static`: a regular import.
- `type`: `import type`, or an import under Python's `if TYPE_CHECKING:`. These edges count in `impact`.
- `dynamic`: `import("literal")` or `importlib.import_module("literal")`.
- `registry`: an import inside a file listed in `registry`.
- `mock`: `vi.mock`, `jest.mock` or `mock.patch`.
- `reexport` and `star`: re-export edges.

Ref `kind`: `call`, `jsx`, `use`, `type`.

Edge `status`: `resolved`, `external`, `asset`, `unresolved`, `ambiguous`.
`unresolved` rows record file, line, spec and a reason:

| Reason | Meaning | Usual fix |
|---|---|---|
| `alias-target-not-found` | An alias matched but no file exists at its target | Fix `paths` / `[aliases]` |
| `unknown-alias` | Looks like an alias (`@/`, `~`, `#`) but nothing defines it | Add it to `[aliases]` |
| `file-not-found` | Relative import to a file git doesn't list | Broken import, or an untracked/generated file |
| `module-not-found` | Python module under a local package not found | Set `source_roots` |
| `workspace-entry-not-found` | Workspace package without a resolvable entry | Check its `package.json` fields |
| `export-not-found` | The target file has no such export | Real bug, or a generated/CommonJS module |
| `ambiguous-star-export` | Two `export *` sources provide the name | Make the barrel explicit |
| `dynamic-non-literal`, `mock-non-literal` | The module name is computed at runtime | Informational; cannot be resolved statically |
| `parse-error` | The file could not be parsed | Check the file |

## Limitations

- References are tracked for top-level symbols and imported names. Locals, object
  members and class methods are not separate symbols: a call to `obj.method()` is a
  `use` of `obj`. The ref's scope is the enclosing top-level symbol.
- A name shadowed by a nested local of the same name is still attributed to the outer binding.
- Names that arrive in Python through `from x import *` are not linked at the ref level.
  The file edge still exists.
- Computed dynamic imports and lazily imported modules whose members are accessed
  dynamically cannot be followed. Modules imported without a binding (dynamic,
  registry, side effect) count all their exports as used.

## Using it from an LLM agent

`skills/teia/SKILL.md` is a Claude Code skill. It covers setting up `teia`
in a repo (including writing `teia.toml` until `stats` is clean) and using it
to answer structural questions. To enable it in a repo, copy the folder to
`<repo>/.claude/skills/teia/`.

## Development

```sh
uv sync
uv run pytest
uv run teia --root path/to/repo index
```

Layout: `scan.py` (files and hashes), `parse.py` (parallel parsing), `languages/`
(adapters), `resolve.py` (TS module resolution), `link.py` (re-exports and refs),
`db.py` (SQLite), `cli.py` (commands), `config.py` (`teia.toml`), `export.py`
(file-level graph and its formats), `view/` (HTML viewer: `template.html`, `app.js`,
`app.css` and optional `vendor/` libraries, read with `importlib.resources`).

### Adding a language

Add a module under `teia/languages/` that implements `LanguageAdapter`
(`languages/base.py`):
- `extensions`;
- `parse()`, which returns symbols, imports, exports, refs and unresolved items;
- `make_resolver()`, whose resolver returns `internal` / `asset` / `external` / `unresolved`;
- `explicit_exports`.

Then register the adapter in `languages/__init__.py`. Scan, link, db and cli need no change.
