"""Dangerous computer actions must require an application-issued user grant."""

from jarvis.actions import computer_settings as cs


def test_shutdown_rejects_model_only_confirmation(monkeypatch):
    called = []
    monkeypatch.setitem(cs.ACTION_MAP, "shutdown", lambda: called.append(True))
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)

    result = cs.computer_settings({"action": "shutdown", "confirmed": "yes"})

    assert result.startswith("CONFIRMATION_REQUIRED:shutdown:")
    assert called == []


def test_shutdown_runs_only_with_internal_user_grant(monkeypatch):
    called = []
    monkeypatch.setitem(cs.ACTION_MAP, "shutdown", lambda: called.append(True))
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)

    result = cs.computer_settings({"action": "shutdown", "_user_confirmation_granted": True})

    assert result == "Done: shutdown."
    assert called == [True]


def test_restart_rejects_without_internal_user_grant(monkeypatch):
    called = []
    monkeypatch.setitem(cs.ACTION_MAP, "restart", lambda: called.append(True))
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)

    result = cs.computer_settings({"action": "restart", "confirmed": "true"})

    assert result.startswith("CONFIRMATION_REQUIRED:restart:")
    assert called == []


def test_lock_screen_requires_internal_user_grant(monkeypatch):
    called = []
    monkeypatch.setitem(cs.ACTION_MAP, "lock_screen", lambda: called.append(True))
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    result = cs.computer_settings({"action": "lock_screen"})
    assert result.startswith("CONFIRMATION_REQUIRED:lock_screen:")
    assert called == []


def test_dangerous_terminal_text_is_blocked_before_enter(monkeypatch):
    typed = []
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    monkeypatch.setattr(cs, "type_text", lambda text, press_enter_after=False: typed.append(text))

    result = cs.computer_settings(
        {"action": "type_text", "value": "shutdown now", "press_enter": "true"}
    )

    assert result.startswith("COMMAND_BLOCKED:")
    assert typed == []
