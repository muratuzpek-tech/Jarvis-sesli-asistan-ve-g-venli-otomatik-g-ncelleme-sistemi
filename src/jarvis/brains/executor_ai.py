"""executor_ai.py — EXECUTOR AI: Uygulama Beyni.

Görevi (bölüm 7): ONAYLANMIŞ işlemleri GERÇEK, mevcut araçlarla
gerçekleştirmek. Araçları kendi içinde YENİDEN YAZMAZ - sadece var olan
araçları çağırır.

NOT (kullanıcının talimatındaki örnek araçlarla mevcut proje arasındaki
fark, ŞEFFAFLIK İÇİN burada belirtiliyor): Talimatta örnek olarak
"auto_task_scheduler" ve "telegram_notify" verilmiş, ama bunlar mevcut
actions/ klasöründe DEĞİL (Downloads'ta ayrı, kullanılmayan deneme
dosyaları olarak duruyorlar). Onların yerine gerçekten var olan ve
kanıtlanmış araçlar kullanılıyor: file_controller, jarvis_backup_tool,
guvenli_kasa, discovery/entegrasyon (GitHub kendini geliştirme hattı).

GÜVENLİK SINIRI (tools_kopru.py ile BİREBİR AYNI felsefe - bilerek
tekrarlanmıyor, DOĞRUDAN tools_kopru.NotAllowedTool kullanılıyor):
tanınmayan bir araç adı gelirse ASLA "o zaman kod yazıp çalıştırayım" gibi
bir yedek yola düşülmez.

send_message ÖZELLİKLE Executor'da DOĞRUDAN çağrılamaz - bu oturumda
gerçek bir güvenlik açığına (yanlış kişiye otomatik WhatsApp mesajı)
sebep olan TAM OLARAK bu davranıştı (bkz. actions/agent_loop.py'nin
_notify_pending_approval geçmişi). Bir mesaj gönderilmesi gerekiyorsa
Executor bunu ORCHESTRATOR'a geri bildirir, gerçek gönderim ancak
kullanıcının Jarvis'e normal konuşmada AÇIKÇA onay verdiği, main.py'nin
ZATEN var olan send_message akışı üzerinden olur."""
from __future__ import annotations

from pathlib import Path

from jarvis.brains.base_brain import BaseBrain, BrainError


def _exec_file_controller(params: dict) -> str:
    from jarvis.actions.file_controller import file_controller
    # DUZELTME (kullanici onayli, 2026-09-15, "'Done.' hata gizleme"):
    # file_controller() HER durumda (basari/hata) anlamli bir string doner -
    # `or "Done."` sadece bu string BOS ("") oldugunda devreye giriyordu, ki
    # bunun TEK gercek senaryosu bos bir dosyanin OKUNMASIYDI (icerik="").
    # O durumda "Done." donmek, "dosya basariyla okundu ama icerigi bos"
    # bilgisini gizleyip anlamsiz/yaniltici bir mesaja donusturuyordu (canli
    # E2E testte gozlemlendi: basarisiz bir yazmadan sonra bos kalan dosya
    # okunup "Done." donmustu - gercekte ne olustugu gizlenmis oluyordu).
    # file_controller() zaten None donmedigi icin bu fallback'e hic gerek yok.
    return file_controller(parameters=params)


def _exec_backup_create(params: dict) -> str:
    # Kok dizin TEK yerden: backup_tool.jarvis_project_root() (src/jarvis).
    from jarvis.backup_tool import JarvisBackupTool
    path = JarvisBackupTool.for_jarvis().create_backup()
    return f"Yedek oluşturuldu: {path}"


def _exec_backup_rollback(params: dict) -> str:
    """Jarvis klasorunun TAMAMINI en son yedege atomik olarak geri alir
    (HIGH risk, yalnizca kullanici onayiyla). Basarisizlik "tamamlandi"
    olarak donmez: BrainError yukseltilir, adim basarisiz sayilir."""
    from jarvis.backup_tool import JarvisBackupTool
    tool = JarvisBackupTool.for_jarvis()
    if not tool.rollback():
        raise BrainError(f"executor_ai: rollback başarısız, proje değiştirilmedi: {tool.last_error}")
    return "Rollback tamamlandı."


def _exec_vault_encrypt(params: dict) -> str:
    from jarvis.guvenli_kasa import encrypt_file
    password = params.get("password")
    if not password:
        raise BrainError("executor_ai: guvenli_kasa.encrypt için 'password' zorunlu (asla varsayılan/uydurma parola kullanılmaz).")
    target = encrypt_file(Path(params["source"]), password, delete_original=bool(params.get("delete_original", False)))
    return f"Şifrelendi: {target}"


def _exec_vault_decrypt(params: dict) -> str:
    from jarvis.guvenli_kasa import decrypt_file
    password = params.get("password")
    if not password:
        raise BrainError("executor_ai: guvenli_kasa.decrypt için 'password' zorunlu.")
    target = decrypt_file(Path(params["source"]), password, delete_encrypted=bool(params.get("delete_encrypted", False)))
    return f"Şifre çözüldü: {target}"


def _exec_github_search(params: dict) -> str:
    from jarvis.actions.github_arama import github_search
    return github_search(params) or "Done."


def _exec_windows_system(params: dict) -> str:
    # YENI (kullanici talimati, Capability/Agent/Tool/Executor/Auditor
    # mimarisi, 2026-09-16): actions/capability_resolver.py'nin GERCEK,
    # dogrulanmis bir "windows_system" cozumlemesi buraya ulastirdigi
    # params ({"command_name": "..."}) ile calisir. Ikinci bir tool sistemi
    # OLUSTURULMUYOR - actions/tools_kopru.py'nin MEVCUT wrapper'i
    # (_call_windows_system -> actions/windows_shell.run()) DOGRUDAN
    # cagriliyor, tipki agent_loop'un zaten yaptigi gibi. windows_shell.py
    # zaten kendi allowlist/timeout/output-limit guvenligini uyguluyor -
    # burada TEKRARLANMIYOR.
    from jarvis.actions.tools_kopru import ALLOWED_TOOLS
    return ALLOWED_TOOLS["windows_system"](params) or "Done."


# Kasitli olarak KISA tutulur - sadece gercekten guvenli, mevcut ve test
# edilmis araclar. tools_kopru.py'nin ALLOWED_TOOLS'uyla AYNI felsefe.
# "windows_system" (2026-09-16 eklendi): actions/tools_kopru.py'de zaten
# calisan, salt-okunur, allowlist'li bir capability - ikinci bir uygulama
# YAZILMADI, sadece MEVCUT wrapper'a yonlendiriliyor (bkz. yukaridaki
# _exec_windows_system).
_ALLOWED_ACTIONS = {
    "file_controller": _exec_file_controller,
    "backup_create": _exec_backup_create,
    "backup_rollback": _exec_backup_rollback,
    "vault_encrypt": _exec_vault_encrypt,
    "vault_decrypt": _exec_vault_decrypt,
    "github_search": _exec_github_search,
    "windows_system": _exec_windows_system,
}

SYSTEM_PROMPT = """Sen JARVIS AI Beyin Takımı'nın UYGULAMA BEYNİsin (executor_ai).
Sadece zaten onaylanmış, yapılandırılmış bir işlemi çalıştırırsın. Kod
yazmaz, karar vermezsin - sana ne söylenirse (izin verilen araçlar
içindeyse) onu, olduğu gibi çalıştırırsın."""


class ExecutorAI(BaseBrain):
    NAME = "executor_ai"
    SYSTEM_PROMPT = SYSTEM_PROMPT

    def handle(self, message: dict) -> dict:
        payload = message.get("payload") or {}
        action = payload.get("action", "")
        params = payload.get("params") or {}

        if action == "send_message":
            # BILEREK ENGELLENIYOR - bkz. dosya basi guvenlik notu.
            raise BrainError(
                "executor_ai: send_message DOĞRUDAN çağrılamaz. Bir mesaj gönderilmesi "
                "gerekiyorsa bunu orchestrator'a 'requires_user_channel' olarak bildir; "
                "gerçek gönderim SADECE kullanıcının Jarvis'e sesli/yazılı AÇIKÇA onay "
                "verdiği mevcut main.py akışı üzerinden yapılabilir."
            )

        fn = _ALLOWED_ACTIONS.get(action)
        if fn is None:
            # tools_kopru.NotAllowedTool ile AYNI felsefe (tanınmayan bir işlem
            # ASLA "o zaman kod yazıp çalıştırayım" yedeğine düşmez) - ama mesaj
            # burada executor_ai'ye özel, tools_kopru'nun agent_loop'a özel
            # metnini yanıltıcı şekilde tekrar etmiyoruz.
            raise BrainError(
                f"executor_ai: '{action}' izinli işlemler listesinde değil. "
                f"İzinli işlemler: {', '.join(sorted(_ALLOWED_ACTIONS))}"
            )

        try:
            result = fn(params)
        except BrainError:
            raise
        except Exception as e:
            raise BrainError(f"executor_ai: '{action}' çalıştırılırken hata: {e}") from e

        self.log(f"Çalıştırıldı: {action} -> {str(result)[:150]}")
        return self.ok(message, {"action": action, "result": result})
