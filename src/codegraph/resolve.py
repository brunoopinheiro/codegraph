"""Module resolution for the JS/TS family, driven only by the target project's configuration.

Handles tsconfig/jsconfig `paths` + `baseUrl` (with `extends` chains and `references`),
extension order, `index.*`, `.js` -> `.ts` ESM specifiers, workspace packages and
node_modules (reported as external).
"""
from __future__ import annotations

import json
import os
import posixpath
import re
from pathlib import Path

from .config import Config, glob_match

DEFAULT_EXTS = [".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs"]
JS_EXTS = (".js", ".jsx", ".mjs", ".cjs")
TS_FOR_JS = (".ts", ".tsx", ".mts", ".cts", ".d.ts")
CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")


_JSONC_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.S)


def read_jsonc(path: Path) -> dict:
    """Parse JSON with comments and trailing commas (tsconfig style)."""
    text = path.read_text(encoding="utf-8-sig")
    text = _JSONC_TOKENS.sub(lambda m: m.group(0) if m.group(0).startswith('"') else "", text)
    try:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
    except ValueError:
        return {}


def _join(*parts: str) -> str:
    return posixpath.normpath(posixpath.join(*parts)).removeprefix("./")


def match_paths(spec: str, paths: dict[str, list[str]], base: str) -> list[str] | None:
    """tsconfig `paths` matching: exact keys win, then the longest prefix before `*`."""
    best: tuple[int, list[str]] | None = None
    for pattern, targets in paths.items():
        targets = [targets] if isinstance(targets, str) else targets
        if "*" not in pattern:
            if pattern == spec:
                best = (1 << 30, targets)
            continue
        pre, suf = pattern.split("*", 1)
        if spec.startswith(pre) and spec.endswith(suf) and len(spec) >= len(pre) + len(suf):
            if best is None or len(pre) > best[0]:
                star = spec[len(pre):len(spec) - len(suf)]
                best = (len(pre), [t.replace("*", star) for t in targets])
    return None if best is None else [_join(base, t) for t in best[1]]


class NodeResolver:
    def __init__(self, root: Path, all_files: list[str], files: set[str], cfg: Config):
        self.root, self.files, self.cfg = root, files, cfg
        self.exts = cfg.extensions or DEFAULT_EXTS
        self.config_dirs = {posixpath.dirname(p): p for p in sorted(all_files, reverse=True)
                            if posixpath.basename(p) in CONFIG_NAMES}  # tsconfig wins over jsconfig
        self._configs: dict[str, tuple[dict, str | None]] = {}
        self._nearest: dict[str, str | None] = {}
        self._cache: dict[tuple[str, str], tuple[str, str]] = {}
        self.packages = self._workspace_packages(all_files)

    # -- configuration -------------------------------------------------------------------
    def _load_config(self, rel: str, depth: int = 0) -> dict:
        """Effective {paths, paths_base, base_url} after following `extends` (root-relative)."""
        eff: dict = {"paths": None, "paths_base": None, "base_url": None}
        path = self.root / rel
        if depth > 10 or not path.is_file():
            return eff
        data, here = read_jsonc(path), posixpath.dirname(rel)
        extends = data.get("extends")
        for ext in [extends] if isinstance(extends, str) else extends or []:
            parent = self._extends_path(here, ext)
            if parent:
                eff.update({k: v for k, v in self._load_config(parent, depth + 1).items() if v is not None})
        opts = data.get("compilerOptions") or {}
        if "baseUrl" in opts:
            eff["base_url"] = _join(here, opts["baseUrl"])
        if "paths" in opts:
            eff["paths"], eff["paths_base"] = opts["paths"], here
        if eff["paths"] is None:  # solution-style config: borrow paths from references
            for ref in data.get("references") or []:
                target = _join(here, ref.get("path", ""))
                if not target.endswith(".json"):
                    target = _join(target, "tsconfig.json")
                sub = self._load_config(target, depth + 1)
                if sub["paths"] is not None:
                    eff.update(sub)
                    break
        return eff

    def _extends_path(self, here: str, ext: str) -> str | None:
        if ext.startswith((".", "/")):
            cand = _join(here, ext)
            return cand if cand.endswith(".json") or (self.root / cand).is_file() else cand + ".json"
        d = here
        while True:  # package config: look in node_modules up the tree
            base = _join(d, "node_modules", ext)
            for cand in (base, base + ".json", _join(base, "tsconfig.json")):
                if (self.root / cand).is_file():
                    return cand
            if not d:
                return None
            d = posixpath.dirname(d)

    def config_for(self, directory: str) -> dict:
        if directory not in self._nearest:
            d, found = directory, None
            while True:
                if d in self.config_dirs:
                    found = self.config_dirs[d]
                    break
                if not d:
                    break
                d = posixpath.dirname(d)
            self._nearest[directory] = found
        rel = self._nearest[directory]
        if rel is None:
            return {"paths": None, "paths_base": None, "base_url": None}
        if rel not in self._configs:
            self._configs[rel] = self._load_config(rel)
        return self._configs[rel]

    def _workspace_packages(self, all_files: list[str]) -> dict[str, tuple[str, dict]]:
        patterns: list[str] = []
        if "package.json" in all_files:
            ws = read_jsonc(self.root / "package.json").get("workspaces") or []
            patterns += ws.get("packages", []) if isinstance(ws, dict) else ws
        if "pnpm-workspace.yaml" in all_files:
            text = (self.root / "pnpm-workspace.yaml").read_text(encoding="utf-8")
            patterns += re.findall(r"^\s*-\s*['\"]?([^'\"\n#]+?)['\"]?\s*$", text, re.M)
        packages = {}
        for p in all_files:
            d = posixpath.dirname(p)
            if posixpath.basename(p) != "package.json" or not d or "node_modules" in p:
                continue
            if patterns and not any(glob_match(d, pat.rstrip("/")) for pat in patterns):
                continue
            data = read_jsonc(self.root / p)
            if data.get("name"):
                packages[data["name"]] = (d, data)
        return packages

    # -- resolution ----------------------------------------------------------------------
    def probe(self, base: str) -> str | None:
        files = self.files
        if base in files:
            return base
        stem, ext = posixpath.splitext(base)
        if ext in JS_EXTS:
            for e in TS_FOR_JS:
                if stem + e in files:
                    return stem + e
        for e in self.exts:
            if base + e in files:
                return base + e
        for e in self.exts:
            if base + "/index" + e in files:
                return base + "/index" + e
        return None

    def resolve(self, spec: str, from_file: str) -> tuple[str, str]:
        here = posixpath.dirname(from_file)
        key = (here, spec)
        if key not in self._cache:
            self._cache[key] = self._resolve(spec, here)
        return self._cache[key]

    def _resolve(self, spec: str, here: str) -> tuple[str, str]:
        if spec.startswith((".", "/")):
            hit = self.probe(_join(here, spec) if spec.startswith(".") else spec.lstrip("/"))
            return ("internal", hit) if hit else ("unresolved", "file-not-found")
        tc = self.config_for(here)
        alias_matched = False
        for paths, base in ((self.cfg.aliases, ""), (tc["paths"], tc["base_url"] or tc["paths_base"] or "")):
            cands = match_paths(spec, paths, base) if paths else None
            if cands is not None:
                alias_matched = True
                for cand in cands:
                    hit = self.probe(cand)
                    if hit:
                        return "internal", hit
        if tc["base_url"] is not None:
            hit = self.probe(_join(tc["base_url"], spec))
            if hit:
                return "internal", hit
        if alias_matched:
            return "unresolved", "alias-target-not-found"
        parts = spec.split("/")
        pkg = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        if pkg in self.packages:
            return self._resolve_package(pkg, "/".join(parts[2 if spec.startswith("@") else 1:]))
        if spec.startswith(("@/", "~", "#")) or pkg == "@":
            return "unresolved", "unknown-alias"
        return "external", pkg

    def _resolve_package(self, pkg: str, sub: str) -> tuple[str, str]:
        d, data = self.packages[pkg]
        if sub:
            cands = [_join(d, sub), _join(d, "src", sub)]
        else:
            exp = data.get("exports")
            dot = exp.get(".", exp) if isinstance(exp, dict) else exp
            if isinstance(dot, dict):
                dot = dot.get("import") or dot.get("types") or dot.get("default")
            fields = [data.get(k) for k in ("source", "types", "module", "main")] + [dot]
            cands = [_join(d, f) for f in fields if isinstance(f, str)]
            cands += [c.replace("/dist/", "/src/") for c in cands] + [_join(d, "src/index"), _join(d, "index")]
        for cand in cands:
            hit = self.probe(cand) or self.probe(posixpath.splitext(cand)[0])
            if hit:
                return "internal", hit
        return "unresolved", "workspace-entry-not-found"
