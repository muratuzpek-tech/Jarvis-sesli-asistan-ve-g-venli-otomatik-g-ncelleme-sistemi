"""Kullanici cumlesi acik bir onay mi / ret mi? Saf fonksiyonlar.

main.py (bekleyen tehlikeli islem onayi) ve dev_agent (confirm_code kapisi)
ayni kurali kullanir. Onay: normalize edilmis cumlenin TUM kelimeleri onay
kumesinde olmali; "Tamam ama once ne yapacagini soyle" ya da "evet, bu dosya
ne?" gibi ek eylem/soru iceren cumleler onay sayilmaz (eskiden yalnizca ilk
kelimeye bakiliyordu)."""
import re

# Onay cumlesi YALNIZCA bunlardan olusabilir.
CONFIRMATION_TOKENS = frozenset({
    "evet", "evt", "tamam", "olur", "onay", "onayla", "onaylıyorum", "onayliyorum",
    "devam", "et", "edebilirsiniz", "yes", "ok", "okey", "lütfen", "lutfen",
})
# Tam cumle olarak kabul edilen acik onay ifadeleri (eski davranis).
CONFIRMATION_PHRASES = frozenset({
    "onaylıyorum", "onayliyorum", "evet onaylıyorum", "evet onayliyorum",
    "evet yap", "tamam onayla", "tamam yap", "devam et", "approve", "approve it", "yes do it",
})
# Cumlenin herhangi bir yerinde bunlar varsa ASLA onay sayilmaz.
NEGATIVE_WORDS = frozenset({
    "hayır", "hayir", "iptal", "dur", "durdur", "vazgeç", "vazgec",
    "yapma", "etme", "başlatma", "baslatma", "bekle", "istemiyorum",
    "değil", "degil", "no", "cancel", "stop", "deny",
})
_HEX_ID = re.compile(r"[0-9a-f]{8,}")
_COMPACT_PHRASES = frozenset(p.replace(" ", "") for p in CONFIRMATION_PHRASES | CONFIRMATION_TOKENS)


def _is_reference(token: str) -> bool:
    """Kimlik/kod benzeri token ("52d6b6b2", "confirm_code", "abc123") ek bir
    eylem degildir; "evet <gorev-id>" yine duyurulan istegi onaylar (onay
    her zaman en son duyurulan istege baglidir, metindeki kimlige degil)."""
    return "_" in token or any(ch.isdigit() for ch in token) or bool(_HEX_ID.fullmatch(token))


def normalize(text: str) -> str:
    cleaned = re.sub(r"[^\w\s]", " ", str(text or "").casefold())
    return " ".join(cleaned.split())


def is_rejection(text: str) -> bool:
    return any(tok in NEGATIVE_WORDS for tok in normalize(text).split())


def is_confirmation(text: str) -> bool:
    norm = normalize(text)
    if not norm:
        return False
    tokens = norm.split()
    if any(tok in NEGATIVE_WORDS for tok in tokens):
        return False
    if norm in CONFIRMATION_PHRASES:
        return True
    words = [tok for tok in tokens if not _is_reference(tok)]
    if words and all(tok in CONFIRMATION_TOKENS for tok in words):
        return True
    # Canli ASR kelimeyi parcalara bolebiliyor ("onaylı yorum").
    return norm.replace(" ", "") in _COMPACT_PHRASES
