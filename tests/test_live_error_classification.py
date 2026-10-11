"""tests/test_live_error_classification.py

Canli oturum hatasi siniflandirmasi (JarvisLive.run yeniden baglanma dongusu).

Eskiden _is_auth_error "401"/"403"/"1007"yi alt dize olarak ariyordu:
"retry in 31007s" gibi bir metin de "API key invalid" penceresini aciyordu.
WebSocket 1007 (invalid argument) / 1008 kapanisi aslinda bir canli oturum
ayarinin reddedilmesidir; anahtar penceresi acilmamali, log'a net mesaj
yazilmali.
"""
import pytest

from jarvis.main import _config_rejection_message, _is_auth_error, _is_config_rejection


@pytest.mark.parametrize("text", [
    "Quota exceeded, retry in 31007s",
    "server busy 31007sn",
    "request id 14010 failed",
    "port 4030 refused",
])
def test_numbers_inside_other_numbers_are_not_auth_errors(text):
    assert not _is_auth_error(text)


@pytest.mark.parametrize("text", [
    "400 API key not valid. Please pass a valid API key.",
    "API_KEY_INVALID",
    "401 UNAUTHENTICATED",
    "403 PERMISSION_DENIED",
    "Permission denied for this project",
    "HTTP 401",
    "status 403",
    "1008 API key not valid",
    "1007 None. API key not valid. Please pass a valid API key.",
])
def test_real_auth_errors_are_still_detected(text):
    assert _is_auth_error(text)


@pytest.mark.parametrize("text", [
    "received 1007 (invalid frame payload data) Request contains an invalid argument.",
    "1007 None. Request contains an invalid argument.",
    "sent 1008 (policy violation) Unsupported language code",
    "INVALID_ARGUMENT: realtime_input_config",
])
def test_websocket_setting_rejection_is_not_auth(text):
    assert not _is_auth_error(text)
    assert _is_config_rejection(text)


@pytest.mark.parametrize("text", [
    "1008 API key not valid", "Quota exceeded, retry in 31007s", "TimeoutError",
])
def test_config_rejection_needs_close_code_without_key_text(text):
    assert not _is_config_rejection(text)


def test_config_rejection_message_is_clear_and_masks_key_like_values():
    msg = _config_rejection_message(
        "1007 Request contains an invalid argument. key=AIzaSyA1234567890abcdefghijklmnopqrstu")
    assert msg.startswith("canlı oturum ayarı reddedildi: ")
    assert "invalid argument" in msg
    assert "AIzaSy" not in msg
