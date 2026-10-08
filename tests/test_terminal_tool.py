"""Regression tests for the controlled terminal tool."""

from types import SimpleNamespace
import tempfile
from pathlib import Path

import pytest

from jarvis.actions.terminal_tool import terminal_tool
from jarvis.core.approval_service import approval_service


def _code(text: str) -> str:
    return next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Onay kodu:"))


def test_readonly_command_runs_without_confirmation():
    result = terminal_tool({"command": "python --version"})
    assert result.startswith("exit_code: 0")


def test_code_command_is_preview_only_until_next_user_turn():
    command = 'python -c "print(123)"'
    preview = terminal_tool({"command": command})
    assert preview.startswith("ONAY GEREKLİ")
    code = _code(preview)

    # The model cannot approve its own preview in the same turn.
    same_turn = terminal_tool({"command": command, "confirm_code": code})
    assert same_turn.startswith("ONAY GEREKLİ")

    approval_service.mark_user_turn()
    executed = terminal_tool({"command": command, "confirm_code": code})
    assert executed.startswith("exit_code: 0")
    assert "123" in executed


def test_approval_code_is_bound_to_exact_command():
    command = 'python -c "print(456)"'
    preview = terminal_tool({"command": command})
    code = _code(preview)
    approval_service.mark_user_turn()
    other = terminal_tool({"command": 'python -c "print(789)"', "confirm_code": code})
    assert other.startswith("ONAY GEREKLİ")


# ── Uzun tanılama komutları ve test verisi izolasyonu ──


def test_pytest_uses_long_bounded_timeout_and_isolated_jarvis_home(monkeypatch, tmp_path):
    import jarvis.actions.terminal_tool as terminal_module

    calls = {}

    def fake_run(*args, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(terminal_module.subprocess, "run", fake_run)
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    command = "python -m pytest -q"
    preview = terminal_tool({"command": command})
    assert preview.startswith("ONAY GEREKLİ")
    assert "Timeout: 300 saniye" in preview
    code = _code(preview)

    approval_service.mark_user_turn()
    result = terminal_tool({"command": command, "confirm_code": code})

    assert result.startswith("exit_code: 0")
    assert calls["timeout"] == 300
    assert calls["env"]["JARVIS_HOME"] != str(tmp_path)
    diagnostic_home = calls["env"]["JARVIS_HOME"]
    assert Path(diagnostic_home).parent == Path(tempfile.gettempdir())
    assert Path(diagnostic_home).name.startswith("jarvis-terminal-")


def test_explicit_terminal_timeout_is_bounded(monkeypatch):
    import jarvis.actions.terminal_tool as terminal_module

    monkeypatch.setattr(
        terminal_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert terminal_tool({"command": "python --version", "timeout": 120}).startswith("exit_code: 0")
    assert terminal_tool({"command": "python --version", "timeout": 901}) == (
        "timeout 1 ile 900 saniye arasında olmalı."
    )


# ── Salt-okunur sayılan programların yazan/çalıştıran bayrakları ──
# _is_readonly "-" ile başlayan token'ları atlıyordu: find -delete/-exec,
# rg --pre, git diff --output, git branch -D onay kodu istemeden çalışıyordu.

from jarvis.actions.terminal_tool import _is_readonly


@pytest.mark.parametrize("argv", [
    ["find", ".", "-delete"],
    ["find", ".", "-type", "f", "-exec", "rm", "{}", "+"],
    ["find", ".", "-execdir", "sh", "-c", "x", ";"],
    ["find", ".", "-ok", "rm", "{}", ";"],
    ["find", ".", "-okdir", "rm", "{}", ";"],
    ["find", ".", "-fprint", "out.txt"],
    ["find", ".", "-fprintf", "out.txt", "%p"],
    ["find", ".", "-fls", "out.txt"],
    ["rg", "--pre", "sh", "x"],
    ["rg", "--pre=sh", "x"],
    ["git", "diff", "--output=.bashrc"],
    ["git", "log", "--output", ".bashrc"],
    ["git", "show", "--output=x"],
    ["git", "branch", "-D", "main"],
    ["git", "branch", "--delete", "main"],
    ["git", "branch", "-m", "a", "b"],
    ["git", "branch", "yeni-dal"],
])
def test_writing_flags_are_not_readonly(argv):
    assert _is_readonly(argv) is False


@pytest.mark.parametrize("argv", [
    ["find", ".", "-name", "*.py"],
    ["find", ".", "-type", "f", "-print"],
    ["rg", "-n", "TODO"],
    ["git", "diff", "--stat"],
    ["git", "log", "--oneline", "-5"],
    ["git", "branch"],
    ["git", "branch", "-a", "-v"],
    ["git", "branch", "--show-current"],
])
def test_plain_readonly_commands_stay_readonly(argv):
    assert _is_readonly(argv) is True


def test_find_delete_requires_approval_and_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    victim = tmp_path / "Belgeler" / "tez.docx"
    victim.parent.mkdir()
    victim.write_text("onemli")

    result = terminal_tool({"command": f"find {victim.parent} -type f -delete"})

    assert result.startswith("ONAY GEREKLİ")
    assert victim.exists()
