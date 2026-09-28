"""Offline regression tests for GUI headless verification in dev_agent.

Root problem covered here: a generated Tkinter project with concrete
expected outputs sometimes lacks a real --headless-test mode, and the
planner's run_command stays "python main.py". Launching that command
opens root.mainloop(), which never returns until the 30s/90s timeout
budget is spent, and a stale/missing database is then misread as a
failure. These tests exercise `_build_project`'s gating logic end-to-end
with every network/model/subprocess call mocked out.
"""
from __future__ import annotations

from jarvis.actions import dev_agent as da

GUI_DESCRIPTION = (
    "Build a Tkinter GUI that scrapes a list of URLs and stores the "
    "results in a SQLite database."
)
NON_GUI_DESCRIPTION = "Scrape a list of URLs and store the results in a SQLite database."


def _plan(run_command: str = "python main.py", dependencies: list[str] | None = None,
          expected_outputs: list[dict] | None = None) -> dict:
    return {
        "project_name": "gui_scraper",
        "entry_point": "main.py",
        "files": [{"path": "main.py", "imports": []}],
        "run_command": run_command,
        "dependencies": dependencies or [],
        "expected_outputs": expected_outputs if expected_outputs is not None else [
            {"path": "database.db", "description": "one row per scraped URL"}
        ],
        "shared_data_contracts": [],
    }


def _silence_static_analysis(monkeypatch) -> None:
    """These proactive passes are unrelated to the headless gate under test
    and normally mutate files on disk; keep them inert so tests only
    observe the headless-specific behaviour."""
    monkeypatch.setattr(da, "_proactively_fix_cross_file_imports", lambda *a, **k: [])
    monkeypatch.setattr(da, "_proactively_add_request_timeouts", lambda *a, **k: [])
    monkeypatch.setattr(da, "_proactively_add_user_agent_headers", lambda *a, **k: [])
    monkeypatch.setattr(da, "_proactively_lint_generated_files", lambda *a, **k: None)
    monkeypatch.setattr(da, "_proactively_detect_gui_manual_only_triggers", lambda *a, **k: None)
    monkeypatch.setattr(da, "_detect_circular_imports", lambda *a, **k: None)
    # 2026-09-28: kalite kapisi ve kabul testi de bu testlerin konusu degil
    # (kendi testleri: test_python_quality.py, test_acceptance.py,
    # test_dev_agent_quality_gate.py). Ornek kodlar bilerek kisa/eksik oldugu
    # icin kapi hakli olarak bulgu uretir; burada yalnizca headless mantigi sinaniyor.
    monkeypatch.setattr(da, "_python_quality_issues", lambda *a, **k: {})
    monkeypatch.setattr(da, "_plan_acceptance", lambda *a, **k: None)


def _stub_successful_run(monkeypatch, run_calls: list) -> None:
    def fake_run_project(run_command, project_dir, timeout=30):
        run_calls.append(run_command)
        return "SUCCESS: processed=1 successful=1 failed=0 database_records=1"

    monkeypatch.setattr(da, "_run_project", fake_run_project)
    monkeypatch.setattr(da, "_check_expected_outputs", lambda *a, **k: [])
    monkeypatch.setattr(da, "_check_output_contents", lambda *a, **k: [])
    monkeypatch.setattr(da, "_open_vscode", lambda *a, **k: True)


def _setup_common(monkeypatch, tmp_path, plan) -> None:
    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(da, "_plan_project", lambda description, language: dict(plan))
    _silence_static_analysis(monkeypatch)


def test_gui_missing_headless_gets_focused_repair_then_runs_headless(monkeypatch, tmp_path):
    plan = _plan()
    _setup_common(monkeypatch, tmp_path, plan)

    written = {"main.py": "import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n"}
    monkeypatch.setattr(da, "_write_file", lambda **kwargs: written[kwargs["file_info"]["path"]])

    fix_calls: list[dict] = []

    def fake_fix_files(**kwargs):
        fix_calls.append(kwargs)
        assert kwargs["known_error_type"] == "lint_error"
        assert kwargs["lint_issues"] and "main.py" in kwargs["lint_issues"]
        issue_codes = [i["code"] for i in kwargs["lint_issues"]["main.py"]]
        assert "GUI-HEADLESS-VERIFICATION-MISSING" in issue_codes
        return {"main.py": "import argparse\n\ndef main():\n    p = argparse.ArgumentParser()\n"
                            "    p.add_argument('--headless-test', action='store_true')\n"
                            "    args = p.parse_args()\n"
                            "    if args.headless_test:\n        print('SUCCESS')\n"}

    monkeypatch.setattr(da, "_fix_files", fake_fix_files)

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert len(fix_calls) == 1, "exactly one focused headless repair call expected"
    assert run_calls == ["python main.py --headless-test"]
    assert "working" in result
    assert "Verified outputs" in result


def test_gui_still_missing_headless_after_repair_skips_execution(monkeypatch, tmp_path):
    plan = _plan(dependencies=["requests"])
    _setup_common(monkeypatch, tmp_path, plan)

    monkeypatch.setattr(da, "_write_file", lambda **kwargs: "import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n")
    monkeypatch.setattr(da, "_fix_files", lambda **kwargs: {})  # model returns no changes

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)
    install_calls: list[object] = []
    monkeypatch.setattr(da, "_install_dependencies", lambda *a, **k: install_calls.append(a) or "ok")

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert run_calls == [], "GUI must never be launched as a fake verification attempt"
    assert install_calls == [], "dependencies must not be installed when verification is impossible"
    assert "ATLANDI" in result or "skipped" in result.lower()
    assert str(tmp_path) in result


def test_gui_headless_repair_rate_limit_skips_execution(monkeypatch, tmp_path):
    plan = _plan()
    _setup_common(monkeypatch, tmp_path, plan)

    monkeypatch.setattr(da, "_write_file", lambda **kwargs: "import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n")

    def raise_rate_limit(**kwargs):
        raise da.RateLimitError("quota exceeded")

    monkeypatch.setattr(da, "_fix_files", raise_rate_limit)

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert run_calls == []
    assert "ATLANDI" in result or "skipped" in result.lower()


def test_headless_mode_on_non_entry_module_is_detected(monkeypatch, tmp_path):
    plan = {
        "project_name": "gui_scraper",
        "entry_point": "main.py",
        "files": [{"path": "main.py", "imports": ["runner"]}, {"path": "runner.py", "imports": []}],
        "run_command": "python main.py",
        "dependencies": [],
        "expected_outputs": [{"path": "database.db", "description": "one row per scraped URL"}],
        "shared_data_contracts": [],
    }
    _setup_common(monkeypatch, tmp_path, plan)

    written = {
        "main.py": "import tkinter as tk\nimport runner\nroot = tk.Tk()\nroot.mainloop()\n",
        "runner.py": "import argparse\n\ndef run_headless():\n    print('SUCCESS')\n\n"
                     "if __name__ == '--headless-test':\n    pass\n"
                     "# --headless-test handled via runner.run_headless()\n",
    }
    monkeypatch.setattr(da, "_write_file", lambda **kwargs: written[kwargs["file_info"]["path"]])

    fix_calls: list[dict] = []
    monkeypatch.setattr(da, "_fix_files", lambda **kwargs: fix_calls.append(kwargs) or {})

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert fix_calls == [], "headless mode already present in a non-entry file, no repair needed"
    assert run_calls == ["python main.py --headless-test"]
    assert "working" in result


def test_headless_flag_is_not_duplicated_when_already_in_run_command(monkeypatch, tmp_path):
    plan = _plan(run_command="python main.py --headless-test")
    _setup_common(monkeypatch, tmp_path, plan)

    monkeypatch.setattr(
        da, "_write_file",
        lambda **kwargs: "import argparse\n\np = argparse.ArgumentParser()\n"
                         "p.add_argument('--headless-test', action='store_true')\n",
    )
    fix_calls: list[dict] = []
    monkeypatch.setattr(da, "_fix_files", lambda **kwargs: fix_calls.append(kwargs) or {})

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert fix_calls == []
    assert run_calls == ["python main.py --headless-test"]
    assert run_calls[0].count("--headless-test") == 1
    assert "working" in result


def test_non_gui_task_retains_existing_behavior(monkeypatch, tmp_path):
    plan = _plan()
    _setup_common(monkeypatch, tmp_path, plan)

    monkeypatch.setattr(da, "_write_file", lambda **kwargs: "print('hello')\n")
    fix_calls: list[dict] = []
    monkeypatch.setattr(da, "_fix_files", lambda **kwargs: fix_calls.append(kwargs) or {})

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(NON_GUI_DESCRIPTION, "python", "", 30)

    assert fix_calls == [], "non-GUI tasks must never trigger the headless repair"
    assert run_calls == ["python main.py"], "run_command must stay untouched for non-GUI tasks"
    assert "working" in result


def test_gui_without_expected_outputs_retains_existing_behavior(monkeypatch, tmp_path):
    plan = _plan(expected_outputs=[])
    _setup_common(monkeypatch, tmp_path, plan)

    monkeypatch.setattr(da, "_write_file", lambda **kwargs: "import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n")
    fix_calls: list[dict] = []
    monkeypatch.setattr(da, "_fix_files", lambda **kwargs: fix_calls.append(kwargs) or {})

    run_calls: list[str] = []
    _stub_successful_run(monkeypatch, run_calls)

    result = da._build_project(GUI_DESCRIPTION, "python", "", 30)

    assert fix_calls == [], "no expected outputs means nothing to verify, so no headless gate"
    assert run_calls == ["python main.py"], "GUI without expected outputs keeps its normal launch"
    assert "working" in result
