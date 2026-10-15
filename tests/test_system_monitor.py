from types import SimpleNamespace

from jarvis.actions import system_monitor as sm


def _memory(percent):
    total = 16 * 1024**3
    used = int(total * percent / 100)
    return SimpleNamespace(
        percent=percent,
        total=total,
        used=used,
        available=total - used,
    )


def test_ram_does_not_alert_on_one_high_sample(monkeypatch):
    monkeypatch.setattr(sm.psutil, "cpu_percent", lambda interval=None: 10.0)
    monkeypatch.setattr(sm.psutil, "virtual_memory", lambda: _memory(93.0))
    monkeypatch.setattr(sm, "_get_cpu_temp", lambda: -1.0)
    monkeypatch.setattr(sm, "_get_gpu_usage", lambda: -1.0)

    monitor = sm.SystemMonitor()

    assert monitor.check() is None
    assert monitor._ram_streak == 1


def test_ram_alert_requires_three_consecutive_high_samples(monkeypatch):
    monkeypatch.setattr(sm.psutil, "cpu_percent", lambda interval=None: 10.0)
    monkeypatch.setattr(sm.psutil, "virtual_memory", lambda: _memory(93.0))
    monkeypatch.setattr(sm, "_get_cpu_temp", lambda: -1.0)
    monkeypatch.setattr(sm, "_get_gpu_usage", lambda: -1.0)

    monitor = sm.SystemMonitor()

    assert monitor.check() is None
    assert monitor.check() is None
    alert = monitor.check()

    assert alert is not None
    assert "93%" in alert
    assert "16.0 GB" in alert
    assert "available" in alert
    assert monitor._ram_streak == 0


def test_ram_streak_resets_after_normal_sample(monkeypatch):
    monkeypatch.setattr(sm.psutil, "cpu_percent", lambda interval=None: 10.0)
    samples = iter([_memory(93.0), _memory(93.0), _memory(40.0), _memory(93.0)])
    monkeypatch.setattr(sm.psutil, "virtual_memory", lambda: next(samples))
    monkeypatch.setattr(sm, "_get_cpu_temp", lambda: -1.0)
    monkeypatch.setattr(sm, "_get_gpu_usage", lambda: -1.0)

    monitor = sm.SystemMonitor()

    assert monitor.check() is None
    assert monitor.check() is None
    assert monitor.check() is None
    assert monitor._ram_streak == 0
    assert monitor.check() is None
    assert monitor._ram_streak == 1
