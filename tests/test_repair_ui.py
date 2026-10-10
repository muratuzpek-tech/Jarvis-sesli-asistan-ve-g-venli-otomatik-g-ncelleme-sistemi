from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QShortcut, QKeySequence
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFileDialog

from jarvis.core.secure_config import save_config
from jarvis.ui import MainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _face_path() -> str:
    return str(next(Path(__file__).parents[1].glob("src/jarvis/assets/*.png")))


def _jpeg_bytes() -> bytes:
    image = QImage(32, 24, QImage.Format.Format_RGB32)
    image.fill(0x102030)
    payload = QByteArray()
    buffer = QBuffer(payload)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "JPG")
    buffer.close()
    return bytes(payload)


def _mock_lan_scan(monkeypatch, hosts=()):
    import ipaddress
    from datetime import datetime
    from jarvis.network_discovery import LocalIPv4Network, ScanSnapshot

    network = ipaddress.IPv4Network("192.168.1.0/24")
    local = LocalIPv4Network("Ethernet", ipaddress.IPv4Address("192.168.1.37"), network)
    snapshot = ScanSnapshot(
        network=network,
        local_address=local.address,
        gateway=ipaddress.IPv4Address("192.168.1.1"),
        hosts=tuple(hosts),
        scanned_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_seconds=0.1,
    )
    monkeypatch.setattr("jarvis.ui.active_local_networks", lambda: (local,))
    monkeypatch.setattr("jarvis.ui.scan_lan", lambda *_args, **_kwargs: snapshot)
    return snapshot


def _apply_lan_snapshot(window, snapshot):
    window._lan_snapshot = snapshot
    window._lan_canvas.set_snapshot(snapshot)
    window._update_lan_summary(snapshot)
    window._show_lan_node_details(window._lan_canvas.selected_node)

def test_mute_button_and_f4_are_real_qt_actions(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    window = MainWindow(_face_path())
    try:
        assert window._mute_btn.isVisible() is False  # parent is not shown yet
        window.show()
        qapp.processEvents()
        assert window._mute_btn.isVisible()
        assert window._muted is False
        assert "SESSİZ MOD KAPALI" in window._mute_btn.text()
        QTest.mouseClick(window._mute_btn, Qt.MouseButton.LeftButton)
        assert window._muted is True
        # DUZELTME (canli testte bulundu, 2026-09-22): offscreen/headless
        # test ortaminda pencere aktivasyonu gercek bir pencere yoneticisi
        # olmadigi icin QShortcut'in varsayilan WindowShortcut baglamini
        # (aktif pencere sarti) tetiklemiyor - activateWindow() bile
        # yetmiyor, bu platformun kendi sinirlamasi. Gercek kod (_toggle_mute,
        # F4 QShortcut baglantisi ui.py'de) dogru ve degismedi. OS tus
        # olayini simule etmek yerine, F4'e baglanmis GERCEK QShortcut
        # nesnesini bulup dogrudan tetikliyoruz - testin asil amaci zaten
        # "gercek bir Qt aksiyonu mu (dead code degil mi)" diye dogrulamak.
        f4_shortcuts = [sc for sc in window.findChildren(QShortcut)
                        if sc.key() == QKeySequence("F4")]
        assert f4_shortcuts, "F4 icin kayitli bir QShortcut bulunamadi"
        f4_shortcuts[0].activated.emit()
        assert window._muted is False
    finally:
        window.close()
        qapp.processEvents()


def test_environment_key_is_presence_only_and_setup_preserves_fields(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "env-presence-only")
    window = MainWindow(_face_path())
    try:
        assert window._ready is True
        assert window._check_config() is True
    finally:
        window.close()
        qapp.processEvents()

    monkeypatch.delenv("GEMINI_API_KEY")
    save_config({"gemini_api_key": "old", "os_system": "linux", "other": "keep"})
    window = MainWindow(_face_path())
    try:
        window._on_setup_done("new-key", "windows")
        saved = json.loads((tmp_path / "config" / "api_keys.json").read_text(encoding="utf-8"))
        assert saved["gemini_api_key"] == "new-key"
        assert os.environ["GEMINI_API_KEY"] == "new-key"
        assert saved["os_system"] == "windows"
        assert saved["other"] == "keep"
    finally:
        window.close()
        qapp.processEvents()


def test_camera_frame_has_visible_stack_target(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    window = MainWindow(_face_path())
    try:
        window._on_cam_stream(True)
        assert window._chat_stack.currentWidget() is window._camera_page
        window._on_cam_frame(_jpeg_bytes())
        assert not window._cam_live_lbl.pixmap().isNull()
        window._on_cam_stream(False)
        assert window._chat_stack.currentWidget() is window._chat_panel
    finally:
        window.close()
        qapp.processEvents()


def test_send_is_visible_and_callback_invoked(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    received: list[str] = []
    window = MainWindow(_face_path())
    try:
        window.on_text_command = received.append
        window._input.setText("offline hello")
        window._send()
        QTest.qWait(80)
        assert received == ["offline hello"]
        assert window._chat_messages_layout.count() >= 2
        assert any("offline hello" in label.text() for label in window.findChildren(type(window._chat_empty_lbl)) if label.text())
    finally:
        window.close()
        qapp.processEvents()


def test_navigation_and_file_picker_attach_without_approval_claim(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    selected = tmp_path / "note.txt"
    selected.write_text("offline", encoding="utf-8")
    window = MainWindow(_face_path())
    try:
        window.show()
        if window._overlay is not None:
            window._overlay.hide()
        window.activateWindow()
        # DUZELTME (canli testte bulundu, 2026-09-22): tek bir
        # qapp.processEvents() cagrisi, offscreen platformda pencerenin
        # GERCEKTEN aktif hale gelmesi icin yeterli event-loop turunu
        # garanti etmiyordu - bu yuzden setFocus() cagrisi (ui.py, "Dosyalar"
        # sekmesi tiklaninca) gecerli olmadan test devam ediyor, hasFocus()
        # hep False donuyordu. Qt'nin bu tam senaryo icin onerdigi
        # qWaitForWindowActive, pencere gercekten aktif olana kadar
        # (timeout ile) event loop'u dondurur - izole testte calistigi
        # dogrulandi. Gercek kod (_drop_zone.setFocus() cagrisi) dogruydu.
        assert QTest.qWaitForWindowActive(window, 2000), "pencere aktif olmadi"
        window._nav_buttons["Sistem"].click()
        assert window._chat_stack.currentWidget() is window.hud
        window._nav_buttons["Dosyalar"].click()
        qapp.processEvents()
        assert window._drop_zone.hasFocus()
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kwargs: (str(selected), "All Files (*.*)"))
        window._drop_zone._browse()
        assert window._current_file == str(selected)
        assert "tell jarvis" in window._file_hint.text().lower()
    finally:
        window.close()
        qapp.processEvents()


def test_close_stops_owned_timers(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    window = MainWindow(_face_path())
    window.close()
    qapp.processEvents()
    assert not window._clock_tmr.isActive()
    assert not window._metric_tmr.isActive()
    assert not window._drop_zone._anim_tmr.isActive()
    assert not window._log._tmr.isActive()
    assert not window.hud._tmr.isActive()
    assert not window._lan_refresh_tmr.isActive()


def test_system_network_only_shows_observed_runtime_and_a_thin_real_chat_strip(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "presence-only-test-key")
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path())
    try:
        assert window._network_canvas.status_for_node("main")[0] == "unknown"
        assert window._network_canvas.status_for_node("brain")[0] == "unknown"
        assert window._team_chat_lbl.text() == "Henüz mesaj yok"
        assert window._team_chat_lbl.parentWidget().maximumHeight() == 34

        window.show()
        window._navigate("AI Ekip")
        qapp.processEvents()
        assert window._chat_stack.currentWidget() is window._team_page
        assert window._right_panel.isHidden()
        assert window._task_flow.isHidden()

        window._apply_state("LISTENING")
        assert window._network_canvas.status_for_node("main")[0] == "connected"
        assert window._network_canvas.status_for_node("voice")[0] == "connected"
        assert window._network_canvas.status_for_node("brain")[0] == "unknown"

        window._on_log("You: Bu gerçek konuşma satırı")
        assert window._team_chat_lbl.text() == "Sen · Bu gerçek konuşma satırı"
        window._on_log("Jarvis: Son yanıt")
        assert window._team_chat_lbl.text() == "JARVIS · Son yanıt"

        window._navigate("Canlı Sohbet")
        qapp.processEvents()
        assert not window._right_panel.isHidden()
        assert not window._task_flow.isHidden()
    finally:
        window.close()
        qapp.processEvents()


def test_system_network_reads_agent_board_without_creating_or_faking_jobs(qapp, monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    memory = tmp_path / "memory"
    memory.mkdir()
    database = memory / "agent_board.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (id TEXT, description TEXT, status TEXT, started_at TEXT, finished_at TEXT)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?)",
            ("job-1", "Gerçek kayıtlı görev", "running", "2026-10-09T12:00:00", None),
        )

    window = MainWindow(_face_path())
    try:
        tasks = window._read_task_files()
        assert tasks == [{
            "id": "job-1", "goal": "Gerçek kayıtlı görev", "status": "running",
            "updated_at": "2026-10-09T12:00:00", "_source": "agent_board.db",
        }]
        window._network_canvas.update_task_records(tasks)
        assert window._network_canvas.status_for_node("tasks")[0] == "running"
        assert window._network_canvas.status_for_node("brain")[0] == "unknown"
    finally:
        window.close()
        qapp.processEvents()


def test_brain_team_health_is_separate_from_last_task(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        canvas = window._network_canvas
        canvas.update_runtime_status("brain", "ready", "Arka plan görev döngüsü çalışıyor · 20 sn")
        canvas.update_task_records([{
            "id": "task-1", "goal": "Gerçek takım görevi", "status": "completed",
            "_source": "brain_tasks.json",
        }])

        assert canvas.status_for_node("brain")[0] == "ready"
        assert canvas.status_for_node("tasks")[0] == "completed"
        canvas.update_task_records([{
            "id": "task-recovery",
            "goal": "Sonucu doğrulanacak görev",
            "status": "pending",
            "payload": {"_restart_recovery": {"action": "manual_review", "reason": "Sonuç doğrulanamadı"}},
        }])
        assert canvas.status_for_node("tasks")[0] == "recovery_required"
        assert "Sonuç doğrulanamadı" in canvas.task_panel_details()[2]
    finally:
        window.close()
        qapp.processEvents()


def test_lan_view_shows_live_scan_results_without_real_network_access(qapp, monkeypatch, tmp_path):
    import ipaddress
    from jarvis.network_discovery import HostObservation

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "presence-only-test-key")
    host = HostObservation(
        ipaddress.IPv4Address("192.168.1.3"),
        (80,),
        ("arp", "tcp"),
    )
    _mock_lan_scan(monkeypatch, (host,))
    window = MainWindow(_face_path())
    try:
        assert window._lan_scan_worker is None
        assert not window._lan_refresh_tmr.isActive()
        assert window._team_graph_stack.currentWidget() is window._network_canvas
        for button in (window._team_arch_btn, window._team_lan_btn, window._lan_scan_btn, window._lan_live_btn):
            stylesheet = button.styleSheet()
            assert stylesheet.count("{") == stylesheet.count("}")

        window.show()
        window._navigate("AI Ekip")
        window._set_team_mode(1)
        worker = window._lan_scan_worker
        assert worker is not None
        assert worker.wait(1000)
        qapp.processEvents()

        assert window._lan_controls.isVisible()
        assert window._lan_live_btn.isChecked()
        lan_layout = window._lan_controls.layout()
        assert sum(lan_layout.itemAt(i).widget() is window._lan_note_lbl for i in range(lan_layout.count())) == 1
        assert "1 yanıt veren cihaz" in window._lan_status_lbl.text()
        assert [node[0] for node in window._lan_canvas._nodes()] == ["192.168.1.3"]
        assert window._lan_summary_values["network"].text() == "192.168.1.0/24"
        assert window._lan_summary_values["count"].text() == "1 yanıt veren cihaz"
        assert window._lan_summary_values["duration"].text() == "0.1 sn"
        assert window._lan_summary_values["result"].text() == "Kısmi keşif"
        assert "toplam cihaz sayısı" in window._lan_note_lbl.text().lower()

        window._show_lan_node_details("192.168.1.3")
        assert window._team_node_title.text() == "Yerel ağ cihazı"
        assert window._team_node_status.text() == "ARP yanıtı"
        assert "HTTP:80" in window._team_node_desc.text()

        window._lan_live_btn.setChecked(False)
        window._set_team_mode(0)
        assert window._lan_controls.isHidden()
    finally:
        window.close()
        qapp.processEvents()

def test_lan_map_without_snapshot_has_no_nodes_or_scan_findings(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        qapp.processEvents()

        assert window._lan_snapshot is None
        assert window._lan_scan_worker is None
        assert not window._lan_refresh_tmr.isActive()
        assert window._lan_canvas._nodes() == []
        assert "henüz tarama yapılmadı" in window._lan_canvas._empty_state_text().lower()
        assert window._lan_summary_values["count"].text() == "—"
        assert window._lan_summary_values["result"].text() == "Veri yok"
        assert window._team_node_title.text() == "Keşfedilmiş cihaz yok"
        assert window._team_node_status.text() == "Tarama başlatılmadı"
        assert "192.168.1.37" in window._team_node_path.text()
        assert "tarama bulgusu değil" in window._team_node_path.text().lower()
        assert "yanıt veren cihazlar" in window._team_node_desc.text().lower()
    finally:
        window.close()
        qapp.processEvents()


def test_lan_map_empty_snapshot_keeps_route_and_local_metadata_out_of_nodes(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    snapshot = _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        _apply_lan_snapshot(window, snapshot)

        assert window._lan_canvas._nodes() == []
        assert window._lan_canvas.selected_node is None
        assert "bu taramada yanıt veren cihaz bulunmadı." == window._lan_canvas._empty_state_text().lower()
        assert window._lan_summary_values["count"].text() == "0 yanıt veren cihaz"
        assert window._lan_summary_values["result"].text() == "Kısmi keşif"
        assert window._team_node_title.text() == "Keşfedilmiş cihaz yok"
        assert window._team_node_status.text() == "Yanıt veren cihaz yok"
        assert "192.168.1.1" in window._team_node_path.text()
        assert "yanıtı doğrulanmadı" in window._team_node_path.text().lower()
        assert "192.168.1.37" in window._team_node_path.text()
        assert "tarama bulgusu değil" in window._team_node_path.text().lower()
        assert "tarama bulgusu değildir" in window._team_node_desc.text().lower()
    finally:
        window.close()
        qapp.processEvents()


def test_lan_map_empty_snapshot_without_gateway_still_excludes_local_interface(qapp, monkeypatch, tmp_path):
    from dataclasses import replace

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    snapshot = replace(_mock_lan_scan(monkeypatch), gateway=None)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        _apply_lan_snapshot(window, snapshot)

        assert window._lan_canvas._nodes() == []
        assert window._lan_canvas._empty_state_text() == "Bu taramada yanıt veren cihaz bulunmadı."
        assert window._lan_summary_values["count"].text() == "0 yanıt veren cihaz"
        assert "192.168.1.37" in window._team_node_path.text()
        assert "rota kaydı" not in window._team_node_path.text().lower()
        assert "tarama bulgusu değil" in window._team_node_path.text().lower()
        assert "tarama bulgusu değildir" in window._team_node_desc.text().lower()
    finally:
        window.close()
        qapp.processEvents()


def test_cancelled_empty_lan_snapshot_is_clear_and_has_no_nodes(qapp, monkeypatch, tmp_path):
    from dataclasses import replace

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    snapshot = replace(_mock_lan_scan(monkeypatch), cancelled=True)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        window._on_lan_scan_result(snapshot)

        assert window._lan_canvas._nodes() == []
        assert "tarama durduruldu" in window._lan_canvas._empty_state_text().lower()
        assert window._lan_summary_values["count"].text() == "0 yanıt veren cihaz"
        assert window._lan_summary_values["result"].text() == "Durduruldu · kısmi"
        assert window._team_node_status.text() == "Tarama durduruldu"
        assert "Durduruldu" in window._lan_status_lbl.text()
    finally:
        window.close()
        qapp.processEvents()


def test_lan_scan_failure_keeps_previous_snapshot_and_marks_failure(qapp, monkeypatch, tmp_path):
    import ipaddress
    from jarvis.network_discovery import HostObservation

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    host = HostObservation(
        ipaddress.IPv4Address("192.168.1.3"),
        (),
        ("arp",),
    )
    snapshot = _mock_lan_scan(monkeypatch, (host,))
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        window._on_lan_scan_result(snapshot)
        window._on_lan_scan_failed("simulated scan failure")

        assert window._lan_snapshot is snapshot
        assert [node[0] for node in window._lan_canvas._nodes()] == ["192.168.1.3"]
        assert window._lan_summary_values["count"].text() == "1 yanıt veren cihaz"
        assert "tarama başarısız" in window._lan_status_lbl.text().lower()
        assert window._team_node_title.text() == "Yerel ağ cihazı"
        assert window._team_node_status.text() == "ARP yanıtı"
    finally:
        window.close()
        qapp.processEvents()


def test_starting_new_lan_scan_keeps_previous_snapshot_visible(qapp, monkeypatch, tmp_path):
    class _Signal:
        def connect(self, _slot):
            pass

    class _PendingWorker:
        def __init__(self, *_args, **_kwargs):
            self.progress = _Signal()
            self.result_ready = _Signal()
            self.failed = _Signal()
            self.finished = _Signal()
            self.started = False

        def start(self):
            self.started = True

        def isRunning(self):
            return self.started

        def deleteLater(self):
            pass

    import ipaddress
    from jarvis.network_discovery import HostObservation

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    host = HostObservation(
        ipaddress.IPv4Address("192.168.1.3"),
        (),
        ("arp",),
    )
    snapshot = _mock_lan_scan(monkeypatch, (host,))
    monkeypatch.setattr("jarvis.ui.LanScanWorker", _PendingWorker)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._set_team_mode(1)
        window._on_lan_scan_result(snapshot)
        window._backend_enabled = True
        window._start_lan_scan()

        assert window._lan_scan_worker is not None
        assert window._lan_scan_worker.started
        assert window._lan_snapshot is snapshot
        assert [node[0] for node in window._lan_canvas._nodes()] == ["192.168.1.3"]
        assert window._lan_summary_values["count"].text() == "1 yanıt veren cihaz"
        assert "canlı cihaz" in window._lan_status_lbl.text().lower()
    finally:
        window._lan_scan_worker = None
        window.close()
        qapp.processEvents()


def test_stopping_live_monitor_cancels_current_scan(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "presence-only-test-key")
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path())
    worker = Mock()
    worker.isRunning.return_value = True
    try:
        previous = window._lan_live_btn.blockSignals(True)
        window._lan_live_btn.setChecked(True)
        window._lan_live_btn.blockSignals(previous)
        window._lan_scan_worker = worker
        window._lan_restart_after_finish = True
        window._lan_refresh_tmr.start()

        window._set_lan_monitoring(False)

        assert not window._lan_refresh_tmr.isActive()
        worker.requestInterruption.assert_called_once_with()
        assert not window._lan_restart_after_finish
        assert "durduruluyor" in window._lan_status_lbl.text().lower()
    finally:
        window._lan_scan_worker = None
        window.close()
        qapp.processEvents()


def test_architecture_map_fits_available_viewport(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window.resize(1440, 923)
        window.show()
        window._navigate("AI Ekip")
        window._set_team_mode(0)
        qapp.processEvents()

        assert window._network_canvas.width() <= window._team_graph_stack.width()
        assert window._network_canvas.height() <= window._team_graph_stack.height()
        assert window._network_canvas.minimumHeight() <= window._team_graph_stack.height()
        assert window._team_title_lbl.text() == "JARVIS MİMARİSİ"
        assert "şematik bileşen görünümü" in window._team_desc.text().lower()
        assert "gerçek ağ cihazlarını göstermez" in window._team_desc.text().lower()
        assert not window._team_legend.isHidden()
        assert not window._team_details_panel.isHidden()
        assert window._team_details_panel.width() < window.width() // 3
        for node_id in ("planner", "research", "coder", "security", "brain_memory", "executor", "auditor"):
            assert node_id in window._network_canvas._positions
    finally:
        window.close()
        qapp.processEvents()


def test_top_status_cards_follow_only_observed_backend_and_device_state(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "status-presence-only")
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path())
    try:
        assert set(window._status_cards) == {"system", "gemini", "microphone", "speaker"}
        assert window._status_cards["microphone"]["state"].text() == "Bilinmiyor"
        window._apply_state("CONNECTING")
        assert window._status_cards["gemini"]["state"].text() == "Bağlanıyor"

        window._apply_state("LISTENING")
        assert window._status_cards["gemini"]["state"].text() == "Bağlı · Dinliyor"
        assert window._status_cards["system"]["state"].text() == "Cihaz verisi eksik"
        window._on_mic_device("Microphone (Test)")
        window._on_speaker_device("Speaker (Test)")
        assert window._status_cards["microphone"]["state"].text() == "Hazır"
        assert window._status_cards["speaker"]["state"].text() == "Hazır"
        assert window._status_cards["system"]["state"].text() == "Hazır"
        assert window._status_cards["gemini"]["updated"].text().startswith("Güncellendi:")

        window._on_mic_device("Bilinmiyor")
        assert window._status_cards["microphone"]["state"].text() == "Bilinmiyor"
        assert "Bilinmiyor" in window._mic_lbl.text()
    finally:
        window.close()
        qapp.processEvents()


def test_composer_microphone_and_tool_context_use_existing_ui_actions(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "composer-presence-only")
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path())
    try:
        window.show()
        window._on_mic_device("Microphone (Test)")
        assert window._muted is False
        QTest.mouseClick(window._composer_mute_btn, Qt.MouseButton.LeftButton)
        assert window._muted is True
        assert window._composer_mute_btn.text() == "🔇"

        window._apply_network_status("tool", "running", "powershell_control")
        assert "powershell_control" in window._composer_context_lbl.text()
        assert "Çalışıyor" in window._composer_context_lbl.text()
    finally:
        window.close()
        qapp.processEvents()


def test_architecture_and_lan_panels_remain_separate(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        window._navigate("AI Ekip")
        window._set_team_mode(0)
        assert window._team_title_lbl.text() == "JARVIS MİMARİSİ"
        assert not window._team_legend.isHidden()
        assert window._lan_summary_panel.isHidden()
        window._show_network_node_details("brain")
        assert window._team_node_status.text() == "Veri yok"
        assert window._team_node_heartbeat.text() == "Henüz heartbeat alınmadı"

        window._set_team_mode(1)
        assert window._team_title_lbl.text() == "CANLI LAN"
        assert window._team_legend.isHidden()
        assert not window._lan_summary_panel.isHidden()
        assert window._lan_summary_values["count"].text() == "—"
        assert window._lan_summary_values["result"].text() == "Veri yok"
        window._lan_canvas.set_local_address("192.168.1.37")
        assert window._lan_snapshot is None
        assert window._lan_canvas._nodes() == []
    finally:
        window.close()
        qapp.processEvents()


def test_heartbeat_stale_is_derived_from_last_ui_observation(qapp, monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _mock_lan_scan(monkeypatch)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        canvas = window._network_canvas
        canvas.update_runtime_status("planner", "idle", "Hazır, boşta")
        canvas._runtime_received_at["planner"] = time.monotonic() - 11
        state, detail = canvas.status_for_node("planner")
        assert state == "stale"
        assert "UI'ye" in detail
    finally:
        window.close()
        qapp.processEvents()


def test_ui_only_mode_does_not_claim_backend_or_voice_connection(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    window = MainWindow(_face_path(), backend_enabled=False)
    try:
        assert window._overlay is None
        assert "Yerel arayüz" in window._center_state_lbl.text()
        assert "UI-only" in window._chat_meta_lbl.text()
        assert "Kapalı" in window._internet_lbl.text()
        assert not window._lan_scan_btn.isEnabled()
        assert not window._lan_live_btn.isEnabled()
        assert not window._mute_btn.isEnabled()
        assert "UI-ONLY" in window._mute_btn.text()
        assert not window._lan_refresh_tmr.isActive()
    finally:
        window.close()
        qapp.processEvents()
