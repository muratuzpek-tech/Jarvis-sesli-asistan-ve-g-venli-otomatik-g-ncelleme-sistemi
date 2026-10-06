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
import logging

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


def audit_entry(tool_name: str, params: dict, result: str, approved: bool, *,
                executed: bool | None = None, source: str | None = None,
                verdict: str | None = None, fingerprint: str | None = None) -> None:
    """Geriye donuk uyumlu sarmalayici: TEK denetim kaydina
    (JARVIS_HOME/memory/audit.log, core/audit_log.log_action bicimi) yazar.
    Eski ~/.jarvis/audit.log artik kullanilmaz; params maskelenir.
    approved: gercek kullanici onayi; executed verilmezse approved'a esittir."""
    from jarvis.core.audit_log import log_action
    log_action(
        module="tool_gate", action=str(tool_name),
        approval_required=bool(verdict == "needs_approval"),
        result=str(result or ""), source=source, verdict=verdict,
        fingerprint=fingerprint, tool=str(tool_name), params=dict(params or {}),
        approved=bool(approved),
        executed=bool(approved) if executed is None else bool(executed),
    )


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
