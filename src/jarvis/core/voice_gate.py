"""Ses kapısı: JARVIS yalnız "Hey Jarvis" diyen MURAT'a cevap verir.

Murat@goxs 2026-09-30: "benim sesimi tanısın, her şeye cevap veriyor" (TV, başka
konuşmalar). Mikrofon sesi Gemini'ye ancak şu iki koşul sağlanınca gider:

1. "Hey Jarvis" uyandırma kelimesi duyulur (openWakeWord, bilgisayarda çalışır).
2. O anki ses, kayıtlı ses profiline benzer (WeSpeaker ses izi, sherpa-onnx ile
   bilgisayarda çalışır; ses hiçbir yere gönderilmez).

İkisi de tutarsa kapı açılır; konuşma bitip PENCERE saniye sessiz kalınca kapanır.
Profil yoksa ya da kütüphaneler kurulu değilse kapı hiç devreye girmez ve JARVIS
eskisi gibi her sesi dinler (kurulum: scripts/ses_kaydi.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from collections.abc import Callable

import numpy as np

SAMPLE_RATE = 16000
FRAME = 1280                     # openWakeWord 80 ms'lik parçalarla çalışır
WAKE_WORD = "hey_jarvis"

VOICE_DIR = Path.home() / ".local" / "share" / "MuratJARVIS" / "voice"
PROFILE_PATH = VOICE_DIR / "ses_profili.npy"
SETTINGS_PATH = VOICE_DIR / "ses_ayarlari.json"
SPEAKER_MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                     "speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx")
SPEAKER_MODEL_SHA256 = "5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94"
SPEAKER_MODEL_PATH = VOICE_DIR / "wespeaker_en_voxceleb_resnet34.onnx"

DEFAULTS = {
    "benzerlik_esigi": 0.45,     # ses izi benzerliği (kosinüs); kayıtta kişiye göre ayarlanır
    "uyanma_esigi": 0.5,         # "Hey Jarvis" algılama puanı
    "pencere_sn": 12.0,          # son konuşmadan sonra kapının açık kalma süresi
    "ses_esigi": 500.0,          # bu RMS'in üstü "konuşma var" sayılır
}


def settings() -> dict:
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return data


def save_settings(values: dict) -> None:
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps({**settings(), **values}, indent=2), encoding="utf-8")
    os.chmod(SETTINGS_PATH, 0o600)


def ensure_speaker_model(log=print) -> Path:
    """Ses izi modelini (26 MB) bir kez indirir ve SHA-256 ile doğrular."""
    if SPEAKER_MODEL_PATH.is_file() and _sha256(SPEAKER_MODEL_PATH) == SPEAKER_MODEL_SHA256:
        return SPEAKER_MODEL_PATH
    import urllib.request
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SPEAKER_MODEL_PATH.with_suffix(".indiriliyor")
    log("Ses tanıma modeli indiriliyor (26 MB, bir kez)…")
    urllib.request.urlretrieve(SPEAKER_MODEL_URL, tmp)  # nosec B310: sabit https adresi + SHA-256 kontrolü
    if _sha256(tmp) != SPEAKER_MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("indirilen ses modeli doğrulanamadı (SHA-256 uyuşmadı); kullanılmadı")
    tmp.replace(SPEAKER_MODEL_PATH)
    return SPEAKER_MODEL_PATH


def ensure_wake_model() -> None:
    import openwakeword.utils as owu
    owu.download_models(model_names=[WAKE_WORD])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class SpeakerVerifier:
    """Sesten 256 boyutlu ses izi çıkarır (bilgisayarda, ~50 ms)."""

    def __init__(self, model_path: Path | None = None):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model_path or SPEAKER_MODEL_PATH),
                                                          num_threads=1)
        self._ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, samples: np.ndarray) -> np.ndarray:
        wav = samples.astype(np.float32)
        if np.issubdtype(samples.dtype, np.integer):
            wav = wav / 32768.0                                  # int16 → [-1, 1]
        stream = self._ex.create_stream()
        stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=wav)
        stream.input_finished()
        vec = np.asarray(self._ex.compute(stream), dtype=np.float32)
        return vec / (np.linalg.norm(vec) or 1.0)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / ((np.linalg.norm(a) * np.linalg.norm(b)) or 1.0))


class VoiceGate:
    """Mikrofon parçalarını alır; yalnız kapı AÇIKKEN `send(bytes)` ile Gemini'ye iletir.
    Ağır işler (uyandırma kelimesi, ses izi) ayrı bir iş parçacığında yapılır; ses
    kartının geri çağrısı hiç bekletilmez."""

    def __init__(self, send: Callable[[bytes], None], profile: np.ndarray, wake=None, verifier=None,
                 log=print, clock=time.monotonic, cfg: dict | None = None):
        self._send, self._profile, self._log, self._clock = send, profile, log, clock
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self._wake = wake
        self._verifier = verifier
        self._q: queue.Queue = queue.Queue(maxsize=400)
        self._pending = np.zeros(0, dtype=np.int16)
        self._ring: deque = deque(maxlen=int(2.5 * SAMPLE_RATE / FRAME))
        self.active = False
        self._last_activity = 0.0
        self._last_try = -10.0
        self._thread: threading.Thread | None = None

    # ── dışarıdan çağrılanlar ──
    def start(self) -> VoiceGate:
        self._thread = threading.Thread(target=self._run, name="ses-kapisi", daemon=True)
        self._thread.start()
        return self

    def feed(self, samples: np.ndarray) -> None:
        try:
            self._q.put_nowait(np.asarray(samples, dtype=np.int16).reshape(-1).copy())
        except queue.Full:
            pass

    def touch(self) -> None:
        """JARVIS konuşurken/cevap gelirken pencere kapanmasın."""
        if self.active:
            self._last_activity = self._clock()

    # ── iç işleyiş ──
    def _run(self) -> None:
        while True:
            self.process(self._q.get())

    def process(self, chunk: np.ndarray) -> None:
        self._pending = np.concatenate([self._pending, chunk])
        while len(self._pending) >= FRAME:
            frame, self._pending = self._pending[:FRAME], self._pending[FRAME:]
            self._frame(frame)

    def _frame(self, frame: np.ndarray) -> None:
        now = self._clock()
        if self.active:
            self._send(frame.tobytes())
            rms = float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))
            if rms >= self.cfg["ses_esigi"]:
                self._last_activity = now
            if now - self._last_activity > self.cfg["pencere_sn"]:
                self.active = False
                self._reset_wake()
                self._log("🔒 Ses kapısı kapandı (sessizlik). Tekrar 'Hey Jarvis' deyin.")
            return
        self._ring.append(frame)
        score = float(self._wake.predict(frame).get(WAKE_WORD, 0.0))
        if score < self.cfg["uyanma_esigi"] or now - self._last_try < 1.5:
            return
        self._last_try = now
        audio = np.concatenate(list(self._ring)[-int(2.0 * SAMPLE_RATE / FRAME):])
        sim = similarity(self._verifier.embed(audio), self._profile)
        if sim >= self.cfg["benzerlik_esigi"]:
            self.active = True
            self._last_activity = now
            self._reset_wake()
            self._log(f"🔓 'Hey Jarvis' — ses tanındı (benzerlik {sim:.2f}); dinliyorum.")
            for tail in list(self._ring)[-3:]:          # son 0.24 sn: komutun başı kaybolmasın
                self._send(tail.tobytes())
        else:
            self._log(f"🚫 'Hey Jarvis' duyuldu ama ses tanınmadı (benzerlik {sim:.2f} < "
                      f"{self.cfg['benzerlik_esigi']:.2f}); yok sayıldı.")

    def _reset_wake(self) -> None:
        reset = getattr(self._wake, "reset", None)
        if callable(reset):
            reset()


def create_gate(send: Callable[[bytes], None], log=print) -> VoiceGate | None:
    """Profil ve kütüphaneler hazırsa kapıyı kurar, yoksa None (eski davranış).
    JARVIS_VOICE_GATE=0 ile kapatılır."""
    if os.environ.get("JARVIS_VOICE_GATE", "").strip() == "0":
        log("[SES] Ses kapısı kapalı (JARVIS_VOICE_GATE=0): her ses dinleniyor.")
        return None
    if not PROFILE_PATH.is_file():
        log("[SES] Ses profili yok: her ses dinleniyor. Kurmak için: .venv/bin/python scripts/ses_kaydi.py")
        return None
    try:
        from openwakeword.model import Model
        profile = np.load(PROFILE_PATH)
        verifier = SpeakerVerifier(ensure_speaker_model(log))
        ensure_wake_model()
        wake = Model(wakeword_models=[WAKE_WORD], inference_framework="onnx")
    except Exception as exc:  # noqa: BLE001
        log(f"[SES] Ses kapısı kurulamadı ({type(exc).__name__}: {exc}); her ses dinleniyor.")
        return None
    cfg = settings()
    log(f"[SES] 🔒 Ses kapısı açık: yalnız 'Hey Jarvis' diyen kayıtlı sese cevap verilir "
        f"(eşik {cfg['benzerlik_esigi']:.2f}).")
    return VoiceGate(send, profile, wake=wake, verifier=verifier, log=log, cfg=cfg).start()
