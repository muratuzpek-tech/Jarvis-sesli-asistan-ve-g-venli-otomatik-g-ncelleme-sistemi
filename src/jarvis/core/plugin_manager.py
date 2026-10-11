"""Safe local plugin registry.

Plugins are explicit Python modules placed under ``plugins/`` in the user data
area. A plugin must export ``register()`` returning a list of tool descriptors.
No arbitrary paths are imported, and failures in one plugin do not stop Jarvis.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any
from collections.abc import Callable

from jarvis.paths import data_dir

BASE_DIR = Path(__file__).resolve().parent.parent
LEGACY_PLUGIN_DIR = BASE_DIR / "plugins"
DEFAULT_PLUGIN_DIR = data_dir() / "plugins"


class PluginManager:
    def __init__(self, plugin_dir: Path = DEFAULT_PLUGIN_DIR) -> None:
        self.plugin_dir = plugin_dir.resolve()
        self.tools: dict[str, tuple[dict[str, Any], Callable[[dict], Any], str]] = {}
        self.errors: dict[str, str] = {}

    def discover(self) -> dict[str, str]:
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, str] = {}
        for path in sorted(self.plugin_dir.glob("*.py")):
            if path.name.startswith("_") or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.py", path.name):
                continue
            try:
                module_name = f"jarvis_plugin_{path.stem}"
                spec = importlib.util.spec_from_file_location(module_name, path)
                if not spec or not spec.loader:
                    raise ImportError("plugin loader oluşturulamadı")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise ValueError("register() fonksiyonu eksik")
                descriptors = register()
                for descriptor in descriptors:
                    self._add_descriptor(descriptor, path.name)
                loaded[path.name] = "ok"
            except Exception as exc:
                self.errors[path.name] = str(exc)
                loaded[path.name] = f"error: {exc}"
        return loaded

    def _add_descriptor(self, descriptor: dict[str, Any], source: str) -> None:
        name = str(descriptor["name"])
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise ValueError(f"geçersiz araç adı: {name}")
        handler = descriptor.get("handler")
        schema = descriptor.get("schema", {"type": "object", "properties": {}})
        if not callable(handler):
            raise ValueError(f"{name}: handler callable değil")
        self.tools[name] = ({"type": "function", "function": {"name": name, "description": str(descriptor.get("description", "Yerel eklenti aracı")), "parameters": schema}}, handler, source)

    def schemas(self) -> list[dict[str, Any]]:
        return [item[0] for item in self.tools.values()]

    def call(self, name: str, args: dict[str, Any]) -> Any:
        if name not in self.tools:
            raise KeyError(name)
        return self.tools[name][1](args)

    def status(self) -> dict[str, Any]:
        return {"plugin_dir": str(self.plugin_dir), "tools": {name: source for name, (_, _, source) in self.tools.items()}, "errors": self.errors}


def create_example_plugin(plugin_dir: Path = DEFAULT_PLUGIN_DIR) -> Path:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    path = plugin_dir / "example_tools.py"
    if not path.exists():
        path.write_text('''def register():
    def hello(args):
        return {"message": "Merhaba " + str(args.get("name", "efendim"))}
    return [{"name": "plugin_hello", "description": "Örnek yerel eklenti aracı", "handler": hello, "schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}]
''', encoding="utf-8")
    return path
