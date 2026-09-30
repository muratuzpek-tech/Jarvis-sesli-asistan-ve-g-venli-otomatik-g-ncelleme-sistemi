"""Murat@goxs 2026-09-30: 'Benim oğlum var' → hafızaya 'son_name = Emir' (uydurma);
'İyi günler' → JARVIS kendini kapattı; yazıya dökme 'Ya rı n hat ır lat' diye bölünüyordu."""
from jarvis import main as jm


def test_invented_name_is_not_saved():
    assert jm._unheard_names("Emir", "Benim oğlum var.") == ["Emir"]
    assert jm._unheard_names("Emir", "Saat 10 emirdeyiz") == ["Emir"]      # başka kelimenin parçası
    assert jm._unheard_names("Miran", "Oğlum Miran.") == []
    assert jm._unheard_names("Son named Miran", "Miran'ın okulu saat 17'de") == []
    assert jm._unheard_names("Mira", "Mira'yı okuldan al") == []
    assert jm._unheard_names("likes tea", "çay severim") == []              # isim yok: denetlenmez


def test_goodbye_is_not_shutdown():
    assert not jm._asked_to_close("İyi günler Jarvis")
    assert not jm._asked_to_close("Tamam teşekkürler sağ ol")
    assert jm._asked_to_close("Jarvis kendini kapat")
    assert jm._asked_to_close("kapanabilirsin artık")


def test_transcript_fragments_join_into_words():
    parts = [jm._with_lead(r, jm._clean_transcript(r)) for r in (" Ya", "rı", "n hat", "ır", "lat", "man", " gere", "ken")]
    assert jm._join_transcript(parts) == "Yarın hatırlatman gereken"


def test_known_names_and_vad_in_config(monkeypatch):
    mem = {"relationships": {"son_name": {"value": "Miran"}, "wife": {"value": "Wife named Ayşe"}},
           "identity": {"name": {"value": "Murat"}}}
    assert jm._known_names(mem) == ["Murat", "Miran", "Ayşe"]
    import types as _t
    monkeypatch.setattr(jm, "load_memory", lambda: mem)
    cfg = jm.JarvisLive._build_config(_t.SimpleNamespace(_resume_handle=None, _resume_time=0.0))
    assert "Miran" in cfg.system_instruction and "caiz" in cfg.system_instruction
    assert cfg.realtime_input_config.automatic_activity_detection.silence_duration_ms == 800


def test_live_model_can_be_chosen_by_env(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVE_MODEL", "gemini-3.8-live")
    src = open(jm.__file__, encoding="utf-8").read()
    assert "model=self._live_model" in src and "eski modele dönülüyor" in src
    assert jm.LIVE_MODEL.endswith("native-audio-preview-12-2025")


def test_known_name_misheard_is_accepted():
    assert jm._sounds_like("Miran", "Saat 17'de Mira'nın okuldan al")
    assert not jm._sounds_like("Miran", "Emir geldi")


def test_self_knowledge_in_config(monkeypatch):
    import types as _t
    monkeypatch.setattr(jm, "load_memory", lambda: {})
    cfg = jm.JarvisLive._build_config(_t.SimpleNamespace(_resume_handle=None, _resume_time=0.0,
                                                         _live_model="gemini-3.8-live"))
    si = cfg.system_instruction
    assert "gemini-3.8-live" in si and "Recurring reminders (e.g. weekdays 17:00) ARE supported" in si
    assert "CANNOT change your own" in si
