---
name: codegraph
description: Use when you need structural facts about a TypeScript/JavaScript or Python codebase - who references or calls a symbol, what a change to a file or symbol can break, how two files are connected, import cycles, unused exports, the file-level import graph (export or an interactive view for the user) - or when setting up codegraph in a repository. Prefer it over grep for "who uses X" and "what breaks if I change Y", because it resolves aliases, barrels and re-exports to the real definition.
---

# codegraph

`codegraph` indexes the repo into a SQLite graph (`.codegraph/graph.db`, self-ignored)
and answers structural questions deterministically. Its answers are only as good as
its resolution, so **check `stats` before trusting any result** in a repo you haven't
set up in this session.

## 1. Make sure it runs

```sh
codegraph --help
```

If the command is missing, ask the user how they want it installed
(`uv tool install <path-or-git-url>`, or `uvx --from <path-or-git-url> codegraph`).
Do not install packages without asking.

## 2. Set up the repo (once per repo, or when `stats` gets worse)

Goal: `stats` shows `unresolved=0` and `ambiguous=0`. Failing that, every remaining
cause is understood and listed for the user.

1. Index and read the health report:
   ```sh
   codegraph index
   codegraph stats --json
   ```
2. For each entry in `top_causes`, apply the fix from the table below. Write the
   changes to `codegraph.toml` at the repo root, then run `codegraph index` and
   `codegraph stats` again. Editing the toml triggers a full re-index automatically.
3. Look for **registry files**. These are files that wire many modules lazily, such as
   route tables, module registries or plugin maps. A typical sign is many `import("./…")`
   calls in one file:
   ```sh
   codegraph stats --json        # import_kinds.dynamic > 0 means there are lazy imports
   ```
   Find them with your search tool, for example files with several `import(` matches.
   List them under `registry` so their edges get `kind=registry` and count as
   runtime dependencies in `cycles`.
4. Declare **entrypoints** so `unused-exports` doesn't report framework-consumed files.
   Common entrypoints:
   - `main.tsx` / `index.tsx`;
   - Next.js `app/**` and `pages/**`;
   - Vite/Storybook configs and `*.stories.tsx`;
   - CLI modules named in `pyproject.toml` `[project.scripts]`;
   - Django/Flask apps loaded by settings.
5. If tests don't follow the default globs, set `tests`. The defaults are `*.test.*`, `*.spec.*`, `__tests__/`, `__mocks__/`, `test/`, `tests/`, `e2e/`, `test_*.py`, `*_test.py` and `conftest.py`.
6. Check the `config` block of `stats` (or the text output): it shows how many files each
   `registry`, `entrypoints` and `tests` pattern matched. A pattern with 0 matches is a
   typo or a wrong path; fix it. `codegraph index` also warns on stderr about unknown
   keys in the toml, so read its stderr.
7. Show the user the final `codegraph.toml` and the `stats` result. Suggest committing
   the toml; it is the repo's shared configuration. Do not commit `.codegraph/`.

### `codegraph.toml` reference

Every supported key. Paths are relative to the repo root with `/`. "globs" means glob
patterns (`*` and `?` stay inside a path segment, `**/` matches any number of
directories); the other keys are plain values.

```toml
include = ["src/**"]                   # globs. Index only matching files. Default: every file git lists
exclude = ["**/*.generated.ts"]        # globs. Skip matching files
registry = ["src/modules/registry.ts"] # globs. Imports in these files get kind=registry
entrypoints = ["src/main.tsx"]         # globs. Exports in these files are never reported as unused
tests = ["**/*.test.*", "**/__tests__/**"]  # globs. REPLACES the default test globs
source_roots = ["src"]                 # plain directories, no globs. Python import roots (default ".", "src", "lib")
extensions = [".ts", ".tsx", ".js"]    # plain suffixes, no globs. TS resolution order (default covers all)

[aliases]                              # tsconfig "paths" syntax: pattern = string or list of targets
"~/*" = ["src/*"]
```

A misspelled key is ignored (with a warning from `index`), so verify with `stats`.

### Fixes by cause

| `reason` in stats | What to do |
|---|---|
| `unknown-alias` | An `@/x`, `~/x` or `#x` specifier that nothing defines. Find where the bundler defines it (`vite.config.*` `resolve.alias`, `webpack.config.*`, `babel.config.*`, `jest.config.*` `moduleNameMapper`) and mirror it under `[aliases]`, e.g. `"@/*" = ["src/*"]`. |
| `alias-target-not-found` | The alias exists but points to the wrong place. Compare tsconfig `paths`/`baseUrl` with the real folders; override with `[aliases]` if the tsconfig is right for the compiler but not for this layout. |
| `module-not-found` (Python) | Set `source_roots` to the folders that are on `sys.path`, e.g. `["src"]` or `["backend", "libs/shared"]`. |
| `workspace-entry-not-found` | The workspace's `package.json` has no `source`/`types`/`module`/`main`/`exports` pointing to a tracked file. Report it; add an alias to the package's `src/index` if needed. |
| `file-not-found` | Usually a genuinely broken import, or a file that is generated/untracked by git. Report it; don't paper over it. |
| `export-not-found` | The import names something the target doesn't export. Often a real bug; also happens with CommonJS or generated modules. Report it. |
| `ambiguous-star-export` | Two `export *` barrels provide the same name. Report the barrel; the fix is in the code (explicit re-export). |
| `dynamic-non-literal`, `mock-non-literal` | Computed module names. Informational; nothing to configure. |
| `parse-error` | The file couldn't be parsed. Report it. |

High counts of `external` are normal: npm packages, the stdlib and third-party Python libraries all count as external. `asset` means an existing CSS/SVG/JSON file, which is also fine.

If a cause can't be fixed through configuration, stop and tell the user. Never edit `tsconfig.json`, `package.json` or source code just to make `stats` clean.

## 3. Use it

Always pass `--json` when you will parse the output. Symbols are a bare name or
`path:name` when the name exists in several files. Paths are relative to the repo root.

| Question | Command |
|---|---|
| Where is X used? | `codegraph refs X --json` |
| Who calls / renders X? | `codegraph callers X --json` |
| What does X depend on? | `codegraph callees X --json` |
| What can break if I change this file / symbol? | `codegraph impact src/foo.ts --json` / `codegraph impact X --json` (`--depth N` to limit, `--through-registry` for the full traversal) |
| How does A reach B? | `codegraph path src/a.ts src/b.ts --json` |
| Are there import cycles? | `codegraph cycles --json` (add `--kinds static,reexport,star,registry,type,dynamic` for type/lazy edges; `--members` lists every file in text output; `--edges` lists the edges) |
| What exports are unused? | `codegraph unused-exports --summary` first, then `codegraph unused-exports --class dead --path 'src/x/**' --limit 50 --json` |
| The file-level import graph as data | `codegraph export` (JSON to stdout; `--format gexf\|mermaid\|dot`, `--out FILE`) |
| Show the user the import graph | `codegraph view` (writes `.codegraph/view.html` and opens it; `--no-open` to only write it) |

- Run `codegraph index` first in every session and after you edit files. It is
  incremental and takes about a second when nothing changed. Its output says whether the
  index was `up_to_date` and which `commit` it was built at; `stats` has the same `index`
  block with `commit_matches_head`.

### `export` and `view`

Both cover the file-level import graph (files and resolved imports, not symbols), with
the same filters: `--kinds` (default `static,reexport,star,registry`; add `type`,
`dynamic`), `--path GLOB` (repeatable), `--group-depth N` (default 3) and the global
`--include-tests`. `export --collapse` gives one node per directory group.

- `view` is for humans: an interactive page to explore the graph. Offer it when the user
  wants to see the architecture; don't use it to answer questions yourself. Keep using the
  `--json` commands (`impact`, `path`, `cycles`, ...) for your own analysis.
- `export --format json` is the raw graph if you need all edges at once; filter with
  `--path` first on a large repo. `mermaid` and `dot` refuse more than 250 nodes: use
  `--collapse` (with a small `--group-depth`) or `--path` to get a diagram that fits in a
  document or PR description.
- `view` prints on stderr whether its libraries are inlined (works offline) or loaded from
  a CDN (the page needs network access). Tell the user if it is the CDN mode.

### Reading `impact`

- By default it stops at registry files: a registry file that imports something impacted
  is listed (`via: "registry"`, also in `registry_stopped`) but what imports the registry
  is not, because that would be the whole app. Use `--through-registry` only when you
  really need the full closure. Say so to the user when `registry_stopped` is not empty:
  "the registry file X wires this module; everything that loads X is not included".
- Each file has `depth`, `via` (the edge kind it was reached by) and `from`. The text
  output ends with counts per depth. Start with `--depth 1` or `2` on a widely used file.

### Reading `unused-exports`

Entries have a `class`:

- `dead`: no reference at all. Candidates for deletion, after checking "What it does not see".
- `local-only`: used only inside its own file. The `export` keyword is unnecessary; the code is not dead.
- `test-only`: used only from tests, also through a barrel. Not dead code; do not
  suggest deleting it without asking.

`--include-tests` makes test usage count as real usage, so `test-only` entries drop out of
the list. Other filters: `--kind function,component,type,interface,const,...`,
`--path GLOB` (repeatable), `--class`, `--limit N`, `--summary` (counts per class, kind and
directory, no entries). `--json` has `total` (before `--limit`) and `shown`. On a large
repo always start with `--summary`, then narrow with filters.

### Reading `cycles`

Each cycle lists every member (`files`), one `example` loop, `edge_kinds` (edges inside the
component per kind), `held_by` (kinds whose removal breaks the cycle) and `needs_registry`.
A cycle with `needs_registry: true` exists only because of a lazy registry wiring, which
is a different problem from a static import cycle.
- Tests and mocks are hidden by default. Add `--include-tests` when the question is about
  test coverage of a symbol, or before changing a signature, because tests call it too.
- Before a refactor, run `impact` on the symbol and on its file. Use the list as
  the set of places to check and to run tests for.

## What it does not see

Before claiming that something is unused or unaffected, check whether it falls under one of these limits. If it might, also search for the name as text.

- Members, methods and locals are not separate symbols: `obj.method()` is a `use` of `obj`.
- Names imported in Python through `from x import *` are not linked at the ref level.
- Computed dynamic imports, string-based lookups (DI containers, `getattr`, route strings)
  and framework conventions (file-based routing, Django apps) are invisible unless covered
  by `registry` / `entrypoints`.
- Code generated at build time and not tracked by git is invisible.
