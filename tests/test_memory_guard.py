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
