import ast
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import typing
from pathlib import Path



def _find_existing_project(project_name: str) -> Path | None:
    """Ayni isimde mevcut proje var mi? Varsa o klasoru done."""
    base = Path.home() / "jarvis_programs"
    if not base.exists():
        return None
    name_lower = (project_name or "").lower().replace(" ", "_")[:30]
    for d in sorted(base.iterdir()):
        if d.is_dir() and name_lower[:10] in d.name.lower():
            return d
    return None

def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
# JARVIS_PROJECTS_DIR ile başka bir diske (ör. büyük HDD) taşınabilir; sistem
# diski dolmasın diye (canlı test 2026-09-28: 49 GB'lık kök bölüm doldu).
PROJECTS_DIR     = Path(os.environ.get("JARVIS_PROJECTS_DIR", "").strip() or Path.home() / "Desktop" / "JarvisProjects").expanduser()
MAX_FIX_ATTEMPTS = 5
MODEL_PLANNER    = "gemini-flash-latest"
MODEL_WRITER     = "gemini-flash-latest"

# Onay bekleyen (henuz baslatilmamis) dev_agent istekleri - file_controller.py'deki
# confirm_code deseniyle ayni mantik: ilk cagri hicbir sey kurmaz/calistirmaz, sadece
# bir kod doner; kullanici acikca onaylayip ayni kodla tekrar cagirilana kadar pip
# install / uretilen kodu calistirma adimlarina gecilmez.
_pending_dev_agent: dict[str, dict] = {}
# AYNI GOREV ICIN AYNI KOD: timeout sonrasi yeni kod uretme → loop kir
_task_to_code: dict[str, str] = {}  # desc[:80]|lang|project → code

# ONAY KAPISI, MODELE GUVENMEZ (2026-09-28, Windows canli testi): Gemini
# "Onaylıyor musunuz?" dedikten sonra kullanicinin cevabini beklemeden ayni
# turda confirm_code ile ikinci cagriyi yapti ve proje basladi. Artik main.py
# her kullanici mesajinda/konusmasinda note_user_turn() cagirir; confirm_code
# ancak kod VERILDIKTEN SONRA gercek bir kullanici turu geldiyse kabul edilir.
# None = izleme yok (testler/CLI gibi arayuzsuz kullanim) → eski davranis.
_last_user_turn_at: float | None = None
# Kullanicinin ISTEK cumlesinin gec gelen ses-yazi parcalari "onay" sayilmasin.
USER_TURN_GRACE_S = 1.5


def note_user_turn(now: float | None = None) -> None:
    """Kullanicidan gercek bir girdi (ses veya yazi) geldigini kaydeder."""
    global _last_user_turn_at
    _last_user_turn_at = time.monotonic() if now is None else now


def _user_confirmed_after(issued_at: float) -> bool:
    if _last_user_turn_at is None:
        return True
    return _last_user_turn_at >= issued_at + USER_TURN_GRACE_S


def confirmation_problem(confirm_code: str) -> str | None:
    """confirm_code kullanilabilir mi? Sorun varsa kullaniciya/modele donecek
    mesaj, yoksa None. (agent_board da is baslatmadan once bunu kullanir.)"""
    pending = _pending_dev_agent.get((confirm_code or "").strip())
    if pending is None:
        return "Onay kodu geçersiz veya süresi dolmuş. Aynı description ile yeniden çağır — sistem aynı görev için aynı kodu döndürecek."
    if not _user_confirmed_after(pending.get("issued_at", 0.0)):
        print("[DevAgent] ⛔ confirm_code kullanıcı cevap vermeden kullanıldı — reddedildi.")
        return (
            "ONAY HENÜZ ALINMADI — proje BAŞLATILMADI. Onay kodu verildikten sonra kullanıcıdan "
            "hiç cevap gelmedi. Kullanıcıya ne yapılacağını anlat ve SUS; kullanıcı açıkça "
            "'evet/onaylıyorum' dedikten SONRA aynı confirm_code ile tekrar çağır."
        )
    return None


def _get_api_key() -> str:
    from jarvis.core.secure_config import get_gemini_api_key
    return get_gemini_api_key()


def _ollama_options() -> dict:
    """KARARLILIK (canli test 2026-09-28: ayni kod 4/5 sonra 2/5 verdi).
    - num_ctx: Ollama'nin varsayilan baglam penceresi kucuk (2048-4096 token);
      cok dosyali duzeltme istemleri SESSIZCE kesiliyor, model dosyanin bir
      kismini hic gormeden 'duzeltiyor' ve ayni hatayi tekrarliyordu.
    - temperature: kod icin dusuk sicaklik daha tutarli sonuc verir.
    Ortam degiskenleriyle ayarlanabilir (JARVIS_OLLAMA_CTX, JARVIS_OLLAMA_TEMP)."""
    def _num(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, "") or default)
        except ValueError:
            return default
    return {"num_ctx": int(_num("JARVIS_OLLAMA_CTX", 8192)), "temperature": _num("JARVIS_OLLAMA_TEMP", 0.2)}


def _get_model(model_name: str):
    """Once yerel Ollama'yi (qwen2.5-coder) dener - Google Gemini kesintilerinde
    bile calisir. Ollama kapaliysa/kurulu degilse otomatik olarak Gemini'ye
    (orijinal davranis) duser."""
    import requests as _requests

    OLLAMA_URL = "http://localhost:11434/api/generate"
    # DUZELTME (2026-09-28): 7b model iki canli testte ici bos/yer tutucu kod
    # birakti (todo_app, CodeReviewProgram). Varsayilan 14b; kurulu degilse
    # 7b'ye duser. JARVIS_DEVAGENT_MODEL ile degistirilebilir.
    OLLAMA_MODEL = os.environ.get("JARVIS_DEVAGENT_MODEL", "").strip() or "qwen2.5-coder:14b"

    class _OllamaResponse:
        def __init__(self, text):
            self.text = text

    class _OllamaWrapper:
        def generate_content(self, contents):
            prompt = contents if isinstance(contents, str) else str(contents)
            resp = _requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                      "options": _ollama_options()},
                timeout=300,  # 14b model çok dosyalı istemlerde 120 sn'yi aşabiliyor
            )
            resp.raise_for_status()
            data = resp.json()
            return _OllamaResponse(data.get("response", ""))

    # Ollama gercekten calisiyor mu, hizli bir saglik kontrolu (1sn)
    # JARVIS_DEVAGENT_PREFER=gemini: yerel model yerine doğrudan Gemini (daha güçlü,
    # ama kota tüketir). Varsayılan: önce yerel Ollama.
    prefer_gemini = os.environ.get("JARVIS_DEVAGENT_PREFER", "").strip().lower() == "gemini"
    try:
        if prefer_gemini:
            raise RuntimeError("Gemini tercih edildi")
        tags = _requests.get("http://localhost:11434/api/tags", timeout=5).json()
        installed = {m.get("name", "") for m in tags.get("models", [])}
        if OLLAMA_MODEL not in installed and "qwen2.5-coder:7b" in installed and not os.environ.get("JARVIS_DEVAGENT_MODEL"):
            print(f"[DevAgent] ⚠️ {OLLAMA_MODEL} kurulu değil, qwen2.5-coder:7b kullanılıyor.")
            OLLAMA_MODEL = "qwen2.5-coder:7b"
        print(f"[DevAgent] Yerel Ollama kullaniliyor: {OLLAMA_MODEL} (Gemini'ye bagimli degil).")
        return _OllamaWrapper()
    except Exception:
        pass

    from google import genai
    from jarvis.actions.resilience import call_with_resilience, ModelFallbackNeeded, AllAttemptsFailed
    _c = genai.Client(api_key=_get_api_key())
    _breaker = _get_gemini_breaker()

    class _W:
        def generate_content(self, contents):
            try:
                return call_with_resilience(
                    lambda: _c.models.generate_content(model=model_name, contents=contents),
                    breaker=_breaker,
                )
            except ModelFallbackNeeded as e:
                print(f"[DevAgent] ⚠️ Model '{model_name}' kullanılamıyor (muhtemelen kaldırılmış): {e}")
                raise
            except AllAttemptsFailed as e:
                print(f"[DevAgent] ❌ Gemini'ye ulaşılamıyor, tüm denemeler başarısız: {e.last_error}")
                raise

    return _W()


_GEMINI_BREAKER = None


def _get_gemini_breaker():
    """Tek, paylasilan bir devre kesici - modul yeniden import edilse bile
    hata sayaci sifirlanmaz (process omru boyunca kalici)."""
    global _GEMINI_BREAKER
    if _GEMINI_BREAKER is None:
        from jarvis.actions.resilience import CircuitBreaker
        _GEMINI_BREAKER = CircuitBreaker(name="gemini-devagent", failure_threshold=3, cooldown_seconds=60)
    return _GEMINI_BREAKER


def _strip_fences(text: str) -> str:
    text = text.strip()
    # Modeller kod bloğundan SONRA sıkça açıklama ekler (örn. "### API anahtarı
    # nasıl alınır"). Sadece bastaki/sondaki tek tırnağı silmek, bu durumda
    # kapanış tırnağını ve arkasındaki Markdown metnini dosyada bırakıp
    # SyntaxError'a yol açıyordu. Bunun yerine, İLK tam kod bloğunu (açılış...
    # kapanış) bulup SADECE onu alıyoruz - öncesi/sonrası ne olursa olsun atılır.
    match = re.search(r"```[a-zA-Z]*\r?\n?(.*?)\r?\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Üçlü tırnak hiç bulunamadıysa (nadir durum), eski davranışa dön.
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_live_scrape(description: str) -> bool:
    from jarvis.actions.devkit.task_intake import has_url, needs_browser_or_web
    return has_url(description) and needs_browser_or_web(description)


# Tek başına başarıyı süresiz engellememesi gereken kalite bulguları
# (Go'daki LINT_BLOCKING_ROUNDS ile aynı fikir).
SOFT_QUALITY_CODES = frozenset({"UNUSED-DEFINITION"})
SOFT_QUALITY_ROUNDS = 1


def _acceptance_expectation_wrong(description: str, spec: dict, problems: list[str], output: str,
                                  program_text: str = "") -> tuple[bool, str]:
    """Hakem: kabul testindeki beklenti mi yanlış, program mı? Her hatada ve
    hakem kendi hesabıyla çelişkiliyse (False, ...) — şüphede program suçlu."""
    try:
        from jarvis.actions.devkit.acceptance import build_dispute_prompt, dispute_is_consistent, parse_dispute
        response = _get_model(MODEL_PLANNER).generate_content(
            build_dispute_prompt(description, spec, problems, output))
        wrong, reason, values = parse_dispute(response.text)
        if not wrong:
            return False, reason
        missing = [t for m in re.finditer(r"İÇERMİYOR: (\[.*?\])\.", "\n".join(problems))
                   for t in re.findall(r"'([^']*)'", m.group(1))]
        ok, why = dispute_is_consistent(values, program_text or output, missing)
        return (True, reason) if ok else (False, f"hakem kararı reddedildi: {why}")
    except Exception as e:  # noqa: BLE001
        return False, f"hakem çalışmadı: {type(e).__name__}"


def _plan_acceptance(description: str, plan: dict, log=print) -> "dict | None":
    """Kabul testi spesifikasyonu ister; her hatada None (build engellenmez)."""
    if _is_live_scrape(description):
        # Canlı test 2026-09-28: model gerçek siteye benzemeyen bir örnek sayfa
        # uydurdu; gerçek sitede DOĞRU çalışan program bu sayfada reddedildi.
        # Gerçek URL'li kazıma görevlerinde doğrulama gerçek çalıştırmanın
        # çıktısı üzerinden yapılır (beklenen çıktı + uydurma-veri kontrolü).
        log("ℹ️ Kabul testi uygulanmayacak: gerçek bir siteyi kazıma görevi — doğrulama gerçek çalıştırmanın çıktısıyla yapılacak.")
        return None
    try:
        from jarvis.actions.devkit.acceptance import build_prompt, parse_spec, validate_spec
        response = _get_model(MODEL_PLANNER).generate_content(build_prompt(description, plan))
        spec, reason = validate_spec(parse_spec(response.text))
    except Exception as e:  # noqa: BLE001
        log(f"ℹ️ Kabul testi hazırlanamadı ({type(e).__name__}); yalnızca kalite kapısı kullanılacak.")
        return None
    if spec is None:
        log(f"ℹ️ Kabul testi uygulanmayacak: {reason}")
        return None
    tokens = sum(len(e["contains"]) for e in spec["expect"])
    log(f"🧪 Kabul testi hazır: {len(spec['fixtures'])} örnek dosya, {tokens} beklenen ifade.")
    return spec


_SELENIUM_HINT = (
    "IMPORTANT: this project's plan uses PLAYWRIGHT, but the code uses SELENIUM. Selenium needs a system "
    "Chrome + chromedriver that is NOT available here (errors like 'DevToolsActivePort file doesn't exist' or "
    "'session not created' cannot be fixed by changing Selenium options). REWRITE the browser part with "
    "Playwright's sync API: from playwright.sync_api import sync_playwright; with sync_playwright() as p: "
    "browser = p.chromium.launch(headless=True); page = browser.new_page(); page.goto(url, timeout=...); "
    "page.mouse.wheel(0, 10000) / page.click(...) in a loop until enough items are loaded; "
    "html = page.content(). Remove every selenium import."
)


_STRPTIME_MISMATCH = re.compile(r"time data '([^']*)' does not match format '([^']*)'")


def _known_error_hint(output: str) -> str:
    """Sık görülen, modelin tekrar tekrar göremediği hatalar için kesin ipucu."""
    m = _STRPTIME_MISMATCH.search(output or "")
    if m:
        # Canlı test 2026-09-29 (hata_saatleri): satır boşluktan bölünüp YALNIZ
        # tarih parçası tam biçimle çözülmeye çalışıldı; 5 tur aynı hata.
        return (
            f"\n\nROOT CAUSE: datetime.strptime received the text {m.group(1)!r} but the format "
            f"{m.group(2)!r} expects more (or different) parts. The string you pass must contain EXACTLY what "
            f"the format describes. If the line looks like 'YYYY-MM-DD HH:MM:SS LEVEL msg', split once more "
            f"(parts = line.split(maxsplit=3); stamp = parts[0] + ' ' + parts[1]) or use line[:19] — or "
            f"simply read the hour as int(parts[1][:2]). Do not swallow this error with try/except."
        )
    return ""


def _bare_filename_hint(output: str, run_command: str) -> str:
    """'No such file or directory: 'a.jpg'' hatasında dosya aslında komut
    satırındaki girdi klasöründeyse: program TAM YOL yerine yalnızca dosya adını
    kullanıyor (os.listdir sonucu birleştirilmemiş). Canlı test 2026-09-29'da
    model bunu 5 denemede de göremedi."""
    names = set(re.findall(r"No such file or directory: '([^'/\\]+)'", output or ""))
    if not names:
        return ""
    for arg in shlex.split(run_command or "", posix=os.name != "nt")[1:]:
        d = Path(arg)
        try:
            if d.is_dir() and any((d / n).exists() or any(d.rglob(n)) for n in names):
                return (
                    f"\n\nROOT CAUSE: {sorted(names)} exist inside the input folder '{arg}', but the code opens/copies "
                    f"them by BARE FILE NAME, which is resolved against the current working directory. Use the full "
                    f"path: iterate with Path(src).iterdir()/rglob('*') and use those Path objects, or "
                    f"os.path.join(src_dir, name) — never the bare name from os.listdir()."
                )
        except OSError:
            continue
    return ""


def _selenium_instead_of_playwright(file_codes: dict[str, str], dependencies) -> dict[str, list[dict]]:
    """Plan Playwright istiyor ama kod Selenium import ediyorsa dosya başına bulgu."""
    deps = " ".join(str(d) for d in dependencies or []).casefold()
    if "playwright" not in deps:
        return {}
    found: dict[str, list[dict]] = {}
    # FIX: dosya uzantisini normalize et
    _norm_fc = {}
    for _fp, _fc in file_codes.items():
        if _fp.endswith('.python'):
            _fp = _fp[:-7] + '.py'
        elif not _fp.endswith(('.py', '.json', '.txt', '.md', '.html', '.css', '.js', '.yaml', '.yml', '.cfg', '.toml', '.xml')):
            _fp = _fp + '.py'
        _norm_fc[_fp] = _fc
    file_codes = _norm_fc

    for path, code in file_codes.items():
        for no, line in enumerate(code.splitlines(), 1):
            if re.match(r"\s*(from|import)\s+selenium\b", line):
                found.setdefault(path, []).append(
                    {"code": "USE-PLAYWRIGHT-NOT-SELENIUM", "message": _SELENIUM_HINT, "line": no, "col": 0}
                )
                break
    return found


def _recipes_for_fix(description: str, language: str) -> str:
    """Düzeltme istemine de usta şablonlarını ekler (hata olursa boş döner)."""
    try:
        from jarvis.actions.devkit.recipes import recipes_block
        return recipes_block(description, language)
    except Exception:  # noqa: BLE001
        return ""


def _python_quality_issues(file_codes: dict[str, str]) -> dict[str, list[dict]]:
    """Modelden bagimsiz kalite bulgulari; hata olursa build'i asla engellemez."""
    try:
        from jarvis.actions.devkit.python_quality import analyze_sources
        return analyze_sources(file_codes)
    except Exception as e:  # noqa: BLE001
        print(f"[DevAgent] ⚠️ Kalite kapısı çalıştırılamadı: {e}")
        return {}


def _devkit_generate(prompt: str) -> str:
    """devkit kuruculari icin LLM koprusu: _get_model'in sagladigi yerel
    Ollama -> Gemini (devre kesicili) zincirini aynen kullanir."""
    return _get_model(MODEL_WRITER).generate_content(prompt).text


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _undefined_name_note(output: str, file_codes: dict[str, str]) -> tuple[str, list[str]] | None:
    """'name X is not defined' hatasında X'in kullanıldığı yerleri bulur."""
    import ast as _ast
    names = sorted(set(re.findall(r"name '(\w+)' is not defined", output or "")))
    if not names:
        return None
    lines, files = [], []
    for name in names[:3]:
        for fp, code in file_codes.items():
            if not fp.endswith(".py"):
                continue
            try:
                tree = _ast.parse(code)
            except SyntaxError:
                continue
            used = sorted({n.lineno for n in _ast.walk(tree)
                           if isinstance(n, _ast.Name) and n.id == name and isinstance(n.ctx, _ast.Load)})
            if used:
                files.append(fp)
                lines.append(f"'{name}' is USED in {fp} at line(s) {used[:8]}")
    if not lines:
        return None
    return ("UNDEFINED NAME LOCATED (deterministic): " + "; ".join(lines) + ". At each of these places the "
            "name does not exist in that scope: define it there, pass it in as a parameter, or use the variable "
            "that actually holds that value. Do not hide errors with a broad try/except that only prints them.",
            files)


def _traceback_project_files(output: str, project_files: list[str]) -> list[str]:
    """Traceback'te geçen proje dosyaları (en derinden en dışa, tekrarsız)."""
    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+\d+', re.IGNORECASE)
    found: list[str] = []
    for raw_path in reversed(pattern.findall(output)):
        for pf in project_files:
            if (Path(pf).name == Path(raw_path).name or raw_path.endswith(pf)) and pf not in found:
                found.append(pf)
    return found


def _classify_error(output: str, project_dir: Path | None = None) -> str:

    low = output.lower()

    if "cannot import name" in low:
        # Paket EKSIK degil - modul var ama beklenen isim (sinif/fonksiyon)
        # onun icinde tanimli degil. pip install bunu asla cozemez, bu yuzden
        # "no module named" kontrolunden ONCE, ayri bir tur olarak yakalanmali.
        # (Asagidaki kontrol "importerror" gecen HER SEYI yakaladigi icin, bu
        # satir olmadan bu dal asla calismazdi.)
        return "import_error"

    if any(x in low for x in ("no module named", "modulenotfounderror", "importerror")):
        # "No module named X" HER ZAMAN eksik harici paket anlamina gelmez -
        # projenin KENDI dosyalarindan biri de olabilir (ornegin utils/helpers.py
        # var ama __init__.py eksik oldugu icin import calismiyor). Boyle
        # durumda pip install denemek bosuna zaman kaybettirir (5 deneme
        # boyunca ayni hatayi tekrar tekrar gorduren tam olarak bu bug'du).
        if project_dir is not None:
            match = re.search(r"No module named ['\"]([a-zA-Z0-9_\.]+)['\"]", output, re.IGNORECASE)
            if match:
                top_level = match.group(1).split(".")[0]
                if (project_dir / top_level).exists() or (project_dir / f"{top_level}.py").exists():
                    return "local_import_error"
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    # Yukaridaki kontroller UYAKALANMAMIS bir traceback'in kalibini (SinifAdi:
    # mesaj) arar. Ama kod kendi hatasini "except X as e: print(e)" ile
    # yakalayip YALNIZCA mesaji yazdirirsa (sinif adi hic gorunmez), yukaridaki
    # hicbir kalip eslesmez ve calisma yanlislikla "hatasiz" sayilir - proje
    # gercekte hicbir satir yazmamis olsa bile (ornek: sqlite3'e ham bir Python
    # list'i parametre olarak baglamaya calisan kod, "Error binding parameter
    # 2: type 'list' is not supported" diye yazdirir ve sessizce yutar; sinif
    # adi olan "sqlite3.Error"/"ProgrammingError" hic goze gorunmez). Bu genel
    # bir yedek: "error"/"exception" kelimesi baska hicbir ozel kaliba
    # uymadan, kendi basina (kelime siniri ile) gecerse yine de supheli sayilir.
    # Bilinen odun: "0 error bulundu" gibi zararsiz bir cikti da tetikleyebilir
    # - ama bu, zaten var olan kaba anahtar-kelime yaklasiminin ayni turden bir
    # riski, yeni degil; sessizce yutulan gercek bir hatayi kacirmak (mevcut
    # davranis) bundan daha pahaliya mal oluyor.
    if re.search(r"\b(error|exception)\b", low):
        return "runtime_error"

    return "none"


def _has_error(output: str, run_command: str) -> bool:
    
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    error_type = _classify_error(output)
    return error_type != "none"

class RateLimitError(Exception):
    pass


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.database"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"],
  "shared_data_contracts": [
    "Describe here any data structure passed BETWEEN files that don't necessarily import each other directly (e.g. a dict/object built in one file and consumed in another via a function argument, not an import). Example: 'An expense is a dict with keys: amount (float), category (str), date (str, YYYY-MM-DD) - used identically by the GUI, the database layer, and any chart/report code.'"
  ],
  "expected_outputs": [
    {{"path": "database.db", "description": "What a CORRECT result looks like inside this file after the app has genuinely worked (e.g. 'contains one row per scraped URL, with a non-empty title and paragraph')."}}
  ]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".
7. CRITICAL for correctness: if two or more files exchange a data structure (a dict, a class instance, a tuple shape) — even files that never import each other, because the data actually flows through a third file like main.py — describe its EXACT shape ONCE in "shared_data_contracts" (field names, types, whether it's a dict or a specific class). Every file that touches this data MUST use the identical shape. This is the most common source of real bugs: e.g. one file builds {{"amount": ..., "category": ...}} while another expects an object with .amount/.category attributes.
8. If running the entry point is supposed to durably create or update a file (a database, a report, an exported document, a log, a generated image, etc.), list each such file's relative path in "expected_outputs" with a one-line description of what a CORRECT result looks like inside it. If the result is a FOLDER TREE (files copied/moved/sorted into sub-folders), list that output folder's relative path (e.g. "sorted") instead of guessing individual file names. Leave this list EMPTY only for purely interactive/display-only programs that persist nothing (e.g. a calculator, a GUI that only shows numbers on screen). This is critical: a program can run to completion with NO Python error while silently producing nothing real (a network call that fails silently, a thread that never runs, wrong file path) — "expected_outputs" is what lets that be caught instead of wrongly reported as a success.
9. This is a completely standalone, independent project with NO relationship to any AI assistant framework. NEVER plan a file path or an import under a top-level name "jarvis" (e.g. "jarvis/core/engine.py", or importing "jarvis.anything") — that name does not exist for this project and is never a real requirement, no matter what the description mentions.
10. If the task needs content that appears only after JavaScript runs (infinite scroll, "load more" buttons, dynamic pages, "wait until the page is fully loaded"), plain HTTP clients (requests/httpx/urllib) are WRONG: use Playwright and list "playwright" in dependencies. If the task works on a web page, take the URL from the command line (sys.argv[1]) and put the real URL from the description into run_command.
11. ALWAYS include a "README.md" in the files list (describe setup, usage, architecture, test instructions). If the user requests tests OR the project is non-trivial (more than 2 files), also include a "tests/" directory with at least 3 test files covering core functionality. Tests use pytest format (functions named test_*).

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        plan = _validate_plan(json.loads(raw), description)
        from jarvis.actions.devkit.task_intake import BROWSER_RULE, needs_browser, plan_uses_browser
        if language.strip().lower() == "python" and needs_browser(description) and not plan_uses_browser(plan):
            print("[DevAgent] ⚠️ Görev JavaScript/kaydırma istiyor ama plan tarayıcı kullanmıyor — plan yeniden isteniyor.")
            response = model.generate_content(prompt + "\n\n" + BROWSER_RULE + "\nJSON:")
            plan = _validate_plan(json.loads(_strip_fences(response.text)), description)
            if not plan_uses_browser(plan):
                raise ValueError(
                    "görev JavaScript ile yüklenen içerik istiyor (kaydırma/'daha fazla yükle') ama planlayıcı "
                    "iki denemede de tarayıcı (Playwright) kullanmadı; requests ile bu içerik alınamaz."
                )
        return _ensure_url_in_run_command(plan, description)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}") from e
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e)) from e
        raise


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>()\[\]]+")


def _ensure_url_in_run_command(plan: dict, description: str) -> dict:
    """Aciklamada bir URL varsa ve run_command hic URL icermiyorsa ekler.
    (canli test 2026-09-28: plan 'python main.py' dedi, program 'Usage: python
    main.py <URL>' ile cikti; duzeltme dongusu run_command'i degistiremedigi
    icin 5 deneme bosa gitti.)"""
    urls = _URL_IN_TEXT.findall(description or "")
    cmd = str(plan.get("run_command") or "")
    if urls and cmd and "://" not in cmd:
        url = urls[0].rstrip(".,;:!?")
        plan["run_command"] = f"{cmd} {url}"
        print(f"[DevAgent] 🔧 run_command'a görevdeki URL eklendi: {plan['run_command']}")
    return plan


def _validate_plan(plan: dict, description: str) -> dict:
    """Reject incomplete plans before generated files are written."""
    if not isinstance(plan, dict):
        raise ValueError("Planner response must be a JSON object.")
    files = plan.get("files")
    entry = plan.get("entry_point")
    if not isinstance(files, list) or not files:
        raise ValueError("Planner returned no project files.")
    paths = [f.get("path") for f in files if isinstance(f, dict)]
    if any(not isinstance(p, str) or not p.strip() for p in paths):
        raise ValueError("Every planned file must have a non-empty relative path.")
    if not isinstance(entry, str) or entry not in paths:
        raise ValueError("Planner entry_point must match a file in files.")
    if Path(entry).is_absolute() or ".." in Path(entry).parts:
        raise ValueError("Planner entry_point must stay inside the project.")
    if len(paths) != len(set(paths)):
        raise ValueError("Planner returned duplicate file paths.")

    desc = description.lower()
    persistence_words = (
        "database", "sqlite", "save", "store", "write", "export", "report",
        "log", "dosyaya", "veritaban", "kaydet",
    )
    if any(word in desc for word in persistence_words):
        outputs = plan.get("expected_outputs")
        if not isinstance(outputs, list) or not outputs:
            raise ValueError(
                "This request requires durable output, but planner returned no expected_outputs."
            )
    outputs = plan.get("expected_outputs", [])
    if not isinstance(outputs, list):
        raise ValueError("expected_outputs must be a list.")
    for output in outputs:
        path = output.get("path") if isinstance(output, dict) else output
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Every expected output must have a relative path.")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise ValueError(f"Expected output escapes project: {path}")
    # AUTO-INJECT README.md and tests/ if missing and project is non-trivial
    _paths = {f.get("path", "") for f in plan.get("files", [])}
    _has_readme = any("readme" in p.lower() for p in _paths)
    _has_tests = any("test" in p.lower() for p in _paths)

    if not _has_readme:
        plan["files"].append({
            "path": "README.md",
            "description": "Project documentation - setup, usage, architecture, testing",
            "imports": []
        })
        print("[DevAgent] README.md plan'a otomatik eklendi.")

    if not _has_tests and len(plan.get("files", [])) >= 3:
        plan["files"].insert(0, {
            "path": "tests/test_core.py",
            "description": "Unit tests for core business logic - at least 5 test functions",
            "imports": []
        })
        plan["files"].insert(0, {
            "path": "tests/test_integration.py",
            "description": "Integration tests for end-to-end workflows - at least 3 test functions",
            "imports": []
        })
        print("[DevAgent] tests/ plan'a otomatik eklendi.")

    return plan


def _sanitize_plan_against_self_reference(plan: dict) -> dict:
    """Model bazen urettigi projenin dosya YAPISINA bile kendi calisma
    ortaminin adini ("jarvis") bir dosya/paket gibi sizdiriyor - ornegin
    dogrudan "jarvis/core/engine.py" diye BIR DOSYA planliyor ve baska
    dosyalarin "imports" listesine "jarvis.core.engine" ekliyor.

    GERCEK MOTIVASYON: 2026-09-23'teki 4. VE 5. canli web_scraper
    testlerinde bu IKI FARKLI SEKILDE gerceklesti: once sadece main.py
    icinde tek satirlik bir halusinasyon-import olarak (Patch 7 bunu
    yazim asamasinda engelliyor), sonra planin KENDISINDE gercek bir
    dosya olarak ("jarvis/core/engine.py" planlanip diske yazildi). Bu
    IKINCI durum COK DAHA KOTU: _classify_error, "jarvis/" klasoru
    projenin GERCEKTEN kendi (yerel) klasoru oldugunu goruyor (cunku artik
    gercekten var) ve "local_import_error" olarak siniflandirip sadece
    eksik __init__.py eklemeyi deniyor - bu ASLA gercek sorunu cozmuyor
    (ayni traceback 5 denemenin 5'inde de degismeden tekrarlandi, cunku
    projenin calistigi Python yorumlayicisinda GERCEK "jarvis" paketi
    (JARVIS'in kendi kod tabani) zaten kurulu/erisilebilir olabiliyor ve
    yerel sahte "jarvis/" klasoruyle CATISIYOR). Bu fonksiyon, planlama
    ANINDA - herhangi bir dosya diske yazilmadan ONCE - "jarvis" adini
    tasiyan TUM dosyalari ve TUM importlari plandan deterministik olarak
    temizler; boylece _write_file'a bu hatali bilgi hic ulasmaz."""
    files = plan.get("files", [])
    kept_files: list[dict] = []
    dropped_paths: list[str] = []
    for f in files:
        path = f.get("path", "") if isinstance(f, dict) else ""
        top = path.split("/")[0].split("\\")[0] if path else ""
        top_no_ext = top[:-3] if top.lower().endswith(".py") else top
        if top_no_ext.lower() == "jarvis":
            dropped_paths.append(path)
            continue
        kept_files.append(f)

    if not dropped_paths:
        return plan

    print(f"[DevAgent] ⚠️ Plan, bu projeye ait olmayan sahte 'jarvis' dosyaları içeriyordu, kaldırıldı: {dropped_paths}")
    for f in kept_files:
        if isinstance(f, dict) and f.get("imports"):
            f["imports"] = [
                imp for imp in f["imports"]
                if not (isinstance(imp, str) and (imp == "jarvis" or imp.startswith("jarvis.")))
            ]

    new_plan = dict(plan)
    new_plan["files"] = kept_files
    return new_plan


def _safe_project_path(project_dir: Path, file_path: str) -> "Path | None":
    """DUZELTME (denetim bulgusu F-02): planlayici/duzeltme modelinin urettigi
    GORECELI olmasi beklenen bir dosya yolunu, proje kokunun (project_dir)
    KESINLIKLE disina cikamayacak sekilde dogrular. Mutlak yollar (ör.
    Windows'ta "C:\\..." ya da Linux'ta "/etc/...") VE '..' ile disari tasan
    gorece yollar REDDEDILIR (None doner, HICBIR SEY diske yazilmaz).

    GERCEK RISK: model plani {"path": "../../outside.py"} ya da mutlak bir
    yol donebilirdi; eski kod `project_dir / file_path` ile dogrudan
    birlestirip relative_to() kontrolu YAPMIYORDU."""
    try:
        candidate = (project_dir / file_path).resolve()
        root = project_dir.resolve()
        candidate.relative_to(root)
        return candidate
    except (ValueError, OSError):
        return None


def _check_expected_outputs(project_dir: Path, expected_outputs: list, run_started_at: float) -> list[str]:
    """Plan'da bildirilen "expected_outputs" dosyalarinin, projenin bu
    calistirilmasi SIRASINDA gercekten olusup/guncellenip guncellenmedigini
    kontrol eder. Bos liste = sorun yok.

    GERCEK MOTIVASYON: 2026-09-23'te canli bir dev_agent testinde
    (web_scraper projesi) program HICBIR Python hatasi vermeden calisip
    "Scraping completed" yazdi, ama gercekte veritabanina TEK BIR satir bile
    yazilmamisti - once bir thread-kilitlenmesi (ThreadPoolExecutor'i "with"
    ile kullanmak mainloop()'un hic baslamamasina yol aciyordu), sonra da
    Wikipedia'nin varsayilan User-Agent'i 403 ile reddetmesi yuzunden. Ikisi
    de klasik bir Python traceback'i URETMEDI, bu yuzden _has_error()/
    _classify_error() bu hatalarin IKISINI de goremezdi - dev_agent, hicbir
    sey uretmemis bir programi "calisiyor, efendim" diye rapor ediyordu.
    Bu fonksiyon, "cokmedi" ile "gercekten dogru calisti"nin AYNI SEY
    OLMADIGINI" dogrulayan somut bir kontrol saglar."""
    problems: list[str] = []
    for item in expected_outputs or []:
        rel_path = item.get("path") if isinstance(item, dict) else str(item)
        if not rel_path:
            continue
        full_path = _safe_project_path(project_dir, rel_path)
        if full_path is None:
            continue
        if full_path.is_dir():
            # Klasör çıktısı (ör. dosyaları alt klasörlere ayıran görev): bu
            # çalıştırmada içine en az bir dosya yazılmış olmalı.
            files = [f for f in full_path.rglob("*") if f.is_file()]
            if not files:
                problems.append(f"Folder '{rel_path}' was created but contains no files.")
            # ctime de sayılır: shutil.copy2 kopyada eski mtime'ı korur.
            elif max(max(f.stat().st_mtime, f.stat().st_ctime) for f in files) < run_started_at - 2:
                problems.append(f"Folder '{rel_path}' exists but no file inside it was written during this run.")
            continue
        if not full_path.is_file():
            elsewhere = [q.relative_to(project_dir).as_posix() for q in project_dir.rglob(Path(rel_path).name)
                         if q.is_file() and ".jarvis" not in q.parts and q.stat().st_mtime >= run_started_at - 2][:3]
            if elsewhere:
                problems.append(f"'{rel_path}' was written to the WRONG place ({', '.join(elsewhere)}); "
                                f"write it exactly at '{rel_path}' relative to the working directory.")
            else:
                problems.append(f"'{rel_path}' was never created.")
            continue
        try:
            stat = full_path.stat()
        except OSError:
            continue
        if stat.st_mtime < run_started_at - 2:
            problems.append(f"'{rel_path}' exists but was NOT updated during this run (stale - from before, or never actually touched now).")
        elif stat.st_size == 0:
            problems.append(f"'{rel_path}' was created/updated during this run but is completely empty (0 bytes).")
    return problems


def _check_output_contents(project_dir: Path, expected_outputs: list) -> list[str]:
    """Validate durable outputs, not merely their timestamps."""
    problems: list[str] = []
    for item in expected_outputs or []:
        rel_path = item.get("path") if isinstance(item, dict) else str(item)
        description = str(item.get("description", "") if isinstance(item, dict) else "").lower()
        full_path = _safe_project_path(project_dir, rel_path)
        if full_path is None or not full_path.is_file():
            continue
        if full_path.stat().st_size == 0:
            problems.append(f"'{rel_path}' is empty.")
            continue
        suffix = full_path.suffix.lower()
        if suffix in {".json", ".jsonl"}:
            try:
                if suffix == ".json":
                    json.loads(full_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                problems.append(f"'{rel_path}' is not valid JSON: {exc}")
        if suffix in {".db", ".sqlite", ".sqlite3"}:
            try:
                with sqlite3.connect(str(full_path), timeout=5) as conn:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    if integrity != "ok":
                        problems.append(f"'{rel_path}' failed SQLite integrity_check: {integrity}")
                    if any(word in description for word in ("row", "record", "kayıt", "scrap", "result")):
                        tables = conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                        row_count = 0
                        for (table_name,) in tables:
                            if table_name == "sqlite_sequence" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_name):
                                continue
                            row_count += conn.execute(
                                f'SELECT COUNT(*) FROM "{table_name}"'  # nosec B608: table_name is restricted by the identifier regex above.
                            ).fetchone()[0]
                        if row_count == 0:
                            problems.append(f"'{rel_path}' is valid but contains no data rows.")
            except (OSError, sqlite3.Error) as exc:
                problems.append(f"'{rel_path}' could not be validated as SQLite: {exc}")
    return problems


def _detect_manual_trigger_only(source: str) -> list[str]:
    """GUI callback'lerinin SADECE bir dugme/etkilesime baglanip, programin
    kendisi tarafindan hicbir zaman otomatik cagrilmadigini tespit etmeye
    calisir (best-effort, deterministik, LLM'e sormadan - yanlis pozitif
    vermemeye ozen gosterir, emin olunamayan durumda sessizce bos liste
    doner).

    GERCEK MOTIVASYON: 2026-09-23'teki 2. canli web_scraper testinde, model
    tum gercek isi bir "Start Scraping" dugmesinin command= callback'inin
    ARKASINA gizlemisti; dev_agent programi calistirip bekledi ama hicbir
    dugmeye tiklayamadigi icin 90 saniye sonunda hicbir sey olmamisti. Bu,
    klasik "sessiz mantik hatasi"ndan FARKLI bir kok neden: kod calisiyor,
    sadece dev_agent'in otomatik dogrulama yontemiyle asla tetiklenemiyor."""
    candidates: set[str] = set()
    for m in re.finditer(r"command\s*=\s*self\.(\w+)", source):
        candidates.add(m.group(1))
    for m in re.finditer(r"command\s*=\s*(\w+)\b(?!\.)", source):
        candidates.add(m.group(1))
    for m in re.finditer(r"\.bind\([^,]+,\s*self\.(\w+)", source):
        candidates.add(m.group(1))

    orphans = []
    for name in candidates:
        without_wiring = re.sub(rf"command\s*=\s*(?:self\.)?{re.escape(name)}\b", "", source)
        without_wiring = re.sub(rf"\.bind\([^,]+,\s*self\.{re.escape(name)}\b", "", without_wiring)
        direct_call_pattern = re.compile(rf"(?<!def ){re.escape(name)}\s*\(")
        if not direct_call_pattern.search(without_wiring):
            orphans.append(name)
    return orphans


_FILE_WRITE_LITERAL_PATTERNS = [
    re.compile(r"sqlite3\.connect\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bDatabaseManager\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bdb_path\s*[:=]\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\bopen\(\s*['\"]([^'\"]+\.(?:db|sqlite3?|csv|json|xlsx|txt|log))['\"]"),
    re.compile(r"\.to_csv\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\.to_excel\(\s*['\"]([^'\"]+)['\"]"),
    re.compile(r"\.save\(\s*['\"]([^'\"]+)['\"]"),
]


def _find_referenced_file_literals(source: str) -> set[str]:
    """Kod icinde gecen, bir dosyaya YAZMAK icin kullanilan string
    literal'leri (sqlite3.connect("x.db") gibi) toplar - best-effort,
    URL'leri ve cok kisa/anlamsiz esleşmeleri eler."""
    found: set[str] = set()
    for pattern in _FILE_WRITE_LITERAL_PATTERNS:
        for m in pattern.finditer(source):
            literal = m.group(1)
            if literal and len(literal) > 2 and "://" not in literal:
                found.add(literal)
    return found


def _detect_output_filename_mismatch(expected_outputs: list, all_sources: str) -> list[str]:
    """Plan'in "expected_outputs" ile soz verdigi dosya adi, YAZILAN kodun
    HICBIR YERINDE gecmiyorsa ama ayni uzantili BASKA bir dosya adi
    aciqca kullaniliyorsa, bunu somut bir uyumsuzluk olarak raporlar.

    GERCEK MOTIVASYON: 2026-09-23'teki 3. canli web_scraper testinde plan
    "database.db" bekliyordu, ama _write_file() bu beklentiyi HIC
    gormedigi icin (sadece shared_data_contracts aliyordu, expected_outputs
    degil) kendi basina "wikipedia.db" adinda bambaska bir dosyaya yazan
    kod uretti. _check_expected_outputs() bunu "database.db guncellenmedi"
    diye doğru tespit etti, ama _fix_files()'a giden mesaj HANGI dosyanin
    yanlislikla kullanildigini soylemiyordu - bu yuzden LLM 5 denemede de
    ayni hatayi tekrarladi. Bu fonksiyon, o spesifik ipucunu saglar."""
    if not expected_outputs or not all_sources:
        return []
    referenced = _find_referenced_file_literals(all_sources)
    mismatches: list[str] = []
    for item in expected_outputs:
        rel_path = item.get("path") if isinstance(item, dict) else str(item)
        if not rel_path:
            continue
        expected_name = Path(rel_path).name
        if expected_name in all_sources or rel_path in all_sources:
            continue
        expected_ext = Path(expected_name).suffix.lower()
        alt_candidates = sorted({
            r for r in referenced
            if expected_ext and Path(r).suffix.lower() == expected_ext and Path(r).name != expected_name
        })
        if alt_candidates:
            mismatches.append(
                f"expected output '{rel_path}' does not appear ANYWHERE in the "
                f"source code, but the code writes to a differently-named file "
                f"with the same extension instead: {', '.join(alt_candidates)}."
            )
    return mismatches


_BLOCKING_DIALOG_PATTERN = re.compile(
    r"\b(?:messagebox\.(?:showinfo|showerror|showwarning|askyesno|askokcancel|"
    r"askretrycancel|askquestion|askyesnocancel)|simpledialog\.ask\w*)\s*\("
)


def _detect_blocking_dialog_calls(source: str) -> list[str]:
    """Giris dosyasinin otomatik calisan yolunda (mainloop() BASLAMADAN once,
    ya da mainloop() hic olmadan calisan bir betikte) tkinter'in modal
    dialog fonksiyonlarindan (messagebox.showinfo/showerror/askyesno vb.,
    simpledialog.ask...) biri cagriliyorsa bunu tespit eder - bu
    fonksiyonlar bir INSAN tiklayana KADAR surecin kendisini bloke eder.

    GERCEK MOTIVASYON: 2026-09-23'teki 6. canli web_scraper testinde
    main.py, tum GERCEK Tkinter arayuzunu (ilerleme cubugu, log kutusu -
    ayri bir gui/app.py dosyasinda duzgunce yazilmisti) HIC KULLANMADAN,
    islem bitince dogrudan "messagebox.showinfo(...)" cagiriyordu. Bu,
    hicbir Tk() penceresi/mainloop() olmadan bile GERCEK bir modal
    pencere acip _get_temp_root() ile kendi ic donguisunu baslatiyor ve
    kimse tiklamadigi icin sonsuza kadar (dev_agent'in zaman asimina
    kadar) bekliyor - iki ayri zaman asimi (30sn sonra 90sn) da bununla
    tam olarak eslesiyor. _detect_manual_trigger_only bunu YAKALAYAMAZ
    (bir dugmeye baglanmis bir fonksiyon degil, dogrudan cagrilan bir
    fonksiyon) - bu yuzden ayri, tamamlayici bir tespit gerekiyor."""
    return sorted(set(_BLOCKING_DIALOG_PATTERN.findall(source)))


def _format_output_problem_message(
    run_output: str,
    problems: list[str],
    entry_source: str = "",
    filename_mismatches: list[str] | None = None,
) -> str:
    """"Sessiz basarisizlik" (program cokmedi ama soz verilen ciktiyi
    uretmedi) durumunu, _fix_files'a (LLM tabanli genel duzeltmeye) gercekten
    yardimci olacak somut bir teshis mesajina cevirir. Asagidaki olasi
    nedenler, 2026-09-23'teki canli hata avinda GERCEKTEN karsilasilan
    sinifllardir - varsayimsal degildir."""
    problems_text = "\n".join(f"  - {p}" for p in problems)
    output_excerpt = run_output[:800].strip() if run_output and run_output.strip() else "(no output at all)"

    mismatch_note = ""
    if filename_mismatches:
        mismatch_lines = "\n".join(f"  - {m}" for m in filename_mismatches)
        mismatch_note = (
            f"\n\nSTRONG SUSPECT (filename mismatch):\n{mismatch_lines}\n"
            "This is very likely just a wrong filename/path hardcoded somewhere "
            "(a constructor default argument, a sqlite3.connect(...) call, an "
            "open(...) call, etc.). Search EVERY file for where this path is "
            "opened/created and change it to the EXACT required name — do not "
            "invent or keep a differently-named file."
        )

    blocking_dialog_note = ""
    if entry_source:
        dialog_calls = _detect_blocking_dialog_calls(entry_source)
        if dialog_calls:
            names = ", ".join(c.rstrip("(") for c in dialog_calls)
            blocking_dialog_note = (
                f"\n\nSTRONG SUSPECT (blocking dialog): the entry point calls "
                f"{names} directly. These tkinter dialog functions open a REAL "
                "modal window and block the calling code until a human clicks "
                "a button on it — even if no Tk() root/mainloop() exists yet, "
                "one is created implicitly. This program will be run and "
                "observed automatically with NO human available to click "
                "anything, so it will hang until timeout. Do not call these "
                "from the automatic startup path — log results to the "
                "console or a log widget instead, and only show such a "
                "dialog in response to a real user-initiated action."
            )

    manual_trigger_note = ""
    if entry_source:
        orphan_handlers = _detect_manual_trigger_only(entry_source)
        if orphan_handlers:
            names = ", ".join(orphan_handlers)
            manual_trigger_note = (
                f"\n\nSTRONG SUSPECT: the function(s) {names} appear to be wired "
                "ONLY to a button click or event binding, and are never invoked "
                "anywhere else in the entry point. This program will be run and "
                "observed automatically with NO human available to click "
                "anything or type input. If the project description gives "
                "concrete parameters (a fixed count, specific data, etc.), make "
                f"the entry point call {names} AUTOMATICALLY on startup (e.g. "
                "right after building the window, or via root.after(100, ...)) "
                "so the real work happens without waiting for a click, while "
                "still leaving the button there for a human to use later."
            )

    # ONEMLI: manual_trigger_note (varsa) - en somut/eyleme donusturulebilir
    # ipucu - genel neden listesinden ONCE gelmeli. SEBEP: _build_project son
    # "basaramadim" mesajinda last_output'u 600 karaktere KESIYOR - once ilk
    # patch5 testinde bu spesifik ipucu, uzun genel liste yuzunden tam da
    # kesilen kisma dusup kullaniciya hic ulasmiyordu.
    return (
        "NO PYTHON ERROR OCCURRED, but the program did not produce the output "
        "it was supposed to produce:\n"
        f"{problems_text}"
        f"{mismatch_note}"
        f"{blocking_dialog_note}"
        f"{manual_trigger_note}\n\n"
        "Other possible causes if the above suspect doesn't apply: a "
        "blocking call (e.g. using ThreadPoolExecutor as a context manager, "
        "which waits for the task to finish before a GUI's mainloop() can "
        "even start) that prevents real work from ever happening; a network "
        "request that fails silently because of a missing/wrong header (many "
        "real sites, including Wikipedia, reject a plain requests.get() with "
        "no User-Agent) with the exception swallowed and never surfaced; "
        "wrong assumptions about an external page/API's structure; writing "
        "to the wrong working directory or file path; a retry loop that "
        "looks like it retries but never actually re-attempts the failed "
        "operation; or a persistence/processing function described in "
        "another file (e.g. one that says it saves data) that is declared "
        "but never actually called from the entry point, with a separate, "
        "incomplete reimplementation used instead. Fix the actual logic, "
        "not just the error message.\n\n"
        "Actual console output from the run (may look harmless even though "
        "nothing was produced):\n"
        f"{output_excerpt}"
    )


def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
    shared_contracts: str = "",
    expected_outputs: str = "",
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info["path"]
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    shared_contracts_block = (
        "Shared data contracts ALL files must follow EXACTLY, even files that do not "
        "import each other (data often flows through a third file like main.py):\n"
        + shared_contracts
    ) if shared_contracts else ""

    from jarvis.actions.devkit.recipes import recipes_block
    lang_rules += "\n\n" + recipes_block(project_description, language)

    expected_outputs_block = (
        "Files this project MUST create or update on disk when it runs, with the "
        "EXACT relative path required (any file/database/log path you write in this "
        "code must match one of these paths character-for-character — never invent "
        "a different filename, even one that seems more fitting to the project's "
        "theme):\n" + expected_outputs
    ) if expected_outputs else ""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

{shared_contracts_block}

{expected_outputs_block}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- NEVER leave a function whose body is only a docstring plus pass / return None / return [] / return {{}} — every function must do its real job. An automated checker rejects such stubs and the project will NOT be accepted.
- Exactly ONE `if __name__ == "__main__":` block per file, at the very END of the file, after all definitions.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.
- If the project description gives concrete parameters (a fixed count, specific data, "save results to a database/file", etc.), the entry point must PERFORM that exact behavior AUTOMATICALLY as soon as the program starts — do not gate it behind a manual UI action (typing into a field, clicking a "Start" button) unless the description explicitly asks for manual/interactive input. This code will be verified by launching it and observing real output, with NO human available to click or type anything. A button/field may still be ADDED on top for a human to use later, but the described core behavior must also run by itself on startup.
- If another project file's description says it exposes a function (e.g. one that saves/persists data), the entry point must CALL that exact function — never leave it unused, and never silently reimplement its logic inline instead of calling it.
- If a list of required output file paths is given above, every place in this file that opens/creates/connects to a database or file for writing must use one of those EXACT paths — do not default to, invent, or fall back to any other filename.
- This is a completely standalone, independent program with NO relationship to any AI assistant framework. NEVER import a package named "jarvis" or anything resembling it, and never assume any "jarvis"-namespaced module is available — it does not exist in this project and is not a real installable dependency. Implement any needed functionality (saving data, calling an API, etc.) directly within this project's own files.
- If you use any submodule of a standard library package that is not automatically available from a bare "import X as y" (for example tkinter's ttk, filedialog, messagebox, simpledialog, colorchooser, font, scrolledtext — each needs its own explicit "from tkinter import ttk" style import), you MUST add that explicit import — do not assume importing the parent package makes its submodules' names available.
- NEVER call a blocking modal dialog function (tkinter's messagebox.showinfo/showerror/showwarning/askyesno/askokcancel/etc., or simpledialog.ask...) from the automatic startup path — these open a real window and block execution until a human clicks it, and this program will be run and observed automatically with no human available to click anything. Print results to the console or a log widget instead; only show such a dialog in direct response to a real user-initiated action (e.g. inside a button's own callback), never unconditionally on startup or at the end of automatic processing.
- If a GUI file (Tkinter, etc.) is one of this project's OTHER files, and the description calls for a graphical interface, the entry point must actually instantiate and run that GUI (create its window class and call its mainloop) — never write a separate headless/console version of the same logic in the entry point that ignores the GUI file, leaving it unused.
- EVERY network call (requests.get/post/put/delete/patch, a requests.Session's own get/post/etc., urllib, httpx, etc.) MUST include an explicit timeout (e.g. requests.get(url, timeout=10)). Never call a network function with no timeout — a single slow or unresponsive server then blocks the whole program indefinitely with no Python error at all, which will be reported as a silent failure, not a crash.
- If the description asks for parallel/concurrent/threaded work (e.g. "N paralel thread"), the entry point must actually use the threaded/concurrent implementation — never write a second, sequential version of the same logic and call that one instead, leaving the real parallel implementation unused.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        # Yazmadan ONCE sozdizimi kontrolu (sadece Python icin): hatali kod
        # hic diske yazilmasin, mumkunse hatayi modele gosterip bir kez
        # daha denensin.
        if file_path.endswith(".py"):
            try:
                compile(code, file_path, "exec")
            except SyntaxError as syntax_err:
                print(f"[DevAgent] ⚠️ Sözdizimi hatası tespit edildi ({file_path}), düzeltme deneniyor...")
                fix_prompt = (
                    f"{prompt}\n\n"
                    f"NOT: Bir önceki denemen şu sözdizimi hatasını içeriyordu: "
                    f"satır {syntax_err.lineno}: {syntax_err.msg}. "
                    f"Bu hatayı düzelterek TAM ve GEÇERLİ kodu tekrar yaz."
                )
                retry_response = model.generate_content(fix_prompt)
                retry_code = _strip_fences(retry_response.text)
                try:
                    compile(retry_code, file_path, "exec")
                    code = retry_code
                    print(f"[DevAgent] ✅ Düzeltme başarılı: {file_path}")
                except SyntaxError as second_err:
                    print(f"[DevAgent] ❌ İkinci denemede de sözdizimi hatası var "
                          f"({file_path}, satır {second_err.lineno}): {second_err.msg}. "
                          f"Yine de yazılıyor, sonraki adımda (_fix_files) düzeltilmeye çalışılacak.")

        full_path = _safe_project_path(project_dir, file_path)
        if full_path is None:
            raise ValueError(
                f"Güvenlik: planlayıcının verdiği dosya yolu ('{file_path}') proje "
                f"klasörü dışına çıkıyor, reddedildi (path traversal koruması)."
            )
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e)) from e
        raise

def _install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    result = _pip_install_dependencies(dependencies, project_dir)
    if any(re.split(r"[>=<!\[]", str(d))[0].strip().lower() == "playwright" for d in dependencies or []):
        result += " | " + _ensure_playwright_browser()
    return result


def _ensure_playwright_browser() -> str:
    """pip playwright'i kurar ama tarayiciyi indirmez; ilk calistirmada
    "Executable doesn't exist" hatasi alinmasin. Zaten varsa hizlica doner."""
    try:
        r = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if r.returncode == 0:
            return "Playwright Chromium ready."
        return f"Playwright browser install warning (non-fatal): {(r.stderr or r.stdout)[-200:]}"
    except Exception as e:  # noqa: BLE001
        return f"Playwright browser install error (non-fatal): {e}"


def _pip_install_dependencies(dependencies: list[str], project_dir: Path) -> str:
    if not dependencies:
        return "No external dependencies."

    # DUZELTME (Patch 13, 2026-09-23, 8. canli web_scraper testi): planlayici
    # LLM, _plan_project promptundaki "standart kutuphane modulleri
    # dependencies'e girmez" kuralina bazen uymuyor (ornegin "sqlite3").
    # Bu fonksiyon simdiye kadar BURAYI hic kontrol etmiyordu - Patch 10'daki
    # sys.stdlib_module_names koruması sadece REAKTIF _try_auto_install
    # icine eklenmisti, bu PROAKTIF (ilk calistirmadan once, plan'daki
    # listeye gore calisan) yola hic ugramamisti. Sonuc: "sqlite3" gibi bir
    # isim her build'de sessizce pip show/install'a gonderiliyor ve HER
    # SEFERINDE "Could not find a version that satisfies the requirement
    # sqlite3" hatasiyla basarisiz oluyordu (zararsiz ama gereksiz/kafa
    # karistirici bir uyari, ayrica olasi bir aginin/CI'in bosa harcanmasi).
    # Ayni sys.stdlib_module_names kontrolunu burada da uygulayarak iki
    # kurulum yolunu da (reaktif + proaktif) tutarli hale getiriyoruz.
    stdlib_names = getattr(sys, "stdlib_module_names", frozenset())
    # Import adı ile PyPI dağıtım adı her zaman aynı değildir. Yerel model
    # sıkça BeautifulSoup yazar; kurulabilir gerçek dağıtım beautifulsoup4'tür.
    package_aliases = {
        "beautifulsoup": "beautifulsoup4",
        "bs4": "beautifulsoup4",
        "pillow": "Pillow",
        "cv2": "opencv-python",
        "yaml": "PyYAML",
    }
    real_dependencies = []
    for dep in dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        if pkg_name.lower() in stdlib_names:
            print(f"[DevAgent] ⚠️ '{pkg_name}' zaten Python standart kütüphanesinin bir parçası (pip'te böyle bir paket yok) - planlayıcı bunu yanlışlıkla dependencies listesine eklemiş, kurulum denenmeyecek.")
            continue
        alias = package_aliases.get(pkg_name.lower())
        if alias:
            version_suffix = dep[len(pkg_name):]
            dep = alias + version_suffix
            pkg_name = alias
            print(f"[DevAgent] 🔧 Paket adı düzeltildi: {dep} (planner import adını kullanmıştı).")
        real_dependencies.append(dep)

    if not real_dependencies:
        return "No external dependencies (all listed names were standard-library modules, skipped)."

    to_install = []
    for dep in real_dependencies:
        pkg_name = re.split(r"[>=<!]", dep)[0].strip()
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", pkg_name],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            to_install.append(dep)
        else:
            print(f"[DevAgent] ✓ Already installed: {pkg_name}")

    if not to_install:
        return f"All dependencies already installed: {', '.join(real_dependencies)}"

    print(f"[DevAgent] 📦 Installing: {to_install}")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install"] + to_install,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=120, cwd=str(project_dir)
        )
        if result.returncode == 0:
            return f"Installed: {', '.join(to_install)}"
        return f"Install warning (non-fatal): {result.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return "Dependency install timed out (non-fatal)."
    except Exception as e:
        return f"Install error (non-fatal): {e}"

def _open_vscode(project_dir: Path) -> bool:
    # JARVIS_DEVAGENT_OPEN_EDITOR=0 → proje bittiginde VSCode acilmaz
    # (canli testte her gorev icin bir pencere acilmasin diye).
    if os.environ.get("JARVIS_DEVAGENT_OPEN_EDITOR", "1").strip().lower() in ("0", "false", "no", "hayir", "hayır"):
        return False
    vscode_candidates = [
        "code",
        rf"C:\Users\{Path.home().name}\AppData\Local\Programs\Microsoft VS Code\bin\code.cmd",
        r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
    ]
    for cmd in vscode_candidates:
        try:
            launch = [cmd, str(project_dir)]
            # Windows .cmd launcherları shell=True olmadan cmd.exe üzerinden,
            # sabit ve kullanıcı girdisi içermeyen argümanlarla çalıştırılır.
            if os.name == "nt" and cmd.lower().endswith(".cmd"):
                launch = ["cmd.exe", "/d", "/c", cmd, str(project_dir)]
            subprocess.Popen(
                launch,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            time.sleep(1.5)
            print(f"[DevAgent] 💻 VSCode opened: {project_dir}")
            return True
        except Exception:
            continue
    return False

# run_command, planlama asamasinda MODELIN URETTIGI bir string - kullanicidan
# gelmiyor ama yine de kor guvenilmemeli. subprocess shell=False ile calistigi
# icin pipe/redirect/glob gibi shell metakarakterleri zaten yorumlanmiyor; bu
# liste, modelin literal argv olarak yikici bir komut ONERMESINE karsi son bir
# savunma katmani (defense in depth) - "gelistirici modu" degil, mevcut
# onay-kapili/sabit-workspace tasarimina eklenen ek bir kontrol.
_DANGEROUS_RUN_PATTERNS = (
    "rm -rf", "rm -fr", "rm -r -f", "rm -f -r",
    "chmod -r 777", "chmod 777 -r", "chmod -r 000",
    "chown -r", "mkfs", "dd if=", "dd of=/dev",
    ":(){", ":() {",  # fork bomb
    "sudo ", "su -", "su root",
    "shutdown", "reboot", "poweroff", "halt",
    "> /dev/sd", "> /dev/nvme",
)


def _is_dangerous_run_command(run_command: str) -> str | None:
    low = " ".join(run_command.lower().split())
    for pattern in _DANGEROUS_RUN_PATTERNS:
        if pattern in low:
            return pattern
    return None


def _read_partial_timeout_output(out_path: Path, err_path: Path, max_chars: int = 1500) -> str:
    """Yama 19: bir 'Timed out' donusunden ONCE, surec zaten oldurulmus olsa
    bile stdout.log/stderr.log dosyalarinda o ana kadar ne yazildiysa
    OKUMAYA calisir - GERCEK bir canli testte (11. test, WikipediaScraper,
    2026-09-24) bulunan bosluk: _run_project'in timeout dalı bu dosyalari
    HIC OKUMUYORDU, sadece sabit 'Timed out after Ns...' metnini
    donduruyordu - surekli calisan bir GUI/thread uygulamasinda arka plan
    thread'inin SESSIZCE yakalayip print ettigi GERCEK hatalar (orn. "SQLite
    objects created in a thread can only be used in that same thread" -
    App.__init__'te ana thread'de acilan bir sqlite3 baglantisinin, daha
    sonra threading.Thread ile baslatilan bir arka plan is parcaciginda
    kullanilmasi) hicbir zaman modele ulasmiyordu; _fix_files 3 tam dongu
    boyunca gercek ipucundan tamamen habersiz, kor kor tahmin yurutmek
    zorunda kaliyordu. Bu, 403 Forbidden (Yama 17) ile AYNI temel desen:
    yakalanip yazdirilan ama hicbir zaman traceback olarak firlatilmayan
    bir hata - farkli olarak burada sorun dosya-hedefleme degil, hatanin
    fixer'a ULASIP ULASMADIGI (gorunurluk).
    ONEMLI: surekli calisan (asla kendiliginden bitmeyen) bir GUI/sunucu
    icin bu KISMEN yazilmis, olasilikla YARIM KALMIS bir ciktidir - taninmis
    bir hata FORMATINI (traceback, "Error: ...") ARIYORSAK bile bu kismi
    olmasi sorun degil, cunku amac SADECE bir ipucu yakalayabilmek."""
    parts = []
    for label, path in (("STDOUT", out_path), ("STDERR", err_path)):
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
        if text:
            parts.append(f"{label}:\n{text}")
    if not parts:
        return ""
    excerpt = "\n\n".join(parts)[:max_chars]
    # NOT: bu aciklama cumlesi KASITLI OLARAK kisa - _format_output_problem_message
    # asagida bu tum metni SADECE ILK 800 KARAKTERE kesiyor; uzun bir on-yazi,
    # asil ipucu olan gercek hata satirini tam da o kesim noktasina itebilirdi.
    return (
        f"\n\nPartial output before kill (may reveal a silent background error):\n{excerpt}"
    )


def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    print(f"[DevAgent] 🚀 Running: {run_command}")

    danger = _is_dangerous_run_command(run_command)
    if danger:
        print(f"[DevAgent] 🛑 Reddedildi — yıkıcı komut kalıbı tespit edildi: '{danger}'")
        return (
            f"REFUSED: run_command contains a destructive pattern ('{danger}') and was "
            f"NOT executed. This is not a real failure to fix — do not attempt to work "
            f"around it, report it to the user as-is."
        )

    try:
        parts = shlex.split(run_command, posix=(os.name != "nt"))
        if not parts:
            return "Run error: planner returned an empty run_command."
        if parts[0].lower() == "python":
            parts[0] = sys.executable

        # NOT: cikti dogrudan PIPE'a degil, gercek bir dosyaya yaziliyor ve
        # surecin sadece KENDI CIKISI (Popen.wait) bekleniyor - subprocess.run(
        # capture_output=True) KULLANMIYORUZ. SEBEP: Windows'ta bazi antivirus/
        # EDR yazilimlari (Norton dahil) yeni baslayan process'lere kendi
        # bilesenini enjekte edip cocuk surecin stdout/stderr PIPE'ina kendi
        # handle'ini da ekliyor - Python communicate()/capture_output=True
        # PIPE'in TAMAMEN kapanmasini (TUM handle'lar dahil) bekledigi icin,
        # enjekte edilen bilesen kendi handle'ini kapatmadikca sure, script
        # gercekte aninda bitmis olsa bile, TAM timeout suresi kadar "asili"
        # gorunuyor (2026-09-21'de canli testte gozlemlendi: timeout 30s->90s
        # yapilinca da SUREKLI tam o surede kesildi - gercek bir hesaplama
        # degil, bir PIPE kilitlenmesi isareti). Gercek dosyaya yazip sadece
        # process handle'ini beklemek bu sinifta bir soruna hic girmiyor.
        #
        # NOT2: tmp_dir'i "with tempfile.TemporaryDirectory()" YERINE elle
        # (mkdtemp + finally: rmtree(ignore_errors=True)) yonetiyoruz. SEBEP:
        # Flask gibi kendi reloader/alt-surecini forklayan programlarda,
        # timeout'ta sadece dogrudan cocugu (proc.kill()) oldurmek YETMIYOR -
        # reloader'in baslattigi TORUN surec (gercek sunucu) hayatta kalip log
        # dosyalarini acik tutmaya devam edebiliyor. Eski kod "with
        # TemporaryDirectory()" kullaniyordu; bu durumda dizin silinirken
        # Windows WinError 32 ("dosya baska bir islem tarafindan
        # kullaniliyor") firlatiyordu ve bu hata "Timed out..." mesajimizi
        # return ETMEDEN once with-blogundan cikarken olustugu icin asil
        # mesaji YUTUYOR, disaridaki "except Exception" bunu genel bir "Run
        # error" gibi gosteriyordu - 5 denemenin 5'i de ayni sekilde basarisiz
        # oluyordu (2026-09-21, Flask testinde canli gozlemlendi). Simdi: (1)
        # timeout'ta tum surec agacini olduruyoruz (Windows'ta taskkill /T
        # /F), (2) temizlik hatasi ASLA asil sonucu maskelemiyor.
        tmp_dir = tempfile.mkdtemp(prefix="jarvis_devagent_")
        try:
            out_path = Path(tmp_dir) / "stdout.log"
            err_path = Path(tmp_dir) / "stderr.log"
            result_text = None
            with open(out_path, "w", encoding="utf-8") as out_f, \
                 open(err_path, "w", encoding="utf-8") as err_f:
                proc = subprocess.Popen(
                    parts,
                    stdout=out_f, stderr=err_f,
                    cwd=str(project_dir),
                )
                disk_problem = _wait_with_disk_guard(proc, timeout, project_dir, (out_path, err_path))
                if disk_problem == "timeout":
                    _kill_process_tree(proc)
                    partial_excerpt = _read_partial_timeout_output(out_path, err_path)
                    result_text = (
                        f"Timed out after {timeout}s — long-running app (server/GUI) is likely working."
                        f"{partial_excerpt}"
                    )
                elif disk_problem:
                    _kill_process_tree(proc)
                    print(f"[DevAgent] 🛑 Program durduruldu: {disk_problem}")
                    result_text = (
                        f"KILLED — the program was stopped because {disk_problem}. It is writing far too much "
                        f"data (runaway loop appending to a file/log, or printing endlessly). Add a hard stop "
                        f"and write only the requested output once."
                    )

            if result_text is not None:
                return result_text

            stdout = out_path.read_text(encoding="utf-8", errors="replace").strip()
            stderr = err_path.read_text(encoding="utf-8", errors="replace").strip()

            combined_parts = []
            if stdout:
                combined_parts.append(f"STDOUT:\n{stdout}")
            if stderr:
                combined_parts.append(f"STDERR:\n{stderr}")

            return "\n\n".join(combined_parts) if combined_parts else "Ran with no output."
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except FileNotFoundError as e:
        return f"Command not found: {e}"
    except Exception as e:
        return f"Run error: {e}"


def _dir_size(path: Path, limit: int) -> int:
    """Klasör boyutu; `limit`i aşınca saymayı bırakır (büyük klasörde yavaşlamasın)."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    if total > limit:
                        return total
            except OSError:
                continue
    except OSError:
        pass
    return total


def _wait_with_disk_guard(proc: subprocess.Popen, timeout: float, project_dir: Path,
                          logs: tuple[Path, ...]) -> str:
    """proc.wait(timeout) yerine: bitene kadar bekler ama diski doldurmaya
    başlayan programı DURDURUR. Dönüş: "" (normal bitti), "timeout" ya da
    durdurma sebebi.

    NEDEN (2026-09-28): canlı test sırasında disk tamamen doldu ve sistem
    kararsızlaştı. O seferki suçlu başka bir uygulamaydı, ama JARVIS'in
    yazdığı bir programın sonsuz döngüde dosyaya yazması da aynı sonucu
    doğurur. Sınırlar: JARVIS_RUN_MAX_MB (proje + çıktı, varsayılan 500),
    JARVIS_MIN_FREE_MB (diskte kalması gereken boş alan, varsayılan 500)."""
    def _mb(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    max_bytes = _mb("JARVIS_RUN_MAX_MB", 500) * 2**20
    min_free = _mb("JARVIS_MIN_FREE_MB", 500) * 2**20
    deadline = time.monotonic() + timeout
    next_check = 0.0
    while True:
        try:
            proc.wait(timeout=0.5)
            return ""
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now >= deadline:
            return "timeout"
        if now < next_check:
            continue
        next_check = now + 2.0
        try:
            if shutil.disk_usage(project_dir).free < min_free:
                return f"free disk space dropped below {min_free // 2**20} MB"
        except OSError:
            pass
        used = sum(p.stat().st_size for p in logs if p.exists()) + _dir_size(project_dir, max_bytes)
        if used > max_bytes:
            return f"its project folder + console output grew beyond {max_bytes // 2**20} MB"


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """proc'un kendisini VE (varsa) torun sureclerini oldurur. Flask'in
    reloader'i gibi kendi alt-surecini forklayan araclarda proc.kill() TEK
    BASINA yetmiyor - torun surec hayatta kalip dosya/port acik tutmaya
    devam edebiliyor. Windows'ta "taskkill /T /F" tum agaci olduruyor;
    diger platformlarda dogrudan cocugu oldurmek yeterli."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass

def _try_fix_local_import(error_output: str, project_dir: Path) -> bool:
    """'No module named X' hatasi, X projenin KENDI klasoru/dosyasiysa,
    en yaygin sebep eksik __init__.py'dir. Projedeki her alt klasore
    (varsa) bunu ekler - guvenli, tekrar calistirilabilir bir islem."""
    added_any = False
    for sub in project_dir.rglob("*"):
        if sub.is_dir() and not sub.name.startswith((".", "__")):
            init_file = sub / "__init__.py"
            if not init_file.exists():
                # Klasorde en az bir .py dosyasi varsa paket say
                if any(sub.glob("*.py")):
                    init_file.write_text("", encoding="utf-8")
                    added_any = True
    if added_any:
        print("[DevAgent] 🔧 Eksik __init__.py dosyaları eklendi (yerel modül içe aktarma düzeltmesi).")
    return added_any


_TYPING_PUBLIC_NAMES = frozenset(n for n in dir(typing) if not n.startswith("_"))


def _try_fix_typing_import(error_output: str, project_dir: Path, project_files: list[str]) -> bool:
    """'NameError: name 'X' is not defined' hatasi, X gercekten `typing`
    modulunun bir uyesiyse (Any, Optional, Dict, List, Union, Callable, vb.),
    LLM'e tekrar sormadan DOGRUDAN, deterministik bir metin islemiyle duzeltir:
    ilgili dosyadaki 'from typing import ...' satirina eksik adi ekler (yoksa
    yeni bir import satiri ekler).

    GERCEK MOTIVASYON: 2026-09-23'te canli bir dev_agent calismasinda
    (PersonalExpenseTracker projesi) tam olarak bu hata (eksik 'Any' importu,
    gui/expense_chart.py) modelin MAX_FIX_ATTEMPTS(5) denemesinin 4'unde de
    (dosya her seferinde bastan yazildigi icin) giderilemedi - LLM'in dosyayi
    yeniden uretmesi, bu turden tek satirlik/mekanik eksiklikleri guvenilir
    sekilde yakalayamiyor. Bu fonksiyon _fix_files (LLM tabanli, olasiliksal
    yeniden yazma) cagrilmadan ONCE denenir; basarili olursa bir model
    cagrisina bile gerek kalmaz."""
    match = re.search(r"NameError: name ['\"](\w+)['\"] is not defined", error_output)
    if not match:
        return False
    missing_name = match.group(1)
    if missing_name not in _TYPING_PUBLIC_NAMES:
        return False

    error_file, _ = _parse_traceback(error_output, project_files)
    if not error_file:
        return False
    full_path = _safe_project_path(project_dir, error_file)
    if full_path is None or not full_path.is_file():
        return False

    file_text = full_path.read_text(encoding="utf-8")
    import_pattern = re.compile(r"^from typing import (.+)$", re.MULTILINE)
    existing_match = import_pattern.search(file_text)
    if existing_match:
        existing_names = [n.strip() for n in existing_match.group(1).split(",")]
        if missing_name in existing_names:
            return False  # zaten import edilmis - baska bir sey bozuk, burada cozulemez
        new_line = f"from typing import {', '.join(sorted(existing_names + [missing_name]))}"
        file_text = file_text[:existing_match.start()] + new_line + file_text[existing_match.end():]
    else:
        file_text = f"from typing import {missing_name}\n" + file_text

    full_path.write_text(file_text, encoding="utf-8")
    print(f"[DevAgent] 🔧 Eksik 'typing.{missing_name}' importu otomatik eklendi ({error_file}).")
    return True


_TKINTER_SUBMODULES = frozenset({
    "ttk", "filedialog", "messagebox", "simpledialog", "colorchooser",
    "font", "scrolledtext", "dnd",
})


def _try_fix_tkinter_submodule_import(error_output: str, project_dir: Path, project_files: list[str]) -> bool:
    """'NameError: name 'X' is not defined' hatasi, X gercekten yaygin bir
    tkinter ALT MODULU ise (ttk, filedialog, messagebox, vb.), LLM'e tekrar
    sormadan DOGRUDAN, deterministik bir metin islemiyle duzeltir:
    "from tkinter import X" satirini ekler. _try_fix_typing_import ile
    AYNI mantik/desen, sadece typing yerine tkinter alt-modulleri icin.

    GERCEK MOTIVASYON: 2026-09-23'teki 4. canli web_scraper testinde model
    "ttk.Progressbar(...)" yazdi ama "import tkinter as tk" YAPMANIN
    "ttk"yi otomatik erisilir kilmadigini (ayrica "from tkinter import ttk"
    gerektigini) atladi. _fix_files (LLM tabanli, tum dosyayi yeniden
    yazan) bu TEK SATIRLIK eksikligi İKİ AYRI DÜZELTME DENEMESİNDE de
    (attempt 2 ve attempt 3, birebir ayni traceback ile) gideremedi -
    _try_fix_typing_import'un typing icin zaten cozdugu sorunun BİREBİR
    AYNISI, sadece farkli bir modul icin."""
    match = re.search(r"NameError: name ['\"](\w+)['\"] is not defined", error_output)
    if not match:
        return False
    missing_name = match.group(1)
    if missing_name not in _TKINTER_SUBMODULES:
        return False

    error_file, _ = _parse_traceback(error_output, project_files)
    if not error_file:
        return False
    full_path = _safe_project_path(project_dir, error_file)
    if full_path is None or not full_path.is_file():
        return False

    file_text = full_path.read_text(encoding="utf-8")
    already_ok_patterns = [
        rf"from tkinter import[^\n]*\b{missing_name}\b",
        rf"import tkinter\.{missing_name}\b",
    ]
    if any(re.search(p, file_text) for p in already_ok_patterns):
        return False  # zaten import edilmis - baska bir sey bozuk, burada cozulemez

    import_pattern = re.compile(r"^import tkinter as tk$", re.MULTILINE)
    existing = import_pattern.search(file_text)
    new_line = f"from tkinter import {missing_name}"
    if existing:
        file_text = file_text[:existing.end()] + "\n" + new_line + file_text[existing.end():]
    else:
        file_text = f"{new_line}\n" + file_text

    full_path.write_text(file_text, encoding="utf-8")
    print(f"[DevAgent] 🔧 Eksik 'tkinter.{missing_name}' importu otomatik eklendi ({error_file}).")
    return True


def _apply_bad_symbol_fix(
    missing_name: str,
    module_dotted: str,
    target_path: str,
    public_top_level: list[str],
    importer_path: str,
    project_dir: Path,
) -> bool:
    """'_try_fix_bad_symbol_import' ve (Patch 11) proje HENUZ
    CALISTIRILMADAN calisan proaktif statik denetleyicinin PAYLASTIGI asil
    duzeltme mantigi. Ikisi de ayni guvenli iki-durumlu stratejiyi
    kullanir - burada TEK bir yerde tutuluyor ki iki cagiran arasinda
    davranis asla birbirinden sapmasin.

    Strateji (guvenli, iki durum):
    1) Eksik isim, ice aktaran dosyada IMPORT SATIRI DISINDA hic
       kullanilmiyorsa: dogrudan, sadece o ismi import satirindan siler.
    2) Eksik isim baska yerde de kullaniliyorsa VE hedef modulde tam
       olarak TEK bir public (alt cizgiyle baslamayan) isim tanimliysa:
       eksik ismin ice aktaran dosyadaki TUM (tam kelime) gecislerini o
       tek gercek isimle degistirir.
    Iki durumdan hicbiri kesin degilse hicbir sey yapmaz (False doner) -
    boylece bu fonksiyon asla riskli bir tahminde bulunmaz."""
    importer_full = _safe_project_path(project_dir, importer_path)
    if importer_full is None or not importer_full.is_file():
        return False

    file_text = importer_full.read_text(encoding="utf-8")
    name_pattern = re.compile(rf"\b{re.escape(missing_name)}\b")
    occurrences = len(name_pattern.findall(file_text))

    import_line_pattern = re.compile(
        rf"^from {re.escape(module_dotted)} import (.+)$", re.MULTILINE
    )
    import_match = import_line_pattern.search(file_text)
    if not import_match:
        return False

    if occurrences <= 1:
        # Sadece import satirinda geciyor, hic kullanilmiyor - guvenle sil.
        names = [n.strip() for n in import_match.group(1).split(",")]
        remaining = [n for n in names if n != missing_name]
        if remaining:
            new_line = f"from {module_dotted} import {', '.join(remaining)}"
            file_text = file_text[:import_match.start()] + new_line + file_text[import_match.end():]
        else:
            file_text = file_text[:import_match.start()] + file_text[import_match.end():].lstrip("\n")
        importer_full.write_text(file_text, encoding="utf-8")
        print(f"[DevAgent] 🔧 Kullanilmayan/kirik import '{missing_name}' kaldirildi ({importer_path}).")
        return True

    if len(public_top_level) == 1:
        real_name = public_top_level[0]
        file_text = name_pattern.sub(real_name, file_text)
        importer_full.write_text(file_text, encoding="utf-8")
        print(
            f"[DevAgent] 🔧 Yanlis isim '{missing_name}' -> gercek isim "
            f"'{real_name}' ile degistirildi ({importer_path}, {target_path}'de tanimli tek public isim)."
        )
        return True

    return False  # belirsiz (0 ya da 2+ aday) - LLM tabanli genel duzeltmeye birak


def _try_fix_bad_symbol_import(error_output: str, project_dir: Path, project_files: list[str]) -> bool:
    """'ImportError: cannot import name 'X' from 'Y'' hatasini, LLM'e
    sormadan, deterministik bir AST analiziyle duzeltmeyi dener - bir
    calisma denemesi BASARISIZ OLDUKTAN SONRA (traceback metninden)
    tetiklenir. Asil duzeltme mantigi icin bkz. _apply_bad_symbol_fix
    (Patch 11'de, proje hic calistirilmadan once calisan proaktif
    denetleyiciyle paylasilmak uzere oraya tasindi).

    GERCEK MOTIVASYON: 2026-09-23'te canli bir dev_agent calismasinda
    (book_reader projesi) main.py, gui.py'nin GERCEK sinifi 'BookApp' iken
    'from gui import Application' yazmisti - var olmayan bir isim. Bu hata
    5 deneme boyunca duzelemedi, cunku _classify_error onu yanlislikla
    "dependency_error" (eksik paket) sanip LLM'e o baglamda sunuyordu."""
    match = re.search(
        r"cannot import name ['\"](\w+)['\"] from ['\"]([\w\.]+)['\"]",
        error_output,
    )
    if not match:
        return False
    missing_name, module_dotted = match.group(1), match.group(2)

    module_rel = module_dotted.replace(".", "/") + ".py"
    target_path = None
    for pf in project_files:
        if pf == module_rel or pf.endswith("/" + module_rel) or Path(pf).stem == Path(module_rel).stem:
            target_path = pf
            break
    if target_path is None:
        return False
    target_full = _safe_project_path(project_dir, target_path)
    if target_full is None or not target_full.is_file():
        return False

    try:
        tree = ast.parse(target_full.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    public_top_level = [
        node.name for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    if missing_name in public_top_level:
        return False  # aslinda orada tanimli - baska bir sey bozuk, burada cozulemez

    error_file, _ = _parse_traceback(error_output, project_files)
    if not error_file:
        return False

    return _apply_bad_symbol_fix(
        missing_name=missing_name,
        module_dotted=module_dotted,
        target_path=target_path,
        public_top_level=public_top_level,
        importer_path=error_file,
        project_dir=project_dir,
    )


def _proactively_fix_cross_file_imports(project_dir: Path, file_codes: dict[str, str]) -> list[str]:
    """Proje HENUZ TEK BIR KEZ BILE CALISTIRILMADAN, TUM dosyalarin
    birbirinden yaptigi 'from X import Y' importlarini AST ile statik
    olarak dogrular ve bulunan HER uyusmazligi (_apply_bad_symbol_fix'in
    ayni guvenli iki-durumlu stratejisiyle) calistirma denemesi
    harcamadan, ucretsiz ve aninda duzeltir. Duzeltilen dosyalarin
    yollarini dondurur (bos liste = ya sorun yoktu ya da bulunanlar
    belirsizdi/duzeltilemedi - ikisi de LLM tabanli _fix_files'a birakilir).

    GERCEK MOTIVASYON: 2026-09-23'teki web_scraper_pro canli testinde
    main.py 'core.scrapers'i DOGRU import ediyordu, ama core/scrapers.py -
    _write_file'in dependency_context'i sayesinde utils/helpers.py'nin
    GERCEK icerigini prompt'ta GOREBILMESINE RAGMEN - orada hic
    tanimlanmayan bir 'log_message' fonksiyonunu import etmisti. Bu,
    proje HIC CALISTIRILMADAN, saf statik analizle aninda yakalanabilecek
    bir hataydi; ama eski akiste boyle bir hata sadece PAHALI bir calistir-
    basarisiz-ol-duzelt dongusuyle (gercek Python surecini baslatma +
    LLM'e sorma) fark ediliyordu. Daha kotusu: bu proje ardisik olarak
    BIRDEN FAZLA farkli import uyusmazligi iceriyordu (once core.scrapers,
    o duzelince ortaya cikan log_message) - MAX_FIX_ATTEMPTS (5) boyle
    ardisik/farkli hatalar arasinda hizla tukeniyordu. Bu fonksiyon, ilk
    calistirmadan ONCE TUM dosyalari birbirine karsi kontrol ederek,
    birden fazla uyusmazligi TEK GECISTE, sifir maliyetle yakalar."""
    fixed_paths: list[str] = []
    project_files = list(file_codes.keys())
    definitions = {fp: set(_extract_top_level_names(code)) for fp, code in file_codes.items()}

    for fp, code in list(file_codes.items()):
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            target_path = _resolve_dotted_module_to_path(node.module, project_files)
            if not target_path or target_path == fp:
                continue
            target_defined = definitions.get(target_path, set())
            target_public = [n for n in target_defined if not n.startswith("_")]
            for alias in node.names:
                name = alias.name
                if name == "*" or name.startswith("_") or name in target_defined:
                    continue
                fixed = _apply_bad_symbol_fix(
                    missing_name=name,
                    module_dotted=node.module,
                    target_path=target_path,
                    public_top_level=target_public,
                    importer_path=fp,
                    project_dir=project_dir,
                )
                if fixed:
                    new_full = _safe_project_path(project_dir, fp)
                    if new_full is not None and new_full.is_file():
                        new_text = new_full.read_text(encoding="utf-8")
                        file_codes[fp] = new_text
                        code = new_text  # bu dosyanin kalan importlari icin de guncel metni kullan
                        if fp not in fixed_paths:
                            fixed_paths.append(fp)
    return fixed_paths


_REQUESTS_HTTP_METHODS = frozenset({"get", "post", "put", "delete", "patch", "head", "options", "request"})


def _add_missing_request_timeouts(source: str, default_timeout: int = 10) -> "tuple[str, int]":
    """Kaynak kodda DOGRUDAN 'requests.get(...)'/'requests.post(...)' vb.
    seklinde yapilan HTTP cagrilarinda 'timeout=' parametresi eksikse,
    guvenli bir varsayilan (default_timeout saniye) ekler. ast.unparse ile
    tum dosyayi yeniden yazmak yerine, cagrinin GERCEK konumuna (end_col_offset)
    bakip kapanis parantezinden hemen once metni cerrahi olarak ekler -
    boylece bicimlendirme/yorumlar bozulmaz.

    GERCEK MOTIVASYON: 2026-09-23'teki web_scraper canli testinde,
    retry_scrape() 10 URL icin, HER BIRINE 2 deneme hakkiyla, TAMAMEN
    SIRALI (kullanicinin acikca istedigi paralel/thread'li YERINE) sekilde
    requests.get(url) cagiriyordu - HICBIR timeout= parametresi olmadan.
    Bu oturumda zaten gercek ag/VPN baglanti sorunlari GOZLEMLENMISTI; tek
    bir yavas/askida kalan istek bile, HICBIR Python hatasi ORTAYA
    CIKMADAN, sadece "database.db guncellenmedi" seklinde sessizce 90
    saniyelik zaman asimina neden oluyordu - 2 ayri LLM tabanli duzeltme
    denemesi bile bunu fark edip cozemedi. Bilincli olarak SADECE
    dogrudan 'requests.X(...)' modul-seviyesi cagrilari hedefleniyor
    (requests.Session() nesneleri UZERINDEN yapilan cagrilar degil) -
    boylece hicbir zaman alakasiz bir '.get(...)' cagrisina (bir dict,
    bir cache, os.environ, vb.) yanlislikla dokunulmaz."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0

    # Satir basi offsetlerini ONCEDEN hesapla - (lineno, col) -> mutlak
    # karakter konumu donusumu icin (cok satirli/sarmalanmis cagrilarda
    # SADECE o satir icinde calismak, satirlar arasi bir virguldan HEMEN
    # SONRA gelen kapanis parantezinde CIFT VIRGUL hatasina yol acardi -
    # bkz. asagidaki geriye-dogru-virgul-kontrolu).
    src_lines = source.splitlines(keepends=True)
    line_start_offsets = [0]
    for src_line in src_lines:
        line_start_offsets.append(line_start_offsets[-1] + len(src_line))

    def _to_offset(lineno: int, col: int) -> int:
        return line_start_offsets[lineno - 1] + col

    insert_offsets: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "requests"
            and func.attr in _REQUESTS_HTTP_METHODS
        ):
            continue
        if any(kw.arg == "timeout" for kw in node.keywords):
            continue
        if any(kw.arg is None for kw in node.keywords):  # **kwargs yayilimi - dokunma
            continue
        if not node.args and not node.keywords:
            continue
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if end_lineno is None or end_col is None:
            continue
        # kapanis ')' karakterinden HEMEN ONCEKI mutlak konum
        insert_offsets.append(_to_offset(end_lineno, end_col) - 1)

    if not insert_offsets:
        return source, 0

    result = source
    for off in sorted(insert_offsets, reverse=True):
        # DUZELTME: cok satirli (sarmalanmis) bir cagrida son argumanin
        # zaten sondaki virgulu ("headers=headers,\n)") olabilir - bu
        # durumda ONUNE bir virgul DAHA eklemek "x,\n, timeout=10)" gibi
        # CIFT VIRGUL SozdizimiHatasi'na yol acar. Geriye dogru (bosluk/
        # yeni satirlari atlayarak) bakip zaten bir virgul varsa, sadece
        # "timeout=N" ekle (bosuna ikinci bir virgul ekleme).
        j = off - 1
        while j >= 0 and result[j] in " \t\r\n":
            j -= 1
        needs_comma = not (j >= 0 and result[j] == ",")
        insertion = f", timeout={default_timeout}" if needs_comma else f" timeout={default_timeout}"
        result = result[:off] + insertion + result[off:]

    return result, len(insert_offsets)


def _proactively_add_request_timeouts(project_dir: Path, file_codes: dict[str, str]) -> list[str]:
    """Proje HENUZ calistirilmadan, TUM dosyalarda dogrudan
    'requests.get/post/...(...)' seklinde yapilan HTTP cagrilarina, eksikse
    guvenli bir varsayilan timeout ekler - bkz. _add_missing_request_timeouts
    docstring'i icin gercek motivasyon."""
    fixed_paths: list[str] = []
    for fp, code in list(file_codes.items()):
        new_code, count = _add_missing_request_timeouts(code)
        if count:
            full_path = _safe_project_path(project_dir, fp)
            if full_path is None or not full_path.is_file():
                continue
            full_path.write_text(new_code, encoding="utf-8")
            file_codes[fp] = new_code
            fixed_paths.append(fp)
            print(f"[DevAgent] 🔧 {fp}: {count} adet 'requests' çağrısına eksik 'timeout=' eklendi.")
    return fixed_paths


# DUZELTME (Yama 16, 2026-09-23, 9. canli web_scraper testi): GERCEK,
# tekrar tekrar gozlemlenen bir baska sessiz-basarisizlik sinifi daha:
# requests.get(url, timeout=10) gibi TIMEOUT'U OLAN ama 'headers=' HIC
# OLMAYAN bir cagri, Wikipedia gibi bircok gercek sitede "403 Client Error:
# Forbidden" ile REDDEDILIYOR - cunku bu siteler varsayilan
# "python-requests/x.x" User-Agent'ini engelliyor. Bu bir Python hatasi
# DEGIL (kod duzgun calisiyor, sadece HTTP katmaninda reddediliyor), bu
# yuzden hicbir onceki yama bunu kapsamiyordu. _add_missing_request_timeouts
# ile AYNI cerrahi-ekleme yontemini (mutlak offset + geriye-dogru-virgul-
# kontrolu) kullanarak, SADECE 'headers=' PARAMETRESI HIC OLMAYAN
# cagrilara (var olan bir headers= sozlugunu KARISTIRMAYA CALISMIYORUZ -
# icinde User-Agent olup olmadigini guvenilir sekilde anlamak AST'de
# genel durumda imkansiz; belirsizse dokunma felsefesi) tarayici-benzeri
# bir varsayilan User-Agent ekliyoruz.
_DEFAULT_SCRAPER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _add_missing_user_agent_header(source: str) -> "tuple[str, int]":
    """Kaynak kodda DOGRUDAN 'requests.get(...)'/'requests.post(...)' vb.
    seklinde yapilan HTTP cagrilarinda 'headers=' parametresi HIC yoksa,
    guvenli bir varsayilan tarayici User-Agent'i ekler - bkz. yukaridaki
    modul-seviyesi yorum icin gercek motivasyon. _add_missing_request_timeouts
    ile BIREBIR AYNI cerrahi ekleme yontemini kullanir (kod tekrarini
    onlemek yerine, Yama 12'nin zaten test edilmis/canlida dogrulanmis
    fonksiyonuna DOKUNMADAN, ayni deseni yeniden uygulamayi tercih ettik -
    boylece Yama 12'nin davranisinda regresyon riski SIFIR)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source, 0

    src_lines = source.splitlines(keepends=True)
    line_start_offsets = [0]
    for src_line in src_lines:
        line_start_offsets.append(line_start_offsets[-1] + len(src_line))

    def _to_offset(lineno: int, col: int) -> int:
        return line_start_offsets[lineno - 1] + col

    insert_offsets: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "requests"
            and func.attr in _REQUESTS_HTTP_METHODS
        ):
            continue
        if any(kw.arg == "headers" for kw in node.keywords):
            continue
        if any(kw.arg is None for kw in node.keywords):  # **kwargs yayilimi - dokunma
            continue
        if not node.args and not node.keywords:
            continue
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if end_lineno is None or end_col is None:
            continue
        insert_offsets.append(_to_offset(end_lineno, end_col) - 1)

    if not insert_offsets:
        return source, 0

    result = source
    header_literal = f"{{'User-Agent': '{_DEFAULT_SCRAPER_USER_AGENT}'}}"
    for off in sorted(insert_offsets, reverse=True):
        j = off - 1
        while j >= 0 and result[j] in " \t\r\n":
            j -= 1
        needs_comma = not (j >= 0 and result[j] == ",")
        insertion = f", headers={header_literal}" if needs_comma else f" headers={header_literal}"
        result = result[:off] + insertion + result[off:]

    return result, len(insert_offsets)


def _proactively_add_user_agent_headers(project_dir: Path, file_codes: dict[str, str]) -> list[str]:
    """Proje HENUZ calistirilmadan, TUM dosyalarda dogrudan
    'requests.get/post/...(...)' seklinde yapilan HTTP cagrilarina, 'headers='
    hic yoksa varsayilan bir tarayici User-Agent'i ekler - bkz.
    _add_missing_user_agent_header docstring'i icin gercek motivasyon."""
    fixed_paths: list[str] = []
    for fp, code in list(file_codes.items()):
        new_code, count = _add_missing_user_agent_header(code)
        if count:
            full_path = _safe_project_path(project_dir, fp)
            if full_path is None or not full_path.is_file():
                continue
            full_path.write_text(new_code, encoding="utf-8")
            file_codes[fp] = new_code
            fixed_paths.append(fp)
            print(f"[DevAgent] 🔧 {fp}: {count} adet 'requests' çağrısına eksik User-Agent header'ı eklendi (403 Forbidden riskine karşı).")
    return fixed_paths


# DUZELTME (Yama 14, 2026-09-23): Kullanicinin sordugu "tum dosyayi tarayip
# calisir hale getiren hazir bir program yok mu" sorusuna cevaben eklendi.
# Boyle sihirli/genel bir arac YOK VE OLAMAZ (bir programin GERCEKTEN
# istenen seyi yaptigini, calistirmadan/anlamdan kesin olarak bilmenin bir
# yolu yok - bu, "duzeltme" degil "dogrulama" sorunu). AMA "tanimsiz isim"
# (NameError'a yol acacak) ve syntax hatasi gibi GENIS bir kategori, kodu
# hic calistirmadan, olgun ve hazir bir statik analiz araciyla (ruff -
# dev_agent'in KENDI CI'inde zaten kullandigimiz ayni arac) tespit
# edilebilir. Bunu tek tek her hata sinifi icin elle AST kontrolu yazmak
# yerine (Yama 10/11 gibi), dogrudan ruff'a devrediyoruz - "boyle bir
# program var mi" sorusunun dogru cevabi "var, ama ozel amacli degil,
# genel amacli bir linter; onu tekrar icat etmeye gerek yok".
#
# Bu kontrol ozellikle onemli cunku: bir arka plan thread'i (threading.Thread
# ile baslatilan) icindeki bir NameError, ana thread'e/GUI'ye hicbir
# traceback sizdirmadan SESSIZCE thread'i oldurebilir - ana pencere/mainloop
# calismaya devam eder, hicbir Python hatasi gorunmez, tek belirti programin
# "cokmeden ama beklenen ciktiyi uretmeden" zaman asimina ugramasidir - tam
# olarak 8. canli web_scraper testinde gozlemlenen "90 saniye timeout +
# database.db guncellenmedi" belirtisiyle ayni sinif. Bu yuzden bu kontrolu,
# _run_project hic cagrilmadan, dosyalar yazildiktan hemen sonra yapiyoruz -
# boylece MAX_FIX_ATTEMPTS butcesinden hicbir sey harcamadan (ve 30-90
# saniyelik bosa gecen bir calistirmadan) yakalanip duzeltilebilir.
#
# SADECE yuksek-guven, KESIN calisma-zamani hatasi anlamina gelen kurallar
# secildi: F821 (tanimsiz isim - NameError), F822/F823 (ilgili tanimsiz-
# referans durumlari), E9 (syntax hatalari - programin ic parse bile
# edilemeyecegi anlamina gelir). F401 (kullanilmayan import) / F841
# (kullanilmayan degisken) BILEREK DISARIDA - bunlar gercek bir CALISMA
# HATASI degil, sadece stil/temizlik bilgisi; projenin "belirsizse/riskli
# ise dokunma" felsefesiyle tutarli olarak, gercek bir crash'e yol
# acmayacak seyler icin LLM'e gereksiz "duzeltme" gorevi verilmiyor.
_RUFF_PROACTIVE_SELECT = "F821,F822,F823,E9"


def _run_ruff_check(project_dir: Path):
    """`ruff` bazi kurulumlarda "python -m ruff" olarak (normal pip paketi -
    dev_agent'in KENDI CI'inde kullandigi sekilde), bazilarinda ise sadece
    PATH'te bagimsiz bir yurutulebilir dosya olarak (orn. uv/pipx ile
    kurulmus) bulunabilir. Ikisini de sirayla dener - hangisi calisirsa onu
    kullanir. Hicbiri calismazsa None doner (istisna FIRLATMAZ)."""
    for cmd_prefix in ([sys.executable, "-m", "ruff"], ["ruff"]):
        try:
            result = subprocess.run(
                cmd_prefix + ["check", "--select", _RUFF_PROACTIVE_SELECT,
                              "--output-format", "json", str(project_dir)],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        # ONEMLI: "python -m ruff" modulu hic YOKSA, Python bunu da
        # returncode 1 ile bitirir (tipki ruff'in "bulgu var" durumu gibi!)
        # - ikisini SADECE returncode'a bakarak ayirt edemeyiz. stderr'de
        # "No module named" gecmesi, bu komut FORMUNUN gecersiz oldugunu
        # (ve JSON stdout'un bos/anlamsiz oldugunu) gosterir - bu durumda
        # yanlislikla "temiz, bulgu yok" sonucuna varmak yerine diger
        # komut seklini (bagimsiz "ruff" yurutulebilir dosyasi) deniyoruz.
        module_missing = "No module named" in (result.stderr or "")
        if result.returncode in (0, 1) and not module_missing:
            return result
    return None


def _proactively_lint_generated_files(project_dir: Path, file_codes: dict[str, str]) -> "dict[str, list[dict]] | None":
    """Projedeki TUM dosyalara, ilk calistirmadan once ruff (sadece yukarida
    aciklanan yuksek-guven kural alt kumesiyle) uygular. Bulgu yoksa veya
    ruff bu ortamda hic kullanilamiyorsa (kurulu degil, kurulumu basarisiz,
    ag yok, vb.) SESSIZCE None doner - bu opsiyonel bir iyilestirmedir,
    dev_agent'in temel calismasi buna BAGLI DEGILDIR ve asla build'i
    engellemez ya da kullaniciya hata olarak gosterilmez."""
    result = _run_ruff_check(project_dir)
    if result is None:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "ruff"],
                capture_output=True, text=True, timeout=60,
            )
        except Exception:
            return None
        result = _run_ruff_check(project_dir)
        if result is None:
            return None

    try:
        findings = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return None

    if not findings:
        return None

    issues_by_file: dict[str, list[dict]] = {}
    for finding in findings:
        abs_path = finding.get("filename", "")
        if not abs_path:
            continue
        try:
            rel_path = Path(abs_path).resolve().relative_to(project_dir.resolve()).as_posix()
        except (ValueError, OSError):
            continue
        if rel_path not in file_codes:
            continue
        loc = finding.get("location") or {}
        issues_by_file.setdefault(rel_path, []).append({
            "code": finding.get("code") or "?",
            "message": finding.get("message") or "",
            "line": loc.get("row"),
            "col": loc.get("column"),
        })

    return issues_by_file or None


# DUZELTME (Yama 15, 2026-09-23, 8. canli web_scraper testi): "database.db
# guncellenmiyor + 90s zaman asimi" belirtisinin GERCEK kok nedeni bulundu -
# LLM'in olusturdugu Tkinter uygulamasi, asil isi (scraping'i baslatan
# thread) SADECE bir "Start Scraping" butonuna tiklaninca calistiriyordu;
# __init__ icinde OTOMATIK cagrilmiyordu (bkz. o testten cekilen gercek
# main.py: ilk hali sadece create_widgets() cagiriyordu, start_scraping()
# YOK). dev_agent'in kendi calistirma/dogrulama mekanizmasi GUI'yi baslatir
# ama HICBIR ZAMAN gercek bir insan gibi butona tiklamaz - bu yuzden program
# hicbir Python hatasi vermeden, sadece "bos" bir pencere acik kalarak
# zaman asimina ugruyor ve expected_outputs (orn. database.db) hicbir zaman
# guncellenmiyor. Bu, Patch 9'un "otomatik baslangic yolunda bloklayan
# dialog cagirma" kuralinin dogal bir uzantisi: bir GUI, otomatik/headless
# dogrulamada calisacaksa, asil isini kullanicidan BAGIMSIZ olarak da
# tetiklemelidir.
#
# YANLIS POZITIFTEN KACINMAK ICIN (orn. bir "Temizle"/"Çıkış" butonunun
# KASITLI OLARAK sadece tiklamayla calismasi gerektigi durumu yanlislikla
# "otomatik calistir" diye isaretlememek icin) COK KATI/dar bir kural
# kullaniliyor - Yama 11'deki "tek aday" felsefesiyle BIREBIR AYNI: sadece
# TUM SINIFTA TEK BIR boyle buton/menu-komutu varsa (yani hangi eylemin
# "asil is" oldugu konusunda HICBIR belirsizlik yoksa) VE o metod sinifin
# baska hicbir yerinde cagrilmiyorsa isaretlenir; 2+ boyle komut varsa
# (Start/Stop/Clear gibi), HANGISININ otomatik calismasi gerektigi
# belirsizdir - bu durumda TAMAMEN SESSIZ KALINIR, tahmin yurutulmez.
_GUI_TRIGGER_WIDGET_SUFFIXES = ("Button",)
_GUI_TRIGGER_METHOD_NAMES = frozenset({"add_command"})


def _call_func_name(call: ast.Call) -> str:
    """Bir Call node'unun cagirdigi fonksiyon/metodun SON isim parcasini
    dondurur (orn. ttk.Button(...) icin 'Button', menu.add_command(...) icin
    'add_command'). Eslesme yoksa bos string doner."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _file_imports_tkinter(tree: ast.Module) -> bool:
    """Dosyanin modul-seviyesinde tkinter'i import edip etmedigini kontrol
    eder (Yama 18). GERCEK bir canli testte bulunan bosluk: Tkinter'da GUI
    sinifi yazmanin IKI esit derecede yaygin deseni var - (1) KALITIM:
    'class App(tk.Tk):' ve (2) BILESIM/composition: 'class App:' + '__init__
    (self, root)' + root disaridan Tk() olarak verilir. Yama 15 SADECE (1)'i
    taniyordu (base_names icinde 'Tk'/'Frame' arayarak) - bilesim deseni hic
    incelenmiyordu, ve tam da bu yuzden 10. canli testte (Wikipedia_Scraper)
    'class App:' (hicbir siniftan turemeyen, root'u parametre alan) sinifinin
    butona-bagli-tek-tetikleyici hatasi HIC yakalanamadi. Dosya seviyesinde
    tkinter import'u aramak, hangi OOP deseni kullanilirsa kullanilsin bu
    dosyanin gercekten bir Tkinter GUI dosyasi oldugunu guvenli sekilde
    tespit eder."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in ("tkinter", "Tkinter") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in ("tkinter", "Tkinter"):
                return True
    return False


def _detect_gui_manual_only_trigger(source: str) -> "list[dict] | None":
    """Tek bir dosyanin kaynagini tarar; yukarida aciklanan KATI kurala gore
    'sadece butona bagli, hicbir yerde otomatik cagrilmayan' bir GUI
    tetikleyicisi bulursa, o dosya icin bulgu listesini doner (yoksa None)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    findings: list[dict] = []
    file_imports_tk = _file_imports_tkinter(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        base_names = []
        for base in node.bases:
            if isinstance(base, ast.Attribute):
                base_names.append(base.attr)
            elif isinstance(base, ast.Name):
                base_names.append(base.id)
        # Yama 18: kalitim deseni (Tk/Frame'den tureme) YA DA dosya
        # tkinter import ediyorsa (bilesim/composition deseni de dahil).
        # Yanlis-pozitif riski dusuk kalir cunku asagidaki widget-cagrisi
        # kontrolu (Button sonek / add_command) zaten cok dar kapsamli.
        looks_like_gui = any(("Tk" in b or "Frame" in b) for b in base_names) or file_imports_tk
        if not looks_like_gui:
            continue

        has_init = any(
            isinstance(item, ast.FunctionDef) and item.name == "__init__"
            for item in node.body
        )
        if not has_init:
            continue

        # DUZELTME (kendi Yama-15 testimde bulundu): "baska yerde de
        # kullaniliyor mu" kontrolunu SADECE dogrudan self.X(...)
        # CAGRILARIYLA sinirlamak yanlis pozitif veriyordu - orn.
        # self.after(100, self.X) veya Thread(target=self.X) gibi, X'i bir
        # CALLBACK olarak baska bir cagriya ARGUMAN olarak GECEN (ama
        # kendisi dogrudan CAGIRMAYAN) COK YAYGIN, MESRU otomatik-tetikleme
        # kaliplarini "hic kullanilmiyor" saniyordu. Bu yuzden "baska yerde
        # kullanim" kontrolu artik cok daha genis: command= kwarg SLOTUNUN
        # KENDISI HARIC, sinif icindeki HERHANGI bir self.X referansi
        # (cagrilsin cagrilmasin) "baska yerde de var" sayilir - bu, olasi
        # yanlis pozitifi tamamen ortadan kaldiran, KASITLI OLARAK daha
        # MUHAFAZAKAR bir tanim (Yama 11/15'in "belirsizse dokunma"
        # felsefesiyle tutarli: az bulgu, ama bulunanlar yuksek guvenli).
        trigger_candidates: list[tuple[str, int]] = []
        trigger_slot_node_ids: set[int] = set()

        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func_name = _call_func_name(sub)
            is_trigger_widget = (
                func_name.endswith(_GUI_TRIGGER_WIDGET_SUFFIXES)
                or func_name in _GUI_TRIGGER_METHOD_NAMES
            )
            if is_trigger_widget:
                for kw in sub.keywords:
                    if (kw.arg == "command" and isinstance(kw.value, ast.Attribute)
                            and isinstance(kw.value.value, ast.Name)
                            and kw.value.value.id == "self"):
                        trigger_candidates.append((kw.value.attr, sub.lineno))
                        trigger_slot_node_ids.add(id(kw.value))

        other_referenced: set[str] = set()
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self" and id(sub) not in trigger_slot_node_ids):
                other_referenced.add(sub.attr)

        uncalled = [
            (name, lineno) for (name, lineno) in trigger_candidates
            if name not in other_referenced
        ]

        if len(trigger_candidates) == 1 and len(uncalled) == 1:
            name, lineno = uncalled[0]
            findings.append({
                "code": "GUI-MANUAL-ONLY-TRIGGER",
                "message": (
                    f"class '{node.name}' wires a button/menu command to "
                    f"'self.{name}', but self.{name}(...) is NEVER also "
                    f"called automatically (e.g. at the end of __init__). "
                    f"This is the ONLY such command in the class, so it is "
                    f"almost certainly the app's core/real work. An "
                    f"automated headless test harness runs this script but "
                    f"can never click a button, so self.{name} will NEVER "
                    f"execute and no expected output will ever be produced "
                    f"— the program will just sit idle until it times out. "
                    f"Fix by ALSO calling self.{name}() automatically (for "
                    f"example at the end of __init__, or scheduled via "
                    f"self.after(100, self.{name}) if it needs the GUI to "
                    f"be fully constructed first) — keep the button working "
                    f"too, for real interactive use."
                ),
                "line": lineno,
                "col": 0,
            })

    return findings or None


def _proactively_detect_gui_manual_only_triggers(file_codes: dict[str, str]) -> "dict[str, list[dict]] | None":
    """Projedeki TUM dosyalara _detect_gui_manual_only_trigger uygular ve
    sonuclari _proactively_lint_generated_files ile AYNI sekle
    ({dosya: [bulgu, ...]}) getirir - boylece ikisi tek bir _fix_files
    cagrisinda BIRLESTIRILEBILIR (asagida _build_project'te yapiliyor)."""
    issues_by_file: dict[str, list[dict]] = {}
    for fp, code in file_codes.items():
        findings = _detect_gui_manual_only_trigger(code)
        if findings:
            issues_by_file[fp] = findings
    return issues_by_file or None


# Bazi paketlerin IMPORT adi (kod icinde "import X") ile PyPI'daki GERCEK
# paket adi FARKLI - bunu bilmeden "No module named X" -> "pip install X"
# yapmak calisir gibi gorunur ama bazilari icin asla basarili olmaz:
# ozellikle 'sklearn', PyPI'da KASITLI OLARAK bozuk/deprecated bir stub -
# gercek paket 'scikit-learn'. Bu yuzden bu eslemeyi kontrol etmeden pip'e
# ham import adini vermek, ayni hatanin sonsuz dongu gibi tekrar tekrar
# denenmesine yol aciyordu (2026-09-21'de canli dev_agent testinde
# gozlemlendi: kmeans_clustering projesi 5 denemede de duzelemedi, gercek
# sebep 'sklearn' paketinin hicbir zaman kurulamamasiydi).
_IMPORT_TO_PYPI = {
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "cv2": "opencv-python",
    "pil": "Pillow",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "serial": "pyserial",
    "usb": "pyusb",
    "attr": "attrs",
    "nmap": "python-nmap",
    "openssl": "pyOpenSSL",
    "win32com": "pywin32",
    "win32api": "pywin32",
    "crypto": "pycryptodome",
    "gi": "PyGObject",
    "opengl": "PyOpenGL",
}


def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    """ModuleNotFoundError varsa eksik paketi otomatik kurmaya çalışır."""
    pattern = re.compile(
        r"No module named ['\"]([a-zA-Z0-9_\-\.]+)['\"]", re.IGNORECASE
    )
    match = pattern.search(error_output)
    if not match:
        return False

    module_name = match.group(1).split(".")[0]

    # GUVENLIK/HIZ (2026-09-23, 4. canli web_scraper testi): model bazen
    # ureteceği bagimsiz projenin icine, kendi calisma ortaminin adi olan
    # "jarvis"i (ornegin "import jarvis.core.engine as engine") halusinasyon
    # olarak yaziyor. Bu ASLA gercek, pip ile kurulabilir harici bir paket
    # DEGILDIR - kurmaya calismak sadece zaman kaybettirir ve "wheel build"
    # hatasiyla kullanicida yanlis bir "kurulum bozuk" izlenimi birakir.
    # Boyle bir import, _fix_files'in LLM tabanli genel duzeltmesine
    # birakilmali (o da zaten bu importu kaldirmayi genelde basariyor).
    if module_name.lower() == "jarvis":
        print("[DevAgent] ⚠️ 'jarvis' harici bir paket değil, bu projeye YANLIŞLIKLA eklenmiş bir import olmalı - kurulum denenmeyecek.")
        return False

    # DUZELTME (2026-09-23, 7. canli web_scraper testi): "sqlite3" gibi
    # Python standart kutuphanesinin PARCASI olan modul isimleri de bazen
    # buraya "eksik" gibi geliyor (baska bir hata sinifi - importun
    # kendisi degil, kod BASKA bir sebeple calismiyor - ama _classify_error
    # bunu "No module named"/dependency_error olarak etiketleyebiliyor).
    # Bunlari pip ile kurmaya calismak HER ZAMAN basarisiz olur ("Could not
    # find a version that satisfies the requirement sqlite3") ve "jarvis"
    # halusinasyonuyla AYNI zaman-kaybi/yanlis-izlenim sorununu yaratir.
    # sys.stdlib_module_names (3.10+) TUM standart kutuphane modullerinin
    # kesin listesini verir - "jarvis" icin yapilan ozel kontrolun daha
    # genel, otomatik-guncel hali.
    if module_name in getattr(sys, "stdlib_module_names", frozenset()):
        print(f"[DevAgent] ⚠️ '{module_name}' zaten Python standart kütüphanesinin bir parçası (pip'te böyle bir paket yok, ayrıca kurulmasına gerek yok) - kurulum denenmeyecek.")
        return False

    pkg = _IMPORT_TO_PYPI.get(module_name.lower(), module_name.replace("_", "-"))
    print(f"[DevAgent] 🔧 Auto-installing missing package: {pkg} (import: {module_name})")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", pkg],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=60, cwd=str(project_dir)
        )
        return result.returncode == 0
    except Exception:
        return False

def _search_error_context(error_output: str) -> str:
    """Hata mesaji icin internette gercek coz umler arar - tipki bir
    gelistiricinin hata mesajini Google'da aramasi gibi. Basarisiz
    olursa sessizce bos dondurur (arama olmadan da calismaya devam eder)."""
    try:
        from jarvis.actions.web_search import _ddg_search
        # Hata ciktisinin SON satiri genelde en spesifik/aranabilir kismidir
        # (ornek: "ModuleNotFoundError: No module named 'x'")
        lines = [ln.strip() for ln in error_output.strip().splitlines() if ln.strip()]
        query = lines[-1][:150] if lines else error_output[:150]
        results = _ddg_search(f"python {query}", max_results=3)
        if not results:
            return ""
        formatted = "\n".join(
            f"- {r['title']}: {r['snippet'][:200]}" for r in results if r.get("snippet")
        )
        return f"\n\nWeb search results for this error (for reference, verify before applying):\n{formatted}\n" if formatted else ""
    except Exception:
        return ""


def _resolve_dotted_module_to_path(module_dotted: str, project_files: list[str]) -> "str | None":
    """'web.scrape' gibi nokta-ayrimli bir modul adini, projenin GERCEK
    dosya yoluna ('web/scrape.py') esler. Proje dosyalari arasinda
    bulunamazsa (harici bir paketse) None doner."""
    candidate = module_dotted.replace(".", "/") + ".py"
    candidate_init = module_dotted.replace(".", "/") + "/__init__.py"
    for pf in project_files:
        normalized = pf.replace("\\", "/")
        if normalized == candidate or normalized == candidate_init:
            return pf
    return None


def _extract_top_level_names(source: str) -> list[str]:
    """Bir Python dosyasinin en ust seviyede tanimladigi, BASKA bir
    dosyanin "from X import Y" ile alabilecegi isimleri (fonksiyon, sinif,
    modul-seviyesi degisken) cikarir. Regex degil AST kullanir - yorum
    satirlarindaki veya string icindeki "def "/"class " gibi sahte
    eslesmelere karsi guvenlidir."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
    return names


def _project_module_name(fp: str) -> str:
    """'core/database.py' -> 'core.database'; 'main.py' -> 'main'."""
    norm = fp.replace("\\", "/")
    if norm.endswith(".py"):
        norm = norm[:-3]
    if norm.endswith("/__init__"):
        norm = norm[: -len("/__init__")]
    return norm.replace("/", ".")


def _top_level_local_imports(tree: ast.Module, module_names: set[str]) -> set[str]:
    """Bir dosyanin SADECE modul-seviyesindeki (fonksiyon/sinif govdesine
    GOMULU OLMAYAN) import ifadelerini tarar ve projenin KENDI dosyalarina
    (module_names) karsilik gelenleri dondurur. KASITLI OLARAK ast.walk
    DEGIL, sadece tree.body (dogrudan modul govdesi) taraniyor - bir
    fonksiyon/metod GOVDESI icine gizlenmis (gecikmeli/lazy) bir import,
    dongusel importu KIRMAK icin YAYGIN ve GECERLI bir teknik oldugundan,
    bunu yanlislikla "hala dongusel" saymak yanlis pozitif olurdu."""
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in module_names:
                found.add(node.module)
            else:
                for mn in module_names:
                    if node.module == mn or node.module.startswith(mn + "."):
                        found.add(mn)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in module_names:
                    found.add(alias.name)
                else:
                    for mn in module_names:
                        if alias.name == mn or alias.name.startswith(mn + "."):
                            found.add(mn)
    return found


def _detect_circular_imports(file_codes: dict[str, str]) -> "dict[str, list[dict]] | None":
    """Projenin KENDI dosyalari arasinda, modul YUKLENIRKEN (import zamaninda)
    olusan bir dongusel import olup olmadigini AST tabanli bir bagimlilik
    grafigi kurup tespit eder (Yama 20).

    GERCEK MOTIVASYON: 12. canli JARVIS testinde (WikipediaScraper,
    2026-09-24) reaktif duzeltici, "database.db guncellenmedi" sorununu
    cozmeye calisirken core/database.py'nin KENDI '__main__' bloguna
    `from main import WikipediaScraperApp` + `app = WikipediaScraperApp(...)`
    ekledi - main.py de zaten `from core.database import save_data`
    yaptigi icin bu, modul-seviyesinde GERCEK bir dongusel import olusturdu
    (database.py'nin __main__ blogu hiçbir zaman calistirilmasa bile, TEK
    BASINA modul-seviyesi 'from main import ...' satiri, main.py import
    edilirken ANINDA cikmeza yol aciyor). Python bunu "cannot import name
    'X' from partially initialized module 'Y' (most likely due to a
    circular import)" seklinde bildiriyor - ama _detect_import_name_mismatch
    (Yama 10) SADECE "from 'Y'" (dogrudan tek tirnak) desenini taniyordu,
    "from partially initialized module 'Y'" (araya giren ek metin) UYUŞMUYOR
    - bu yuzden reaktif duzeltici 2 tam deneme (4 ve 5) boyunca HANGI
    dosyanin gercekten sorumlu oldugunu (core/database.py) HICBIR ZAMAN
    ogrenemedi, ayni cokme birebir tekrarlandi.

    Bu fonksiyon, hata metninin TAM ifadesine bagli kalmadan (daha saglam),
    projenin GERCEK mevcut halini dogrudan tarayarak calisir."""
    module_to_file = {_project_module_name(fp): fp for fp in file_codes}
    module_names = set(module_to_file)

    graph: dict[str, set[str]] = {}
    for fp, code in file_codes.items():
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        mod = _project_module_name(fp)
        graph[mod] = _top_level_local_imports(tree, module_names) - {mod}

    visiting: set[str] = set()
    visited: set[str] = set()
    cycle_found: list[str] | None = None

    def dfs(node: str, path: list[str]) -> bool:
        nonlocal cycle_found
        visiting.add(node)
        path.append(node)
        for nxt in graph.get(node, ()):
            if nxt in visiting:
                idx = path.index(nxt)
                cycle_found = path[idx:] + [nxt]
                return True
            if nxt not in visited and dfs(nxt, path):
                return True
        path.pop()
        visiting.discard(node)
        visited.add(node)
        return False

    for mod in sorted(graph):
        if mod not in visited and dfs(mod, []):
            break

    if not cycle_found:
        return None

    cycle_files = [module_to_file[m] for m in cycle_found if m in module_to_file]
    chain_desc = " -> ".join(cycle_files)

    issues: dict[str, list[dict]] = {}
    for fp in dict.fromkeys(cycle_files):
        issues.setdefault(fp, []).append({
            "code": "CIRCULAR-IMPORT",
            "message": (
                f"Module-level circular import detected: {chain_desc}. "
                "At least one of these top-level 'import'/'from ... import "
                "...' statements executes while the other module is still "
                "in the middle of being loaded, which will crash with "
                "'cannot import name ... from partially initialized "
                "module ...'. Fix by either (a) moving one of the "
                "offending imports to be LOCAL to the specific function/ "
                "method that actually needs it (a lazy import inside the "
                "function body — this is the standard, safe fix and does "
                "NOT run at module-load time), or (b) moving the shared "
                "piece both files need into a third, lower-level file that "
                "neither of the cycle's files needs to import from the "
                "other for. Do not simply delete the functionality."
            ),
            "line": 1,
            "col": 0,
        })
    return issues


def _detect_import_name_mismatch(error_output: str, file_codes: dict[str, str]) -> "dict | None":
    """"cannot import name 'X' from 'Y'" seklindeki bir ImportError'i,
    projenin GERCEK dosyalariyla eslestirip somut, eyleme-donusturulebilir
    bir teshis uretir: X hangi dosyadan isteniyor, o dosyada GERCEKTE
    hangi isimler tanimli.

    GERCEK MOTIVASYON: 2026-09-23'teki 7. canli web_scraper testinde
    main.py, 'web/scrape.py'den 'fetch_pages' import etmeye calisiyordu
    ama o isim orada tanimli degildi - AYNI hata 5 denemenin 5'inde de
    DEGISMEDEN tekrarlandi. Kok neden: _fix_files'in "import_error" dali,
    SADECE error_file'i (main.py, traceback'in gosterdigi yer) duzeltme
    adayi yapiyor ve "error_file'i import eden dosyalari" ariyordu - ama
    error_file zaten giris noktasi oldugu icin onu import eden hicbir
    dosya yok, bu arama hep bos donuyordu. Asil eksik olan, main.py'nin
    KENDISININ import ETTIGI hedef modul (web/scrape.py) - o dosya hicbir
    zaman ayri bir duzeltme denemesi olarak ele alinmadigi icin LLM, onun
    GERCEKTE ne icerdigini gormeden (sadece 1500 karakterle kirpilmis
    "read-only" baglamdan tahmin ederek) ayni yanlis ismi tekrar tekrar
    uretmeye devam ediyordu."""
    match = re.search(
        r"cannot import name ['\"](\w+)['\"] from ['\"]([\w\.]+)['\"]",
        error_output,
    )
    if not match:
        return None
    missing_name, target_module = match.group(1), match.group(2)
    target_path = _resolve_dotted_module_to_path(target_module, list(file_codes.keys()))
    if not target_path:
        return None
    actual_names = _extract_top_level_names(file_codes.get(target_path, ""))
    return {
        "missing_name": missing_name,
        "target_module": target_module,
        "target_path": target_path,
        "actual_names": actual_names,
    }


def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
    shared_contracts: str = "",
    expected_outputs: str = "",
    known_error_type: str = "",
    lint_issues: "dict[str, list[dict]] | None" = None,
    repeat_of_previous: bool = False,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    # ONEMLI: "output_missing" _classify_error()'un kendi sozlugunde YOKTUR -
    # sadece _build_project()'in dis dongusunde uretilen yapay bir etiket.
    # known_error_type verilmisse (ozellikle output_missing icin) ONA
    # guveniyoruz; yoksa (dogrudan/test amacli cagrilarda) eskisi gibi
    # error_output metnini kendimiz siniflandiriyoruz.
    error_type = known_error_type or _classify_error(error_output)
    web_context = _search_error_context(error_output)

    # DUZELTME (2026-09-23, 7. canli web_scraper testi - bkz.
    # _detect_import_name_mismatch docstring'i): "cannot import name X from
    # Y" hatasinda, asil duzeltilmesi/incelenmesi gereken dosya cogu zaman
    # error_file (import EDEN dosya) degil, Y'nin KENDISIDIR (import
    # EDILEN, ismi eksik olan dosya). Bunu, asagidaki files_to_fix
    # olusturmadan ONCE tespit ediyoruz ki hem error_file hem target_path
    # duzeltme adayi olabilsin.
    import_mismatch = (
        _detect_import_name_mismatch(error_output, file_codes)
        if error_type == "import_error" else None
    )

    # DUZELTME (Yama 20, 12. canli WikipediaScraper testi): import_mismatch
    # (yukarida) SADECE "cannot import name 'X' from 'Y'" (dogrudan tek
    # tirnak) desenini yakalar - Python'un dongusel-import hatasindaki asil
    # metin ("... from partially initialized module 'Y' (most likely due
    # to a circular import)") bu regex ile UYUSMAZ, bu yuzden bu durumda
    # import_mismatch hep None kalir. _detect_circular_imports ise hata
    # metnine hic bakmadan, projenin GERCEK GUNCEL haline (file_codes)
    # dogrudan AST ile bakar - daha saglam ve tam da bu sinifi yakalar.
    circular_import = (
        _detect_circular_imports(file_codes)
        if error_type in ("import_error", "local_import_error") else None
    )

    files_to_fix: list[str] = []

    # DUZELTME (Yama 14): proaktif ruff bulgulari icin - error_output bir
    # traceback DEGIL (henuz hic calistirilmadi), bu yuzden error_file/
    # _parse_traceback burada hicbir sey bulamaz. ruff zaten HANGI
    # dosyada oldugunu tam olarak soyluyor, o yuzden files_to_fix'i
    # dogrudan lint_issues'un anahtarlarindan olusturuyoruz - asagidaki
    # traceback-tabanli dallara hic girmeden.
    if error_type == "lint_error" and lint_issues:
        files_to_fix = sorted(lint_issues.keys())
    elif error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    elif error_type == "output_missing":
        # DUZELTME (2026-09-23, 3. canli web_scraper testi): "output_missing"
        # durumunda gercek hatanin GIRIS DOSYASINDA olacagi varsayimi HER ZAMAN
        # dogru degil - bir onceki calistirmada asil kusurlu satir (yanlis db
        # dosya adi, sahte retry, vs.) database.py/helpers.py gibi baska bir
        # dosyada da olabilir. Traceback olmadigi icin _parse_traceback hicbir
        # sey bulamaz; sadece entry_point'i duzeltmeye calismak, hata GERCEKTEN
        # baska bir dosyadaysa 5 denemeyi de bosa harcar. Bu yuzden entry_point
        # ile birlikte, kalicilik/veri yazma ile ilgili anahtar kelimeler
        # gecen TUM dosyalari da adaylara ekliyoruz.
        files_to_fix.append(entry_point)
        _persistence_hints = ("sqlite3", ".db", "open(", "to_csv", "to_excel", "json.dump", "save_to", "requests.post")
        for fp, code in file_codes.items():
            if fp != entry_point and any(hint in code for hint in _persistence_hints):
                files_to_fix.append(fp)
    elif error_type == "runtime_error":
        # DUZELTME (Yama 17, 2026-09-23, 9. canli web_scraper testi): GERCEK
        # canlida gozlemlendi - bir istisna YAKALANIP print edilirse (orn.
        # `except requests.RequestException as e: print(f"Failed to fetch
        # {url}: {e}")`), cikti "403 Client Error: Forbidden" gibi bir metin
        # icerir ("error:" gectigi icin _classify_error bunu dogru sekilde
        # "runtime_error" sayar) AMA hicbir zaman gercek bir Python
        # traceback'i ("File ..., line N") ICERMEZ - cunku hata zaten
        # yakalanmis, hic firlatilmamistir. Bu durumda _parse_traceback
        # HICBIR SEY bulamadigi icin error_file HER ZAMAN None olur ve
        # (bu dal olmadan) asagidaki son "else" SADECE entry_point'i
        # hedefler. Ama asil bozuk kod (orn. eksik User-Agent header'i
        # yuzunden 403 alan requests.get cagrisi) COGU ZAMAN entry_point'te
        # DEGIL, cagriyi yapan yardimci dosyada (utils/helpers.py gibi)
        # bulunur. Canli testte GOZLEMLENDI: LLM 4 kez ust uste main.py'yi
        # "duzeltti" ama zaten dogru oldugu icin HER SEFERINDE BAYT BAYT
        # AYNI kodu geri uretti - gercek bozuk dosyaya (helpers.py) HICBIR
        # ZAMAN dokunulmadi, 5 denemenin tamami bosa gitti. Bu yuzden
        # "output_missing" ile AYNI mantik: entry_point ile birlikte, ag/IO
        # cagrisi barindiran TUM dosyalari da adaylara ekliyoruz.
        files_to_fix.append(entry_point)
        _no_traceback_hints = (
            "requests.get(", "requests.post(", "requests.put(", "requests.delete(",
            "requests.patch(", "requests.head(", "requests.Session(",
            "urlopen(", "httpx.", ".raise_for_status(",
        )
        for fp, code in file_codes.items():
            if fp != entry_point and any(hint in code for hint in _no_traceback_hints):
                files_to_fix.append(fp)
        # Bir fonksiyonun çağrıldığı dosya ile tanımlandığı dosya arasındaki
        # sözleşme uyuşmazlığında traceback çoğu zaman yalnızca çağıranı
        # gösterir. Örneğin worker.py, fetch_page() imzasına uymayan bir
        # keyword gönderirse sadece main.py'yi düzeltmek aynı hatayı tekrarlar.
        if any(token in error_output.lower() for token in (
            "unexpected keyword", "unexpected key", "got an unexpected"
        )):
            for fp in file_codes:
                if fp not in files_to_fix:
                    files_to_fix.append(fp)
    else:
        files_to_fix.append(entry_point)

    undefined_note = _undefined_name_note(error_output, file_codes)
    if undefined_note:
        # Canlı test 2026-09-29 (tarihe_gore_ayir): "Hata: name 'src' is not
        # defined" yakalanıp yazdırıldığı için traceback yoktu; model 5 tur
        # yalnız main.py'yi düzeltti. Adın geçtiği dosyalar da düzeltilir.
        for fp in undefined_note[1]:
            if fp not in files_to_fix:
                files_to_fix.append(fp)
        error_output = error_output + "\n\n" + undefined_note[0]

    if repeat_of_previous:
        # Canlı test 2026-09-29 (js_kazima): hata main.py:29'da görünüyordu ama
        # traceback'in en derin proje dosyası helpers.py olduğu için 4 tur
        # boyunca YALNIZ helpers.py düzeltildi, hata birebir tekrarlandı.
        # Aynı hata tekrarlanıyorsa traceback'teki TÜM proje dosyaları
        # (çağıran taraf dahil) düzeltmeye açılır.
        for fp in _traceback_project_files(error_output, list(file_codes.keys())):
            if fp not in files_to_fix:
                files_to_fix.append(fp)

    if import_mismatch and import_mismatch["target_path"] not in files_to_fix:
        # Hedef modulu (import EDILEN, ismi eksik olan dosya) de kendi
        # ayri duzeltme denemesini alsin - sadece error_file'in "read-only"
        # baglaminda 1500 karaktere kirpilmis halde gorunmesin.
        files_to_fix.append(import_mismatch["target_path"])

    if circular_import:
        # Yama 20: dongudeki TUM dosyalar (sadece error_file/entry_point
        # degil) kendi ayri duzeltme denemesini alsin - dongu ancak
        # dongudeki dosyalardan EN AZ BIRI degistirilirse kirilabilir.
        for fp in circular_import:
            if fp not in files_to_fix:
                files_to_fix.append(fp)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        import_mismatch_note = ""
        if import_mismatch:
            names_list = ", ".join(import_mismatch["actual_names"]) or "(no top-level functions/classes/variables found in that file at all)"
            import_mismatch_note = (
                f"\n\nIMPORT NAME MISMATCH DETECTED (this is very likely the real bug): "
                f"some file does `from {import_mismatch['target_module']} import "
                f"{import_mismatch['missing_name']}`, but {import_mismatch['target_path']} "
                f"does NOT define anything called '{import_mismatch['missing_name']}'. "
                f"The names ACTUALLY defined at the top level of "
                f"{import_mismatch['target_path']} are: {names_list}. Fix this by either "
                f"(a) if you are editing the file that imports it, change the import "
                f"statement to use whichever of these existing names provides the same "
                f"functionality, or (b) if you are editing {import_mismatch['target_path']} "
                f"itself, add or rename a function/class so that "
                f"'{import_mismatch['missing_name']}' actually exists there with the "
                f"expected behavior. Pick exactly one of these two fixes — do not leave "
                f"the same mismatched name in place, and do not fix it in both files "
                f"(that would just create a new, different mismatch)."
            )

        circular_import_note = ""
        if circular_import and fix_path in circular_import:
            chain_desc = " -> ".join(circular_import.keys())
            circular_import_note = (
                f"\n\nCIRCULAR IMPORT DETECTED (this is very likely the real "
                f"bug): {chain_desc} — these project files import each other "
                f"at MODULE LOAD TIME, which crashes with \"cannot import "
                f"name ... from partially initialized module ...\". Fix by "
                f"moving the import that this specific file ({fix_path}) "
                f"doesn't strictly need at load time to be LOCAL to the "
                f"function/method that actually uses it (import it inside "
                f"that function body instead of at the top of the file), or "
                f"remove it entirely if it was added unnecessarily (for "
                f"example, a file should almost never import and launch the "
                f"application's own entry point from its own __main__ "
                f"block — that responsibility belongs to the entry point "
                f"alone)."
            )

        lint_issues_note = ""
        if lint_issues and fix_path in lint_issues:
            lines_desc = "\n".join(
                f"  - Line {iss.get('line')}, col {iss.get('col')}: [{iss.get('code')}] {iss.get('message')}"
                for iss in lint_issues[fix_path]
            )
            lint_issues_note = (
                f"\n\nSTATIC ANALYSIS FOUND THESE LIKELY RUNTIME BUGS in this exact "
                f"file, found BEFORE the program was ever run:\n{lines_desc}\n"
                f"A code like NameError/UnboundLocalError/E9xx (from ruff) means a name "
                f"that does not exist at that point in the code, or a genuine syntax "
                f"problem — fix it precisely. A code like GUI-MANUAL-ONLY-TRIGGER means "
                f"the described method is only reachable via a button/menu click and is "
                f"never also invoked automatically — fix it exactly as the message "
                f"describes, while keeping the button itself working too."
            )

        repeat_note = ""
        if repeat_of_previous:
            # Gercek 14. testte gozlemlendi (NewsAggregatorLinux14, 2026-09-24):
            # bir arguman-sayisi uyusmazligi (TypeError) 3 deneme boyunca
            # BIREBIR AYNI hatayla cakildi - fixer her seferinde ayni (hala
            # bozuk) prompt'u goruyor ve dusuk sicaklikli yerel bir model
            # (Ollama/qwen2.5-coder) buyuk ihtimalle ayni (hala yanlis)
            # kodu tekrar uretiyor. Onceki deneme BASARISIZ oldugunu ve
            # AYNI SEYI TEKRARLAMAMASI gerektigini modele acikca soylemek,
            # onu farkli/daha dikkatli bir cozume itmek icin.
            repeat_note = (
                "\n\n⚠️ If patching keeps failing, REWRITE this file from scratch following the PROVEN "
                "PATTERNS shown above (keep the same public function names/signatures so other files "
                "still work) instead of editing the broken version again."
                "\n\n⚠️ REPEATED FAILURE WARNING: your PREVIOUS fix attempt for this "
                "exact file did NOT resolve the problem — running the project again "
                "produced the EXACT SAME error output as before, byte-for-byte. This "
                "means your last change either didn't touch the real bug, or "
                "reintroduced it. Do NOT repeat the same edit again. Look very "
                "carefully and precisely at the traceback: check that every function "
                "call's argument COUNT and ORDER exactly matches that function's real "
                "signature in the file where it is defined (a common cause of this "
                "exact situation is one file calling a method with a different "
                "number of arguments than the method actually accepts), and check "
                "for any other cross-file assumption that might be wrong."
            )

        shared_contracts_block = (
            "Shared data contracts ALL files must follow EXACTLY:\n" + shared_contracts
        ) if shared_contracts else ""

        expected_outputs_block = (
            "Files this project MUST create or update on disk when it runs, with the "
            "EXACT relative path required (never write to a different filename than "
            "these, even one that seems more fitting to the project's theme):\n"
            + expected_outputs
        ) if expected_outputs else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

{shared_contracts_block}

{expected_outputs_block}

{_recipes_for_fix(project_description, language)}
File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}
{import_mismatch_note}
{circular_import_note}
{lint_issues_note}
{repeat_note}
{web_context}
Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.
- Double-check: every name you use that comes from a module (tk.ttk, filedialog, messagebox, etc.) must have its own explicit import statement — "import tkinter as tk" alone does NOT make "ttk" or other submodules available as bare names.
- NEVER import a package named "jarvis" or anything resembling it — this is a standalone project with no relationship to any AI assistant framework, and no such package exists here.
- NEVER call a blocking modal dialog function (messagebox.showinfo/showerror/askyesno/etc., simpledialog.ask...) from the automatic startup path — it opens a real window and blocks forever waiting for a human click that will never come during automated verification. Print/log instead.
- EVERY network call (requests.get/post/etc., a Session's own get/post, urllib, httpx...) MUST have an explicit timeout= — a call with none can hang the whole program forever on one slow server with no Python error at all.
- NEVER leave stub functions (only docstring + pass / return [] / return None) — implement the real logic. Exactly ONE `if __name__ == "__main__":` block, at the end of the file.
- Inside `with open(...) as NAME:` never reuse NAME as a loop/assignment variable.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            full_path = _safe_project_path(project_dir, fix_path)
            if full_path is None:
                print(f"[DevAgent] 🛑 Güvenlik: düzeltme yolu ('{fix_path}') proje "
                      f"klasörü dışına çıkıyor, atlandı (path traversal koruması).")
                continue
            full_path.parent.mkdir(parents=True, exist_ok=True)
            if full_path.exists():
                from datetime import datetime as _dt
                stamp  = _dt.now().strftime("%Y%m%d-%H%M%S")
                # DUZELTME (2026-09-28): yedekler eskiden kaynak dosyanin YANINA
                # (main.<tarih>.bak.py) yaziliyordu; proje kokunu dolduruyordu.
                # Artik gizli .jarvis/backups/ klasorunde tutuluyor.
                backup_dir = project_dir / ".jarvis" / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / f"{fix_path.replace('/', '__')}.{stamp}.bak"
                # F-10: yedek BYTE duzeyinde (birebir kopya) - encoding
                # round-trip'i yedegin ORIJINALLE ayni olmasini bozabiliyordu.
                backup.write_bytes(full_path.read_bytes())
            full_path.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e)) from e
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _present_plan_for_review(plan: dict, log=print) -> str:
    """SUPERPOWERS A: PLAN-FIRST. Kod yazılmadan önce plan gösterilir."""
    files = plan.get("files", [])
    entry = plan.get("entry_point", "main.py")
    deps = plan.get("dependencies", [])
    outputs = plan.get("expected_outputs", [])
    parts = [f"PROJE PLANI ({len(files)} dosya):"]
    parts.append(f"  Giris: {entry}")
    if deps:
        shown = ", ".join(deps[:5])
        extra = f" (+{len(deps)-5})" if len(deps) > 5 else ""
        parts.append(f"  Bagimlilik: {shown}{extra}")
    for i, fi in enumerate(files, 1):
        fp = fi.get("path", "?") if isinstance(fi, dict) else str(fi)
        purpose = fi.get("purpose", fi.get("description", "")) if isinstance(fi, dict) else ""
        parts.append(f"  {i}. {fp}" + (f" - {purpose}" if purpose else ""))
    if outputs:
        parts.append("  Beklenen ciktilar:")
        for o in outputs:
            op = o.get("path", "?") if isinstance(o, dict) else str(o)
            od = o.get("description", "") if isinstance(o, dict) else ""
            parts.append(f"    * {op}" + (f": {od}" if od else ""))
    summary = "\n".join(parts)
    log(summary)
    return summary


def _generate_test_scaffold(plan: dict, project_dir: Path, log=print):
    """SUPERPOWERS B: TDD. Testler implementation'dan ONCE yazilir."""
    files = plan.get("files", [])
    entry = plan.get("entry_point", "main.py")
    plan.get("expected_outputs", [])

    mods = []
    for fi in files:
        fp = fi.get("path", "") if isinstance(fi, dict) else str(fi)
        if fp.endswith(".py") and not fp.startswith("test_"):
            mod = fp.replace("/", ".").replace("\\", ".").removesuffix(".py")
            mods.append(mod)

    tests = {}
    smoke_body = (
        '"""TDD: Smoke test - implementation ONCESI yazildi."""\n'
        "import importlib, os, sys\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))\n"
        f"MODULES = {mods!r}\n\n"
        "def test_imports():\n"
        "    fails = []\n"
        "    for m in MODULES:\n"
        "        try:\n"
        "            importlib.import_module(m)\n"
        "        except Exception as e:\n"
        "            fails.append(str(e))\n"
        "    assert not fails, fails\n\n"
        "def test_entry_exists():\n"
        "    root = os.path.join(os.path.dirname(__file__), '..')\n"
        f"    assert os.path.exists(os.path.join(root, '{entry}')), 'entry eksik'\n"
    )
    tests["tests/test_smoke.py"] = smoke_body

    syntax_body = (
        '"""TDD: Syntax test - her .py parse edilmeli."""\n'
        "import ast, glob, os\n\n"
        "def test_no_syntax_errors():\n"
        "    root = os.path.join(os.path.dirname(__file__), '..')\n"
        "    errs = []\n"
        "    for fp in glob.glob(os.path.join(root, '**', '*.py'), recursive=True):\n"
        "        if os.sep + 'tests' + os.sep in fp:\n"
        "            continue\n"
        "        try:\n"
        "            with open(fp) as f:\n"
        "                ast.parse(f.read())\n"
        "        except SyntaxError as e:\n"
        "            errs.append(str(e))\n"
        "    assert not errs, errs\n"
    )
    tests["tests/test_syntax.py"] = syntax_body

    for rp, content in tests.items():
        tp = project_dir / rp
        tp.parent.mkdir(parents=True, exist_ok=True)
        tp.write_text(content, encoding="utf-8")

    log(f"TDD: {len(tests)} test dosyasi ONCE yazildi (koddan once)")
    for rp in tests:
        log(f"  Test: {rp}")
    return tests


def _comprehensive_review(project_dir: Path, file_codes: dict, log=print):
    """SUPERPOWERS C: REVIEW. Kod calistirilmadan once guvenlik+kalite denetimi."""
    import ast as _ast
    issues = []
    SEC = {"exec": "KRITIK", "eval": "KRITIK", "compile": "YUKSEK",
           "__import__": "YUKSEK", "system": "YUKSEK", "popen": "YUKSEK"}
    for fp in sorted(file_codes):
        if not fp.endswith(".py"):
            continue
        cs = file_codes[fp]
        try:
            tree = _ast.parse(cs)
        except SyntaxError as e:
            issues.append(("KRITIK", fp, f"SyntaxError: {e}"))
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                fn = ""
                if isinstance(node.func, _ast.Name):
                    fn = node.func.id
                elif isinstance(node.func, _ast.Attribute):
                    fn = node.func.attr
                if fn in SEC:
                    issues.append((SEC[fn], fp, f"{fn}() satir {node.lineno}"))
            if isinstance(node, _ast.ExceptHandler) and node.type is None:
                issues.append(("ORTA", fp, f"bare except satir {node.lineno}"))
        if not cs.strip():
            issues.append(("KRITIK", fp, "dosya bos!"))
    crit = [i for i in issues if i[0] == "KRITIK"]
    high = [i for i in issues if i[0] == "YUKSEK"]
    med = [i for i in issues if i[0] == "ORTA"]
    if not issues:
        log("REVIEW: Tum dosyalar temiz (guvenlik + kalite)")
    else:
        log(f"REVIEW SONUCU: {len(crit)} kritik, {len(high)} yuksek, {len(med)} orta")
        for sev, fp, msg in issues:
            icon = {"KRITIK": "!!", "YUKSEK": "! ", "ORTA": "~ "}.get(sev, "  ")
            log(f"  [{icon}] [{sev}] {fp}: {msg}")
        if crit:
            log("UYARI: Kritik sorunlar var - duzeltilmeli!")
    return issues


RESULT_HEADER = "--- SONUÇ ---"
_MISSING_MODULE_RE = re.compile(r"No module named ['\"]([A-Za-z0-9_\.]+)['\"]")


def _build_succeeded(result: str) -> bool:
    return "is working" in result or "çalışıyor" in result


def _result_summary(result: str, project_dir: "Path | None") -> str:
    """Kurulum sonucunun sonuna eklenen sabit blok: durum, proje klasoru,
    varsa eksik moduller. Pano (agent_board) bu blogu ayri saklar."""
    if project_dir is None:
        found = re.search(re.escape(str(PROJECTS_DIR)) + r"[\\/][^\s'\"]+", result)
        project_dir = Path(found.group(0).rstrip(".,;:—)")) if found else None
    lines = [
        RESULT_HEADER,
        f"Durum: {'BAŞARILI' if _build_succeeded(result) else 'BAŞARISIZ'}",
        f"Proje klasörü: {project_dir if project_dir is not None else 'oluşturulmadı'}",
    ]
    missing = list(dict.fromkeys(_MISSING_MODULE_RE.findall(result)))
    if missing:
        lines.append(f"Eksik modül: {', '.join(missing)}")
    return "\n".join(lines)


def split_result_summary(result: str) -> "tuple[str, str]":
    """(sonuc blogu, govde). Blok yoksa ("", result)."""
    body, sep, summary = (result or "").rpartition("\n\n" + RESULT_HEADER)
    if not sep:
        return "", result or ""
    return RESULT_HEADER + summary, body


def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
    outcome: "dict | None" = None,
) -> str:

    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    # devkit (2026-09-28): Python disi diller (simdilik Go) kendi dil araç
    # zinciriyle (go build/vet/golangci-lint) kurulur ve asagidaki Python'a
    # ozgu akisa HIC girmez - bkz. jarvis/actions/devkit/__init__.py.
    from jarvis.actions.devkit import get_builder
    builder = get_builder(language)
    if builder is not None:
        return builder(
            description=description,
            project_name=project_name,
            timeout=timeout,
            generate=_devkit_generate,
            projects_dir=PROJECTS_DIR,
            log=log,
            speak=speak,
            open_editor=_open_vscode,
            is_rate_limit=_is_rate_limit,
        )

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    plan = _sanitize_plan_against_self_reference(plan)

    proj_name    = project_name or plan.get("project_name", "jarvis_project")
    proj_name    = re.sub(r"[^\w\-]", "_", proj_name)
    project_dir  = PROJECTS_DIR / proj_name
    # DUZELTME (2026-09-28): ayni adla yeni proje kurulurken eski denemenin
    # dosyalari (eski main.py, utils/...) klasorde kalip yenileriyle karisiyordu.
    # Eski klasor SILINMEZ, PROJECTS_DIR/.arsiv altina tasinir.
    if project_dir.is_dir() and any(project_dir.iterdir()):
        archive_root = PROJECTS_DIR / ".arsiv"
        archive_root.mkdir(parents=True, exist_ok=True)
        archived = archive_root / f"{proj_name}-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            shutil.move(str(project_dir), str(archived))
            log(f"📦 Aynı adlı önceki proje arşivlendi: {archived}")
        except OSError as exc:
            log(f"⚠️ Önceki proje arşivlenemedi ({exc}); dosyalar karışabilir.")
    project_dir.mkdir(parents=True, exist_ok=True)
    if outcome is not None:
        outcome["project_dir"] = project_dir

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])
    shared_contracts_text = "\n".join(f"- {c}" for c in plan.get("shared_data_contracts", []) if c)
    expected_outputs = plan.get("expected_outputs", [])
    expected_outputs_text = "\n".join(
        f"- {(o.get('path') if isinstance(o, dict) else str(o))}"
        + (f": {o.get('description', '')}" if isinstance(o, dict) and o.get("description") else "")
        for o in expected_outputs if o
    )
    gui_task = bool(re.search(r"\b(?:tkinter|tk\.)\b", description, re.IGNORECASE))

    # KABUL TESTI (2026-09-28): program, cevabi onceden bilinen kucuk bir
    # ornek uzerinde de calistirilip "istenen isi DOGRU yapiyor mu" diye
    # sinanir - bkz. jarvis/actions/devkit/acceptance.py. Spesifikasyon
    # uretilemezse/gecersizse test ATLANIR, build engellenmez.
    acceptance_spec = None if gui_task else _plan_acceptance(description, plan, log)
    acc_last: tuple[str, int] | None = None   # (son kabul hatası, o anki kodun imzası)
    acc_judged = False
    acceptance_disputed = ""
    soft_quality_rounds = 0
    quality_warnings = ""
    if acceptance_spec:
        from jarvis.actions.devkit.acceptance import contract_text
        shared_contracts_text = (shared_contracts_text + "\n- " + contract_text(acceptance_spec)).strip()

    # Aynı proje adıyla yapılan tekrar denemelerde eski database.db/report
    # dosyası yeni çalışmanın sonucu gibi görünmemeli. Yalnızca planner'ın
    # beklenen çıktı listesinde bulunan, proje içindeki dosyalar temizlenir;
    # kaynak dosyalarına veya proje dışına dokunulmaz.
    for output in expected_outputs:
        output_path = output.get("path") if isinstance(output, dict) else str(output)
        full_output = _safe_project_path(project_dir, output_path)
        if full_output and full_output.is_file():
            try:
                full_output.unlink()
                log(f"🧹 Eski beklenen çıktı temizlendi: {output_path}")
            except OSError as exc:
                log(f"⚠️ Eski çıktı temizlenemedi: {output_path} ({exc})")

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    # SUPERPOWERS (2026-10-03): Plan-first + TDD
    _present_plan_for_review(plan, log=log)
    _generate_test_scaffold(plan, project_dir, log=log)

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                    shared_contracts=shared_contracts_text,
                    expected_outputs=expected_outputs_text,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    # DUZELTME (2026-09-23, web_scraper_pro canli testi - bkz.
    # _proactively_fix_cross_file_imports docstring'i): proje ILK KEZ
    # calistirilmadan ONCE, tum dosyalarin birbirinden yaptigi importlari
    # ucretsiz bir statik analiz gecisiyle dogrula/duzelt - boylece birden
    # fazla ardisik import-isim uyusmazligi, pahali calistir-basarisiz-ol
    # dongusunu (ve MAX_FIX_ATTEMPTS butcesini) tuketmeden, tek seferde
    # yakalanip cozulsun.
    proactively_fixed = _proactively_fix_cross_file_imports(project_dir, file_codes)
    if proactively_fixed:
        log(f"İlk çalıştırmadan önce {len(proactively_fixed)} dosyadaki import isim uyuşmazlığı proaktif olarak düzeltildi: {proactively_fixed}")

    # DUZELTME (2026-09-23, web_scraper canli testi - bkz.
    # _add_missing_request_timeouts docstring'i): dogrudan requests.get/post
    # vb. cagrilarinda timeout= eksikse, ilk calistirmadan once ekle - tek
    # bir yavas/askida kalan ag istegi, hicbir Python hatasi vermeden
    # dev_agent'in TUM zaman asimi butcesini (30sn + 90sn) tuketebiliyordu.
    timeout_fixed = _proactively_add_request_timeouts(project_dir, file_codes)
    if timeout_fixed:
        log(f"İlk çalıştırmadan önce {len(timeout_fixed)} dosyadaki eksik HTTP timeout'u proaktif olarak eklendi: {timeout_fixed}")

    # DUZELTME (Yama 16): dogrudan requests.get/post vb. cagrilarinda
    # 'headers=' hic yoksa varsayilan bir tarayici User-Agent'i ekle - bkz.
    # _add_missing_user_agent_header docstring'i. Bircok gercek site
    # (Wikipedia dahil) varsayilan python-requests User-Agent'ini 403 ile
    # reddediyor; bu bir Python hatasi olmadigi icin _classify_error bunu
    # traceback'siz bir "runtime_error" olarak gorur ve (Yama 17 olmadan)
    # dogru dosyayi hic bulamazdi.
    user_agent_fixed = _proactively_add_user_agent_headers(project_dir, file_codes)
    if user_agent_fixed:
        log(f"İlk çalıştırmadan önce {len(user_agent_fixed)} dosyadaki eksik User-Agent header'ı proaktif olarak eklendi: {user_agent_fixed}")

    # DUZELTME (Yama 14): ruff ile genis kapsamli, yuksek-guven statik analiz
    # (tanimsiz isim/syntax) - bkz. _proactively_lint_generated_files
    # docstring'i. ruff bu ortamda kullanilamiyorsa (kurulu degil/kurulamadi)
    # SESSIZCE None doner, build hicbir sekilde engellenmez.
    lint_issues = _proactively_lint_generated_files(project_dir, file_codes) or {}

    # DUZELTME (Yama 15): "buton olmadan asla calismayan GUI" tespiti - bkz.
    # _detect_gui_manual_only_trigger docstring'i. Ayni {dosya: [bulgu,...]}
    # sekli oldugu icin ruff bulgularinin AYNI sozluguyle birlestirilip TEK
    # bir _fix_files cagrisinda (mumkunse tek bir model isteginde) hem statik
    # analiz hem bu davranissal sorun cozdurulebiliyor.
    gui_trigger_issues = _proactively_detect_gui_manual_only_triggers(file_codes) or {}
    for _fp, _issues in gui_trigger_issues.items():
        lint_issues.setdefault(_fp, []).extend(_issues)

    # DUZELTME (Yama 20): dongusel import tespiti - bkz. _detect_circular_imports
    # docstring'i. AYNI birlesik {dosya: [bulgu,...]} sekli, bu yuzden ruff ve
    # GUI-tetikleyici bulgularinin YANINA eklenip TEK bir _fix_files
    # cagrisinda birlikte cozdurulebiliyor. Bu, dongu DAHA ILK yazimda
    # olustuysa bile (12. testte oldugu gibi bir REAKTIF duzeltme sirasinda
    # DEGIL) ilk calistirma denemesi hic harcanmadan yakalanmasini saglar.
    circular_import_issues = _detect_circular_imports(file_codes) or {}
    for _fp, _issues in circular_import_issues.items():
        lint_issues.setdefault(_fp, []).extend(_issues)

    # Kalite kapısı (2026-09-28): içi boş fonksiyon, çift __main__, with-hedefi
    # ezme - bkz. jarvis/actions/devkit/python_quality.py.
    for _fp, _issues in _python_quality_issues(file_codes).items():
        lint_issues.setdefault(_fp, []).extend(_issues)

    # Plan Playwright dediği hâlde yazar model Selenium kullandı mı? (canlı test
    # 2026-09-28: 5 denemenin hepsinde Selenium'un Chrome hatası yamalandı.)
    for _fp, _issues in _selenium_instead_of_playwright(file_codes, dependencies).items():
        lint_issues.setdefault(_fp, []).extend(_issues)

    if lint_issues:
        affected = ", ".join(sorted(lint_issues.keys()))
        log(f"İlk çalıştırmadan önce statik analizle olası çalışma-zamanı/davranış hatası tespit edildi ({affected}), model ile düzeltiliyor (bir çalıştırma denemesi harcanmadan)...")
        try:
            lint_fixed = _fix_files(
                error_output="Static analysis found likely runtime bugs before the program was ever run — see per-file details below.",
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
                shared_contracts=shared_contracts_text,
                expected_outputs=expected_outputs_text,
                known_error_type="lint_error",
                lint_issues=lint_issues,
            )
            file_codes.update(lint_fixed)
        except RateLimitError:
            log("Rate limit - ruff proaktif düzeltmesi atlandı, normal çalıştırma denemesine geçiliyor.")

    # GUI projeleri otomatik doğrulanabilir bir yol taşımak zorunda. Sadece
    # butonlu bir Tkinter uygulaması dev-agent tarafından başlatıldığında
    # pencere açık kalır, fakat scraping/database işi hiç başlamayabilir. Bu
    # kontrol, yukarıdaki proaktif/statik düzeltmeler (ruff, GUI-tetikleyici,
    # dongusel import) BİTTİKTEN SONRA, dosyaların NİHAİ halinde çalışır -
    # boylece o duzeltmelerin ekledigi/degistirdigi kod da hesaba katilir.
    def _headless_mode_present() -> bool:
        return "--headless-test" in "\n".join(file_codes.values())

    headless_unverifiable = False
    if gui_task and expected_outputs and not _headless_mode_present():
        log("GUI projesi somut beklenen çıktılara sahip ama hiçbir dosyada --headless-test modu yok; odaklı bir düzeltme isteniyor...")
        try:
            headless_fixed = _fix_files(
                error_output=(
                    "This GUI project has concrete expected outputs but NO --headless-test "
                    "mode exists anywhere in the generated source. Add a complete argparse "
                    "--headless-test mode that performs the FULL real workflow (the same work "
                    "the GUI button triggers) WITHOUT creating a Tk root window or calling "
                    "mainloop(), and prints measurable completion/results (e.g. processed/"
                    "successful/failed/database_records) so an automated harness can verify "
                    "success. CRITICAL: --headless-test must NEVER read its input from a live "
                    "GUI widget (Entry.get(), Text.get(), StringVar, etc.); use deterministic "
                    "sample/default input so the real workflow is exercised end to end. "
                    "Shared worker/business-logic functions called by --headless-test must "
                    "never construct, reference, or update Tk widgets or the Tk root (no "
                    "Label(root, ...), widget.config(), root.update_idletasks(), or global "
                    "root lookup). Refactor shared logic to perform the real work and return "
                    "plain data; only the GUI callback wrapper may update widgets. Compute "
                    "processed/successful/failed/database_records from actual return values "
                    "or a database query, never hardcoded literals. Print FAILED when any "
                    "expected condition is unmet; print SUCCESS only when the complete "
                    "workflow and database verification succeed. Keep the interactive GUI "
                    "unchanged for normal use."
                ),
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
                shared_contracts=shared_contracts_text,
                expected_outputs=expected_outputs_text,
                known_error_type="lint_error",
                lint_issues={entry_point: [{
                    "code": "GUI-HEADLESS-VERIFICATION-MISSING",
                    "message": (
                        "This GUI project has concrete expected outputs but no "
                        "--headless-test mode. Add argparse --headless-test that runs the "
                        "complete workflow without opening Tk/mainloop, prints processed/"
                        "successful/failed/database_records and prints SUCCESS only when "
                        "everything succeeds. --headless-test must drive the real workflow "
                        "with deterministic sample/default input, never by reading an empty "
                        "GUI widget (Entry/Text/StringVar). Shared worker functions must "
                        "never touch a Tk widget or the Tk root; refactor them to return "
                        "plain data instead, and let only the GUI callback update widgets. "
                        "The printed processed/successful/failed/database_records values "
                        "must come from the real run or a database query, never hardcoded "
                        "literals. Keep the interactive GUI and its Start button working "
                        "normally."
                    ),
                    "line": 0,
                    "col": 0,
                }]},
            )
            if headless_fixed:
                file_codes.update(headless_fixed)
        except RateLimitError:
            log("Rate limit - headless doğrulama modu düzeltmesi atlandı.")
        except Exception as e:
            log(f"Headless doğrulama düzeltmesi başarısız: {e}")

        if not _headless_mode_present():
            headless_unverifiable = True
            log("⚠️ Odaklı düzeltmeden sonra da --headless-test modu üretilemedi; GUI sahte bir doğrulama gibi ASLA başlatılmayacak.")

    if headless_unverifiable:
        msg = (
            f"'{proj_name}' projesi için dosyalar {project_dir} içine kaydedildi, ancak "
            f"otomatik doğrulama ATLANDI, efendim: bu GUI projesi somut beklenen çıktılara "
            f"sahip ama modelden istenen --headless-test modu üretilemedi (kota sınırı da "
            f"olabilir). GUI'yi sahte bir doğrulama denemesi olarak başlatmadım - projeyi "
            f"elle VSCode'dan kontrol edin."
        )
        if speak:
            speak(msg)
        return msg

    if gui_task and _headless_mode_present() and "--headless-test" not in run_command:
        run_command = f"{run_command} --headless-test"
        log(f"Headless doğrulama komutu seçildi: {run_command}")

    if dependencies:
        install_result = _install_dependencies(dependencies, project_dir)
        log(install_result)
        if install_result.startswith(("Install warning", "Install error", "Dependency install timed out")):
            msg = (
                f"'{proj_name}' projesi için bağımlılıklar kurulamadı; çalışma doğrulaması yapılmadı. "
                f"Dosyalar {project_dir} içinde duruyor."
            )
            if speak:
                speak(msg)
            return f"{msg}\n\n{install_result}"

    _open_vscode(project_dir)

    last_output      = ""
    auto_installs    = 0
    timeout_extended = False
    current_timeout  = timeout
    previous_fix_error_output: str | None = None

    for attempt in range(1, MAX_FIX_ATTEMPTS + 1):
        # SUPERPOWERS (2026-10-03): Review (calistirma oncesi)
        _comprehensive_review(project_dir, file_codes, log=log)

        log(f"Running project (attempt {attempt}/{MAX_FIX_ATTEMPTS})...")
        run_started_at = time.time()
        last_output = _run_project(run_command, project_dir, current_timeout)
        log(f"Output preview: {last_output[:150]}")

        if last_output.startswith("REFUSED:"):
            # Bu bir kod hatasi degil, bir GUVENLIK reddi - self-fix dongusune
            # asla girmez (model farkli bir yikici komut denemeye kalkabilir).
            # Dogrudan, durumu oldugu gibi kullaniciya bildirerek durur.
            msg = (
                f"'{proj_name}' projesi için üretilen çalıştırma komutu yıkıcı bir "
                f"kalıp içerdiği için ÇALIŞTIRILMADI (güvenlik reddi), efendim. "
                f"Dosyalar {project_dir} içinde duruyor, elle kontrol etmeniz gerekiyor."
            )
            if speak: speak(msg)
            return f"{msg}\n\n{last_output}"

        is_timeout = last_output.startswith("Timed out")

        if is_timeout and not timeout_extended and attempt < MAX_FIX_ATTEMPTS:
            # _has_error() timeout'u kasitli olarak hata SAYMIYOR (bir sunucu/
            # GUI kasitli olarak surekli calisabilir), AMA bu hicbir sey
            # DOGRULANMADI demektir - antivirus/soguk-import gecikmesi ya da
            # gercekten takili kalmis bozuk bir betik de ayni ciktiyi verir
            # (2026-09-21'de canli testte gozlemlendi: Norton 360 taramasi
            # yuzunden ilk import 30sn'yi asti). Once, henuz kullanilmadiysa,
            # BIR KEZ uzatilmis timeout ile tekrar denenir - fresh bir pip
            # install sonrasi soguk import gecikmesini karsilamak icin.
            timeout_extended = True
            current_timeout = timeout * 3
            log(f"Zaman asimi - {current_timeout}s ile bir kez daha deneniyor (soguk import/antivirus taramasi olabilir)...")
            time.sleep(1)
            continue

        has_crash_error = _has_error(last_output, run_command)

        # DUZELTME (2026-09-23, web_scraper canli testi): "cokmedi" ile
        # "gercekten dogru calisti" AYNI SEY DEGIL. O testte program hicbir
        # Python hatasi vermeden calisip bitti ("Scraping completed" yazdi),
        # ama gercekte hicbir satir veritabanina yazilmamisti. _has_error()
        # SADECE Python traceback'lerini arar, boyle sessiz/mantik
        # hatalarini asla goremez. Plan "expected_outputs" bildirdiyse, o
        # dosyalarin bu calistirmada GERCEKTEN olusup/guncellenip
        # guncellenmedigini kendimiz kontrol ediyoruz.
        output_problems = []
        if expected_outputs and not has_crash_error:
            output_problems.extend(
                _check_expected_outputs(project_dir, expected_outputs, run_started_at)
            )
            if not output_problems:
                output_problems.extend(_check_output_contents(project_dir, expected_outputs))
        if is_timeout and output_problems and not gui_task:
            # Canlı test 2026-09-29: JS kazıyıcı sonsuz kaydırma döngüsünde
            # takıldı; model yalnızca "çıktı yok" duyduğu için 4 denemede de
            # asıl sorunu (bitmeyen döngü) görmedi ve her deneme 360 sn sürdü.
            output_problems.insert(0, (
                f"The program NEVER FINISHED — it was killed after {current_timeout}s. This is a batch task, "
                f"not a server/GUI: it is stuck in an endless loop or waiting forever (e.g. scrolling an "
                f"infinite-scroll page until 'no new content' which never happens, waiting for a selector that "
                f"never appears, a retry loop without a limit). Add a HARD STOP: stop as soon as the requested "
                f"number of items is collected, cap loops with a maximum iteration count, and give every wait a "
                f"timeout. Write the output file as soon as the data is collected."
            ))
            current_timeout = timeout  # uzatılmış süre yalnızca soğuk başlangıç içindi
        if output_problems:
            log(f"Program çökmedi ama beklenen çıktı üretilmedi: {output_problems}")
            filename_mismatches = _detect_output_filename_mismatch(
                expected_outputs, "\n".join(file_codes.values())
            )
            if filename_mismatches:
                log(f"Dosya adı uyuşmazlığı tespit edildi: {filename_mismatches}")
            last_output = _format_output_problem_message(
                last_output, output_problems, file_codes.get(entry_point, ""), filename_mismatches
            )

        if not has_crash_error and not output_problems:
            # KALITE KAPISI (2026-09-28): "cokmedi + cikti dosyasi var" artik
            # YETMIYOR. Ici bos fonksiyon / cift __main__ / yalnizca basliktan
            # olusan rapor varsa proje BASARILI SAYILMAZ; duzeltme turuna girer.
            quality = _python_quality_issues(file_codes)
            from jarvis.actions.devkit.python_quality import (
                header_only_outputs,
                placeholder_data_outputs,
                truncated_value_outputs,
            )
            header_problems = header_only_outputs(project_dir, expected_outputs)
            for _p in truncated_value_outputs(project_dir, expected_outputs):
                quality.setdefault(entry_point, []).append(
                    {"code": "OUTPUT-TRUNCATED", "message": _p, "line": 0, "col": 0}
                )
            for _p in placeholder_data_outputs(project_dir, expected_outputs, description):
                quality.setdefault(entry_point, []).append(
                    {"code": "OUTPUT-PLACEHOLDER-DATA", "message": _p, "line": 0, "col": 0}
                )
            if header_problems:
                quality.setdefault(entry_point, []).extend(
                    {"code": "OUTPUT-HEADER-ONLY", "message": p, "line": 0, "col": 0} for p in header_problems
                )
            if quality and all(i["code"] in SOFT_QUALITY_CODES for iss in quality.values() for i in iss):
                # Canlı test 2026-09-29 (kitap_raporu): çıktısı doğru program yalnızca
                # "kullanılmayan fonksiyon" yüzünden 3 tur reddedildi. Tek başına
                # bu bulgu bir tur düzeltme hakkı alır; sonra uyarıya dönüşür.
                soft_quality_rounds += 1
                if soft_quality_rounds > SOFT_QUALITY_ROUNDS:
                    quality_warnings = "; ".join(
                        f"{fp}: " + ", ".join(i["code"] for i in iss) for fp, iss in quality.items())
                    log(f"ℹ️ Yalnızca küçük kod bulguları kaldı ({quality_warnings}); başarıyı engellemiyor.")
                    quality = {}
            if quality:
                summary = "; ".join(
                    f"{fp}: " + ", ".join(i["code"] for i in iss) for fp, iss in quality.items()
                )
                log(f"❌ Kalite kapısı geçilemedi ({summary}) — başarılı sayılmadı, düzeltiliyor...")
                last_output = f"QUALITY GATE FAILED: {summary}\n\nProgram output:\n{last_output}"
                if attempt == MAX_FIX_ATTEMPTS:
                    break
                try:
                    fixed = _fix_files(
                        error_output=(
                            "The program ran without crashing, but an automated quality gate found that it "
                            "does NOT really do its job (see per-file findings). Fix every finding."
                        ),
                        project_description=description,
                        all_files=files,
                        file_codes=file_codes,
                        language=language,
                        project_dir=project_dir,
                        entry_point=entry_point,
                        shared_contracts=shared_contracts_text,
                        expected_outputs=expected_outputs_text,
                        known_error_type="lint_error",
                        lint_issues=quality,
                        repeat_of_previous=(previous_fix_error_output == summary),
                    )
                    file_codes.update(fixed)
                    previous_fix_error_output = summary
                except RateLimitError:
                    msg = "Rate limit reached during quality fix. Project saved, check it manually in VSCode."
                    if speak: speak(msg)
                    return msg
                time.sleep(1)
                continue
            if acceptance_spec and not is_timeout:
                from jarvis.actions.devkit.acceptance import describe_fixtures, run_acceptance
                acc_problems, acc_output = run_acceptance(
                    project_dir, entry_point, acceptance_spec, timeout=max(30, timeout * 2)
                )
                if acc_problems and any("kendi klasörüne" in pr for pr in acc_problems):
                    from jarvis.actions.devkit.acceptance import rewrite_script_dir_paths
                    rewritten = rewrite_script_dir_paths(file_codes)
                    if rewritten:
                        backup_dir = project_dir / ".jarvis" / "backups"
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        stamp = time.strftime("%Y%m%d-%H%M%S")
                        for rel, code in rewritten.items():
                            target = project_dir / rel
                            if target.exists():
                                (backup_dir / f"{rel.replace('/', '__')}.{stamp}.bak").write_bytes(target.read_bytes())
                            target.write_text(code, encoding="utf-8")
                        file_codes.update(rewritten)
                        log(f"🔧 Çıktı yolu programın klasörü yerine çalışma klasörüne çevrildi (model yerine deterministik): {sorted(rewritten)}")
                        continue
                if acc_problems:
                    # Canlı test 2026-09-29 (hata_saatleri): modelin YAZDIĞI beklenti
                    # yanlıştı ('15,1'); program 5 farklı yazımda da aynı doğru
                    # sonucu verdi ve 5 tur boşa gitti. FARKLI kod aynı çıktıyı
                    # veriyorsa beklenti şüphelidir: bir kez hakem sorusu sorulur.
                    acc_sig = "\n".join(acc_problems)
                    code_sig = hash(tuple(sorted(file_codes.items())))
                    # Canlı test 2026-09-29 (2. tur): model "doğru" bulduğu kodu hiç
                    # değiştirmeyince (metin_istatistigi, yapilacaklar_raporu) hakem hiç
                    # sorulmadı. Artık AYNI başarısızlık ikinci kez görülünce sorulur;
                    # hakem yalnızca örnek girdiden kendi hesabıyla karar verir.
                    if (acc_last and acc_last[0] == acc_sig and not acc_judged
                            and not any("oluşturulmadı" in pr or "hata koduyla" in pr for pr in acc_problems)):
                        acc_judged = True
                        run_dir = project_dir / ".jarvis" / "acceptance" / "run"
                        prog_text = acc_output + "\n" + "\n".join(
                            (run_dir / e["output"]).read_text(encoding="utf-8", errors="replace")
                            for e in acceptance_spec["expect"]
                            if e["output"] != "STDOUT" and (run_dir / e["output"]).is_file()
                            and (run_dir / e["output"]).stat().st_size < 200_000)
                        wrong, why = _acceptance_expectation_wrong(
                            description, acceptance_spec, acc_problems, acc_output, prog_text)
                        if not wrong:
                            log(f"⚖️ Hakem: beklenti geçerli sayıldı ({why[:160]})")
                        if wrong:
                            log(f"⚖️ Kabul testi beklentisinin kendisi hatalı bulundu (farklı kod aynı sonucu verdi): {why[:200]}")
                            acceptance_disputed = why[:300]
                            acc_problems = []
                    acc_last = (acc_sig, code_sig)
                if acc_problems:
                    log(f"❌ Kabul testi geçilemedi: {acc_problems[0][:200]}")
                    last_output = "ACCEPTANCE TEST FAILED:\n" + "\n".join(acc_problems) + f"\n\nProgram output:\n{acc_output}"
                    if attempt == MAX_FIX_ATTEMPTS:
                        break
                    acc_issue = {
                        "code": "ACCEPTANCE-FAILED",
                        "message": (
                            "The program was run on this KNOWN sample input and gave a WRONG result. "
                            f"Command: python {entry_point} {' '.join(acceptance_spec['args'])} "
                            f"(<FIXTURE> = sample folder below). Problems: {' | '.join(acc_problems)[:1500]}\n"
                            f"Sample input files:\n{describe_fixtures(acceptance_spec, 2000)}"
                        ),
                        "line": 0, "col": 0,
                    }
                    try:
                        fixed = _fix_files(
                            error_output=last_output[:2500],
                            project_description=description,
                            all_files=files,
                            file_codes=file_codes,
                            language=language,
                            project_dir=project_dir,
                            entry_point=entry_point,
                            shared_contracts=shared_contracts_text,
                            expected_outputs=expected_outputs_text,
                            known_error_type="lint_error",
                            lint_issues={fp: [acc_issue] for fp in file_codes},
                            repeat_of_previous=(previous_fix_error_output == last_output),
                        )
                        file_codes.update(fixed)
                        previous_fix_error_output = last_output
                    except RateLimitError:
                        msg = "Rate limit reached during acceptance fix. Project saved, check it manually in VSCode."
                        if speak: speak(msg)
                        return msg
                    time.sleep(1)
                    continue
                if not acceptance_disputed:
                    log("✅ Kabul testi geçti (örnek girdide beklenen sonuçlar üretildi).")
            if is_timeout:
                if expected_outputs:
                    verified_note = f" Beklenen çıktılar gerçekten doğrulandı ({', '.join(str(o.get('path', o)) if isinstance(o, dict) else str(o) for o in expected_outputs)})."
                else:
                    verified_note = " AMA betiğin gerçekten doğru çalıştığını DOĞRULAYAMADIM, takılı kalmış da olabilir."
                msg = (
                    f"'{proj_name}' projesi {current_timeout} saniye içinde tamamlanmadı, efendim. "
                    f"Bu, kasıtlı olarak sürekli çalışan bir sunucu/GUI uygulaması olabilir.{verified_note} "
                    f"Dosyalar {project_dir} içinde duruyor, lütfen VSCode'dan elle kontrol edin."
                )
            else:
                verified_note = (
                    f" Verified outputs: {', '.join(str(o.get('path', o)) if isinstance(o, dict) else str(o) for o in expected_outputs)}."
                    if expected_outputs else ""
                )
                if quality_warnings:
                    verified_note += f" Küçük kod uyarıları (engel değil): {quality_warnings}."
                if acceptance_disputed:
                    verified_note += (" Not: örnek girdideki otomatik beklenti hatalı çıktı ve devre dışı bırakıldı "
                                      f"({acceptance_disputed[:160]}) — sonucu bir kez kontrol edin.")
                elif acceptance_spec:
                    verified_note += " Acceptance test on a known sample input passed."
                elif _is_live_scrape(description):
                    # Dış analiz raporu (2026-09-29): gerçek site görevlerinde içerik
                    # otomatik doğrulanamıyor; kullanıcıya bunu açıkça söyle.
                    verified_note += (" Not: çıktı içeriği gerçek siteye karşı otomatik doğrulanamadı "
                                      "(yalnızca dosya/biçim kontrolleri yapıldı) — ilk birkaç satıra bir göz atın.")
                msg = (
                    f"Project '{proj_name}' is working, sir. "
                    f"Built in {attempt} attempt{'s' if attempt > 1 else ''}.{verified_note} "
                    f"Saved to: {project_dir}"
                )
            if speak: speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        if attempt == MAX_FIX_ATTEMPTS:
            break

        error_type = "output_missing" if output_problems else _classify_error(last_output, project_dir)

        if error_type == "local_import_error":
            fixed = _try_fix_local_import(last_output, project_dir)
            if fixed:
                log("Yerel modül içe aktarma sorunu düzeltildi (eksik __init__.py), tekrar deneniyor...")
                time.sleep(1)
                continue

        if error_type == "dependency_error" and auto_installs < 3:
            installed = _try_auto_install(last_output, project_dir)
            if installed:
                auto_installs += 1
                log("Missing dependency installed, retrying...")
                time.sleep(1)
                continue

        if error_type == "runtime_error":
            fixed_typing = _try_fix_typing_import(last_output, project_dir, list(file_codes.keys()))
            if fixed_typing:
                log("Eksik 'typing' importu otomatik eklendi (model cagrilmadan), tekrar deneniyor...")
                time.sleep(1)
                continue

            fixed_tkinter = _try_fix_tkinter_submodule_import(last_output, project_dir, list(file_codes.keys()))
            if fixed_tkinter:
                log("Eksik 'tkinter' alt-modul importu otomatik eklendi (model cagrilmadan), tekrar deneniyor...")
                time.sleep(1)
                continue

        if error_type == "import_error":
            fixed_symbol = _try_fix_bad_symbol_import(last_output, project_dir, list(file_codes.keys()))
            if fixed_symbol:
                log("Yanlis/kirik isim importu otomatik duzeltildi (model cagrilmadan), tekrar deneniyor...")
                time.sleep(1)
                continue

        repeat_of_previous = (
            previous_fix_error_output is not None and last_output == previous_fix_error_output
        )
        if repeat_of_previous:
            log("⚠️ Önceki düzeltme denemesiyle BİREBİR AYNI hata tekrar oluştu - model bu kez uyarılıyor.")
        log(f"Fixing errors (type: {error_type})...")
        try:
            _sel = _selenium_instead_of_playwright(file_codes, dependencies)
            _extra = ((("\n\n" + _SELENIUM_HINT) if _sel else "") + _bare_filename_hint(last_output, run_command)
                      + _known_error_hint(last_output))
            updated = _fix_files(
                error_output=last_output + _extra,
                project_description=description,
                all_files=files,
                file_codes=file_codes,
                language=language,
                project_dir=project_dir,
                entry_point=entry_point,
                shared_contracts=shared_contracts_text,
                expected_outputs=expected_outputs_text,
                known_error_type=error_type,
                repeat_of_previous=repeat_of_previous,
            )
            file_codes.update(updated)
            previous_fix_error_output = last_output
            time.sleep(1)
        except RateLimitError:
            msg = "Rate limit reached during fix. Project saved, check it manually in VSCode."
            if speak: speak(msg)
            return msg
        except Exception as e:
            log(f"Fix step failed: {e}")

    msg = (
        f"I couldn't fully fix '{proj_name}' after {MAX_FIX_ATTEMPTS} attempts, sir. "
        f"Project is saved at {project_dir} — open it in VSCode and check manually."
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast error:\n{last_output[:600]}"


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    timeout      = int(p.get("timeout", 30))
    confirm_code = (p.get("confirm_code") or "").strip()

    if not description:
        return "Please describe the project you want me to build, sir."

    # EKSIK BILGI KAPISI: "[Hedef URL]" gibi doldurulmamis sablonlarla ya da
    # URL'si verilmemis bir kazima goreviyle plan yapilmaz; once sorulur.
    if not confirm_code:
        from jarvis.actions.devkit.task_intake import missing_inputs
        missing = missing_inputs(description)
        if missing:
            return (
                "BİLGİ EKSİK — proje BAŞLATILMADI: " + "; ".join(missing) + ". "
                "Kullanıcıya eksik bilgiyi (ör. gerçek web adresini) sor; cevabını aldıktan sonra "
                "açıklamayı gerçek değerle güncelleyip dev_agent'ı yeniden çağır. Değeri kendin uydurma."
            )

    # ONAY KAPISI: bu adim pip ile paket kurar ve modelin urettigi kodu
    # gercekten calistirir - confirm_code verilmeden hicbiri yapilmaz.
    if not confirm_code:
        _task_key = f"{(p.get('description',''))[:80]}|{p.get('language','')}|{p.get('project_name','')}"

        _existing_code = _task_to_code.get(_task_key)

        if _existing_code and _existing_code in _pending_dev_agent:

            code = _existing_code  # AYNI GOREV -> AYNI KOD

        else:

            code = secrets.token_hex(3)

            _task_to_code[_task_key] = code
        _pending_dev_agent[code] = {
            "description": description, "language": language,
            "project_name": project_name, "timeout": timeout,
            "issued_at": time.monotonic(),
        }
        installer = "go mod tidy" if language.strip().lower() in ("go", "golang") else "pip"
        return (
            f"ONAY GEREKLİ: \"{description}\" açıklamasıyla yeni bir {language} projesi "
            f"oluşturulacak. Bu adım gerekli paketleri {installer} ile kurar ve üretilen kodu "
            f"gerçekten çalıştırır. Kullanıcıya bunu tarif et; kullanıcı SESLİ/YAZILI olarak "
            f"açıkça onaylarsa (bir sonraki mesajında), dev_agent'ı aynı description/language/"
            f"project_name ile ve confirm_code='{code}' parametresiyle TEKRAR çağır. "
            f"Kullanıcı onaylamadan bu kodu kendi kendine kullanma. "
            f"Bu kodu olduğu gibi kullan — yeni kod üretmek GEREKMEZ."
        )
    # Kota/ağ/model hatasında aynı açık onayla tekrar denenebilsin. Eski akış
    # build başlamadan kodu siliyor, Gemini 429 sonrasında kullanıcıyı yeni
    # onay döngüsüne zorluyordu.
    problem = confirmation_problem(confirm_code)
    if problem:
        return problem
    pending = _pending_dev_agent[confirm_code]

    outcome: dict = {}
    result = _build_project(
        description  = pending["description"],
        language     = pending["language"],
        project_name = pending["project_name"],
        timeout      = pending["timeout"],
        speak        = speak,
        player       = player,
        outcome      = outcome,
    )
    if not result.startswith("Rate limit reached"):
        _pending_dev_agent.pop(confirm_code, None)
    # Sesli ozet (speak) degismez; yalnizca donen metne sabit sonuc blogu eklenir.
    return f"{result}\n\n{_result_summary(result, outcome.get('project_dir'))}"
