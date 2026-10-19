"""Güvenli indirme aracının birim ve yerel HTTP entegrasyon testleri."""
from __future__ import annotations

import functools
import http.server
import threading

import pytest

from jarvis.actions.download_tool import _validate_url, download_file
from jarvis.core.approval_service import approval_service


def _code(preview: str) -> str:
    return preview.split("Onay kodu: ", 1)[1].splitlines()[0].strip()


@pytest.fixture(autouse=True)
def reset_approval_state():
    yield
    # Test kodları TTL beklemeden temizlenir; gerçek kullanıcı onay akışı etkilenmez.
    for code in list(getattr(approval_service, "_pending", {})):
        approval_service.cancel(code)


def test_rejects_insecure_and_private_internet_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_url("http://example.com/file.txt")
    with pytest.raises(ValueError, match="yerel/özel"):
        _validate_url("https://127.0.0.1/file.txt")


def test_first_call_is_preview_and_does_not_create_file(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    result = download_file({
        "url": "https://example.com/report.txt",
        "path": "downloads",
        "filename": "report.txt",
    })

    assert result.startswith("ONAY GEREKLİ")
    assert "indirme henüz başlamadı" in result
    assert not (downloads / "report.txt").exists()


def test_local_http_download_requires_approval_and_verifies_size(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    source = tmp_path / "source.txt"
    payload = b"JARVIS safe integration test\n"
    source.write_bytes(payload)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HOME", str(tmp_path))
    url = f"http://127.0.0.1:{server.server_port}/source.txt"
    try:
        preview = download_file({"url": url, "path": "downloads", "filename": "copy.txt"})
        code = _code(preview)
        assert not (downloads / "copy.txt").exists()
        approval_service.mark_user_turn()
        result = download_file({
            "url": url, "path": "downloads", "filename": "copy.txt", "confirm_code": code,
        })
        assert "İndirme başarılı" in result
        assert (downloads / "copy.txt").read_bytes() == payload
    finally:
        server.shutdown()
        server.server_close()


def test_existing_file_is_not_overwritten_without_explicit_flag(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "copy.txt"
    target.write_text("original", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))

    preview = download_file({
        "url": "https://example.com/copy.txt",
        "path": "downloads",
        "filename": "copy.txt",
    })
    approval_service.mark_user_turn()
    result = download_file({
        "url": "https://example.com/copy.txt",
        "path": "downloads",
        "filename": "copy.txt",
        "confirm_code": _code(preview),
    })
    assert "zaten var" in result
    assert target.read_text(encoding="utf-8") == "original"
