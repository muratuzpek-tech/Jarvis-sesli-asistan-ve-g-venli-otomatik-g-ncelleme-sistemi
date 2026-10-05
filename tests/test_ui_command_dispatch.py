"""tests/test_ui_command_dispatch.py

1) MainWindow her metin komutu / dosya bildirimi icin ayri bir thread
   aciyordu; hizli arka arkaya gelen komutlar main.py'deki onay durumunu
   ayni anda isliyordu. Komutlar artik tek bir isci thread'de, geldikleri
   sirayla ve birbirleriyle cakismadan islenir.
2) _read_task_files hata bayragini her cagrida koşulsuz sifirliyordu; bozuk
   bir gorev dosyasi her 1.5 sn'de bir log basiyordu.

Qt offscreen calisir; HOME/XDG/JARVIS_HOME tmp_path'e yonlendirilir.
"""
import json
import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def ui_mod(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jarvis_home"))
    import jarvis.ui as ui
    monkeypatch.setattr(ui, "memory_dir", lambda: tmp_path / "mem")
    monkeypatch.setattr(ui, "tasks_dir", lambda: tmp_path / "tasks")
    (tmp_path / "mem").mkdir()
    (tmp_path / "tasks").mkdir()
    return ui


@pytest.fixture
def win(qapp, ui_mod):
    w = ui_mod.MainWindow(ui_mod.__file__)
    yield w
    w.close()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class _Recorder:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.order = []
        self.threads = set()
        self._lock = threading.Lock()

    def __call__(self, text):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.threads.add(threading.current_thread().name)
        time.sleep(self.delay)
        with self._lock:
            self.order.append(text)
            self.active -= 1


def test_text_commands_run_one_at_a_time_in_order(win):
    rec = _Recorder()
    win.on_text_command = rec
    for i in range(5):
        win._input.setText(f"komut {i}")
        win._send()

    assert _wait(lambda: len(rec.order) == 5)
    assert rec.max_active == 1
    assert rec.order == [f"komut {i}" for i in range(5)]


def test_commands_do_not_run_on_gui_thread(win):
    rec = _Recorder(delay=0)
    win.on_text_command = rec
    win._input.setText("x")
    win._send()
    assert _wait(lambda: rec.order == ["x"])
    assert threading.main_thread().name not in rec.threads


def test_worker_survives_a_failing_command(win):
    seen = []

    def cb(text):
        if text == "patla":
            raise RuntimeError("boom")
        seen.append(text)

    win.on_text_command = cb
    for t in ("patla", "sonraki"):
        win._input.setText(t)
        win._send()
    assert _wait(lambda: seen == ["sonraki"])


def test_file_notification_is_serialized_with_text_commands(win, tmp_path):
    rec = _Recorder()
    win.on_text_command = rec
    f = tmp_path / "rapor.txt"
    f.write_text("x")
    win._input.setText("once")
    win._send()
    win._on_file_selected(str(f))
    win._input.setText("sonra")
    win._send()

    assert _wait(lambda: len(rec.order) == 3)
    assert rec.max_active == 1
    assert rec.order[0] == "once" and rec.order[2] == "sonra"
    assert rec.order[1].startswith("[FILE_UPLOADED]")


# ── _read_task_files hata tekrarini bastirmali ──

class _FakeWin:
    def __init__(self):
        self.logs = []
        self._log_sig = type("S", (), {"emit": lambda _s, m: self.logs.append(m)})()


def test_task_read_error_is_logged_once_while_it_persists(ui_mod, tmp_path):
    (tmp_path / "mem" / "agent_tasks.json").write_text("{bozuk")
    fake = _FakeWin()
    for _ in range(5):
        ui_mod.MainWindow._read_task_files(fake)
    assert len(fake.logs) == 1


def test_task_read_error_is_logged_again_after_recovery(ui_mod, tmp_path):
    path = tmp_path / "mem" / "agent_tasks.json"
    fake = _FakeWin()
    path.write_text("{bozuk")
    ui_mod.MainWindow._read_task_files(fake)
    path.write_text(json.dumps([{"id": "1", "status": "done"}]))
    tasks = ui_mod.MainWindow._read_task_files(fake)
    assert [t["id"] for t in tasks] == ["1"]
    path.write_text("{yine bozuk")
    ui_mod.MainWindow._read_task_files(fake)
    assert len(fake.logs) == 2
