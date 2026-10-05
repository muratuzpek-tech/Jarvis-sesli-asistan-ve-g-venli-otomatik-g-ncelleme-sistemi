"""
tools_kopru.py — agent_loop.py'nin Gemini'den gelen "sıradaki adım"
kararını Jarvis'in ZATEN VAR olan, güvenli sınırları test edilmiş
araçlarına yönlendiren ince köprü katmanı.

TASARIM SINIRI (kasıtlı, kullanıcıyla birlikte kararlaştırıldı):
Bu dosya YENİ bir yetenek (ham komut çalıştırma, LLM'in yazdığı Python
kodunu çalıştırma, vb.) EKLEMEZ — sadece aşağıdaki ALLOWED_TOOLS
listesindeki, halihazırda main.py'de de kullanılan gerçek fonksiyonları
çağırır. Bunun sebebi: Downloads'ta bulunan başka Jarvis denemelerinde
("Mark-XXXIX-OR"'un generated_code'u, "auto_task_scheduler"ın
shell=True ile ham komut çalıştırması) tam olarak bunun ne kadar
tehlikeli olabileceğini gördük — tanımadığı bir adım gelince LLM'in
kendi kod/komut uydurup çalıştırmasına asla izin verilmemeli. Burada
tanınmayan bir 'tool' adı gelirse, NotAllowedTool fırlatılır — asla
bir fallback ile rastgele kod çalıştırılmaz.

Ayrıca her aracın YIKICI (geri dönüşü zor/riskli) olup olmadığını
`is_destructive()` ile sınıflandırır — agent_loop.py bunu kullanarak
yıkıcı adımları otomatik çalıştırmadan önce kullanıcı onayına sunar.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any


class NotAllowedTool(Exception):
    """agent_loop, izin verilmeyen/tanınmayan bir araç adı istediğinde
    fırlatılır. Asla bir 'rastgele kod yaz ve çalıştır' fallback'ine
    düşülmez — bu, bu köprünün var olma sebebidir."""
    def __init__(self, tool: str):
        self.tool = tool
        super().__init__(
            f"'{tool}' agent_loop için izinli araçlar listesinde değil. "
            f"İzinli araçlar: {', '.join(sorted(ALLOWED_TOOLS))}"
        )


def _call_file_controller(parameters: dict) -> str:
    from jarvis.actions.file_controller import file_controller
    # DUZELTME (kullanici onayli, 2026-09-16, "'Done.' hata gizleme" -
    # brains/executor_ai.py'deki AYNI duzeltmenin buradaki karsiligi):
    # file_controller() HER durumda anlamli bir string doner - `or "Done."`
    # sadece bu string BOS ("") oldugunda devreye giriyordu, ki bunun TEK
    # gercek senaryosu bos bir dosyanin OKUNMASIYDI (icerik=""). Bu koprude
    # de AYNI file_controller() cagriliyor, dolayisiyla AYNI risk gecerliydi.
    # Diger 9 '... or "Done."' cagrisi (_call_reminder, _call_web_search,
    # _call_weather_report, _call_computer_settings, _call_send_message,
    # _call_github_arama, _call_github_arac_bul_ve_degerlendir,
    # _call_discovered_topydo, _call_discovered_jarvis_registry) TEK TEK
    # kaynak kodu okunarak dogrulandi - alttaki fonksiyonlarin HICBIRI
    # bos/falsy string donmuyor, o yuzden onlara DOKUNULMADI (olu kod,
    # gereksiz degisiklik yapilmadi).
    return file_controller(parameters=parameters)


def _call_reminder(parameters: dict) -> str:
    from jarvis.actions.reminder import reminder
    return reminder(parameters=parameters) or "Done."


def _call_web_search(parameters: dict) -> str:
    from jarvis.actions.web_search import web_search
    return web_search(parameters=parameters) or "Done."


def _call_weather_report(parameters: dict) -> str:
    from jarvis.actions.weather_report import weather_action
    return weather_action(parameters=parameters) or "Done."


def _call_system_status(parameters: dict) -> str:
    from jarvis.actions.system_monitor import get_system_status
    status = get_system_status()
    return "\n".join(f"{k}: {v}" for k, v in status.items())


def _call_system_scan_and_repair(parameters: dict) -> str:
    from jarvis.actions.system_scan import system_scan_and_repair
    return system_scan_and_repair(parameters=parameters)


def _call_computer_settings(parameters: dict) -> str:
    from jarvis.actions.computer_settings import computer_settings
    # agent_loop asla kendi kendine confirmed=yes gecemez - bu satir
    # yikici bir eylemin (shutdown/restart) LLM tarafindan sessizce
    # onaylanmis gibi gonderilmesini engeller. Kullanici onay verdiginde
    # bile gercek calistirma agent_loop.approve_task() icinde, BURADAN
    # BAGIMSIZ ayri bir yolla yapilir (bkz. agent_loop.py).
    parameters = dict(parameters)
    parameters.pop("confirmed", None)
    return computer_settings(parameters=parameters) or "Done."


def _call_send_message(parameters: dict) -> str:
    from jarvis.actions.send_message import send_message
    return send_message(parameters=parameters) or "Done."


def _call_github_arama(parameters: dict) -> str:
    from jarvis.actions.github_arama import github_search
    return github_search(parameters) or "Done."


def _call_github_arac_bul_ve_degerlendir(parameters: dict) -> str:
    from jarvis.actions.github_arama import github_arac_bul_ve_degerlendir
    return github_arac_bul_ve_degerlendir(parameters) or "Done."


def _call_entegrasyon_uygula(parameters: dict) -> str:
    # SADECE agent_loop.approve_task() uzerinden, kullanici acikca
    # onayladiktan SONRA cagrilir - bkz. is_destructive() ve
    # github_arama.py'nin basindaki tasarim notu. GitHub'dan bulunan bir
    # adayi Jarvis'in GERCEK koduna (actions/, tools_kopru.py) yazar.
    from jarvis.actions.github_arama import apply_pending_integration
    return apply_pending_integration(parameters)


def _call_discovery_register(parameters: dict) -> str:
    # SADECE agent_loop.approve_task() uzerinden, kullanici acikca
    # onayladiktan SONRA cagrilir - discovery.py'nin basindaki tasarim
    # notuna bakin: bu sadece bir KAYIT ekler, Jarvis'in gercek koduna
    # (main.py, actions/) hicbir sey yazmaz.
    from jarvis.actions.discovery import register_discovered_tool
    return register_discovered_tool(parameters)


def _call_discovered_topydo(parameters: dict) -> str:
    from jarvis.actions.discovered_topydo import run
    return run(parameters) or "Done."


def _call_discovered_jc(parameters: dict) -> str:
    from jarvis.actions.discovered_jc import run
    return run(parameters) or "Done."


def _call_windows_system(parameters: dict) -> str:
    """windows_shell.run()'a ince bir koprü - dosyanin basindaki tasarim
    sinirini (LLM'in kendi kod/komut uydurup calistirmasina asla izin
    verilmez) IHLAL ETMEZ: windows_shell.py SABIT bir allowlist disinda
    hicbir komutu kabul etmiyor, model sadece onceden tanimli bir
    command_name secebiliyor - ham/keyfi bir PowerShell komutu asla
    gecemiyor."""
    from jarvis.actions.windows_shell import run
    return run(parameters) or "Done."


# tool_adi -> cagiran fonksiyon. Kasitli olarak KISA tutulur - sadece
# gercekten guvenli ve test edilmis araclar.
ALLOWED_TOOLS: dict[str, Any] = {
    "windows_system": _call_windows_system,
    "discovered_jc": _call_discovered_jc,
    "discovered_topydo": _call_discovered_topydo,
    "file_controller":     _call_file_controller,
    "reminder":            _call_reminder,
    "web_search":          _call_web_search,
    "weather_report":      _call_weather_report,
    "system_status":       _call_system_status,
    "system_scan_and_repair": _call_system_scan_and_repair,
    "computer_settings":   _call_computer_settings,
    "send_message":        _call_send_message,
    "github_arama":        _call_github_arama,
    "github_arac_bul_ve_degerlendir": _call_github_arac_bul_ve_degerlendir,
    "entegrasyon_uygula":  _call_entegrasyon_uygula,
    "discovery_register":  _call_discovery_register,
}

# Her aracin parametre sekli, planlama sirasinda Gemini'ye gosterilecek
# kisa aciklama - agent_loop.py'nin prompt'unda kullanilir.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "windows_system": "SALT-OKUNUR Windows sistem bilgisi sorgular. parameters: command_name "
                       "(ZORUNLU) - izinli değerler: process_list, service_list, system_info, "
                       "cpu_load, disk_info, network_info, network_connections. Ham/keyfi "
                       "PowerShell komutu KABUL ETMEZ - sadece bu sabit isimler. Sonuç JSON "
                       "metni olarak döner (ok/command/exit_code/stdout/stderr/duration_ms).",
    "discovered_jc": "Standart komut satırı araçlarının ve sistem dosyalarının çıktılarını JSON formatına dönüştürmeyi sağlayan açık kaynaklı 'jc' aracının kaynak kodları. (otomatik entegre edildi)",
    "discovered_topydo": "Basit yapılacaklar listesi (todo) aracı (otomatik entegre edildi). "
                          "parameters: action=add|list|done|delete|prioritize|clear. "
                          "add: 'task' (veya 'content'/'text') zorunlu, opsiyonel 'priority' (A-Z). "
                          "done/delete/prioritize: 'task_id' (listedeki sıra no) zorunlu; prioritize ayrıca 'priority' ister. "
                          "list: opsiyonel 'filter' (arama kelimesi), 'all' (true ise tamamlananlar da gösterilir). "
                          "Örnek — 'sütü al'ı listeye eklemek için: action='add', task='sütü al'.",
    "file_controller":   "action: list|create_file|create_folder|delete|move|copy|find|disk_usage|info|extract|write|find_replace; path (ör. 'desktop'); name; content (create_file/write icin, TUM dosya icerigini bu yazar); old_text VE new_text (SADECE find_replace icin, ikisi de ZORUNLU - dosyadaki old_text'i new_text ile degistirir). Var olan bir dosyanin SADECE bir kismini degistirmek icin 'write' DEGIL 'find_replace' kullan (write butun dosyayi siler ve bastan yazar, find_replace sadece eslesen metni degistirir, cok daha guvenlidir).",
    "reminder":          "date: YYYY-MM-DD; time: HH:MM; message",
    "web_search":        "query: string; mode: 'search'|'news'|'research'|'price' (opsiyonel)",
    "weather_report":    "city: string",
    "system_status":     "parametre gerekmez - CPU/RAM/GPU/sicaklik bilgisi doner",
"system_scan_and_repair": "parametre gerekmez - projenin tum python dosyalarini gercekten import ederek calisma zamani hatalarini tarar, pyproject.toml bagimliliklarinin surum uyumunu kontrol eder ve eksik/uyumsuz olanlari onay istemeden otomatik kurar",
    "computer_settings": "action: volume/brightness/wifi gibi TEK bir OS ayari; description; value (opsiyonel). shutdown/restart/lock_screen YIKICI sayilir.",
    "send_message":      "receiver; message_text; platform (whatsapp/telegram/vb.) - kullanici adina disariya mesaj gittigi icin HER ZAMAN once onay ister.",
    "github_arama":      "query: string; min_stars (opsiyonel); max_results (opsiyonel) - GitHub'da salt-okunur depo arar, hicbir sey indirmez.",
    "github_arac_bul_ve_degerlendir": "query: string; min_stars (opsiyonel). GitHub'da Jarvis icin YENI bir arac arar, karantinada indirip analiz eder - Jarvis'in koduna hicbir sey YAZMAZ. 'ARAÇ' bulursa sonucta source_name doner; bir sonraki adimda entegrasyon_uygula(source_name=...) cagirilmali. Yeni aday kalmadiysa done=true say.",
    "entegrasyon_uygula": "SADECE github_arac_bul_ve_degerlendir bir 'ARAÇ' buldugunda, ondan donen source_name ile cagir (ör. source_name='kullanici/repo'). YIKICI sayilir - Jarvis'in gercek koduna yazar, her zaman once onay ister.",
    "discovery_register": "SADECE onay akisi tarafindan cagrilir - name, description, quarantine_path. Elle cagirma.",
}


# Yikici sayilan (action, tool) kombinasyonlari - agent_loop bunlari asla
# dogrudan calistirmaz, once kullaniciya sorar. file_controller icin liste
# TUTULMAZ: salt-okunur eylemler (file_controller.READONLY_ACTIONS) disindaki
# her eylem - takma adlar ve bilinmeyen adlar dahil - yikicidir.
_DESTRUCTIVE_SETTINGS_ACTIONS = {"shutdown", "restart", "lock_screen", "lock"}


def is_destructive(tool: str, parameters: dict) -> bool:
    """Bu adimin geri donusu zor/riskli olup olmadigini belirler. Emin
    olunamayan/beklenmedik bir durumda GUVENLI tarafta hata yapariz:
    True (onay iste) don."""
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()

    if tool == "file_controller":
        from jarvis.actions.file_controller import is_readonly_action
        return not is_readonly_action(action)
    if tool == "computer_settings":
        return action in _DESTRUCTIVE_SETTINGS_ACTIONS
    if tool == "send_message":
        # Kullanici adina disariya giden HER mesaj onay ister - bu,
        # sistem talimatindaki "Sending any message on the user's
        # behalf" kuraliyla birebir uyumlu.
        return True
    if tool == "discovery_register":
        # Discovery.py'nin bulup Gemini'nin "tool" dedigi bir seyi kalici
        # kayda gecirmek - internetten gelen bir seyi ilk defa Jarvis'in
        # hafizasina almak oldugu icin savunma amacli hep onay ister,
        # normalde zaten dogrudan awaiting_approval olarak olusturuluyor.
        return True
    if tool == "entegrasyon_uygula":
        # GitHub'da bulunan bir adayi Jarvis'in GERCEK koduna (actions/,
        # tools_kopru.py) yazar - discovery_register ile ayni sebeple
        # (internetten gelen bir seyi ilk defa calisir hale getirmek) HER
        # ZAMAN onay ister. github_arac_bul_ve_degerlendir'in KENDISI
        # yikici DEGILDIR (sadece izole karantinaya indirir/analiz eder) -
        # bu yuzden ayri, YIKICI bir ikinci arac olarak tutuluyor.
        return True
    if tool in {"backup_rollback", "vault_encrypt", "vault_decrypt"}:
        return True
    if tool in ALLOWED_TOOLS:
        return False
    # Taninmayan bir arac zaten call_tool() icinde NotAllowedTool ile
    # reddedilecek, ama guvenlik icin burada da varsayilan True donelim.
    return True


_APPROVED_TOOL: ContextVar[str | None] = ContextVar("jarvis_approved_tool", default=None)


def call_approved_tool(tool: str, parameters: dict) -> str:
    """Execute a destructive tool only inside the explicit approval path."""
    if not is_destructive(tool, parameters or {}):
        return call_tool(tool, parameters)
    token = _APPROVED_TOOL.set(tool)
    try:
        return call_tool(tool, parameters)
    finally:
        _APPROVED_TOOL.reset(token)


def call_tool(tool: str, parameters: dict) -> str:
    """Verilen aracin GERCEK, mevcut Python fonksiyonunu cagirir. Tanimadigi
    bir arac icin ASLA rastgele kod calistirmaz - NotAllowedTool firlatir."""
    fn = ALLOWED_TOOLS.get(tool)
    if fn is None:
        raise NotAllowedTool(tool)
    if is_destructive(tool, parameters or {}) and _APPROVED_TOOL.get() != tool:
        raise PermissionError(f"Araç açık kullanıcı onayı olmadan çalıştırılamaz: {tool}")
    return fn(parameters or {})
