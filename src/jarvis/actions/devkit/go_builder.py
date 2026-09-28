"""Go proje kurucusu: planla → yaz → doğrula → düzelt → çalıştır → kanıtla.

Akış (her adımın sonucu log'a yazılır):
  1. PLAN     LLM'den katı bir JSON plan alınır ve DOĞRULANIR (yalnızca proje
              içi .go yolları, kökte main.go, "jarvis" adı yok, go.mod yok).
  2. YAZ      Her dosya LLM ile yazılır; "package" ile başlamayan çıktı reddedilir.
  3. HAZIRLA  go mod init → gofmt -w → go mod tidy (deterministik, LLM'siz).
  4. DOĞRULA  go build → go vet → golangci-lint (standard set). Bulgu varsa
              yalnızca ilgili dosyalar, bulgularıyla birlikte LLM'e düzelttirilir.
  5. ÇALIŞTIR Derlenmiş ikili, planın run_args'ı ile shell'siz çalıştırılır.
              panic / sıfırdan farklı çıkış kodu = hata; yığın izinden dosya:satır
              çıkarılıp düzeltmeye verilir.
  6. KANITLA  Plan beklenen çıktı dosyaları bildirdiyse, bu çalıştırmada
              gerçekten oluşup boş olmadıkları kontrol edilir.
Adım 4-6 toplam MAX_ATTEMPTS düzeltme bütçesini paylaşır. Aynı bulgu kümesi
art arda tekrarlarsa model açıkça uyarılır.

LLM, generate(prompt) -> str olarak enjekte edilir; bu modül Gemini'yi
doğrudan import etmez (test edilebilirlik + tek sorumluluk).
"""
from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from jarvis.actions.devkit.go_toolchain import WORK_DIR, GoToolchain, sanitize_module_name
from jarvis.actions.devkit.toolchain import Diagnostic, group_by_file

MAX_ATTEMPTS = 6
LINT_BLOCKING_ROUNDS = 3   # bu kadar turdan sonra lint (yalnız lint) artık engel değil
MAX_FILES = 12
MAX_RUN_ARGS = 16

Generate = Callable[[str], str]
Log = Callable[[str], None]


class RateLimited(Exception):
    """LLM kota/rate-limit hatası — kurulum durdurulur, dosyalar korunur."""


class PlanError(ValueError):
    """Plan kullanılamaz durumda."""


# ─────────────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────────────
def _strip_fences(text: str) -> str:
    """LLM yanıtındaki İLK kod bloğunu alır; öncesindeki/sonrasındaki açıklama
    metni atılır (yerel modeller sıkça "Here is the code:" ekler)."""
    text = (text or "").strip()
    m = re.search(r"```[\w+-]*[ \t]*\r?\n(.*?)\r?\n?```", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


def _escapes_project(rel: str) -> bool:
    """Yol mutlak mı ya da '..' ile dışarı çıkıyor mu? Platformdan bağımsız:
    Windows'ta Path("/tmp/x").is_absolute() False döner, POSIX'te "C:/x"
    göreli sayılır; ikisini de yakalar."""
    posix = rel.replace("\\", "/")
    return (
        posix.startswith("/")
        or bool(PureWindowsPath(rel).drive)
        or ".." in PurePosixPath(posix).parts
    )


def _safe_path(project_dir: Path, rel: str) -> Path | None:
    """Göreli yolu proje kökü DIŞINA çıkamayacak şekilde çözer."""
    if not rel or _escapes_project(rel) or "\\" in rel:
        return None
    try:
        candidate = (project_dir / rel).resolve()
        candidate.relative_to(project_dir.resolve())
        return candidate
    except (ValueError, OSError):
        return None


def _call(generate: Generate, prompt: str, is_rate_limit: Callable[[Exception], bool] | None) -> str:
    try:
        return generate(prompt)
    except Exception as e:  # LLM istemcisi her türlü hata fırlatabilir
        if is_rate_limit and is_rate_limit(e):
            raise RateLimited(str(e)) from e
        raise


# ─────────────────────────────────────────────────────────────────────────
# 1. PLAN
# ─────────────────────────────────────────────────────────────────────────
_PLAN_PROMPT = """You are a senior Go architect. Plan a small, complete, standalone Go program.

Description: {description}

Return ONLY valid JSON (no markdown):
{{
  "project_name": "snake_case_name",
  "module": "example.com/snake_case_name",
  "files": [
    {{"path": "main.go", "description": "package main entry point: what it does, which internal packages it uses"}},
    {{"path": "internal/monitor/monitor.go", "description": "package monitor: exported functions and their exact signatures"}}
  ],
  "run_args": [],
  "long_running": false,
  "expected_outputs": [
    {{"path": "report.log", "description": "what a CORRECT result looks like inside this file"}}
  ]
}}

Rules:
1. Go standard library ONLY unless the description truly requires a third-party module.
2. "main.go" (package main) MUST be at the project root. Other packages go under "internal/<name>/<name>.go"
   and are imported as "<module>/internal/<name>". Keep it minimal (1-4 files is typical).
3. For every exported function a file exposes, write its EXACT signature in that file's description,
   so every other file calls it identically.
4. "run_args": command-line arguments for an automated verification run (no shell syntax, plain strings).
   The run must finish by itself within a few seconds and must NOT need a human.
5. "long_running": true ONLY for servers/daemons that are supposed to keep running.
6. If the program writes files (log, report, db...), list each RELATIVE path in "expected_outputs".
   Paths must stay inside the project directory (no absolute paths, no "..").
7. Never plan go.mod/go.sum (they are generated), never use a package or path named "jarvis".

JSON:"""


def plan_project(description: str, generate: Generate, is_rate_limit=None) -> dict:
    raw = _call(generate, _PLAN_PROMPT.format(description=description), is_rate_limit)
    text = _strip_fences(raw)
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f"Planlayıcı geçersiz JSON döndürdü: {e}") from e
    return validate_plan(plan)


def validate_plan(plan: object) -> dict:
    if not isinstance(plan, dict):
        raise PlanError("Plan bir JSON nesnesi olmalı.")
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise PlanError("Planda dosya yok.")
    if len(files) > MAX_FILES:
        raise PlanError(f"Plan çok büyük ({len(files)} dosya, üst sınır {MAX_FILES}).")

    clean_files: list[dict] = []
    seen: set[str] = set()
    for f in files:
        if not isinstance(f, dict):
            raise PlanError("Her dosya bir nesne olmalı.")
        path = str(f.get("path", "")).strip().replace("\\", "/")
        parts = PurePosixPath(path).parts
        if (
            not path.endswith(".go")
            or path.endswith("_test.go")
            or _escapes_project(path)
            or any(p.startswith((".", "_")) for p in parts)
            or any(p.lower() in ("jarvis", "jarvis.go") for p in parts)
        ):
            raise PlanError(f"Geçersiz dosya yolu: {path!r}")
        if path in seen:
            raise PlanError(f"Aynı dosya iki kez planlanmış: {path}")
        seen.add(path)
        clean_files.append({"path": path, "description": str(f.get("description", ""))[:800]})
    if "main.go" not in seen:
        raise PlanError("Kökte main.go olmalı.")
    # main.go en sona: önce bağımlı olunan paketler yazılsın.
    clean_files.sort(key=lambda f: f["path"] == "main.go")

    run_args = plan.get("run_args", [])
    if not isinstance(run_args, list) or len(run_args) > MAX_RUN_ARGS or not all(isinstance(a, str) for a in run_args):
        raise PlanError("run_args düz metinlerden oluşan kısa bir liste olmalı.")
    if any("\x00" in a or len(a) > 300 for a in run_args):
        raise PlanError("run_args geçersiz karakter/uzunluk içeriyor.")

    outputs = []
    for o in plan.get("expected_outputs", []) or []:
        p = o.get("path") if isinstance(o, dict) else o
        if not isinstance(p, str) or not p.strip():
            raise PlanError("Beklenen çıktı yolu boş olamaz.")
        p = p.strip().replace("\\", "/")
        if _escapes_project(p) or p.split("/")[0] == WORK_DIR:
            raise PlanError(f"Beklenen çıktı proje dışına çıkıyor: {p}")
        outputs.append({"path": p, "description": str(o.get("description", "")) if isinstance(o, dict) else ""})

    name = re.sub(r"[^\w\-]", "_", str(plan.get("project_name") or "go_app"))[:60] or "go_app"
    return {
        "project_name": name,
        "module": sanitize_module_name(str(plan.get("module") or name)),
        "files": clean_files,
        "run_args": run_args,
        "long_running": bool(plan.get("long_running", False)),
        "expected_outputs": outputs,
    }


# ─────────────────────────────────────────────────────────────────────────
# 2. YAZ / 4. DÜZELT — ortak kurallar
# ─────────────────────────────────────────────────────────────────────────
_GO_RULES = """Go rules (MANDATORY — the code is verified with go build, go vet and golangci-lint
standard set: errcheck, govet, ineffassign, staticcheck, unused):
- Output ONLY raw Go source for this one file. No markdown, no backticks, no prose outside // comments.
- ZERO unused variables, imports, functions, types or constants.
- EVERY returned error is checked with `if err != nil` and handled meaningfully (log/return/wrap with %w).
  Never discard an error with `_`. This includes Close(), Write/Fprintf, Flush(), Encode(), Sync().
  For deferred Close on a file you WROTE to, use a named error return and merge the Close error.
- No shell: never use exec.Command("sh"/"bash", "-c", ...). If os/exec is truly needed, pass a fixed
  binary and separate, validated arguments; never interpolate untrusted input into a command.
- Validate all external input (flags, file paths, env). Use filepath.Clean; create files with 0o600/0o644.
- Standard library only unless the plan says otherwise. The program must finish on its own during an
  automated run (no interactive prompts, no waiting for stdin) unless it is a long-running server.
- Imports of project packages use the module path: "{module}/internal/<name>".
- Explain non-obvious logic with short // comments."""


def _project_overview(plan: dict) -> str:
    return "\n".join(f"  - {f['path']}: {f['description']}" for f in plan["files"])


def _outputs_block(plan: dict) -> str:
    if not plan["expected_outputs"]:
        return ""
    lines = "\n".join(f"  - {o['path']}: {o['description']}" for o in plan["expected_outputs"])
    return f"Files the program MUST create/update (exact relative paths, relative to the working directory):\n{lines}\n"


def _validate_go_source(code: str) -> str | None:
    """Açıkça bozuk LLM çıktısını diske yazmadan yakalar."""
    stripped = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE).lstrip()
    if not stripped.startswith("package "):
        return "çıktı 'package' bildirimiyle başlamıyor"
    return None


def write_file(project_dir: Path, plan: dict, file_info: dict, written: dict[str, str],
               description: str, generate: Generate, is_rate_limit=None) -> str:
    context = "".join(f"\n--- {p} (already written) ---\n{c[:2500]}" for p, c in written.items())
    prompt = f"""You are a senior Go engineer writing production-quality code.

Program goal: {description}
Module path: {plan['module']}
Project files:
{_project_overview(plan)}
{_outputs_block(plan)}
Automated verification run arguments: {plan['run_args'] or '(none)'}
{context}

{_GO_RULES.format(module=plan['module'])}

Write the complete code for: {file_info['path']}
Purpose: {file_info['description']}

Code:"""
    code = _strip_fences(_call(generate, prompt, is_rate_limit))
    problem = _validate_go_source(code)
    if problem:
        code = _strip_fences(_call(generate, f"{prompt}\n\nYour previous answer was rejected: {problem}. "
                                             f"Return ONLY the Go source file.", is_rate_limit))
        if _validate_go_source(code):
            raise ValueError(f"{file_info['path']}: geçerli Go kaynağı üretilemedi")
    target = _safe_path(project_dir, file_info["path"])
    if target is None:
        raise ValueError(f"Güvenlik: {file_info['path']} proje dışına çıkıyor")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(code, encoding="utf-8")
    return code


def _backup(project_dir: Path, rel: str) -> None:
    """Düzeltme öncesi kopyayı .jarvis/backups/ altına alır (go araçları görmez)."""
    src = project_dir / rel
    if not src.is_file():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dst = project_dir / WORK_DIR / "backups" / f"{rel.replace('/', '__')}.{stamp}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def fix_files(project_dir: Path, plan: dict, codes: dict[str, str], problem: str,
              diags_by_file: dict[str, list[Diagnostic]], description: str,
              generate: Generate, is_rate_limit=None, repeated: bool = False) -> list[str]:
    """Bulgulu dosyaları (bulgu dosyasızsa tüm projeyi) LLM'e düzelttirir."""
    targets = [p for p in diags_by_file if p in codes] or list(codes)
    fixed: list[str] = []
    for rel in targets:
        others = "".join(f"\n--- {p} ---\n{c[:3000]}" for p, c in codes.items() if p != rel)
        findings = "\n".join(f"  - {d.render()}" for d in diags_by_file.get(rel, []))
        repeat_note = (
            "\nWARNING: the previous fix attempt produced EXACTLY the same findings. Do not repeat the same edit; "
            "re-read the findings, check call signatures across files and change the approach.\n"
            if repeated else ""
        )
        prompt = f"""You are an expert Go debugger. Fix the file below so the whole project passes
go build, go vet, golangci-lint (errcheck, govet, ineffassign, staticcheck, unused) and runs correctly.

Program goal: {description}
Module path: {plan['module']}
Project files:
{_project_overview(plan)}
{_outputs_block(plan)}
Other files (read-only context):{others[:9000]}

Problem: {problem}
Findings in THIS file:
{findings or '  (no file-specific findings — see the problem description above)'}
{repeat_note}
{_GO_RULES.format(module=plan['module'])}
- Keep all correct behaviour; change only what is needed.

Current code of {rel}:
{codes[rel]}

Fixed complete code for {rel}:"""
        code = _strip_fences(_call(generate, prompt, is_rate_limit))
        if _validate_go_source(code):
            continue  # bozuk yanıtla sağlam dosyanın üstüne yazma
        target = _safe_path(project_dir, rel)
        if target is None:
            continue
        _backup(project_dir, rel)
        target.write_text(code, encoding="utf-8")
        codes[rel] = code
        fixed.append(rel)
    return fixed


# ─────────────────────────────────────────────────────────────────────────
# 5-6. ÇALIŞTIR / KANITLA
# ─────────────────────────────────────────────────────────────────────────
_STACK_RE = re.compile(r"((?:[A-Za-z]:)?[\w\-./\\]+\.go):(\d+)")


def runtime_diagnostics(output: str, project_dir: Path, known: set[str]) -> list[Diagnostic]:
    """panic yığın izindeki (mutlak yollu) proje dosyası satırlarını çıkarır."""
    # Windows'ta sürücü harfi/büyük-küçük harf farkı olabilir; hem verilen hem
    # çözülmüş kökü dene.
    roots = {
        (r.as_posix().rstrip("/") + "/").lower()
        for r in (project_dir, project_dir.resolve())
    }
    result: list[Diagnostic] = []
    for m in _STACK_RE.finditer(output):
        path = m.group(1).replace("\\", "/")
        for root in roots:
            if path.lower().startswith(root):
                path = path[len(root):]
                break
        if path in known and all(d.path != path or d.line != int(m.group(2)) for d in result):
            result.append(Diagnostic(path, int(m.group(2)), 0, "runtime failure (see program output)", "run"))
    return result


def check_expected_outputs(project_dir: Path, plan: dict, started_at: float) -> list[str]:
    problems = []
    for o in plan["expected_outputs"]:
        target = _safe_path(project_dir, o["path"])
        if target is None:
            problems.append(f"{o['path']}: proje dışına çıkıyor")
        elif not target.is_file():
            problems.append(f"{o['path']}: oluşturulmadı")
        elif target.stat().st_mtime < started_at - 1:
            problems.append(f"{o['path']}: bu çalıştırmada güncellenmedi")
        elif target.stat().st_size == 0:
            problems.append(f"{o['path']}: boş")
    return problems


# ─────────────────────────────────────────────────────────────────────────
# Ana akış
# ─────────────────────────────────────────────────────────────────────────
def build_go_project(
    description: str,
    project_name: str = "",
    timeout: int = 30,
    *,
    generate: Generate,
    projects_dir: Path,
    log: Log = print,
    speak: Callable[[str], None] | None = None,
    open_editor: Callable[[Path], object] | None = None,
    is_rate_limit: Callable[[Exception], bool] | None = None,
    tool_timeout: float = 180.0,
) -> str:
    def finish(msg: str, detail: str = "") -> str:
        if speak:
            speak(msg)
        return f"{msg}\n\n{detail}".strip()

    # Araçlar hiç yoksa LLM kotası harcamadan dur.
    probe = GoToolchain(projects_dir, tool_timeout)
    if not probe.tools.usable:
        return finish("Go derleyicisi (go) bu bilgisayarda bulunamadı efendim; Go projesi kurulamaz. "
                      "Önce Go'yu kurun (ör. https://go.dev/dl).")

    try:
        log("Go projesi planlanıyor...")
        plan = plan_project(description, generate, is_rate_limit)
    except RateLimited:
        return finish("Rate limit reached, sir. Please try again in a moment.")
    except PlanError as e:
        return finish(f"Planlama başarısız: {e}")

    name = re.sub(r"[^\w\-]", "_", project_name).strip("_") or plan["project_name"]
    project_dir = projects_dir / name
    project_dir.mkdir(parents=True, exist_ok=True)
    tc = GoToolchain(project_dir, tool_timeout)
    log(f"Proje: {name} | modül: {plan['module']} | dosyalar: {[f['path'] for f in plan['files']]}")
    log(tc.version() or "go sürümü okunamadı")

    # Eski çalıştırmadan kalan beklenen çıktılar yeni başarı gibi görünmesin.
    for o in plan["expected_outputs"]:
        t = _safe_path(project_dir, o["path"])
        if t is not None and t.is_file():
            t.unlink()

    codes: dict[str, str] = {}
    try:
        for f in plan["files"]:
            log(f"Yazılıyor: {f['path']}")
            codes[f["path"]] = write_file(project_dir, plan, f, codes, description, generate, is_rate_limit)
    except RateLimited:
        return finish(f"Kota sınırı nedeniyle '{name}' yarım kaldı; yazılan dosyalar {project_dir} içinde.")
    except ValueError as e:
        return finish(f"Dosya yazılamadı: {e}")

    r = tc.init_module(plan["module"])
    if not r.ok:
        return finish("go mod init başarısız oldu.", r.output)
    tc.gofmt()
    tidy = tc.tidy()
    log("go mod tidy tamam" if tidy.ok else f"go mod tidy uyarısı: {tidy.output[:300]}")
    if open_editor:
        try:
            open_editor(project_dir)
        except Exception as e:  # editör açılamaması kurulumu engellemesin
            log(f"Editör açılamadı: {e}")

    lint_skipped = tc.tools.golangci_lint is None
    if lint_skipped:
        log("⚠️ golangci-lint bulunamadı — lint adımı atlanacak (build + vet yine zorunlu).")

    previous_signature: str | None = None
    last_detail = ""
    lint_failures = 0
    lint_warnings = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"Doğrulama turu {attempt}/{MAX_ATTEMPTS}")
        tc.gofmt()
        stage, diags, detail = "", [], ""

        res, diags = tc.build()
        if not res.ok:
            stage, detail = "go build başarısız", res.output
        if not stage:
            res, diags = tc.vet()
            if not res.ok:
                stage, detail = "go vet bulgu verdi", res.output
        if not stage:
            lres, lint_diags = tc.lint()
            if lres is not None and not lres.ok:
                lint_failures += 1
                if lint_failures < LINT_BLOCKING_ROUNDS:
                    stage, detail, diags = "golangci-lint bulgu verdi", lres.output, lint_diags
                else:
                    # Canlı test 2026-09-29: build + vet temizken yalnızca stil
                    # uyarıları yüzünden 6 tur harcandı, program hiç çalıştırılmadı.
                    # Derlenen ve vet'ten geçen program artık ÇALIŞTIRILIP gerçek
                    # çıktısıyla doğrulanıyor; kalan lint bulguları rapora uyarı olarak eklenir.
                    lint_warnings = lres.output
                    log(f"⚠️ golangci-lint {lint_failures}. kez bulgu verdi; build + vet temiz olduğu için "
                        f"program yine de çalıştırılıyor (lint bulguları uyarı olarak raporlanacak).")
        if not stage:
            started = time.time()
            run = tc.run_binary(plan["run_args"], timeout)
            detail = run.output
            if run.timed_out:
                if plan["long_running"] and not plan["expected_outputs"]:
                    return finish(
                        f"'{name}' derlendi, vet ve lint'ten geçti; {timeout} sn boyunca çökmeden çalıştı "
                        f"(sunucu/sürekli çalışan program olarak planlandı). Dosyalar: {project_dir}",
                        f"Çıktı:\n{run.output[:1500]}")
                stage = f"program {timeout} sn içinde bitmedi (takılıyor olabilir)"
                diags = []
            elif not run.ok:
                stage = f"program hata ile bitti (çıkış kodu {run.returncode})"
                diags = runtime_diagnostics(run.output, project_dir, set(codes))
            else:
                problems = check_expected_outputs(project_dir, plan, started)
                if problems:
                    stage = "program çökmedi ama beklenen çıktıyı üretmedi: " + "; ".join(problems)
                    diags = []
                else:
                    lint_note = " (golangci-lint kurulu olmadığı için lint atlandı)" if lint_skipped else ", golangci-lint"
                    if lint_warnings:
                        lint_note = " (golangci-lint stil uyarıları kaldı, aşağıda)"
                    outs = ", ".join(o["path"] for o in plan["expected_outputs"])
                    return finish(
                        f"'{name}' Go projesi çalışıyor efendim: go build, go vet{lint_note} temiz; "
                        f"program başarıyla çalıştı{f' ve {outs} doğrulandı' if outs else ''}. "
                        f"{attempt}. turda tamamlandı. Konum: {project_dir}",
                        f"Çıktı:\n{run.output[:1500]}"
                        + (f"\n\ngolangci-lint uyarıları:\n{lint_warnings[:1500]}" if lint_warnings else ""))

        last_detail = f"{stage}\n{detail[:1500]}"
        log(f"❌ {stage}")
        if attempt == MAX_ATTEMPTS:
            break

        signature = stage + "|" + "|".join(sorted(d.render() for d in diags))
        repeated = signature == previous_signature
        previous_signature = signature
        try:
            fixed = fix_files(project_dir, plan, codes, f"{stage}\nTool output:\n{detail[:2500]}",
                              group_by_file(diags), description, generate, is_rate_limit, repeated)
        except RateLimited:
            return finish(f"Kota sınırı nedeniyle düzeltme yarım kaldı; proje {project_dir} içinde.", last_detail)
        log(f"🔧 Düzeltilen dosyalar: {fixed or 'yok'}")
        if not fixed:
            continue
        tc.tidy()

    return finish(
        f"'{name}' {MAX_ATTEMPTS} denemede tamamen düzeltilemedi efendim. Dosyalar {project_dir} içinde; "
        f"son hata aşağıda.", last_detail)
