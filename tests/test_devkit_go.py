"""devkit Go kurucusu testleri.

Birim testleri (plan doğrulama, bulgu ayrıştırma, güvenli komut çalıştırma)
her yerde çalışır. Uçtan uca testler gerçek `go` (ve varsa golangci-lint)
ile sahte bir LLM kullanır; go kurulu değilse atlanır.
"""
from __future__ import annotations

import json
import shutil
import sys
import time

import pytest

from jarvis.actions.devkit import get_builder
from jarvis.actions.devkit.go_builder import (
    PlanError,
    RateLimited,
    build_go_project,
    runtime_diagnostics,
    validate_plan,
)
from jarvis.actions.devkit.go_toolchain import sanitize_module_name
from jarvis.actions.devkit.toolchain import parse_diagnostics, run_tool

HAS_GO = shutil.which("go") is not None


# ── birim testleri ───────────────────────────────────────────────────────
def test_registry_routes_go_only():
    assert get_builder("Go") is build_go_project
    assert get_builder("golang") is build_go_project
    assert get_builder("python") is None
    assert get_builder("") is None


def _plan(**over):
    base = {"project_name": "x", "module": "example.com/x",
            "files": [{"path": "main.go", "description": "entry"}], "run_args": [], "expected_outputs": []}
    base.update(over)
    return base


@pytest.mark.parametrize("files", [
    [{"path": "../evil.go"}],
    [{"path": "/etc/evil.go"}],
    [{"path": "main.py"}],
    [{"path": "helper.go"}],                       # main.go yok
    [{"path": "main.go"}, {"path": "jarvis/x.go"}],
    [{"path": "main.go"}, {"path": ".hidden/x.go"}],
    [{"path": "main.go"}, {"path": "main.go"}],
    [{"path": "main.go"}, {"path": "main_test.go"}],
])
def test_validate_plan_rejects_bad_files(files):
    with pytest.raises(PlanError):
        validate_plan(_plan(files=files))


@pytest.mark.parametrize("outputs", [["../x.log"], ["/tmp/x.log"], [".jarvis/app"], [""]])
def test_validate_plan_rejects_bad_outputs(outputs):
    with pytest.raises(PlanError):
        validate_plan(_plan(expected_outputs=outputs))


def test_validate_plan_rejects_bad_run_args():
    with pytest.raises(PlanError):
        validate_plan(_plan(run_args="--x; rm -rf /"))
    with pytest.raises(PlanError):
        validate_plan(_plan(run_args=["a"] * 50))


def test_validate_plan_orders_main_last_and_sanitizes():
    p = validate_plan(_plan(module="Example.com/My App!",
                            files=[{"path": "main.go"}, {"path": "internal/a/a.go"}]))
    assert [f["path"] for f in p["files"]] == ["internal/a/a.go", "main.go"]
    assert p["module"] == "example.com/my-app"


def test_sanitize_module_name_fallback():
    assert sanitize_module_name("../..") == "jarvisapp"
    assert sanitize_module_name("") == "jarvisapp"


def test_parse_diagnostics_formats():
    out = "\n".join([
        "# example.com/x",
        "./main.go:6:2: declared and not used: x",
        "vet: ./internal/a/a.go:3:1: bad",
        "main.go:10:2: Error return value of `f.Close` is not checked (errcheck)",
        "main.go:1: : # example.com/x",
        "/root/go/pkg/mod/other/lib.go:1:1: not ours",
    ])
    diags = parse_diagnostics(out, "build", {"main.go", "internal/a/a.go"})
    assert [(d.path, d.line) for d in diags] == [("main.go", 6), ("internal/a/a.go", 3), ("main.go", 10)]


def test_runtime_diagnostics_maps_absolute_stack(tmp_path):
    out = f"panic: boom\n\ngoroutine 1 [running]:\nmain.main()\n\t{tmp_path}/main.go:12 +0x1d\n"
    diags = runtime_diagnostics(out, tmp_path, {"main.go"})
    assert [(d.path, d.line) for d in diags] == [("main.go", 12)]


def test_run_tool_no_shell_and_timeout(tmp_path):
    # Kabuk metakarakterleri yorumlanmaz: argüman olduğu gibi geçer.
    r = run_tool([sys.executable, "-c", "import sys; print(sys.argv[1])", "a; echo HACKED"], tmp_path, 10)
    assert r.ok and r.output == "a; echo HACKED"
    # Zaman aşımında alt süreçler dahil öldürülür.
    t0 = time.time()
    r = run_tool([sys.executable, "-c", "import time; print('start', flush=True); time.sleep(60)"], tmp_path, 1)
    assert r.timed_out and "start" in r.output and time.time() - t0 < 15
    r = run_tool(["definitely-not-a-real-binary-xyz"], tmp_path, 5)
    assert r.not_found and not r.ok


def test_missing_go_stops_before_llm(tmp_path, monkeypatch):
    import jarvis.actions.devkit.go_toolchain as gt
    monkeypatch.setattr(gt.shutil, "which", lambda name: None)

    def never(prompt):
        raise AssertionError("LLM çağrılmamalıydı")

    msg = build_go_project("x", generate=never, projects_dir=tmp_path, log=lambda m: None)
    assert "bulunamadı" in msg


def test_rate_limit_is_reported(tmp_path):
    if not HAS_GO:
        pytest.skip("go yok")

    def limited(prompt):
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    msg = build_go_project("x", generate=limited, projects_dir=tmp_path, log=lambda m: None,
                           is_rate_limit=lambda e: "429" in str(e))
    assert msg.startswith("Rate limit")
    with pytest.raises(RateLimited):
        from jarvis.actions.devkit.go_builder import _call
        _call(limited, "p", lambda e: True)


# ── uçtan uca (gerçek go araç zinciri, sahte LLM) ────────────────────────
GOOD_MAIN = '''package main

import (
	"fmt"
	"log"
	"os"
)

// writeReport raporu yazar; Close hatası da döndürülür.
func writeReport(path string) (err error) {
	f, err := os.Create(path)
	if err != nil {
		return fmt.Errorf("create: %w", err)
	}
	defer func() {
		if cerr := f.Close(); cerr != nil && err == nil {
			err = cerr
		}
	}()
	if _, err = fmt.Fprintln(f, "RAM OK"); err != nil {
		return fmt.Errorf("write: %w", err)
	}
	return nil
}

func main() {
	if err := writeReport("report.log"); err != nil {
		log.Fatalf("hata: %v", err)
	}
	fmt.Println("done")
}
'''

BAD_MAIN = '''package main

import (
	"fmt"
	"os"
)

func main() {
	unused := 42
	f, _ := os.Create("report.log")
	fmt.Fprintln(f, "RAM OK")
	f.Close()
}
'''

PLAN = {
    "project_name": "ram_report", "module": "example.com/ram_report",
    "files": [{"path": "main.go", "description": "writes report.log"}],
    "run_args": [], "long_running": False,
    "expected_outputs": [{"path": "report.log", "description": "contains RAM OK"}],
}


class FakeLLM:
    def __init__(self, writes, fixes):
        self.writes, self.fixes, self.prompts = list(writes), list(fixes), []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if prompt.startswith("You are a senior Go architect"):
            return "```json\n" + json.dumps(PLAN) + "\n```"
        if prompt.startswith("You are an expert Go debugger"):
            return self.fixes.pop(0)
        return self.writes.pop(0)


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_e2e_happy_path(tmp_path):
    llm = FakeLLM([GOOD_MAIN], [])
    opened = []
    msg = build_go_project("rapor yaz", generate=llm, projects_dir=tmp_path, log=lambda m: None,
                           open_editor=opened.append)
    assert "çalışıyor" in msg, msg
    proj = tmp_path / "ram_report"
    assert (proj / "report.log").read_text().strip() == "RAM OK"
    assert (proj / "go.mod").is_file() and opened == [proj]


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_e2e_fixes_lint_and_build_errors(tmp_path):
    llm = FakeLLM([BAD_MAIN], [GOOD_MAIN])
    logs: list[str] = []
    msg = build_go_project("rapor yaz", generate=llm, projects_dir=tmp_path, log=logs.append)
    assert "çalışıyor" in msg and "2. turda" in msg, (msg, logs)
    fix_prompt = next(p for p in llm.prompts if p.startswith("You are an expert Go debugger"))
    assert "declared and not used" in fix_prompt
    # Yedek, go araçlarının görmediği .jarvis/ altında; paket klasörüne .go yedeği bırakılmaz.
    proj = tmp_path / "ram_report"
    assert sorted(p.name for p in proj.glob("*.go")) == ["main.go"]
    assert any((proj / ".jarvis" / "backups").iterdir())


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_e2e_gives_up_cleanly(tmp_path):
    llm = FakeLLM([BAD_MAIN], [BAD_MAIN] * 10)
    logs: list[str] = []
    msg = build_go_project("rapor yaz", generate=llm, projects_dir=tmp_path, log=logs.append)
    assert "düzeltilemedi" in msg
    assert any("tekrar" in p or "EXACTLY the same" in p for p in llm.prompts)


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_e2e_accepts_code_wrapped_in_prose(tmp_path):
    wrapped = f"Here is the code:\n```go\n{GOOD_MAIN}```\nThis program writes a report."
    msg = build_go_project("rapor yaz", generate=FakeLLM([wrapped], []), projects_dir=tmp_path,
                           log=lambda m: None)
    assert "çalışıyor" in msg, msg


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_e2e_rejects_non_go_llm_output(tmp_path):
    llm = FakeLLM(["Sure! Here is your code:", "still not code"], [])
    msg = build_go_project("rapor yaz", generate=llm, projects_dir=tmp_path, log=lambda m: None)
    assert "Dosya yazılamadı" in msg
    assert not (tmp_path / "ram_report" / "main.go").exists()


# ── dev_agent entegrasyonu ───────────────────────────────────────────────
@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_dev_agent_routes_go_to_devkit(tmp_path, monkeypatch):
    import jarvis.actions.dev_agent as da

    llm = FakeLLM([GOOD_MAIN], [])
    monkeypatch.setattr(da, "_devkit_generate", llm)
    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(da, "_open_vscode", lambda p: False)
    monkeypatch.setattr(da, "_plan_project", lambda *a, **k: pytest.fail("Python akışına girmemeliydi"))

    preview = da.dev_agent({"description": "rapor yaz", "language": "Go"})
    assert "go mod tidy" in preview and "pip" not in preview
    code = preview.split("confirm_code='")[1].split("'")[0]
    # Onay kapisi fail-closed: kullanicinin gercek "evet" turu gerekir.
    monkeypatch.setattr(da, "_last_user_turn_at", None)
    monkeypatch.setattr(da, "_last_user_turn_text", None)
    da.note_user_turn("evet", now=da._pending_dev_agent[code]["issued_at"] + 5)
    result = da.dev_agent({"description": "rapor yaz", "language": "Go", "confirm_code": code})
    assert "çalışıyor" in result, result
    assert (tmp_path / "ram_report" / "report.log").is_file()


def test_binary_name_has_exe_on_windows():
    from jarvis.actions.devkit.go_toolchain import binary_name
    assert binary_name("win32") == "app.exe"
    assert binary_name("linux") == "app"


@pytest.mark.parametrize("rel", ["/tmp/x", "C:/x.log", "c:\\x.log", "\\\\srv\\share\\x", "a/../../x"])
def test_escapes_project_is_platform_independent(rel):
    from jarvis.actions.devkit.go_builder import _escapes_project

    assert _escapes_project(rel)


def test_stack_regex_keeps_windows_drive_letter():
    from jarvis.actions.devkit.go_builder import _STACK_RE

    m = _STACK_RE.search("\tC:/Users/murat/proj/main.go:7 +0x1d")
    assert m and m.group(1) == "C:/Users/murat/proj/main.go" and m.group(2) == "7"


@pytest.mark.skipif(not HAS_GO, reason="go kurulu değil")
def test_lint_only_findings_stop_blocking_after_three_rounds(tmp_path, monkeypatch):
    """Canlı test 2026-09-29: build + vet temizken yalnızca lint yüzünden 6 tur
    harcandı ve program hiç çalıştırılmadı."""
    from types import SimpleNamespace

    from jarvis.actions.devkit import go_builder

    calls = []

    def always_lint_findings(self):
        calls.append(1)
        return SimpleNamespace(ok=False, output="main.go:3:1: exported func should have comment (revive)"), []

    monkeypatch.setattr(go_builder.GoToolchain, "lint", always_lint_findings)
    llm = FakeLLM([GOOD_MAIN], [GOOD_MAIN] * 10)
    logs: list[str] = []
    msg = build_go_project("rapor yaz", generate=llm, projects_dir=tmp_path, log=logs.append)
    assert "çalışıyor" in msg and "3. turda" in msg, (msg, logs)
    assert "stil uyarıları" in msg and "revive" in msg
    assert (tmp_path / "ram_report" / "report.log").read_text().strip() == "RAM OK"
    assert len(calls) == 3
