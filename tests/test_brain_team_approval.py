"""tests/test_brain_team_approval.py

Brain Team bir adimi HIGH risk gorup waiting_approval'a aldiginda:
  * kullaniciya HIC bildirim gitmiyordu (yalnizca konsola print),
  * UI "awaiting_approval" ariyordu, orkestrator "waiting_approval" yaziyordu
    (gorev aktif sayilmiyor, Onay sayaci 0),
  * kullanicinin onay verebilecegi bir yol yoktu - gorev sessizce takiliyordu.

Artik bekleyen adim icin hedef yolu acikca iceren bir ONAY ISTEGI gider,
adim main.py'deki tek kullanimlik, 60 sn TTL'li, parmak izi baglı onay
mekanizmasina (_set_pending_dangerous / _consume_dangerous_confirmation)
kaydedilir. Yalnizca kullanicinin gercek bir sonraki turundaki "evet"
orch.approve(task_id)'yi, "hayir" orch.deny(task_id)'yi cagirir. Modelin
gonderdigi task_id/confirm_code hicbir zaman onay sayilmaz.

IZOLASYON: HOME ve JARVIS_HOME tmp_path'e yonlendirilir; gorev deposu tmp'de;
jarvis.task_manager / jarvis.watchdog / jarvis.task_results logger'larinin
handler'lari test boyunca tmp dosyalarina cevrilir (bu logger'lar import
aninda gercek log dizinine baglaniyor). LLM/ag cagrisi yapilmaz. Oturum
sonunda gercek ~/.local/share/MuratJARVIS ve ev dizinindeki dosyalarin
degismedigi dogrulanir.
"""
import asyncio
import hashlib
import importlib
import json
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


REAL_HOME = _real_home()
REAL_DATA = REAL_HOME / ".local" / "share" / "MuratJARVIS"
_GUARDED = [
    REAL_DATA / "tasks" / "brain_tasks.json",
    REAL_DATA / "logs" / "task_results.log",
    REAL_DATA / "logs" / "task_manager.log",
    REAL_DATA / "logs" / "watchdog.log",
    REAL_DATA / "memory" / "audit.log",
    REAL_HOME / "Desktop" / "notlar.txt",
    REAL_HOME / "Masaüstü" / "notlar.txt",
]


def _snapshot():
    out = {}
    for p in _GUARDED:
        out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler GERCEK MuratJARVIS verisine / ev dizinine dokundu!"


class _UI:
    def __init__(self):
        self.logs = []
        self.muted = False

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    jhome = tmp_path / "jarvis_home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(jhome))
    for var in list(os.environ):
        if var.startswith("XDG_") and var.endswith("_DIR"):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    import jarvis.actions.file_controller as fc_mod
    importlib.reload(fc_mod)
    from jarvis.paths import tasks_dir, logs_dir
    tmp = tmp_path.resolve()
    assert Path.home().resolve().is_relative_to(tmp)
    assert tasks_dir().resolve().is_relative_to(tmp)
    assert logs_dir().resolve().is_relative_to(tmp)

    # Import aninda gercek log dizinine baglanan logger'lar -> tmp.
    saved = {}
    for name in ("jarvis.task_manager", "jarvis.watchdog", "jarvis.task_results"):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.level)
        lg.handlers = [logging.FileHandler(tmp_path / f"{name}.log", encoding="utf-8")]
        lg.setLevel(logging.INFO)
    import jarvis.core.watchdog as wd
    monkeypatch.setattr(wd, "LOGS_DIR", tmp_path)

    import jarvis.core.brain_orchestrator as bo
    from jarvis.core.task_manager import TaskManager
    orch = bo.BrainOrchestrator()
    orch.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    orch._risk_of_step = lambda step: ("high", "test: HIGH risk")
    monkeypatch.setattr(bo, "get_orchestrator", lambda: orch)

    approved = []
    real_approve = orch.approve

    def _record_approve(task_id):
        approved.append(task_id)
        return f"Onaylandı ve gerçekleştirildi: {task_id}"

    orch.approve = _record_approve

    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.session = None
    jl._loop = None
    jl._bg_tasks = None
    spoken = []
    jl.speak = lambda text: spoken.append(str(text))
    orch._last_player = jl

    yield SimpleNamespace(orch=orch, jl=jl, home=home, approved=approved, spoken=spoken,
                          real_approve=real_approve, tmp=tmp_path)

    for name, (handlers, level) in saved.items():
        lg = logging.getLogger(name)
        for h in lg.handlers:
            h.close()
        lg.handlers = handlers
        lg.setLevel(level)
    monkeypatch.undo()
    importlib.reload(fc_mod)


def _create_waiting_task(env, name="notlar.txt", announce=True):
    """main.py'nin [FILE_MODIFICATION] yolunun olusturdugu gorevin aynisi;
    _tick onu HIGH risk adimda waiting_approval'a alir."""
    task = env.orch.tasks.create(
        name=f"[FILE_MODIFICATION] Masaüstüne {name} oluştur",
        agent="executor_ai",
        priority="medium",
        payload={
            "goal": f"Masaüstüne {name} oluştur",
            "plan": [{"order": 1, "description": f"Masaüstüne {name} oluştur",
                      "agent": "executor_ai", "operation": "execute",
                      "priority": "medium", "file_path": ""}],
            "step_index": 0, "history": [], "audit_retries": 0,
            "file_modification": {"action": "create_file", "path": "desktop",
                                  "name": name, "content": "not"},
        },
    )
    player = env.orch._last_player
    if not announce:
        env.orch._last_player = None
    try:
        env.orch._tick()
    finally:
        env.orch._last_player = player
    t = env.orch.tasks.get(task["id"])
    assert t["status"] == "waiting_approval", t
    return task["id"]


def _status(env, tid):
    return env.orch.tasks.get(tid)["status"]


# ── 1) waiting_approval'a geciste kullaniciya onay istegi ──

def test_waiting_approval_sends_approval_request_with_target(env):
    plain = SimpleNamespace(logs=[], spoken=[])
    env.orch._last_player = SimpleNamespace(write_log=plain.logs.append, speak=plain.spoken.append)

    _create_waiting_task(env)

    text = "\n".join(plain.logs + plain.spoken)
    assert text, "kullaniciya hicbir bildirim gitmedi"
    target = str((env.home / "Desktop" / "notlar.txt").resolve())
    assert target in text
    assert "file_controller" in text and "create_file" in text
    assert "evet" in text.lower() and "hayır" in text.lower()
    assert "[BRAIN_TEAM_SONUC]" not in text        # sonuc ozeti degil, onay istegi


def test_waiting_approval_is_logged_to_task_results(env):
    _create_waiting_task(env)
    log = (env.tmp / "jarvis.task_results.log").read_text(encoding="utf-8")
    assert "WAITING_APPROVAL" in log and "notlar.txt" in log


def test_approval_request_registers_pending_in_jarvis(env):
    tid = _create_waiting_task(env)
    assert env.jl._pending_dangerous_action == "brain_team"
    assert env.jl._brain_pending_task_id == tid
    joined = "\n".join(env.jl.ui.logs + env.spoken)
    assert str((env.home / "Desktop" / "notlar.txt").resolve()) in joined


# ── 2) UI durum adi ──

def test_ui_uses_orchestrator_status_name():
    from jarvis.core.task_manager import WAITING_APPROVAL
    import jarvis.ui as ui
    assert WAITING_APPROVAL == "waiting_approval"
    assert "awaiting_approval" not in Path(ui.__file__).read_text(encoding="utf-8")


@pytest.fixture
def qt_window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    home = tmp_path / "uihome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh_ui"))
    import jarvis.ui as ui
    (tmp_path / "mem").mkdir()
    (tmp_path / "tasks").mkdir()
    monkeypatch.setattr(ui, "memory_dir", lambda: tmp_path / "mem")
    monkeypatch.setattr(ui, "tasks_dir", lambda: tmp_path / "tasks")
    w = ui.MainWindow(ui.__file__)
    yield w, tmp_path
    w.close()
    del app


def test_ui_shows_waiting_approval_task_as_active_and_counts_it(qt_window):
    w, tmp = qt_window
    (tmp / "tasks" / "brain_tasks.json").write_text(json.dumps([
        {"id": "aaa", "status": "completed", "name": "eski", "updated_at": "2026-10-05T10:00:00"},
        {"id": "52b2e97b", "status": "waiting_approval", "name": "notlar.txt oluştur",
         "updated_at": "2026-10-05T17:59:16"},
    ]), encoding="utf-8")
    w._refresh_task_center()
    assert w._task_status_lbl.text().startswith("Onay bekliyor"), w._task_status_lbl.text()
    assert "52b2e97b" in w._task_status_lbl.text()
    assert "Onay: 1" in w._tasks_summary.toPlainText()


# ── 3) Onay yalnizca gercek kullanici turundan ──

def test_user_yes_approves_the_announced_task(env):
    tid = _create_waiting_task(env)
    env.jl._on_text_command("evet")
    assert env.approved == [tid]


def test_no_user_turn_no_approval(env):
    _create_waiting_task(env)
    assert env.approved == []
    assert _status(env, env.jl._brain_pending_task_id) == "waiting_approval"


def test_model_cannot_approve_via_tool_call(env):
    tid = _create_waiting_task(env)
    assert "brain_team" not in [d["name"] for d in main_mod.TOOL_DECLARATIONS]
    for args in ({"action": "approve", "task_id": tid},
                 {"action": "approve", "task_id": tid, "confirm_code": "evet"}):
        asyncio.run(env.jl._execute_tool(SimpleNamespace(name="brain_team", args=args, id="1")))
    assert env.approved == []
    assert _status(env, tid) == "waiting_approval"


def test_model_supplied_task_id_or_code_is_not_approval(env):
    tid_a = _create_waiting_task(env, "a.txt")
    tid_b = _create_waiting_task(env, "b.txt")   # en son duyurulan B
    # Kullanici "evet" der; metin icinde A'nin id'si ve bir kod olsa bile
    # onay her zaman duyurulan (B) goreve ve gercek adim icerigine baglidir.
    env.jl._on_text_command(f"evet {tid_a} confirm_code=abc123")
    assert env.approved == [tid_b]
    assert _status(env, tid_a) == "waiting_approval"


def test_yes_is_single_use(env):
    tid = _create_waiting_task(env)
    env.jl._on_text_command("evet")
    env.jl._on_text_command("evet")
    assert env.approved == [tid]


def test_no_denies_the_task(env):
    tid = _create_waiting_task(env)
    env.jl._on_text_command("hayır")
    assert env.approved == []
    assert _status(env, tid) == "cancelled"
    assert env.jl._pending_dangerous_action is None


def test_unannounced_waiting_task_is_not_approved(env):
    _create_waiting_task(env, announce=False)
    env.jl._on_text_command("evet")
    assert env.approved == []


def test_other_tasks_approval_does_not_run_another(env):
    tid_a = _create_waiting_task(env, "a.txt")
    env.jl._on_text_command("evet")
    tid_b = _create_waiting_task(env, "b.txt")
    assert env.approved == [tid_a]
    assert _status(env, tid_b) == "waiting_approval"
    env.jl._on_text_command("evet")
    assert env.approved == [tid_a, tid_b]


def test_latest_announcement_wins_older_task_is_not_approved(env):
    tid_a = _create_waiting_task(env, "a.txt")
    tid_b = _create_waiting_task(env, "b.txt")
    env.jl._on_text_command("evet")
    assert env.approved == [tid_b]
    assert _status(env, tid_a) == "waiting_approval"


def test_changed_step_is_not_approved(env):
    tid = _create_waiting_task(env)
    task = env.orch.tasks.get(tid)
    task["payload"]["pending_step"]["description"] = "Masaüstündeki her şeyi sil"
    env.orch.tasks.update(tid, payload=task["payload"])
    env.jl._on_text_command("evet")
    assert env.approved == []


def test_expired_confirmation_is_not_approved(env, monkeypatch):
    tid = _create_waiting_task(env)
    env.jl._grant_dangerous_confirmation()        # ör. sesli kismi transkriptte "evet"
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 61)
    env.jl._on_text_command("evet")               # 61 sn sonra tur tamamlandi
    assert env.approved == []
    assert _status(env, tid) == "waiting_approval"
    env.jl._on_text_command("evet")               # taze bir onay calisir
    assert env.approved == [tid]


def test_approval_runs_the_real_orchestrator_approve(env, monkeypatch):
    """Kayit tutucu yerine gercek approve: adim calistirilir (executor stub)."""
    executed = []
    env.orch.approve = env.real_approve
    monkeypatch.setattr(env.orch, "_execute_step", lambda task, step: executed.append(step) or "File created: notlar.txt")
    monkeypatch.setattr(env.orch, "_finish_step", lambda task, step, result: None)
    tid = _create_waiting_task(env)
    env.jl._on_text_command("evet")
    assert len(executed) == 1
    assert env.orch.tasks.get(tid)["payload"]["pending_step"] is None
