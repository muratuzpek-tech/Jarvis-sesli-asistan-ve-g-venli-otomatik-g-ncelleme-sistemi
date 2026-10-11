"""tests/test_agentic_coder_paths.py — agentic_coder proje dizini sınırlaması

LLM'den gelen dosya adı ile yazma / çalıştırma / okuma proje dizini dışına
çıkamamalı: onay "bu projeye kod yaz" içindir, $HOME'un herhangi bir yerine
yazıp orayı çalıştırmak için değil.
"""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac


# ── _contained_path ───────────────────────────────────────────

def test_contained_relative_and_nested(tmp_path):
    assert ac._contained_path(tmp_path, "main.py") == (tmp_path / "main.py").resolve()
    assert ac._contained_path(tmp_path, "pkg/mod.py") == (tmp_path / "pkg/mod.py").resolve()


@pytest.mark.parametrize("name", ["../x.py", "a/../../x.py", "/etc/passwd", "", ".", "a\x00b"])
def test_contained_rejects_escape(tmp_path, name):
    root = tmp_path / "proj"
    root.mkdir()
    assert ac._contained_path(root, name) is None


def test_contained_rejects_symlink_escape(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    assert ac._contained_path(root, "link/evil.py") is None


# ── _run_file ─────────────────────────────────────────────────

_MARKER_SCRIPT = "from pathlib import Path\nPath({marker!r}).write_text('ran')\n"


def test_run_file_blocks_outside_root(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    marker = tmp_path / "marker"
    script = tmp_path / "evil.py"
    script.write_text(_MARKER_SCRIPT.format(marker=str(marker)))

    out = ac._run_file(script, root=root)

    assert out.startswith("[BLOCKED]")
    assert not marker.exists()


def test_run_file_runs_inside_root(tmp_path):
    marker = tmp_path / "marker"
    script = tmp_path / "ok.py"
    script.write_text(_MARKER_SCRIPT.format(marker=str(marker)))

    out = ac._run_file(script, root=tmp_path)

    assert out.startswith("[SUCCESS]")
    assert marker.exists()


# ── solve() uçtan uca: sahte LLM proje dışına çıkmaya çalışır ──

_LONG_CODE = (
    '"""Proje disina yazilmaya calisilan modul."""\n\n\n'
    "def toplam(a: int, b: int) -> int:\n"
    '    """Iki sayiyi toplar ve sonucu dondurur."""\n'
    "    return a + b\n\n\n"
    "def carpim(a: int, b: int) -> int:\n"
    '    """Iki sayiyi carpar ve sonucu dondurur."""\n'
    "    return a * b\n"
)


def test_solve_never_touches_outside_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    project = home / "proj"

    # Proje dışında, ama $HOME içinde: eski kontrol bunlara izin veriyordu.
    marker = home / "ran_marker"
    outside_script = home / "outside.py"
    outside_script.write_text(_MARKER_SCRIPT.format(marker=str(marker)))
    secret = home / "secret.txt"
    secret.write_text("GIZLI-ANAHTAR-123")

    decisions = iter([
        {"action": "write", "args": {"filename": "../escape.py", "content": _LONG_CODE}},
        {"action": "write", "args": {"filename": str(home / "abs_escape.py"), "content": _LONG_CODE}},
        {"action": "run", "args": {"command": f"python {outside_script}"}},
        {"action": "run", "args": {"filename": "../outside.py"}},
        {"action": "inspect", "args": {"filename": "../secret.txt"}},
    ])
    prompts: list[str] = []

    def fake_model(prompt: str) -> str:
        prompts.append(prompt)
        d = next(decisions, {"action": "accept", "args": {}, "response": "bitti"})
        return json.dumps({"thought": "t", **d})

    coder = ac.AgenticCoder(model_fn=fake_model, max_iterations=6)
    asyncio.run(coder.solve(description="hesap makinesi", project_path=str(project)))

    assert not (home / "escape.py").exists()
    assert not (home / "abs_escape.py").exists()
    assert not marker.exists()
    assert not any("GIZLI-ANAHTAR-123" in p for p in prompts)
    assert any("PATH_REJECTED" in p for p in prompts)
