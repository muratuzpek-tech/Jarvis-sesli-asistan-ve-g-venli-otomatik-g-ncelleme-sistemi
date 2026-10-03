"""
agent — Jarvis 2.0 ReAct Agent Runtime
"""
from tools.agent.react_runtime import ReactAgent, react_solve

__all__ = ["ReactAgent", "react_solve"]

from tools.registry import registry, ToolContext

@registry.register(
    name="react_agent",
    description="ReAct agent: Think→Act→Observe→Finish döngüsü. "
                "Çok adımlı karmaşık görevler için (analiz et, yaz, yükle vb.). "
                "Tüm araçları otomatik kullanır. Max 10 tur.",
    parameters={
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "Görev hedefi (ör: 'Proje yapısını analiz et ve README yaz')"
            },
        },
        "required": ["goal"],
    },
    security="normal",
    category="agent",
)
async def _react_handler(args: dict, ctx: ToolContext) -> str:
    from tools.agent.react_runtime import react_solve
    raw = args.get("goal", None)
    if raw is None:
        raw = args.get("task", None)
    if raw is None:
        return "HATA: Gorev hedefi bos. goal parametresi gerekli."
    if not isinstance(raw, str):
        return "HATA: goal metin olmali. Gelen tip: " + type(raw).__name__
    goal = raw.strip()
    if not goal or len(goal) < 2:
        return "HATA: Gorev hedefi cok kisa."
    if len(goal) > 50000:
        goal = goal[:50000]
    return await react_solve(goal=goal, ctx=ctx)