"""Manual browser E2E for dashboard WebCrypto AES-GCM -> FastAPI.

Run explicitly because CI does not install a browser by default:
    python -m playwright install chromium
    QT_QPA_PLATFORM=offscreen pytest -q tests/manual/test_dashboard_e2e.py
"""
from __future__ import annotations

import socket
import threading
import time
from contextlib import closing

import pytest
from playwright.sync_api import sync_playwright

from jarvis.dashboard.server import DashboardServer


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"dashboard did not start on port {port}")


def test_browser_webcrypto_payload_is_accepted_and_tampering_is_rejected():
    uvicorn = pytest.importorskip("uvicorn")
    dashboard = DashboardServer()
    key = dashboard.new_key()
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(dashboard.app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_server(port)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            base = f"http://127.0.0.1:{port}"

            page.goto(f"{base}/login", wait_until="networkidle")
            page.locator("#key").fill(key)
            page.wait_for_url(f"{base}/")
            page.locator("#enc").wait_for_function(
                "element => element.classList.contains('on')"
            )

            page.locator("#inp").fill("E2E AES-GCM command")
            with page.expect_request(lambda request: request.url.endswith("/api/command")) as request_info:
                page.locator("#inp").press("Enter")
            request = request_info.value
            body = request.post_data_json

            assert isinstance(body, dict)
            assert isinstance(body.get("enc"), str)
            assert "text" not in body
            token = page.evaluate("sessionStorage.getItem('jarvis_token')")
            assert dashboard._decrypt(token, body["enc"]) == "E2E AES-GCM command"

            raw = bytearray(__import__("base64").b64decode(body["enc"]))
            raw[-1] ^= 1
            tampered = __import__("base64").b64encode(raw).decode("ascii")
            response = page.request.post(
                f"{base}/api/command",
                headers={"Authorization": f"Bearer {token}"},
                data={"enc": tampered},
            )
            assert response.status == 400
            assert response.json()["error"] == "Decryption failed"
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()
