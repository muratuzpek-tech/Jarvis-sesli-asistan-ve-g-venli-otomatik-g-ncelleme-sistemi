import subprocess
import sys
import re
import time
import secrets
from pathlib import Path


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR           = get_base_dir()
API_CONFIG_PATH    = BASE_DIR / "config" / "api_keys.json"
DESKTOP            = Path.home() / "Desktop"
MAX_BUILD_ATTEMPTS = 3
GEMINI_MODEL       = "gemini-flash-latest"


def _get_api_key() -> str:
    from jarvis.core.secure_config import get_gemini_api_key
    return get_gemini_api_key()


def _get_gemini(model: str = GEMINI_MODEL):
    """Once yerel Ollama'yi (qwen2.5-coder) dener - Gemini kesinti/model
    hatalarinda bile calisir. Ollama yoksa otomatik Gemini'ye duser."""
    import requests as _requests

    OLLAMA_URL = "http://localhost:11434/api/generate"
    OLLAMA_MODEL = "qwen2.5-coder:7b"

    class _OllamaResponse:
        def __init__(self, text):
            self.text = text

    class _OllamaWrapper:
        def generate_content(self, contents):
            prompt = contents if isinstance(contents, str) else str(contents)
            resp = _requests.post(
                OLLAMA_URL,
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            return _OllamaResponse(resp.json().get("response", ""))

    try:
        tags_resp = _requests.get("http://localhost:11434/api/tags", timeout=5)
        tags_resp.raise_for_status()
        installed = {m.get("name") or m.get("model") for m in tags_resp.json().get("models", [])}
        if OLLAMA_MODEL not in installed:
            print(f"[Code] Yerel Ollama çalışıyor ama '{OLLAMA_MODEL}' modeli kurulu değil, Gemini'ye geçiliyor.")
            raise RuntimeError("ollama model not installed")
        print("[Code] Yerel Ollama kullanılıyor (Gemini'ye bağımlı değil).")
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
                    lambda: _c.models.generate_content(model=model, contents=contents),
                    breaker=_breaker,
                )
            except ModelFallbackNeeded as e:
                print(f"[Code] ⚠️ Model '{model}' kullanılamıyor (muhtemelen kaldırılmış): {e}")
                raise
            except AllAttemptsFailed as e:
                print(f"[Code] ❌ Gemini'ye ulaşılamıyor, tüm denemeler başarısız: {e.last_error}")
                raise

    return _W()


_GEMINI_BREAKER = None


def _get_gemini_breaker():
    global _GEMINI_BREAKER
    if _GEMINI_BREAKER is None:
        from jarvis.actions.resilience import CircuitBreaker
        _GEMINI_BREAKER = CircuitBreaker(name="gemini-codehelper", failure_threshold=3, cooldown_seconds=60)
    return _GEMINI_BREAKER


def _clean_code(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _resolve_save_path(output_path: str, language: str) -> Path:
    ext_map = {
        "python": ".py", "py": ".py",
        "javascript": ".js", "js": ".js",
        "typescript": ".ts", "ts": ".ts",
        "html": ".html", "css": ".css",
        "java": ".java", "cpp": ".cpp", "c": ".c",
        "bash": ".sh", "shell": ".sh", "powershell": ".ps1",
        "sql": ".sql", "json": ".json", "rust": ".rs", "go": ".go",
    }
    if output_path:
        p = Path(output_path)
        return p if p.is_absolute() else DESKTOP / p
    ext = ext_map.get((language or "python").lower(), ".py")
    return DESKTOP / f"jarvis_code{ext}"


# --- Yazma hedefi guvenlik politikasi -------------------------------------
#
# GERCEK RISK (denetim bulgusu F-01): _resolve_save_path() mutlak bir
# output_path'i (ör. "/etc/cron.d/x", "C:\\Program Files\\...") OLDUGU GIBI
# donduruyordu - hicbir izin kokune bagli degildi. code_helper "guvenilir ic
# bilesen" sayilmisti ama yol/model ciktisi aslinda kullanici/model girdisi.
# Asagidaki tek politika noktasi, file_controller._is_safe_path ile AYNI
# kurali (kullanici ev dizini disina cikma yasagi, sembolik link ile disari
# tasma REDDEDILIR) kod yazma yollarina da uygular.
def _is_within_home(path: Path) -> bool:
    try:
        resolved = path.resolve()
        home = Path.home().resolve()
        return resolved == home or resolved.is_relative_to(home)
    except Exception:
        return False


class UnsafeWriteTarget(Exception):
    """resolve_write_target() hedefin kullanici ev dizini disinda oldugunu
    tespit ettiginde firlatilir - diske HICBIR yazma yapilmadan."""


def resolve_write_target(output_path: str, language: str) -> Path:
    """TUM kod-yazma yollarinin (write/optimize'in yeni-dosya dali) GECTIGI
    TEK politika noktasi. Hedef kullanici ev dizini disindaysa YAZMADAN
    ONCE reddeder."""
    target = _resolve_save_path(output_path, language)
    if not _is_within_home(target):
        raise UnsafeWriteTarget(
            f"Güvenlik: '{target}' kullanıcı ana dizini dışında olduğu için "
            f"buraya yazma reddedildi. Lütfen Masaüstü, Belgeler veya proje "
            f"klasörünüz gibi ana dizin içinde bir konum belirtin."
        )
    return target


# Var olan bir dosyanin UZERINE YAZILMASI (edit/optimize), acik kullanici
# onayi olmadan otomatik yapilmaz (F-01: "code_helper ... mevcut dosyaları
# değiştirebiliyor"). Iki adimli onay, file_controller.move_file ile AYNI
# desendedir: ilk cagri hicbir dosyaya dokunmaz, onaylanmis icerigi saklayip
# kisa omurlu bir kod doner; gercek yazma SADECE dogru kodla olur.
_pending_code_edits: dict[str, tuple[Path, str]] = {}


def _confirm_and_save(path: Path, content: str, confirm_code: str = "") -> str:
    if not path.exists():
        # Yeni dosya - uzerine yazilacak mevcut icerik yok, dogrudan kaydet.
        return _save_file(path, content)
    if not confirm_code:
        code = secrets.token_hex(3)
        _pending_code_edits[code] = (path, content)
        return (
            f"ONAY GEREKLİ (henüz kaydedilmedi): '{path}' zaten var, üzerine "
            f"yazılacak. Kullanıcı SESLİ/YAZILI olarak açıkça onaylarsa, aynı "
            f"eylemi confirm_code='{code}' parametresiyle TEKRAR çağırın. "
            f"Kullanıcı onaylamadan bu kodu kendi kendine kullanma.\n\n"
            f"Önizleme:\n{_preview(content)}"
        )
    pending = _pending_code_edits.pop(confirm_code, None)
    if pending is None or pending[0] != path or pending[1] != content:
        return "Onay kodu geçersiz veya süresi dolmuş. Önce confirm_code vermeden çağırıp yeni önizleme/kod alın."
    return _save_file(path, content)


_RETRYABLE_WINERRORS = (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION


def _write_bytes_with_retry(path: Path, data: bytes, attempts: int = 3, base_delay: float = 0.2) -> None:
    """DUZELTME (denetim bulgusu F-03): dosya kilidi/OneDrive senkron
    catismalarinda kisa, sinirli exponential backoff ile yeniden dener;
    kalici basarisizlikta WinError 32/33'u kullaniciya anlamli bir mesaja
    cevirir (eskiden sadece genel 'Exception' ile yakalaniyordu)."""
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            path.write_bytes(data)
            return
        except OSError as exc:
            last_exc = exc
            winerror = getattr(exc, "winerror", None)
            retryable = winerror in _RETRYABLE_WINERRORS
            if not retryable or attempt == attempts - 1:
                if retryable:
                    raise RuntimeError(
                        f"Dosya başka bir uygulama veya OneDrive tarafından "
                        f"kullanılıyor: {path.name}. İlgili programı/senkronizasyonu "
                        f"kapatıp tekrar deneyin."
                    ) from exc
                if isinstance(exc, PermissionError) or winerror == 5:
                    raise RuntimeError(f"İzin reddedildi: {path.name}.") from exc
                raise
            time.sleep(base_delay * (2 ** attempt))
    if last_exc:
        raise last_exc


def _read_file(file_path: str) -> tuple[str, str]:
    if not file_path:
        return "", "No file path provided."
    p = Path(file_path)
    if not p.exists():
        return "", f"File not found: {file_path}"
    try:
        return p.read_text(encoding="utf-8"), ""
    except Exception as e:
        return "", f"Could not read file: {e}"


def _save_file(path: Path, content: str) -> str:
    try:
        print(f"[Code] 🔍 TEŞHİS: path={path!r}  path.exists()={path.exists()}")
        backup_note = ""
        if path.exists():
            from datetime import datetime
            stamp  = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(f"{path.stem}.{stamp}.bak{path.suffix}")
            # DUZELTME (denetim bulgusu F-10): yedek artik BYTE duzeyinde
            # aliniyor (read_bytes/write_bytes) - eskiden read_text(errors=
            # "replace") kullanildigi icin bozuk/farkli kodlamali dosyalarda
            # yedek, ORIJINALIN birebir kopyasi OLMUYORDU (sessiz veri kaybi).
            _write_bytes_with_retry(backup, path.read_bytes())
            backup_note = f" (yedek: {backup.name})"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_bytes_with_retry(path, content.encode("utf-8"))
        return f"Saved to: {path}{backup_note}"
    except Exception as e:
        print(f"[Code] 🔍 TEŞHİS hata: {type(e).__name__}: {e}")
        return f"Could not save: {e}"


def _preview(code: str, lines: int = 10) -> str:
    all_lines = code.splitlines()
    preview   = "\n".join(all_lines[:lines])
    suffix    = f"\n... ({len(all_lines) - lines} more lines)" if len(all_lines) > lines else ""
    return preview + suffix


def _has_error(output: str) -> bool:
    error_signals = ["error", "exception", "traceback", "syntaxerror",
                     "nameerror", "typeerror", "stderr", "failed", "crash"]
    return any(s in output.lower() for s in error_signals)


def _take_screenshot() -> Path | None:
    try:
        import pyautogui
        screenshot_path = Path.home() / "Desktop" / f"jarvis_debug_{int(time.time())}.png"
        screenshot = pyautogui.screenshot()
        screenshot.save(str(screenshot_path))
        print(f"[Code] 📸 Screenshot: {screenshot_path}")
        return screenshot_path
    except Exception as e:
        print(f"[Code] ⚠️ Screenshot failed: {e}")
        return None


def _image_to_base64(path: Path) -> str:
    import base64
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _detect_intent(description: str, file_path: str, code: str) -> str:
    desc = (description or "").lower()

    screen_kw = ["ekrandaki", "screen", "ekranda", "bu hatayı", "why am i getting",
                 "neden hata", "what's wrong", "ne yanlış", "screenshot", "görüntü"]
    if any(k in desc for k in screen_kw):
        return "screen_debug"

    optimize_kw = ["optimize", "refactor", "clean up", "improve", "temizle",
                   "iyileştir", "daha iyi", "make it better", "hızlandır"]
    if any(k in desc for k in optimize_kw) and (code or file_path):
        return "optimize"

    if file_path:
        p = Path(file_path)
        edit_kw  = ["edit", "update", "modify", "change", "add", "remove",
                    "refactor", "fix", "rename", "replace", "düzenle", "değiştir"]
        run_kw   = ["run", "execute", "launch", "start", "çalıştır"]
        build_kw = ["build", "make it work", "try", "attempt"]

        if p.exists() and any(k in desc for k in edit_kw):
            return "edit"
        if p.exists() and any(k in desc for k in run_kw):
            return "run"
        if any(k in desc for k in build_kw):
            return "build"
        if p.exists():
            return "explain"

    explain_kw = ["explain", "what does", "describe", "analyze", "açıkla", "ne yapıyor"]
    if any(k in desc for k in explain_kw) and (code or file_path):
        return "explain"

    build_kw = ["build", "make it work", "try and", "attempt"]
    if any(k in desc for k in build_kw):
        return "build"

    return "write"

def _write(description: str, language: str, output_path: str, player=None) -> tuple[str, Path]:
    lang  = language or "python"
    model = _get_gemini()

    prompt = f"""You are an expert {lang} developer.
Write clean, working, well-commented {lang} code for the description below.

Rules:
- Output ONLY the code. No explanation, no markdown, no backticks.
- Add helpful inline comments.
- Handle errors and edge cases properly.
- Use modern best practices.

Description: {description}

Code:"""

    response = model.generate_content(prompt)
    code     = _clean_code(response.text)
    path     = resolve_write_target(output_path, lang)
    _save_file(path, code)
    return code, path


def _fix_code(code: str, error_output: str, description: str) -> str:
    model  = _get_gemini()
    prompt = f"""You are an expert debugger.
The code below failed with the following error. Fix it.
Return ONLY the corrected code — no explanation, no markdown, no backticks.

Original goal: {description}

Error:
{error_output[:2000]}

Broken code:
{code}

Fixed code:"""

    response = model.generate_content(prompt)
    return _clean_code(response.text)


def _run_file(path: Path, args: list, timeout: int) -> str:
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".ps1": ["powershell", "-File"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}."

    try:
        result = subprocess.run(
            interp + [str(path)] + (args or []),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, cwd=str(path.parent)
        )
        output = result.stdout.strip()
        error  = result.stderr.strip()
        parts  = []
        if output: parts.append(f"Output:\n{output}")
        if error:  parts.append(f"Stderr:\n{error}")
        return "\n\n".join(parts) if parts else "Executed with no output."

    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout}s."
    except FileNotFoundError:
        return f"Interpreter not found: {interp[0]}."
    except Exception as e:
        return f"Execution error: {e}"


def _build(description, language, output_path, args, timeout, speak=None, player=None) -> str:
    if not description:
        return "Please describe what you want me to build, sir."

    if player:
        player.write_log("[Code] Build started...")

    lang = language or "python"

    try:
        code, path = _write(description, lang, output_path, player)
        print(f"[Code] ✅ Written: {path}")
    except Exception as e:
        msg = f"Could not write initial code: {e}"
        if speak: speak(msg)
        return msg

    last_output = ""
    for attempt in range(1, MAX_BUILD_ATTEMPTS + 1):
        print(f"[Code] 🔄 Attempt {attempt}/{MAX_BUILD_ATTEMPTS}")
        if player:
            player.write_log(f"[Code] Attempt {attempt}...")

        last_output = _run_file(path, args, timeout)

        if not _has_error(last_output):
            msg = (
                f"Build complete, sir. "
                f"The code is working after {attempt} attempt{'s' if attempt > 1 else ''}. "
                f"Saved to {path}."
            )
            if speak: speak(msg)
            return f"{msg}\n\nOutput:\n{last_output}"

        print(f"[Code] ⚠️ Error on attempt {attempt}, fixing...")
        if player:
            player.write_log(f"[Code] Fixing (attempt {attempt})...")

        try:
            code = _fix_code(code, last_output, description)
            _save_file(path, code)
        except Exception as e:
            msg = f"Could not fix code on attempt {attempt}: {e}"
            if speak: speak(msg)
            return msg

    msg = (
        f"I was unable to build a working version after {MAX_BUILD_ATTEMPTS} attempts, sir. "
        f"The last error was: {last_output[:200]}"
    )
    if speak: speak(msg)
    return f"{msg}\n\nLast code saved to: {path}"

def _write_action(description, language, output_path, player) -> str:
    if not description:
        return "Please describe what you want me to write, sir."
    if player:
        player.write_log("[Code] Writing code...")
    try:
        code, path = _write(description, language, output_path, player)
        print(f"[Code] ✅ Written: {path}")
        return f"Code written. Saved to: {path}\n\nPreview:\n{_preview(code)}"
    except Exception as e:
        return f"Could not generate code: {e}"


def _edit_action(file_path, instruction, player, confirm_code: str = "") -> str:
    if not file_path:
        return "Please provide a file path to edit, sir."
    if not instruction:
        return "Please describe what change to make, sir."

    content, err = _read_file(file_path)
    if err:
        return err

    if player:
        player.write_log("[Code] Editing file...")

    model  = _get_gemini()
    prompt = f"""You are an expert code editor.
Apply the following change to the code below.
Return ONLY the complete updated code — no explanation, no markdown, no backticks.

Change: {instruction}

Original code:
{content}

Updated code:"""

    try:
        response = model.generate_content(prompt)
        edited   = _clean_code(response.text)
    except Exception as e:
        print(f"[Code] ❌ Edit hatası (gerçek detay): {type(e).__name__}: {e}")
        return f"Could not edit code: {e}"

    # DUZELTME (denetim bulgusu F-01): mevcut bir dosyanin uzerine yazmak
    # ACIK kullanici onayi gerektirir - _confirm_and_save ilk cagrida hicbir
    # seye dokunmaz, sadece onizleme + onay kodu doner.
    status = _confirm_and_save(Path(file_path), edited, confirm_code)
    print(f"[Code] ✅ Edited: {file_path}")
    return f"File edited. {status}\n\nPreview:\n{_preview(edited)}"


def _explain_action(file_path, code, player) -> str:
    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to explain, sir."

    if player:
        player.write_log("[Code] Analyzing code...")

    model  = _get_gemini()
    prompt = f"""Explain what this code does in simple, clear language.
Focus on: what it does, how it works, and any important details.
Be concise — 3 to 6 sentences maximum.

Code:
{code[:4000]}

Explanation:"""

    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"Could not explain code: {e}"


def _run_action(file_path, args, timeout, player) -> str:
    if not file_path:
        return "Please provide a file path to run, sir."
    p = Path(file_path)
    if not p.exists():
        return f"File not found: {file_path}"
    if player:
        player.write_log(f"[Code] Running {p.name}...")
    return _run_file(p, args, timeout)


def _optimize_action(file_path, code, language, output_path, player, confirm_code: str = "") -> str:

    if file_path and not code:
        code, err = _read_file(file_path)
        if err:
            return err
    if not code:
        return "Please provide code or a file path to optimize, sir."

    if player:
        player.write_log("[Code] Optimizing code...")

    lang  = language or "python"
    model = _get_gemini()

    prompt = f"""You are an expert {lang} developer and code reviewer.
Optimize the following code for:
1. Performance — eliminate unnecessary operations, use efficient data structures
2. Readability — clear variable names, proper formatting, logical structure
3. Best practices — modern {lang} patterns, error handling, type hints if applicable
4. Remove dead code, redundant comments, and unnecessary complexity

Return ONLY the optimized code — no explanation, no markdown, no backticks.

Original code:
{code[:6000]}

Optimized code:"""

    try:
        response  = model.generate_content(prompt)
        optimized = _clean_code(response.text)
    except Exception as e:
        return f"Could not optimize code: {e}"

    # Kaydet. Mevcut bir dosyanin (file_path) uzerine yazmak ACIK kullanici
    # onayi gerektirir (F-01); yeni bir dosya (output_path) ev dizini disina
    # cikamaz (resolve_write_target).
    if file_path:
        save_path = Path(file_path)
        status = _confirm_and_save(save_path, optimized, confirm_code)
    else:
        try:
            save_path = resolve_write_target(output_path, lang)
        except UnsafeWriteTarget as e:
            return str(e)
        status = _save_file(save_path, optimized)
    print(f"[Code] ✅ Optimized: {save_path}")

    original_lines  = len(code.splitlines())
    optimized_lines = len(optimized.splitlines())
    diff = original_lines - optimized_lines

    return (
        f"Code optimized. {status}\n"
        f"Lines: {original_lines} → {optimized_lines} "
        f"({'−' if diff > 0 else '+'}{abs(diff)} lines)\n\n"
        f"Preview:\n{_preview(optimized)}"
    )


def _screen_debug_action(description, file_path, player, speak=None) -> str:

    if player:
        player.write_log("[Code] Taking screenshot for analysis...")

    print("[Code] 📸 Capturing screen for debug...")


    screenshot_path = _take_screenshot()
    if not screenshot_path:
        return "Could not take screenshot, sir. Please make sure PyAutoGUI is installed."


    file_content = ""
    if file_path:
        file_content, err = _read_file(file_path)
        if err:
            print(f"[Code] ⚠️ Could not read file: {err}")

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=_get_api_key())

        image_bytes  = screenshot_path.read_bytes()

        user_question = description or "What error or problem do you see on the screen? How can it be fixed?"

        context = ""
        if file_content:
            context = f"\n\nAdditionally, here is the related file content:\n```\n{file_content[:4000]}\n```"

        analysis_prompt = f"""You are an expert programmer and debugger analyzing a screenshot.

User's question: {user_question}{context}

Please:
1. Identify any errors, exceptions, or problems visible on the screen
2. Explain what is causing the problem in simple terms
3. Provide a concrete fix or solution
4. If there's code visible, show the corrected version

Be specific and actionable. If you see an error message, quote it exactly."""

        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
            analysis_prompt,
        ]

        response = client.models.generate_content(
            model="gemini-flash-latest",
            contents=contents,
        )

        analysis = response.text.strip()
        print("[Code] ✅ Screen analysis complete")

        try:
            screenshot_path.unlink()
        except Exception:
            pass

        if file_path and file_content:

            code_match = re.search(r"```[a-zA-Z]*\n(.*?)```", analysis, re.DOTALL)
            if code_match:
                # DUZELTME (denetim bulgusu F-01): model ciktisini OTOMATIK
                # olarak dosyaya yazmak, kullanicinin hic onay vermedigi bir
                # dosya degisikligi anlamina geliyordu. Simdi sadece ONERI
                # sunulur; gercek kaydetme SADECE kullanici acikca
                # onayladiktan sonra, AYRI bir 'edit' cagrisiyla (kendi
                # onay kapisindan gecerek) yapilir.
                analysis += (
                    f"\n\n💡 Düzeltilmiş kod önerisi hazırlandı ancak dosyaya "
                    f"KAYDEDİLMEDİ. Kaydetmemi isterseniz onaylayın, ardından "
                    f"'edit' eylemiyle {file_path} üzerine uygulayabilirim."
                )
                print(f"[Code] ℹ️ Fixed code proposed (not auto-saved): {file_path}")

        return analysis

    except Exception as e:

        try:
            screenshot_path.unlink()
        except Exception:
            pass
        return f"Screen analysis failed: {e}"


def code_helper(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None
) -> str:
    """
    Called from main.py.

    parameters:
        action      : write | edit | explain | run | build | screen_debug | optimize | auto
        description : What the code should do / what change to make / what problem to analyze
        language    : Programming language (default: python)
        output_path : Where to save — user specifies full path or filename
        file_path   : Path to existing file (edit / explain / run / build / optimize)
        code        : Raw code string (explain/optimize without a file)
        args        : CLI argument list for run/build
        timeout     : Execution timeout in seconds (default: 30)
    """
    p           = parameters or {}
    action      = p.get("action", "auto").lower().strip()
    description = p.get("description", "").strip()
    language    = p.get("language", "python").strip()
    output_path = p.get("output_path", "").strip()
    file_path   = p.get("file_path", "").strip()
    code        = p.get("code", "").strip()
    args        = p.get("args", [])
    timeout     = int(p.get("timeout", 30))
    confirm_code = str(p.get("confirm_code", "")).strip()

    if action == "auto":
        action = _detect_intent(description, file_path, code)
        print(f"[Code] 🤖 Auto-detected: {action}")

    if action == "write":
        return _write_action(description, language, output_path, player)

    elif action == "edit":
        return _edit_action(
            file_path,
            description or p.get("instruction", ""),
            player,
            confirm_code,
        )

    elif action == "explain":
        return _explain_action(file_path, code, player)

    elif action == "run":
        return _run_action(file_path, args, timeout, player)

    elif action == "build":
        return _build(description, language, output_path, args, timeout, speak, player)

    elif action == "optimize":
        return _optimize_action(file_path, code, language, output_path, player, confirm_code)

    elif action == "screen_debug":
        return _screen_debug_action(description, file_path, player, speak)

    else:
        return f"Unknown action: '{action}'. Use write, edit, explain, run, build, optimize, or screen_debug."