"""Regression tests for speech-driven Windows actions and read-only queries."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from jarvis.actions import computer_settings as cs
from jarvis.actions import open_app, tools_kopru, windows_shell
from jarvis.actions.capability_resolver import resolve_capability
from jarvis.actions.intent_router import match_system_read


@pytest.mark.parametrize(
    "text",
    [
        "Hangi programlar RAM kullanıyor?",
        "En çok bellek kullanan uygulamaları göster",
        "RAM kullanan uygulamalar hangileri?",
        "Bellek bölümündeki programları listele",
    ],
)
def test_per_process_memory_questions_route_to_process_list(text: str) -> None:
    assert match_system_read(text) == "process_list"


def test_opening_task_manager_is_not_misrouted_as_a_process_query() -> None:
    assert resolve_capability("Open Task Manager") is None
    assert resolve_capability("Task Manager process list") == {
        "capability": "windows_system",
        "tool": "windows_system",
        "command": "process_list",
        "action": "windows_system",
        "parameters": {"command_name": "process_list"},
    }


def test_process_list_is_fixed_sorted_and_limited() -> None:
    argv = windows_shell._ALLOWED_COMMANDS["process_list"]
    command = argv[-1]
    assert "Sort-Object -Property WorkingSet -Descending" in command
    assert "Select-Object -First 15" in command
    assert "MemoryMB" in command
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]


def test_windows_process_list_ignores_model_supplied_shell_text(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return windows_shell.subprocess.CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr(windows_shell.subprocess, "run", fake_run)
    result = json.loads(windows_shell.run({"command_name": "process_list", "script": "Remove-Item C:\\"}))

    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0][0] == windows_shell._ALLOWED_COMMANDS["process_list"]
    assert calls[0][1].get("shell", False) is False


def test_task_manager_opens_without_pyautogui_and_reports_verified_process(monkeypatch) -> None:
    monkeypatch.setattr(cs, "_OS", "Windows")
    monkeypatch.setattr(cs, "_PYAUTOGUI", False)
    monkeypatch.setattr(cs, "_wait_for_windows_process", lambda *args, **kwargs: True)
    monkeypatch.setattr(cs, "log_action", lambda **kwargs: None)

    result = cs.computer_settings({"action": "task_manager"})

    assert result == "VERIFIED: Task Manager process is running; window visibility was not checked."


def test_task_manager_launch_failure_is_not_reported_as_success(monkeypatch) -> None:
    monkeypatch.setattr(cs, "_OS", "Windows")
    monkeypatch.setattr(cs, "_PYAUTOGUI", False)
    monkeypatch.setattr(cs, "_wait_for_windows_process", lambda *args, **kwargs: False)
    monkeypatch.setattr(cs, "log_action", lambda **kwargs: None)

    def fail_to_start(*args, **kwargs):
        raise FileNotFoundError("taskmgr.exe unavailable")

    monkeypatch.setattr(cs.subprocess, "Popen", fail_to_start)
    result = cs.computer_settings({"action": "task_manager"})

    assert result.startswith("Action failed (task_manager):")
    assert "VERIFIED" not in result


def test_windows_update_opens_exact_settings_uri_without_installing(monkeypatch) -> None:
    opened = []
    monkeypatch.setattr(cs, "_OS", "Windows")
    monkeypatch.setattr(cs, "_PYAUTOGUI", False)
    monkeypatch.setattr(cs, "_open_windows_settings_uri", opened.append)
    monkeypatch.setattr(cs, "log_action", lambda **kwargs: None)

    result = cs.computer_settings({"action": "windows_update"})

    assert opened == ["ms-settings:windowsupdate"]
    assert result == (
        "VERIFIED: Windows Settings process is running; the Windows Update page was requested "
        "but not visually checked."
    )


def test_settings_open_failure_is_reported_not_hidden(monkeypatch) -> None:
    monkeypatch.setattr(cs, "_OS", "Windows")
    monkeypatch.setattr(cs, "_PYAUTOGUI", False)
    monkeypatch.setattr(
        cs, "_open_windows_settings_uri", lambda uri: (_ for _ in ()).throw(OSError("blocked"))
    )
    monkeypatch.setattr(cs, "log_action", lambda **kwargs: None)

    result = cs.computer_settings({"action": "windows_update"})

    assert result.startswith("Action failed (windows_update): blocked")


def test_read_only_window_launches_do_not_require_agent_approval() -> None:
    for action in ("task_manager", "open_settings", "windows_update"):
        assert tools_kopru.is_destructive("computer_settings", {"action": action}) is False


def test_windows_open_app_does_not_claim_success_without_process(monkeypatch) -> None:
    monkeypatch.setattr(open_app, "_SYSTEM", "Windows")
    monkeypatch.setattr(open_app.shutil, "which", lambda name: "C:/Apps/example.exe")
    monkeypatch.setattr(open_app.subprocess, "Popen", lambda *args, **kwargs: object())
    monkeypatch.setattr(open_app, "_wait_for_windows_process", lambda *args, **kwargs: False)

    assert open_app._launch_windows("example.exe") is False


def test_windows_process_verification_accepts_exe_suffix(monkeypatch) -> None:
    monkeypatch.setattr(open_app, "_PSUTIL", True)
    monkeypatch.setattr(
        open_app.psutil,
        "process_iter",
        lambda attrs: [SimpleNamespace(info={"name": "chrome.exe"})],
    )

    assert open_app._windows_process_running("chrome") is True
