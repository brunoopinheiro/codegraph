import json
import sqlite3
import subprocess

import pytest

from teia.cli import main

TS_FILES = {
    "tsconfig.json": '{\n  // solution config extends a base\n  "extends": "./tsconfig.base.json",\n'
                     '  "compilerOptions": { "strict": true, },\n}\n',
    "tsconfig.base.json": '{ "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["src/*"] } } }',
    "teia.toml": 'registry = ["src/registry.ts"]\n',
    "src/lib/date.ts": "export function isoDate(d: Date) {\n  return d.toISOString();\n}\n"
                       "export function unusedThing() {}\n",
    "src/lib/index.ts": "export * from './date';\n",
    "src/index.ts": "export * from './lib';\n",
    "src/types.ts": "export interface Model { id: string }\n",
    "src/a.ts": "export const dup = 1;\n",
    "src/b.ts": "export const dup = 2;\n",
    "src/amb.ts": "export * from './a';\nexport * from './b';\n",
    "src/useAmb.ts": "import { dup } from './amb';\nconsole.log(dup);\n",
    "src/app.tsx": "import { isoDate } from '@/index';\nimport type { Model } from '@/types';\n"
                   "export function App(m: Model) {\n  return <div>{isoDate(new Date())}</div>;\n}\n"
                   "export const lazyPage = () => import('./pages/page');\n"
                   "export const bad = (n: string) => import(n);\n",
    "src/pages/page.tsx": "export default function Page() {\n  return null;\n}\n",
    "src/registry.ts": "export const registry = { page: () => import('./pages/page') };\n",
    "src/cycle/x.ts": "import { y } from './y';\nexport const x = () => y();\n",
    "src/cycle/y.ts": "import { x } from './x';\nexport const y = () => x;\n",
    "src/styles.css": "body {}\n",
    "src/styled.ts": "import './styles.css';\nimport './missing.css';\n",
    "src/app.test.ts":"import { vi } from 'vitest';\nimport { isoDate } from './lib/date';\n"
                       "vi.mock('./lib/date', () => ({}));\nisoDate(new Date());\n",
}

PY_FILES = {
    "src/shop/__init__.py": 'from .pricing import total\n\n__all__ = ["total"]\n',
    "src/shop/pricing.py": "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n\n"
                           "if TYPE_CHECKING:\n    from shop.models import Item\n\n\n"
                           "def total(items: list[Item]) -> float:\n    return sum(price(i) for i in items)\n\n\n"
                           "def price(item: \"Item\") -> float:\n    return item.value\n\n\n"
                           "def unused_helper():\n    pass\n",
    "src/shop/models.py": "class Item:\n    value = 1.0\n",
    "src/shop/cart.py": "from shop import total\nfrom . import pricing\nimport shop.pricing as p\n\n\n"
                        "def checkout(items):\n    return total(items) + pricing.price(items[0]) + p.total(items)\n",
    "src/shop/plugins.py": "import importlib\n\nloaded = importlib.import_module(\"shop.models\")\n\n\n"
                           "def load(name):\n    return importlib.import_module(name)\n",
    "src/shop/a.py": "from shop import b\n\nVALUE = 1\n",
    "src/shop/b.py": "from shop import a\n\nOTHER = a.VALUE\n",
    "tests/test_cart.py": "from unittest import mock\n\nfrom shop.cart import checkout\n\n\n"
                          "@mock.patch(\"shop.pricing.price\")\ndef test_checkout(price):\n    checkout([])\n",
}


def make_repo(root, files):
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def runner(repo, capsys):
    """Run the CLI against `repo` with --json and return the parsed output."""
    def run(*args):
        capsys.readouterr()
        main([*args, "--root", str(repo), "--json"])
        return json.loads(capsys.readouterr().out)
    run("index")
    return run


def query(repo, sql, *params):
    con = sqlite3.connect(repo / ".teia" / "graph.db")
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, params)]


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path, TS_FILES)


@pytest.fixture
def cg(repo, capsys):
    return runner(repo, capsys)


@pytest.fixture
def py_repo(tmp_path):
    return make_repo(tmp_path, PY_FILES)


@pytest.fixture
def pycg(py_repo, capsys):
    return runner(py_repo, capsys)
