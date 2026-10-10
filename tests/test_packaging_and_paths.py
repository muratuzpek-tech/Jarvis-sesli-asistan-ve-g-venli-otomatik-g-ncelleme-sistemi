"""Yeni yapının sözleşmesi: kod dizini salt okunur, veri dizini ayrı, sır yok."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import jarvis
from jarvis import paths
from jarvis.core import secure_config

PKG = Path(jarvis.__file__).resolve().parent
REPO = PKG.parents[1]


def test_version_and_entrypoint() -> None:
    assert jarvis.__version__
    from jarvis.__main__ import main

    assert callable(main)


def test_data_dirs_are_outside_the_package(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_API_KEYS", raising=False)
    for d in (
        paths.memory_dir(),
        paths.logs_dir(),
        paths.tasks_dir(),
        paths.config_dir(),
        paths.certs_dir(),
    ):
        assert d.is_dir()
        assert tmp_path in d.parents or d == tmp_path
        assert PKG not in d.parents


def test_no_runtime_state_inside_package() -> None:
    forbidden = {
        "logs",
        "tasks",
        "memory/long_term.json",
        "config/api_keys.json",
        "config/certs",
    }
    present = {rel for rel in forbidden if (PKG / rel).exists()}
    assert not present, f"kod dizinine kullanıcı verisi sızmış: {present}"


def test_no_private_keys_or_api_keys_in_repo() -> None:
    tracked = [
        p
        for p in REPO.rglob("*")
        if p.is_file()
        and "developer_archive" not in p.parts
        and ".git" not in p.parts
        # .venv içindeki 3. parti dosyaları sır taramasına dahil etme.
        and not any(part.startswith(".venv") for part in p.parts)
        and "node_modules" not in p.parts
        and "__pycache__" not in p.parts
        # Testlerin ürettiği geçici klasörleri depo sırrı olarak değerlendirme.
        and not any(part.startswith(".pytest-tmp-") for part in p.parts)
    ]

    assert not [p for p in tracked if p.suffix in {".key", ".pem", ".pfx"}]

    marker = "BEGIN " + "RSA PRIVATE KEY"  # Bu dosyanın kendisini yakalamasın.
    leaked = []
    for p in tracked:
        if p.name == "api_keys.json":
            leaked.append(p)
        elif p.suffix in {".py", ".json", ".toml", ".ps1", ".md"}:
            try:
                if marker in p.read_text(encoding="utf-8", errors="ignore"):
                    leaked.append(p)
            except OSError:
                pass

    assert not leaked, f"pakette sır var: {leaked}"


def test_api_key_is_read_from_env_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "env-key")
    assert secure_config.get_gemini_api_key() == "env-key"

    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(RuntimeError):
        secure_config.get_gemini_api_key()

    written = secure_config.save_config({"gemini_api_key": "file-key"})
    assert written.parent == paths.config_dir()
    assert json.loads(written.read_text(encoding="utf-8"))["gemini_api_key"] == "file-key"
    assert secure_config.get_gemini_api_key() == "file-key"


def test_placeholder_key_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    secure_config.save_config(
        {"gemini_api_key": "REPLACE_WITH_GEMINI_API_KEY_ENV_VAR"}
    )
    with pytest.raises(RuntimeError):
        secure_config.get_gemini_api_key()


def test_self_signed_cert_is_generated_locally(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("JARVIS_API_KEYS", raising=False)
    result = secure_config.ensure_self_signed_cert()
    assert result is not None
    key, cert = result
    assert key.read_bytes().startswith(b"-----BEGIN")
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")


def test_hud_asset_exists() -> None:
    assert paths.asset("jarvis_icon.png").is_file()


def test_legacy_top_level_imports_still_resolve(tmp_path) -> None:
    code = "import actions.web_search as m; print(m.__name__)"
    env = os.environ.copy()
    env.update({"JARVIS_HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"})
    env.pop("JARVIS_API_KEYS", None)
    out = subprocess.run(
        [sys.executable, "-c", f"import jarvis; {code}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert "web_search" in out.stdout


def test_every_module_imports(tmp_path) -> None:
    code = (
        "import importlib, importlib.util, pkgutil, jarvis, sys\n"
        "bad=[]\n"
        'optional_missing = {"jarvis.guvenli_kasa"} '
        'if importlib.util.find_spec("tkinter") is None else set()\n'
        "for m in pkgutil.walk_packages(jarvis.__path__, 'jarvis.'):\n"
        "    try: importlib.import_module(m.name)\n"
        "    except Exception as e:\n"
        "        if m.name not in optional_missing: bad.append(f'{m.name}: {e}')\n"
        "print('|'.join(bad))\n"
    )
    env = os.environ.copy()
    env.update({"JARVIS_HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"})
    env.pop("JARVIS_API_KEYS", None)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", out.stdout
