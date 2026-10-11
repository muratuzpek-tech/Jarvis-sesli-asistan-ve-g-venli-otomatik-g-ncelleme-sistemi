"""capability_registry.py — Jarvis'in GERÇEKTEN sahip olduğu yeteneklerin
canlı, gerçek kaynaktan (main.py + tools_kopru.py) AST ile çıkarılan listesi.

NEDEN AST (main.py/tools_kopru.py hiçbir zaman import EDİLMEZ): main.py'yi
gerçekten import etmek Gemini Live bağlantısını, mikrofonu, Qt uygulamasını
vb. tetikler - bu modül SADECE dosyaları metin olarak okuyup ast.parse ile
literal TOOL_DECLARATIONS / ALLOWED_TOOLS / TOOL_DESCRIPTIONS yapılarını
çıkarır, hiçbir yan etkisi yoktur ("sadece oku, çalıştırma" ilkesi -
actions/capability_resolver.py'nin de referans verdiği aynı ilke).

Kullananlar (bu arayüz KORUNDU, imzalar değişmedi):
  - actions/discovery.py::_gap_analyze() / _capability_usability_analyze()
    -> get_capability_summary()
  - actions/capability_resolver.py -> get_capabilities(),
    _extract_dict_keys(source, dict_name)
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
MAIN_PY_PATH = BASE_DIR / "main.py"
TOOLS_KOPRU_PATH = BASE_DIR / "actions" / "tools_kopru.py"

# tools_kopru.py::is_destructive()'in GERÇEK davranışıyla tutarlı, statik
# bir ÖZET sınıflandırması (is_destructive() parametreye/action'a bağlı
# çalıştığı için gerçek karar HER ZAMAN çalıştığı an is_destructive()'e
# aittir - burası SADECE Gemini'ye gösterilecek kısa bir etiket):
#   - her zaman onay isteyen araçlar (send_message, discovery_register,
#     entegrasyon_uygula - bkz. tools_kopru.is_destructive())
_ALWAYS_HIGH_RISK = {"send_message", "discovery_register", "entegrasyon_uygula"}
#   - riski action/parametreye göre değişen araçlar (file_controller,
#     computer_settings - bkz. file_controller.READONLY_ACTIONS ve
#     tools_kopru._DESTRUCTIVE_SETTINGS_ACTIONS)
#     + salt-okunur olsa da sistem/ağ bilgisi ifşa eden windows_system
#     (bkz. windows_shell.py ekleme planı - bilinçli olarak "conditional").
_CONDITIONAL_RISK = {"file_controller", "computer_settings", "windows_system"}

# discovered_*.py dosyalarında sık görülen ama standart kütüphanede OLMAYAN
# paket adları - guess_missing_deps() yanlış-pozitif üretmemek için SADECE
# bu bilinen üçüncü-parti adlarını kontrol eder, tanımadığı bir importu
# hiç raporlamaz.
_KNOWN_THIRD_PARTY_IMPORTS = {
    "requests", "psutil", "PySide6", "PyQt6", "numpy", "pandas",
    "bs4", "yaml", "PIL", "cv2", "sounddevice", "pyaudio",
    # discovered_*.py taramasinda GERCEKTEN karsilasilan, ortamda kurulu
    # olmayabilecek paketler (bkz. discovered_jc.py -> jc, requirements.txt'e
    # eklendi; discovered_jarvis_registry.py -> boto3, requirements.txt'e
    # BILEREK eklenmedi - AWS Secrets Manager'a bagli, bu proje kapsami
    # disinda, "eksik bagimlilik" olarak DOGRU raporlanmasi gerekiyor).
    "jc", "boto3",
}


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _find_assignment_value(tree: ast.Module, name: str) -> ast.AST | None:
    """Modül seviyesindeki `name = ...` atamasının SAĞ tarafındaki AST
    düğümünü döner - birden fazla varsa dosyada EN SON atanan değeri alır.
    Hiçbir kod ÇALIŞTIRILMAZ, sadece AST üzerinde gezinilir."""
    result: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    result = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name and node.value is not None:
                result = node.value
    return result


def _extract_literal(source: str, literal_name: str) -> Any:
    """Kaynak koddaki `literal_name = <literal>` atamasının değerini, KOD
    ÇALIŞTIRMADAN (ast.literal_eval) döner. Değer literal değilse (ör. bir
    fonksiyon/isim referansı içeriyorsa) None döner."""
    if not source:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    node = _find_assignment_value(tree, literal_name)
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def _extract_dict_keys(source: str, dict_name: str) -> list[str]:
    """`dict_name = {...}` sözlüğünün SADECE anahtarlarını döner. Değerler
    (ör. ALLOWED_TOOLS'taki fonksiyon referansları) literal olmadığı için
    tüm sözlük ast.literal_eval ile okunamaz - bu yüzden anahtarlar tek tek,
    değerlere hiç bakılmadan çıkarılır."""
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    node = _find_assignment_value(tree, dict_name)
    if not isinstance(node, ast.Dict):
        return []
    keys: list[str] = []
    for key_node in node.keys:
        if key_node is None:  # **spread girişi - anahtarı yok
            continue
        try:
            key = ast.literal_eval(key_node)
        except (ValueError, TypeError):
            continue
        if isinstance(key, str):
            keys.append(key)
    return keys


def _risk_for_tool(name: str, allowed_keys: list[str]) -> str:
    if name in _ALWAYS_HIGH_RISK:
        return "high"
    if name in _CONDITIONAL_RISK:
        return "conditional"
    if name in allowed_keys:
        # tools_kopru.is_destructive fail-closed: yalnizca acikca salt-okunur
        # isaretli araclar onaysiz calisir.
        from jarvis.actions.tools_kopru import _READONLY_TOOLS
        return "low" if name in _READONLY_TOOLS else "high"
    return "unknown"


def _third_party_available(module_name: str) -> bool:
    """Bir üçüncü parti paketin şu an kurulu (import edilebilir) olup
    olmadığını, GERÇEKTEN import ETMEDEN kontrol eder (find_spec yan
    etkisiz bir arama yapar, modülün kendisini çalıştırmaz)."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def guess_missing_deps(mod_path: Path) -> list[str]:
    """Bir discovered_*.py dosyasının TÜM üst seviye import satırlarını AST
    ile okuyup (dosya hiç ÇALIŞTIRILMADAN), bilinen üçüncü-parti paketlerden
    şu an ortamda kurulu OLMAYANLARI döner. Tanımadığı bir import adını asla
    "eksik" diye raporlamaz (yanlış-pozitif üretmemek için)."""
    source = _read_source(mod_path)
    if not source:
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    missing: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names = [node.module.split(".")[0]]
        for name in names:
            if name in _KNOWN_THIRD_PARTY_IMPORTS and not _third_party_available(name):
                if name not in missing:
                    missing.append(name)
    return missing


def get_capabilities() -> list[dict[str, Any]]:
    """Jarvis'in main.py (Gemini'ye açık TOOL_DECLARATIONS) + tools_kopru.py
    (agent_loop'a açık ALLOWED_TOOLS/TOOL_DESCRIPTIONS) üzerinden GERÇEKTEN
    sahip olduğu yeteneklerin listesini döner."""
    capabilities: dict[str, dict[str, Any]] = {}

    main_source = _read_source(MAIN_PY_PATH)
    declarations = _extract_literal(main_source, "TOOL_DECLARATIONS")
    if isinstance(declarations, list):
        for entry in declarations:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            capabilities[name] = {
                "name": name,
                "source": "main.py",
                "description": str(entry.get("description", "")).strip(),
                "risk": "unknown",
                "available": True,
                "missing_deps": [],
            }

    tk_source = _read_source(TOOLS_KOPRU_PATH)
    allowed_keys = _extract_dict_keys(tk_source, "ALLOWED_TOOLS")
    descriptions = _extract_literal(tk_source, "TOOL_DESCRIPTIONS")
    if not isinstance(descriptions, dict):
        descriptions = {}

    for name in allowed_keys:
        if name in capabilities:
            capabilities[name]["source"] = "main.py+tools_kopru.py"
            if not capabilities[name]["description"]:
                capabilities[name]["description"] = str(descriptions.get(name, "")).strip()
        else:
            capabilities[name] = {
                "name": name,
                "source": "tools_kopru.py",
                "description": str(descriptions.get(name, "")).strip(),
                "risk": "unknown",
                "available": True,
                "missing_deps": [],
            }

    for name, entry in capabilities.items():
        entry["risk"] = _risk_for_tool(name, allowed_keys)

    actions_dir = BASE_DIR / "actions"
    for name, entry in capabilities.items():
        if not name.startswith("discovered_"):
            continue
        mod_path = actions_dir / f"{name}.py"
        if not mod_path.exists():
            entry["available"] = False
            entry["missing_deps"] = [f"<dosya bulunamadi: actions/{name}.py>"]
            continue
        missing = guess_missing_deps(mod_path)
        entry["available"] = len(missing) == 0
        entry["missing_deps"] = missing

    return sorted(capabilities.values(), key=lambda e: e["name"])


def get_capability_summary(max_chars: int = 12000) -> str:
    caps = get_capabilities()
    lines: list[str] = []
    for c in caps:
        avail = "" if c["available"] else f" [EKSIK BAGIMLILIK: {', '.join(c['missing_deps'])}]"
        desc = c["description"][:160]
        lines.append(f"- {c['name']} (risk={c['risk']}, kaynak={c['source']}){avail}: {desc}")

    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]
    remaining = len(caps) - truncated.count("\n") - 1
    return truncated + f"\n... (+{max(remaining, 0)} yetenek daha, yer sinirindan kesildi)"


if __name__ == "__main__":
    caps = get_capabilities()
    print(f"Toplam {len(caps)} yetenek bulundu.\n")
    print(get_capability_summary())
