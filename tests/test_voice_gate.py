"""Ses kapısı (Murat@goxs 2026-09-30: 'benim sesimi tanısın, her şeye cevap veriyor')."""
from __future__ import annotations

import numpy as np

from jarvis.core import voice_gate as vg


class _Wake:
    def __init__(self):
        self.fire = False
        self.resets = 0

    def predict(self, frame):
        return {"hey_jarvis": 0.9 if self.fire else 0.0}

    def reset(self):
        self.resets += 1


class _Verifier:
    def __init__(self, vec):
        self.vec = vec

    def embed(self, samples):
        return self.vec


class _Clock:
    t = 100.0

    def __call__(self):
        return self.t


PROFILE = np.array([1.0, 0.0], dtype=np.float32)


def _gate(voice_vec):
    sent, logs, wake, clock = [], [], _Wake(), _Clock()
    g = vg.VoiceGate(sent.append, PROFILE, wake=wake, verifier=_Verifier(np.array(voice_vec, np.float32)),
                     log=logs.append, clock=clock, cfg={"pencere_sn": 5.0, "ses_esigi": 500.0})
    return g, sent, logs, wake, clock


def _speech(n=vg.FRAME):
    return (np.sin(np.arange(n) / 5.0) * 3000).astype(np.int16)


def test_nothing_is_sent_before_wake_word():
    g, sent, logs, wake, clock = _gate([1.0, 0.0])
    for _ in range(20):
        g.process(_speech())
    assert sent == [] and not g.active


def test_wake_word_from_owner_opens_gate_and_forwards_audio():
    g, sent, logs, wake, clock = _gate([0.95, 0.31])
    for _ in range(5):
        g.process(_speech())
    wake.fire = True
    g.process(_speech())
    wake.fire = False
    assert g.active and "ses tanındı" in logs[-1]
    n = len(sent)
    g.process(_speech())
    assert len(sent) == n + 1


def test_wake_word_from_someone_else_is_ignored():
    g, sent, logs, wake, clock = _gate([0.1, 0.99])
    wake.fire = True
    g.process(_speech())
    assert not g.active and sent == [] and "tanınmadı" in logs[-1]


def test_gate_closes_after_silence_and_touch_keeps_it_open():
    g, sent, logs, wake, clock = _gate([1.0, 0.0])
    wake.fire = True
    g.process(_speech())
    wake.fire = False
    assert g.active
    silence = np.zeros(vg.FRAME, np.int16)
    clock.t += 4.0
    g.touch()                       # JARVIS cevap veriyor
    clock.t += 4.0
    g.process(silence)
    assert g.active
    clock.t += 6.0
    g.process(silence)
    assert not g.active and "kapandı" in logs[-1]


def test_no_profile_means_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(vg, "PROFILE_PATH", tmp_path / "yok.npy")
    monkeypatch.delenv("JARVIS_VOICE_GATE", raising=False)
    logs = []
    assert vg.create_gate(lambda b: None, log=logs.append) is None
    assert "Ses profili yok" in logs[0]
    monkeypatch.setenv("JARVIS_VOICE_GATE", "0")
    assert vg.create_gate(lambda b: None, log=logs.append) is None


def test_frames_are_regrouped_to_80ms():
    g, sent, logs, wake, clock = _gate([1.0, 0.0])
    g.active = True
    g._last_activity = clock.t
    g.process(_speech(2048))        # mikrofon 2048'lik parçalar veriyor
    g.process(_speech(2048))
    assert [len(b) for b in sent] == [vg.FRAME * 2] * 3


def test_voiced_keeps_only_speech_and_normalized_levels():
    rng = np.random.default_rng(0)
    sessiz = (rng.standard_normal(6 * vg.SAMPLE_RATE) * 40).astype(np.int16)
    konusma = (np.sin(np.arange(2 * vg.SAMPLE_RATE) / 7.0) * 2500).astype(np.int16)
    kayit = np.concatenate([sessiz[:3 * vg.SAMPLE_RATE], konusma, sessiz[3 * vg.SAMPLE_RATE:]])
    kisim = vg.voiced(kayit)
    assert 1.8 * vg.SAMPLE_RATE <= len(kisim) <= 2.1 * vg.SAMPLE_RATE
    assert len(vg.voiced(sessiz)) < 0.3 * vg.SAMPLE_RATE      # yalnız uğultu: konuşma yok
    assert int(np.abs(vg.normalized(kisim)).max()) == 16000
