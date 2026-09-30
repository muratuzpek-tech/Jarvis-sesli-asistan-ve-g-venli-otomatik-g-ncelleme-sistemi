"""Murat@goxs 2026-09-30: Ubuntu'da hatırlatıcı Windows'un 'schtasks' aracını çağırdı;
hafta içi her gün 17:00 hatırlatması üç ayrı tarih olarak denendi."""
import subprocess

from jarvis.actions import reminder as r


def test_real_os_wins_over_config(monkeypatch):
    monkeypatch.setattr(r.platform, "system", lambda: "Linux")
    assert r._get_os() == "linux"


def test_repeat_to_systemd_calendar():
    assert r.repeat_to_calendar("weekdays", "17:00") == "Mon..Fri *-*-* 17:00:00"
    assert r.repeat_to_calendar("hafta içi", "7:5") == "Mon..Fri *-*-* 07:05:00"
    assert r.repeat_to_calendar("daily", "08:30") == "*-*-* 08:30:00"
    assert r.repeat_to_calendar("pzt,çarşamba,fri", "17:00") == "Mon,Wed,Fri *-*-* 17:00:00"
    assert r.repeat_to_calendar("ara sıra", "17:00") is None


def test_repeating_reminder_writes_persistent_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(r.platform, "system", lambda: "Linux")
    monkeypatch.setattr(r.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(r.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []
    monkeypatch.setattr(r.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    out = r.reminder({"time": "17:00", "message": "Miran'ı okuldan al", "repeat": "weekdays"})
    assert "Tekrarlayan hatırlatma kuruldu" in out, out
    timers = list((tmp_path / ".config/systemd/user").glob("*.timer"))
    assert len(timers) == 1 and "OnCalendar=Mon..Fri *-*-* 17:00:00" in timers[0].read_text()
    assert any("enable" in c for c in calls)
    script = next((tmp_path / ".jarvis/reminders").glob("*.py")).read_text()
    assert "unlink" not in script                       # tekrarlayan: kendini silmez
    assert r.list_repeating() == [timers[0].stem]
