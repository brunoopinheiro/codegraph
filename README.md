# codegraph

Index a repository into a SQLite symbol graph and answer structural questions
(`refs`, `callers`, `callees`, `impact`, `path`, `cycles`, `unused-exports`, `stats`).

```sh
uv sync
uv run codegraph --root path/to/repo index
uv run codegraph --root path/to/repo refs isoDate
uv run codegraph --root path/to/repo cycles --json
```

The database lives in `<repo>/.codegraph/graph.db` (the folder ignores itself).
Re-running `index` only re-parses files whose content changed.

## Optional `codegraph.toml` (repo root)

```toml
include = ["src/**"]                  # globs; default: everything git lists
exclude = ["**/*.generated.ts"]
registry = ["src/modules/registry.ts"] # imports here get kind=registry
entrypoints = ["src/main.tsx"]         # exports here are never "unused"
tests = ["**/*.test.*", "**/*.spec.*", "**/__tests__/**", "**/test_*.py"]
source_roots = ["src"]                 # Python import roots

[aliases]                              # extra aliases, same syntax as tsconfig paths
"~/*" = ["src/*"]
```

## Edge kinds

`static`, `type` (`import type`), `dynamic` (`import("literal")`), `registry`,
`mock` (`vi.mock`/`jest.mock`, hidden unless `--include-tests`), `reexport`, `star`.
Refs have kind `call`, `jsx`, `use` or `type`.

## Languages

- **TypeScript / TSX / JavaScript** (tree-sitter): tsconfig/jsconfig `paths` + `baseUrl`
  (with `extends` chains and solution-style `references`), extension order, `index.*`,
  `.js` → `.ts` ESM specifiers, workspaces (`package.json` / `pnpm-workspace.yaml`),
  `node_modules` as external.
- **Python** (stdlib `ast`): absolute imports from the source roots (`.`, `src`, `lib`,
  or `source_roots` in `codegraph.toml`), relative imports, `from pkg import submodule`,
  `__all__`, `if TYPE_CHECKING:` imports (kind `type`), `importlib.import_module("…")`
  (kind `dynamic`) and `mock.patch("pkg.mod.name")` (kind `mock`).

## Adding a language

Add a module under `codegraph/languages/` that implements `LanguageAdapter`
(`extensions`, `parse()`, `make_resolver()`) and register it in
`codegraph/languages/__init__.py`. The core (scan, link, db, cli) is language-agnostic.
