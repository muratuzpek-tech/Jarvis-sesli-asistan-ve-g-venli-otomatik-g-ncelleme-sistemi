"""
actions/capability_resolver.py — Brain Team (Planner/Executor/Auditor) icin
Capability -> Agent -> Tool coz(umleme) katmani.

NEDEN BU MODUL VAR (kullanici talimati, 2026-09-16):
Jarvis'te iki ayri gorev yolu var: (A) agent_loop -> tools_kopru ->
windows_system -> windows_shell.py (calisiyor, Windows'ta test edildi) ve
(B) Brain Team: Planner -> brain_orchestrator -> executor_ai ->
executor_ai._ALLOWED_ACTIONS (SADECE 6 sabit legacy eylem biliyor,
capability_registry/tools_kopru/windows_shell'i hic taniMIYOR). Bu modul,
IKINCI bir tool/capability sistemi OLMADAN, (B) yolunun (A) yolunda zaten
var olan GERCEK capability/tool kayitlarini kullanabilmesini saglar.

TASARIM SINIRLARI (kullanici talimati ile birebir):
- Ikinci bir capability/tool registry OLUSTURULMUYOR: actions/tools_kopru.py
  (ALLOWED_TOOLS/TOOL_DESCRIPTIONS) ve actions/capability_registry.py
  (get_capabilities()) TEK dogru kaynak - bu modul sadece onlari OKUR.
- windows_shell.py'ye HICBIR sekilde dokunulmuyor, guvenlik modeli
  (allowlist exact-match, shell=True yok, raw command yok, sifir parametre
  disinda hicbir sey) DEGISMIYOR.
- Bu modul HICBIR ZAMAN kendi basina bir tool CALISTIRMAZ - sadece HANGI
  (capability, tool, command, parameters) kombinasyonunun uygulanmasi
  gerektigini belirler. Gercek calistirma HER ZAMAN executor_ai.py'nin
  _ALLOWED_ACTIONS'i + tools_kopru.py'nin mevcut wrapper'lari uzerinden
  olur.
- resolve_capability() (serbest metin) SADECE windows_system icin - diger
  capability'ler (github_arama, backup, vb.) zaten calisan legacy
  _infer_executor_action() yoluna DOKUNULMADAN birakildi (kullanici
  talimati: "legacy action'larin geriye donuk uyumlulugunu koru").
  Eslesme yoksa (None) cagiran taraf legacy fallback'e duser - HICBIR ZAMAN
  "muhtemelen budur" diye zorlama yapilmaz.
- resolve_structured() ACIKCA belirtilmis (capability, tool, parameters)
  icin - bugun Planner bunu URETMIYOR (planner_ai.py'ye DOKUNULMADI,
  SYSTEM_PROMPT buyutulmedi - kullanici talimati madde 9), ama gelecekte
  yapilandirilmis bir girdi gelirse GERCEK kayitlara karsi sikica
  dogrulanir; gecersizse SESSIZCE baska bir seye DUSMEZ, ValueError
  firlatir (REJECT).
"""
from __future__ import annotations

import ast
import logging
import sys
from pathlib import Path
from typing import Any

from jarvis.actions import tools_kopru
from jarvis.actions import capability_registry
from jarvis.actions import windows_shell
from jarvis.paths import logs_dir


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
LOGS_DIR = logs_dir()


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("jarvis.tool_resolver")
    if not logger.handlers:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOGS_DIR / "tool_resolver.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        except Exception:
            pass
    return logger


_logger = _make_logger()

_LOG_SAFE_FIELDS = {"capability", "tool", "command", "agent", "source", "reason"}


def log_resolution(**fields: Any) -> None:
    """[TOOL_RESOLVER] izlenebilir bir trace satiri yazar. SADECE isim/etiket
    turunden alanlar kabul edilir (_LOG_SAFE_FIELDS) - sifre, token, API
    anahtari ya da baska hassas veri buraya ASLA gecirilmemeli/yazilmamali."""
    try:
        safe = {k: v for k, v in fields.items() if k in _LOG_SAFE_FIELDS}
        line = " ".join(f"{k}={v!r}" for k, v in safe.items())
        _logger.info(f"[TOOL_RESOLVER] {line}")
    except Exception:
        pass  # loglama basarisiz olsa bile cozumleme ASLA cokmemeli


# ── 1) AGENT REGISTRY ────────────────────────────────────────────────────
# Mevcut 7 beynin GERCEK modul/sinif adi. brain_orchestrator.py'nin zaten
# yaptigi bus.register() kaydinin, disaridan sorgulanabilir/dogrulanabilir
# bir yansimasi - YENI bir agent sistemi DEGIL, mevcut siniflari yeniden
# YAZMIYOR (kullanici talimati madde 4).
_AGENT_MODULES: dict[str, tuple[str, str]] = {
    "planner_ai":  ("jarvis.brains.planner_ai", "PlannerAI"),
    "research_ai": ("jarvis.brains.research_ai", "ResearchAI"),
    "coder_ai":    ("jarvis.brains.coder_ai", "CoderAI"),
    "security_ai": ("jarvis.brains.security_ai", "SecurityAI"),
    "memory_ai":   ("jarvis.brains.memory_ai", "MemoryAI"),
    "executor_ai": ("jarvis.brains.executor_ai", "ExecutorAI"),
    "auditor_ai":  ("jarvis.brains.auditor_ai", "AuditorAI"),
}


def _read_source(module_dotted: str) -> str | None:
    try:
        rel_path = module_dotted.replace(".", "/") + ".py"
        path = BASE_DIR / rel_path
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _class_exists_in_source(module_dotted: str, class_name: str) -> bool:
    """GERCEK import YAPMADAN (base_brain.py Gemini SDK / actions/resilience.py
    gibi agir bagimliliklar tasir - bunlari resolver'in kendi import
    zincirine sokmamak icin) kaynak metni ast ile tarar, sinifin GERCEKTEN
    o dosyada tanimli olup olmadigini dogrular. Tahmin ETMEZ - capability_
    registry.py'nin main.py/tools_kopru.py'yi okurken kullandigi AYNI
    'sadece oku, calistirma' ilkesi."""
    try:
        source = _read_source(module_dotted)
        if source is None:
            return False
        tree = ast.parse(source)
        return any(isinstance(node, ast.ClassDef) and node.name == class_name
                   for node in ast.walk(tree))
    except Exception:
        return False


def _extract_executor_legacy_actions() -> list[str]:
    """brains/executor_ai.py'deki _ALLOWED_ACTIONS sozlugunun anahtarlarini,
    capability_registry.py'nin ZATEN VAR olan _extract_dict_keys() yardimcisi
    ile (import etmeden, ast ile) okur - ayni ayiklama mantigi ikinci kez
    YAZILMIYOR."""
    try:
        source = _read_source("jarvis.brains.executor_ai")
        if source is None:
            return []
        return capability_registry._extract_dict_keys(source, "_ALLOWED_ACTIONS")
    except Exception:
        return []


def get_agent_registry() -> dict[str, dict[str, Any]]:
    """Her beyin icin {agent, module, class, real, capabilities} dondurur.
    'real' = kaynak dosyada o sinif GERCEKTEN tanimli mi (ast ile
    dogrulanmis, tahmin degil). 'capabilities' SADECE executor_ai icin
    doldurulur - diger beyinlerin gorevi zaten kendi adiyla ayni sey
    (arastirma/kodlama/denetim/guvenlik/hafiza), capability listesi
    kavrami sadece "hangi araci calistirabiliyor" sorusu icin anlamli."""
    registry: dict[str, dict[str, Any]] = {}
    for name, (module_dotted, class_name) in _AGENT_MODULES.items():
        registry[name] = {
            "agent": name,
            "module": module_dotted,
            "class": class_name,
            "real": _class_exists_in_source(module_dotted, class_name),
            "capabilities": [],
        }

    legacy_actions = set(_extract_executor_legacy_actions())
    if "executor_ai" in registry:
        registry["executor_ai"]["capabilities"] = sorted(legacy_actions | _STRUCTURED_DISPATCHABLE)
    return registry


# ── 2) WINDOWS_SYSTEM SERBEST-METIN TETIKLEYICI TABLOSU ─────────────────
# Planner SADECE dogal dil aciklamasi uretiyor (planner_ai.py'ye
# DOKUNULMADI - prompt buyutulmedi, LLM'e tool sectirilmiyor, kullanici
# talimati madde 9). Bu tablo, o metni GERCEK windows_shell komutlariyla
# eslestirmek icin kullanilan TEK, gozlemlenebilir veri kaynagi - brain_
# orchestrator.py icine dagilmis, surekli buyuyen bir "keyword hack"
# YERINE burada. Yanlis/eski olsa BILE asagidaki resolve_capability()
# eslesen komutu GERCEK windows_shell.list_allowed_commands() listesiyle
# CAPRAZ DOGRULAR - bu tablo tek basina hicbir seyi calistirmaya yetmez.
_WINDOWS_SYSTEM_TRIGGERS: dict[str, tuple[str, ...]] = {
    "process_list": (
        "calisan islem", "calisan surec", "islemleri listele",
        "surecleri listele", "process list", "process'leri",
        "process listesi", "gorev yoneticisi", "task manager",
        "hangi islemler calisiyor",
    ),
    "service_list": (
        "servis durum", "servisleri goster", "servis listesi",
        "service list", "windows servis", "servislerin durumu",
    ),
    "system_info": (
        "sistem bilgisi", "sistem bilgilerini", "system info",
        "isletim sistemi bilgisi", "windows surumu", "bilgisayarin sistem",
    ),
    "cpu_load": (
        "cpu kullanim", "islemci yuku", "cpu load", "cpu yuku",
        "islemci kullanim",
    ),
    "disk_info": (
        "disk bilgisi", "disk kullanim", "disk info", "disk alani",
    ),
    "network_info": (
        "ag adaptor", "network adapter", "ag bilgisi", "ag arayuz",
    ),
    "network_connections": (
        "acik baglanti", "tcp baglant", "network connection", "ag baglantilari",
    ),
}

_TR_MAP = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g",
                          "Ğ": "g", "ü": "u", "Ü": "u", "ö": "o", "Ö": "o",
                          "ç": "c", "Ç": "c"})


def _normalize(text: str) -> str:
    if not text:
        return ""
    return text.translate(_TR_MAP).lower()


def resolve_capability(description: str) -> dict[str, Any] | None:
    """Serbest-metin bir Planner adim aciklamasini GERCEK bir
    capability+tool+command'a cozmeye calisir. SADECE windows_system icin
    (kullanici talimati madde 8 - dosya/klasor islemleri EKLENMEDI, onlar
    file_controller'da kaliyor). Eslesme YOKSA ya da eslesen komut GERCEK
    kayitlardan biri DEGILSE None doner - cagiran taraf (brain_orchestrator.
    _resolve_executor_call) bunu legacy _infer_executor_action() fallback'ine
    dusurur (kullanici talimati madde 7 - "yapilandirilmis tool bilgisi
    yoksa legacy fallback olarak kullanilabilir")."""
    if not description or not isinstance(description, str):
        return None
    norm = _normalize(description)

    matched_command: str | None = None
    for command_name, triggers in _WINDOWS_SYSTEM_TRIGGERS.items():
        if any(_normalize(t) in norm for t in triggers):
            matched_command = command_name
            break
    if matched_command is None:
        return None

    # GERCEK kayitlarla CAPRAZ DOGRULAMA - yukaridaki tablo yanlis/eski olsa
    # bile asla var olmayan/izinsiz bir seyi calistirmaya kalkismaz.
    real_commands = windows_shell.list_allowed_commands()
    if matched_command not in real_commands:
        log_resolution(capability="windows_system", command=matched_command,
                        reason="DOGRULAMA BASARISIZ: windows_shell allowlist'inde yok")
        return None

    real_capability_names = {c["name"] for c in capability_registry.get_capabilities()}
    if "windows_system" not in real_capability_names:
        log_resolution(capability="windows_system",
                        reason="DOGRULAMA BASARISIZ: capability_registry'de yok")
        return None

    if "windows_system" not in tools_kopru.ALLOWED_TOOLS:
        log_resolution(capability="windows_system",
                        reason="DOGRULAMA BASARISIZ: tools_kopru.ALLOWED_TOOLS'ta yok")
        return None

    result: dict[str, Any] = {
        "capability": "windows_system",
        "tool": "windows_system",
        "command": matched_command,
        "action": "windows_system",  # executor_ai._ALLOWED_ACTIONS anahtari
        "parameters": {"command_name": matched_command},
    }
    log_resolution(capability="windows_system", tool="windows_system",
                    command=matched_command, agent="executor_ai", source="free_text")
    return result


# resolve_structured()'in GERCEKTEN calistirabildigi capability'lerin listesi
# - get_agent_registry() de DOGRULUK icin AYNI sabiti kullanir.
_STRUCTURED_DISPATCHABLE: set[str] = {"windows_system"}


def resolve_structured(capability: str, tool: str | None, parameters: dict | None) -> dict[str, Any]:
    """Adimda ACIKCA belirtilmis (capability, tool, parameters) icin -
    bugun Planner bunu URETMIYOR, ama yapilandirilmis bir girdi gelirse
    GERCEK kayitlara karsi sikica dogrular. Gecersizse ValueError firlatir
    - SESSIZCE baska bir seye DUSMEZ (TEST 6/7: bilinmeyen tool/capability
    acikca REJECT edilmeli)."""
    capability = (capability or "").strip()
    tool = (tool or capability or "").strip()
    parameters = parameters or {}

    real_capability_names = {c["name"] for c in capability_registry.get_capabilities()}
    if capability not in real_capability_names:
        log_resolution(capability=capability, tool=tool, reason="REJECT: bilinmeyen capability")
        raise ValueError(f"Bilinmeyen capability: '{capability}' (capability_registry'de kayitli degil).")
    if tool not in tools_kopru.ALLOWED_TOOLS:
        log_resolution(capability=capability, tool=tool, reason="REJECT: bilinmeyen tool")
        raise ValueError(f"Bilinmeyen tool: '{tool}' (tools_kopru.ALLOWED_TOOLS'ta kayitli degil).")

    if capability == "windows_system":
        command_name = str(parameters.get("command_name", "")).strip()
        if command_name not in windows_shell.list_allowed_commands():
            log_resolution(capability=capability, tool=tool, command=command_name,
                            reason="REJECT: bilinmeyen windows_system komutu")
            raise ValueError(f"Bilinmeyen windows_system komutu: '{command_name}'.")
        log_resolution(capability=capability, tool=tool, command=command_name,
                        agent="executor_ai", source="structured")
        return {"action": "windows_system", "parameters": {"command_name": command_name}}

    # windows_system disinda yapilandirilmis girdi icin: capability/tool
    # tools_kopru'da GERCEK ve izinli olabilir ama executor_ai'nin BUGUNKU
    # _ALLOWED_ACTIONS'inda calistirilacak bir yol tanimli degilse, yine de
    # REJECT et - sessizce baska bir seye DONUSTURULMEZ.
    log_resolution(capability=capability, tool=tool,
                    reason="REJECT: executor_ai icin yapilandirilmis yol yok")
    raise ValueError(
        f"'{capability}' capability'si icin executor_ai'nin bugunku _ALLOWED_ACTIONS'inda "
        f"yapilandirilmis bir calistirma yolu tanimli degil."
    )


# Geriye donuk cagri uyumlulugu icin takma adlar
resolve = resolve_capability
resolve_tool = resolve_capability