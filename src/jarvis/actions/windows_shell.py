"""
actions/windows_shell.py — Jarvis'in KONTROLLÜ, SALT-OKUNUR Windows sistem
sorgulama yeteneği.

NEDEN BU MODÜL VAR: Discovery/usability analiz sistemi bir aday aracın
(ör. jc) Jarvis tarafından GERÇEKTEN çağrılabilir olup olmadığını
değerlendiriyor. jc'nin retrospektif testinde ortaya çıkan gerçek sorun:
Jarvis'in HİÇBİR aracı ham bir CLI komutunun metin çıktısını üretmiyor —
yani "bir komutun çıktısını parse eden" araçların girdisini üretecek
hiçbir şey yoktu. Bu modül o boşluğu, GÜVENLİ bir şekilde kapatıyor.

GÜVENLİK TASARIMI (bilinçli sınırlar):
- BU BİR "HERHANGİ BİR KOMUTU ÇALIŞTIR" ARACI DEĞİLDİR. `run()` SADECE
  `parameters["command_name"]` okur — başka HİÇBİR anahtar (raw_command,
  args, script, vb.) subprocess'e asla ulaşmaz.
- Allowlist'teki her komutun argv listesi SABİT bir Python literal'idir.
  Hiçbir yerde kullanıcı/model verisiyle STRING BİRLEŞTİRME (concatenation)
  yapılmaz ve `shell=True` KESİNLİKLE kullanılmaz — bu yüzden injection
  yüzeyi validasyonla değil, MİMARİ OLARAK yok edilmiştir: `command_name`
  allowlist'te tam eşleşmiyorsa (ör. "process_list; Remove-Item ...")
  reddedilir, asla "en yakın eşleşmeyi" veya "yine de dene"yi denemez.
- İlk sürüm TAMAMEN SALT-OKUNUR: process/service/system/disk/network
  bilgisi sorgulanır. Process sonlandırma, servis başlatma/durdurma, dosya
  silme, registry değişikliği, kullanıcı/firewall değişikliği, keyfi
  PowerShell script çalıştırma, yönetici yetkisi — BUNLARIN HİÇBİRİ bu
  modülde YOKTUR (ne allowlist'te bir giriş ne de bir kod yolu).
- Dosya/klasör listeleme BİLEREK burada YOK — bunu zaten actions/
  file_controller.py yapıyor; aynı yeteneği burada tekrarlamak gereksiz
  kod/risk yaratırdı (discovery sistemimizin kendi REDUNDANT kavramına
  aykırı olurdu).
- Her çağrı kısa bir timeout ile sınırlı (DEFAULT_TIMEOUT_SECONDS) - takılan
  bir process Jarvis'in agent_loop turunu asla kilitlemez.
- Çıktı sınırsız şekilde modele verilmez - MAX_OUTPUT_CHARS/MAX_OUTPUT_LINES
  ile kırpılır.
- computer_settings.py'nin ZATEN kullandığı Windows-subprocess deseniyle
  BİREBİR tutarlı: liste-biçimli argv, `CREATE_NO_WINDOW` (konsol penceresi
  hiç açılmaz), `shell=True` YOK.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from typing import Any

MAX_OUTPUT_CHARS = 4000
MAX_OUTPUT_LINES = 200
DEFAULT_TIMEOUT_SECONDS = 8

# computer_settings.py'deki AYNI desen: Windows'ta konsol penceresi hiç
# açılmasın (Jarvis arka planda çalışırken kullanıcının önüne siyah bir
# terminal penceresi zıplamasın), başka platformlarda no-op.
_WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}

# command_name -> SABİT argv listesi. Bu sözlüğün DEĞERLERİ hiçbir zaman
# kullanıcı/model girdisiyle değiştirilmez/birleştirilmez - her biri düz bir
# Python string literal'idir. Hepsi salt-okunur PowerShell cmdlet'leri
# kullanır ve yapılandırılmış (JSON) çıktı için ConvertTo-Json ile biter.
_ALLOWED_COMMANDS: dict[str, list[str]] = {
    "process_list": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-Process | Sort-Object -Property WorkingSet -Descending | "
        "Select-Object -First 15 Name,Id,@{Name='MemoryMB';Expression={[math]::Round($_.WorkingSet / 1MB, 1)}},CPU "
        "| ConvertTo-Json",
    ],
    "service_list": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-Service | Select-Object Name,DisplayName,Status | ConvertTo-Json",
    ],
    "system_info": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-ComputerInfo | Select-Object WindowsProductName,OsVersion,OsArchitecture,"
        "CsTotalPhysicalMemory | ConvertTo-Json",
    ],
    "cpu_load": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-CimInstance Win32_Processor | Select-Object Name,LoadPercentage,"
        "NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json",
    ],
    "disk_info": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-Volume | Select-Object DriveLetter,FileSystemLabel,FileSystem,Size,"
        "SizeRemaining | ConvertTo-Json",
    ],
    "network_info": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,LinkSpeed,"
        "MacAddress | ConvertTo-Json",
    ],
    "network_connections": [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "Get-NetTCPConnection | Select-Object LocalAddress,LocalPort,RemoteAddress,"
        "RemotePort,State | ConvertTo-Json",
    ],
}


def list_allowed_commands() -> list[str]:
    """Allowlist'i TEK bir yerden (bu dosyadan) okumak isteyen diğer
    modüller (ör. ileride capability_registry) için salt-okunur yardımcı -
    ikinci bir kopya elle yazılmasın."""
    return sorted(_ALLOWED_COMMANDS)


def _truncate_output(text: str) -> str:
    """Çıktıyı hem satır hem karakter sınırına göre kırpar - ikisi de asla
    aşılmaz. Kırpma olduysa bunu AÇIKÇA belirtir, sessizce kesmez."""
    if not text:
        return text
    truncated = False
    lines = text.splitlines()
    if len(lines) > MAX_OUTPUT_LINES:
        lines = lines[:MAX_OUTPUT_LINES]
        truncated = True
    result = "\n".join(lines)
    if len(result) > MAX_OUTPUT_CHARS:
        result = result[:MAX_OUTPUT_CHARS]
        truncated = True
    if truncated:
        result += "\n... (çıktı kırpıldı)"
    return result


def _error_result(command_name: str, message: str, duration_ms: int = 0) -> dict[str, Any]:
    return {
        "ok": False,
        "command": command_name or "(belirtilmedi)",
        "exit_code": None,
        "stdout": "",
        "stderr": message,
        "duration_ms": duration_ms,
    }


def run(parameters: dict) -> str:
    """tools_kopru._call_windows_system() tarafından çağrılır.

    SADECE parameters["command_name"] okunur. Başka HİÇBİR parametre
    (parameters içindeki diğer tüm anahtarlar) subprocess'e ulaşmaz - bu
    ilk sürüm sıfır kullanıcı-kontrollü parametre kabul eder, bu yüzden
    komut enjeksiyonu için hiçbir veri yolu yoktur.

    Dönüş: her zaman geçerli bir JSON metni -
    {"ok": bool, "command": str, "exit_code": int|None, "stdout": str,
     "stderr": str, "duration_ms": int}
    """
    parameters = parameters or {}
    command_name = str(parameters.get("command_name", "")).strip()

    if command_name not in _ALLOWED_COMMANDS:
        return json.dumps(_error_result(
            command_name,
            f"Bilinmeyen veya izin verilmeyen komut: '{command_name}'. "
            f"İzinli komutlar: {', '.join(list_allowed_commands())}. "
            "Bu araç keyfi/ham bir PowerShell komutu KABUL ETMEZ.",
        ), ensure_ascii=False)

    argv = _ALLOWED_COMMANDS[command_name]  # SABİT liste - hiçbir kullanıcı verisi karışmaz
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=DEFAULT_TIMEOUT_SECONDS, **_WIN_HIDE,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        result = {
            "ok": proc.returncode == 0,
            "command": command_name,
            "exit_code": proc.returncode,
            "stdout": _truncate_output(proc.stdout or ""),
            "stderr": _truncate_output(proc.stderr or ""),
            "duration_ms": duration_ms,
        }
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = _error_result(
            command_name, f"Zaman aşımı ({DEFAULT_TIMEOUT_SECONDS}s içinde tamamlanmadı).",
            duration_ms,
        )
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = _error_result(
            command_name, "powershell bulunamadı (Windows dışı bir ortam olabilir).",
            duration_ms,
        )
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        result = _error_result(command_name, f"{type(e).__name__}: {e}", duration_ms)

    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    # Elle/hızlı doğrulama: `python -m actions.windows_shell <command_name>`
    import sys as _sys
    name = _sys.argv[1] if len(_sys.argv) > 1 else "system_info"
    print(run({"command_name": name}))
