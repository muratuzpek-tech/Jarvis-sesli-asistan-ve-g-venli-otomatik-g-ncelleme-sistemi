"""Regression tests for browser session startup failures."""

import pytest

from jarvis.actions.browser_control import _BrowserSession


def test_start_propagates_browser_initialization_error(monkeypatch):
    session = _BrowserSession("chrome")

    async def fail_init():
        raise OSError("simulated Playwright startup failure")

    monkeypatch.setattr(session, "_async_init", fail_init)

    with pytest.raises(RuntimeError, match="Could not initialize browser session") as exc:
        session.start()

    assert isinstance(exc.value.__cause__, OSError)
    assert "simulated Playwright startup failure" in str(exc.value.__cause__)
    assert session._ready.is_set()
    assert session._loop is not None
    assert session._loop.is_closed()
