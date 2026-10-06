"""
tools/developer/agentic_coder.py — Jarvis 2.0 Agentic Coding Engine

BORAN-SERT'TEN ALINAN (Brain/coding_engine.py):
  - Iteratif yaz→çalıştır→test et→düzelt döngüsü (max 15)
  - "TODO/pass" YASAK → her fonksiyon ÇALIŞIR kod içermeli
  - Multi-file proje desteği (dosya dosya yaz)
  - Structured output (JSON thought/tool/args/response)

SENİN SİSTEME ADAPTE EDİLEN:
  - Gemini API (Ollama yerine → sizin mevcut altyapınız)
  - tools/registry entegrasyonu
  - stdin=DEVNULL güvenlik (input() bloğu)
  - mevcut code_helper._clean_code() markdown extractor

AKIŞ:
    USER: "Hesap makinesi yaz"
      ↓
    1. PLAN     → Ne yapılacak? Hangi dosyalar?
    2. INSPECT  → Hedef dizin mevcut mu?
    3. WRITE    → Dosya 1 yaz (TAM ÇALIŞIR)
    4. RUN      → python3 dosya.py
    5. TEST     → Çıktı doğru mu?
    6. ERROR?   → Hata var mı?
    7. FIX      → Hata düzelt + yeniden yaz
    8. ACCEPT   → Çalışıyor → BİTİR (veya 15 iterasyon → zorla bitir)
"""
from __future__ import annotations

import ast
import asyncio
import functools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from collections.abc import Callable

logger = logging.getLogger("tools.developer.agentic")


# ═══ AGENTGREP HELPER ═══
import subprocess as _sp_mod
import shutil as _sh_mod

def _agentgrep(*args):
    ag = _sh_mod.which("agentgrep") or str(Path.home() / "agentgrep/target/release/agentgrep")
    try:
        r = _sp_mod.run([ag, *args], capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip()[:2000]
    except Exception:
        return ""

def _scan_project(description, root):
    findings = []
    keywords = [w for w in str(description).lower().split() if len(w) > 3][:8]
    if keywords:
        out = _agentgrep("find", *keywords, "--path", str(root))
        if out:
            findings.append(f"[FILES]\n{out}")
    return "\n\n".join(findings)[:3000]


# ── LLM secimi ────────────────────────────────────────────────
# Gemini modeli: JARVIS_CODER_GEMINI_MODEL ile degistirilebilir. Eskiden
# "gemini-2.0-flash" sabitti; "-latest" takma adi her zaman gecerli bir
# modele isaret eder. NOT: bu takma ad agent_loop ile ayni gunluk kotayi
# paylasir; kota dolunca asagidaki devre kesici Gemini'yi sureli kapatir.
_GEMINI_CODER_MODEL = os.environ.get("JARVIS_CODER_GEMINI_MODEL", "gemini-flash-latest")

# Ollama yedegi: OLLAMA_CODER_MODEL verilmemisse kurulu ilk KOD modeli.
# 7b once: her iterasyon bir LLM cagrisi, hiz onemli (canli testte
# qwen2.5-coder:7b 3 iterasyonda bitirdi; genel qwen2.5:7b 25'te bitiremedi).
_OLLAMA_PREFERRED = ("qwen2.5-coder:7b", "qwen2.5-coder:14b", "qwen2.5-coder", "qwen2.5:7b")
_OLLAMA_BASE = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not _OLLAMA_BASE.startswith("http"):
    _OLLAMA_BASE = f"http://{_OLLAMA_BASE}"
_ollama_model_cache: str | None = None

# 429 sonrasi Gemini'nin tekrar denenmeyecegi zaman (monotonic). Eskiden her
# iterasyon dolu kotaya bir istek daha atip ancak sonra Ollama'ya dusuyordu.
_gemini_disabled_until = 0.0
_RETRY_DELAY_RE = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)


def _gemini_api_key() -> str:
    """Once ortam degiskeni, sonra Jarvis'in kendi guvenli ayar dosyasi.
    Eskiden sadece os.environ okunuyordu; anahtar ayar dosyasindaysa
    (normal kurulum) kodlama HIC Gemini kullanmadan Ollama'ya gidiyordu."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        from jarvis.core.secure_config import get_gemini_api_key
        return get_gemini_api_key()
    except Exception:
        return ""


def _pick_ollama_coder_model() -> str:
    global _ollama_model_cache
    explicit = os.environ.get("OLLAMA_CODER_MODEL", "").strip()
    if explicit:
        return explicit
    if _ollama_model_cache:
        return _ollama_model_cache
    chosen = _OLLAMA_PREFERRED[-1]
    try:
        import requests
        resp = requests.get(f"{_OLLAMA_BASE}/api/tags", timeout=3)
        resp.raise_for_status()
        installed = {m.get("name", "") for m in resp.json().get("models", [])}
        for name in _OLLAMA_PREFERRED:
            if name in installed:
                chosen = name
                break
    except Exception:
        pass
    _ollama_model_cache = chosen
    logger.info(f"[Coder] Ollama kod modeli: {chosen}")
    return chosen


def _note_gemini_failure(exc: Exception) -> None:
    """429/kota hatasinda Gemini'yi API'nin soyledigi sure kadar atla."""
    global _gemini_disabled_until
    import time as _time
    text = str(exc)
    upper = text.upper()
    if "429" in text or "RESOURCE_EXHAUSTED" in upper:
        match = _RETRY_DELAY_RE.search(text)
        if match:
            wait = float(match.group(1))
        elif "perday" in text.lower():
            wait = 3600.0
        else:
            wait = 60.0
        reason = "kotası dolu"
    elif (
        type(exc).__name__ == "ServerError"
        or "UNAVAILABLE" in upper
        or re.search(r"\b50[0-4]\b", text)
    ):
        # 503 "high demand": canli testte her iterasyon once Gemini'ye gidip
        # ayni hatayi alip sonra Ollama'ya dusuyordu. Kisa bir ara ver.
        wait = 120.0
        reason = "aşırı yüklü (5xx)"
    else:
        return
    _gemini_disabled_until = max(_gemini_disabled_until, _time.monotonic() + wait)
    logger.warning(f"[Coder] Gemini {reason} → {wait:.0f}sn boyunca doğrudan Ollama")


def _gemini_available() -> bool:
    import time as _time
    return _time.monotonic() >= _gemini_disabled_until


# ── Pano icin canli durum (salt okunur) ───────────────────────
# solve() tur basinda ve eylem sonunda DEGISMEZ bir ozet yayimlar; dis
# thread'ler (pano) yalnizca live_status() ile kilit altinda KOPYA okur.
# Canli CodingTask asla disari verilmez. Ozette dosya icerigi, ham model
# ciktisi, prompt ya da ret alintisi yoktur; metinler pano maskesinden gecer.
_LIVE_LOCK = threading.Lock()
_LIVE: OrderedDict[str, MappingProxyType] = OrderedDict()
_LIVE_MAX_RUNS = 10
_LIVE_MAX_EVENTS = 20
_LIVE_MAX_FILES = 50
_last_model: tuple[str, str] | None = None     # (saglayici, model adi)
_KNOWN_ACTIONS = frozenset({"write", "fix", "accept", "run", "inspect", "error"})


def _set_last_model(provider: str, name: str) -> None:
    global _last_model
    with _LIVE_LOCK:
        _last_model = (provider, name)


def last_model() -> dict | None:
    """Varsayilan model zincirinin (Gemini -> Ollama) son kullandigi model."""
    with _LIVE_LOCK:
        lm = _last_model
    return {"provider": lm[0], "name": lm[1]} if lm else None


def _live_clean(text: Any, limit: int) -> str:
    """Panonun maskesi (anahtar, onay kodu, ev yolu...) KESMEDEN ONCE.
    Pano modulu yuklenemiyorsa hicbir metin yayimlanmaz."""
    try:
        from jarvis.agent_panel_state import clean_text
    except Exception:
        return ""
    return clean_text(text, limit)


def _event_text(step: CodingStep) -> str:
    """Adimin panoya giden kisa hali. Ham alanlar (program ciktisi, model
    yaniti, LLM hata metni, ret sebebi -dosyadan satir alintilayabilir-,
    modelin uydurdugu eylem adi) DUSURULUR; yalnizca sabit kategoriler ve
    dosya adlari kalir."""
    action = step.action if step.action in _KNOWN_ACTIONS else "?"
    detail = step.detail or ""
    if action == "run":
        body = "çalıştırıldı"
    elif action == "error":
        body = "LLM hatası"
    elif detail.startswith("REDDEDİLDİ (") and ")" in detail:
        body = detail[:detail.index(")") + 1]
    elif detail.startswith("REDDEDİLDİ"):
        body = "REDDEDİLDİ"
    elif detail.startswith("✅ ACCEPT"):
        body = "✅ ACCEPT"
    elif detail.startswith("Bilinmeyen action"):
        body = "bilinmeyen eylem"
    else:
        body = detail
    return f"#{step.step_num} {action}: {body} ({'OK' if step.success else 'HATA'})"


def _live_snapshot(task: CodingTask, steps: list[CodingStep], status: str,
                   model: dict | None, max_iterations: int = 0) -> MappingProxyType:
    """CodingTask'tan degismez ozet (yalnizca str/int/tuple/proxy)."""
    files = tuple(
        MappingProxyType({"name": _live_clean(name, 120), "size": len(content.encode("utf-8", "replace"))})
        for name, content in list(task.files_written.items())[:_LIVE_MAX_FILES]
    )
    rejects = MappingProxyType({
        "reject": task.reject_total,
        "eval": sum(task.eval_rejects.values()),
        "lock": task.lock_total,
        "method": len(task.method_gaps),       # su an eksik metot sayisi
        "ruff": len(task.ruff_problems),       # ruff hatasi kalan dosya sayisi
    })
    model = model or {"provider": "bilinmiyor", "name": ""}
    reason = task.stop_reason.split(":", 1)[0] if task.stop_reason else ""
    return MappingProxyType({
        "task_id": task.run_id,
        "project": _live_clean(task.project_path.name, 60),
        "description": _live_clean(task.description, 80),
        "status": status,
        "iteration": task.iterations,
        "max_iterations": max_iterations,
        "files": files,
        "rejects": rejects,
        "model": MappingProxyType({"provider": str(model.get("provider", "")),
                                   "name": _live_clean(model.get("name", ""), 60)}),
        "events": tuple(_live_clean(_event_text(s), 120) for s in steps[-_LIVE_MAX_EVENTS:]),
        "reason": _live_clean(reason, 60),
    })


def _live_publish(snapshot: MappingProxyType) -> None:
    with _LIVE_LOCK:
        _LIVE[snapshot["task_id"]] = snapshot
        _LIVE.move_to_end(snapshot["task_id"])
        while len(_LIVE) > _LIVE_MAX_RUNS:
            # once biten en eski kosu; hepsi calisiyorsa en eski
            victim = next((k for k, s in _LIVE.items() if s["status"] != "çalışıyor"), None)
            _LIVE.pop(victim if victim is not None else next(iter(_LIVE)))


def _thaw(value: Any) -> Any:
    if isinstance(value, (MappingProxyType, dict)):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(v) for v in value]
    return value


def live_status() -> list[dict]:
    """Kayitli kosularin (en eski once) duz sozluk KOPYALARI."""
    with _LIVE_LOCK:
        snaps = list(_LIVE.values())
    return [_thaw(s) for s in snaps]


# ── Sabitler ──────────────────────────────────────────────────
MAX_ITERATIONS = 25
_MAX_OUTPUT_CHARS = 2000
_RUN_TIMEOUT = 30

_FORBIDDEN_PATTERNS = [
    (re.compile(r"\bpass\b(?!\w)", re.MULTILINE), "pass statement"),
    (re.compile(r"#\s*(TODO|FIXME|XXX|buraya|burayı|doldur)", re.IGNORECASE), "placeholder comment"),
    (re.compile(r"^\s*\.\.\.\s*$", re.MULTILINE), "ellipsis stub"),
    (re.compile(r"NotImplementedError"), "NotImplementedError stub"),
]


@dataclass
class CodingTask:
    """Bir kodlama görevinin durumu."""
    description: str
    language: str = "python"
    project_path: Path = field(default_factory=lambda: Path.cwd())
    files_written: dict[str, str] = field(default_factory=dict)  # rel_path → content
    last_written_file: str = ""
    same_file_writes: int = 0
    expected_files: list[str] = field(default_factory=list)
    min_test_count: int = 0
    last_content_hash: str = ""
    stuck_count: int = 0
    iterations: int = 0
    errors: list[str] = field(default_factory=list)
    rewrite_counts: dict = field(default_factory=dict)
    status: str = "running"
    accepted: bool = False
    final_response: str = ""
    # Import denetimi: yerel import'tan plana eklenen dosyalar, son gercek
    # import denemesinin sorunlari ve ayni ModuleNotFoundError serisi.
    auto_expected: list[str] = field(default_factory=list)
    import_problems: list[str] = field(default_factory=list)
    import_missing: list[str] = field(default_factory=list)
    import_check_key: tuple = ()
    mnf_streak: int = 0
    missing_modules: list[str] = field(default_factory=list)
    # Dongu ici pytest geri bildirimi (SMART EXIT adayi iken) ve gorevi
    # erken bitiren sebep (ayni dosya/icerik dongusu, kalici eksik modul).
    pytest_key: tuple = ()
    pytest_error: str = ""
    same_write_key: str = ""
    same_write_count: int = 0
    stop_reason: str = ""
    # ruff'in duzeltemedigi GERCEK hatalar (dosya -> metin) ve ayni hata serisi.
    ruff_problems: dict = field(default_factory=dict)
    ruff_sig: frozenset = frozenset()
    ruff_streak: int = 0
    # Ayni dosyanin ust uste reddi (dosya, kategori) -> sayac.
    reject_key: tuple = ()
    reject_count: int = 0
    eval_rejects: dict = field(default_factory=dict)   # dosya -> eval/exec ret sayisi
    # Kilitli (zaten yazilmis) dosyaya tekrar yazma denemeleri: ust uste seri
    # (dosya, sayac), toplam sayac; sonuca eklenecek notlar.
    # Yerel sinifta olmayan metot cagrilari ve ayni sorun serisi.
    method_gaps: list = field(default_factory=list)
    method_sig: frozenset = frozenset()
    method_streak: int = 0
    # Pano: kosu kimligi ve toplam ret sayisi (reject_count yalnizca seri).
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    reject_total: int = 0
    lock_file: str = ""
    lock_streak: int = 0
    lock_total: int = 0
    tests_skipped: bool = False
    notes: list[str] = field(default_factory=list)
    # Aciklama GUI istiyor ama planda giris dosyasi yoktu: main.py (ya da baska
    # bir GUI giris dosyasi) yazilmadan gorev bitmez.
    entry_required: bool = False


@dataclass
class CodingStep:
    """Tek bir iterasyon adımı."""
    step_num: int
    thought: str = ""
    action: str = ""      # write | run | inspect | fix | accept
    detail: str = ""
    success: bool = False




def _fn_role_hints(filename: str, task) -> str:
    """Return role hints based on file type — generic, not hardcoded."""
    ext = Path(filename).suffix.lower()
    if filename.lower() in ('readme.md', 'readme.rst'):
        return 'Project documentation: setup, usage, architecture, testing instructions.'
    if 'test' in filename.lower():
        return 'Test file: pytest-style test functions (def test_*), cover edge cases.'
    if ext == '.py':
        lower = filename.lower()
        if 'model' in lower or 'schema' in lower:
            return 'Data classes/dataclass/TypeScript interfaces — pure structure definitions.'
        if 'storage' in lower or 'db' in lower or 'repo' in lower:
            return 'File/DB I/O — persistence layer with CRUD operations.'
        if 'cli' in lower or 'view' in lower or 'ui' in lower:
            return 'User interface — input/output/display layer.'
        if 'main' in lower or 'app' in lower or 'run' in lower:
            return 'Entry point — imports and wires other modules together.'
        return 'Business logic/utility functions as described in the task.'
    return 'As described in the task.'


# ── Import denetimi ───────────────────────────────────────────
# Canli hata: plan [main.py, README.md] iken model main.py'de
# "from calculator import Calculator" yaziyordu; calculator.py planda
# olmadigi icin hic yazilmiyor, SMART EXIT yalnizca compile() ile "temiz"
# diyordu. Yerel import'lar ast ile cikarilip plana eklenir; ayrica her turda
# gercek import denemesi yapilir (pencere acmadan, zaman asimli).

_GUI_MODULES = frozenset({
    "tkinter", "customtkinter", "ttkbootstrap", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "wx", "kivy", "pygame", "pyglet", "arcade", "dearpygui", "flet", "toga",
})
_IMPORT_CHECK_TIMEOUT = 15
_MNF_STREAK_LIMIT = 3
_IMPORT_MARKER = "@@JARVIS_IMPORT_CHECK@@"
# argv[2]: [[etiket, modul, ad|None], ...]. ad None ise modul import edilir;
# degilse "from modul import ad" denetlenir (giris betigi calistirilmadan).
_IMPORT_SCRIPT = r"""
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
found = []
for label, mod, attr in json.loads(sys.argv[2]):
    try:
        m = importlib.import_module(mod)
        if attr and not hasattr(m, attr):
            try:
                importlib.import_module(mod + "." + attr)
            except ModuleNotFoundError:
                raise ImportError(f"cannot import name {attr!r} from {mod!r}") from None
    except ModuleNotFoundError as e:
        found.append([label, "ModuleNotFoundError", e.name or "", str(e)])
    except ImportError as e:
        found.append([label, "ImportError", "", str(e)])
    except BaseException:
        # Calisma zamani hatalari (pencere acilamadi, input() EOF...) import
        # denetiminin konusu degil; onlari run adimi ve testler yakalar.
        pass
# Modul satir sonu olmadan prompt basmis olabilir: isaret yeni satirdan.
sys.stdout.write("\n@@JARVIS_IMPORT_CHECK@@" + json.dumps(found) + "\n")
sys.stdout.flush()
"""
_ENTRY_SCRIPT_NAMES = frozenset({"main.py", "cli.py", "app.py", "__main__.py"})


@functools.lru_cache(maxsize=1)
def _known_top_level_modules() -> frozenset[str]:
    names = set(sys.stdlib_module_names) | set(sys.builtin_module_names)
    try:
        from importlib.metadata import packages_distributions
        names |= set(packages_distributions())
    except Exception:
        pass
    return frozenset(names)


def _guarded_import_nodes(tree: ast.AST) -> set[int]:
    """try: import x / except ImportError: ... icindeki import'lar opsiyoneldir."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = set()
        for h in node.handlers:
            t = h.type
            elts = t.elts if isinstance(t, ast.Tuple) else [t]
            catches |= {getattr(e, "id", None) for e in elts}
            if t is None:
                catches.add("ImportError")
        if catches & {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}:
            for stmt in node.body:
                guarded |= {id(n) for n in ast.walk(stmt)}
    return guarded


def _imported_modules(filename: str, source: str) -> list[str]:
    """Dosyanin import ettigi moduller (mutlak, noktali ad). Goreli import'lar
    dosyanin paketine gore cozulur."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    guarded = _guarded_import_nodes(tree)
    package = [p for p in Path(filename).parent.parts if p not in ("", ".")]
    out: list[str] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                if node.module:
                    out.append(".".join([*base, node.module]))
                else:
                    out.extend(".".join([*base, alias.name]) for alias in node.names)
            elif node.module:
                out.append(node.module)
    return out


def _module_exists(root: Path, files: dict, dotted: str) -> bool:
    rel = dotted.replace(".", "/")
    for cand in (f"{rel}.py", f"{rel}/__init__.py"):
        if cand in files or (root / cand).is_file():
            return True
    return (root / rel).is_dir()


def _missing_local_modules(files: dict, root: Path) -> list[str]:
    """Yazilan .py dosyalarinin import ettigi; projede, stdlib'de ve kurulu
    paketlerde olmayan moduller -> yazilmasi gereken dosya adlari."""
    known = _known_top_level_modules()
    missing: list[str] = []
    for fn, src in files.items():
        if not fn.endswith(".py"):
            continue
        for dotted in _imported_modules(fn, src):
            parts = dotted.split(".")
            if not all(p.isidentifier() for p in parts):
                continue
            if _module_exists(root, files, parts[0]):
                # Yerel paket var: ilk eksik alt modul yazilmali (pkg/sub.py).
                sub = next((".".join(parts[:d]) for d in range(2, len(parts) + 1)
                            if not _module_exists(root, files, ".".join(parts[:d]))), None)
                if sub is None:
                    continue
                name = sub.replace(".", "/") + ".py"
            elif parts[0] in known:
                continue
            else:
                name = dotted.replace(".", "/") + ".py"
            if name not in missing:
                missing.append(name)
    return missing


# ── Dosyalar arasi metot varlik kapisi (LLM'siz, ast) ─────────
# main.py `self.calculator.calculate()` cagiriyor ama yerel Calculator
# sinifinda `calculate` yoksa bu, cagri yalnizca bir GUI geri cagrisinda
# calistigi icin --help / import / pytest ile yakalanmaz. Belirsiz her durumda
# (dinamik atama, __getattr__, yerel olmayan taban, setattr) sessiz kalinir.

_DYNAMIC_ATTR_HOOKS = frozenset({"__getattr__", "__getattribute__", "__dict__", "__slots__"})


def _local_modules(files: dict) -> dict[str, tuple[str, ast.Module]]:
    """Yerel modul adi -> (dosya, ast)."""
    out = {}
    for fn, src in files.items():
        name = _module_name(fn)
        if name is None:
            continue
        try:
            out[name] = (fn, ast.parse(src))
        except SyntaxError:
            continue
    return out


def _resolve_from(fn: str, node: ast.ImportFrom) -> str | None:
    if not node.level:
        return node.module
    pkg = list(Path(fn).parent.parts)
    if node.level - 1 > len(pkg):
        return None
    base = pkg[:len(pkg) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])]) or None


def _top_classes(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}


def _import_table(fn: str, tree: ast.Module, modules: dict) -> tuple[dict, dict]:
    """(ad -> (modul, sinif), ad -> modul): yerel modulden import edilen
    siniflar ve yerel modul takma adlari (yalnizca modul duzeyindeki import'lar)."""
    classes, mods = {}, {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            src = _resolve_from(fn, node)
            if src not in modules:
                continue
            for a in node.names:
                if a.name in _top_classes(modules[src][1]):
                    classes[a.asname or a.name] = (src, a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in modules and (a.asname or "." not in a.name):
                    mods[a.asname or a.name] = a.name
    return classes, mods


def _class_attrs(modules: dict, mod: str, cls: str, _seen: frozenset = frozenset()) -> set | None:
    """Sinifin (yerel tabanlar dahil) bilinen tum ozellik adlari; dinamik ya da
    yerel olmayan tabanli siniflarda None (= denetleme)."""
    if (mod, cls) in _seen or mod not in modules:
        return None
    fn, tree = modules[mod]
    node = _top_classes(tree).get(cls)
    if node is None or node.keywords:            # metaclass=... -> dinamik
        return None
    attrs: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            attrs.add(item.name)
        elif isinstance(item, ast.Assign):
            attrs.update(t.id for t in item.targets if isinstance(t, ast.Name))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            attrs.add(item.target.id)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Store):
            attrs.add(sub.attr)                   # self.x = ... (ornek ozniteligi)
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
              and sub.func.id in ("setattr", "vars")):
            return None
        elif isinstance(sub, ast.Attribute) and sub.attr == "__dict__":
            return None
    if attrs & _DYNAMIC_ATTR_HOOKS:
        return None
    classes, mods = _import_table(fn, tree, modules)
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "object":
            continue
        if isinstance(base, ast.Name) and base.id in _top_classes(tree):
            target = (mod, base.id)
        elif isinstance(base, ast.Name) and base.id in classes:
            target = classes[base.id]
        elif (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
              and base.value.id in mods):
            target = (mods[base.value.id], base.attr)
        else:
            return None                            # yerel olmayan taban
        inherited = _class_attrs(modules, *target, _seen | {(mod, cls)})
        if inherited is None:
            return None
        attrs |= inherited
    return attrs


def _file_method_gaps(user_fn: str, tree: ast.Module, modules: dict) -> list[tuple]:
    """Tek dosyadaki `x.metot()` / `self.x.metot()` cagrilarindan, x'in tek
    ve kesin olarak bir yerel sinifin ornegi oldugu ama sinifta `metot`
    bulunmayanlar: [(sinifin_dosyasi, sinif, metot, cagiran_dosya, satir)]."""
    classes, mods = _import_table(user_fn, tree, modules)
    if not classes and not mods:
        return []

    def cls_of(value):
        if not isinstance(value, ast.Call):
            return None
        f = value.func
        if isinstance(f, ast.Name) and f.id in classes:
            return classes[f.id]
        if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id in mods and f.attr in _top_classes(modules[mods[f.value.id]][1])):
            return (mods[f.value.id], f.attr)
        return None

    bindings: dict[tuple, list] = {}
    calls: list[tuple] = []
    handled: set[int] = set()   # izlenen atamalarin hedef dugumleri

    def visit(node, scopes, klass):
        def key_of(target):
            if isinstance(target, ast.Name):
                return ("n", id(scopes[-1]), target.id)
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == "self" and klass is not None):
                return ("s", id(klass), target.attr)
            return None

        def bind(k, value):
            bindings.setdefault(k, []).append(value)

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = child.args
                for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                    if arg is not None:
                        bind(("n", id(child), arg.arg), None)
                visit(child, [*scopes, child], klass)
                continue
            if isinstance(child, ast.ClassDef):
                visit(child, scopes, child)
                continue
            if isinstance(child, (ast.Global, ast.Nonlocal)):
                for name in child.names:
                    bind(("n", id(scopes[-1]), name), None)
                    bind(("n", id(scopes[0]), name), None)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)) and child.value is not None:
                for t in (child.targets if isinstance(child, ast.Assign) else [child.target]):
                    k = key_of(t)
                    if k is not None:
                        bind(k, cls_of(child.value))
                        handled.add(id(t))
            elif (isinstance(child, (ast.Name, ast.Attribute)) and isinstance(child.ctx, ast.Store)
                    and id(child) not in handled):
                k = key_of(child)            # for/with/tuple/walrus... -> belirsiz
                if k is not None:
                    bind(k, None)
            elif (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and not child.func.attr.startswith("__")):
                recv = child.func.value
                if isinstance(recv, ast.Name):
                    calls.append((("n", [id(sc) for sc in scopes], recv.id),
                                  child.func.attr, child.lineno))
                elif key_of(recv) is not None:
                    calls.append((key_of(recv), child.func.attr, child.lineno))
            visit(child, scopes, klass)

    visit(tree, [tree], None)

    def resolve(key):
        if key[0] == "n":
            _, scope_ids, name = key
            for sid in reversed(scope_ids):
                vals = bindings.get(("n", sid, name))
                if vals is not None:
                    break
            else:
                return None
        else:
            vals = bindings.get(key)
            if not vals:
                return None
        first = vals[0]
        return first if first is not None and all(v == first for v in vals) else None

    out = []
    for key, method, line in calls:
        target = resolve(key)
        if target is None:
            continue
        attrs = _class_attrs(modules, *target)
        if attrs is None or method in attrs:
            continue
        out.append((modules[target[0]][0], target[1], method, user_fn, line))
    return out


def _method_gaps(files: dict) -> list[tuple[str, str, str, str, int]]:
    """Yerel sinif ornegi uzerinden cagrilan ama sinifta olmayan metotlar
    (cagiran dosya + metot basina ilk satir)."""
    modules = _local_modules(files)
    gaps: dict[tuple, tuple] = {}
    for user_fn, src in files.items():
        if not user_fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for gap in _file_method_gaps(user_fn, tree, modules):
            gaps.setdefault(gap[:4], gap)
    return sorted(gaps.values(), key=lambda g: (g[3], g[4]))


def _missing_methods(files: dict) -> list[str]:
    return [f"{p}: {c} sınıfında '{m}' metodu yok ({u} satır {ln} kullanıyor)"
            for p, c, m, u, ln in _method_gaps(files)]


def _uses_gui(files: dict) -> bool:
    for fn, src in files.items():
        if fn.endswith(".py") and any(m.split(".")[0] in _GUI_MODULES for m in _imported_modules(fn, src)):
            return True
    return False


# Canli hata: Jarvis'in yeniden cagrisinda aciklama "Tkinter ile ... calculator.py"
# oldu; plan [calculator.py, README.md] cikti, GUI'siz calculator.py yazilip
# SMART EXIT ile "bitti" denildi. GUI isteyen aciklamada giris dosyasi beklenir.
_GUI_HINT_RE = re.compile(r"tkinter|aray[üu]z|\bgui\b|pencere|tu[şs]\s*tak[ıi]m", re.IGNORECASE)
_ENTRY_PLACEHOLDER = "main.py"


def _wants_gui(description: str) -> bool:
    return bool(_GUI_HINT_RE.search(description or ""))


def _is_runnable(fn: str, source: str) -> bool:
    """Calistirilinca bir sey yapan dosya: giris adi, __main__ korumasi ya da
    ust duzeyde calisan kod (yalnizca import/def/class/atama olan modul degil)."""
    if Path(fn).name in _ENTRY_SCRIPT_NAMES:
        return True
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return False
    if _has_main_guard(tree) or _toplevel_blocks(tree):
        return True
    inert = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef,
             ast.ClassDef, ast.Assign, ast.AnnAssign)
    return any(not isinstance(s, inert)
               and not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
               for s in tree.body)


def _gui_entry_files(files: dict) -> list[str]:
    """GUI giris dosyasi sayilanlar: main.py/cli.py/app.py/__main__.py ya da
    pencereyi (kendisi veya import ettigi yerel modul uzerinden) acan, calistirilabilir dosya."""
    gui_local = {_module_name(fn) for fn, src in files.items() if _uses_gui({fn: src})}
    out = []
    for fn, src in files.items():
        if not fn.endswith(".py") or _is_test_file(fn):
            continue
        if Path(fn).name in _ENTRY_SCRIPT_NAMES:
            out.append(fn)
        elif _is_runnable(fn, src) and (
                _uses_gui({fn: src}) or gui_local & set(_imported_modules(fn, src))):
            out.append(fn)
    return out


def _headless_env() -> dict:
    env = dict(os.environ)
    for key in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(key, None)
    env.update({"QT_QPA_PLATFORM": "offscreen", "MPLBACKEND": "Agg",
                "SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
                "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _module_name(fn: str) -> str | None:
    p = Path(fn)
    if p.suffix != ".py":
        return None
    stem = p.stem
    if stem == "conftest" or stem.startswith("test_") or stem.endswith("_test"):
        return None
    parts = list(p.parent.parts) + ([] if stem == "__init__" else [stem])
    parts = [x for x in parts if x not in ("", ".")]
    if not parts or not all(x.isidentifier() for x in parts):
        return None
    return ".".join(parts)


def _has_main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            names = [node.test.left, *node.test.comparators]
            if any(isinstance(n, ast.Name) and n.id == "__name__" for n in names):
                return True
    return False


def _toplevel_blocks(tree: ast.Module) -> bool:
    """Ust duzeyde (def/class disinda) input() ya da `while True` var mi."""
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.While) and isinstance(node.test, ast.Constant) and node.test.value:
                return True
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "input"):
                return True
    return False


def _is_entry_script(fn: str, source: str) -> bool:
    """Giris betigi: import edilince program calisir. Bunlar import EDILMEZ."""
    if Path(fn).name in _ENTRY_SCRIPT_NAMES:
        return True
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return False
    return not _has_main_guard(tree) and _toplevel_blocks(tree)


def _static_import_checks(fn: str, source: str) -> list[list]:
    """Giris betiginin import ettigi her modul/ad icin denetim satiri."""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    guarded = _guarded_import_nodes(tree)
    package = [p for p in Path(fn).parent.parts if p not in ("", ".")]
    checks: list[list] = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            checks.extend([fn, alias.name, None] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                mod = ".".join([*base, node.module] if node.module else base)
            else:
                mod = node.module or ""
            if not mod:
                continue
            checks.extend([fn, mod, alias.name] for alias in node.names if alias.name != "*")
    return checks


def _import_problems(files: dict, root: Path, timeout: int = _IMPORT_CHECK_TIMEOUT) -> tuple[list[str], list[str]]:
    """Projedeki modulleri ayri bir surecte GERCEKTEN import eder (ekran yok,
    stdin yok, zaman asimli). Giris betikleri (main.py, cli.py, app.py ya da
    __main__ korumasiz ust duzey input()/while True) calistirilmaz; yalnizca
    import ettikleri modul ve adlar denetlenir.
    Donus: (sorun metinleri, bulunamayan moduller)."""
    checks: list[list] = []
    for fn in files:
        mod = _module_name(fn)
        if not mod:
            continue
        source = files.get(fn) or ""
        if not source:
            fp = _contained_path(root, fn)
            if fp is not None and fp.is_file():
                source = fp.read_text(encoding="utf-8", errors="replace")
        if _is_entry_script(fn, source):
            checks.extend(_static_import_checks(fn, source))
        else:
            checks.append([fn, mod, None])
    if not checks:
        return [], []
    try:
        r = subprocess.run(
            [sys.executable, "-c", _IMPORT_SCRIPT, str(root), json.dumps(checks)],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
            env=_headless_env(),
        )
    except subprocess.TimeoutExpired:
        return [f"import denemesi zaman aşımı ({timeout}s): modül yüklenirken program bekliyor "
                "(üst düzey kod __main__ korumasına alınmalı)"], []
    except Exception as e:
        return [f"import denemesi yapılamadı: {type(e).__name__}"], []
    out = r.stdout or ""
    at = out.rfind(_IMPORT_MARKER)
    if at < 0:
        # Modul sureci kendisi bitirdiyse (os._exit, sys.exit) isaret hic
        # basilmaz. Bu bir import hatasi kaniti degildir: SMART EXIT'i
        # engelleyen sahte sorun uretme, yalnizca log'a yaz.
        tail = (r.stderr or out).strip()[-300:]
        logger.warning(f"[Coder] import denetimi sonuç vermedi (exit {r.returncode}): {tail}")
        return [], []
    try:
        found = json.loads(out[at + len(_IMPORT_MARKER):].splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        logger.warning("[Coder] import denetimi çıktısı ayrıştırılamadı")
        return [], []
    problems: list[str] = []
    missing: list[str] = []
    for label, kind, name, msg in found:
        problems.append(f"{label} import edilemedi: {kind}: {msg}"[:400])
        if kind == "ModuleNotFoundError" and name and name not in missing:
            missing.append(name)
    return problems, missing


_PYTEST_TIMEOUT = 60
_REJECT_LIMIT = 3
_REJECTED_PREFIX = "_reddedilen_"


def _rejected_copy_name(filename: str) -> str:
    safe = re.sub(r"[^\w.-]+", "_", filename or "").strip("._") or "dosya"
    return f"{_REJECTED_PREFIX}{safe}.txt"
_RUFF_STREAK_LIMIT = 3
_SAME_WRITE_LIMIT = 3
_METHOD_STREAK_LIMIT = 3    # ayni eksik metot bu kadar tur duzelmezse gorev biter
_LOCK_STREAK_LIMIT = 2      # ayni dosyaya ust uste kilit -> eksik dosyayi LLM'siz uret
_LOCK_TOTAL_LIMIT = 4       # toplam kilit bunu gecerse gorev biter
_TEST_CORE_ATTEMPTS = 2
# Model yazmayi reddederse deterministik uretilen dosyalar.
_GENERATED_FILES = ("README.md", "tests/__init__.py", "tests/test_core.py")


def _is_test_file(fn: str) -> bool:
    stem = Path(fn).stem.lower()
    return fn.endswith(".py") and (stem.startswith("test_") or stem.endswith("_test"))


def _run_project_pytest(root: Path, timeout: int = _PYTEST_TIMEOUT) -> str:
    """Proje testlerini ekransiz, stdin kapali calistirir. Bos donus = gecti
    (ya da toplanacak test yok); degilse modele verilecek hata metni."""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, timeout=timeout, env=_headless_env(),
        )
    except subprocess.TimeoutExpired:
        return f"PYTEST ZAMAN AŞIMI ({timeout}s): testler bitmiyor (sonsuz döngü / input() bekleyen test?)"
    except Exception as e:
        return f"PYTEST çalıştırılamadı: {type(e).__name__}"
    if r.returncode in (0, 5):  # 5: toplanacak test yok
        return ""
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return ("PYTEST BAŞARISIZ — testler geçmiyor; çıktıyı oku ve KODU (ya da hatalı testi) "
            f"düzelt:\n{out[-600:]}")


_EOF_RE = re.compile(r"^EOFError\b", re.MULTILINE)


def _interactive_eof(stderr: str | None) -> bool:
    """Traceback'in son satiri EOFError: stdin kapaliyken input() cagrildi."""
    lines = [ln for ln in (stderr or "").strip().splitlines() if ln.strip()]
    return bool(lines) and bool(_EOF_RE.match(lines[-1]))


def _verify_project(task, run_pytest=True):
    import subprocess
    import sys
    import re as _vt
    problems = []
    for _fn, _fc in task.files_written.items():
        _fp = _contained_path(task.project_path, _fn)
        if _fp is None:
            problems.append(f"Proje dışı yol: {_fn}")
            continue
        _fp.parent.mkdir(parents=True, exist_ok=True)
        _fp.write_text(_fc, encoding="utf-8")
    for _ef in task.expected_files:
        if _ef not in task.files_written:
            _dp = task.project_path / _ef
            if not _dp.is_file():
                problems.append(f"Eksik dosya: {_ef}")
    _rd = task.files_written.get("README.md", "")
    if len(_rd.strip()) < 200:
        problems.append(f"README.md yetersiz ({len(_rd.strip())} chars, min 200)")
    _tc = ""
    for _fn, _fc in task.files_written.items():
        _st = Path(_fn).stem.lower()
        if _st.startswith("test_") or _st.endswith("_test"):
            _tc += _fc + "\n"
    _tf = _vt.findall(r"def\s+(test_\w+)\s*\(", _tc)
    if task.min_test_count > 0 and len(_tf) < task.min_test_count:
        problems.append(f"Test sayisi yetersiz: {len(_tf)}/{task.min_test_count}")
    elif not _tf and any("test" in f.lower() for f in task.files_written):
        problems.append("Test dosyalarinda def test_* yok")
    for _fn, _fc in task.files_written.items():
        if _fn.endswith(".py"):
            try:
                compile(_fc, _fn, "exec")
            except SyntaxError as _se:
                problems.append(f"SyntaxError {_fn}: line {_se.lineno}: {_se.msg}")
    for _fn, _fc in task.files_written.items():
        if _fn.endswith("__init__.py"):
            continue
        if len(_fc.strip()) < 150:
            problems.append(f"{_fn}: cok kisa ({len(_fc)} chars)")
    if run_pytest and _tc:
        try:
            _pt = subprocess.run(
                [sys.executable, "-m", "pytest", "--tb=short", "-q"],
                cwd=str(task.project_path), capture_output=True,
                text=True, timeout=60)
            if _pt.returncode != 0:
                problems.append(f"pytest FAILED: {(_pt.stdout or '')[-200:]}")
        except Exception as _pe:
            problems.append(f"pytest hatasi: {_pe}")
    _rl = _ruff_blocking([task.project_path / f for f in task.files_written if f.endswith(".py")],
                         task.project_path)
    problems.extend(f"RUFF {ln}" for ln in _rl.splitlines() if ln.strip())
    for _mf in _missing_local_modules(task.files_written, task.project_path):
        problems.append(f"Eksik modül dosyası: {_mf} (import ediliyor ama yazılmadı)")
    _imp_problems, _ = _import_problems(task.files_written, task.project_path)
    problems.extend(_imp_problems)
    problems.extend(_missing_methods(task.files_written))
    # GUI (tkinter/PyQt...) projesinde "main.py --help" argumani tanimaz,
    # dogrudan pencereyi acar: yalnizca ast + gercek import denetimi yeterli.
    if _uses_gui(task.files_written):
        return (len(problems) == 0, problems)
    # stdin kapali + ekransiz: etkilesimli program input()'ta EOFError ile
    # biter; bu bir hata degil, "kullanicidan girdi bekliyor" demektir.
    _quiet = dict(cwd=str(task.project_path), capture_output=True, text=True,
                  stdin=subprocess.DEVNULL, env=_headless_env())
    for _cn in ("cli.py", "main.py"):
        if (task.project_path / _cn).is_file():
            try:
                _ct = subprocess.run([sys.executable, _cn, "--help"], timeout=15, **_quiet)
                if _ct.returncode != 0 and not _interactive_eof(_ct.stderr):
                    try:
                        _imp = subprocess.run(
                            [sys.executable, "-c",
                             f"import {Path(_cn).stem}; print('IMPORT_OK')"],
                            timeout=10, **_quiet)
                        if "IMPORT_OK" not in _imp.stdout and not _interactive_eof(_imp.stderr):
                            problems.append(f"{_cn} hem --help hem import basarisiz (exit {_ct.returncode}): {(_ct.stderr or _ct.stdout or '')[-150:]}")
                    except Exception:
                        problems.append(f"{_cn} calismiyor (exit {_ct.returncode})")
                break
            except subprocess.TimeoutExpired:
                problems.append(f"{_cn} TIMEOUT (15s)")
            except Exception:
                pass
    return (len(problems) == 0, problems)

# ── System Prompt ─────────────────────────────────────────────

_SYSTEM_PROMPT = """Sen Jarvis'in Kıdemli Baş Yazılım Mühendisisisin.

GÖREVİN: Kullanıcının kodlama isteklerini çöz, TAM ÇALIŞAN KOD yaz.

ASLA YAPMA:
- `pass` yazma
- `# TODO`, `# buraya yazın` gibi placeholder yorum yazma
- İskellet/stub/boş fonksiyon yazma
- `...` ile kod kısaltma
- Fonksiyonları "geri kalanı aynı" diye geçiştirme

HER FONKSİYON ÇALIŞIR KOD İÇERMELİ. `python3 dosya.py` ile HATASIZ çalışmalı.

ÇOK DOSYALI PROJELER:
- Her adımda tek dosyanın TAM ve ÇALIŞIR kodunu yaz
- Dosyalar arası import'ları doğru yaz
- TÜM dosyaları YAZDIĞINDAN EMİN OL!
- Her dosyayı ayrı ayrı yaz: ana dosya, yardımcı modül, config vb.
- TÜM dosyaları yaz — main.py DAHİL!
- Üretilen dosya sayısı = istenen dosya sayısı eşleşmeden ACCEPT yapma.

ÇIKTI FORMATI (SADECE JSON):
{
  "thought": "Ne yapıyorum ve neden",
  "action": "write | run | inspect | fix | accept",
  "args": {
    "filename": "dosya_adı.py",
    "content": "tam kod (sadece write/fix için)",
    "command": "komut (sadece run için)",
    "fix_note": "düzeltme açıklaması (sadece fix için)"
  },
  "response": "Sadece accept ise final özet"
}
"""


# ── Yardımcı Fonksiyonlar ────────────────────────────────────

_EVAL_EXEC = frozenset({"eval", "exec"})
_EVAL_EXEC_MSG = (
    "eval/exec kullanma; ast.literal_eval ya da kendi ayrıştırıcını yaz. "
    "eval kullanma. Şu güvenli değerlendiriciyi Calculator sınıfına calculate(expr) "
    "metodu olarak ekle ve main.py'de ekran metnini ona ver."
)

# eval reddinde modele verilen, kopyalanabilir guvenli degerlendirici. Kucuk
# modeller (Ollama) alternatifi bilmeden ayni eval'i tekrar yaziyordu. Bu kod
# kendi dogrulamamizdan (_validate_file, eval/exec kapisi, ruff) gecer.
_SAFE_EVAL_SHORT = '''import ast
import operator

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod}


def safe_eval(expr: str) -> float:
    """Yalnizca sayi, + - * / ** %, parantez ve tek terimli eksi; gerisi ValueError."""
    def walk(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("üs çok büyük")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise ValueError(f"izin verilmeyen ifade: {type(node).__name__}")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError("geçersiz ifade") from e
    return walk(tree.body)
'''

_SAFE_EVAL_CLASS = '''

class Calculator:
    """Hesap makinesi: ekran metnini eval() kullanmadan hesaplar."""

    def calculate(self, expr: str) -> float:
        """Ekran metnini (ör. "2+3*4") hesaplar; geçersiz ifadede ValueError."""
        return safe_eval(expr)
'''


def _safe_eval_code(full: bool = False) -> str:
    """Kopyalanabilir guvenli degerlendirici: kisa (yalnizca safe_eval) ya da
    tam (Calculator.calculate ile)."""
    return _SAFE_EVAL_SHORT + (_SAFE_EVAL_CLASS if full else "")


def _safe_eval_hint(full: bool) -> str:
    if not full:
        return ("KISA ÖRNEK (kopyala; Calculator sınıfına calculate(expr) metodu olarak bağla):\n"
                f"```python\n{_safe_eval_code(False)}```")
    return ("TAM ÖRNEK — bu kodu Calculator'ın bulunduğu dosyaya AYNEN koy (sınıfın kendi "
            "metotlarını koru, calculate'i ekle):\n"
            f"```python\n{_safe_eval_code(True)}```\n"
            "main.py'de \"=\" tuşu: sonuc = Calculator().calculate(ekran_metni); "
            "ValueError ve ZeroDivisionError yakala, ekrana \"Error\" yaz. eval/exec YOK.")


def _is_builtins_ref(node: ast.AST) -> bool:
    """builtins / __builtins__ / __import__('builtins') ifadesi mi."""
    if isinstance(node, ast.Name):
        return node.id in ("builtins", "__builtins__")
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "__import__")


def _eval_exec_call(tree: ast.AST) -> ast.Call | None:
    """eval()/exec() cagrisi: dogrudan, builtins.eval, __import__(...).exec
    ya da getattr(builtins, 'eval'). Kendi nesnesinin .eval() metodu serbest."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in _EVAL_EXEC:
            return node
        if isinstance(f, ast.Attribute) and f.attr in _EVAL_EXEC and _is_builtins_ref(f.value):
            return node
        if (isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2
                and _is_builtins_ref(node.args[0])
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _EVAL_EXEC):
            return node
    return None


# Python'da stub/yer tutucu denetimi ast + tokenize ile yapilir. Eskiden duz
# metin aramasi docstring/metin/yorumdaki "pass" kelimesini, `except X: pass`,
# `class Hata(Exception): pass`, Protocol govdesindeki `...` ve
# `except NotImplementedError` satirlarini da stub sayip mesru kodu
# reddediyordu (canli: "main.py yaz" 22 tur ust uste reddedildi).
_PLACEHOLDER_COMMENT_RE = re.compile(
    r"^\s*(?:(?P<tag>TODO|FIXME|XXX)\b"
    r"|(?:buraya|burayı)\b.*\b(?:yaz|ekle|doldur|gel)"
    r"|doldur\b|implement\b|your code)",
    re.IGNORECASE,
)
_STUB_SKIP_DECORATORS = frozenset({"abstractmethod", "overload", "abstractproperty"})


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _is_stub_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
    return False


def _stub_function(tree: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Govdesi (docstring disinda) yalnizca pass / ... / raise NotImplementedError
    olan fonksiyon. Protocol siniflari ve @abstractmethod/@overload haric."""
    protocol_funcs: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(_decorator_name(b) == "Protocol" for b in node.bases):
            protocol_funcs |= {id(n) for n in node.body}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or id(node) in protocol_funcs:
            continue
        if any(_decorator_name(d) in _STUB_SKIP_DECORATORS for d in node.decorator_list):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if not body or all(_is_stub_stmt(st) for st in body):
            return node
    return None


def _placeholder_comment(code: str) -> tuple[int, str] | None:
    """Yalnizca GERCEK yorumlarda (metin/docstring degil) yer tutucu."""
    import io
    import tokenize
    try:
        for tok in tokenize.generate_tokens(io.StringIO(code).readline):
            if tok.type == tokenize.COMMENT and _PLACEHOLDER_COMMENT_RE.match(tok.string.lstrip("#")):
                return tok.start[0], tok.string.strip()
    except (tokenize.TokenError, SyntaxError):
        return None
    return None


def _validate_code(code: str, language: str = "python") -> tuple[bool, str]:
    """Kodda yasaklı kalıp var mı kontrol et. Ret mesajı modele NEYİ
    değiştirmesi gerektiğini söyler."""
    if language != "python":
        for pattern, label in _FORBIDDEN_PATTERNS:
            if pattern.search(code):
                return False, f"YASAK: {label} bulundu — bu kalıbı kaldırıp gerçek kodu yaz"
        return True, "OK"

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, (f"SYNTAX_ERROR: line {e.lineno}: {e.msg} — dosyanın TAM ve "
                       "derlenebilir halini yaz")
    stub = _stub_function(tree)
    if stub is not None:
        return False, (f"YASAK (line {stub.lineno}): '{stub.name}' fonksiyonu boş (yalnızca "
                       "pass / ... / NotImplementedError) — gövdesine gerçek mantığı yaz")
    placeholder = _placeholder_comment(code)
    if placeholder is not None:
        line, text = placeholder
        return False, (f"YASAK (line {line}): yer tutucu yorum ({text[:60]}) — yorumu sil "
                       "ve o kısmı gerçekten kodla (tam halini yaz)")
    # Canli hata: "=" tusu eval() ile yazilmisti (kod enjeksiyonu).
    bad = _eval_exec_call(tree)
    if bad is not None:
        return False, f"YASAK (line {bad.lineno}): {_EVAL_EXEC_MSG}"

    return True, "OK"


_LANG_EXT = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "node": "js",
    "typescript": "ts", "ts": "ts", "go": "go", "golang": "go", "ruby": "rb",
    "php": "php", "bash": "sh", "shell": "sh", "sh": "sh", "rust": "rs",
    "java": "java", "c": "c", "cpp": "cpp", "c++": "cpp",
}


def _lang_ext(language: str) -> str:
    """'python' -> 'py'. Eskiden dosya adi f"main.{language}" ile
    uretiliyordu; beklenen dosya 'main.python' oluyor, model de bu adla
    gercekten bir dosya yaziyordu."""
    lang = (language or "python").strip().lower()
    return _LANG_EXT.get(lang, lang)


_CODE_SUFFIXES = {".py", ".js", ".ts", ".go", ".rb", ".php", ".sh", ".java", ".rs", ".c", ".cpp"}


# Canli hata: model icerigi ```python ... ``` icinde verdi, citler dosyaya
# girdi ve gecerli kod SyntaxError ile reddedildi. Yalnizca TUM icerigi saran
# cit soyulur (ilk satir ```dil, son satir ```); ortadaki ``` (docstring,
# metin) korunur. Markdown dosyalarinda cit icerigin parcasi olabilir.
_WRAP_FENCE_RE = re.compile(r"\A\s*```[\w+.#-]*[ \t]*\n(?P<body>.*?\n)?[ \t]*```\s*\Z", re.DOTALL)
_FENCE_KEEP_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}


def _strip_wrapping_fence(filename: str, content: str) -> str:
    if Path(filename).suffix.lower() in _FENCE_KEEP_SUFFIXES:
        return content
    m = _WRAP_FENCE_RE.match(content or "")
    return (m.group("body") or "") if m else content


def _validate_file(filename: str, content: str, language: str = "python") -> tuple[bool, str]:
    """Dosya turune gore dogrulama.

    .py      → yasakli kaliplar + ast.parse
    diger kod → sadece yasakli kaliplar (pass/TODO/... stub yasagi)
    .json    → json.loads
    metin (.md, .txt, .toml, .yml, .html, .css ...) → kontrol yok
    """
    suffix = Path(filename).suffix.lower()
    if suffix == ".py":
        return _validate_code(content, "python")
    if suffix in _CODE_SUFFIXES:
        return _validate_code(content, suffix.lstrip("."))
    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as e:
            return False, f"JSON_ERROR: line {e.lineno}: {e.msg}"
    return True, "OK"


def _contained_path(root: Path, name: str) -> Path | None:
    """LLM'den gelen dosya adini proje kokune bagla. Mutlak yol, '../' veya
    koke disari isaret eden bir symlink proje disina cikiyorsa None doner.
    Onay "bu projeye kod yaz" icindir, $HOME'un herhangi bir yerine yazmak
    ya da oradaki bir dosyayi calistirmak icin degil."""
    if not name or "\x00" in name:
        return None
    root = root.resolve()
    candidate = (root / name).resolve()
    if candidate == root or not candidate.is_relative_to(root):
        return None
    return candidate


def _run_file(path: Path, *, root: Path, timeout: int = _RUN_TIMEOUT) -> str:
    """Dosyayı çalıştır ve çıktıyı döndür (stdin=DEVNULL güvenli).
    Sadece `root` (proje dizini) icindeki dosyalar calistirilir."""
    path = path.resolve()
    if _contained_path(root, str(path)) != path:
        return f"[BLOCKED] Proje dizini dışında dosya çalıştırılamaz: {path}"
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}"

    try:
        result = subprocess.run(
            interp + [str(path)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(path.parent),
        )
        parts = []
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()[:_MAX_OUTPUT_CHARS]}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()[:_MAX_OUTPUT_CHARS]}")
        status = "SUCCESS" if result.returncode == 0 else f"FAIL(rc={result.returncode})"
        return f"[{status}] " + ("\n".join(parts) if parts else "(no output)")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] {timeout}sn aşıldı"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


def _parse_model_response(raw: str) -> dict:
    """Model çıktısından JSON çıkar (farklı formatlara dayanıklı)."""
    raw = raw.strip()

    # 1. Direkt JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block içinde
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL)
    for block in blocks:
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    # 3. İlk { ... } bloğunu yakala
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    # 4. json_repair: kesik (num_predict sinirinda bitmis), tirnak/virgul
    # hatali LLM ciktisini onar. Eskiden burada {} donup dongu tamamen
    # bitiyordu. Onarilan "content" kesik olabilir; o durumda _validate_code
    # SYNTAX_ERROR ile reddeder ve model bir sonraki turda tekrar yazar.
    repaired = repair_llm_json(raw)
    if repaired:
        return repaired

    return {}


def repair_llm_json(raw: str) -> dict:
    """json_repair kuruluysa bozuk JSON'u dict'e onarir; degilse {}."""
    try:
        from json_repair import repair_json
    except ImportError:
        return {}
    text = raw
    fence = re.search(r"```(?:json)?\s*\n(.*)", raw, re.DOTALL)
    if fence:
        text = fence.group(1).rsplit("```", 1)[0]
    start = text.find("{")
    if start == -1:
        return {}
    try:
        obj = repair_json(text[start:], return_objects=True)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) and obj else {}


def _ruff_cmd() -> list[str] | None:
    exe = _sh_mod.which("ruff")
    if exe:
        return [exe]
    try:
        import ruff  # noqa: F401  (pip paketi `python -m ruff` saglar)
    except ImportError:
        return None
    return [sys.executable, "-m", "ruff"]


# Yalnizca GERCEK hatalar engeller: sozdizimi (E9), F63x/F7xx, tanimsiz ad
# (F821/F822/F823) ve yeniden tanim (F811). F401/E501 gibi stil kurallari
# engellemez. pytest fikstur kaliplari F811 urettigi icin test dosyalarinda F811 yok.
_RUFF_BLOCKING = "E9,F63,F7,F82,F811"
_RUFF_BLOCKING_TESTS = "E9,F63,F7,F82"
_RUFF_LINE_RE = re.compile(r"^(?P<file>[^:]+):\d+:\d+: (?P<rest>.*)$")


def _ruff_select(path: Path) -> str:
    return _RUFF_BLOCKING_TESTS if _is_test_file(path.name) else _RUFF_BLOCKING


def _ruff_blocking(paths: list[Path], root: Path) -> str:
    """Verilen .py dosyalarindaki engelleyici ruff bulgulari (concise, goreli yol)."""
    cmd = _ruff_cmd()
    paths = [p for p in paths if p.suffix == ".py" and p.is_file()]
    if cmd is None or not paths:
        return ""
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(_ruff_select(p), []).append(str(p))
    out: list[str] = []
    for select, files in groups.items():
        try:
            r = subprocess.run(
                [*cmd, "check", "--isolated", "--quiet", "--no-cache",
                 "--output-format", "concise", "--select", select, *files],
                capture_output=True, text=True, timeout=30)
        except Exception as e:
            logger.debug(f"[Coder] ruff atlandı: {type(e).__name__}")
            continue
        text = (r.stdout or "").strip()
        if text:
            out.append(text)
    prefix = str(root.resolve()) + os.sep
    return "\n".join(out).replace(prefix, "").replace(str(root) + os.sep, "")


def _ruff_signature(problems: dict) -> frozenset:
    """Satir/sutun numarasindan bagimsiz hata imzasi (ayni hata = ayni imza)."""
    sig = set()
    for text in problems.values():
        for line in text.splitlines():
            m = _RUFF_LINE_RE.match(line.strip())
            sig.add((m.group("file"), m.group("rest")) if m else line.strip())
    return frozenset(sig)


def _ruff_autofix(path: Path) -> tuple[str | None, str]:
    """Yazilan .py dosyasina LLM'siz on-duzeltme uygular.

    1) `ruff check --fix` (sadece guvenli duzeltmeler: kullanilmayan import vb.)
    2) `ruff format`
    3) Kalan GERCEK hatalari (sozdizimi, tanimsiz isim) dondurur ki bir
       sonraki LLM turu sadece onlara odaklansin.

    --isolated: kullanici/proje ayarlari hesaba katilmaz, sonuc tekrarlanabilir.
    Donus: (duzeltilmis_icerik veya None, kalan_hatalar_metni)
    """
    cmd = _ruff_cmd()
    if cmd is None or path.suffix != ".py":
        return None, ""
    common = ["--isolated", "--quiet", "--no-cache"]
    try:
        subprocess.run([*cmd, "check", *common, "--fix", "--exit-zero", str(path)],
                       capture_output=True, text=True, timeout=20)
        subprocess.run([*cmd, "format", *common, str(path)],
                       capture_output=True, text=True, timeout=20)
        remaining = subprocess.run(
            [*cmd, "check", *common, "--output-format", "concise",
             "--select", _ruff_select(path), str(path)],
            capture_output=True, text=True, timeout=20,
        )
        fixed = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.debug(f"[Coder] ruff atlandı: {type(e).__name__}")
        return None, ""
    problems = (remaining.stdout or "").strip().replace(str(path.parent) + "/", "")
    return fixed, problems[:1500]


# ── Agentic Coding Engine ─────────────────────────────────────

class AgenticCoder:
    """
    Iteratif kod yazma motoru.

    Döngü:
        think → action(write/run/inspect/fix/accept) → observe → repeat

    Güvenlik:
        - MAX_ITERATIONS = 15 → sonsuz döngü koruması
        - _validate_code → TODO/pass yasak
        - stdin=DEVNULL → input() bloğu yok
    """

    def __init__(
        self,
        model_fn: Callable[[str], str] | None = None,
        ui: Any = None,
        max_iterations: int = MAX_ITERATIONS,
    ):
        """
        Args:
            model_fn: LLM prompt → raw text fonksiyonu.
                      Varsayılan: Gemini (eğer kullanılabilirse)
            ui:        JarvisUI (opsiyonel, progress göstermek için)
            max_iterations: Maksimum döngü sayısı
        """
        self._model_fn = model_fn or self._default_model
        self._uses_default_model = model_fn is None
        self._ui = ui
        self._max = max_iterations
        self._live_ref: tuple[CodingTask, list[CodingStep]] | None = None

    @staticmethod
    def _default_model(prompt: str) -> str:
        """
        Varsayılan LLM: Gemini API.
        Kullanılamazsa Ollama fallback.
        """
        # 1. Gemini dene (CRITICAL FIX: API key kontrolü ÖNCE, client leak önleme)
        api_key = _gemini_api_key()
        if not api_key:
            logger.info("[Coder] Gemini API anahtarı yok → Ollama")
        elif not _gemini_available():
            logger.info("[Coder] Gemini kotası dolu (bekleme süresi) → Ollama")
        else:
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
                try:
                    from google.genai import types as _gtypes
                    resp = client.models.generate_content(
                        model=_GEMINI_CODER_MODEL,
                        contents=prompt,
                        config=_gtypes.GenerateContentConfig(
                            max_output_tokens=8192,
                            temperature=0.3,
                            # Ollama'daki format="json" karsiligi: cevap
                            # dogrudan JSON gelsin, markdown citi olmasin.
                            response_mime_type="application/json",
                            automatic_function_calling=_gtypes.AutomaticFunctionCallingConfig(disable=True),
                        ),
                    )
                    _set_last_model("gemini", _GEMINI_CODER_MODEL)
                    return resp.text or ""
                finally:
                    # CRITICAL FIX: Client async resource leak prevention
                    try:
                        if hasattr(client, '_async_httpx_client'):
                            import asyncio as _aio
                            _aio.get_event_loop().run_until_complete(
                                client._async_httpx_client.aclose()
                            )
                    except Exception:
                        pass
            except Exception as e:
                _note_gemini_failure(e)
                logger.warning(f"[Coder] Gemini hatası ({type(e).__name__}) → Ollama")

        # 2. Ollama fallback
        try:
            import ollama
            _ollama_kwargs = dict(
                model=_pick_ollama_coder_model(),
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.2, "num_predict": 8192, "num_ctx": 16384},
            )
            try:
                resp = ollama.chat(format="json", **_ollama_kwargs)
            except ollama.ResponseError as json_err:
                # Canli testte format="json" istekleri ~170 token sonra HTTP
                # 500 ile kesildi (ayni istek Jarvis disinda 200). JSON
                # kisitini kaldirip bir kez daha dene; cikti zaten
                # _parse_model_response + json_repair ile ayristiriliyor.
                logger.warning(
                    f"[Coder] Ollama format=json hatası ({str(json_err)[:200]}) → kısıtsız tekrar"
                )
                resp = ollama.chat(**_ollama_kwargs)
            _set_last_model("ollama", _ollama_kwargs["model"])
            return resp.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[Coder] Ollama da yok ({type(e).__name__}: {str(e)[:200]})")
            return json.dumps({
                "thought": "LLM bulunamadı",
                "action": "error",
                "args": {},
                "response": "HATA: Kullanılabilir LLM yok (Gemini/Ollama)",
            })

    async def solve(
        self,
        description: str,
        language: str = "python",
        project_path: str | None = None,
        target_filename: str | None = None,
    ) -> str:
        """
        Kodlama görevini çöz (iteratif döngü).

        Args:
            description:     Ne yapılacağı ("Hesap makinesi yaz")
            language:        Programlama dili
            project_path:    Hedef dizin (varsayılan: ~/Desktop/jarvis_code)
            target_filename: Ana dosya adı (otomatik belirlenebilir)

        Returns:
            str: Final özet + yapılan işlerin listesi
        """
        # Son durum (bitti/başarısız) her çıkış yolunda — istisna ve iptal
        # dahil — panoya yazılır; pano hatası sonucu ASLA değiştirmez.
        self._live_ref = None
        finished = False
        try:
            result = await self._solve(description, language, project_path, target_filename)
            finished = True
            return result
        finally:
            ref = self._live_ref
            ok = finished and ref is not None and ref[0].accepted
            self._publish_live("bitti" if ok else "başarısız")

    def _live_model(self) -> dict:
        if not self._uses_default_model:
            return {"provider": "özel", "name": ""}
        return last_model() or {"provider": "bilinmiyor", "name": ""}

    def _publish_live(self, status: str = "çalışıyor") -> None:
        """Panoya degismez ozet yayimlar. Hata asla disari sizmaz."""
        try:
            if self._live_ref is None:
                return
            task, steps = self._live_ref
            _live_publish(_live_snapshot(task, steps, status, self._live_model(), self._max))
        except Exception as e:
            logger.debug(f"[Coder] pano durumu yayımlanamadı: {type(e).__name__}")

    async def _solve(
        self,
        description: str,
        language: str,
        project_path: str | None,
        target_filename: str | None,
    ) -> str:
        # Extract project_path from description if not explicitly passed
        if not project_path:
            import re as _rp
            _path_m = _rp.search(r'(~/[\w/\-_.]+|/[\w/\-_.]+)', str(description))
            if _path_m:
                _pp = Path(_path_m.group()).expanduser()
                # Only use if it looks like a project dir (not just a word)
                if '/' in str(_pp) and len(str(_pp)) > 5:
                    project_path = str(_pp)
        
        # Goreli yol ("StockFlow") calisma dizinine gore cozuluyordu; Jarvis
        # repo kokunden calistigi icin proje Jarvis'in KENDI reposuna
        # yaziliyordu. Goreli yollar her zaman ~/jarvis_programs altina gider.
        if project_path:
            _pp = Path(str(project_path)).expanduser()
            if not _pp.is_absolute():
                _pp = Path.home() / "jarvis_programs" / _pp
            project_path = str(_pp)

        # SECURITY: enforce $HOME containment for project paths
        _candidate = (Path(project_path) if project_path
                      else _auto_project_dir(description)).expanduser().resolve()
        _home = Path.home().resolve()
        if not (_home in _candidate.parents or _candidate == _home):
            raise ValueError(f"GÜVENLİK: proje yolu $HOME dışında: {_candidate}")
        task = CodingTask(
            description=description,
            language=language,
            project_path=_candidate,
        )
        task.project_path.mkdir(parents=True, exist_ok=True)

        # Auto-parse expected files from description
        import re as _re
        _fn_pattern = _re.findall(r"(?:[\w/]+\.py|[\w/]+\.js|[\w/]+\.ts|[\w/]+\.html|[\w/]+\.css|[\w/]+\.json|[\w/]+\.txt|[\w/]+\.md|[\w/]+\.toml|[\w/]+\.cfg|[\w/]+\.ini|[\w/]+\.yml|[\w/]+\.yaml|[\w/]+\.rst)", description)
        # Also catch bare directory references like "tests/" or "src/"
        _dir_pattern = _re.findall(r"(?<![\w/])([a-zA-Z_][\w]*[/\\])(?![\w])", description)
        for _d in _dir_pattern:
            _fn_pattern.append(_d.rstrip("/\\") + "/__init__.py")
        task.expected_files = list(dict.fromkeys(_fn_pattern))  # unique, preserve order
        if target_filename and target_filename not in task.expected_files:
            task.expected_files.append(target_filename)
        if not task.expected_files:
            task.expected_files = [f"main.{_lang_ext(language)}"]
        # Always require README.md and tests/ for multi-file projects
        if 'README.md' not in task.expected_files:
            task.expected_files.append('README.md')
        _has_real_test = any(
            Path(f).stem.startswith('test_') or Path(f).stem.endswith('_test')
            for f in task.expected_files
        )
        if len(task.expected_files) >= 3 and not _has_real_test:
            if 'tests/__init__.py' not in task.expected_files:
                task.expected_files.append('tests/__init__.py')
            if 'tests/test_core.py' not in task.expected_files:
                task.expected_files.append('tests/test_core.py')
        # GUI isteniyor ama planda giris dosyasi yok: main.py beklenir (README'den
        # once). Test zorunlulugu (3+ dosya) bu ekleme yuzunden degismez.
        if (_wants_gui(description)
                and not any(Path(f).name in _ENTRY_SCRIPT_NAMES for f in task.expected_files)):
            task.entry_required = True
            task.expected_files.insert(task.expected_files.index('README.md'), _ENTRY_PLACEHOLDER)
        _tc_m = _re.search(r'(\d+)\s+(?:adet\s+)?test\b', description, _re.I)
        if _tc_m:
            task.min_test_count = int(_tc_m.group(1))
        self._ui_progress(f"  [PLAN] Beklenen dosyalar: {task.expected_files}")

        steps: list[CodingStep] = []
        last_run_output = ""
        last_error = ""
        self._live_ref = (task, steps)
        self._publish_live()

        description = str(description)  # HIGH FIX: type coercion
        self._ui_progress(f"🔨 Agentic coding başlıyor: {description[:60]}")

        # ═══ AGENTGREP SCAN: projedeki mevcut dosyaları tara ═══
        try:
            _scan_result = await asyncio.to_thread(_scan_project, description, task.project_path)
            if _scan_result:
                self._ui_progress(f"  [SCAN] agentgrep: {_scan_result[:100]}")
        except Exception:
            pass

        llm_error: str | None = None
        _llm_error_streak = 0

        # NOT: Bu coroutine Jarvis'in canli ses oturumuyla AYNI event loop'ta
        # calisiyor. LLM, dosya calistirma ve pytest gibi bloklayan isler
        # asyncio.to_thread ile ayri thread'e aliniyor; aksi halde kodlama
        # boyunca mikrofon/hoparlor/websocket tamamen donuyordu.
        for i in range(self._max):
            self._publish_live()      # onceki eylemin sonu (to_thread isi yok)
            # ── Import denetimi (LLM'siz): eksik yerel modul + gercek import ──
            await asyncio.to_thread(self._sync_imports, task)
            self._sync_entry(task)
            if task.mnf_streak > _MNF_STREAK_LIMIT:
                # Ayni ModuleNotFoundError modele 3 tur gosterildi, duzelmedi:
                # kalan turlar kota yakmasin.
                task.missing_modules = list(task.import_missing)
                task.stop_reason = task.final_response = (
                    f"EKSIK MODUL: {', '.join(task.missing_modules)} — aynı ModuleNotFoundError "
                    f"{_MNF_STREAK_LIMIT} tur üst üste düzelmedi, görev durduruldu."
                )
                self._ui_progress(f"  ❌ {task.final_response}")
                break

            # ── ruff gercek hatalari: ayni hata 3 tur duzelmezse dur ──
            _sig = _ruff_signature(task.ruff_problems)
            task.ruff_streak = (task.ruff_streak + 1 if _sig and _sig == task.ruff_sig
                                else (1 if _sig else 0))
            task.ruff_sig = _sig
            if task.ruff_streak > _RUFF_STREAK_LIMIT:
                _txt = "; ".join(t.splitlines()[0] for t in task.ruff_problems.values() if t)
                task.stop_reason = task.final_response = (
                    f"RUFF HATASI düzelmedi ({_RUFF_STREAK_LIMIT} tur üst üste): {_txt[:400]} — görev durduruldu."
                )
                self._ui_progress(f"  ❌ {task.final_response}")
                break

            # ── eksik metot: tum dosyalar yazildiktan sonra 3 tur duzelmezse dur ──
            task.method_gaps = _method_gaps(task.files_written)
            _msig = frozenset(task.method_gaps)
            if not _msig or self._next_missing(task):
                task.method_streak = 0     # model once eksik dosyalari yazsin
            else:
                task.method_streak = task.method_streak + 1 if _msig == task.method_sig else 1
            task.method_sig = _msig
            if task.method_streak > _METHOD_STREAK_LIMIT:
                task.stop_reason = task.final_response = (
                    f"EKSİK METOT düzelmedi ({_METHOD_STREAK_LIMIT} tur üst üste): "
                    + "; ".join(_missing_methods(task.files_written))[:400] + " — görev durduruldu."
                )
                self._ui_progress(f"  ❌ {task.final_response}")
                break

            # ── pytest geri bildirimi: SMART EXIT adayi + test dosyasi varsa ──
            if self._smart_exit_candidate(task) and any(map(_is_test_file, task.files_written)):
                await asyncio.to_thread(self._sync_pytest, task)
            else:
                task.pytest_error, task.pytest_key = "", ()
            if task.pytest_error:
                last_error = task.pytest_error

            task.iterations = i + 1
            self._publish_live()      # tur basi
            self._ui_progress(f"  ⚙️ Iterasyon {task.iterations}/{self._max}")

            # ── LLM'e sorma ────────────────────────────────────
            prompt = self._build_prompt(task, steps, last_run_output, last_error, target_filename)
            _auto_missing = [f for f in task.auto_expected if f not in task.files_written]
            if _auto_missing:
                prompt += "\n\n═══ EKSİK MODÜL DOSYASI (import ediliyor ama YOK) ═══\n"
                for _am in _auto_missing:
                    prompt += f"  → {_am} eksik, yaz! (import eden dosya bu modülü bekliyor)\n"
            if task.ruff_problems:
                prompt += ("\n═══ RUFF: GERÇEK HATALAR (tanımsız ad / sözdizimi) — DÜZELTMEDEN "
                           "ACCEPT YOK; dosyanın TAM halini yaz ═══\n")
                prompt += "\n".join(task.ruff_problems.values())[:1500] + "\n"
            if task.method_gaps:
                prompt += ("\n═══ EKSİK METOT (başka dosyadaki sınıfta yok) — EKLEMEDEN "
                           "ACCEPT YOK; ilgili dosyanın TAM halini yaz ═══\n")
                for _p, _c, _m, _u, _ln in task.method_gaps[:5]:
                    prompt += (f"  → {_p} dosyasına {_c}.{_m} metodunu ekle "
                               f"({_u} satır {_ln} çağırıyor)\n")
            if task.import_problems:
                prompt += "\n═══ IMPORT DENEMESİ BAŞARISIZ (düzelt) ═══\n"
                prompt += "\n".join(f"  {p}" for p in task.import_problems[:5]) + "\n"
            _missing = [f for f in task.expected_files if f not in task.files_written]
            if _missing:
                prompt += "\n\n═══ KALAN DOSYALAR (HENÜZ YAZILMADI — HEMEN ŞİMDİ YAZ) ═══\n"
                for _mf in _missing:
                    prompt += f"  → {_mf}\n"
                if task.entry_required and _ENTRY_PLACEHOLDER in _missing:
                    prompt += (f"  ({_ENTRY_PLACEHOLDER}: GUI giriş dosyası — pencere/arayüz burada "
                               "kurulur, `if __name__ == \"__main__\":` ile başlatılır; "
                               "giriş dosyası olmadan görev BİTMEZ)\n")
            if task.files_written:
                _wl = list(task.files_written.keys())
                prompt += f"\n═══ YAZILAN: {_wl} ═══\n"
                prompt += f"═══ FARKLI BİR DOSYA YAZ! {_wl[-1]} TEKRAR YAZMA! ═══\n"
            raw = await asyncio.to_thread(self._model_fn, prompt)
            self._ui_progress(f"    🔍 RAW[:200]: {repr(raw[:200])}")
            decision = _parse_model_response(raw)

            if not decision:
                self._ui_progress("  ⚠️ JSON parse başarısız → accept")
                task.final_response = f"Kod oluşturuldu ({task.iterations} iterasyon). JSON parse hatası."
                break

            thought = decision.get("thought", "")
            action = decision.get("action", "unknown").lower()
            if action not in ("write", "fix", "accept", "run", "inspect", "error"):
                self._ui_progress(f"    ⚠️ Bilinmeyen action: {action} → atlanıyor")
            args = decision.get("args", {})
            response = decision.get("response", "")

            # ── ACTION: error (Gemini ve Ollama ikisi de yok) ──────
            # Kalan iterasyonlarda ayni basarisiz LLM cagrisini tekrarlamak
            # yerine hemen dur; asil sebep son mesajda kaybolmasin.
            if action == "error":
                llm_error = response or "HATA: Kullanılabilir LLM yok (Gemini/Ollama)"
                steps.append(CodingStep(step_num=i + 1, thought=thought, action="error",
                                        detail=llm_error, success=False))
                _llm_error_streak += 1
                # Tek bir gecici hata (Ollama mesgul/GPU dolu, ag kopmasi) tum
                # gorevi bitirmesin: 2 kez kisa bekleyip tekrar dene.
                if _llm_error_streak <= 2:
                    self._ui_progress(f"    ⚠️ {llm_error} — 10sn sonra tekrar denenecek ({_llm_error_streak}/2)")
                    await asyncio.sleep(10)
                    continue
                self._ui_progress(f"    ❌ {llm_error}")
                break
            _llm_error_streak = 0
            llm_error = None

            self._ui_progress(f"    🎯 action={action} file={args.get('filename','?')} thought={thought[:60]}")
            step = CodingStep(step_num=i + 1, thought=thought, action=action)

            # ── ZERO PROGRESS TRACKER: 3 boş iterasyon → zorla write prompt ──
            if not hasattr(task, '_zero_progress'):
                task._zero_progress = 0

            # ═══ SMART EXIT: tum dosyalar + compile + import + pytest temiz → accept ═══
            _all_clean = self._smart_exit_candidate(task) and not task.pytest_error
            if _all_clean and task.iterations >= 2:
                task.final_response = f"Tum dosyalar yazildi ve temiz: {sorted(task.files_written.keys())}"
                steps.append(CodingStep(step_num=i+1, thought="auto-accept: all files valid", action="accept", detail="SMART_EXIT", success=True))
                break


            # ── ZERO PROGRESS: 3 boş → zorla dosya yazdır ──
            if action not in ("write", "fix"):
                task._zero_progress += 1
            else:
                task._zero_progress = 0

            if task._zero_progress >= 3:
                self._ui_progress(f"  ⚠️ 3 boş iterasyon! Kalan dosyalar: {[f for f in task.expected_files if f not in task.files_written]}")

            # ── ACTION: write / fix ────────────────────────────
            if action in ("write", "fix"):
                _fn_target = args.get("filename", "")
                if _fn_target:
                    task.rewrite_counts[_fn_target] = task.rewrite_counts.get(_fn_target, 0) + 1
                    # Kilit yalnizca eksik dosya varken ve dosya temizken: ruff
                    # hatasi olan dosyanin duzeltilmesi engellenmez.
                    if (task.rewrite_counts[_fn_target] > 3 and _fn_target in task.files_written
                            and _fn_target not in task.ruff_problems
                            and all(g[0] != _fn_target for g in task.method_gaps)
                            and self._next_missing(task)):
                        last_error = await self._handle_lock(task, step, steps, _fn_target)
                        if task.stop_reason:
                            break
                        continue
                    task.lock_file, task.lock_streak = "", 0
            if action in ("write", "fix"):
                filename = args.get("filename") or target_filename or f"main.{_lang_ext(task.language)}"
                content = args.get("content", "")
                if isinstance(content, str):
                    content = _strip_wrapping_fence(filename, content)

                # Ayni dosya ayni icerikle art arda yaziliyorsa model dongude:
                # kalan turlari (kotayi) yakmadan sebebiyle birlikte bitir.
                _wk = f"{filename}\0{hash(content)}"
                if _wk == task.same_write_key:
                    task.same_write_count += 1
                else:
                    task.same_write_key, task.same_write_count = _wk, 1
                if task.same_write_count >= _SAME_WRITE_LIMIT:
                    _reason = (last_error or task.pytest_error
                               or (task.import_problems[0] if task.import_problems else "")
                               or "son hata kaydı yok")
                    task.stop_reason = (f"döngü: {filename} aynı içerikle tekrar yazılıyor, "
                                        f"sebep: {_reason[:400]}")
                    step.detail = f"🔁 DÖNGÜ ({_SAME_WRITE_LIMIT}x aynı içerik): {filename}"
                    step.success = False
                    steps.append(step)
                    self._ui_progress(f"  ❌ {task.stop_reason}")
                    break

                # Same file write tracking + force rotate
                if filename == task.last_written_file:
                    task.same_file_writes += 1
                    if task.same_file_writes >= 3:
                        task.same_file_writes = 0
                        # Force next unwritten file
                        for exp_f in task.expected_files:
                            if exp_f not in task.files_written:
                                filename = exp_f
                                last_error = f"ZORLA ROTATE: {task.last_written_file} dosyasina 3 kez yazdin. Simdi MUTLAKA {filename} yaz."
                                step.detail = f"ROTATE -> {filename}"
                                break
                else:
                    task.same_file_writes = 0
                task.last_written_file = filename

                # __init__.py gibi paket isaretleyicileri bos/kisa olabilir;
                # 200 karakter kurali onlari sonsuza kadar reddediyordu.
                _min_len = 0 if Path(filename).name == "__init__.py" else 200
                if (not content and _min_len) or len(content.strip()) < _min_len:
                    last_error = f"CONTENT_TOO_SHORT ({filename}): {len(content)} char yazdin. En az {_min_len} karakter dolu icerik yaz."
                    if self._reject(task, step, steps, filename, "COK_KISA", content,
                                    f"çok kısa ({len(content.strip())} karakter, en az {_min_len}) — "
                                    "dosyanın tam ve dolu halini yaz"):
                        break
                    continue

                # Dogrulama dosya TURUNE gore: eskiden README.md dahil her
                # dosya Python olarak derleniyordu -> Markdown hep SYNTAX_ERROR
                # aliyor, model hatayi calc.py'de sanip donguye giriyordu.
                valid, val_msg = _validate_file(filename, content, task.language)
                if not valid:
                    task.errors.append(f"Iteration {i+1}: {filename}: {val_msg}")
                    last_error = f"VALIDATION ({filename}): {val_msg}"
                    if _EVAL_EXEC_MSG in val_msg:
                        # Ilk retde kisa, sonrakilerde tam (Calculator.calculate) ornek.
                        _n = task.eval_rejects[filename] = task.eval_rejects.get(filename, 0) + 1
                        last_error += "\n" + _safe_eval_hint(full=_n >= 2)
                    if self._reject(task, step, steps, filename, "VALIDATION", content, val_msg):
                        break
                    continue

                fpath = _contained_path(task.project_path, filename)
                if fpath is None or fpath.name.startswith(_REJECTED_PREFIX):
                    task.errors.append(f"Iteration {i+1}: {filename}: proje dizini dışı")
                    last_error = (f"PATH_REJECTED ({filename}): dosya adi proje dizini "
                                  "icinde GORELI bir yol olmali (mutlak yol ve '..' yasak).")
                    if self._reject(task, step, steps, filename, "PATH", content,
                                    "proje dizini dışı yol — proje içinde göreli bir dosya adı kullan"):
                        break
                    continue
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
                # Stuck detection: same content repeatedly
                _ch = str(hash(content))[:8]
                if _ch == task.last_content_hash:
                    task.stuck_count += 1
                    if task.stuck_count >= 3:
                        last_error = f"STUCK: Ayni kodu 3 kez urettin! DAHA FAZLA KOD, daha detayli yaz. {filename} icin en az 300 karakter dolu fonksiyon yaz."
                        step.detail = f"🔄 STUCK ({task.stuck_count}x ayni icerik) — DETAYLI yazmalisin"
                        step.success = False
                        steps.append(step)
                        continue
                else:
                    task.stuck_count = 0
                task.last_content_hash = _ch

                # Dosya basariyla yazildi: ona ait eski hata mesajini temizle.
                # Eskiden "VALIDATION (README.md)" gibi bir mesaj dosya duzgun
                # yazildiktan sonra da kaliyor, model her turda ayni dosyayi
                # yeniden "duzeltiyordu". ZORLA ROTATE talimati korunur.
                if last_error and not last_error.startswith("ZORLA ROTATE"):
                    last_error = ""

                # ── RUFF ön-düzeltme (LLM'siz, kotasız) ──
                # Basit hatalari (kullanilmayan import, bicim) ruff duzeltir;
                # LLM'e sadece ruff'in duzeltemedigi gercek hatalar gider.
                _fixed, _ruff_left = await asyncio.to_thread(_ruff_autofix, fpath)
                if _fixed is not None:
                    if _fixed != content:
                        self._ui_progress(f"    🧹 ruff: {filename} otomatik düzeltildi")
                    content = _fixed
                if _ruff_left:
                    task.ruff_problems[filename] = _ruff_left
                else:
                    task.ruff_problems.pop(filename, None)
                task.files_written[filename] = content
                if _ruff_left:
                    last_error = (
                        f"RUFF ({filename}) kalan hatalar — SADECE bunlari duzelt, "
                        f"dosyanin TAM halini yaz:\n{_ruff_left}"
                    )
                    self._ui_progress(f"    ⚠️ ruff: {filename} içinde {len(_ruff_left.splitlines())} hata kaldı")
                    # Dosya diskte kalir ama engellenir (accept/SMART EXIT yok).
                    _first = _ruff_left.splitlines()[0]
                    if self._reject(task, step, steps, filename, "RUFF", content,
                                    f"ruff: {_first} — bu hatayı düzeltip dosyanın tam halini yaz"):
                        break
                    continue
                if last_error.startswith(f"RUFF ({filename})"):
                    last_error = ""

                task.reject_key, task.reject_count = (), 0
                step.detail = f"📝 {filename} ({len(content)} chars)"
                step.success = True
                steps.append(step)
                self._ui_progress(f"    📝 {filename} yazıldı ({len(content):,} chars)")

            # ── ACTION: run ────────────────────────────────────
            elif action == "run":
                cmd_target = args.get("command", "") or args.get("filename", "")
                if cmd_target:
                    fpath = _contained_path(task.project_path, cmd_target.split()[-1])
                    if fpath is None:
                        last_error = f"PATH_REJECTED ({cmd_target}): sadece proje icindeki dosyalar calistirilabilir."
                        step.detail = f"REDDEDİLDİ: proje dışı yol {cmd_target}"
                        step.success = False
                        steps.append(step)
                    elif fpath.exists():
                        _fsz = fpath.stat().st_size
                        if _fsz < 200:
                            last_error = f"KUCUK DOSYA: {fpath.name} sadece {_fsz} bytes. Daha fazla kod yaz — fonksiyonlar, class, mantik ekle!"
                        last_run_output = await asyncio.to_thread(_run_file, fpath, root=task.project_path)
                        last_error = ""
                        step.detail = last_run_output[:200]
                        step.success = "[SUCCESS]" in last_run_output
                        steps.append(step)
                        self._ui_progress(f"    ▶️  {fpath.name}: {'✅' if step.success else '❌'}")
                    else:
                        step.detail = f"Dosya yok: {cmd_target}"
                        step.success = False
                        steps.append(step)
                else:
                    step.detail = "Çalıştırılacak dosya belirtilmedi"
                    step.success = False
                    steps.append(step)

            # ── ACTION: inspect ────────────────────────────────
            elif action == "inspect":
                target = args.get("filename", "")
                # Okunan icerik LLM'e (bulut) gider: proje disi okuma = veri sizintisi.
                fpath = _contained_path(task.project_path, target)
                # Reddedilen icerik kopyalari yalnizca hata ayiklama icindir: model goremez.
                if fpath is not None and fpath.is_file() and not fpath.name.startswith(_REJECTED_PREFIX):
                    content = fpath.read_text(encoding="utf-8")
                    last_run_output = f"FILE: {target}\n{content[:_MAX_OUTPUT_CHARS]}"
                    step.detail = f"👁️ {target} okundu"
                else:
                    listing = [p.name for p in task.project_path.iterdir()
                               if not p.name.startswith(_REJECTED_PREFIX)]
                    last_run_output = f"DIR: {task.project_path}\nFiles: {listing}"
                    step.detail = "👁️ Dizin listelendi"
                step.success = True
                steps.append(step)

            # ── ACTION: accept ─────────────────────────────────
            elif action == "accept":
                _ok, _vp = await asyncio.to_thread(_verify_project, task, run_pytest=False)
                if not _ok:
                    last_error = "ACCEPT_REDDEDILDI: " + "; ".join(_vp[:4])
                    step.detail = f"❌ ACCEPT REJECTED ({len(_vp)} sorun)"
                    step.success = False
                    steps.append(step)
                    self._ui_progress(f"    ❌ ACCEPT REDDEDILDI ({len(_vp)} sorun): {_vp[0][:80] if _vp else ''}")
                    continue
                step.detail = f"✅ ACCEPT: {response[:100]}"
                step.success = True
                steps.append(step)
                task.final_response = response or (
                    f"Tamamlandı. {len(task.files_written)} dosya yazıldı, "
                    f"{task.iterations} iterasyon."
                )
                break

            else:
                step.detail = f"Bilinmeyen action: {action}"
                step.success = False
                steps.append(step)

        if llm_error and not task.files_written:
            return (f"❌ {llm_error} — {task.iterations}. iterasyonda durduruldu, hiçbir dosya yazılmadı.\n"
                    + self._result_block(task))

        # FINAL QUALITY GATE — accepted HERE only
        self._sync_entry(task)
        _ok, _vp = await asyncio.to_thread(_verify_project, task, run_pytest=True)
        task.accepted = _ok and not task.stop_reason
        if not task.missing_modules:
            await asyncio.to_thread(self._sync_imports, task)
            task.missing_modules = list(dict.fromkeys(
                task.import_missing
                + [f[:-3].replace("/", ".") for f in task.auto_expected if f not in task.files_written]))
        if not task.accepted:
            task.errors.extend(_vp[:5])
            _cause = f"LLM'e ulaşılamadı ({llm_error}) — " if llm_error else ""
            _stop = f"{task.stop_reason} " if task.stop_reason else ""
            task.final_response = (
                f"{_stop}{_cause}DOGRULAMA BASARISIZ ({len(_vp)} sorun): "
                + "; ".join(_vp[:5])
            )
        elif not task.final_response:
            task.final_response = f"Tamamlandi. {len(task.files_written)} dosya yazildi."

        summary = self._build_summary(task, steps)
        self._ui_progress(f"  🏁 Tamamlandı: {len(task.files_written)} dosya, {task.iterations} iterasyon")
        try:
            import sys as _s
            _s.path.insert(0, str(Path(__file__).parent.parent / "src"))
            from jarvis.path_utils import register_project
            register_project(name=task.project_path.name, root=task.project_path, entry=self._entry_file(task) or "main.py", status="accepted" if task.accepted else "needs_fix")
        except Exception:
            pass
        return summary

    # ── Import senkronu ────────────────────────────────────────

    @staticmethod
    def _sync_imports(task: CodingTask) -> None:
        """Yerel import'lardan eksik dosyalari plana ekler (artik import
        edilmeyenleri cikarir) ve dosyalar degistiyse gercek import denemesini
        yeniler. Ayni ModuleNotFoundError seti her turda mnf_streak'i artirir."""
        needed = _missing_local_modules(task.files_written, task.project_path)
        for name in [f for f in task.auto_expected if f not in needed and f not in task.files_written]:
            task.auto_expected.remove(name)
            if name in task.expected_files:
                task.expected_files.remove(name)
        for name in needed:
            if name not in task.expected_files:
                task.expected_files.append(name)
                task.auto_expected.append(name)

        key = tuple(sorted((k, hash(v)) for k, v in task.files_written.items() if k.endswith(".py")))
        previous = list(task.import_missing)
        if key != task.import_check_key:
            task.import_check_key = key
            task.import_problems, task.import_missing = (
                _import_problems(task.files_written, task.project_path) if key else ([], []))
        if task.import_missing and task.import_missing == previous:
            task.mnf_streak += 1
        else:
            task.mnf_streak = 1 if task.import_missing else 0

    @staticmethod
    def _sync_entry(task: CodingTask) -> None:
        """GUI gorevinde baska bir giris dosyasi (app.py, pencereyi acan
        calistirilabilir dosya) yazildiysa bekleyen main.py plandan cikar;
        yoksa (geri) eklenir."""
        if not task.entry_required:
            return
        ph = _ENTRY_PLACEHOLDER
        if _gui_entry_files(task.files_written):
            if ph in task.expected_files and ph not in task.files_written:
                task.expected_files.remove(ph)
        elif ph not in task.expected_files:
            at = task.expected_files.index("README.md") if "README.md" in task.expected_files else len(task.expected_files)
            task.expected_files.insert(at, ph)

    def _reject(self, task: CodingTask, step: CodingStep, steps: list[CodingStep],
                filename: str, category: str, content: str, reason: str) -> bool:
        """Reddi ekrana yazar, son reddedilen icerigi proje klasorune
        _reddedilen_<dosya>.txt olarak kaydeder ve ayni dosya + ayni kategori
        ust uste _REJECT_LIMIT kez reddedildiyse gorevi durdurur (True)."""
        reason = " ".join(str(reason).split())
        step.detail = f"REDDEDİLDİ ({category}): {reason[:150]}"
        step.success = False
        steps.append(step)
        self._ui_progress(f"    ⛔ {filename} reddedildi: {reason[:150]}")
        try:
            (task.project_path / _rejected_copy_name(filename)).write_text(content or "", encoding="utf-8")
        except OSError as e:
            logger.debug(f"[Coder] reddedilen içerik kaydedilemedi: {type(e).__name__}")
        key = (filename, category)
        task.reject_total += 1
        task.reject_count = task.reject_count + 1 if key == task.reject_key else 1
        task.reject_key = key
        if task.reject_count < _REJECT_LIMIT:
            return False
        task.stop_reason = (f"döngü: {filename} {task.reject_count} kez reddedildi, "
                            f"son sebep: {reason[:300]}")
        self._ui_progress(f"  ❌ {task.stop_reason}")
        return True

    # ── Kilitli dosya: gorunur ret + deterministik ilerleme ──────

    @staticmethod
    def _next_missing(task: CodingTask) -> str | None:
        """Plandaki (expected_files + auto_expected) henuz yazilmamis ilk dosya."""
        for name in dict.fromkeys(task.expected_files + task.auto_expected):
            if name not in task.files_written:
                return name
        return None

    async def _handle_lock(self, task: CodingTask, step: CodingStep,
                           steps: list[CodingStep], filename: str) -> str:
        """Kilitli dosyaya yazma denemesi: ekrana ve modele sonraki eksik
        dosyayi soyler. Ust uste _LOCK_STREAK_LIMIT kilitte uretilebilir eksik
        dosyalari LLM'siz yazar; toplam _LOCK_TOTAL_LIMIT asilirsa stop_reason
        koyar. Donus: modele gidecek last_error."""
        task.lock_total += 1
        task.lock_streak = task.lock_streak + 1 if task.lock_file == filename else 1
        task.lock_file = filename
        nxt = self._next_missing(task)
        step.detail = f"KİLİTLİ: {filename} zaten yazıldı ({task.lock_total}. kilit)"
        step.success = False
        steps.append(step)
        self._ui_progress(f"    ⛔ {filename} kilitli (zaten yazıldı), şimdi {nxt} yaz")
        if task.lock_total > _LOCK_TOTAL_LIMIT:
            task.stop_reason = f"döngü: {filename} kilitli, model {nxt}'ya geçmedi"
            self._ui_progress(f"  ❌ {task.stop_reason}")
            return ""
        if task.lock_streak >= _LOCK_STREAK_LIMIT and nxt in _GENERATED_FILES:
            await self._generate_missing(task)
            task.lock_file, task.lock_streak = "", 0
            nxt = self._next_missing(task)
        if nxt is None:
            return f"{filename} TAMAM ve kilitli, TEKRAR YAZMA. Tüm dosyalar yazıldı: şimdi accept yap."
        return f"{filename} TAMAM ve kilitli, TEKRAR YAZMA. Şimdi SADECE {nxt} yaz."

    async def _generate_missing(self, task: CodingTask) -> None:
        """Siradaki eksik dosya(lar) README.md / tests/ ise onlari LLM'siz
        (test_core.py icin dar bir prompt'la) yazar; zincir uretilemeyen
        ilk dosyada durur."""
        while (name := self._next_missing(task)) in _GENERATED_FILES:
            if name == "README.md":
                content = self._readme_template(task)
            elif name == "tests/__init__.py":
                content = ""
            else:
                content = await self._write_test_core(task)
                if content is None:
                    self._skip_tests(task)
                    continue
            if not await asyncio.to_thread(self._store_generated, task, name, content):
                return

    def _store_generated(self, task: CodingTask, name: str, content: str) -> bool:
        fpath = _contained_path(task.project_path, name)
        if fpath is None:
            return False
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        task.files_written[name] = content
        self._ui_progress(f"    🤖 {name} otomatik yazıldı (LLM'siz, {len(content):,} chars)")
        return True

    def _readme_template(self, task: CodingTask) -> str:
        files = [f for f in dict.fromkeys(task.expected_files + list(task.files_written))
                 if f != "README.md"]
        entry = self._entry_file(task) or next((f for f in files if f.endswith(".py")), None)
        lines = [
            f"# {task.project_path.name}",
            "",
            task.description.strip(),
            "",
            "Bu README, model yazmadığı için Jarvis agentic coder tarafından şablondan",
            "otomatik üretildi. Aşağıda projedeki dosyalar ve çalıştırma komutu yer alır.",
            "",
            "## Dosyalar",
            "",
            *(f"- `{f}`" for f in files),
            "",
            "## Çalıştırma",
            "",
            "```bash",
            f"cd {task.project_path}",
            f"python3 {entry}" if entry else "# çalıştırılabilir giriş dosyası yok",
            "```",
        ]
        if any(f.startswith("tests/") or _is_test_file(f) for f in files):
            lines += ["", "## Testler", "", "```bash", "python3 -m pytest -q", "```"]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _module_api(task: CodingTask) -> list[str]:
        """Yazilmis (test disi) modullerin adi ve ust duzey public adlari."""
        out = []
        for fn, src in task.files_written.items():
            if not fn.endswith(".py") or _is_test_file(fn) or Path(fn).name == "__init__.py":
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            names = [n.name for n in tree.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and not n.name.startswith("_")]
            out.append(f"  - {fn[:-3].replace('/', '.')}: {', '.join(names) or '(public ad yok)'}")
        return out

    async def _write_test_core(self, task: CodingTask) -> str | None:
        """tests/test_core.py icin modele DAR bir prompt verir (sadece o dosya +
        modul adlari). _TEST_CORE_ATTEMPTS denemede gecerli, ruff-temiz ve
        pytest'ten gecen icerik gelmezse None."""
        name = "tests/test_core.py"
        error = ""
        for _ in range(_TEST_CORE_ATTEMPTS):
            prompt = "\n".join([
                f"SADECE {name} dosyasını yaz (başka dosya YOK). pytest testleri olsun.",
                "Proje kökündeki modüller (import adı: ad listesi):",
                *self._module_api(task),
                "Kurallar: modülleri proje kökünden import et (ör. `from calc import topla`), "
                "en az 2 `def test_...` ve gerçek assert yaz; input()/GUI penceresi açma.",
                *([f"Önceki deneme reddedildi: {error[:600]}"] if error else []),
                'SADECE JSON: {"action": "write", "args": {"filename": "' + name
                + '", "content": "<dosyanın tam içeriği>"}}',
            ])
            raw = await asyncio.to_thread(self._model_fn, prompt)
            args = (_parse_model_response(raw) or {}).get("args") or {}
            content = args.get("content") if isinstance(args, dict) else None
            if not isinstance(content, str) or "def test_" not in content:
                error = "yanıtta `def test_...` içeren content yok"
                continue
            content = _strip_wrapping_fence(name, content)
            ok, msg = _validate_file(name, content, task.language)
            if not ok:
                error = msg
                continue
            fpath = task.project_path / name
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content, encoding="utf-8")
            fixed, ruff_left = await asyncio.to_thread(_ruff_autofix, fpath)
            content = fixed if fixed is not None else content
            error = (f"ruff: {ruff_left}" if ruff_left
                     else await asyncio.to_thread(_run_project_pytest, task.project_path))
            if not error:
                return content
            fpath.unlink(missing_ok=True)
        return None

    def _skip_tests(self, task: CodingTask) -> None:
        """test_core.py yazilamadi: plandan cikar; bizim urettigimiz bos
        tests/__init__.py de kalkar (aksi halde 'test dosyasinda def test_ yok')."""
        for name in ("tests/test_core.py", "tests/__init__.py"):
            if name == "tests/__init__.py" and task.files_written.get(name, "").strip():
                continue
            for lst in (task.expected_files, task.auto_expected):
                if name in lst:
                    lst.remove(name)
            if name in task.files_written:
                del task.files_written[name]
                (task.project_path / name).unlink(missing_ok=True)
        task.tests_skipped = True
        note = "tests/test_core.py yazılamadı, atlandı"
        task.notes.append(note)
        self._ui_progress(f"    ⚠️ {note}")

    @staticmethod
    def _smart_exit_candidate(task: CodingTask) -> bool:
        """Tum gerekli dosyalar yazildi, .py'ler derleniyor, import temiz."""
        _enforce = {'README.md'}
        _has_test = any('test' in f.lower() for f in task.files_written)
        _planned = [f for f in task.expected_files if f not in task.auto_expected
                    and not (task.entry_required and f == _ENTRY_PLACEHOLDER)]
        if not _has_test and len(_planned) >= 3 and not task.tests_skipped:
            _enforce.add('tests/__init__.py')
        if not (set(task.expected_files) | _enforce).issubset(task.files_written):
            return False
        if task.import_problems or task.ruff_problems or _method_gaps(task.files_written):
            return False
        for _fn, _fc in task.files_written.items():
            # Sadece .py derlenir; README.md'yi derlemek SMART EXIT'i
            # hic tetiklenmez yapiyordu (25 iterasyonun hepsi harcaniyordu).
            if not _fn.endswith(".py"):
                continue
            try:
                compile(_fc, _fn, "exec")
            except SyntaxError:
                return False
        return True

    @staticmethod
    def _sync_pytest(task: CodingTask) -> None:
        """Dosyalar degistiyse proje testlerini yeniden calistirir."""
        key = tuple(sorted((k, hash(v)) for k, v in task.files_written.items()))
        if key == task.pytest_key:
            return
        task.pytest_key = key
        task.pytest_error = _run_project_pytest(task.project_path)

    # ── Prompt Builder ─────────────────────────────────────────

    def _build_prompt(
        self,
        task: CodingTask,
        steps: list[CodingStep],
        last_run_output: str,
        last_error: str,
        target_filename: str | None = None,
    ) -> str:
        """Durum → LLM prompt."""
        parts = [_SYSTEM_PROMPT]

        parts.append(f"\n## GÖREV\n{task.description}")
        parts.append(f"\n## DİL: {task.language}")
        parts.append(f"## PROJE YOLU: {task.project_path}")
        if target_filename:
            parts.append(f"## ANA DOSYA: {target_filename}")

        if task.files_written:
            parts.append("\n## YAZILAN DOSYALAR:")
            for fname in list(task.files_written.keys()):
                parts.append(f"- {fname}")

        if steps:
            recent = steps[-5:]  # Son 5 adım
            parts.append("\n## SON ADIMLAR:")
            for s in recent:
                parts.append(f"  {s.step_num}. [{s.action}] {s.thought[:80]} → {s.detail[:80]} ({'OK' if s.success else 'FAIL'})")

        if last_run_output:
            parts.append(f"\n## SON ÇALIŞTIRMA ÇIKTISI:\n{last_run_output[:1500]}")

        if last_error:
            parts.append(f"\n## HATA:\n{last_error}")

        _missing = [f for f in task.expected_files if f not in task.files_written]
        _written_names = list(task.files_written.keys())
        parts.append("\n## DOSYA PLANI (TUMU yazilmali!)")
        parts.append(f"  Yazilan: {_written_names if _written_names else 'hicbir sey yok'}")
        parts.append(f"  EKSIK: {_missing if _missing else 'tumu yazildi!'}")
        if _missing:
            parts.append(f"  -> SIMDI MUTLAKA {_missing[0]} dosyasini yaz. AYNI dosyaya tekrar yazma!")
            parts.append("  -> TUM dosyalar bitmeden ACCEPT yapma!")
        else:
            parts.append("  -> Tum dosyalar yazildi. Run et, test et, sonra ACCEPT.")
        _wr = list(task.files_written.keys())
        if _wr:
            _lc = task.files_written[_wr[-1]]
            parts.append(_lc[:300])
            parts.append('ONCEKI DOSYA YUKARIDA. AYNISINI TEKRAR YAZMA!')
            parts.append(f'\n## DOSYA {fname} = {_fn_role_hints(fname, task)}')
            parts.append('SIMDI MUTLAKA FARKLI dosya yaz.')
        parts.append("\n## SIMDI NE YAPMALISIN?")
        if not task.files_written:
            parts.append("→ İlk dosyayı yaz (action: write)")
        elif last_error:
            parts.append("→ Hatayı düzelt (action: fix) veya yeniden yaz (action: write)")
        elif not last_run_output:
            parts.append("→ Çalıştır (action: run)")
        elif "[SUCCESS]" in last_run_output:
            parts.append("→ Çalışıyor! Accept yap (action: accept) veya başka dosya gerekiyorsa yaz")
        else:
            parts.append("→ Hata var → düzelt (action: fix)")

        parts.append('\nSADECE JSON çıktısı ver.')

        return "\n".join(parts)

    # ── Summary ────────────────────────────────────────────────

    @staticmethod
    def _entry_file(task: CodingTask) -> str | None:
        """Gercek giris dosyasi: main.py > cli.py > app.py > calistirilabilir ilk
        test-disi .py. Eskiden ozet her projede 'python3 main.py' diyordu (main.py
        olmasa da); sonra yalnizca modul olan calculator.py'yi (GUI/__main__ yok)
        giris sayiyordu."""
        py_files = [f for f in task.files_written if f.endswith(".py")]
        for preferred in ("main.py", "cli.py", "app.py"):
            if preferred in py_files:
                return preferred
        for f in py_files:
            name = Path(f).name
            if (not name.startswith("test_") and name != "__init__.py" and "/" not in f
                    and _is_runnable(f, task.files_written[f])):
                return f
        return None

    @staticmethod
    def _result_block(task: CodingTask) -> str:
        """Sonucun basindaki sabit blok: durum, proje klasoru, eksik modul.
        main.py arka plan sonucunu 3000 karakterde kestigi icin en ustte durur."""
        lines = [
            "--- SONUÇ ---",
            f"Durum: {'BAŞARILI' if task.accepted else 'BAŞARISIZ'}",
            f"Proje klasörü: {task.project_path}",
        ]
        missing = [f for f in task.expected_files
                   if f not in task.files_written and not (task.project_path / f).is_file()]
        if missing:
            lines.append(f"Eksik dosya: {', '.join(missing)}")
        if task.missing_modules:
            lines.append(f"Eksik modül: {', '.join(task.missing_modules)}")
        _py = [f for f in task.files_written if f.endswith(".py") and not _is_test_file(f)
               and Path(f).name != "__init__.py"]
        if _py and AgenticCoder._entry_file(task) is None:
            lines.append(f"Not: çalıştırılabilir giriş dosyası yok ({', '.join(_py)} yalnızca "
                         "modül; GUI ya da __main__ kodu içermiyor)")
        lines.extend(task.notes)
        return "\n".join(lines)

    def _build_summary(self, task: CodingTask, steps: list[CodingStep]) -> str:
        _entry = self._entry_file(task)
        _run_line = (
            f"▶️ Çalıştır: cd {task.project_path} && python3 {_entry}"
            if _entry else "▶️ Çalıştır: (çalıştırılabilir giriş dosyası yok)"
        )
        lines = [
            self._result_block(task),
            "",
            f"🔧 AGENTIC CODING — {task.description}",
            f"📂 Konum: {task.project_path}",
            f"📊 Durum: {'tamamlandi' if task.accepted else 'hatali'}",
            _run_line,
            f"📊 {task.iterations} iterasyon, {len(task.files_written)} dosya, "
            f"{len(task.errors)} hata, {'ACCEPTED ✅' if task.accepted else 'NOT ACCEPTED ⚠️'}",
            "",
        ]

        if task.files_written:
            lines.append("📝 Yazılan dosyalar:")
            for fname in task.files_written:
                fpath = task.project_path / fname
                size = fpath.stat().st_size if fpath.exists() else 0
                lines.append(f"  • {fname} ({size:,} bytes)")

        lines.append("")
        lines.append(f"💬 {task.final_response}")
        return "\n".join(lines)

    def _ui_progress(self, msg: str) -> None:
        """UI'a ilerleme yaz (opsiyonel)."""
        logger.info(msg)
        if self._ui:
            try:
                self._ui.write_log(msg)
            except Exception:
                pass


# ── Kolay Kullanım Fonksiyonu ────────────────────────────────

def _auto_project_dir(description: str) -> Path:
    """Her proje icin ayri alt klasor: ~/jarvis_programs/<slug>/"""
    import re
    from datetime import datetime
    base = Path.home() / "jarvis_programs"
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = (description or "").translate(tr).lower()
    stop = {"bir","ile","yaz","olsun","icin","ve","veya","dosya","dosyalardan",
            "dosyalari","kaliteli","calisan","kod","yazilsin","gelistirme",
            "the","for","and","with","app","application","create","build",
            "python","projesi","proje","uygulama","olustur","yap","tam"}
    words = [w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 3 and w not in stop]
    name = "_".join(words[:3]) if words else f"proje_{datetime.now().strftime('%Y%m%d_%H%M')}"
    target = base / name
    n = 2
    while target.exists() and any(target.iterdir()):
        target = base / f"{name}_{n}"
        n += 1
    return target


async def agentic_solve(
    description: str,
    language: str = "python",
    project_path: str | None = None,
    target_filename: str | None = None,
    ui: Any = None,
    model_fn: Callable[[str], str] | None = None,
) -> str:
    """
    Tek satırda agentic kodlama.

    Usage:
        result = await agentic_solve("Basit hesap makinesi yaz")
    """
    coder = AgenticCoder(model_fn=model_fn, ui=ui)
    return await coder.solve(
        description=description,
        language=language,
        project_path=project_path,
        target_filename=target_filename,
    )
