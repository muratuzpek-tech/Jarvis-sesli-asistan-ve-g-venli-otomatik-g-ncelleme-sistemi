"""
bridge_actions.py — Eski sistemin araçlarını tools.registry'ye köprüler.

tools_kopru.py'deki ALLOWED_TOOLS listesindeki mevcut araçları,
ReAct Agent'in görebilmesi için registry'e kaydeder.
YENI yetenek EKLEMEZ — sadece var olan fonksiyonlari wrap eder.
"""
from tools.registry import registry, ToolContext


@registry.register(
    name="web_search",
    description="Internette arama yap. Gercek web sonuclari doner.",
    parameters={
        "type": "OBJECT",
        "properties": {"query": {"type": "STRING", "description": "Arama sorgusu"}},
        "required": ["query"],
    },
    security="read_only",
    category="web",
)
async def _bridge_web_search(args: dict, ctx: ToolContext) -> str:
    from jarvis.actions.web_search import web_search
    return web_search(parameters=args) or "Sonuc bulunamadi."


@registry.register(
    name="file_controller",
    description="Dosya islemleri: olustur, oku, listele, sil, kopyala, tasi.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "create_file|read|list|delete|copy|move"},
            "path": {"type": "STRING", "description": "Dosya yolu"},
            "name": {"type": "STRING", "description": "Dosya adi (create icin)"},
            "content": {"type": "STRING", "description": "Icerik (create icin)"},
        },
        "required": ["action", "path"],
    },
    security="dangerous",
    category="filesystem",
)
async def _bridge_file_controller(args: dict, ctx: ToolContext) -> str:
    from jarvis.actions.file_controller import file_controller
    return file_controller(parameters=args)


@registry.register(
    name="weather_report",
    description="Hava durumu raporu al.",
    parameters={
        "type": "OBJECT",
        "properties": {"city": {"type": "STRING", "description": "Sehir adi"}},
    },
    security="read_only",
    category="web",
)
async def _bridge_weather(args: dict, ctx: ToolContext) -> str:
    from jarvis.actions.weather_report import weather_action
    return weather_action(parameters=args) or "Hava durumu alinamadi."


@registry.register(
    name="reminder",
    description="Hatirlatici kur veya listele.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "add|list|remove"},
            "text": {"type": "STRING", "description": "Hatirlatici metni"},
            "time": {"type": "STRING", "description": "Zaman (HH:MM)"},
        },
        "required": ["action"],
    },
    security="normal",
    category="productivity",
)
async def _bridge_reminder(args: dict, ctx: ToolContext) -> str:
    from jarvis.actions.reminder import reminder
    return reminder(parameters=args) or "Done."


@registry.register(
    name="system_status",
    description="Sistem durumunu goster (CPU, RAM, disk, pil).",
    parameters={"type": "OBJECT", "properties": {}},
    security="read_only",
    category="computer",
)
async def _bridge_system_status(args: dict, ctx: ToolContext) -> str:
    from jarvis.actions.system_monitor import get_system_status
    status = get_system_status()
    return "\n".join(f"{k}: {v}" for k, v in status.items())
