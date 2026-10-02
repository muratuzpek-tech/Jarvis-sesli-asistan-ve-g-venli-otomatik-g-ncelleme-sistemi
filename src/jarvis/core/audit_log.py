"""Structured audit trail — kim ne yapti, ne zaman, sonuc ne oldu.

Guvenlik-relevant her olay JSON Lines olarak kaydedilir.
Dosya: ~/.jarvis/memory/audit.log

Ilkeler:
- Onay kodlari ASLA ham olarak loglanmaz (SHA-256 hash).
- Audit log hatasi asla ana islemi DURDURMAZ.
- Yazi basarisiz olursa stderr'e uyari yazilir.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timezone, timedelta

_lock = threading.Lock()
_TR_TZ = timezone(timedelta(hours=3))

# extra alaninin ustune YAZAMAYACAGI korumali alanlar
_PROTECTED_FIELDS = frozenset({
    "timestamp", "module", "action", "detail",
    "risk", "approval_required", "result", "approval_hash",
})


def _get_log_path():
    from jarvis.paths import memory_dir
    return memory_dir() / "audit.log"


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


def log_action(
    *,
    module: str,
    action: str,
    detail: str = "",
    risk: str = "low",
    approval_required: bool = False,
    approval_code: str = "",
    result: str = "",
    extra: dict | None = None,
) -> None:
    """Tek bir guvenlik-relevant islemi kaydet."""
    record: dict = {
        "timestamp": datetime.now(_TR_TZ).isoformat(),
        "module": str(module),
        "action": str(action),
        "detail": str(detail)[:500],
        "risk": str(risk),
        "approval_required": bool(approval_required),
        "result": str(result)[:500],
    }
    if approval_code:
        record["approval_hash"] = _hash_code(approval_code)
    if extra:
        for k, v in extra.items():
            if k not in _PROTECTED_FIELDS:
                record[k] = v

    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        log_path = _get_log_path()
        with _lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        print(f"[AUDIT-ERROR] Log yazilamadi: {exc}", file=sys.stderr)


def read_recent(count: int = 50) -> list[dict]:
    entries: list[dict] = []
    try:
        log_path = _get_log_path()
        with open(log_path, "r", encoding="utf-8") as fh:
            for line in fh.readlines()[-count:]:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return entries


def query(**filters) -> list[dict]:
    entries = read_recent(count=5000)
    if not filters:
        return entries
    return [
        e for e in entries
        if all(str(e.get(k, "")) == str(v) for k, v in filters.items())
    ]
