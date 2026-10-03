"""tests/test_media.py — Sentiment + Spotify regression tests"""
import sys
sys.path.insert(0, '.')
from tools.media.sentiment import analyze_sentiment
from tools.media.spotify_ctrl import clean_music_query


SENTIMENT_TESTS = [
    ("çok mutluyum", "happy"),
    ("çok stresliyim", "anxious"),
    ("çok heyecanlıyım", "excited"),
    ("sinirliyim", "angry"),
    ("moralim bozuk", "sad"),
    ("sakinim", "calm"),
    ("hiç mutlu değilim", "sad"),
    ("Harika gün çok mutluyum", "happy"),
]

def test_sentiment_all():
    for text, expected in SENTIMENT_TESTS:
        r = analyze_sentiment(text)
        assert r.mood == expected, f"'{text}' → {r.mood}, expected {expected}"

def test_sentiment_negation():
    r = analyze_sentiment("hiç mutlu değilim")
    assert r.mood in ("sad", "negative")

def test_clean_query():
    result = clean_music_query("Sezen Aksu Gülümse çal")
    assert "Sezen Aksu" in result
