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
from dataclasses import dataclass, field
from pathlib import Path
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


def _uses_gui(files: dict) -> bool:
    for fn, src in files.items():
        if fn.endswith(".py") and any(m.split(".")[0] in _GUI_MODULES for m in _imported_modules(fn, src)):
            return True
    return False


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
_SAME_WRITE_LIMIT = 3


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
    for _mf in _missing_local_modules(task.files_written, task.project_path):
        problems.append(f"Eksik modül dosyası: {_mf} (import ediliyor ama yazılmadı)")
    _imp_problems, _ = _import_problems(task.files_written, task.project_path)
    problems.extend(_imp_problems)
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

def _validate_code(code: str, language: str = "python") -> tuple[bool, str]:
    """Kodda yasaklı kalıp var mı kontrol et."""
    for pattern, label in _FORBIDDEN_PATTERNS:
        if pattern.search(code):
            return False, f"YASAK: {label} bulundu"

    # Python syntax kontrolü
    if language == "python":
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"SYNTAX_ERROR: line {e.lineno}: {e.msg}"

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
             "--select", "E9,F63,F7,F82", str(path)],
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
        self._ui = ui
        self._max = max_iterations

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
        _tc_m = _re.search(r'(\d+)\s+(?:adet\s+)?test\b', description, _re.I)
        if _tc_m:
            task.min_test_count = int(_tc_m.group(1))
        self._ui_progress(f"  [PLAN] Beklenen dosyalar: {task.expected_files}")

        steps: list[CodingStep] = []
        last_run_output = ""
        last_error = ""

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
            # ── Import denetimi (LLM'siz): eksik yerel modul + gercek import ──
            await asyncio.to_thread(self._sync_imports, task)
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

            # ── pytest geri bildirimi: SMART EXIT adayi + test dosyasi varsa ──
            if self._smart_exit_candidate(task) and any(map(_is_test_file, task.files_written)):
                await asyncio.to_thread(self._sync_pytest, task)
            else:
                task.pytest_error, task.pytest_key = "", ()
            if task.pytest_error:
                last_error = task.pytest_error

            task.iterations = i + 1
            self._ui_progress(f"  ⚙️ Iterasyon {task.iterations}/{self._max}")

            # ── LLM'e sorma ────────────────────────────────────
            prompt = self._build_prompt(task, steps, last_run_output, last_error, target_filename)
            _auto_missing = [f for f in task.auto_expected if f not in task.files_written]
            if _auto_missing:
                prompt += "\n\n═══ EKSİK MODÜL DOSYASI (import ediliyor ama YOK) ═══\n"
                for _am in _auto_missing:
                    prompt += f"  → {_am} eksik, yaz! (import eden dosya bu modülü bekliyor)\n"
            if task.import_problems:
                prompt += "\n═══ IMPORT DENEMESİ BAŞARISIZ (düzelt) ═══\n"
                prompt += "\n".join(f"  {p}" for p in task.import_problems[:5]) + "\n"
            _missing = [f for f in task.expected_files if f not in task.files_written]
            if _missing:
                prompt += "\n\n═══ KALAN DOSYALAR (HENÜZ YAZILMADI — HEMEN ŞİMDİ YAZ) ═══\n"
                for _mf in _missing:
                    prompt += f"  → {_mf}\n"
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
                    if task.rewrite_counts[_fn_target] > 3 and _fn_target in task.files_written:
                        step.detail = f"SKIP: {_fn_target} zaten 3+ kez yazildi (locked)"
                        step.success = True
                        steps.append(step)
                        continue
            if action in ("write", "fix"):
                filename = args.get("filename") or target_filename or f"main.{_lang_ext(task.language)}"
                content = args.get("content", "")

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
                    step.detail = f"COK KISA ({filename}, {len(content)} char) — en az {_min_len} gerekli, REDDEDILDI"
                    step.success = False
                    steps.append(step)
                    last_error = f"CONTENT_TOO_SHORT ({filename}): {len(content)} char yazdin. En az {_min_len} karakter dolu icerik yaz."
                    continue

                # Dogrulama dosya TURUNE gore: eskiden README.md dahil her
                # dosya Python olarak derleniyordu -> Markdown hep SYNTAX_ERROR
                # aliyor, model hatayi calc.py'de sanip donguye giriyordu.
                valid, val_msg = _validate_file(filename, content, task.language)
                if not valid:
                    task.errors.append(f"Iteration {i+1}: {filename}: {val_msg}")
                    last_error = f"VALIDATION ({filename}): {val_msg}"
                    step.detail = f"REDDEDİLDİ: {val_msg}"
                    step.success = False
                    steps.append(step)
                    continue

                fpath = _contained_path(task.project_path, filename)
                if fpath is None:
                    task.errors.append(f"Iteration {i+1}: {filename}: proje dizini dışı")
                    last_error = (f"PATH_REJECTED ({filename}): dosya adi proje dizini "
                                  "icinde GORELI bir yol olmali (mutlak yol ve '..' yasak).")
                    step.detail = f"REDDEDİLDİ: proje dışı yol {filename}"
                    step.success = False
                    steps.append(step)
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
                    last_error = (
                        f"RUFF ({filename}) kalan hatalar — SADECE bunlari duzelt, "
                        f"dosyanin TAM halini yaz:\n{_ruff_left}"
                    )
                    self._ui_progress(f"    ⚠️ ruff: {filename} içinde {len(_ruff_left.splitlines())} hata kaldı")
                elif last_error.startswith(f"RUFF ({filename})"):
                    last_error = ""

                task.files_written[filename] = content
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
                if fpath is not None and fpath.is_file():
                    content = fpath.read_text(encoding="utf-8")
                    last_run_output = f"FILE: {target}\n{content[:_MAX_OUTPUT_CHARS]}"
                    step.detail = f"👁️ {target} okundu"
                else:
                    listing = [p.name for p in task.project_path.iterdir()]
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
    def _smart_exit_candidate(task: CodingTask) -> bool:
        """Tum gerekli dosyalar yazildi, .py'ler derleniyor, import temiz."""
        _enforce = {'README.md'}
        _has_test = any('test' in f.lower() for f in task.files_written)
        _planned = [f for f in task.expected_files if f not in task.auto_expected]
        if not _has_test and len(_planned) >= 3:
            _enforce.add('tests/__init__.py')
        if not (set(task.expected_files) | _enforce).issubset(task.files_written):
            return False
        if task.import_problems:
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
        """Gercek giris dosyasi: main.py > cli.py > app.py > ilk test-disi .py.
        Eskiden ozet her projede 'python3 main.py' diyordu (main.py olmasa da)."""
        py_files = [f for f in task.files_written if f.endswith(".py")]
        for preferred in ("main.py", "cli.py", "app.py"):
            if preferred in py_files:
                return preferred
        for f in py_files:
            name = Path(f).name
            if not name.startswith("test_") and name != "__init__.py" and "/" not in f:
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
        if task.missing_modules:
            lines.append(f"Eksik modül: {', '.join(task.missing_modules)}")
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
