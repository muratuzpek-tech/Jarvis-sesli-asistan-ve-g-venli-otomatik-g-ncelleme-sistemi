"""Güvenlik kafesi (bubblewrap) — "kötü niyetli" programlarla gerçekten kapalı mı?

Makinede bubblewrap yoksa ya da kafes kurulamıyorsa (ör. kullanıcı ad alanı
kapalı CI) kafes testleri atlanır; kafessiz davranış testleri yine çalışır.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from jarvis.actions.devkit import sandbox

WORKS, WHY = sandbox.sandbox_works()
needs_bwrap = pytest.mark.skipif(not WORKS, reason=f"kafes yok: {WHY}")


def _run(tmp_path: Path, code: str, *args: str, monkeypatch=None) -> subprocess.CompletedProcess:
    proj = tmp_path / "proje"
    proj.mkdir(exist_ok=True)
    (proj / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")
    cmd, env, state = sandbox.wrap([sys.executable, str(proj / "main.py"), *args], proj, log=lambda m: None)
    assert state == "kafes"
    return subprocess.run(cmd, cwd=proj, env=env, capture_output=True, text=True, timeout=60)


@needs_bwrap
def test_project_write_and_input_read_are_allowed(tmp_path):
    girdi = tmp_path / "girdi"
    girdi.mkdir()
    (girdi / "veri.txt").write_text("merhaba", encoding="utf-8")
    r = _run(tmp_path, """
        import sys
        from pathlib import Path
        text = Path(sys.argv[1], "veri.txt").read_text()
        Path("sonuc.txt").write_text(text.upper())
        print("TAMAM")
    """, str(girdi))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "proje" / "sonuc.txt").read_text() == "MERHABA"


@needs_bwrap
def test_input_folder_is_read_only(tmp_path):
    girdi = tmp_path / "girdi"
    girdi.mkdir()
    r = _run(tmp_path, """
        import sys
        from pathlib import Path
        try:
            Path(sys.argv[1], "bozuk.txt").write_text("x")
            print("YAZDI")
        except OSError:
            print("ENGELLENDI")
    """, str(girdi))
    assert "ENGELLENDI" in r.stdout and not (girdi / "bozuk.txt").exists()


@needs_bwrap
def test_files_outside_project_are_invisible(tmp_path):
    gizli = tmp_path / "ev" / ".ssh"
    gizli.mkdir(parents=True)
    (gizli / "id_rsa").write_text("GIZLI-ANAHTAR")
    r = _run(tmp_path, f"""
        from pathlib import Path
        for p in [{str(gizli / 'id_rsa')!r}, "/root/.ssh/id_rsa", "/home"]:
            try:
                print(p, "OKUNDU", Path(p).read_text()[:20] if Path(p).is_file() else list(Path(p).iterdir())[:3])
            except OSError as e:
                print(p, "GORUNMUYOR", type(e).__name__)
        print("HOME", Path.home(), list(Path.home().iterdir()))
    """)
    assert "GIZLI-ANAHTAR" not in r.stdout
    assert r.stdout.count("GORUNMUYOR") >= 2, r.stdout
    assert "HOME /tmp/home []" in r.stdout


@needs_bwrap
def test_writing_outside_project_is_blocked(tmp_path):
    hedef = tmp_path / "disari.txt"
    r = _run(tmp_path, f"""
        from pathlib import Path
        try:
            Path({str(hedef)!r}).write_text("x")
            print("YAZDI")
        except OSError:
            print("ENGELLENDI")
    """)
    assert not hedef.exists(), r.stdout


@needs_bwrap
def test_api_keys_are_not_passed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sizmasin-123")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_sizmasin")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/user/1000/keyring/ssh")
    r = _run(tmp_path, """
        import os
        print(sorted(os.environ))
        print("\\n".join(os.environ.values()))
    """)
    for gizli in ("sizmasin-123", "ghp_sizmasin", "SSH_AUTH_SOCK", "GEMINI_API_KEY"):
        assert gizli not in r.stdout


@needs_bwrap
def test_host_processes_are_invisible(tmp_path):
    import os
    r = _run(tmp_path, """
        import os, sys
        pid = int(sys.argv[1])
        try:
            os.kill(pid, 0)
            print("GORUNDU")
        except ProcessLookupError:
            print("YOK")
        print("PIDS", sorted(int(p) for p in os.listdir("/proc") if p.isdigit()))
    """, str(os.getpid()))
    assert "YOK" in r.stdout, r.stdout


@needs_bwrap
def test_timeout_kills_the_whole_process_tree(tmp_path, monkeypatch):
    """Zaman aşımında torun süreç de ölmeli (eskiden yalnız doğrudan çocuk ölüyordu)."""
    from jarvis.actions import dev_agent as da
    proj = tmp_path / "proje"
    proj.mkdir()
    (proj / "main.py").write_text(textwrap.dedent("""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c",
                          "import time, pathlib; time.sleep(3); pathlib.Path('torun_yasiyor.txt').write_text('x')"])
        time.sleep(60)
    """), encoding="utf-8")
    out = da._run_project("python main.py", proj, timeout=1)
    assert out.startswith("Timed out")
    time.sleep(4)
    assert not (proj / "torun_yasiyor.txt").exists()


def test_required_mode_refuses_without_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX", "required")
    monkeypatch.setattr(sandbox, "sandbox_works", lambda: (False, "test: yok"))
    _, _, state = sandbox.wrap(["python", "main.py"], tmp_path, log=lambda m: None)
    assert state.startswith("REFUSED")


def test_off_mode_and_auto_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SANDBOX", "off")
    assert sandbox.wrap(["python"], tmp_path, log=lambda m: None) == (["python"], None, "kafessiz")
    monkeypatch.setenv("JARVIS_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "sandbox_works", lambda: (False, "test: yok"))
    msgs = []
    monkeypatch.setattr(sandbox, "_warned", False)
    assert sandbox.wrap(["python"], tmp_path, log=msgs.append)[2] == "kafessiz"
    assert msgs and "YOK" in msgs[0]


def test_gui_detection(tmp_path):
    (tmp_path / "a.py").write_text("import tkinter as tk\n")
    assert sandbox.project_uses_gui(tmp_path)
    (tmp_path / "a.py").write_text("import json\n")
    assert not sandbox.project_uses_gui(tmp_path)


def test_linux_default_is_required(monkeypatch):
    monkeypatch.delenv("JARVIS_SANDBOX", raising=False)
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    assert sandbox.mode() == "required"
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    assert sandbox.mode() == "auto"


@needs_bwrap
def test_go_binary_runs_in_sandbox(tmp_path, monkeypatch):
    """Derlenmiş Go ikilisi de kafeste: API anahtarını göremez (burada bir kabuk betiğiyle taklit)."""
    from jarvis.actions.devkit import go_toolchain as gt
    monkeypatch.setenv("GEMINI_API_KEY", "sizmasin-go")
    work = tmp_path / gt.WORK_DIR
    work.mkdir(parents=True)
    fake = work / gt.BINARY_NAME
    fake.write_text("#!/bin/sh\nenv; ls /home 2>&1\n")
    fake.chmod(0o755)
    tc = gt.GoToolchain.__new__(gt.GoToolchain)
    tc.project_dir = tmp_path
    r = tc.run_binary([], 30)
    assert "sizmasin-go" not in r.output and "JARVIS_IN_SANDBOX=1" in r.output, r.output


def test_interpreter_symlink_chain_roots_are_bound(tmp_path, monkeypatch):
    """murat@goxs 2026-09-29: uv '.venv/bin/python → …/cpython-3.12-linux…(takma ad)/bin/python3.12'
    zincirindeki takma ad kafese bağlanmadığı için 'execvp: No such file' alındı."""
    real = tmp_path / "uv" / "cpython-3.12.14"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.12").write_text("")
    alias = tmp_path / "uv" / "cpython-3.12"
    alias.symlink_to(real)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(alias / "bin" / "python3.12")
    monkeypatch.setattr(sandbox.sys, "executable", str(venv / "bin" / "python"))
    monkeypatch.setattr(sandbox.sys, "prefix", str(venv))
    monkeypatch.setattr(sandbox.sys, "base_prefix", str(real))
    roots = {str(p) for p in sandbox._python_runtime_paths()}
    assert {str(venv), str(alias), str(real)} <= roots
