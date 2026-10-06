"""security_ai.py — SECURITY AI: Güvenlik Beyni.

Görevi (bölüm 5 ve 15): kritik işlemleri kontrol eden BAĞIMSIZ güvenlik
katmanı. Her işlem için {"approved": bool, "risk": "low|medium|high",
"reason": "..."} üretir. HIGH riskli işlemlerde kullanıcı onayı zorunludur.

TASARIM TERCİHİ (bilinçli): Risk sınıflandırması SADECE Gemini'nin kararına
bırakılmaz - deterministik bir kural tablosuyla yapılır (aşağıdaki
_HIGH_RISK_ACTIONS / _MEDIUM_RISK_ACTIONS), tıpkı actions/tools_kopru.py'nin
is_destructive() fonksiyonunun ZATEN yaptığı gibi. Sebep: bir LLM'in "bu
güvenli" demesi tek başına güvenilir bir güvenlik sınırı değildir - bu
oturumda gerçek bir güvenlik açığı (WhatsApp'a yanlışlıkla mesaj gönderme)
tam olarak "otomatik/LLM kararına güvenme" yüzünden yaşandı. Gemini SADECE
kullanıcıya gösterilecek okunabilir 'reason' metnini üretir, KARARI değil.
"""
from __future__ import annotations

import re

from jarvis.brains.base_brain import BaseBrain, BrainError

# 15. RİSK SİSTEMİ + 5. bölümdeki "kontrol edeceği işlemler" listesiyle
# birebir uyumlu, deterministik eşleme. (tool, action) -> risk seviyesi.
# file_controller BU TABLOLARDA YOK: classify_risk onu fail-closed olarak
# file_controller.READONLY_ACTIONS ile siniflandirir (salt-okunur -> LOW,
# geri kalan her sey -> HIGH).
_HIGH_RISK = {
    ("computer_settings", "shutdown"),
    ("computer_settings", "restart"),
    ("computer_settings", "lock_screen"),
    ("computer_settings", "lock"),
    ("send_message", None),          # disariya HER mesaj HIGH
    ("entegrasyon_uygula", None),    # Jarvis'in gercek koduna yazmak HIGH
    ("discovery_register", None),
    # NOT: asagidaki isimler, executor_ai.py'nin GERCEK _ALLOWED_ACTIONS
    # sozlugundeki isimlerle BIREBIR ayni olmali - iki dosya arasinda farkli
    # bir isimlendirme kullanmak, risk kontrolunun sessizce atlanmasina yol
    # acabilirdi (bkz. brain_orchestrator._risk_of_step, iki dosyayi da
    # kullanan yer).
    ("vault_encrypt", None),
    ("vault_decrypt", None),
    ("backup_rollback", None),
    ("coder_ai", "modify_critical_file"),
    ("install_program", None),
    ("download_file", None),
}
_MEDIUM_RISK = {
    ("coder_ai", "write_new_file"),
    ("backup_create", None),

    # GUARD: executor varyasyonları (2026-10-03 güvenlik fix)
    ("coder_ai", "write_file"),
}
# Bilinen SALT-OKUNUR işlemler (Adım 3.4): yalnızca bunlar LOW. (tool, None)
# aracın her eylemi demektir. windows_system sabit bir allowlist'ten
# (windows_shell) komut seçer; research/github araması yalnızca okur.
_LOW_RISK = {
    ("research_ai", None),
    ("github_search", None),
    ("github_arama", None),
    ("windows_system", None),
    ("web_search", None),
    ("weather_report", None),
    ("system_status", None),
    ("coder_ai", "analyze"),
}
# Tablolarda olmayan (tanınmayan / sınıflandırılamayan) her şey MEDIUM -
# eskiden LOW'du ve yeni bir executor eylemi sessizce onaysız kalıyordu.
_DEFAULT_RISK = "medium"

# Araç ya da eylem adında KELİME olarak geçerse riski HIGH'a yükselten
# anahtar kelimeler (yalnızca yukarı; alt-dize değil: "information" içindeki
# "format" sayılmaz).
_DANGEROUS_WORDS = frozenset({
    "rm", "rmdir", "del", "delete", "remove", "erase", "wipe", "shred", "unlink",
    "format", "mkfs", "fdisk", "dd", "truncate", "drop",
    "shutdown", "poweroff", "halt", "reboot", "restart",
    "sudo", "su", "chmod", "chown", "kill", "pkill", "killall", "uninstall",
    "sil", "kapat", "biçimlendir",
})
_WORD_RE = re.compile(r"[^\wçğıöşü]+|_", re.UNICODE)


def _has_dangerous_word(*names) -> bool:
    for name in names:
        words = {w for w in _WORD_RE.split(str(name or "").casefold()) if w}
        if words & _DANGEROUS_WORDS:
            return True
    return False


SYSTEM_PROMPT = """Sen JARVIS AI Beyin Takımı'nın GÜVENLİK BEYNİsin (security_ai).

Sana bir işlem (tool, action, hedef) verilecek ve bu işlemin risk seviyesi
ZATEN deterministik bir kuralla belirlenmiş olacak. SENİN TEK GÖREVİN: bu
kararı kullanıcıya açıklayacak KISA, NET bir 'reason' cümlesi yazmak.
KARARI SEN VERMİYORSUN, sadece açıklıyorsun. Onaylanıp onaylanmayacağına
karar verme, sadece riskin NEDEN o seviyede olduğunu 1-2 cümleyle özetle.

SADECE şu JSON şemasında dön:
{"reason": "..."}"""


class SecurityAI(BaseBrain):
    NAME = "security_ai"
    SYSTEM_PROMPT = SYSTEM_PROMPT

    @staticmethod
    def _normalize_action(action: str | None) -> str | None:
        """Action isimlerini standartlaştır — 'delete_file' → 'delete'.

        Güvenlik tablosu kısa ad kullanir ('delete', 'move', ...),
        executor ayni eylemi uzun adla gönderebilir ('delete_file', ...).
        Eşleşme sağlanamazsa KÖTÜ amaçlı isim → default 'low' düşer
        ve işlem ONAYSIZ onaylanir. Bu normalizasyon bunu önler.
        """
        if not action:
            return None
        a = action.strip().lower()
        # Eşanlamlılar → tablo adı
        _ALIAS = {
            "delete_file": "delete",
            "delete_folder": "delete",
            "remove_file": "delete",
            "remove_folder": "delete",
            "remove": "delete",
            "erase": "delete",
            "move_file": "move",
            "move_folder": "move",
            "rename": "move",
            "rename_file": "move",
            "rename_folder": "move",
            "create_file": "create_file",
            "create_folder": "create_folder",
            "write_file": "create_file",
            "write_new_file": "write_new_file",
            "modify_file": "modify_critical_file",
            "edit_file": "modify_critical_file",
            "overwrite": "modify_critical_file",
            "shutdown_pc": "shutdown",
            "power_off": "shutdown",
            "reboot": "restart",
            "lock": "lock_screen",
            "send_message": "send_message",
            "send_email": "send_message",
            "send_mail": "send_message",
            "copy_file": "copy",
            "copy_folder": "copy",
            "install_program": "install_program",
            "install_package": "install_program",
            "pip_install": "install_program",
            "download_file": "download_file",
            "download": "download_file",
        }
        return _ALIAS.get(a, a)

    @staticmethod
    def classify_risk(tool: str, action: str | None) -> str:
        if tool == "file_controller":
            # Fail-closed: tablo yerine file_controller'in kendi takma ad ve
            # salt-okunur eylem listesi. Listede olmayan her eylem (ör.
            # 'trash', 'write', 'extract' ya da bilinmeyen bir ad) HIGH.
            from jarvis.actions.file_controller import is_readonly_action
            return "low" if is_readonly_action(action) else "high"
        norm_action = SecurityAI._normalize_action(action)
        # Tehlikeli anahtar kelime her zaman HIGH'a yükseltir (asla aşağı).
        if _has_dangerous_word(tool, action, norm_action):
            return "high"
        # Hem orijinal hem normalize edilmiş halini kontrol et
        for act in (norm_action, action):
            if (tool, act) in _HIGH_RISK or (tool, None) in _HIGH_RISK:
                return "high"
            if (tool, act) in _MEDIUM_RISK or (tool, None) in _MEDIUM_RISK:
                return "medium"
        for act in (norm_action, action):
            if (tool, act) in _LOW_RISK or (tool, None) in _LOW_RISK:
                return "low"
        return _DEFAULT_RISK

    def handle(self, message: dict) -> dict:
        payload = message.get("payload") or {}
        tool = payload.get("tool", "").strip()
        action = payload.get("action")
        target = payload.get("target", "")
        if not tool:
            raise BrainError("security_ai: 'tool' parametresi boş olamaz.")

        risk = self.classify_risk(tool, action)

        # Gemini'ye SADECE aciklama metni icin danisiyoruz; o cagri
        # basarisiz olsa bile (kota vb.) karar ETKİLENMEZ - deterministik
        # fallback metni kullanilir. Guvenlik kararinin bir LLM cagrisinin
        # basarisina bagli olmasi kabul edilemez.
        try:
            resp = self.call_llm_json(
                f"İŞLEM: tool={tool}, action={action}, hedef={target}, risk={risk}"
            )
            reason = resp.get("reason") if isinstance(resp, dict) else None
        except BrainError:
            reason = None

        if not reason:
            reason = f"'{tool}'" + (f".{action}" if action else "") + f" işlemi {risk.upper()} risk kategorisinde (deterministik kural tablosu)."

        approved = risk != "high"  # HIGH -> kullanici onayi ZORUNLU, burada asla otomatik onaylanmaz
        result = {"approved": approved, "risk": risk, "reason": reason}
        self.log(f"Risk değerlendirildi: tool={tool} action={action} -> {risk} (approved={approved})")
        return self.ok(message, result)
