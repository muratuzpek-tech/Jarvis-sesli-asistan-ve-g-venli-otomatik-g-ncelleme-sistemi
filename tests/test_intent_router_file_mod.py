"""match_file_modification() hızlı yolunun yanlış eşleşme regresyon testleri.

Canlı hata (2026-09-28): uzun bir Go kod yazdırma istemi bu kurala takıldı;
"Go 1.26.0" içindeki "1.26" dosya adı, "Yazdığın"/"yazmanı" kelimeleri "yaz"
komutu, ilk tırnaklı ifade ("JARVIS-ENGINE") içerik sayıldı ve repo kökünde
"1.26" adlı bir dosya oluşturuldu.
"""
from __future__ import annotations

import pytest

from jarvis.actions.intent_router import match_file_modification

GO_PROMPT = """ROL VE BAĞLAM:
Sen "JARVIS-ENGINE" adında, Ubuntu Linux altyapısında çalışan aşırı yetenekli bir Go (Golang) Kıdemli Sistem Programcısı ve Otomasyon Ajanısın.

SİSTEM BİLGİSİ:
- Go Sürümü: Go 1.26.0

KATI KODLAMA KURALLARI (ZORUNLU):
1. SIFIR HATA PRENSİBİ: Yazdığın kodda tanımlanıp kullanılmayan tek bir değişken bulunamaz.

GÖREV:
Senden bir Ubuntu sistem otomasyon scripti yazmanı istiyorum.
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    # Hedef dosyanın "zaten var mı" kontrolü gerçek ev dizinine bakmasın.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        GO_PROMPT,
        "Go 1.26.0 sürümüyle bir script yazmanı istiyorum",
        "Python 3.12 kurulu mu, yazdığın kod çalışır mı?",
        "Yazdığın kodda hata var mı kontrol et, sürüm 2.5.0",
        "Bu yazılım v1.2 ile uyumlu mu",
        "x" * 301 + " rapor.txt oluştur",
    ],
)
def test_should_not_match(text):
    assert match_file_modification(text) is None


def test_simple_create_still_matches():
    r = match_file_modification("masaüstünde notlar.txt adında bir dosya oluştur")
    assert r == {"action": "create_file", "path": "desktop", "name": "notlar.txt"}


def test_create_with_quoted_content_still_matches():
    r = match_file_modification("rapor.txt dosyası oluştur ve içine 'merhaba dünya' yaz")
    assert r is not None
    assert r["name"] == "rapor.txt"
    assert r["content"] == "merhaba dünya"


def test_natural_language_write_still_matches():
    r = match_file_modification("test.txt dosyasının içine MERHABA JARVIS yaz")
    assert r == {
        "action": "write",
        "path": ".",
        "name": "test.txt",
        "content": "MERHABA JARVIS",
        "append": False,
    }


def test_polite_create_form_matches():
    r = match_file_modification("belgeler klasöründe liste.md oluşturur musun")
    assert r is not None and r["path"] == "documents" and r["name"] == "liste.md"
