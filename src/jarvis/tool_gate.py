"""Merkezi arac onay kapisi + audit log."""

from __future__ import annotations
import json
import logging
import secrets
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


def is_destructive(tool_name: str) -> bool:
    return tool_name in _DESTRUCTIVE_TOOLS


def needs_confirmation(tool_name: str) -> bool:
    return tool_name in _DESTRUCTIVE_TOOLS or tool_name in _CONFIRM_TOOLS


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


# Son uretilen kodu tool_name bazinda hatirla
_LAST_PENDING: dict[str, str] = {}  # tool_name → code

def gate(tool_name: str, params: dict, user_approved: bool = False) -> str | None:
    _cc = params.get("confirm_code", "")
    # terminal kendi confirm_code'unu isler — gate'de tuketme
    if tool_name == "terminal":
        return None

    # ═══ EXPLICIT confirm_code varsa → dogrudan tuket ═══
    if _cc and tool_name in _DESTRUCTIVE_TOOLS:
        _stored = _PENDING_CONFIRMATIONS.get(_cc)
        if _stored and _stored[0] == tool_name:
            del _PENDING_CONFIRMATIONS[_cc]
            _LAST_PENDING.pop(tool_name, None)
            return None  # ONAYLANDI!
    # confirm_code varsa → gecti
    if params.get("confirm_code"):
        _LAST_PENDING.pop(tool_name, None)
        return None

    # ═══ USER APPROVED (kodsuz onay) → son pending kodu otomatik tuket ═══
    if user_approved and not _cc:
        _last = _LAST_PENDING.get(tool_name)
        if _last and _last in _PENDING_CONFIRMATIONS:
            _stored = _PENDING_CONFIRMATIONS.pop(_last, None)
            _LAST_PENDING.pop(tool_name, None)
            return None  # OTOMATIK ONAYLANDI!

    if needs_confirmation(tool_name) and not user_approved:
        danger = "TEHLIKELI" if is_destructive(tool_name) else "DIKKAT"
        ps = ", ".join(f"{k}={str(v)[:40]}" for k, v in (params or {}).items())
        _code = generate_confirm_code(tool_name, params)
        return (
            f"CONFIRMATION_REQUIRED:{tool_name}:"
            f"{danger} arac cagrisi: {ps} - Onayliyor musunuz? (evet/hayir). "
            f"ONAY_KODU: {_code} — Kullanici onaylarsa confirm_code='{_code}' ile tekrar cagir."
        )
    return None


# ═══ CONFIRM CODE SYSTEM ═══
_PENDING_CONFIRMATIONS: dict = {}

def generate_confirm_code(tool_name: str, args: dict) -> str:
    code = secrets.token_hex(3).upper()
    _PENDING_CONFIRMATIONS[code] = (tool_name, dict(args))
    _LAST_PENDING[tool_name] = code  # KEYWORD_FALLBACK
    return code

def consume_confirm_code(code: str):
    return _PENDING_CONFIRMATIONS.pop(code, None)
