"""Controlled terminal tool for JARVIS.

Only read-only, explicitly allowlisted commands execute immediately.  Any
other command is previewed and requires a short-lived application approval
code.  Commands are always executed with shell=False and inside the user's
home directory.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from jarvis.core.approval_service import approval_service
import threading
import time
from jarvis.core.audit_log import log_action

_MAX_OUTPUT = 12000
_TIMEOUT = 30

_READONLY_PROGRAMS = {"pwd", "ls", "cat", "head", "tail", "grep", "rg", "find", "which", "whoami", "uname", "df", "du"}
_GIT_READONLY = {"status", "diff", "log", "show", "branch", " rev-parse"}
_PYTHON_SAFE = {("--version",), ("-V",)}


def _display_command(argv: list[str]) -> str:
    return " ".join(shlex.quote(x) for x in argv)


def _resolve_cwd(raw: str | None) -> Path:
    cwd = Path(raw or Path.home()).expanduser()
    resolved = cwd.resolve()
    home = Path.home().resolve()
    if resolved != home and not resolved.is_relative_to(home):
        raise ValueError("Güvenlik: terminal çalışma klasörü kullanıcı ana dizini içinde olmalı.")
    if not resolved.is_dir():
        raise ValueError(f"Çalışma klasörü bulunamadı: {resolved}")
    return resolved


def _is_readonly(argv: list[str]) -> bool:
    if not argv:
        return False
    program = Path(argv[0]).name.lower()
    args = argv[1:]
    home = Path.home().resolve()
    for token in args:
        if token.startswith("-"):
            continue
        if token.startswith("/"):
            try:
                if not Path(token).resolve().is_relative_to(home):
                    return False
            except OSError:
                return False
        if token.startswith("../") or token == ".." or "/../" in token:
            return False
    if program in _READONLY_PROGRAMS:
        return True
    if program == "git":
        return bool(args) and args[0] in {x.strip() for x in _GIT_READONLY}
    if program in {"python", "python3", Path(sys.executable).name.lower()}:
        return tuple(args) in _PYTHON_SAFE
    return False


def _preview(argv: list[str], cwd: Path) -> str:
    item = approval_service.request(
        "terminal",
        f"Komut: {_display_command(argv)} | Çalışma klasörü: {cwd}",
    )
    return (
        "ONAY GEREKLİ (komut henüz çalıştırılmadı).\n"
        f"Komut: {_display_command(argv)}\n"
        f"Çalışma klasörü: {cwd}\n"
        f"Onay kodu: {item.code}\n"
        "Kullanıcı bu işlemi açıkça onayladıktan sonra aynı komutu confirm_code ile tekrar çağır."
    )


_TERMINAL_RATE_LIMIT = 30        # max 30 komut / dakika
_TERMINAL_CALL_TIMES: list[float] = []
_rate_lock = threading.Lock()


def _check_rate_limit() -> tuple[bool, str]:
    now = time.monotonic()
    cutoff = now - 60.0
    with _rate_lock:
        _TERMINAL_CALL_TIMES[:] = [t for t in _TERMINAL_CALL_TIMES if t > cutoff]
        if len(_TERMINAL_CALL_TIMES) >= _TERMINAL_RATE_LIMIT:
            return False, f"Rate limit: max {_TERMINAL_RATE_LIMIT} cmd/min."
        _TERMINAL_CALL_TIMES.append(now)
    return True, ""


def terminal_tool(
    parameters: dict | None = None,
    *,
    application_user_confirmation: bool = False,
) -> str:
    params = parameters or {}

    # GUVENLIK (2026-10-03): Rate limit — tum terminaller bu kontrolden gecer
    _rl_ok, _rl_msg = _check_rate_limit()
    if not _rl_ok:
        log_action(module="terminal_tool", action="rate_limit_block",
                   detail=str(params.get("command", ""))[:200],
                   risk="medium", result="BLOCKED")
        return _rl_msg

    command = str(params.get("command", "")).strip()
    if not command:
        return "Komut belirtilmedi."
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        return f"Komut ayrıştırılamadı: {exc}"
    if not argv:
        return "Komut belirtilmedi."

    try:
        cwd = _resolve_cwd(params.get("cwd"))
    except ValueError as exc:
        return str(exc)

    confirm_code = str(params.get("confirm_code", "")).strip()
    if not _is_readonly(argv):
        summary = f"Komut: {_display_command(argv)} | Çalışma klasörü: {cwd}"
        if not approval_service.consume(
            confirm_code,
            "terminal",
            summary,
            require_user_turn=not application_user_confirmation,
        ):
            log_action(module="terminal_tool", action="preview_required",
                       detail=_display_command(argv)[:200],
                       risk="high", approval_required=True,
                       result="AWAITING_APPROVAL")
            return _preview(argv, cwd)

    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return f"Komut {_TIMEOUT} saniyede zaman aşımına uğradı."
    except FileNotFoundError:
        return f"Komut bulunamadı: {argv[0]}"
    except OSError as exc:
        return f"Komut çalıştırılamadı: {type(exc).__name__}: {exc}"

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parts = [f"exit_code: {proc.returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout[-_MAX_OUTPUT:]}")
    if stderr:
        parts.append(f"stderr:\n{stderr[-_MAX_OUTPUT:]}")
    return "\n\n".join(parts)
