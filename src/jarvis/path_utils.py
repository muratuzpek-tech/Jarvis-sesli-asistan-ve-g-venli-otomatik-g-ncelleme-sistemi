"""Guvenli yol normalizasyonu + project registry."""

from __future__ import annotations
import os
from pathlib import Path

_WHITELIST_ROOTS = [
    Path.home(),
    Path("/tmp"),
    Path("/var/tmp"),
]


def safe_resolve(user_path: str) -> Path:
    raw = str(user_path or "").strip()
    if not raw:
        raise ValueError("Bos yol.")
    expanded = Path(raw).expanduser()
    resolved = expanded.resolve()
    if ".." in raw.split(os.sep):
        raise ValueError(f"Path traversal reddedildi: {raw}")
    allowed = any(
        resolved == root or resolved.is_relative_to(root)
        for root in _WHITELIST_ROOTS
    )
    if not allowed:
        raise ValueError(f"Izin verilmeyen kok: {resolved}")
    return resolved


def project_registry_path() -> Path:
    return Path.home() / ".jarvis" / "projects.json"


def register_project(name: str, root: Path, entry: str, status: str = "generated"):
    import json
    from datetime import datetime, timezone
    rp = project_registry_path()
    rp.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if rp.exists():
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[name] = {
        "project_root": str(root),
        "entry_point": entry,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    rp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data[name]


def lookup_project(name: str) -> dict | None:
    import json
    rp = project_registry_path()
    if not rp.exists():
        return None
    try:
        return json.loads(rp.read_text(encoding="utf-8")).get(name)
    except Exception:
        return None


def list_projects() -> dict:
    import json
    rp = project_registry_path()
    if not rp.exists():
        return {}
    try:
        return json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return {}
