---
name: codegraph
description: Use when you need structural facts about a TypeScript/JavaScript or Python codebase - who references or calls a symbol, what a change to a file or symbol can break, how two files are connected, import cycles, unused exports - or when setting up codegraph in a repository. Prefer it over grep for "who uses X" and "what breaks if I change Y", because it resolves aliases, barrels and re-exports to the real definition.
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
6. Show the user the final `codegraph.toml` and the `stats` result. Suggest committing
   the toml; it is the repo's shared configuration. Do not commit `.codegraph/`.

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
| What can break if I change this file / symbol? | `codegraph impact src/foo.ts --json` / `codegraph impact X --json` |
| How does A reach B? | `codegraph path src/a.ts src/b.ts --json` |
| Are there import cycles? | `codegraph cycles --json` (add `--kinds static,reexport,star,registry,type,dynamic` for type/lazy edges) |
| What exports are dead? | `codegraph unused-exports --json` |

- Run `codegraph index` first in every session and after you edit files. It is
  incremental and takes about a second when nothing changed.
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
