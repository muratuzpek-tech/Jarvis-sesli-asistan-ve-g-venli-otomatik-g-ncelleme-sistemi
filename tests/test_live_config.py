"""tests/test_live_config.py

Konusma tanima kalitesi: canli oturum yapilandirmasi (JarvisLive._build_config).

Eskiden:
  * input_audio_transcription={} - dil ipucu yok; transkript zaman zaman
    Rusca/Telugu parcalar uretiyordu.
  * realtime_input_config yok - Gemini Live varsayilani END_SENSITIVITY_HIGH
    (SDK: "konusmayi daha sik bitirir"): cumle ortasinda kesilme ("Pro je nin").

Artik (kurulu google-genai'nin gercek alanlari):
  * AudioTranscriptionConfig(language_codes=["tr-TR"])  (BCP-47)
  * SpeechConfig.language_code="tr"  (SDK: ISO 639-1, sesli YANIT sentezi icin)
  * RealtimeInputConfig(automatic_activity_detection=AutomaticActivityDetection(
        start HIGH, end LOW, prefix_padding_ms, silence_duration_ms))
    degerler tek yerde sabit, ortam degiskeniyle ayarlanabilir.
  * Sistem talimatinda kisa bir komut sozlugu; FINAL LANGUAGE RULE son parca.

Canli sunucu bu ayarlardan birini reddedip baglantiyi 1007 ile kapatabildigi
icin (Jarvis "API key invalid" gosteriyordu) dil/VAD alanlari AYRI ortam
degiskenleriyle acilir, hepsi varsayilan KAPALI: JARVIS_STT_LANG,
JARVIS_TTS_LANG, JARVIS_VAD. Komut sozlugu hep acik.

AG CAGRISI YOK: config dogrudan kurulur; ayrica SDK'nin live.connect'te
kullandigi Gemini API donusturucusuyle kurulum (setup) mesaji uretilir ve
alanlarin gercekten gonderildigi dogrulanir. HOME/JARVIS_HOME tmp (conftest).
"""
import json

import pytest
from google import genai
from google.genai import _common, types
from google.genai import _live_converters as live_converters

import jarvis.main as main_mod
from jarvis.main import JarvisLive


class _UI:
    muted = False

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def build(monkeypatch):
    for key in ("JARVIS_STT_LANG", "JARVIS_TTS_LANG", "JARVIS_VAD",
                "JARVIS_VAD_START_SENSITIVITY", "JARVIS_VAD_END_SENSITIVITY",
                "JARVIS_VAD_PREFIX_PADDING_MS", "JARVIS_VAD_SILENCE_MS"):
        monkeypatch.delenv(key, raising=False)

    def _build():
        obj = JarvisLive.__new__(JarvisLive)
        obj.ui = _UI()
        obj._first_connect = True
        return obj._build_config()
    return _build


@pytest.fixture
def build_all_on(build, monkeypatch):
    for key in ("JARVIS_STT_LANG", "JARVIS_TTS_LANG", "JARVIS_VAD"):
        monkeypatch.setenv(key, "1")
    return build


def test_new_live_fields_are_off_by_default(build):
    cfg = build()
    assert not isinstance(cfg.input_audio_transcription, types.AudioTranscriptionConfig) \
        or not cfg.input_audio_transcription.language_codes
    assert cfg.realtime_input_config is None
    assert cfg.speech_config.language_code is None
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Charon"


def test_default_setup_message_matches_known_good_connection(build):
    """Varsayilan setup mesajinda yeni alanlarin hicbiri gitmez (stash ile
    sorunsuz baglanan hal)."""
    setup = _setup_message(build())
    assert "language_codes" not in setup.get("inputAudioTranscription", {})
    assert "realtimeInputConfig" not in setup
    assert "language_code" not in setup["generationConfig"]["speechConfig"]


@pytest.mark.parametrize("flag,present,absent", [
    ("JARVIS_STT_LANG", "stt", ("tts", "vad")),
    ("JARVIS_TTS_LANG", "tts", ("stt", "vad")),
    ("JARVIS_VAD", "vad", ("stt", "tts")),
])
def test_each_flag_enables_only_its_own_field(build, monkeypatch, flag, present, absent):
    monkeypatch.setenv(flag, "1")
    cfg = build()
    seen = {
        "stt": isinstance(cfg.input_audio_transcription, types.AudioTranscriptionConfig)
        and cfg.input_audio_transcription.language_codes == ["tr-TR"],
        "tts": cfg.speech_config.language_code == "tr",
        "vad": cfg.realtime_input_config is not None,
    }
    assert seen[present]
    assert not any(seen[k] for k in absent)


@pytest.mark.parametrize("value", ["0", "", "false", "hayir"])
def test_flags_stay_off_for_non_true_values(build, monkeypatch, value):
    for key in ("JARVIS_STT_LANG", "JARVIS_TTS_LANG", "JARVIS_VAD"):
        monkeypatch.setenv(key, value)
    cfg = build()
    assert cfg.realtime_input_config is None and cfg.speech_config.language_code is None


def _setup_message(config) -> dict:
    """live.connect'in Gemini API (API anahtari) icin gonderdigi setup
    mesajinin aynisi - ag cagrisi yapilmaz."""
    client = genai.Client(api_key="test-offline", http_options={"api_version": "v1beta"})
    request = _common.convert_to_dict(live_converters._LiveConnectParameters_to_mldev(
        api_client=client._api_client,
        from_object=types.LiveConnectParameters(
            model=main_mod.LIVE_MODEL, config=config).model_dump(exclude_none=True),
    ))
    return json.loads(json.dumps(_common.encode_unserializable_types(request)))["setup"]


def test_transcription_has_turkish_language_code(build_all_on):
    cfg = build_all_on()
    assert isinstance(cfg.input_audio_transcription, types.AudioTranscriptionConfig)
    assert cfg.input_audio_transcription.language_codes == ["tr-TR"]


def test_activity_detection_is_configured_to_not_cut_sentences(build_all_on):
    aad = build_all_on().realtime_input_config.automatic_activity_detection
    assert aad is not None and not aad.disabled
    assert aad.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW
    assert aad.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_HIGH
    assert aad.silence_duration_ms == main_mod.VAD_DEFAULTS["silence_duration_ms"]
    assert aad.prefix_padding_ms == main_mod.VAD_DEFAULTS["prefix_padding_ms"]


def test_speech_config_has_language_code_and_keeps_voice(build_all_on):
    sc = build_all_on().speech_config
    assert sc.language_code == "tr"
    assert sc.voice_config.prebuilt_voice_config.voice_name == "Charon"


def test_fields_are_really_sent_in_gemini_api_setup_message(build_all_on):
    """Kurulu SDK (2.26) bu alt nesneleri setup mesajina snake_case anahtarla
    koyar (ust anahtarlar camelCase). Bugun calisan voice_config da ayni
    bicimde gider; proto3 JSON ayristiricisi iki bicimi de kabul eder."""
    setup = _setup_message(build_all_on())
    assert setup["inputAudioTranscription"]["language_codes"] == ["tr-TR"]
    aad = setup["realtimeInputConfig"]["automatic_activity_detection"]
    assert aad["end_of_speech_sensitivity"] == "END_SENSITIVITY_LOW"
    assert aad["start_of_speech_sensitivity"] == "START_SENSITIVITY_HIGH"
    assert aad["silence_duration_ms"] == main_mod.VAD_DEFAULTS["silence_duration_ms"]
    assert aad["prefix_padding_ms"] == main_mod.VAD_DEFAULTS["prefix_padding_ms"]
    speech = setup["generationConfig"]["speechConfig"]
    assert speech["language_code"] == "tr"
    assert speech["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Charon"


def test_vad_values_are_overridable_by_env(build, monkeypatch):
    monkeypatch.setenv("JARVIS_VAD", "1")
    monkeypatch.setenv("JARVIS_VAD_SILENCE_MS", "1200")
    monkeypatch.setenv("JARVIS_VAD_PREFIX_PADDING_MS", "150")
    monkeypatch.setenv("JARVIS_VAD_END_SENSITIVITY", "high")
    monkeypatch.setenv("JARVIS_VAD_START_SENSITIVITY", "LOW")
    aad = build().realtime_input_config.automatic_activity_detection
    assert aad.silence_duration_ms == 1200 and aad.prefix_padding_ms == 150
    assert aad.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_HIGH
    assert aad.start_of_speech_sensitivity == types.StartSensitivity.START_SENSITIVITY_LOW


@pytest.mark.parametrize("key,value", [
    ("JARVIS_VAD_SILENCE_MS", "abc"), ("JARVIS_VAD_SILENCE_MS", "-5"),
    ("JARVIS_VAD_SILENCE_MS", "999999"), ("JARVIS_VAD_END_SENSITIVITY", "orta"),
])
def test_invalid_env_values_fall_back_to_defaults(build, monkeypatch, key, value):
    monkeypatch.setenv("JARVIS_VAD", "1")
    monkeypatch.setenv(key, value)
    aad = build().realtime_input_config.automatic_activity_detection
    assert aad.silence_duration_ms == main_mod.VAD_DEFAULTS["silence_duration_ms"]
    assert aad.end_of_speech_sensitivity == types.EndSensitivity.END_SENSITIVITY_LOW


def test_system_instruction_has_command_vocabulary_and_final_language_rule_last(build):
    text = build().system_instruction
    for word in ("Jarvis", "yedek", "masaüstü", "terminal", "dosya", "onay", "evet", "hayır", "notlar"):
        assert word in text, word
    assert text.rstrip().split("\n")[-1].startswith("FINAL LANGUAGE RULE")
