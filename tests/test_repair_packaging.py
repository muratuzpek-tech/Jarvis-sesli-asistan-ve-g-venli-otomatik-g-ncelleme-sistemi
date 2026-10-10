"""Offline regression tests for the packaging area."""
from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import jarvis

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_consistent() -> None:
    import tomllib

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["project"]["version"] == "25.1.0"
    assert jarvis.__version__ == "25.1.0"


def test_ui_only_uses_mocked_ui_without_backend(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[object] = []
    backend_flags: list[bool] = []

    class FakeRoot:
        def mainloop(self) -> None:
            calls.append("mainloop")

    class FakeUI:
        def __init__(self, face_path, *, backend_enabled=True) -> None:
            calls.append(("ui", str(face_path)))
            backend_flags.append(backend_enabled)
            self.root = FakeRoot()

    fake_ui = types.ModuleType("jarvis.ui")
    fake_ui.JarvisUI = FakeUI  # type: ignore[attr-defined]

    fake_paths = types.ModuleType("jarvis.paths")
    fake_paths.asset = lambda name: tmp_path / name  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "jarvis.ui", fake_ui)
    monkeypatch.setitem(sys.modules, "jarvis.paths", fake_paths)
    monkeypatch.delenv("JARVIS_FACE", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    sys.modules.pop("jarvis.main", None)

    from jarvis.__main__ import main

    main(["--ui-only"])

    assert calls == [
        ("ui", str(tmp_path / "jarvis_icon.png")),
        "mainloop",
    ]
    assert backend_flags == [False]
    assert "jarvis.main" not in sys.modules


def test_windows_wrappers_are_directory_safe_and_non_destructive() -> None:
    for name in ("KUR_WINDOWS.cmd", "BASLAT_WINDOWS.cmd", "UI_ONIZLE_WINDOWS.cmd"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert 'cd /d "%~dp0"' in text
        assert ".venv\\scripts\\python.exe" in text
        assert "remove-item" not in text
        assert "set-executionpolicy" not in text

    install = (ROOT / "KUR_WINDOWS.cmd").read_text(encoding="utf-8").lower()
    assert "3.12" in install and "3.11" in install


def test_post_install_check_is_offline_and_isolated(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["JARVIS_HOME"] = str(tmp_path / "caller-home")
    env.pop("JARVIS_API_KEYS", None)

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/post_install_check.py")],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "POST_INSTALL_CHECK_OK" in result.stdout
    assert not (tmp_path / "caller-home").exists()

    source = (ROOT / "scripts/post_install_check.py").read_text(encoding="utf-8")
    assert "async_playwright" not in source
    assert "requests" not in source
