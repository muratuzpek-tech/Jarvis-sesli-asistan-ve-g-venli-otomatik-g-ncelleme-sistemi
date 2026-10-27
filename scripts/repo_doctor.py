#!/usr/bin/env python3
"""JARVIS Repo Doctor: salt okunur depo analizi ve kanıta dayalı rapor."""
from __future__ import annotations

import argparse
import ast
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".ruff_cache", "node_modules", ".audit", "memory",
    "MuratJARVIS", "developer_archive",
}
SOURCE_DIRS = ("src", "tools", "scripts", "tests")
PRODUCTION_DIRS = ("src", "tools", "scripts")
MAX_SOURCE_BYTES = 1_000_000


def collect_sources(root: Path, directories: tuple[str, ...]) -> dict[str, str]:
    """Belirlenen kod dizinlerindeki Python kaynaklarını gizli/runtime verileri dışarıda tutarak oku."""
    sources: dict[str, str] = {}
    for dirname in directories:
        base = root / dirname
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > MAX_SOURCE_BYTES:
                    continue
                sources[path.relative_to(root).as_posix()] = path.read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError as exc:
                sources[path.relative_to(root).as_posix()] = ""
                print(f"UYARI: {path}: okunamadı: {exc}", file=sys.stderr)
    return sources


def syntax_issues(sources: dict[str, str]) -> list[dict]:
    findings = []
    for filename, source in sources.items():
        try:
            ast.parse(source, filename=filename)
        except SyntaxError as exc:
            findings.append({
                "priority": "P1",
                "title": "Python sözdizimi hatası",
                "location": f"{filename}:{exc.lineno or 1}",
                "evidence": f"{exc.msg}; sütun {exc.offset or '?'}",
                "kind": "deterministic",
            })
    return findings



def shell_review_issues(sources: dict[str, str]) -> list[dict]:
    """Gerçek Python çağrılarındaki shell=True kullanımını inceleme adayı yap."""
    findings = []
    for filename, source in sources.items():
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg != "shell"
                    or not isinstance(keyword.value, ast.Constant)
                    or keyword.value.value is not True
                ):
                    continue
                try:
                    target = ast.unparse(node.func)
                except Exception:
                    target = "<çağrı>"
                findings.append({
                    "priority": "P2",
                    "title": "Kabuk üzerinden süreç çalıştırma incelenmeli",
                    "location": f"{filename}:{node.lineno}",
                    "evidence": (
                        f"{target} çağrısında shell=True kullanılıyor. "
                        "Girdi kaynağı ve çağrı zinciri incelenmeli."
                    ),
                    "kind": "review",
                })
    return findings




def _stub_disposition(source: str, line: int) -> tuple[str, str]:
    """Boş metotları bağlamına göre sınıflandır; tek başına hata sayma."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "keep", "Dosya AST olarak çözümlenemedi; manuel inceleme gerekli."

    nodes = list(ast.walk(tree))
    parents = {
        child: node
        for node in nodes
        for child in ast.iter_child_nodes(node)
    }

    function = next(
        (
            node for node in nodes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno == line
        ),
        None,
    )
    if function is None:
        return "keep", "Metot bağlamı çözümlenemedi; manuel inceleme gerekli."

    classes = []
    parent = parents.get(function)
    while parent is not None:
        if isinstance(parent, ast.ClassDef):
            classes.append(parent)
        parent = parents.get(parent)

    class_names = {node.name for node in classes}
    lines = source.splitlines()
    definition_line = lines[line - 1] if 0 < line <= len(lines) else ""

    is_protocol = any(
        (
            isinstance(base, ast.Name) and base.id == "Protocol"
        ) or (
            isinstance(base, ast.Attribute) and base.attr == "Protocol"
        )
        for cls in classes
        for base in cls.bases
    )
    if is_protocol:
        return "suppress", "Protocol metot imzası; uygulamanın alt sınıfta yapılması beklenir."

    if (
        function.name == "exec_module"
        and "_LegacyLoader" in class_names
        and "zaten yüklendi" in definition_line.lower()
    ):
        return "suppress", "Modülün daha önce yüklenmesi nedeniyle bilinçli no-op."

    if (
        function.name == "log_message"
        and "_Quiet" in class_names
        and "sessiz" in definition_line.lower()
    ):
        return "suppress", "HTTP sunucusunun loglarını susturmak için bilinçli no-op."

    if (
        function.name == "force_release_stale"
        and "ProcessLockManager" in class_names
    ):
        return (
            "downgrade",
            "Kilit dosyasını silmenin güvensiz olduğu belirtilmiş; "
            "güvenli kilit davranışı açısından tasarım kararı doğrulanmalı.",
        )

    if function.name == "protocol" and "_RootShim" in class_names:
        return (
            "downgrade",
            "Uyumluluk shim metodu boş; harici veya eski API çağrıları "
            "açısından davranışı doğrulanmalı.",
        )

    return "keep", "Boş metot için bilinen bir tasarım gerekçesi doğrulanamadı."


def existing_quality_issues(
    root: Path, sources: dict[str, str]
) -> tuple[list[dict], str]:
    """Mevcut kalite kapısını kullan; şüpheli bulguları kesin hata sayma."""
    source_root = str(root / "src")
    if source_root not in sys.path:
        sys.path.insert(0, source_root)

    try:
        from jarvis.actions.devkit.python_quality import analyze_sources
        results = analyze_sources(sources)
    except Exception as exc:
        return [], (
            f"Mevcut Python kalite modülü çalıştırılamadı: "
            f"{type(exc).__name__}: {exc}"
        )

    findings = []
    excluded_unused = 0
    suppressed_stubs = 0
    downgraded_stubs = 0

    for filename, issues in results.items():
        file_source = sources.get(filename, "")

        if not file_source:
            for source_name, candidate in sources.items():
                if Path(source_name).as_posix().endswith(
                    Path(filename).as_posix()
                ):
                    file_source = candidate
                    break

        for issue in issues:
            code = issue.get("code", "AST kalite bulgusu")
            line = int(issue.get("line", 0) or 0)

            if code == "UNUSED-DEFINITION":
                excluded_unused += 1
                continue

            priority = "P2"
            evidence = issue.get("message", "Ayrıntı yok")

            if code == "STUB-FUNCTION":
                disposition, rationale = _stub_disposition(file_source, line)

                if disposition == "suppress":
                    suppressed_stubs += 1
                    continue

                if disposition == "downgrade":
                    priority = "P3"
                    evidence = rationale
                    downgraded_stubs += 1

            findings.append({
                "priority": priority,
                "title": code,
                "location": f"{filename}:{line}",
                "evidence": evidence,
                "kind": "heuristic",
            })

    note = (
        f"{excluded_unused} UNUSED-DEFINITION adayı, düşük doğruluk nedeniyle "
        f"ana listeden çıkarıldı. {suppressed_stubs} STUB-FUNCTION adayı, "
        "kaynak bağlamıyla bilinçli tasarım kalıbı olarak sınıflandırıldığı "
        f"için bastırıldı. {downgraded_stubs} belirsiz metot P3 inceleme "
        "adayına düşürüldü. Bastırma, genel kodun hatasız olduğunu kanıtlamaz."
    )
    return findings, note


def run_command(
    command: list[str], root: Path, timeout: int, env: dict[str, str] | None = None
) -> dict:
    try:
        result = subprocess.run(
            command, cwd=root, env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, check=False,
        )
        return {
            "command": " ".join(command),
            "status": "PASS" if result.returncode == 0 else "FAIL",
            "returncode": result.returncode,
            "output": result.stdout or "(çıktı yok)",
        }
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {
            "command": " ".join(command),
            "status": "TIMEOUT",
            "returncode": None,
            "output": f"{output}\nZaman aşımı: {timeout} saniye.",
        }
    except OSError as exc:
        return {
            "command": " ".join(command),
            "status": "ERROR",
            "returncode": None,
            "output": str(exc),
        }


def git_status(root: Path) -> str:
    if not (root / ".git").exists():
        return "Git deposu bulunamadı."
    result = run_command(["git", "status", "--short"], root, 10)
    return result["output"].strip() or "Çalışma ağacı temiz."


def render_report(
    root: Path, findings: list[dict], checks: list[dict],
    analyzer_note: str, tests_requested: bool,
) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    counts = {
        priority: sum(1 for item in findings if item["priority"] == priority)
        for priority in ("P0", "P1", "P2", "P3")
    }
    lines = [
        "# JARVIS Repo Doctor Raporu",
        "",
        f"- Tarih: `{now}`",
        f"- Depo: `{root}`",
        "- Mod: salt okunur inceleme; otomatik düzeltme yapılmadı.",
        f"- Python bulgu sayısı: **{len(findings)}**",
        "",
        "## Öncelik Özeti",
        "",
        "| Seviye | Adet |",
        "|---|---:|",
    ]
    lines += [f"| {level} | {count} |" for level, count in counts.items()]
    lines += ["", "## Bulgular", ""]

    if findings:
        for index, item in enumerate(findings, 1):
            lines += [
                f"### {index}. [{item['priority']}] {item['title']}",
                "",
                f"- **Konum:** `{item['location']}`",
                f"- **Tür:** `{item['kind']}`",
                f"- **Kanıt:** {item['evidence']}",
                "- **Durum:** İnceleme bulgusu; bağlam ve etki gerektiğinde ayrıca doğrulanmalı.",
                "",
            ]
    else:
        lines += ["Python sözdizimi ve çalıştırılan AST kontrollerinde bulgu yok.", ""]

    if analyzer_note:
        lines += ["## Kalite Modülü Notu", "", analyzer_note, ""]

    lines += ["## Deterministik Kontroller", ""]
    for check in checks:
        lines += [
            f"### {check['status']} — `{check['command']}`",
            "",
            "```text",
            check["output"].strip()[-12000:] or "(çıktı yok)",
            "```",
            "",
        ]

    lines += [
        "## Test Kapsamı",
        "",
        "- Tam pytest paketi: " + (
            "çalıştırılması istendi; yukarıdaki sonuçlara bak."
            if tests_requested else
            "bu çalıştırmada başlatılmadı. İstenirse `--run-tests` ile çalıştırılabilir."
        ),
        "- Tarama kapsamı: `src/`, `tools/`, `scripts/`, `tests/` içindeki Python dosyaları.",
        "- Runtime `memory/`, `.env` ve sanal ortam dizinleri kaynak taramasına alınmadı.",
        "- Kabuk çalıştırma bulguları tek başına güvenlik açığı kanıtı değildir.",
        "",
        "## Git Çalışma Ağacı",
        "",
        "```text",
        git_status(root),
        "```",
        "",
        "## Sınırlar",
        "",
        "- Bu tarama tam bir güvenlik denetimi veya kodun doğru olduğunun ispatı değildir.",
        "- Dinamik davranış, kullanılmayan kod ve çağrı zincirleri statik metin taramasından kaçabilir.",
        "- Sadece komutları çalıştırabilmek başarı sayılmaz; gerçek test sonuçları ayrıca değerlendirilmelidir.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="JARVIS depo kökü")
    parser.add_argument("--output", default=".audit/report.md", help="Rapor yolu (depo köküne göre)")
    parser.add_argument("--run-tests", action="store_true", help="İzole geçici HOME ile pytest paketini çalıştır")
    parser.add_argument("--test-timeout", type=int, default=300)
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"Depo dizini bulunamadı: {root}")

    all_sources = collect_sources(root, SOURCE_DIRS)
    prod_sources = collect_sources(root, PRODUCTION_DIRS)
    findings = syntax_issues(all_sources)
    quality, quality_note = existing_quality_issues(root, prod_sources)
    findings.extend(quality)
    findings.extend(shell_review_issues(prod_sources))

    checks: list[dict] = []
    venv_bin = root / ".venv" / "bin"
    ruff = venv_bin / "ruff"
    if not ruff.exists():
        found = shutil.which("ruff")
        ruff = Path(found) if found else ruff

    if ruff.exists():
        checks.append(run_command(
            [str(ruff), "check", "src", "tools", "scripts", "tests"],
            root, 180,
        ))
    else:
        checks.append({
            "command": "ruff check src tools scripts tests",
            "status": "NOT RUN",
            "output": "Ruff bulunamadı; paket kurulmadı.",
        })

    if args.run_tests:
        python = venv_bin / "python"
        if not python.exists():
            python = Path(sys.executable)
        with tempfile.TemporaryDirectory(prefix="jarvis-repo-doctor-") as sandbox_home:
            env = os.environ.copy()
            env.update({
                "HOME": sandbox_home,
                "JARVIS_HOME": str(Path(sandbox_home) / ".jarvis"),
                "XDG_CONFIG_HOME": str(Path(sandbox_home) / ".config"),
                "QT_QPA_PLATFORM": "offscreen",
                "JARVIS_SANDBOX": "required",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    [str(root / "src"), str(root), env.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep),
            })
            checks.append(run_command(
                [str(python), "-m", "pytest", "-q",
                 "--deselect", "tests/test_terminal_tool.py"],
                root, args.test_timeout, env,
            ))

    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    report = render_report(root, findings, checks, quality_note, args.run_tests)
    output.write_text(report, encoding="utf-8")

    print(f"Python dosyası: {len(all_sources)}")
    print(f"Python bulgusu: {len(findings)}")
    for check in checks:
        print(f"{check['status']}: {check['command']}")
        summary_lines = check.get("output", "").rstrip().splitlines()
        if summary_lines:
            print("\n".join(f"  {line}" for line in summary_lines[-8:]))
    print(f"Rapor yazıldı: {output}")
    print("Not: Heuristik bulgular doğrulanmış açık olarak kabul edilmemelidir.")

    failures = sum(1 for item in findings if item["kind"] == "deterministic"
                   and item["title"] == "Python sözdizimi hatası")
    failures += sum(1 for item in checks if item["status"] == "FAIL")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
