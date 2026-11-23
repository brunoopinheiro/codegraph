"""TypeScript / TSX / JavaScript adapter (tree-sitter-typescript)."""
from __future__ import annotations

from pathlib import Path

import tree_sitter as ts
import tree_sitter_typescript as tst

from .base import Export, FileFacts, Import, Ref, Symbol, Unresolved

DECL_KINDS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "class_declaration": "class", "abstract_class_declaration": "class",
    "interface_declaration": "interface", "type_alias_declaration": "type", "enum_declaration": "enum",
}
SKIP_PARENTS = {"import_specifier", "import_clause", "namespace_import", "export_specifier",
                "namespace_export", "export_statement"}
NAMED_DECLS = set(DECL_KINDS) | {"variable_declarator", "function_expression", "class", "method_definition"}
QUERY = """
(identifier) @id
(shorthand_property_identifier) @id
(type_identifier) @type
(call_expression function: (import) arguments: (arguments . (_) @dyn))
(call_expression function: (member_expression object: (identifier) @mobj property: (property_identifier) @mprop)
  arguments: (arguments . (_) @marg))
(call_expression function: (identifier) @req arguments: (arguments . (string) @rarg) (#eq? @req "require"))
(member_expression object: (identifier) @nsobj property: (property_identifier) @nsprop)
"""
MOCK_OBJECTS = {"vi", "jest"}
MOCK_FUNCS = {"mock", "doMock", "unstable_mockModule"}

_cache: dict[bool, tuple[ts.Parser, ts.Query]] = {}


def _tools(tsx: bool) -> tuple[ts.Parser, ts.Query]:
    if tsx not in _cache:
        lang = ts.Language(tst.language_tsx() if tsx else tst.language_typescript())
        _cache[tsx] = (ts.Parser(lang), ts.Query(lang, QUERY))
    return _cache[tsx]


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _literal(node) -> str | None:
    """Value of a string literal or a template string without substitutions."""
    if node.type == "string" or (node.type == "template_string"
                                 and not any(c.type == "template_substitution" for c in node.children)):
        return _text(node)[1:-1]
    return None


def _has_token(node, token: str) -> bool:
    return any(not c.is_named and c.type == token for c in node.children)


class TypeScriptAdapter:
    name = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
    explicit_exports = True

    def make_resolver(self, root: Path, all_files, files, cfg):
        from ..resolve import NodeResolver
        return NodeResolver(root, all_files, files, cfg)

    def parse(self, path: str, source: bytes) -> FileFacts:
        jsxish = not path.endswith((".ts", ".mts", ".cts"))
        parser, query = _tools(jsxish)
        tree = parser.parse(source)
        facts = FileFacts()
        _Walker(facts, path.endswith((".tsx", ".jsx"))).program(tree.root_node)
        self._refs(facts, query, tree.root_node)
        return facts

    # -- references, dynamic imports, mocks, require ---------------------------------------
    def _refs(self, facts: FileFacts, query: ts.Query, root) -> None:
        namespaces = {i.local for i in facts.imports if i.imported == "*" and i.local}
        wanted = {s.name for s in facts.symbols} | {i.local for i in facts.imports if i.local}
        for _, caps in ts.QueryCursor(query).matches(root):
            if "id" in caps or "type" in caps:
                node = (caps.get("id") or caps["type"])[0]
                name = _text(node)
                if name not in wanted:
                    continue
                if name in namespaces and node.parent.type == "member_expression":
                    continue  # `ns.member` is recorded by the @nsobj pattern
                kind = self._ref_kind(node)
                if kind:
                    facts.refs.append(Ref(name, "type" if "type" in caps else kind, node.start_point[0] + 1))
            elif "nsobj" in caps:
                obj = _text(caps["nsobj"][0])
                if obj in namespaces:
                    parent = caps["nsobj"][0].parent
                    kind = "call" if parent.parent.type == "call_expression" else "use"
                    facts.refs.append(Ref(f"{obj}.{_text(caps['nsprop'][0])}", kind, parent.start_point[0] + 1))
            elif "dyn" in caps:
                self._module_call(facts, caps["dyn"][0], "dynamic")
            elif "mobj" in caps:
                if _text(caps["mobj"][0]) in MOCK_OBJECTS and _text(caps["mprop"][0]) in MOCK_FUNCS:
                    self._module_call(facts, caps["marg"][0], "mock")
            elif "req" in caps and _text(caps["req"][0]) == "require":
                self._module_call(facts, caps["rarg"][0], "static")

    @staticmethod
    def _module_call(facts: FileFacts, arg, kind: str) -> None:
        line = arg.start_point[0] + 1
        spec = _literal(arg)
        if spec is None:
            facts.unresolved.append(Unresolved(kind, line, f"{kind}-non-literal", _text(arg)[:80]))
        else:
            facts.imports.append(Import(spec, kind, line, "*", None))

    @staticmethod
    def _ref_kind(node) -> str | None:
        parent = node.parent
        ptype = parent.type
        if ptype in SKIP_PARENTS:
            return None
        if ptype in NAMED_DECLS:
            name = parent.child_by_field_name("name")
            if name is not None and name.start_byte == node.start_byte:
                return None
        if ptype in ("call_expression", "new_expression"):
            fn = parent.child_by_field_name("function") or parent.child_by_field_name("constructor")
            if fn is not None and fn.start_byte == node.start_byte:
                return "call"
        if ptype in ("jsx_opening_element", "jsx_self_closing_element"):
            return "jsx"
        if ptype == "jsx_closing_element":
            return None
        return "use"


class _Walker:
    """Top-level statements: declarations, imports and exports."""

    def __init__(self, facts: FileFacts, jsx_file: bool):
        self.f = facts
        self.jsx = jsx_file

    def program(self, root) -> None:
        for node in root.named_children:
            if node.type == "import_statement":
                self.import_(node)
            elif node.type == "export_statement":
                self.export(node)
            else:
                self.declaration(node, exported=False)

    def _kind(self, name: str, kind: str) -> str:
        if self.jsx and name[:1].isupper() and kind in ("function", "const"):
            return "component"
        return kind

    def _add(self, name: str, kind: str, node, exported: bool) -> None:
        self.f.symbols.append(Symbol(name, self._kind(name, kind), node.start_point[0] + 1,
                                     node.end_point[0] + 1, exported))

    def declaration(self, node, exported: bool, default: bool = False) -> list[str]:
        if node.type == "ambient_declaration" and node.named_children:
            return self.declaration(node.named_children[0], exported, default)
        if node.type in DECL_KINDS:
            name_node = node.child_by_field_name("name")
            name = _text(name_node) if name_node else "default"
            self._add(name, DECL_KINDS[node.type], node, exported)
            return [name]
        names = []
        if node.type in ("lexical_declaration", "variable_declaration"):
            const = _has_token(node, "const")
            for decl in node.named_children:
                name_node = decl.child_by_field_name("name")
                if decl.type != "variable_declarator" or name_node is None or name_node.type != "identifier":
                    continue
                value = decl.child_by_field_name("value")
                vtype = value.type if value is not None else ""
                kind = ("function" if vtype in ("arrow_function", "function_expression", "function")
                        else "class" if vtype == "class" else "const" if const else "variable")
                names.append(_text(name_node))
                self._add(names[-1], kind, node, exported)
        return names

    def import_(self, node) -> None:
        src = node.child_by_field_name("source")
        if src is None:
            return
        spec, line = _literal(src) or "", node.start_point[0] + 1
        type_only = _has_token(node, "type")
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            self.f.imports.append(Import(spec, "static", line, None, None))
            return
        for part in clause.named_children:
            if part.type == "identifier":
                self.f.imports.append(Import(spec, "type" if type_only else "static", line, "default", _text(part)))
            elif part.type == "namespace_import":
                local = next(c for c in part.named_children if c.type == "identifier")
                self.f.imports.append(Import(spec, "type" if type_only else "static", line, "*", _text(local)))
            elif part.type == "named_imports":
                for sp in part.named_children:
                    if sp.type != "import_specifier":
                        continue
                    name = _text(sp.child_by_field_name("name"))
                    alias = sp.child_by_field_name("alias")
                    kind = "type" if type_only or _has_token(sp, "type") else "static"
                    self.f.imports.append(Import(spec, kind, line, name, _text(alias) if alias else name))

    def export(self, node) -> None:
        line = node.start_point[0] + 1
        default = _has_token(node, "default")
        decl = node.child_by_field_name("declaration")
        src = node.child_by_field_name("source")
        if decl is not None:
            for name in self.declaration(decl, True, default):
                self.f.exports.append(Export("default" if default else name, "local", line, name))
            return
        value = node.child_by_field_name("value")
        if default and value is not None:
            if value.type == "identifier":
                self.f.exports.append(Export("default", "local", line, _text(value)))
            else:
                kind = "function" if value.type in ("arrow_function", "function_expression") else "const"
                self._add("default", kind, node, True)
                self.f.exports.append(Export("default", "local", line, "default"))
            return
        spec = _literal(src) if src is not None else None
        type_only = _has_token(node, "type")
        for part in node.named_children:
            if part.type == "export_clause":
                for sp in part.named_children:
                    if sp.type != "export_specifier":
                        continue
                    name = _text(sp.child_by_field_name("name"))
                    alias = sp.child_by_field_name("alias")
                    public = _text(alias) if alias else name
                    if spec is None:
                        self.f.exports.append(Export(public, "local", line, name))
                    else:
                        kind = "type" if type_only or _has_token(sp, "type") else "reexport"
                        self.f.exports.append(Export(public, kind, line, name, spec))
            elif part.type == "namespace_export" and spec is not None:
                alias = next(c for c in part.named_children if c.type in ("identifier", "string"))
                self.f.exports.append(Export(_text(alias), "reexport", line, "*", spec))
        if spec is not None and _has_token(node, "*") and not any(c.type == "namespace_export" for c in node.named_children):
            self.f.exports.append(Export("*", "star", line, None, spec))
