"""tests/test_brain_risk_resolution.py

Brain Team'de risk ve yurutme ayni adimi FARKLI cozuyordu:
  * _risk_of_step yalnizca adim ACIKLAMASINDAN eylem cikariyordu
    (_resolve_executor_call -> _infer_executor_action). "Masaustune
    notlar3.txt olustur" metninde "dosya" kelimesi olmadigi icin salt-okunur
    'info' -> LOW buluyordu.
  * _execute_step ise gorevdeki file_modification bilgisinden create_file
    calistiriyordu.
Sonuc: gorev cd09e55e onay istenmeden COMPLETED oldu, dosya olustu.

Artik risk icin sorulan (tool, action) ile yurutulecek (tool, action) ayni
yardimcidan gelir; cozumleme hata verirse risk fail-closed HIGH'tir.

Bu testlerin HICBIRI _risk_of_step'i stub'lamaz. Gercek SecurityAI
siniflandirmasi kullanilir (LLM yok). _execute_step gercek haliyle calisir
ama executor_ai'ye giden mesaj yalnizca KAYDEDILIR - diske hicbir sey
yazilmaz. _finish_step (auditor LLM'i cagirir) devre disidir.

IZOLASYON: HOME/JARVIS_HOME tmp_path; gorev deposu tmp; import aninda gercek
log dizinine baglanan logger'lar test boyunca tmp'ye cevrilir; oturum
sonunda gercek ~/.local/share/MuratJARVIS dosyalarinin degismedigi dogrulanir.
"""
import hashlib
import importlib
import logging
import os
from pathlib import Path

import pytest


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_DATA = _real_home() / ".local" / "share" / "MuratJARVIS"
_GUARDED = [REAL_DATA / "tasks" / "brain_tasks.json"] + sorted((REAL_DATA / "logs").glob("*.log"))


def _snapshot():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in _GUARDED}


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK MuratJARVIS verisine dokundu!"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    import jarvis.actions.file_controller as fc_mod
    importlib.reload(fc_mod)

    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
        lg.setLevel(logging.INFO)
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)

    import jarvis.core.brain_orchestrator as bo
    from jarvis.brains.security_ai import SecurityAI
    from jarvis.core.task_manager import TaskManager
    from jarvis.paths import tasks_dir
    tmp = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(tmp)
    assert tasks_dir().resolve().is_relative_to(tmp)

    o = bo.BrainOrchestrator()
    o.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    o._last_player = None
    o.asked = []      # risk icin security_ai'ye sorulan (tool, action)
    o.executed = []   # executor_ai'ye gonderilen (tool, action)

    def fake_send(frm, to, task, payload=None, **kw):
        if to == "security_ai":
            o.asked.append((payload["tool"], payload["action"]))
            risk = SecurityAI.classify_risk(payload["tool"], payload["action"])
            return {"status": "completed", "result": {"risk": risk, "reason": "test"}}
        if to == "executor_ai":
            params = payload.get("params") or {}
            o.executed.append((payload["action"], params.get("action")))
            return {"status": "completed", "result": {"action": payload["action"], "result": "kaydedildi"}}
        raise AssertionError(f"beklenmeyen bus.send: {to}")

    o.bus.send = fake_send
    real_execute = o._execute_step
    o.execute_calls = []

    def recording_execute(task, step):
        o.execute_calls.append(step)
        return real_execute(task, step)

    o._execute_step = recording_execute
    o._finish_step = lambda task, step, result: None

    yield o

    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)
    monkeypatch.undo()
    importlib.reload(fc_mod)


def _task(o, description, file_mod):
    return o.tasks.create(
        name=f"[FILE_MODIFICATION] {description}", agent="executor_ai", priority="medium",
        payload={"goal": description,
                 "plan": [{"order": 1, "description": description, "agent": "executor_ai",
                           "operation": "execute", "priority": "medium", "file_path": ""}],
                 "step_index": 0, "history": [], "audit_retries": 0,
                 "file_modification": file_mod})


def _tick(o, description, file_mod):
    t = _task(o, description, file_mod)
    o._tick()
    return o.tasks.get(t["id"])


# ── Aciklamada "dosya" yok: file_modification yine HIGH olmali ──

MUTATING = {
    "create_file": {"action": "create_file", "path": "desktop", "name": "notlar3.txt", "content": ""},
    "delete": {"action": "delete", "path": "desktop", "name": "notlar3.txt"},
    "move": {"action": "move", "path": "desktop", "name": "notlar3.txt", "destination": "documents"},
}


@pytest.mark.parametrize("action", list(MUTATING))
def test_mutating_file_modification_waits_for_approval(orch, action):
    t = _tick(orch, "Masaüstüne notlar3.txt oluştur", MUTATING[action])
    assert orch.asked == [("file_controller", action)], orch.asked
    assert t["status"] == "waiting_approval", t["status"]
    assert t["payload"]["pending_step"]["risk"] == "high"
    assert orch.execute_calls == []
    assert orch.executed == []


def test_old_description_with_dosya_still_waits(orch):
    t = _tick(orch, "Masaüstüne notlar.txt diye bir dosya oluştur",
              {"action": "create_file", "path": "desktop", "name": "notlar.txt", "content": "x"})
    assert t["status"] == "waiting_approval"
    assert orch.execute_calls == []


@pytest.mark.parametrize("file_mod,expected", [
    ({"action": "list", "path": "desktop"}, "list"),
    ({"action": "info", "path": "desktop", "name": "notlar.txt"}, "info"),
])
def test_readonly_step_proceeds_without_approval(orch, file_mod, expected):
    t = _tick(orch, "Masaüstüne bak", file_mod)
    assert t["status"] != "waiting_approval"
    assert orch.asked == [("file_controller", expected)]
    assert orch.executed == [("file_controller", expected)]
    assert len(orch.execute_calls) == 1


def test_resolution_error_is_fail_closed_high(orch, monkeypatch):
    def boom(*a, **k):
        raise ValueError("çözümlenemedi")

    monkeypatch.setattr(orch, "_resolve_action_with_file_modification", boom)
    t = _tick(orch, "Masaüstüne notlar3.txt oluştur", MUTATING["create_file"])
    assert t["status"] == "waiting_approval", t["status"]
    assert t["payload"]["pending_step"]["risk"] == "high"
    assert "çözümlenemedi" in t["payload"]["pending_step"]["reason"]
    assert orch.execute_calls == []


def _approve_as_user(o, tid):
    """main.py'nin kullanici-turu yolunun yaptigi gibi onaylar: depodaki
    istegin cagrisi (adimin kapidaki cozulmus hali) approved_call olarak
    verilir (Adim 3.4; onay kaniti olmadan approve calistirmaz)."""
    task = o.tasks.get(tid)
    return o.approve(tid, approved_call=o._approval_call(task, task["payload"]["pending_step"]))


# ── Risk icin sorulan == yurutulecek ──

@pytest.mark.parametrize("action", list(MUTATING))
def test_asked_equals_executed_after_approval(orch, action):
    t = _tick(orch, "Masaüstüne notlar3.txt oluştur", MUTATING[action])
    assert t["status"] == "waiting_approval"
    _approve_as_user(orch, t["id"])
    assert orch.executed, "onaydan sonra adim calistirilmadi"
    assert orch.asked == orch.executed == [("file_controller", action)]


def test_asked_equals_executed_for_readonly(orch):
    _tick(orch, "Masaüstündeki dosyaları göster", {"action": "list", "path": "desktop"})
    assert orch.asked == orch.executed == [("file_controller", "list")]


def test_asked_equals_executed_without_file_modification(orch):
    """file_modification yoksa iki taraf da ayni aciklama cozumlemesini kullanir."""
    t = _tick(orch, "rapor.txt adlı bir dosya oluştur", None)
    assert t["status"] == "waiting_approval"
    _approve_as_user(orch, t["id"])
    assert orch.asked == orch.executed == [("file_controller", "create_file")]
