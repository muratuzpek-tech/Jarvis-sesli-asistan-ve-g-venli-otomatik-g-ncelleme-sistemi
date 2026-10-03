"""
developer — Jarvis 2.0 geliştirici araçları
"""
from tools.developer.agentic_coder import AgenticCoder, agentic_solve

__all__ = ["AgenticCoder", "agentic_solve"]

from tools.registry import registry, ToolContext

@registry.register(
    name="agentic_code",
    description="Agentic coding: iteratif yaz→çalıştır→test et→düzelt döngüsü. "
                "Tam çalışan kod üretir (TODO/pass/stub YASAK). "
                "Max 15 iterasyon. Multi-file proje destekler.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "description": {"type": "STRING", "description": "Ne yapılacağı (ör: 'Hesap makinesi yaz')"},
            "language": {"type": "STRING", "description": "Programlama dili (default: python)"},
            "project_path": {"type": "STRING", "description": "Hedef dizin (default: ~/Desktop/jarvis_code)"},
            "target_filename": {"type": "STRING", "description": "Ana dosya adı (opsiyonel)"},
        },
        "required": ["description"],
    },
    security="normal",
    category="developer",
)
async def _agentic_code_handler(args: dict, ctx: ToolContext) -> str:
    return await agentic_solve(
        description=args.get("description", ""),
        language=args.get("language", "python"),
        project_path=args.get("project_path"),
        target_filename=args.get("target_filename"),
        ui=ctx.ui,
    )
