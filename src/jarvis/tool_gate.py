"""Merkezi arac onay kapisi + audit log.

GUVENLIK (2026-10-05): Kapi artik onay KODU uretmez ve modelin gonderdigi
confirm_code'u hicbir zaman onay saymaz. Eskiden bos olmayan HER confirm_code
araci gecirirdi ve gercek kod "ONAY_KODU: X" diye modele gosterilirdi; model
kullanici hic onay vermeden araci calistirabiliyordu. Onay yalnizca
kullanicinin gercek turundan gelir: main.py araci, arguman-bagli tek
kullanimlik onayi (_consume_dangerous_confirmation) tukettiyse
user_approved=True ile cagirir.
"""

from __future__ import annotations
import json
import logging
from datetime import datetime, UTC
from pathlib import Path

log = logging.getLogger(__name__)

_DESTRUCTIVE_TOOLS = {
    "send_message", "shutdown_jarvis", "self_improve",
    "file_delete",  "pip_install",
    "git_push", "computer_control",
}
_CONFIRM_TOOLS = {
    "agentic_code", "open_app",
}
# Disaridan entegre edilmis kod (discovered_topydo, discovered_jc ve
# ileride eklenecek her discovered_* araci) fail-closed: her cagri onay ister.
_DISCOVERED_PREFIX = "discovered_"


def is_destructive(tool_name: str) -> bool:
    return tool_name in _DESTRUCTIVE_TOOLS or tool_name.startswith(_DISCOVERED_PREFIX)


def needs_confirmation(tool_name: str) -> bool:
    return is_destructive(tool_name) or tool_name in _CONFIRM_TOOLS


def _audit_log() -> Path:
    return Path.home() / ".jarvis" / "audit.log"


def audit_entry(tool_name: str, params: dict, result: str, approved: bool):
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool_name,
        "params": {k: str(v)[:80] for k, v in (params or {}).items()},
        "result": str(result or "")[:200],
        "approved": approved,
        "destructive": is_destructive(tool_name),
    }
    apath = _audit_log()
    apath.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(apath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("Audit log yazilamadi")


def gate(tool_name: str, params: dict, user_approved: bool = False) -> str | None:
    """Onay gerekiyorsa modele gidecek CONFIRMATION_REQUIRED metni, yoksa None.
    params icindeki confirm_code hicbir zaman dikkate alinmaz."""
    if not needs_confirmation(tool_name) or user_approved:
        return None
    danger = "TEHLIKELI" if is_destructive(tool_name) else "DIKKAT"
    ps = ", ".join(f"{k}={str(v)[:40]}" for k, v in (params or {}).items() if k != "confirm_code")
    return (
        f"CONFIRMATION_REQUIRED:{tool_name}:"
        f"{danger} arac cagrisi: {ps}. Kullaniciya ne yapilacagini TEK cumleyle "
        f"anlat ve 'evet' ya da 'hayir' demesini iste. Kullanici bir sonraki "
        f"mesajinda acikca onaylarsa araci AYNI parametrelerle BIR KEZ tekrar "
        f"cagir; onay kodu yoktur, onay gelmeden cagirma."
    )
