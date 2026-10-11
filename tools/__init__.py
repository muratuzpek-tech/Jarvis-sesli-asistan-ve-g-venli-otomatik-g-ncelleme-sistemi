"""
Jarvis 2.0 — Merkezi Tool Altyapısı

Mimari:
    Agent (Gemini/ReAct)
      ↓
    registry.execute(name, args)
      ↓
    security.check(name, args)    → ONAY / RED
      ↓
    tool.handler(args, ctx)       → Sonuç
      ↓
    Agent (sonucu işler)

Kullanım:
    from tools.registry import registry
    
    # Kayıt
    @registry.register(
        name="my_tool",
        description="Does something cool",
        parameters={...},  # Gemini schema
        security="normal",  # normal | dangerous | destructive
        handler=my_function,
    )
    ...
    
    # Çalıştırma
    result = await registry.execute("my_tool", {"arg1": "val"}, ctx=ctx)
"""

from tools.registry import registry, ToolContext, register
from tools.security import security, SecurityLevel
from tools.schemas import get_all_schemas, generate_declarations

# Alt paketleri yukle ki @registry.register dekoratorleri calissin
import tools.agent       # noqa: F401  → "react_agent" kayit
import tools.developer   # noqa: F401  → "agentic_code" kayit
import tools.media       # noqa: F401  → "spotify_control" kayit
import tools.bridge_actions   # noqa: F401  → web_search, file_controller, etc.

__all__ = [
    "registry", "register", "ToolContext",
    "security", "SecurityLevel",
    "get_all_schemas", "generate_declarations",
]

# ── Smoke Test (python3 -c "import tools; print(tools.registry.stats())") ──
if __name__ == "__main__":
    import json
    print(json.dumps(registry.stats(), indent=2, default=str))
    print(f"\nSchemas generated: {len(generate_declarations())} tools")
