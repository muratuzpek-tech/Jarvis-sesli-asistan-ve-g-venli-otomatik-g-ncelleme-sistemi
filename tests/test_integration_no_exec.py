"""tests/test_integration_no_exec.py

Guvenlik bulgusu 2: entegrasyon.integrate_discovered_tool, LLM'in disaridan
gelen kaynaktan yeniden yazdigi modulu dogrulamak icin sandbox'siz bir alt
surecte `import actions.<mod>` ve `run({})` ile CALISTIRIYORDU (kullanicinin
tum yetkileri, ortam degiskenlerindeki API anahtarlari ve ag erisimiyle).

Artik dogrulama tamamen statiktir: soz dizimi, ust seviye run(), import
aninda calisacak kod (ust seviye/sinif govdesi/decorator/varsayilan arguman
icindeki cagrilar) ve eksik bagimliliklar AST ile denetlenir; modul hicbir
asamada import edilmez ya da calistirilmaz.

IZOLASYON: entegrasyon.BASE_DIR tmp_path'e cevrilir (eski kod bu klasorde
`import actions.<mod>` calistirdigi icin kirmizi test gercek repoya
dokunmadan kaniti uretir). HOME/JARVIS_HOME tmp_path; ag/LLM cagrisi yok.
"""
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def integ(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    import jarvis.actions.entegrasyon as ent
    base = tmp_path / "base"
    (base / "actions").mkdir(parents=True)
    (base / "actions" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(ent, "BASE_DIR", base)
    return ent, base


def _write_module(base: Path, name: str, source: str) -> Path:
    path = base / "actions" / f"{name}.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_import_time_code_is_never_executed(integ, tmp_path):
    ent, base = integ
    marker = tmp_path / "IMPORT_RAN"
    path = _write_module(base, "discovered_evil", (
        f"open({str(marker)!r}, 'w').write('x')\n"
        "def run(parameters: dict) -> str:\n"
        "    return 'ok'\n"
    ))
    ok, detail = ent._verify_module(path, "discovered_evil")
    assert not marker.exists(), "entegrasyon dogrulamasi LLM kodunu import ederek calistirdi"
    assert ok is False, detail


def test_run_is_never_called_during_verification(integ, tmp_path):
    ent, base = integ
    marker = tmp_path / "RUN_RAN"
    path = _write_module(base, "discovered_quiet", (
        "def run(parameters: dict) -> str:\n"
        f"    open({str(marker)!r}, 'w').write('x')\n"
        "    return 'ok'\n"
    ))
    ok, detail = ent._verify_module(path, "discovered_quiet")
    assert not marker.exists(), "entegrasyon dogrulamasi run({}) cagirdi"
    assert ok is True, detail


def test_verification_starts_no_subprocess(integ, monkeypatch):
    ent, base = integ
    path = _write_module(base, "discovered_ok", "def run(parameters: dict) -> str:\n    return 'ok'\n")

    def _forbidden(*a, **k):
        raise AssertionError(f"dogrulama alt surec baslatti: {a}")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    ok, detail = ent._verify_module(path, "discovered_ok")
    assert ok is True, detail


@pytest.mark.parametrize("source,needle", [
    ("def run(parameters: dict) -> str\n    return 'x'\n", "Söz dizimi"),
    ("import os\ndef helper():\n    return 1\n", "run"),
    ("import os\nos.system('id')\ndef run(parameters: dict) -> str:\n    return 'x'\n", "import"),
    ("class A:\n    x = print('y')\ndef run(parameters: dict) -> str:\n    return 'x'\n", "import"),
    ("@print\ndef run(parameters: dict) -> str:\n    return 'x'\n", "import"),
    ("def run(parameters: dict = print()) -> str:\n    return 'x'\n", "import"),
    ("import jarvis_olmayan_paket_123\ndef run(parameters: dict) -> str:\n    return 'x'\n", "FAILED_DEPENDENCY"),
    ("def run(parameters: dict) -> str:\n    import jarvis_olmayan_paket_123\n    return 'x'\n", "FAILED_DEPENDENCY"),
])
def test_static_checks_reject_unsafe_or_broken_modules(integ, source, needle):
    ent, base = integ
    path = _write_module(base, "discovered_bad", source)
    ok, detail = ent._verify_module(path, "discovered_bad")
    assert ok is False
    assert needle in detail, detail


def test_main_guard_and_literals_are_allowed(integ):
    ent, base = integ
    path = _write_module(base, "discovered_fine", (
        '"""Doc."""\n'
        "import json\n"
        "from pathlib import Path\n"
        "LIMIT = 10\n"
        "NAMES: tuple = ('a', 'b')\n"
        "def run(parameters: dict) -> str:\n"
        "    return json.dumps({'n': LIMIT, 'p': str(Path('.'))})\n"
        "if __name__ == '__main__':\n"
        "    print(run({}))\n"
    ))
    ok, detail = ent._verify_module(path, "discovered_fine")
    assert ok is True, detail
