"""Dilden bağımsız araç zinciri yardımcıları (bkz. devkit/__init__.py)."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Bir çalıştırmanın modele/kullanıcıya gösterilecek en fazla çıktı uzunluğu.
MAX_OUTPUT_CHARS = 6000


@dataclass(frozen=True)
class Diagnostic:
    """Derleyici/vet/linter'ın tek bir bulgusu."""

    path: str          # proje köküne göre, "/" ayraçlı
    line: int
    col: int
    message: str
    source: str        # "build" | "vet" | "lint" | "run"

    def render(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: [{self.source}] {self.message}"


@dataclass
class CommandResult:
    """run_tool() sonucu. ok = süreç 0 ile bitti ve zaman aşımı olmadı."""

    args: list[str]
    returncode: int | None
    output: str
    timed_out: bool = False
    not_found: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.not_found


def _kill_tree(proc: subprocess.Popen) -> None:
    """Süreci VE başlattığı alt süreçleri öldürür (Linux/macOS: süreç grubu)."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_tool(
    args: Sequence[str],
    cwd: Path,
    timeout: float,
    env: Mapping[str, str] | None = None,
    replace_env: bool = False,
) -> CommandResult:
    """Bir komutu GÜVENLİ biçimde çalıştırır.

    * shell=False — argümanlar liste olarak geçer, kabuk yorumlaması (;, &&,
      $(), yönlendirme) YOKTUR; komut enjeksiyonu yüzeyi oluşmaz.
    * Çıktı PIPE yerine geçici dosyaya yazılır (dev_agent._run_project'teki
      antivirüs/EDR PIPE kilitlenmesi dersine uygun).
    * Zaman aşımında tüm süreç grubu öldürülür; o ana kadarki çıktı korunur.
    """
    argv = [str(a) for a in args]
    # replace_env=True: güvenlik kafesi ortamı SIFIRDAN kurar (API anahtarları aktarılmaz).
    full_env = dict(env or {}) if replace_env else dict(os.environ)
    if env and not replace_env:
        full_env.update(env)

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=full_env,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=(sys.platform != "win32"),
            )
        except FileNotFoundError:
            return CommandResult(argv, None, f"Komut bulunamadı: {argv[0]}", not_found=True)

        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)

        out.seek(0)
        text = out.read()

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS // 2] + "\n...[kısaltıldı]...\n" + text[-MAX_OUTPUT_CHARS // 2:]
    return CommandResult(argv, proc.returncode, text.strip(), timed_out=timed_out)


# "./main.go:6:2: msg", "vet: ./pkg/x.go:3:1: msg", "main.go:10:2: msg (errcheck)"
_DIAG_RE = re.compile(
    r"^(?:vet:\s*)?(?:\.[/\\])?(?P<path>[\w\-./\\]+\.go):(?P<line>\d+)(?::(?P<col>\d+))?:\s*(?P<msg>.+)$"
)


def parse_diagnostics(output: str, source: str, known_paths: set[str] | None = None) -> list[Diagnostic]:
    """Araç çıktısındaki "dosya:satır:sütun: mesaj" satırlarını ayrıştırır.

    known_paths verilirse yalnızca projeye ait dosyalar döner (ör. modül
    önbelleğindeki üçüncü parti dosyalar dışarıda kalır). Aynı bulgu birden
    fazla kez raporlanırsa (build + typecheck) tekilleştirilir.
    """
    seen: set[tuple[str, int, int, str]] = set()
    result: list[Diagnostic] = []
    for raw in output.splitlines():
        m = _DIAG_RE.match(raw.strip())
        if not m:
            continue
        path = m.group("path").replace("\\", "/")
        if known_paths is not None and path not in known_paths:
            continue
        msg = m.group("msg").strip()
        if msg.startswith(": #") or not msg:
            continue  # golangci-lint'in paket başlığı gürültüsü
        key = (path, int(m.group("line")), int(m.group("col") or 0), msg)
        if key in seen:
            continue
        seen.add(key)
        result.append(Diagnostic(path, key[1], key[2], msg, source))
    return result


def group_by_file(diags: Sequence[Diagnostic]) -> dict[str, list[Diagnostic]]:
    grouped: dict[str, list[Diagnostic]] = {}
    for d in diags:
        grouped.setdefault(d.path, []).append(d)
    return grouped
