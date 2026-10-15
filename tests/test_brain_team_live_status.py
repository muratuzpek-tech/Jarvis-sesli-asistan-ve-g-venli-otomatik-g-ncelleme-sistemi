import logging

from PyQt6.QtWidgets import QApplication

from jarvis.brains.base_brain import BaseBrain, BrainError
from jarvis.core.brain_orchestrator import BrainOrchestrator
from jarvis.main import _report_brain_health
from jarvis.ui import SystemNetworkWidget


class _TaskBrain(BaseBrain):
    NAME = "live_task_test"

    def __init__(self):
        self.fail = False
        self.heartbeat_during_call = None
        super().__init__()

    def handle(self, message):
        self.heartbeat_during_call = self.heartbeat()
        if self.fail:
            raise BrainError("test failure")
        return {"status": "completed", "result": {}}


def test_brain_heartbeat_tracks_only_the_real_assigned_task(monkeypatch):
    monkeypatch.setattr(BaseBrain, "_make_logger", lambda self: logging.getLogger("test.brain"))
    brain = _TaskBrain()
    message = {
        "task": "plan oluştur",
        "payload": {"goal": "Kullanıcının verdiği gerçek hedef"},
    }

    result = brain.call(message)

    assert result["status"] == "completed"
    assert brain.heartbeat_during_call["status"] == "running"
    assert brain.heartbeat_during_call["current_task"] == (
        "plan oluştur — Kullanıcının verdiği gerçek hedef"
    )
    assert brain.heartbeat()["status"] == "idle"
    assert brain.heartbeat()["current_task"] == ""
    assert brain.heartbeat()["last_task"] == "plan oluştur — Kullanıcının verdiği gerçek hedef"


def test_failed_brain_keeps_the_failed_task_and_real_error(monkeypatch):
    monkeypatch.setattr(BaseBrain, "_make_logger", lambda self: logging.getLogger("test.brain"))
    brain = _TaskBrain()
    brain.fail = True

    result = brain.call({"task": "risk değerlendirmesi", "payload": {"target": "dosya yazma adımı"}})
    heartbeat = brain.heartbeat()

    assert result["status"] == "failed"
    assert heartbeat["status"] == "error"
    assert heartbeat["current_task"] == ""
    assert heartbeat["last_task"] == "risk değerlendirmesi — dosya yazma adımı"
    assert heartbeat["last_error"] == "test failure"


def test_orchestrator_snapshot_is_structured_and_includes_agent_task():
    class _Worker:
        def is_alive(self):
            return True

    class _Brain:
        def heartbeat(self):
            return {"name": "planner_ai", "status": "running", "current_task": "gerçek plan görevi"}

    orchestrator = object.__new__(BrainOrchestrator)
    orchestrator._worker_thread = _Worker()
    orchestrator._worker_interval_seconds = 7.0
    for name in ("planner", "research", "coder", "security", "memory", "executor", "auditor"):
        setattr(orchestrator, name, _Brain())

    snapshot = orchestrator.get_team_health_snapshot()

    assert snapshot["worker_running"] is True
    assert snapshot["interval_seconds"] == 7.0
    assert len(snapshot["agents"]) == 7
    assert snapshot["agents"][0]["current_task"] == "gerçek plan görevi"


def test_brain_health_report_forwards_real_task_and_idle_without_fabrication():
    observed = []

    class _UI:
        def set_network_status(self, component, status, detail):
            observed.append((component, status, detail))

    _report_brain_health(
        _UI(),
        {
            "worker_running": True,
            "interval_seconds": 20,
            "agents": [
                {
                    "name": "planner_ai",
                    "status": "running",
                    "current_task": "plan oluştur — kullanıcının gerçek hedefi",
                },
                {"name": "research_ai", "status": "idle", "current_task": ""},
            ],
        },
    )

    assert ("planner", "running", "plan oluştur — kullanıcının gerçek hedefi") in observed
    assert ("research", "idle", "Boşta") in observed


def test_architecture_graph_shows_live_task_or_truthful_empty_state():
    app = QApplication.instance() or QApplication([])
    assert app is not None
    canvas = SystemNetworkWidget()
    try:
        assert canvas.task_label_for_node("planner") == "Görev verisi yok"

        canvas.update_runtime_status("planner", "running", "plan oluştur — gerçek kullanıcı hedefi")
        assert canvas.task_label_for_node("planner") == "plan oluştur — gerçek kullanıcı hedefi"

        canvas.update_runtime_status("planner", "idle", "Boşta")
        assert canvas.task_label_for_node("planner") == "Boşta"

        canvas.update_runtime_status("planner", "error", "Hata · son görev başarısız")
        assert canvas.task_label_for_node("planner") == "Hata"
    finally:
        canvas.close()
