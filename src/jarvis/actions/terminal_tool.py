"""Controlled terminal tool for JARVIS.

Only read-only, explicitly allowlisted commands execute immediately.  Any
other command is previewed and requires a short-lived application approval
code.  Commands are always executed with shell=False and inside the user's
home directory.
"""
from __future__ import annotations

import os
import shlex
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from jarvis.core.approval_service import approval_service
import threading
import time
from jarvis.core.audit_log import log_action

_MAX_OUTPUT = 12000
_DEFAULT_TIMEOUT = 60
_DIAGNOSTIC_TIMEOUT = 300
_MAX_TIMEOUT = 900

_READONLY_PROGRAMS = {"pwd", "ls", "cat", "head", "tail", "grep", "rg", "find", "which", "whoami", "uname", "df", "du"}
_GIT_READONLY = {"status", "diff", "log", "show", "branch", "rev-parse"}
_PYTHON_SAFE = {("--version",), ("-V",)}

# Salt-okunur programlarin dosya silen/yazan veya baska program calistiran
# bayraklari. Bunlardan biri varsa komut onay koduna duser.
_FIND_WRITE_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                       "-fprint", "-fprint0", "-fprintf", "-fls"}
_RG_EXEC_FLAGS = {"--pre", "--hostname-bin"}
_GIT_WRITE_FLAGS = {"--output"}
# `git branch` sadece listeleme bayraklariyla salt-okunur; konumsal arguman
# dal olusturur, -d/-D/-m/-c siler/tasir/kopyalar.
_GIT_BRANCH_LIST_FLAGS = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
                          "-l", "--list", "--show-current", "--no-color", "--color"}


def _flag_name(token: str) -> str:
    return token.split("=", 1)[0]


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


def _expand_home_tokens(argv: list[str]) -> list[str]:
    """Komutlar shell=False ile calistigi icin "~" kabukca genisletilmez:
    `touch ~/x` ev dizinine degil calisma klasorundeki "~" klasorune yazar,
    onizleme de kullaniciya genisletilmemis yolu gosterirdi. Tam olarak "~"
    olan ve "~/" ile baslayan token'lar os.path.expanduser ile genisletilir;
    "~kullanici", "--opt=~/x" ve ortasinda "~" gecenler degistirilmez."""
    return [os.path.expanduser(t) if t == "~" or t.startswith("~/") else t for t in argv]


def _resolve_python_command(argv: list[str]) -> list[str]:
    """Use Jarvis's interpreter when a bare Python name is absent from PATH.

    Desktop launches do not always inherit the user's activated virtualenv.
    Falling back to the interpreter running Jarvis keeps ``python -m pytest``
    deterministic without changing commands when a PATH Python exists.
    """
    if not argv or argv[0].lower() not in {"python", "python3"}:
        return argv
    if shutil.which(argv[0]):
        return argv
    return [sys.executable, *argv[1:]]


def _escapes_home(token: str, home: Path) -> bool:
    """Mutlak ve ".." bileseni iceren bir yol ev dizininin disina mi cikiyor?
    (ör. genisletilmis "~/../etc/passwd")."""
    if not token.startswith("/") or ".." not in Path(token).parts:
        return False
    try:
        return not Path(token).resolve().is_relative_to(home)
    except OSError:
        return True


def _is_readonly(argv: list[str]) -> bool:
    if not argv:
        return False
    # Yol denetimleri GENISLETILMIS yol uzerinden yapilir; terminal_tool ve
    # security_gate (terminal etkisi) ayni sonucu alsin diye burada da.
    argv = _expand_home_tokens(argv)
    # "./ls" ya da "/tmp/x/git" calisma klasorundeki/keyfi bir ikiliyi
    # calistirir; salt-okunur liste yalnizca PATH'teki ciplak adlar icindir.
    if "/" in argv[0] or "\\" in argv[0] or Path(argv[0]).name != argv[0]:
        return False
    program = argv[0].lower()
    args = argv[1:]
    home = Path.home().resolve()
    _ro_prog = program in _READONLY_PROGRAMS
    for token in args:
        if token.startswith("-"):
            continue
        # ".." ile ev dizininden DISARI cikan yol hicbir programda salt-okunur
        # sayilmaz (salt-okunur programlar dahil).
        if _escapes_home(token, home):
            return False
        if token.startswith("/"):
            try:
                if not Path(token).resolve().is_relative_to(home):
                    if not _ro_prog:
                        return False
            except OSError:
                if not _ro_prog:
                    return False
        if token.startswith("../") or token == ".." or "/../" in token:  # noqa: S105 - token bir komut argumani (yol), parola degil
            if not _ro_prog:
                return False
    flags = {_flag_name(t) for t in args if t.startswith("-")}
    if program == "find" and flags & _FIND_WRITE_ACTIONS:
        return False
    if program == "rg" and flags & _RG_EXEC_FLAGS:
        return False
    if program in _READONLY_PROGRAMS:
        return True
    if program == "git":
        if not args or args[0] not in {x.strip() for x in _GIT_READONLY}:
            return False
        if flags & _GIT_WRITE_FLAGS:
            return False
        if args[0] == "branch":
            return all(t in _GIT_BRANCH_LIST_FLAGS for t in args[1:])
        return True
    if program in {"python", "python3", Path(sys.executable).name.lower()}:
        return tuple(args) in _PYTHON_SAFE
    return False


def _preview(argv: list[str], cwd: Path, timeout: int) -> str:
    item = approval_service.request(
        "terminal",
        f"Komut: {_display_command(argv)} | Çalışma klasörü: {cwd}",
    )
    return (
        "ONAY GEREKLİ (komut henüz çalıştırılmadı).\n"
        f"Komut: {_display_command(argv)}\n"
        f"Çalışma klasörü: {cwd}\n"
        f"Timeout: {timeout} saniye\n"
        f"Onay kodu: {item.code}\n"
        "Kullanıcı bu işlemi açıkça onayladıktan sonra aynı komutu confirm_code ile tekrar çağır."
    )


def _is_pytest_command(argv: list[str]) -> bool:
    """Return whether argv invokes pytest through a Python module or script."""
    names = {Path(str(token)).name.lower() for token in argv}
    return "pytest" in names or any(name.startswith("pytest") for name in names)


def _is_diagnostic_command(argv: list[str]) -> bool:
    """Identify bounded, potentially longer read/diagnostic commands.

    This is intentionally only a timeout policy decision; approval and command
    safety remain governed by ``_is_readonly`` and the approval service.
    """
    if _is_pytest_command(argv):
        return True
    names = {Path(str(token)).name.lower() for token in argv}
    return bool(names & {"ruff", "compileall", "diagnose_all.py"})


def _command_timeout(parameters: dict, argv: list[str]) -> int:
    """Resolve a bounded timeout, with a longer default for diagnostics."""
    requested = parameters.get("timeout")
    if requested is None:
        return _DIAGNOSTIC_TIMEOUT if _is_diagnostic_command(argv) else _DEFAULT_TIMEOUT
    try:
        timeout = int(requested)
    except (TypeError, ValueError):
        raise ValueError("timeout saniye cinsinden bir tam sayı olmalı.") from None
    if not 1 <= timeout <= _MAX_TIMEOUT:
        raise ValueError(f"timeout 1 ile {_MAX_TIMEOUT} saniye arasında olmalı.")
    return timeout


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

    # ═══ CD PIPELINE DESTEGI ═══
    # "cd /path && python3 main.py" → cwd=/path, command="python3 main.py"
    # "cd /path ; python3 main.py"  → cwd=/path, command="python3 main.py"
    _cd_match = re.match(r'^cd\s+([^;&|]+?)\s*(?:&&|;)\s*(.+)$', command, re.DOTALL)
    if _cd_match:
        _cd_target = _cd_match.group(1).strip().strip("'\"")
        _rest = _cd_match.group(2).strip()
        params["cwd"] = _cd_target
        command = _rest
        if not command:
            return f"Dizin değiştirildi: {_cd_target}"

    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        return f"Komut ayrıştırılamadı: {exc}"
    if not argv:
        return "Komut belirtilmedi."
    # Onizlemeden, onay ozetinden ve calistirmadan ONCE: kullanicinin gordugu
    # ve onayladigi komut, calisacak komutun aynisi olsun.
    argv = _expand_home_tokens(argv)
    argv = _resolve_python_command(argv)

    try:
        cwd = _resolve_cwd(params.get("cwd"))
    except ValueError as exc:
        return str(exc)

    try:
        timeout_seconds = _command_timeout(params, argv)
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
            return _preview(argv, cwd, timeout_seconds)

    # ===== INTERAKTIF -> XTERM AC =====
    _should_xterm = False
    _target_py = next((str(a) for a in argv if str(a).endswith(('.py', '.python'))), None)
    if _target_py and not Path(_target_py).is_absolute():
        _target_py = str(Path(cwd) / _target_py)
    if _target_py:
        try:
            _txt = Path(_target_py).read_text()
            if 'input(' in _txt or 'tkinter' in _txt or 'Tk()' in _txt:
                _should_xterm = True
        except Exception:
            pass
    if _should_xterm:
        import shutil as _sh
        _term = None
        for _c in ('xterm', 'konsole', 'gnome-terminal', 'kitty', 'alacritty'):
            if _sh.which(_c):
                _term = _c
                break
        if _term:
            _cmd_str = _display_command(argv)
            _pause = '; echo; read -p "_" _x'
            _inner = "cd '" + str(cwd) + "' && " + _cmd_str + _pause
            _full = [_term]
            if _term == 'gnome-terminal':
                _full += ['--', 'bash', '-lc', _inner]
            else:
                _full += ['-e', 'bash', '-c', _inner]
            try:
                subprocess.Popen(
                    _full,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return (
                    'PROGRAM YENI TERMINALDE ACILDI!' + chr(10) +
                    'Komut: ' + _cmd_str + chr(10) +
                    'Klasor: ' + str(cwd) + chr(10) +
                    'Terminal: ' + _term + chr(10) +
                    'Kullanici pencereden interaktif kullanabilir.'
                )
            except Exception as _e:
                return 'Terminal acma hatasi: ' + str(_e)

    # ═══ STDIN DESTEGI ═══
    # input() bekleyen programlar icin stdin verisi
    # params["input"] ile LLM gonderebilir; yoksa bos string
    stdin_data = params.get("input", params.get("stdin", ""))
    if not stdin_data and any(
        Path(arg).suffix == ".py" for arg in argv if Path(arg).suffix == ".py"
    ):
        # Python scriptleri muhtemelen input() ile menu sunuyor
        stdin_data = "0\n0\n0\n0\n0\n"

    if Path(argv[0]).name.lower() in ("git", "git.exe"):
        # Depo yapilandirmasindaki core.fsmonitor keyfi program calistirabilir;
        # salt-okunur "git status" bile bunu tetiklerdi.
        argv = [argv[0], "-c", "core.fsmonitor=false", *argv[1:]]
    run_env = os.environ.copy()
    # Project tests must never write the user's real audit/memory files.  Keep
    # the temporary data directory alive for the child process only; an
    # explicitly supplied JARVIS_HOME remains the caller's deliberate choice.
    diagnostic_home = None
    if _is_pytest_command(argv) and "JARVIS_HOME" not in run_env:
        diagnostic_home = tempfile.TemporaryDirectory(prefix="jarvis-terminal-")
        run_env["JARVIS_HOME"] = diagnostic_home.name
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=run_env,
            input=stdin_data if stdin_data else None,
        )
    except subprocess.TimeoutExpired as exc:
        # ═══ TIMEOUT'TA PARTIAL OUTPUT DON ═══
        _out = ((exc.stdout or "") if isinstance(exc.stdout, str)
                else (exc.stdout or b"").decode("utf-8", "replace")).strip()
        _err = ((exc.stderr or "") if isinstance(exc.stderr, str)
                else (exc.stderr or b"").decode("utf-8", "replace")).strip()
        parts = [f"KOMUT {timeout_seconds} SN'DE ZAMAN ASIMINA UGRADI"]
        parts.append("MUHTEMEL SEBEP: Program interaktif (input() bekliyor). "
                     "Input parametresi ile cevap gonderin: {\"command\": \"...\", \"input\": \"1\\n2\\n\"}")
        if _out:
            parts.append(f"stdout (kismi):\n{_out[-_MAX_OUTPUT:]}")
        if _err:
            parts.append(f"stderr (kismi):\n{_err[-_MAX_OUTPUT:]}")
        return "\n\n".join(parts)
    except FileNotFoundError:
        return f"Komut bulunamadı: {argv[0]}"
    except OSError as exc:
        return f"Komut çalıştırılamadı: {type(exc).__name__}: {exc}"
    finally:
        if diagnostic_home is not None:
            diagnostic_home.cleanup()

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    parts = [f"exit_code: {proc.returncode}"]
    if stdout:
        parts.append(f"stdout:\n{stdout[-_MAX_OUTPUT:]}")
    if stderr:
        parts.append(f"stderr:\n{stderr[-_MAX_OUTPUT:]}")
    if not stdout and not stderr and proc.returncode == 0:
        parts.append("Komut basariyla tamamlandi (cikti yok).")
    return "\n\n".join(parts)
