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
  in "args". The expected tokens are the exact titles/texts you put in that HTML.

If applicable, create a small sample input ("fixtures") whose correct result you know EXACTLY,
and list short tokens that ANY correct output must contain regardless of formatting:
identifiers (function names), file names, or numbers — NOT full sentences, NOT line formats.
Every token must be justified by the fixtures. Include at least one token per requested feature.

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
or {{FIXTURE_URL}} for its file:// URL (web scrapers).
"output" is a relative output file path from the program's working directory, or "STDOUT".
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
        clean_fx.append({"path": str(fx["path"]).replace("\\", "/"), "content": content})
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
    return {"fixtures": clean_fx, "args": args, "expect": clean_ex, "input_contract": contract}, "ok"


def contract_text(spec: dict) -> str:
    """Yazılacak dosyalara eklenecek zorunlu sözleşme."""
    shown = " ".join(spec["args"]).replace(URL_PLACEHOLDER, "file:///<sample_folder>").replace(PLACEHOLDER, "<sample_folder>")
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

    fixture_url = fixture.resolve().as_uri()
    args = [a.replace(URL_PLACEHOLDER, fixture_url, 1).replace(PLACEHOLDER, str(fixture.resolve()), 1)
            for a in spec["args"]]
    entry = (project_dir / entry_point).resolve()
    try:
        proc = subprocess.run([python or sys.executable, str(entry), *args], cwd=str(run_dir),
                              capture_output=True, text=True, timeout=timeout, check=False)
        output = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        code = proc.returncode
    except subprocess.TimeoutExpired:
        return [f"Kabul testi {timeout:.0f} sn içinde bitmedi."], ""
    problems: list[str] = []
    if code != 0:
        problems.append(f"Program kabul testinde hata koduyla bitti ({code}).")
    for ex in spec["expect"]:
        if ex["output"] == "STDOUT":
            text, label = output, "program çıktısı (stdout)"
        else:
            path = run_dir / ex["output"]
            if not path.is_file():
                problems.append(f"'{ex['output']}' kabul testinde oluşturulmadı (çalışma klasörüne yazılmalı).")
                continue
            text, label = path.read_text(encoding="utf-8", errors="replace"), f"'{ex['output']}'"
        low = text.lower()
        missing = [t for t in ex["contains"] if t.lower() not in low]
        if missing:
            problems.append(f"{label} şu beklenen ifadeleri İÇERMİYOR: {missing}. İçerik (ilk 800 karakter): {text[:800]!r}")
    return problems, output[:2000]


def describe_fixtures(spec: dict, limit: int = 3000) -> str:
    """Düzeltme istemine eklenecek örnek girdi özeti."""
    parts = [f"--- {fx['path']} ---\n{fx['content']}" for fx in spec["fixtures"]]
    return "\n".join(parts)[:limit]
