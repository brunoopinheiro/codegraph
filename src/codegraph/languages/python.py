"""Python adapter, built on the standard-library `ast` module (no extra dependency)."""
from __future__ import annotations

import ast
import posixpath
from pathlib import Path

from .base import Export, FileFacts, Import, Ref, Symbol, Unresolved

DYNAMIC_CALLS = {"importlib.import_module", "import_module", "__import__"}
TYPE_CHECKING = {"TYPE_CHECKING", "typing.TYPE_CHECKING"}


def _dotted(node) -> str | None:
    """`a.b.c` for a chain of attributes on a name, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _string(node) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class PythonAdapter:
    name = "python"
    extensions = (".py", ".pyi")
    explicit_exports = False

    def make_resolver(self, root: Path, all_files, files, cfg):
        return PythonResolver(files, cfg.source_roots)

    def parse(self, path: str, source: bytes) -> FileFacts:
        tree = ast.parse(source, filename=path)
        facts = FileFacts()
        self._imports(tree, facts)
        self._symbols(tree, facts)
        wanted = {s.name for s in facts.symbols} | {i.local for i in facts.imports if i.local}
        _RefVisitor(facts, wanted).visit(tree)
        return facts

    @staticmethod
    def _imports(tree: ast.Module, facts: FileFacts) -> None:
        type_only = {id(n) for top in tree.body
                     if isinstance(top, ast.If) and _dotted(top.test) in TYPE_CHECKING for n in ast.walk(top)}
        top_level = {id(n) for n in tree.body}
        for node in ast.walk(tree):
            kind = "type" if id(node) in type_only else "static"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    facts.imports.append(Import(alias.name, kind, node.lineno, "*", alias.asname or alias.name))
            elif isinstance(node, ast.ImportFrom):
                spec = "." * node.level + (node.module or "")
                for alias in node.names:
                    if alias.name == "*":
                        facts.imports.append(Import(spec, kind, node.lineno, "*", None))
                        if id(node) in top_level:
                            facts.exports.append(Export("*", "star", node.lineno, None, spec))
                    else:
                        facts.imports.append(Import(spec, kind, node.lineno, alias.name, alias.asname or alias.name))

    @staticmethod
    def _symbols(tree: ast.Module, facts: FileFacts) -> None:
        public: list[str] | None = None  # from __all__, when present
        for node in tree.body:
            names, kind = [], "variable"
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names, kind = [node.name], "class" if isinstance(node, ast.ClassDef) else "function"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if "__all__" in names and isinstance(node.value, (ast.List, ast.Tuple)):
                    public = [s for s in map(_string, node.value.elts) if s]
                kind = "const" if names and names[0].isupper() else "variable"
            elif type(node).__name__ == "TypeAlias":  # Python 3.12 `type X = ...`
                names, kind = [node.name.id], "type"
            for name in names:
                facts.symbols.append(Symbol(name, kind, node.lineno, node.end_lineno or node.lineno))
        top_imports = {i.local for i in facts.imports if i.imported not in (None, "*")}
        if public is None:  # every public top-level name, including names imported here (re-exports)
            public = [s.name for s in facts.symbols] + sorted(top_imports)
            public = [n for n in public if not n.startswith("_")]
        for name in dict.fromkeys(public):
            facts.exports.append(Export(name, "local", 0, name))
        exported = set(public)
        for symbol in facts.symbols:
            symbol.exported = symbol.name in exported


class _RefVisitor(ast.NodeVisitor):
    """References to top-level symbols and imported names, plus dynamic imports and mocks."""

    def __init__(self, facts: FileFacts, wanted: set[str]):
        self.facts, self.wanted = facts, wanted
        self.prefixes = sorted({i.local for i in facts.imports if i.local}, key=len, reverse=True)

    def _add(self, name: str, kind: str, node) -> None:
        self.facts.refs.append(Ref(name, kind, node.lineno))

    def _ref(self, node, kind: str) -> bool:
        """Record a Name / attribute chain. Returns False when `node` is neither."""
        dotted = _dotted(node)
        if dotted is None:
            return False
        for prefix in self.prefixes:  # `mod.func`, `pkg.mod.func`, `Class.method`
            if dotted.startswith(prefix + "."):
                rest = dotted[len(prefix) + 1:].split(".")
                self._add(f"{prefix}.{rest[0]}", kind if len(rest) == 1 else "use", node)
                return True
        head = dotted.split(".")[0]
        if head in self.wanted:
            self._add(head, kind if head == dotted else "use", node)
        return True

    def _annotation(self, node) -> None:
        if node is None:
            return
        text = _string(node)
        if text is not None:  # forward reference: "Model"
            try:
                parsed = ast.parse(text, mode="eval").body
            except SyntaxError:
                return
            node = ast.increment_lineno(parsed, node.lineno - 1)
        if not self._ref(node, "type"):
            for child in ast.iter_child_nodes(node):
                self._annotation(child)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._ref(node, "use")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if not self._ref(node, "use"):
            self.generic_visit(node)

    def visit_arg(self, node: ast.arg) -> None:
        self._annotation(node.annotation)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._annotation(node.annotation)
        for child in (node.target, node.value):
            if child is not None:
                self.visit(child)

    def visit_FunctionDef(self, node) -> None:
        self._annotation(node.returns)
        for child in [*node.decorator_list, node.args, *node.body]:
            self.visit(child)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        func = _dotted(node.func) or ""
        first = node.args[0] if node.args else None
        if func in DYNAMIC_CALLS and first is not None:
            self._module_call(first, "dynamic", lambda spec: spec)
        elif func.split(".")[-1] == "patch" and first is not None:  # mock.patch("pkg.mod.name")
            self._module_call(first, "mock", lambda target: target.rpartition(".")[0])
        if not self._ref(node.func, "call"):
            self.visit(node.func)
        for child in [*node.args, *node.keywords]:
            self.visit(child)

    def _module_call(self, arg, kind: str, to_module) -> None:
        spec = _string(arg)
        if spec is None:
            self.facts.unresolved.append(Unresolved(kind, arg.lineno, f"{kind}-non-literal", ast.unparse(arg)[:80]))
        elif to_module(spec):
            self.facts.imports.append(Import(to_module(spec), kind, arg.lineno, "*", None))


class PythonResolver:
    """Absolute imports from the source roots, relative imports from the package."""

    def __init__(self, files: set[str], source_roots: list[str]):
        self.files = files
        self.roots = source_roots or [""] + [d for d in ("src", "lib") if any(f.startswith(d + "/") for f in files)]
        self.local_tops: set[str] = set()  # top-level packages that live in this repo
        for root in self.roots:
            prefix = root + "/" if root else ""
            self.local_tops |= {f[len(prefix):].split("/")[0].removesuffix(".py")
                                for f in files if f.startswith(prefix)}

    def _module(self, base: str) -> str | None:
        for cand in (base + ".py", base + ".pyi", posixpath.join(base, "__init__.py"),
                     posixpath.join(base, "__init__.pyi")):
            if cand in self.files and not cand.startswith(("/", ".")):
                return cand
        return None

    def resolve(self, spec: str, from_file: str) -> tuple[str, str]:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            base = posixpath.dirname(from_file)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            rest = spec[level:].replace(".", "/")
            hit = self._module(posixpath.join(base, rest) if rest else base)
            return ("internal", hit) if hit else ("unresolved", "module-not-found")
        for root in self.roots:
            hit = self._module(posixpath.join(root, spec.replace(".", "/")))
            if hit:
                return "internal", hit
        top = spec.split(".")[0]
        return ("unresolved", "module-not-found") if top in self.local_tops else ("external", top)

    def member_module(self, spec: str, name: str, from_file: str) -> tuple[str, str] | None:
        """`from pkg import mod` imports the submodule when one exists: (new spec, path)."""
        sub = spec + name if spec.endswith(".") else f"{spec}.{name}"
        status, value = self.resolve(sub, from_file)
        return (sub, value) if status == "internal" else None
