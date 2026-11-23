"""Language adapter registry. Add a new language by appending its adapter here."""
from __future__ import annotations

from .base import LanguageAdapter
from .python import PythonAdapter
from .typescript import TypeScriptAdapter

ADAPTERS: list[LanguageAdapter] = [TypeScriptAdapter(), PythonAdapter()]


def adapter_for(path: str) -> LanguageAdapter | None:
    for adapter in ADAPTERS:
        if path.endswith(adapter.extensions):
            return adapter
    return None
