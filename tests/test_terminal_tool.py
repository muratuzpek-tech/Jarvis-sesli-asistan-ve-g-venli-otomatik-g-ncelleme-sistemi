"""Regression tests for the controlled terminal tool."""

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


# ── Salt-okunur sayılan programların yazan/çalıştıran bayrakları ──
# _is_readonly "-" ile başlayan token'ları atlıyordu: find -delete/-exec,
# rg --pre, git diff --output, git branch -D onay kodu istemeden çalışıyordu.

import pytest

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
