"""Kabul testi: "program İSTENEN işi doğru yapıyor mu?" sorusunun cevabı.

NEDEN (2026-09-28, üç code-review denemesi): kalite kapısı "kod yarım mı /
bağlanmamış mı" sorusunu yakalıyor ama "doğru sonucu veriyor mu" sorusunu
yakalayamıyor. Üçüncü denemede program çalıştı, rapor doluydu, kapı geçti;
yine de `.bak.py` yedeklerini ve `return []` gövdeli fonksiyonu kaçırdı.
Bunu görmenin tek yolu programı CEVABI ÖNCEDEN BİLİNEN bir örnekte çalıştırmak.

Akış (dev_agent._build_project içinden):
  1. build_prompt()    → LLM'den kabul spesifikasyonu (JSON) istenir.
  2. validate_spec()   → spesifikasyon sıkı doğrulanır; geçersizse kabul testi
                         ATLANIR (build engellenmez, log'a yazılır).
  3. contract_text()   → yazılan her dosyaya "program girdiyi komut satırından
                         almalı" sözleşmesi olarak eklenir.
  4. run_acceptance()  → örnek dosyalar .jarvis/acceptance/fixture/ altına
                         yazılır; program ayrı bir çalışma klasöründe
                         (.jarvis/acceptance/run/) shell'siz çalıştırılır; gerçek
                         çıktılar (report.txt vb.) bu yüzden ezilmez. Beklenen
                         her ifade çıktıda (büyük/küçük harf duyarsız) aranır.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

MAX_FIXTURES = 12
MAX_FIXTURE_BYTES = 8000
MAX_TOKENS = 20
PLACEHOLDER = "{FIXTURE}"
# Web kazıyıcılar için: örnek klasörün file:// adresi (ör. {FIXTURE_URL}/page.html).
URL_PLACEHOLDER = "{FIXTURE_URL}"
ACCEPT_DIR = PurePosixPath(".jarvis/acceptance")

PROMPT = """You design an ACCEPTANCE TEST for a small program that is about to be written.

Program description:
{description}

Planned entry point: {entry}
Planned expected output files: {outputs}

Decide whether the program can be checked automatically on a tiny, fully known sample input:
- NOT applicable for GUI-only apps, randomness, real-time/system-state readings
  (CPU/RAM/processes/time), login-protected sites, or anything needing a human.
- Applicable for programs that read files/folders/text and produce a deterministic result.
- ALSO applicable for WEB SCRAPERS that take the page URL as a command-line argument: write a
  small local HTML page as a fixture (it may contain a <script> that appends items on scroll or
  on a "load more" click, if the task is about that) and pass it as "{{FIXTURE_URL}}/page.html"
  in "args" (it is served over a local http://127.0.0.1 server, so plain HTTP clients work too). The expected tokens are the exact titles/texts you put in that HTML.

If applicable, create a small sample input ("fixtures") whose correct result you know EXACTLY,
and list short tokens that ANY correct output must contain regardless of formatting:
identifiers (function names), file names, or numbers — NOT full sentences, NOT line formats.
Every token must be justified by the fixtures. Include at least one token per requested feature.
If the result depends on file DATES (modification time), give EVERY fixture an "mtime": "YYYY-MM-DD" —
otherwise fixtures are created with today's date and date-based expectations cannot hold.

Return ONLY JSON:
{{
  "applicable": true,
  "reason": "why/why not",
  "fixtures": [{{"path": "sample_project/utils.py", "content": "..."}}],
  "args": ["{{FIXTURE}}/sample_project"],
  "input_contract": "The program takes the folder to analyze as its first command-line argument (sys.argv[1]); when absent it uses the user's folder.",
  "expect": [{{"output": "report.txt", "contains": ["empty_func", "old.bak.py"]}}]
}}
Rules: fixture paths are relative (no "..", no absolute paths), at most {max_fixtures} files.
"args" are the command-line arguments for the test run; use {{FIXTURE}} for the fixture folder,
or {{FIXTURE_URL}} for its local http:// URL (web scrapers).
"output" is a relative output file path from the program's working directory, or "STDOUT".
If the program's result is a FOLDER TREE (files copied/moved/sorted into sub-folders), "output" is that
folder (e.g. "sorted") and "contains" lists the relative file paths expected inside it (e.g. "2024-01/a.jpg").
If not applicable return {{"applicable": false, "reason": "..."}}.
JSON:"""


def build_prompt(description: str, plan: dict) -> str:
    outs = [o.get("path") if isinstance(o, dict) else str(o) for o in plan.get("expected_outputs", []) or []]
    return PROMPT.format(description=description, entry=plan.get("entry_point", "main.py"),
                         outputs=outs or "(none declared)", max_fixtures=MAX_FIXTURES)


def _safe_rel(path: str) -> bool:
    p = PurePosixPath(str(path).replace("\\", "/"))
    return bool(str(path).strip()) and not p.is_absolute() and ".." not in p.parts and "\x00" not in str(path)


def parse_spec(raw_text: str) -> dict | None:
    text = (raw_text or "").strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def validate_spec(spec: object) -> tuple[dict | None, str]:
    """(temiz_spesifikasyon | None, neden). None = kabul testi uygulanmaz."""
    if not isinstance(spec, dict):
        return None, "spesifikasyon JSON nesnesi değil"
    if not spec.get("applicable"):
        return None, f"uygulanamaz: {str(spec.get('reason', ''))[:200]}"
    fixtures = spec.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures or len(fixtures) > MAX_FIXTURES:
        return None, "örnek dosya listesi boş ya da çok uzun"
    clean_fx = []
    for fx in fixtures:
        if not isinstance(fx, dict) or not _safe_rel(fx.get("path", "")):
            return None, f"geçersiz örnek dosya yolu: {fx!r}"[:200]
        content = fx.get("content", "")
        if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_FIXTURE_BYTES:
            return None, f"örnek dosya içeriği geçersiz/çok büyük: {fx.get('path')}"
        item = {"path": str(fx["path"]).replace("\\", "/"), "content": content}
        mtime = str(fx.get("mtime", "") or "").strip()
        if mtime:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?", mtime):
                return None, f"geçersiz mtime: {mtime!r}"
            item["mtime"] = mtime
        clean_fx.append(item)
    args = spec.get("args", [])
    if not isinstance(args, list) or len(args) > 10 or not all(isinstance(a, str) and len(a) < 300 for a in args):
        return None, "args düz metin listesi olmalı"
    for a in args:
        for ph in (URL_PLACEHOLDER, PLACEHOLDER):
            if a.startswith(ph):
                rest = a[len(ph):]
                if rest and not _safe_rel(rest.lstrip("/")):
                    return None, f"arg örnek klasör dışına çıkıyor: {a}"
                break
    expect = spec.get("expect")
    if not isinstance(expect, list) or not expect:
        return None, "beklenti listesi boş"
    clean_ex = []
    for ex in expect:
        if not isinstance(ex, dict):
            return None, "beklenti nesne olmalı"
        out = str(ex.get("output", "")).strip()
        tokens = ex.get("contains", [])
        if out != "STDOUT" and not _safe_rel(out):
            return None, f"geçersiz çıktı yolu: {out}"
        if not isinstance(tokens, list) or not tokens or len(tokens) > MAX_TOKENS:
            return None, "contains listesi boş ya da çok uzun"
        tokens = [str(t).strip() for t in tokens if str(t).strip()]
        if not tokens or any(len(t) > 120 for t in tokens):
            return None, "beklenen ifadeler kısa olmalı"
        clean_ex.append({"output": out, "contains": tokens})
    contract = str(spec.get("input_contract", "")).strip()[:500]
    _correct_word_counts(clean_fx, clean_ex)
    return {"fixtures": clean_fx, "args": args, "expect": clean_ex, "input_contract": contract}, "ok"


_COUNT_TOKEN = re.compile(r"^([^\W\d_][\w'-]{0,40})(\s*[:=]\s*)(\d{1,4})$")
_TEXT_FIXTURE_SUFFIXES = (".txt", ".md", ".text")


def _correct_word_counts(fixtures: list[dict], expect: list[dict]) -> list[str]:
    """Kabul testini yazan model kendi örnek metnindeki kelimeleri YANLIŞ
    sayabiliyor (canlı test 2026-09-28: 'hello world hello' + 'hello universe'
    + 'world hello' için 'hello: 3' bekledi; doğrusu 4 — program doğruydu ama 5
    denemede de reddedildi). Örnekler yalnızca düz metinse, 'kelime: N'
    biçimindeki beklentileri gerçek sayımla deterministik olarak düzeltir."""
    if not fixtures or not all(fx["path"].lower().endswith(_TEXT_FIXTURE_SUFFIXES) for fx in fixtures):
        return []
    corpus = "\n".join(fx["content"] for fx in fixtures).casefold()
    fixed: list[str] = []
    for ex in expect:
        new_tokens = []
        for tok in ex["contains"]:
            m = _COUNT_TOKEN.match(tok)
            if m:
                word, sep, n = m.group(1), m.group(2), int(m.group(3))
                real = len(re.findall(rf"(?<![\w'-]){re.escape(word.casefold())}(?![\w'-])", corpus))
                if real and real != n and abs(real - n) <= 3:
                    fixed.append(f"{tok} → {word}{sep}{real}")
                    tok = f"{word}{sep}{real}"
            new_tokens.append(tok)
        ex["contains"] = new_tokens
    if fixed:
        print(f"[DevAgent] 🔧 Kabul testindeki yanlış sayımlar düzeltildi: {fixed}")
    return fixed


def contract_text(spec: dict) -> str:
    """Yazılacak dosyalara eklenecek zorunlu sözleşme."""
    shown = " ".join(spec["args"]).replace(URL_PLACEHOLDER, "http://127.0.0.1:<port>").replace(PLACEHOLDER, "<sample_folder>")
    base = spec.get("input_contract") or "The program must take its input path from the command line."
    return (
        f"ACCEPTANCE CONTRACT (automatically tested): {base} The program will ALSO be run as: "
        f"`python <entry_point> {shown}` from a DIFFERENT working directory — it must honor these "
        f"command-line arguments instead of a hard-coded path, write its outputs relative to the "
        f"current working directory, and never import from the working directory."
    )


def run_acceptance(project_dir: Path, entry_point: str, spec: dict, timeout: float = 60.0,
                   python: str | None = None) -> tuple[list[str], str]:
    """(sorunlar, program_çıktısı). Sorun listesi boşsa kabul testi geçti."""
    root = project_dir / ACCEPT_DIR
    shutil.rmtree(root, ignore_errors=True)
    fixture, run_dir = root / "fixture", root / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    fixture.mkdir(parents=True, exist_ok=True)
    for fx in spec["fixtures"]:
        target = (fixture / fx["path"]).resolve()
        target.relative_to(fixture.resolve())  # validate_spec zaten garanti ediyor; ikinci kilit
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(fx["content"], encoding="utf-8")
        if fx.get("mtime"):
            # Tarihe bağlı görevler için (canlı test 2026-09-29: örnek dosyalar
            # bugünün tarihiyle oluştuğu için '2024-01' beklentisi imkânsızdı).
            from datetime import datetime
            ts = datetime.fromisoformat(fx["mtime"].replace(" ", "T")).timestamp()
            os.utime(target, (ts, ts))

    uses_url = any(URL_PLACEHOLDER in a for a in spec["args"])
    server = _serve_directory(fixture) if uses_url else None
    fixture_url = f"http://127.0.0.1:{server.server_address[1]}" if server else ""
    args = [a.replace(URL_PLACEHOLDER, fixture_url, 1).replace(PLACEHOLDER, str(fixture.resolve()), 1)
            for a in spec["args"]]
    entry = (project_dir / entry_point).resolve()
    before = _output_snapshot(project_dir, spec)
    try:
        proc = subprocess.run([python or sys.executable, str(entry), *args], cwd=str(run_dir),
                              capture_output=True, text=True, timeout=timeout, check=False)
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return [f"Kabul testi {timeout:.0f} sn içinde bitmedi."], ""
    finally:
        if server:
            server.shutdown()
            server.server_close()
    problems: list[str] = []
    if code != 0:
        problems.append(f"Program kabul testinde hata koduyla bitti ({code}).")
    for ex in spec["expect"]:
        if ex["output"] == "STDOUT":
            text, label = output, "program çıktısı (stdout)"
        else:
            path = run_dir / ex["output"]
            if path.is_dir():
                # Klasör çıktısı (dış analiz raporu 2026-09-29: klasör ağacı üreten
                # doğru program 'oluşturulmadı' diye reddediliyordu): içindeki
                # dosyaların göreli yolları metin olarak karşılaştırılır.
                text, label = _tree_text(path), f"'{ex['output']}' klasörü"
                if not text:
                    problems.append(f"'{ex['output']}' klasörü oluştu ama içinde hiç dosya yok.")
                    continue
            elif not path.is_file():
                stray = _written_elsewhere(project_dir, ex["output"], before)
                if stray:
                    problems.append(
                        f"'{ex['output']}' çalışma klasörüne DEĞİL, programın kendi klasörüne yazıldı ({stray}). "
                        f"Çıktı yolunu Path(__file__).parent / os.path.dirname(__file__) / sabit bir klasörle "
                        f"KURMA; yalnızca göreli yol kullan: open('{ex['output']}', 'w') — program nereden "
                        f"çalıştırılırsa (os.getcwd()) oraya yazmalı."
                    )
                else:
                    problems.append(f"'{ex['output']}' kabul testinde oluşturulmadı (çalışma klasörüne yazılmalı).")
                continue
            else:
                text, label = path.read_text(encoding="utf-8", errors="replace"), f"'{ex['output']}'"
        low = text.lower()
        missing = [t for t in ex["contains"] if not _token_found(t, low)]
        if missing:
            problems.append(f"{label} şu beklenen ifadeleri İÇERMİYOR: {missing}. İçerik (ilk 800 karakter): {text[:800]!r}")
    return problems, output[:2000]


def _tree_text(folder: Path, limit: int = 500) -> str:
    """Klasördeki dosyaların göreli yolları (posix, satır satır)."""
    rels = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    return "\n".join(rels[:limit])


def _stamp(p: Path) -> int | None:
    """Dosyanın, klasörse içindeki en yeni dosyanın değişim zamanı. Klasör
    içinde ctime da sayılır: shutil.copy2 eski mtime'ı KORUR, ama yeni kopyanın
    ctime'ı (Windows'ta oluşturma zamanı) şimdidir."""
    if p.is_file():
        return p.stat().st_mtime_ns
    if p.is_dir():
        times = [max(st.st_mtime_ns, st.st_ctime_ns) for f in p.rglob("*") if f.is_file() for st in [f.stat()]]
        return max(times) if times else None
    return None


_BARE_TAG = re.compile(r"^<([a-z][a-z0-9]*)>$", re.IGNORECASE)


def _token_found(token: str, low_text: str) -> bool:
    """Beklenen ifade çıktıda var mı? '<table>' gibi çıplak bir HTML etiketi,
    özniteliklisini de ('<table border="1">') kabul eder (canlı test 2026-09-29:
    doğru HTML tablosu yalnızca border özniteliği yüzünden 5 kez reddedildi)."""
    t = token.lower()
    if t in low_text:
        return True
    m = _BARE_TAG.match(t)
    return bool(m and re.search(rf"<{m.group(1)}[\s>/]", low_text))


def _output_snapshot(project_dir: Path, spec: dict) -> dict[Path, int]:
    """Kabul testi ÖNCESİNDE, beklenen çıktı adlarını taşıyan proje
    dosyalarının değişim zamanları. (canlı test 2026-09-28: gerçek çalıştırma
    quotes.csv'yi 1 sn önce proje köküne yazmıştı; zaman penceresine dayalı eski
    kontrol bunu 'kabul testinde yanlış klasöre yazdı' sandı ve modeli yanlış
    yöne itti.)"""
    snap: dict[Path, int] = {}
    root = project_dir / ACCEPT_DIR
    for ex in spec["expect"]:
        if ex["output"] == "STDOUT":
            continue
        for p in project_dir.rglob(PurePosixPath(ex["output"]).name):
            try:
                if root not in p.parents and p != root and (st := _stamp(p)) is not None:
                    snap[p] = st
            except OSError:
                continue
    return snap


def _written_elsewhere(project_dir: Path, output: str, before: dict[Path, int]) -> str | None:
    """Beklenen çıktı, kabul testi SIRASINDA çalışma klasörü yerine proje
    içinde başka bir yere mi yazıldı? Yalnızca bu çalıştırmada oluşan ya da
    değişen dosyalar sayılır."""
    root = project_dir / ACCEPT_DIR
    for p in project_dir.rglob(PurePosixPath(output).name):
        try:
            if root in p.parents or p == root:
                continue
            st = _stamp(p)
            if st is not None and before.get(p) != st:
                return p.relative_to(project_dir).as_posix()
        except OSError:
            continue
    return None


def _serve_directory(directory: Path):
    """Örnek HTML'i yerel bir HTTP sunucusundan sunar. file:// adresini
    requests açamaz (canlı test 2026-09-28: düz kazıyıcı kabul testinde hiç
    çıktı üretemedi); http://127.0.0.1 hem requests hem Playwright ile çalışır."""
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class _Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):  # noqa: D401 - sessiz
            pass

    handler = functools.partial(_Quiet, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


_SCRIPT_DIR_PATTERNS = (
    (re.compile(r"Path\(\s*__file__\s*\)(?:\.(?:resolve|absolute)\(\))?(?:\.parent)+"), "Path.cwd()"),
    (re.compile(r"os\.path\.dirname\(\s*os\.path\.(?:abspath|realpath)\(\s*__file__\s*\)\s*\)"), "os.getcwd()"),
    (re.compile(r"os\.path\.dirname\(\s*__file__\s*\)"), "os.getcwd()"),
)


def rewrite_script_dir_paths(file_codes: dict[str, str]) -> dict[str, str]:
    """Çıktıyı programın kendi klasörüne (__file__) yazan yolları çalışma
    klasörüne çevirir. Yalnızca kabul testi bunu KANITLADIĞINDA çağrılır
    (canlı test 2026-09-28: 14b model ipucuna rağmen 5 denemede de
    Path(__file__).parent kullandı). Değişen dosyaları döndürür."""
    changed = {}
    for path, code in file_codes.items():
        if not path.endswith(".py") or "__file__" not in code:
            continue
        new = code
        for rx, repl in _SCRIPT_DIR_PATTERNS:
            new = rx.sub(repl, new)
        if new != code:
            if "os.getcwd()" in new and not re.search(r"^\s*import os\b", new, re.M):
                new = "import os\n" + new
            changed[path] = new
    return changed


def describe_fixtures(spec: dict, limit: int = 3000) -> str:
    """Düzeltme istemine eklenecek örnek girdi özeti."""
    parts = [f"--- {fx['path']} ---\n{fx['content']}" for fx in spec["fixtures"]]
    return "\n".join(parts)[:limit]
