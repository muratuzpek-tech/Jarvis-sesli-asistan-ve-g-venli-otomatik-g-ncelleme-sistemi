"""Proje genelinde import taramasi ve bagimlilik uyumluluk kontrolu.

Bu modul actions/health_check.py'nin "Code files" kontrolunden farklidir:
health_check sadece `ast.parse` ile sozdizimini denetler; burada ise her
modul GERCEKTEN, ayri bir alt-surecte import edilir - eksik bagimlilik,
yanlis import yolu gibi CALISMA ZAMANI hatalarini da yakalar.

Ayrica pyproject.toml'da beyan edilen tum bagimliliklarin kurulu surumleri
importlib.metadata ile denetlenir; eksik veya surum uyumsuz olanlar
KULLANICI ONAYI ISTENMEDEN otomatik olarak `pip install` ile kurulur.

BILINCLI TASARIM NOTU: actions/self_improve.py "once onay" ilkesini
izler (kod DEGISTIRME riskli oldugu icin). Burada ise sadece projenin
KENDI beyan ettigi (pyproject.toml) bagimliliklari kurulur - rastgele
internetten bir sey calistirilmaz, projenin kodu degistirilmez - bu
yuzden kullanicinin acik istegiyle onay adimi olmadan calisir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from jarvis.paths import project_root

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - eski Python icin yedek
    import tomli as tomllib  # type: ignore[no-redef]

from importlib import metadata as importlib_metadata

try:
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # pragma: no cover - packaging normalde kurulu olmali
    Requirement = None  # type: ignore[assignment]


_SRC_ROOT = Path(__file__).resolve().parents[1]  # .../src/jarvis
_IMPORT_TIMEOUT_S = 15.0
_PIP_TIMEOUT_S = 180.0


def _iter_python_modules() -> list[str]:
    """src/jarvis altindaki her .py dosyasini nokta ile ayrilmis modul adina cevirir."""
    modules: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(_SRC_ROOT.parent)  # jarvis/... veya jarvis/x/y.py
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        modules.append(".".join(parts))
    return modules


def _try_import(module_name: str) -> dict:
    """Bir moduluu ayri bir alt-surecte import etmeyi dener; JSON sonuc dondurur."""
    code = (
        "import importlib, json\n"
        "try:\n"
        f"    importlib.import_module({module_name!r})\n"
        "    print(json.dumps({\"ok\": True}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({\"ok\": False, \"error\": f\"{type(exc).__name__}: {exc}\"}))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=_IMPORT_TIMEOUT_S,
            cwd=str(project_root()),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "zaman asimi"}
    for line in reversed((result.stdout or "").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
    return {"ok": False, "error": (result.stderr or "bilinmeyen hata")[-500:]}


def _load_declared_dependencies() -> list[str]:
    pyproject = project_root() / "pyproject.toml"
    if not pyproject.is_file():
        return []
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        deps.extend(extra_deps)
    return deps


def _check_dependency(spec: str) -> dict | None:
    """Bir bagimliligin kurulu olup olmadigini ve surum uyumunu kontrol eder."""
    if Requirement is None:
        return None
    try:
        req = Requirement(spec)
    except Exception:
        return None
    if req.marker is not None:
        try:
            if not req.marker.evaluate():
                return None
        except Exception:
            pass
    try:
        installed_version = importlib_metadata.version(req.name)
    except importlib_metadata.PackageNotFoundError:
        return {"name": req.name, "spec": spec, "status": "missing", "installed": None}
    try:
        uyumlu = (not req.specifier) or req.specifier.contains(installed_version, prereleases=True)
    except Exception:
        uyumlu = True
    if not uyumlu:
        return {
            "name": req.name,
            "spec": spec,
            "status": "incompatible",
            "installed": installed_version,
        }
    return None


def _pip_install(spec: str) -> dict:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", spec],
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return {"spec": spec, "ok": False, "error": "pip zaman asimi"}
    ok = result.returncode == 0
    tail = (result.stdout if ok else result.stderr) or ""
    return {"spec": spec, "ok": ok, "detail": tail[-800:]}


def system_scan_and_repair(parameters: dict | None = None, player=None) -> str:
    """Projeyi tarar: import hatalari + bagimlilik uyumu.

    Paket kurulumları varsayılan olarak yalnızca raporlanır. Gerçek kurulum,
    kullanıcının açıkça ``JARVIS_ALLOW_DEP_INSTALL=1`` ayarlamasıyla yapılır;
    böylece bir tarama/LLM çağrısı tek başına makineye paket yükleyemez.
    Diğer action fonksiyonlarıyla tutarlı olması için JSON string döndürür.
    """
    modules = _iter_python_modules()
    import_errors: list[dict] = []
    for module_name in modules:
        outcome = _try_import(module_name)
        if not outcome.get("ok"):
            import_errors.append({"module": module_name, "error": outcome.get("error", "")})

    dependency_issues: list[dict] = []
    for spec in _load_declared_dependencies():
        issue = _check_dependency(spec)
        if issue:
            dependency_issues.append(issue)

    fixes_applied: list[dict] = []
    fixes_failed: list[dict] = []
    install_allowed = os.environ.get("JARVIS_ALLOW_DEP_INSTALL", "").strip() == "1"
    if install_allowed:
        for issue in dependency_issues:
            outcome = _pip_install(issue["spec"])
            (fixes_applied if outcome["ok"] else fixes_failed).append(outcome)
    elif dependency_issues:
        fixes_failed = [
            {
                "spec": issue["spec"],
                "ok": False,
                "error": "Kurulum engellendi: açık onay için JARVIS_ALLOW_DEP_INSTALL=1 ayarlayın.",
            }
            for issue in dependency_issues
        ]

    summary = {
        "ok": not import_errors and not fixes_failed,
        "taranan_modul_sayisi": len(modules),
        "import_hatalari": import_errors,
        "bagimlilik_sorunlari": dependency_issues,
        "kurulum_izni": install_allowed,
        "otomatik_kurulan": fixes_applied,
        "kurulum_basarisiz": fixes_failed,
    }
    return json.dumps(summary, ensure_ascii=False)
