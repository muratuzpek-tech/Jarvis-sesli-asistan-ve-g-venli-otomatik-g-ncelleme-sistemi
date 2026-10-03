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
