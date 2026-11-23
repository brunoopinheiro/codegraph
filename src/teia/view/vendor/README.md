# Vendored viewer libraries

`teia view` inlines these files into the generated HTML when all three are present
here, so the page works offline. When any is missing, the page loads all three from the
CDN instead (and `teia view` says so on stderr).

Download each URL and save it here under the exact file name. The versions are pinned in
one place, `LIBRARIES` in `src/teia/view/__init__.py`; keep this list in sync with it.

| File name | Source |
|---|---|
| `graphology.umd.min.js` | https://cdn.jsdelivr.net/npm/graphology@0.25.4/dist/graphology.umd.min.js |
| `graphology-library.min.js` | https://cdn.jsdelivr.net/npm/graphology-library@0.8.0/dist/graphology-library.min.js |
| `sigma.min.js` | https://cdn.jsdelivr.net/npm/sigma@3.0.0/dist/sigma.min.js |

All three are MIT licensed. Files in this folder ship in the wheel (the build backend
includes everything under `src/teia/`).
