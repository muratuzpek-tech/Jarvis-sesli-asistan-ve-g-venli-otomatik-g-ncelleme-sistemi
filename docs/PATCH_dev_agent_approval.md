"""dev_agent.py — ONAY SİSTEMİ DÜZELTME (ApprovalRegistry Entegrasyonu).

DÜZELTMELER (2026-10-05):

❌ KALDIR:
  - _pending_dev_agent dict (satırlar 55-90)
  - confirmation_problem() fonksiyonu
  - _user_confirmed_after() fonksiyonu
  - _CONFIRMATION_TTL_S sabiti

✅ YENİ:
  - ApprovalRegistry.register() ile onay kodu üret
  - ApprovalRegistry.confirm() ile onay kontrol et
  - AYNI fingerprint'e sahip isteğe AYNI code (idempotent)
  
AYNEN KORUNDU:
  - Proje yazma ve testleme mantığı
  - quality gate sistemi
  - acceptance testing
  - Tüm diğer fonksiyonlar
"""

# ✅ YENİ IMPORTS (satırlar 1-50'nin içine ekle)
from jarvis.core.approval_registry import (
    get_registry,
    ApprovalType,
)
import hashlib


# ❌ SİLİNECEK KOD (satırlar 55-90 civarı):
# _pending_dev_agent: dict[str, dict] = {}
# _dev_agent_lock = threading.Lock()
# def confirmation_problem(confirm_code: str) -> str | None: ...
# def _user_confirmed_after(issued_at: float) -> bool: ...
# _CONFIRMATION_TTL_S = 60


# ✅ YENİ FONKSİYONLAR (eski confirmation_problem() yerine)

def get_confirmation_code(project_description: str) -> str:
    """Proje için onay kodu oluştur.
    
    AYNI proje açıklaması (fingerprint) → AYNI code döner (idempotent).
    Bu, aynı proje için iki kere "evet" demesi gereksin diye.
    
    Args:
        project_description: Proje açıklaması (örn. "Python calculator app")
        
    Returns:
        one-time confirmation code (örn. "a1b2c3")
    """
    registry = get_registry()
    
    # Fingerprint: proje açıklamasından hash
    fingerprint = hashlib.sha256(
        f"dev_agent:{project_description}".encode()
    ).hexdigest()[:16]
    
    code = registry.register_request(
        approval_type=ApprovalType.DEV_AGENT,
        fingerprint=fingerprint,
        metadata={
            "description": project_description[:200],
            "requested_at": __import__("time").monotonic(),
        },
        ttl_seconds=60  # 60 saniye geçerli
    )
    return code


def confirm_project_approval(confirm_code: str) -> bool:
    """Proje onayını doğrula.
    
    Kullanıcı "evet" dediğinde, bu fonksiyon onay kodunun geçerli olup
    olmadığını kontrol eder. Geçerliyse onay onaylanmış say.
    
    Args:
        confirm_code: Onay kodu
        
    Returns:
        True if code is valid and confirmed, False otherwise
    """
    registry = get_registry()
    request = registry.confirm(confirm_code)
    
    if request is None:
        return False
    
    return request.status == "confirmed"


def get_approval_status(confirm_code: str) -> str:
    """Onay durumunu getir.
    
    Args:
        confirm_code: Onay kodu
        
    Returns:
        "pending", "confirmed", "rejected", "expired", "not_found"
    """
    registry = get_registry()
    return registry.get_status(confirm_code)


# ✅ KULLANIM ÖRNEĞİ (_build_project() fonksiyonunda):
#
# Öncesi (❌ ESKI):
#     code = _get_dev_agent_code(description)
#     result = dev_agent(...)
#     if not result.startswith("BAŞARILI"):
#         return "Onay gerekli"
#
# Sonrası (✅ YENİ):
#     code = get_confirmation_code(project_description)
#     # main.py'de bu code Gemini'ye geri verilir
#     # Kullanıcı "evet" derse → main.py ApprovalRegistry.confirm(code) çağrır
#     if not confirm_project_approval(code):
#         return "ONAY HENÜZ ALINMADI — proje BAŞLATILMADI."
#     # Proje başlat

# NOT: dev_agent.py'deki diğer tüm kod (proje yazma, testing, quality gate, vb.)
# aynen kalır — sadece onay mekanizması merkezi registry'ye taşındı.
