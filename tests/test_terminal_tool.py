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
