"""Onaylanan bir araç çağrısının kimliği (parmak izi) - TEK uygulama.

security_gate.fingerprint bunu yeniden dışa aktarır (kapının API'si);
core/audit_log da aynı fonksiyonu kullanır ki agent_loop / Brain Team
olaylarının parmak izi kapı kayıtlarıyla eşleşsin. Bu modül bilerek hiçbir
jarvis modülüne bağımlı değildir (denetim kaydı kapıyı import etmesin).
"""
from __future__ import annotations

import json
from typing import Any


def fingerprint(action: str, args: Any) -> str:
    """Araç adı + argümanlar. Sayılar registry.execute'taki gibi str'ye
    çevrilir (3 ve "3" aynı işlemdir)."""
    if isinstance(args, dict):
        args = {k: str(v) if isinstance(v, (int, float)) else v for k, v in args.items()}
    return action + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
