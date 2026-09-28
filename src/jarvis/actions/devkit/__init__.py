"""devkit — dev_agent için dile özgü araç zinciri (toolchain) katmanı.

MİMARİ (2026-09-28):
dev_agent.py'nin ana akışı (planla → yaz → kur → çalıştır → düzelt) Python'a
göre yazılmış ve üzerinde onlarca canlı-test yaması var; ona dokunmak riskli.
Yeni diller bu paket üzerinden, o akışa HİÇ dokunmadan eklenir:

  toolchain.py    Dilden bağımsız ortak tipler: Diagnostic, CommandResult,
                  run_tool() (shell=False, zaman aşımlı, süreç grubu öldüren
                  güvenli komut çalıştırıcı) ve parse_diagnostics().
  go_toolchain.py Go'ya özgü, LLM'siz, deterministik adımlar: go mod init,
                  gofmt, go mod tidy, go build, go vet, golangci-lint,
                  derlenmiş ikiliyi çalıştırma.
  go_builder.py   Go proje akışı: LLM ile planla/yaz → araç zinciriyle
                  doğrula → bulguları LLM'e düzelttir → çalıştır → beklenen
                  çıktıları doğrula. LLM bir fonksiyon olarak enjekte edilir
                  (generate(prompt) -> str), bu yüzden gerçek model olmadan
                  test edilebilir.

Yeni bir dil (ör. Rust) eklemek = <dil>_toolchain.py + <dil>_builder.py yazıp
BUILDERS sözlüğüne kaydetmek; dev_agent.py'de değişiklik gerekmez.
"""
from __future__ import annotations

from collections.abc import Callable

from jarvis.actions.devkit.go_builder import build_go_project

# Dil adı (küçük harf) -> proje kurucu. dev_agent._build_project bu sözlüğe
# bakar; burada olmayan diller mevcut Python akışında kalır.
BUILDERS: dict[str, Callable[..., str]] = {
    "go": build_go_project,
    "golang": build_go_project,
}


def get_builder(language: str) -> Callable[..., str] | None:
    """Dil için özel bir kurucu varsa onu, yoksa None döndürür."""
    return BUILDERS.get((language or "").strip().lower())


__all__ = ["BUILDERS", "get_builder"]
