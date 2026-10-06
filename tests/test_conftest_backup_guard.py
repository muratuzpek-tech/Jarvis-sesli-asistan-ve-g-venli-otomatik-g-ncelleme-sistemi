"""tests/test_conftest_backup_guard.py

JarvisBackupTool.for_jarvis() yedekleri HOME/JARVIS_HOME'dan BAGIMSIZ
olarak depodaki src/jarvis_yedekler/ klasorune yazar (backups_root_for(
jarvis_project_root())). Adim 3.4'te bir test gercek executor uzerinden
backup_create calistirdi ve depoda 60 MB'lik yedek klasorleri birakti;
conftest bunu yakalamiyordu.

conftest artik her testten once/sonra (ve oturum sonunda) bu klasorun
alt ogelerini karsilastirir; yeni bir oge olusursa test basarisiz olur.

Kanit pytester ile: GERCEK tests/conftest.py ile ic bir pytest oturumu
calisir; ic test GERCEK JarvisBackupTool.for_jarvis().create_backup()
cagirir. Gercek depoya dokunulmamasi icin jarvis_project_root tmp'deki
kucuk bir klasore yonlendirilir (ic oturum ayni surecte calisir).
"""
from pathlib import Path

import pytest

import jarvis.backup_tool as bt

pytest_plugins = ["pytester"]

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"


@pytest.fixture
def fake_project(tmp_path, monkeypatch):
    project = tmp_path / "proj" / "jarvis"
    project.mkdir(parents=True)
    (project / "__init__.py").write_text("# test\n", encoding="utf-8")
    monkeypatch.setattr(bt, "jarvis_project_root", lambda: project)
    root = bt.backups_root_for(project)
    assert root == tmp_path / "proj" / "jarvis_yedekler"
    return root


def _run_inner(pytester, body: str):
    pytester.makeconftest(_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_inner=body)
    return pytester.runpytest_inprocess("-p", "no:cacheprovider", "-q")


def test_backup_created_during_a_test_fails_that_test(pytester, fake_project):
    result = _run_inner(pytester, """
from jarvis.backup_tool import JarvisBackupTool

def test_writes_a_project_backup():
    JarvisBackupTool.for_jarvis().create_backup()
""")
    created = [p.name for p in fake_project.iterdir()] if fake_project.is_dir() else []
    assert created, "ic test gercekten yedek olusturmali (kanit gecerli)"
    outcomes = result.parseoutcomes()
    assert outcomes.get("errors", 0) + outcomes.get("failed", 0) >= 1, result.stdout.str()
    assert "jarvis_yedekler" in result.stdout.str()


def test_tests_that_do_not_back_up_still_pass(pytester, fake_project):
    result = _run_inner(pytester, """
def test_nothing():
    assert True
""")
    result.assert_outcomes(passed=1)
