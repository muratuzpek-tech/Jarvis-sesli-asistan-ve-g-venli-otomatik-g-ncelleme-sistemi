"""Daha önce hiç testi olmayan altı sesli aracın davranışı, gerçek
main.JarvisLive._execute_tool yolundan (güvenlik kapısı dahil):
screen_process, close_camera, recall_conversation, game_updater,
flight_finder, shutdown_jarvis.

Kamera/ekran yakalama, LLM özeti, oyun güncelleyici ve süreç kapatma
sahtedir: gerçek kapatma, ağ ya da donanım erişimi yok. HOME/JARVIS_HOME
conftest tarafından tmp'ye çevrilir; konuşma günlüğü tmp_path'tedir."""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive


class _UI:
    muted = False
    current_file = None

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a, **k: self.calls.append(name)


class _Recorder:
    def __init__(self, result="tamam"):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


@pytest.fixture
def jl(monkeypatch):
    monkeypatch.setattr(main_mod, "_JARVIS2_REGISTRY_AVAILABLE", False)
    obj = JarvisLive.__new__(JarvisLive)
    obj.ui = _UI()
    obj.spoken = []
    obj.speak = lambda text, *a, **k: obj.spoken.append(text)
    obj.speak_error = lambda *a, **k: None
    obj._vision_busy = False
    obj._vision_last_time = 0.0
    obj._vision_cam_active = False
    obj._pending_vision = None
    return obj


def call(jl, name, **args):
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))
    return str(resp.response.get("result", resp.response))


# ── screen_process ────────────────────────────────────────────────────────

def test_screen_process_captures_screen_and_defers_image(jl, monkeypatch):
    monkeypatch.setattr(main_mod, "_capture_screen", lambda: (b"PNGDATA", "image/png"))
    camera = _Recorder()
    monkeypatch.setattr(main_mod, "_capture_camera", camera)

    result = call(jl, "screen_process", text="Ekranda ne var?")

    assert result.startswith("[VISION_ACTIVE] Screen captured.")
    assert jl._pending_vision == (b"PNGDATA", "image/png", "Ekranda ne var?", "screen")
    assert jl._vision_busy is True
    assert camera.calls == []


def test_screen_process_camera_angle_starts_stream(jl, monkeypatch):
    monkeypatch.setattr(main_mod, "_capture_camera", lambda: (b"JPG", "image/jpeg"))

    result = call(jl, "screen_process", angle="camera", text="Bu ne?")

    assert "Camera captured" in result
    assert jl._vision_cam_active is True
    assert "start_camera_stream" in jl.ui.calls
    assert jl._pending_vision[3] == "camera"


def test_screen_process_ignores_duplicate_call_during_cooldown(jl, monkeypatch):
    capture = _Recorder((b"X", "image/png"))
    monkeypatch.setattr(main_mod, "_capture_screen", capture)
    call(jl, "screen_process", text="1")
    jl._vision_busy = False          # ilk istek bitti ama 4 sn bekleme sürüyor

    result = call(jl, "screen_process", text="2")

    assert "still processing" in result
    assert len(capture.calls) == 1


# ── close_camera ──────────────────────────────────────────────────────────

def test_close_camera_stops_stream(jl):
    assert call(jl, "close_camera") == "Camera closed."
    assert "stop_camera_stream" in jl.ui.calls


# ── recall_conversation ───────────────────────────────────────────────────

@pytest.fixture
def conv_log(tmp_path, monkeypatch):
    from jarvis.actions import conversation_log as cl
    path = tmp_path / "conversation_log.jsonl"
    monkeypatch.setattr(cl, "LOG_PATH", path)
    monkeypatch.setattr(cl, "_get_api_key", lambda: "test-key")
    import google.genai
    monkeypatch.setattr(google.genai, "Client", lambda **k: SimpleNamespace(models=None))
    prompts = []

    def fake_generate(primary, prompt_for_ollama, source):
        prompts.append((prompt_for_ollama, source))
        return "  Kedinin adını konuştunuz.  "

    import jarvis.actions.local_llm as local_llm
    monkeypatch.setattr(local_llm, "generate_with_fallback", fake_generate)
    return SimpleNamespace(path=path, prompts=prompts)


def _write_turns(path, *turns):
    with path.open("w", encoding="utf-8") as f:
        for ts, role, text in turns:
            f.write(json.dumps({"timestamp": ts.isoformat(), "role": role, "text": text}) + "\n")


def test_recall_conversation_summarises_recent_turns(jl, conv_log):
    now = datetime.now()
    _write_turns(conv_log.path,
                 (now - timedelta(minutes=3), "user", "Kedimin adı Tekir"),
                 (now - timedelta(minutes=2), "jarvis", "Not ettim efendim"),
                 (now - timedelta(days=3), "user", "eski konu"))

    result = call(jl, "recall_conversation", minutes=10, topic="kedi")

    assert result == "Kedinin adını konuştunuz."
    (prompt, source), = conv_log.prompts
    assert source == "recall_conversation"
    assert "user: Kedimin adı Tekir" in prompt and "jarvis: Not ettim efendim" in prompt
    assert "eski konu" not in prompt
    assert "'kedi'" in prompt


def test_recall_conversation_empty_range_does_not_call_llm(jl, conv_log):
    _write_turns(conv_log.path, (datetime.now() - timedelta(days=10), "user", "çok eski"))

    result = call(jl, "recall_conversation", minutes=5)

    assert "kayıtlı bir konuşma bulamadım" in result
    assert conv_log.prompts == []


def test_recall_conversation_unknown_period(jl, conv_log):
    assert "anlayamadım" in call(jl, "recall_conversation", period="geçen yüzyıl")
    assert conv_log.prompts == []


# ── game_updater ──────────────────────────────────────────────────────────

def test_game_updater_runs_without_shutdown(jl, monkeypatch):
    updater = _Recorder("Steam güncellemesi başladı")
    monkeypatch.setattr(main_mod, "game_updater", updater)

    result = call(jl, "game_updater", action="update", platform="steam")

    assert result == "Steam güncellemesi başladı"
    (_, kwargs), = updater.calls
    assert kwargs["parameters"] == {"action": "update", "platform": "steam"}


def test_game_updater_shutdown_needs_real_user_approval(jl, monkeypatch):
    updater = _Recorder("indirme sonrası kapatılacak")
    monkeypatch.setattr(main_mod, "game_updater", updater)
    args = {"action": "update", "platform": "steam", "shutdown_when_done": True}

    first = call(jl, "game_updater", **args)
    assert first.startswith("CONFIRMATION_REQUIRED:game_shutdown")
    assert updater.calls == []

    jl._approvals.answer(True)       # kullanıcının gerçek "evet"i
    second = call(jl, "game_updater", **args)

    assert second == "indirme sonrası kapatılacak"
    (_, kwargs), = updater.calls
    assert kwargs["parameters"]["_user_confirmation_granted"] is True

    # Onay tek kullanımlık: üçüncü çağrı yeniden onay ister.
    assert call(jl, "game_updater", **args).startswith("CONFIRMATION_REQUIRED:game_shutdown")
    assert len(updater.calls) == 1


# ── flight_finder ─────────────────────────────────────────────────────────

def test_flight_finder_validates_before_any_search(jl, monkeypatch):
    import jarvis.actions.flight_finder as ff
    monkeypatch.setattr(main_mod, "flight_finder", ff.flight_finder)

    assert call(jl, "flight_finder", origin="IST", destination="",
                date="2026-11-01") == "Please provide both origin and destination, sir."
    assert call(jl, "flight_finder", origin="IST", destination="AMS",
                date="") == "Please provide a departure date, sir."


def test_flight_finder_passes_declared_params(jl, monkeypatch):
    finder = _Recorder("3 uçuş bulundu")
    monkeypatch.setattr(main_mod, "flight_finder", finder)
    args = {"origin": "IST", "destination": "AMS", "date": "2026-11-01",
            "passengers": 2, "cabin": "business"}

    assert call(jl, "flight_finder", **args) == "3 uçuş bulundu"
    (_, kwargs), = finder.calls
    assert kwargs["parameters"] == args


# ── shutdown_jarvis ───────────────────────────────────────────────────────

class _FakeThread:
    started = []

    def __init__(self, target=None, daemon=None):
        self.target = target

    def start(self):
        _FakeThread.started.append(self.target)


def test_shutdown_jarvis_requires_approval_then_schedules_exit(jl, monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(main_mod, "threading", SimpleNamespace(Thread=_FakeThread))

    first = call(jl, "shutdown_jarvis")
    assert first.startswith("CONFIRMATION_REQUIRED:shutdown_jarvis")
    assert _FakeThread.started == [] and jl.spoken == []

    jl._approvals.answer(True)
    call(jl, "shutdown_jarvis")

    assert len(_FakeThread.started) == 1      # kapatma iş parçacığı kuruldu, çalıştırılmadı
    assert jl.spoken == ["Goodbye, sir."]


def test_shutdown_jarvis_rejected_does_not_exit(jl, monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(main_mod, "threading", SimpleNamespace(Thread=_FakeThread))

    call(jl, "shutdown_jarvis")
    jl._approvals.answer(False)
    second = call(jl, "shutdown_jarvis")

    assert _FakeThread.started == []
    assert "Goodbye" not in " ".join(jl.spoken)
    assert second != ""
