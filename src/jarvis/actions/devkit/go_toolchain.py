"""Go araç zinciri — LLM'siz, deterministik adımlar (bkz. devkit/__init__.py).

Tüm komutlar toolchain.run_tool() ile shell=False çalışır. Ortam:
  GOTOOLCHAIN=local  — go.mod başka bir Go sürümü istese bile İNTERNETTEN
                       yeni bir araç zinciri indirilmez.
  GOFLAGS=-mod=mod   — go.mod/go.sum eksikleri build sırasında da çözülür.
Üretilen ikili proje içindeki .jarvis/ klasörüne yazılır; "." ile başlayan
klasörleri go araçları paket olarak görmez, bu yüzden derlemeye karışmaz.
"""
from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from jarvis.actions.devkit.toolchain import (
    CommandResult,
    Diagnostic,
    parse_diagnostics,
    run_tool,
)

GO_ENV = {"GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod"}
WORK_DIR = ".jarvis"            # ikili + yedekler; go araçları yok sayar


def binary_name(platform: str | None = None) -> str:
    """Windows'ta calistirilabilir dosya .exe ile bitmeli."""
    return "app.exe" if (platform or sys.platform) == "win32" else "app"


BINARY_NAME = binary_name()

_MODULE_RE = re.compile(r"^[a-z0-9][a-z0-9._\-]*(/[a-z0-9][a-z0-9._\-]*)*$")


def sanitize_module_name(name: str) -> str:
    """Planlayıcının önerdiği modül adını güvenli, geçerli bir Go modül yoluna çevirir."""
    cleaned = re.sub(r"[^a-z0-9._/\-]", "-", (name or "").strip().lower()).strip("-/.")
    cleaned = re.sub(r"/{2,}", "/", cleaned)
    if not cleaned or not _MODULE_RE.match(cleaned) or ".." in cleaned:
        return "jarvisapp"
    return cleaned


@dataclass
class ToolAvailability:
    go: str | None
    gofmt: str | None
    golangci_lint: str | None

    @property
    def usable(self) -> bool:
        return self.go is not None


class GoToolchain:
    """Tek bir proje klasörü için Go adımları."""

    def __init__(self, project_dir: Path, timeout: float = 120.0) -> None:
        self.project_dir = project_dir
        self.timeout = timeout
        self.tools = ToolAvailability(
            go=shutil.which("go"),
            gofmt=shutil.which("gofmt"),
            golangci_lint=shutil.which("golangci-lint"),
        )

    # ── yardımcılar ──────────────────────────────────────────────────────
    def _go(self, *args: str, timeout: float | None = None) -> CommandResult:
        assert self.tools.go is not None
        return run_tool([self.tools.go, *args], self.project_dir, timeout or self.timeout, GO_ENV)

    def source_files(self) -> set[str]:
        """Projedeki .go dosyaları (proje köküne göre, "/" ayraçlı)."""
        files: set[str] = set()
        for p in self.project_dir.rglob("*.go"):
            rel = p.relative_to(self.project_dir)
            if any(part.startswith((".", "_")) for part in rel.parts[:-1]):
                continue  # go araçlarının da yok saydığı klasörler
            files.add(rel.as_posix())
        return files

    # ── hazırlık ─────────────────────────────────────────────────────────
    def version(self) -> str:
        r = self._go("version", timeout=20)
        return r.output if r.ok else ""

    def init_module(self, module: str) -> CommandResult:
        if (self.project_dir / "go.mod").is_file():
            return CommandResult(["go", "mod", "init"], 0, "go.mod zaten var")
        return self._go("mod", "init", sanitize_module_name(module), timeout=30)

    def gofmt(self) -> CommandResult:
        """Biçimlendirmeyi deterministik olarak düzeltir (LLM gerekmez)."""
        if self.tools.gofmt is None:
            return CommandResult(["gofmt"], 0, "gofmt bulunamadı, atlandı")
        files = sorted(self.source_files())
        if not files:
            return CommandResult(["gofmt"], 0, "biçimlenecek dosya yok")
        return run_tool([self.tools.gofmt, "-w", *files], self.project_dir, 60)

    def tidy(self) -> CommandResult:
        """Bağımlılıkları go.mod/go.sum'a işler (gerekirse indirir)."""
        return self._go("mod", "tidy", timeout=max(self.timeout, 180))

    # ── doğrulama ────────────────────────────────────────────────────────
    def build(self) -> tuple[CommandResult, list[Diagnostic]]:
        out = Path(WORK_DIR) / BINARY_NAME
        (self.project_dir / WORK_DIR).mkdir(exist_ok=True)
        r = self._go("build", "-o", str(out), ".")
        return r, ([] if r.ok else parse_diagnostics(r.output, "build", self.source_files()))

    def vet(self) -> tuple[CommandResult, list[Diagnostic]]:
        r = self._go("vet", "./...")
        return r, ([] if r.ok else parse_diagnostics(r.output, "vet", self.source_files()))

    def lint(self) -> tuple[CommandResult | None, list[Diagnostic]]:
        """golangci-lint v2 'standard' seti: errcheck, govet, ineffassign,
        staticcheck, unused. Kurulu değilse (None, []) döner — doğrulama
        engellenmez, kullanıcıya atlandığı bildirilir."""
        if self.tools.golangci_lint is None:
            return None, []
        r = run_tool(
            [self.tools.golangci_lint, "run", "--default=standard", "--show-stats=false", "./..."],
            self.project_dir, max(self.timeout, 180), GO_ENV,
        )
        return r, ([] if r.ok else parse_diagnostics(r.output, "lint", self.source_files()))

    # ── çalıştırma ───────────────────────────────────────────────────────
    def run_binary(self, args: list[str], timeout: float) -> CommandResult:
        binary = self.project_dir / WORK_DIR / BINARY_NAME
        if not binary.is_file():
            return CommandResult([str(binary)], None, "Derlenmiş ikili bulunamadı", not_found=True)
        # Derlenen program da güvenlik kafesinde çalışır (bkz. devkit/sandbox.py).
        from jarvis.actions.devkit import sandbox
        argv, env, state = sandbox.wrap([str(binary), *args], self.project_dir, log=lambda m: None)
        if state.startswith("REFUSED"):
            return CommandResult(argv, None, state, not_found=True)
        return run_tool(argv, self.project_dir, timeout, env, replace_env=env is not None)
