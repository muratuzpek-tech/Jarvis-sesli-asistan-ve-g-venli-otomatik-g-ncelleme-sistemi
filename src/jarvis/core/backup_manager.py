"""Centralised backup and rollback for file-mutating operations.

Her dosya degisikliginden ONCE otomatik yedek alinir.
~/.jarvis/memory/backups/ altinda saklanir + index JSON'a yazilir.

Ornek kullanim:
    from jarvis.core.backup_manager import safe_modify, rollback
    safe_modify(pathlib.Path("/tmp/x.py"), b"new content")
    rollback("<backup_id>")
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

_lock = threading.Lock()
_TR_TZ = timezone(timedelta(hours=3))

_MAX_BACKUPS = 200


def _backup_root() -> Path:
    from jarvis.paths import memory_dir
    return memory_dir() / "backups"


def _index_path() -> Path:
    return _backup_root() / "index.json"


def _load_index() -> list[dict]:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def _save_index(records: list[dict]) -> None:
    _index_path().parent.mkdir(parents=True, exist_ok=True)
    _index_path().write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prune_old(records: list[dict]) -> list[dict]:
    if len(records) > _MAX_BACKUPS:
        removed = records[: len(records) - _MAX_BACKUPS]
        for rec in removed:
            old_file = _backup_root() / rec.get("backup_name", "")
            old_file.unlink(missing_ok=True)
        records = records[len(records) - _MAX_BACKUPS:]
    return records


def safe_modify(target: Path, new_content: bytes) -> dict:
    target = Path(target)
    backup_id = str(uuid.uuid4())[:8]

    with _lock:
        root = _backup_root()
        root.mkdir(parents=True, exist_ok=True)

        original_existed = target.exists()
        backup_name = f"{backup_id}_{target.name}"

        if original_existed:
            backup_dest = root / backup_name
            shutil.copy2(target, backup_dest)
        else:
            (root / backup_name).write_bytes(b"")

        record = {
            "backup_id": backup_id,
            "target": str(target),
            "backup_name": backup_name,
            "original_existed": original_existed,
            "timestamp": datetime.now(_TR_TZ).isoformat(),
            "size_original": target.stat().st_size if original_existed else 0,
        }

        records = _load_index()
        records.append(record)
        records = _prune_old(records)
        _save_index(records)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(new_content)
        except Exception:
            if original_existed:
                backup_src = root / backup_name
                if backup_src.exists() and backup_src.stat().st_size > 0:
                    shutil.copy2(backup_src, target)
            raise

    from jarvis.core.audit_log import log_action
    log_action(module="backup_manager", action="safe_modify",
               detail=f"target={target} backup_id={backup_id}",
               risk="medium", result="BACKUP_AND_WRITE_OK",
               extra={"backup_id": backup_id})

    return {
        "backup_id": backup_id,
        "backup_path": str(root / backup_name),
        "original_existed": original_existed,
    }


def rollback(backup_id: str) -> str:
    with _lock:
        records = _load_index()
        record = None
        for r in records:
            if r.get("backup_id") == backup_id:
                record = r
                break

        if record is None:
            return f"Backup ID '{backup_id}' bulunamadi."

        target = Path(record["target"])
        backup_file = _backup_root() / record["backup_name"]

        if not record.get("original_existed"):
            target.unlink(missing_ok=True)
        elif backup_file.exists() and backup_file.stat().st_size > 0:
            shutil.copy2(backup_file, target)
        else:
            return f"Yedek dosya eksik veya bos: {record['backup_name']}"

    from jarvis.core.audit_log import log_action
    log_action(module="backup_manager", action="rollback",
               detail=f"backup_id={backup_id} target={target}",
               risk="high", result="ROLLBACK_OK",
               extra={"backup_id": backup_id})

    return f"Rollback tamamlandi: {target}"


def list_backups(limit: int = 20) -> list[dict]:
    records = _load_index()
    return records[-limit:][::-1]


def get_latest_for(target: Path) -> dict | None:
    target_str = str(Path(target))
    records = _load_index()
    for r in reversed(records):
        if r.get("target") == target_str:
            return r
    return None
