"""Tests for browser download path safety."""

from jarvis.actions.browser_control import _safe_download_path


def test_download_path_is_below_downloads_and_sanitizes_filename(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _safe_download_path("../../rapor:özel.pdf")

    assert path.parent == (tmp_path / "Downloads").resolve()
    assert path.name == "rapor__zel.pdf"


def test_download_path_avoids_overwriting_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "rapor.pdf").write_text("existing", encoding="utf-8")

    path = _safe_download_path("rapor.pdf")

    assert path == downloads / "rapor (1).pdf"
    assert not path.exists()
