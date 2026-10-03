"""
tools/media/spotify_ctrl.py — Spotify Müzik Kontrolü

Boran-Sert'in MusicPlayer sınıfı + duygu entegrasyonu.

Özellikler:
  - Şarkı ara & çal (Türkçe query temizleyici ile)
  - Duygu durumuna göre müzik önerisi & çalma
  - Play / Pause / Next / Previous / Resume
  - Şu an çalan şarkı
  - Aktif cihaz yönetimi
  - Spotify yoksa YouTube fallback

Kullanım:
    ctrl = SpotifyController()
    await ctrl.play("Sezen Aksu Gülümse")
    await ctrl.play_by_mood("moralim bozuk")  # → chill müzik
"""
from __future__ import annotations

import logging
import os
import re
import webbrowser
from typing import Any

from tools.media.sentiment import analyze_sentiment, SentimentResult

logger = logging.getLogger("tools.media.spotify")

# ── Türkçe Query Temizleyici (Boran'dan) ──────────────────────

def clean_music_query(text: str) -> str:
    """Türkçe eylem kelimelerini temizle, geriye şarkı adını bırak."""
    patterns = [
        r"\bçal(armısın|ar mısın?|sın)?\b",
        r"\baç(armısın|ar mısın?|sın)?\b",
        r"\boynat(\s*mısın)?\b",
        r"\bçal(\s*ban)\b",
        r"\bdinle(t|telim)?\b",
        r"\bbul(ur musun|urmusun)?\b",
        r"\bbaşlat(\s*mısın)?\b",
        r"\bmüzik\s*",
        r"\bşarkı\s*",
        r"\bparça\s*",
        r"\blütfen\s*",
        r"\bşu\s*",
        r"\bolarak\s*",
    ]
    cleaned = text.strip()
    for p in patterns:
        cleaned = re.sub(p, "", cleaned, flags=re.IGNORECASE)
    # Artifact temizliği: kalan tek harf/suffix parçaları ("yı", "yı ", "n" vb.)
    cleaned = re.sub(r"\b[ytnnmkslr]\w{0,1}\b", "", cleaned)  # tek-iki harfli artıklar
    cleaned = re.sub(r"\s+", " ", cleaned)  # çoklu boşluk
    return cleaned.strip()


class SpotifyController:
    """
    Spotify kontrolcüsü (opsiyonel spotipy bağımlılığı ile).
    
    Spotify kurulu değilse → YouTube fallback çalışır.
    """

    def __init__(self) -> None:
        self._sp = None
        self._logged_in = False

    def _ensure_spotify(self) -> bool:
        """Spotipy bağlantısını başlat (lazy init)."""
        if self._logged_in:
            return self._sp is not None

        self._logged_in = True
        try:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth

            client_id = os.environ.get("SPOTIPY_CLIENT_ID", "")
            client_secret = os.environ.get("SPOTIPY_CLIENT_SECRET", "")
            redirect_uri = os.environ.get("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

            if not client_id:
                logger.info("[Spotify] 🔑 SPOTIPY_CLIENT_ID yok → YouTube fallback")
                return False

            auth = SpotifyOAuth(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
                scope="user-modify-playback-state user-read-playback-state playlist-read-private",
            )
            self._sp = spotipy.Spotify(auth_manager=auth)
            logger.info("[Spotify] ✅ Bağlandı")
            return True

        except ImportError:
            logger.info("[Spotify] 📦 spotipy kurulu değil → YouTube fallback")
            return False
        except Exception as e:
            logger.warning(f"[Spotify] ❌ Bağlanamadı: {type(e).__name__} → YouTube fallback")
            return False

    def _get_active_device(self) -> str | None:
        """Aktif Spotify cihazını bul."""
        try:
            devices = self._sp.devices()
            if not devices or not devices.get("devices"):
                return None
            for device in devices["devices"]:
                if device.get("is_active"):
                    return device["id"]
            # Aktif yoksa ilkini al
            return devices["devices"][0]["id"]
        except Exception:
            return None

    def _youtube_fallback(self, query: str) -> str:
        """Spotify yoksa YouTube'da ara ve aç."""
        import urllib.parse
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        webbrowser.open(url)
        return f"YouTube'da '{query}' arandı (Spotify yerine)"

    # ── Ana Kontroller ─────────────────────────────────────────

    def play_specific(self, query: str) -> str:
        """
        Şarkı ara ve çal.
        
        Args:
            query: "Sezen Aksu Gülümse" veya "Gülümse çal" (temizlenir)
        """
        cleaned = clean_music_query(query)
        if not cleaned:
            return "Hangi şarkıyı çalmamı istersiniz?"

        if self._ensure_spotify():
            try:
                results = self._sp.search(q=cleaned, type="track", limit=1)
                tracks = results.get("tracks", {}).get("items", [])
                if tracks:
                    track = tracks[0]
                    artist = track["artists"][0]["name"]
                    name = track["name"]
                    device_id = self._get_active_device()
                    self._sp.start_playback(
                        device_id=device_id,
                        uris=[track["uri"]],
                    )
                    return f"🎵 Çalıyor: {artist} — {name}"
                else:
                    return f"'{cleaned}' bulunamadı Spotify'da."
            except Exception as e:
                logger.warning(f"[Spotify] Play hatası: {e}")
                return self._youtube_fallback(cleaned)

        return self._youtube_fallback(cleaned)

    def play_by_mood(self, text: str) -> str:
        """
        Duygu durumuna göre müzik çal.
        
        Args:
            text: "Moralim bozuk" veya "Harika gün geçiriyorum"
        """
        sentiment = analyze_sentiment(text)
        genres = sentiment.suggested_genres
        genre_query = " ".join(genres[:2])

        logger.info(f"[MoodMusic] 🎭 {sentiment} → genres={genres}")

        if self._ensure_spotify():
            try:
                # Genre önerisi ile arama yap
                results = self._sp.search(
                    q=f"genre:{genres[0]}", type="playlist", limit=1
                )
                playlists = results.get("playlists", {}).get("items", [])
                if playlists:
                    playlist = playlists[0]
                    device_id = self._get_active_device()
                    self._sp.start_playback(
                        device_id=device_id,
                        context_uri=playlist["uri"],
                    )
                    return (
                        f"🎭 Ruh halin: {sentiment.mood} ({sentiment.label}) "
                        f"→ 🎵 Çalıyor: {playlist['name']} "
                        f"({', '.join(genres)})"
                    )

                # Playlist bulunamazsa genre-based track search
                results = self._sp.search(q=f"genre:{genres[0]}", type="track", limit=5)
                tracks = results.get("tracks", {}).get("items", [])
                if tracks:
                    uris = [t["uri"] for t in tracks]
                    device_id = self._get_active_device()
                    self._sp.start_playback(device_id=device_id, uris=uris)
                    first = tracks[0]
                    return (
                        f"🎭 {sentiment.mood} → 🎵 {first['artists'][0]['name']} "
                        f"— {first['name']} (+{len(tracks)-1} şarkı)"
                    )

            except Exception as e:
                logger.warning(f"[Spotify] Mood play hatası: {e}")

        # Fallback: YouTube'da genre + mood ara
        mood_query = f"{genre_query} music {sentiment.mood}"
        return (
            f"🎭 Duygu analizi: {sentiment.mood} ({sentiment.score:+.2f}) "
            f"→ {self._youtube_fallback(mood_query)}"
        )

    def pause(self) -> str:
        if self._ensure_spotify():
            try:
                self._sp.pause_playback(device_id=self._get_active_device())
                return "⏸️ Duraklatıldı"
            except Exception as e:
                return f"Duraklatma hatası: {e}"
        return "Spotify bağlı değil"

    def resume(self) -> str:
        if self._ensure_spotify():
            try:
                self._sp.start_playback(device_id=self._get_active_device())
                return "▶️ Devam ediyor"
            except Exception as e:
                return f"Devam hatası: {e}"
        return "Spotify bağlı değil"

    def next_track(self) -> str:
        if self._ensure_spotify():
            try:
                self._sp.next_track(device_id=self._get_active_device())
                return "⏭️ Sonraki şarkı"
            except Exception as e:
                return f"Şarkı değiştirme hatası: {e}"
        return "Spotify bağlı değil"

    def prev_track(self) -> str:
        if self._ensure_spotify():
            try:
                self._sp.previous_track(device_id=self._get_active_device())
                return "⏮️ Önceki şarkı"
            except Exception as e:
                return f"Önceki şarkı hatası: {e}"
        return "Spotify bağlı değil"

    def current_track(self) -> str:
        if self._ensure_spotify():
            try:
                track = self._sp.current_playback()
                if not track or not track.get("item"):
                    return "🎵 Şu an hiçbir şey çalmıyor"
                item = track["item"]
                artist = item["artists"][0]["name"]
                name = item["name"]
                progress = track.get("progress_ms", 0) // 1000
                duration = item.get("duration_ms", 0) // 1000
                status = "▶️" if track.get("is_playing") else "⏸️"
                return f"{status} {artist} — {name} ({progress}s / {duration}s)"
            except Exception as e:
                return f"Now playing hatası: {e}"
        return "Spotify bağlı değil"


# ── Singleton ─────────────────────────────────────────────────
spotify = SpotifyController()
