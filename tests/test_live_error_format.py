"""tests/test_live_error_format.py

Canli oturum yeniden baglanma dongusu her hatayi yalnizca
"[JARVIS] Error (RuntimeError)" diye yaziyordu; neden gorunmuyordu.
_describe_live_error: tur + maskelenmis mesaj (<=300) + son traceback
satirinin dosya:satir ve turu; ExceptionGroup alt istisnalari da.
"""
import pytest

from jarvis.main import _describe_live_error

KEY = "AIzaSyA1234567890abcdefghijklmnopqrstu"


def _raise(exc):
    try:
        raise exc
    except BaseException as caught:  # noqa: BLE001
        return caught


def test_runtime_error_message_is_shown_and_key_is_masked():
    err = _raise(RuntimeError(f"cannot schedule new futures after shutdown key={KEY}"))
    out = "\n".join(_describe_live_error(err))
    assert "RuntimeError" in out
    assert "cannot schedule new futures after shutdown" in out
    assert "AIza" not in out and KEY not in out
    assert "test_live_error_format.py:" in out


def test_bare_aiza_key_is_masked():
    out = "\n".join(_describe_live_error(_raise(RuntimeError(f"bad {KEY} here"))))
    assert "AIza" not in out and "bad *** here" in out


def test_message_is_truncated_to_300_chars():
    out = _describe_live_error(_raise(RuntimeError("x" * 1000)))[0]
    assert "x" * 300 in out and "x" * 301 not in out


def test_exception_group_lists_sub_exceptions():
    group = _raise(ExceptionGroup("unhandled errors in a TaskGroup", [
        _raise(RuntimeError(f"cannot schedule new futures after shutdown {KEY}")),
        _raise(ValueError("health_check bozuk")),
    ]))
    lines = _describe_live_error(group)
    out = "\n".join(lines)
    assert lines[0].lstrip().startswith("ExceptionGroup")
    assert "RuntimeError" in out and "cannot schedule new futures" in out
    assert "ValueError" in out and "health_check bozuk" in out
    assert "AIza" not in out


@pytest.mark.parametrize("exc", [RuntimeError(), RuntimeError("")])
def test_error_without_message_or_traceback_still_formats(exc):
    assert _describe_live_error(exc)[0].startswith("RuntimeError")
