from conftest import query


def test_refs_through_package_reexport_submodule_and_alias(pycg):
    refs = pycg("refs", "total")["refs"]
    assert [(r["path"], r["line"], r["kind"]) for r in refs] == [
        ("src/shop/cart.py", 7, "call"), ("src/shop/cart.py", 7, "call")]
    assert [(r["path"], r["line"]) for r in pycg("refs", "price")["refs"]] == [
        ("src/shop/cart.py", 7), ("src/shop/pricing.py", 10)]


def test_type_checking_imports_and_annotations(pycg, py_repo):
    assert query(py_repo, "SELECT kind FROM imports WHERE spec='shop.models' AND imported='Item'") == [{"kind": "type"}]
    kinds = {(r["line"], r["kind"]) for r in pycg("refs", "Item")["refs"]}
    assert kinds == {(9, "type"), (13, "type")}  # list[Item] and the "Item" forward reference
    impact = {f["path"]: f["depth"] for f in pycg("impact", "Item")["files"]}
    assert impact == {"src/shop/pricing.py": 1, "src/shop/cart.py": 1}  # tests hidden by default


def test_dynamic_import_and_mock(pycg, py_repo):
    dyn = query(py_repo, "SELECT i.kind, t.path FROM imports i JOIN files t ON t.id=i.target_file_id "
                         "WHERE i.kind='dynamic'")
    assert dyn == [{"kind": "dynamic", "path": "src/shop/models.py"}]
    assert query(py_repo, "SELECT reason FROM unresolved") == [{"reason": "dynamic-non-literal"}]
    mock = query(py_repo, "SELECT i.spec, t.path FROM imports i JOIN files t ON t.id=i.target_file_id "
                          "WHERE i.kind='mock'")
    assert mock == [{"spec": "shop.pricing", "path": "src/shop/pricing.py"}]
    assert "mock" in {r["kind"] for r in pycg("refs", "price", "--include-tests")["refs"]}


def test_cycle_and_unused(pycg):
    assert [c["files"] for c in pycg("cycles")["cycles"]] == [["src/shop/a.py", "src/shop/b.py"]]
    unused = {u["name"] for u in pycg("unused-exports")["unused"]}
    assert "unused_helper" in unused and "total" not in unused and "price" not in unused


def test_stats_has_no_unresolved_imports(pycg):
    stats = pycg("stats")
    assert stats["edge_status"].get("imports.unresolved", 0) == 0
    assert stats["ambiguous"] == 0
