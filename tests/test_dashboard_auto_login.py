"""tests/test_dashboard_auto_login.py

GET /auto-login?key=... tek kullanimlik anahtari HARCIYOR ve yanit HTML'inde
oturum + suresiz cihaz token'i donuyordu. QR URL'si bir sohbet uygulamasina
yapistirilinca ya da QR okuyucu linki onceden acinca (link preview/prefetch)
token'lari o servis aliyor, gercek telefon "Link Expired" goruyordu. Ayrica
/auto-login, /login'deki IP basina deneme sinirini atliyordu.

Artik GET yalnizca durumsuz bir sayfa doner; anahtari harcayan ve token
veren islem, sayfadaki betigin yaptigi POST'tur (onizleyiciler JS calistirip
POST atmaz) ve /login ile ayni hiz sinirina tabidir.
"""
import pytest

pytest.importorskip("fastapi")


@pytest.fixture
def dash(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setattr("jarvis.dashboard.server._local_ip", lambda: "127.0.0.1")
    from fastapi.testclient import TestClient
    from jarvis.dashboard.server import DashboardServer

    dashboard = DashboardServer()
    return dashboard, TestClient(dashboard.app)


def test_get_does_not_consume_key_or_issue_tokens(dash):
    dashboard, client = dash
    key = dashboard.new_key()

    r = client.get("/auto-login", params={"key": key})

    assert r.status_code == 200
    assert key in dashboard._pending_keys          # anahtar hala kullanilabilir
    assert dashboard._tokens == set()               # hic oturum acilmadi
    assert dashboard._device_sessions == {}
    assert key not in r.text                        # anahtar sayfaya yansitilmaz


def test_prefetch_then_real_phone_still_logs_in(dash):
    dashboard, client = dash
    key = dashboard.new_key()
    client.get("/auto-login", params={"key": key})  # link onizleyici

    r = client.post("/auto-login", json={"key": key})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert dashboard._valid_token(body["token"])
    assert body["device_token"] in dashboard._device_sessions
    assert key not in dashboard._pending_keys       # POST anahtari harcar


def test_post_key_is_single_use(dash):
    dashboard, client = dash
    key = dashboard.new_key()
    assert client.post("/auto-login", json={"key": key}).status_code == 200

    again = client.post("/auto-login", json={"key": key})

    assert again.status_code == 401
    assert again.json()["ok"] is False


@pytest.mark.parametrize("body", [{"key": "WRONGKEY"}, {"key": ""}, {"key": 5}, {}, ["x"]])
def test_post_rejects_invalid_keys(dash, body):
    dashboard, client = dash
    dashboard.new_key()
    r = client.post("/auto-login", json=body)
    assert r.status_code in (400, 401)
    assert dashboard._tokens == set()


def test_post_rejects_expired_key(dash):
    dashboard, client = dash
    key = dashboard.new_key()
    dashboard._pending_keys[key] = 0  # suresi dolmus
    r = client.post("/auto-login", json={"key": key})
    assert r.status_code == 401
    assert dashboard._tokens == set()


def test_post_is_rate_limited_like_login(dash):
    dashboard, client = dash
    key = dashboard.new_key()
    for _ in range(dashboard._LOGIN_MAX_ATTEMPTS):
        assert client.post("/auto-login", json={"key": "BADBADBA"}).status_code == 401

    blocked = client.post("/auto-login", json={"key": key})

    assert blocked.status_code == 429
    assert key in dashboard._pending_keys


def test_failed_login_attempts_count_toward_auto_login_limit(dash):
    dashboard, client = dash
    key = dashboard.new_key()
    for _ in range(dashboard._LOGIN_MAX_ATTEMPTS):
        client.post("/login", json={"pin": "BADBADBA"})
    assert client.post("/auto-login", json={"key": key}).status_code == 429


def test_get_page_does_not_reflect_query(dash):
    _, client = dash
    payload = "</script><script>alert(1)</script>"
    r = client.get("/auto-login", params={"key": payload})
    assert payload not in r.text
    assert "alert(1)" not in r.text
