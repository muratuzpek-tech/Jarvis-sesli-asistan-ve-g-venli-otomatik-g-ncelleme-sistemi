"""
sanitizer.py — hafif, bağımlılıksız PII/credential redaksiyonu.

`memory_manager.remember()`/`update_memory()` ve `conversation_log.log_turn()`
gibi diske kalıcı yazılan her noktada metni bu modülden geçirerek parola,
API anahtarı, token, kullanıcı-adı içeren dosya yolu gibi hassas verilerin
düz metin olarak `long_term.json` / `conversation_log.jsonl` içine
yazılmasını engellemeyi amaçlar.

Presidio/spaCy gibi ağır bağımlılıklar KULLANILMAZ — yalnızca regex.
Bu yüzden %100 garanti değildir, ama en sık karşılaşılan sızıntı
türlerini (bilinen API key formatları, "parola: ..." kalıpları,
kullanıcı adı içeren dosya yolları) önceden yakalar. Ultron projesinin
`utils/sanitizer.py`'ındaki CREDENTIAL_PATTERNS / PATH_PATTERNS
mantığından esinlenilmiştir, buraya Presidio kısmı taşınmamıştır.
"""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Sıra önemlidir: daha spesifik/dar kalıplar önce denenir.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,40}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("generic_bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_.=]{10,}")),
    (
        "labeled_secret",
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|token|secret|access[_-]?key)\s*[:=]\s*"
            r"[^\s,;]{6,}"
        ),
    ),
    (
        "labeled_password",
        # \w{0,6}: "parolam:", "parolanız:", "şifreniz:" gibi Türkçe iyelik
        # eki almış hallerini de yakalar (sadece çıplak "parola:" değil).
        re.compile(r"(?i)\b(parola|password|şifre|sifre|pwd)\w{0,6}\s*[:=]\s*\S+"),
    ),
    ("windows_user_path", re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+")),
    ("unix_home_path", re.compile(r"/home/[^/\s]+")),
]


def sanitize(text: str) -> str:
    """Metindeki bilinen kimlik bilgisi / parola / kullanıcı-yolu örüntülerini
    [REDACTED] ile değiştirir. Metin değilse veya boşsa dokunmadan döner."""
    if not text or not isinstance(text, str):
        return text
    for _name, pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


# Kalici bellege yazilan metin sistem istemine geri girer: talimata benzeyen
# bir kayit (ör. bir web sayfasindan modele sizmis "onceki talimatlari yok
# say") kalici bir prompt-injection olur. Bu kaliplar YALNIZCA isaretler -
# metni degistirmez; karar (onay istemek) security_gate'tedir. Sezgiseldir:
# yanlis pozitif yalnizca bir onay sorusuna, yanlis negatif bugunku davranisa
# yol acar.
_INSTRUCTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("ignore_previous", re.compile(
        r"(?i)\b(ignore|disregard|forget|override)\b.{0,30}\b(previous|prior|above|all|earlier)\b"
        r".{0,20}\b(instruction|rule|prompt|message)s?\b")),
    ("ignore_previous_tr", re.compile(
        r"(?i)(önceki|onceki|yukarıdaki|yukaridaki|tüm|tum|bütün|butun)\s+(talimat|kural|komut|yönerge|yonerge)\w*"
        r".{0,30}(yok\s*say|unut|görmezden|gormezden|uyma|geçersiz|gecersiz)")),
    ("role_override", re.compile(
        r"(?i)\b(you are now|act as|from now on|new instructions?)\b|"
        r"(bundan\s+sonra|artık|artik)\s+(sen|her\s+zaman|daima)")),
    ("always_do", re.compile(
        r"(?i)\b(always|her\s+zaman|daima|otomatik(?:\s+olarak)?)\b.{0,40}"
        r"\b(call|run|execute|invoke|approve|çağır|cagir|çalıştır|calistir|onayla|gönder|gonder|sil)\w*")),
    ("skip_confirmation", re.compile(
        r"(?i)\b(without|don'?t|do\s+not|never)\b.{0,20}\b(ask|asking|confirm|confirmation|approval)\b|"
        r"\b(onay|izin)\w*\s+(istemeden|almadan|sormadan)|\b(sormadan|onaysız|onaysiz)\b")),
    ("approval_token", re.compile(r"(?i)confirm_code|confirmation_required|onay\s*kodu")),
    ("system_tag", re.compile(r"(?i)<\s*/?\s*(system|instructions?|tool|assistant)\s*>|\[\s*(system|sistem)\s*\]")),
    ("system_prompt", re.compile(r"(?i)\b(system|sistem)\s+(prompt|talimat\w*|mesaj\w*|instruction\w*)")),
    ("tool_identifier", re.compile(
        r"\b(send_message|file_controller|computer_control|terminal_tool|self_improve|agent_loop|"
        r"code_helper|agentic_code|shutdown_jarvis|computer_settings)\b")),
]


def instruction_markers(text) -> list[str]:
    """Metin modele verilen bir TALIMATA benziyorsa eslesen kural adlari
    (bos liste = olagan icerik)."""
    if not text or not isinstance(text, str):
        return []
    return [name for name, pattern in _INSTRUCTION_PATTERNS if pattern.search(text)]


def sanitize_value(value):
    """remember()/update_memory() gibi str olmayan değerlerin de güvenle
    geçmesi için: sadece str ise sanitize eder, değilse aynen döner."""
    if isinstance(value, str):
        return sanitize(value)
    return value
