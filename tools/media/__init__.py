"""
media — Jarvis 2.0 medya + duygu araçları
"""
from tools.media.sentiment import analyze_sentiment, SentimentResult
from tools.media.spotify_ctrl import SpotifyController

__all__ = ["analyze_sentiment", "SentimentResult", "SpotifyController"]

from tools.registry import registry, ToolContext

@registry.register(
    name="spotify_control",
    description="Spotify müzik kontrolü: şarkı çal, durdur, ileri/geri, ruh haline göre müzik öner. "
                "Duygu analizi entegre: 'moralim bozuk' → chill müzik. Spotify yoksa YouTube fallback.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "play | play_by_mood | pause | resume | next | prev | current "
                               "(play: şarkıyı çal, play_by_mood: duyguya göre müzik)"
            },
            "query": {"type": "STRING", "description": "Şarkı adı veya duygu ifadesi"},
        },
        "required": ["action"],
    },
    security="normal",
    category="media",
)
def _spotify_handler(args: dict, ctx: ToolContext) -> str:
    from tools.media.spotify_ctrl import spotify
    from tools.media.sentiment import analyze_sentiment

    action = args.get("action", "current").lower().strip()
    query = args.get("query", "")

    if action == "play":
        return spotify.play_specific(query)
    elif action == "play_by_mood":
        return spotify.play_by_mood(query)
    elif action == "pause":
        return spotify.pause()
    elif action == "resume":
        return spotify.resume()
    elif action == "next":
        return spotify.next_track()
    elif action == "prev":
        return spotify.prev_track()
    elif action == "current":
        return spotify.current_track()
    elif action == "sentiment":
        r = analyze_sentiment(query)
        return f"🎭 Duygu: {r} | Önerilen: {', '.join(r.suggested_genres)}"
    else:
        return f"Bilinmeyen Spotify action: {action}"
