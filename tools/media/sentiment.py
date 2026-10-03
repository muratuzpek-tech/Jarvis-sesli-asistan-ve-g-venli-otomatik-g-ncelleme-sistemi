"""
tools/media/sentiment.py — Türkçe Duygu Analizi

Boran-Sert'in emotion_music_map fikri + genişletilmiş Türkçe sözlük.

Çıktı:
    SentimentResult(label, score, mood, suggested_genres)

Kullanım:
    from tools.media.sentiment import analyze_sentiment
    r = analyze_sentiment("Bugün çok moralim bozuk, kendimi kötü hissediyorum")
    → SentimentResult(label='negative', score=-0.8, mood='sad', suggested_genres=['chill','lo-fi','piano'])
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SentimentResult:
    """Duygu analizi sonucu."""
    label: str          # positive | negative | neutral
    score: float        # -1.0 (çok negatif) → +1.0 (çok pozitif)
    mood: str           # sad | angry | anxious | happy | excited | calm | neutral
    suggested_genres: list[str] = field(default_factory=list)
    emotion_keywords: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        icons = {"positive": "😊", "negative": "😔", "neutral": "😐"}
        return f"{icons.get(self.label, '?')} {self.label} ({self.score:+.2f}) mood={self.mood}"


# ── Türkçe Duygu Sözlükleri ───────────────────────────────────

_POSITIVE_WORDS = {
    # Genel pozitif
    "mutlu", "iyi", "güzel", "harika", "muhteşem", "süper", "mükemmel",
    "keyifli", "eğlenceli", "hoş", "sevinçli", "gururlu", "başarılı",
    "muazzam", "efsane", "fevkalade", "tatlı", "şahane", "destan",
    # Heyecan/coşku
    "heyecanlı", "coşkulu", "enerjik", "canlı", "dinamik",
    "özel", "değerli", "sevgili", "aşık", "tutkulu", "sevindim",
    # Başarı
    "başardım", "kazandım", "oldu", "tamam", "çözüldü", "halloldu",
    "anladım", "buldum", "yaptım", "geldi", "açıldı", "çalıştı",
}

_NEGATIVE_SAD = {
    "üzgün", "mutsuz", "kötü", "berbat", "korkunç", "felaket",
    "kederli", "çaresiz", "yalnız", "yapayalnız", "kaybettim",
    "kaybet", "ölü", "veda", "ayrılık", "özlem", "hasret",
    "moralim bozuk", "kendimi kötü", "bunalımda", "depresif",
    "ağlıyorum", "ağlamak", "hüzünlü", "hüzün", "melankoli",
    "boşluktayım", "anlamsız", "umutsuz", "çökmüş", "bitkin",
}

_NEGATIVE_ANGRY = {
    "sinirli", "kızgın", "öfkeli", "bıktım", "usandım",
    "gıcık", "tiksindim", "nefret", "iğrenç", "rezil",
    "delireceğim", "cinnet", "öldürü", "katlet", "yeter artık",
    "bugün çok berbat", "moralim bozuk", "isyan", "sıkıldım",
}

_NEGATIVE_ANXIOUS = {
    "korkuyorum", "endişeli", "kaygı", "tedirgin", "gergin",
    "panik", "stres", "baskı", "sınav", "yetenemiyorum",
    "beceriksiz", "kaygılı", "huzursuz", "rahatsız",
}

_CALM_WORDS = {
    "sakin", "huzurlu", "dingin", "rahat", "uzanmış", "dinlen",
    "nargile", "kahve", "çay", "deniz", "doğa", "kitap oku",
    "meditasyon", "nefes", "yavaş", "yumşak", "lo-fi",
}


# ── Duygu → Müzik Eşlemesi (Boran'dan + genişletilmiş) ────────

_MOOD_MUSIC_MAP: dict[str, dict[str, Any]] = {
    "sad": {
        "genres": ["chill", "lo-fi", "piano", "acoustic", "indie"],
        "target_valence": 0.3,
        "target_energy": 0.3,
        "description": "Sakin, yatıştırıcı",
    },
    "angry": {
        "genres": ["rock", "metal", "punk", "alternative"],
        "target_valence": 0.5,
        "target_energy": 0.85,
        "description": "Enerjik, yatıştırıcı ama güçlü",
    },
    "anxious": {
        "genres": ["ambient", "classical", "instrumental", "lo-fi"],
        "target_valence": 0.4,
        "target_energy": 0.25,
        "description": "Sakinleştirici, odaklanma",
    },
    "happy": {
        "genres": ["pop", "dance", "feel good", "upbeat"],
        "target_valence": 0.85,
        "target_energy": 0.7,
        "description": "Neşeli, pozitif",
    },
    "excited": {
        "genres": ["electronic", "house", "edm", "pop"],
        "target_valence": 0.9,
        "target_energy": 0.85,
        "description": "Yüksek enerji, dans",
    },
    "calm": {
        "genres": ["jazz", "bossa nova", "acoustic", "easy listening"],
        "target_valence": 0.6,
        "target_energy": 0.3,
        "description": "Rahatlatıcı, huzurlu",
    },
    "neutral": {
        "genres": ["indie", "pop", "chill", "acoustic"],
        "target_valence": 0.5,
        "target_energy": 0.5,
        "description": "Dengeli, genel",
    },
}


def analyze_sentiment(text: str) -> SentimentResult:
    """
    Türkçe metinden duygu analizi yap.
    
    Args:
        text: Kullanıcı cümlesi
    
    Returns:
        SentimentResult: label + score + mood + önerilen türler
    
    Örnekler:
        >>> analyze_sentiment("Bugün moralim çok bozuk")
        ... 😔 negative (-0.8) mood=sad
        
        >>> analyze_sentiment("Harika bir gün, çok mutluyum!")
        ... 😊 positive (+0.9) mood=happy
        
        >>> analyze_sentiment("Rahatlayıp dinlenmek istiyorum")
        ... 😐 neutral mood=calm
    """
    lower = text.lower()
    words = set(re.findall(r"[a-zçğıöşü]+", lower))

    # Exact match + substring match (Türkçe ek nedeniyle kelime bölünüyor)
    def _match_set(words: set, lexicon: set) -> set:
        hits = set()
        for w in words:
            if w in lexicon:
                hits.add(w)
            else:
                for lw in lexicon:
                    if lw in w or w in lw:
                        hits.add(lw)
                        break
        return hits

    pos_hits  = _match_set(words, _POSITIVE_WORDS)
    sad_hits  = _match_set(words, _NEGATIVE_SAD)
    ang_hits  = _match_set(words, _NEGATIVE_ANGRY)
    anx_hits  = _match_set(words, _NEGATIVE_ANXIOUS)
    calm_hits = _match_set(words, _CALM_WORDS)

    # Skor hesaplama
    pos_score = len(pos_hits) * 0.3
    neg_score = -(len(sad_hits) * 0.35 + len(ang_hits) * 0.3 + len(anx_hits) * 0.25)
    calm_score = len(calm_hits) * 0.15

    raw_score = pos_score + neg_score + calm_score
    raw_score = max(-1.0, min(1.0, raw_score))

    # Mood belirleme
    all_neg = sad_hits | ang_hits | anx_hits
    if sad_hits and len(sad_hits) >= max(len(ang_hits), len(anx_hits)):
        mood = "sad"
    elif ang_hits and len(ang_hits) >= max(len(sad_hits), len(anx_hits)):
        mood = "angry"
    elif anx_hits and len(anx_hits) >= max(len(sad_hits), len(ang_hits)):
        mood = "anxious"
    elif pos_hits and raw_score > 0.3:
        mood = "excited" if raw_score > 0.6 else "happy"
    elif calm_hits and len(calm_hits) > len(pos_hits):
        mood = "calm"
    elif raw_score > 0.1:
        mood = "happy"
    elif raw_score < -0.1:
        mood = "sad"
    else:
        mood = "neutral"

    # Label
    if raw_score > 0.1:
        label = "positive"
    elif raw_score < -0.1:
        label = "negative"
    else:
        label = "neutral"

    # Mood → genres
    music_cfg = _MOOD_MUSIC_MAP.get(mood, _MOOD_MUSIC_MAP["neutral"])

    return SentimentResult(
        label=label,
        score=round(raw_score, 2),
        mood=mood,
        suggested_genres=music_cfg["genres"],
        emotion_keywords=list(pos_hits | all_neg | calm_hits),
    )
