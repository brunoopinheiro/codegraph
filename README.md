# codegraph

`codegraph` indexes a repository into a SQLite symbol graph and answers structural
questions about it: who references a symbol, what a change can break, how two files
are connected, where the import cycles are and which exports nobody uses.

It is deterministic: no network calls and no LLM. It supports TypeScript / TSX /
JavaScript and Python, and is built to take more languages without changing the core.

## Install

```sh
uv tool install <path-or-git-url-of-this-repo>   # puts `codegraph` on PATH
# or, without installing:
uvx --from <path-or-git-url-of-this-repo> codegraph --help
```

Or install the wheel from a release:

```sh
gh release download v0.1.0 --repo <owner>/codegraph --pattern "*.whl"
pipx install ./codegraph-0.1.0-py3-none-any.whl
```

Requires Python 3.11+ and `git` on PATH (files are listed with `git ls-files`).

### Releasing

Bump `version` in `pyproject.toml`, commit, then push a matching tag (`v0.1.0` for
version `0.1.0`). The `Release` workflow checks that the tag matches the version, runs the
tests, builds the wheel and sdist with `uv build` and attaches them to a GitHub release.

## Quick start

```sh
cd path/to/repo
codegraph index          # first run parses everything; later runs only what changed
codegraph stats          # check resolution health before trusting answers
codegraph refs isoDate
codegraph impact src/lib/date.ts
```

The database is `<repo>/.codegraph/graph.db`. The folder carries its own `.gitignore`,
so nothing needs to be added to the repo's ignore rules.

## Commands

Every command accepts `--root <repo>` (default: the git top level of the current
directory), `--db <path>`, `--json` and `--include-tests`, before or after the
subcommand.

| Command | What it answers |
|---|---|
| `index [--full]` | Build or update the graph. `--full` discards the database first. |
| `refs <symbol>` | Definitions and every reference (`call`, `jsx`, `use`, `type`) with the enclosing top-level symbol. |
| `callers <symbol>` | Who calls or renders the symbol, grouped by caller. |
| `callees <symbol>` | What the symbol calls or renders. |
| `impact <file\|symbol>` | Reverse transitive closure: files that may break if the target changes, with depth. |
| `path <a> <b>` | Shortest import path from `a` to `b` (files or symbols), with edge kinds and lines. |
| `cycles [--kinds k1,k2]` | Import cycles (strongly connected components) and a shortest example loop. |
| `unused-exports` | Exported symbols that nothing uses. |
| `stats` | Counts, edge status and the top 10 causes of `unresolved` / `ambiguous` edges. |

A symbol is a bare name (`isoDate`) or `path:name` (`src/lib/format.ts:isoDate`)
when the name is defined in more than one file. Paths are relative to the repo root.

By default test files and `mock` edges are left out of `refs`, `callers`, `impact`,
`path`, `cycles` and `unused-exports`. `--include-tests` adds them.

`cycles` follows `static`, `reexport`, `star` and `registry` edges by default.
Type-only imports and lazy dynamic imports don't create runtime cycles; add them
with `--kinds static,reexport,star,registry,type,dynamic`.

## Configuration: `codegraph.toml` (optional, repo root)

```toml
include = ["src/**"]                   # globs; default: every file git lists
exclude = ["**/*.generated.ts"]
registry = ["src/modules/registry.ts"] # imports in these files get kind=registry
entrypoints = ["src/main.tsx"]         # exports here are never "unused"
tests = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test_*.py"]
source_roots = ["src"]                 # Python import roots (default: ".", "src", "lib")
extensions = [".ts", ".tsx", ".js"]    # TS resolution order (default covers ts/tsx/d.ts/js/jsx/mjs/cjs)

[aliases]                              # extra aliases, same syntax as tsconfig "paths"
"~/*" = ["src/*"]
```

Changing this file triggers a full re-index on the next `codegraph index`.
Changes to `tsconfig*.json`, `jsconfig.json`, `package.json` or
`pnpm-workspace.yaml` trigger a re-link without re-parsing.

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

`skills/codegraph/SKILL.md` is a Claude Code skill. It covers setting up `codegraph`
in a repo (including writing `codegraph.toml` until `stats` is clean) and using it
to answer structural questions. To enable it in a repo, copy the folder to
`<repo>/.claude/skills/codegraph/`.

## Development

```sh
uv sync
uv run pytest
uv run codegraph --root path/to/repo index
```

Layout: `scan.py` (files and hashes), `parse.py` (parallel parsing), `languages/`
(adapters), `resolve.py` (TS module resolution), `link.py` (re-exports and refs),
`db.py` (SQLite), `cli.py` (commands), `config.py` (`codegraph.toml`).

### Adding a language

Add a module under `codegraph/languages/` that implements `LanguageAdapter`
(`languages/base.py`):
- `extensions`;
- `parse()`, which returns symbols, imports, exports, refs and unresolved items;
- `make_resolver()`, whose resolver returns `internal` / `asset` / `external` / `unresolved`;
- `explicit_exports`.

Then register the adapter in `languages/__init__.py`. Scan, link, db and cli need no change.
