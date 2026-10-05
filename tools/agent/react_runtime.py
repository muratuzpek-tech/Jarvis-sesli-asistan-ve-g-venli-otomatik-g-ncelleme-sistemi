"""
tools/agent/react_runtime.py — Jarvis 2.0 ReAct Agent Runtime

BORAN-SERT'TEN ALINAN (Brain/graph_nodes.py):
  - ReAct Pattern: Think -> Action -> Observation -> Finish
  - finish_task sentinel -> dongu otomatik biter
  - System prompt -> dogal Turkce, tool kullanimi talimatlari

SENIN SISTEME ENTEGRE:
  - tools/registry -> tum araclara erisim (30+ tool!)
  - memory/recall -> gecmis konusmalardan baglam getirme
  - sentiment -> duygu durumuna gore tepki tonu
  - Guvenlik katmani -> her tool security.check()'den gecer
  - Max 10 tur -> sonsuz dongu korumasi

AKIS:
    USER: "Projenin yapisini analiz et, README yaz ve GitHub'a yukle"
      ↓
    THINK:  "Proje yapisini incelemeliyim"
    ACT:    tool="file_controller", args={"action":"tree"}
    OBSERVE: "src/, tools/, memory/ klasorleri var..."
    THINK:  "README yazmam lazim"
    ACT:    tool="code_helper", args={"action":"write",...}
    OBSERVE: "README.md yazildi"
    THINK:  "GitHub'a yuklemem lazim"
    ACT:    tool="terminal", args={"command":"git push"}
    OBSERVE: "Push successful"
    FINISH: "Proje analiz edildi, README yazildi, GitHub'a yuklendi."
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from collections.abc import Callable

from tools.registry import registry, ToolContext

logger = logging.getLogger("tools.agent.react")

# LRU Cache for identical queries
_react_cache = {}
_REACT_CACHE_MAX = 50

MAX_REACT_TURNS = 10
_FINISH_TOOL = "finish_task"


@dataclass
class ReActStep:
    """Tek bir ReAct adimi (Thought + Action + Observation)."""
    turn: int
    thought: str = ""
    tool: str = ""
    args: dict = field(default_factory=dict)
    observation: str = ""
    finished: bool = False
    elapsed_ms: int = 0


@dataclass
class ReActResult:
    """ReAct dongusunun final sonucu."""
    goal: str = ""
    steps: list[ReActStep] = field(default_factory=list)
    final_response: str = ""
    success: bool = False
    total_turns: int = 0
    total_ms: int = 0

    def summary(self) -> str:
        lines = [
            f"[ReAct] {self.goal}",
            f"[ReAct] {self.total_turns} tur, {self.total_ms/1000:.1f}s, "
            f"{'BASARILI' if self.success else 'SINIRDI'}",
            "",
        ]
        for s in self.steps:
            icon = "+" if s.observation and "HATA" not in s.observation[:20] else "-"
            lines.append(f"  {s.turn}. {s.thought[:60]}")
            if s.tool:
                lines.append(f"     -> {s.tool}({list(s.args.keys())}) [{icon}] {s.observation[:80]}")
        lines.append("")
        lines.append(f"SONUC: {self.final_response}")
        return "\n".join(lines)


_SYSTEM_PROMPT = """Sen Jarvis'sin -- yapay zeka asistani.

ReAct protokolu ile calisiyorsun:
1. THINK: Ne yapacagini dusun
2. ACTION: Bir arac (tool) cagir
3. OBSERVE: Sonucu degerlendir
4. Tekrarla veya FINISH ile bitir

KURALLAR:
- Cevaplarin KUSURSUZ, DOGAL TURKCE olmali.
- Araclari cagirirken GERKLI TUM PARAMETRELERI eksiksiz doldur.





- DUSUN, cozum uret, SONRA finish_task cagir.
- Basit sorular (matematik, bilgi, tarih) → dogrudan finish_task ile cevapla.
- Token tozu uretme: 1-2 adimda coz, zaman harcama.

MEVCUT ARACLARIN:
{TOKEN_TOOLS}

CIKTI FORMATI (SADECE JSON):
{
  "thought": "Ne dusunuyorum ve neden",
  "tool": "tool_adi veya finish_task",
  "args": {"param": "deger"},
  "response": "Sadece finish_task ise final ozet mesaji"
}
"""


def _build_system_prompt() -> str:
    # ReAct ic toolbox: SADECE finish_task.
    # External tool'lar (agentic_code, spotify_control vb.)
    # Jarvis ana dispatch calistirir, ReAct degil.
    tool_lines = [
        f"- {_FINISH_TOOL}: Gorevi bitir, cozumu ve ozeti sun",
    ]
    return _SYSTEM_PROMPT.replace("{TOKEN_TOOLS}", chr(10).join(tool_lines))


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL)
    for block in blocks:
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return {}


class ReactAgent:
    """ReAct (Reasoning + Acting) Agent Runtime."""

    def __init__(
        self,
        model_fn: Callable[[str], str] | None = None,
        ctx: ToolContext | None = None,
        max_turns: int = MAX_REACT_TURNS,
    ):
        self._model_fn = model_fn or self._default_model
        self._ctx = ctx or ToolContext()
        self._max = max_turns

    @staticmethod
    def _default_model(prompt: str) -> str:
        try:
            from google import genai
            _api_key = os.environ.get("GEMINI_API_KEY", "")
            if not _api_key:
                raise ValueError("GEMINI_API_KEY bos")
            client = genai.Client(api_key=_api_key)
            resp = client.models.generate_content(
                model="gemini-2.0-flash", contents=prompt,
            )
            return resp.text or ""
        except Exception:
            pass
        try:
            import ollama
            # Dinamik model secimi: yuklu modellerden en iyisini bul
            _best_model = "qwen2.5:7b"  # default: surekli yuklu olan
            try:
                _models = ollama.list()
                _names = [m.get("model", m.get("name", "")) for m in _models.get("models", [])]
                if _names:
                    # Oncelik: coder > 7b > ilk model
                    for pref in ["qwen2.5-coder:7b", "qwen2.5:7b", "qwen2.5-coder:14b"]:
                        if pref in _names:
                            _best_model = pref
                            break
                    else:
                        _best_model = _names[0]
            except Exception:
                pass  # default model kullan
            resp = ollama.chat(
                model=_best_model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                options={"temperature": 0.3},
            )
            return resp.get("message", {}).get("content", "")
        except Exception:
            return json.dumps({
                "thought": "LLM yok",
                "tool": "finish_task",
                "args": {},
                "response": "Kullanilabilir LLM bulunamadi."
            })

    async def solve(self, goal: str) -> str:
        t0 = time.monotonic()
        result = ReActResult(goal=goal)
        system_prompt = _build_system_prompt()
        history: list[dict] = []

        memory_ctx = ""
        try:
            from memory import memory as mem
            memories = await mem.recall(goal, top_k=3)
            memory_ctx = mem.format_for_prompt(memories)
        except Exception:
            pass

        sentiment_hint = ""
        try:
            from tools.media.sentiment import analyze_sentiment
            sent = analyze_sentiment(goal)
            sentiment_hint = (
                f"\n[KULLANICI RUH HALI: {sent.mood} ({sent.label}, {sent.score:+.2f})]"
                f"\nTepki tonunu buna gore ayarla."
            )
        except Exception:
            pass

        for turn in range(1, self._max + 1):
            step_start = time.monotonic()
            step = ReActStep(turn=turn)

            prompt_parts = [system_prompt]
            if memory_ctx:
                prompt_parts.append(f"\n{memory_ctx}")
            if sentiment_hint:
                prompt_parts.append(sentiment_hint)

            prompt_parts.append(f"\n## GOREV\n{goal}")

            if history:
                prompt_parts.append("\n## GECMIS (son 6 adim):")
                for h in history[-6:]:
                    prompt_parts.append(json.dumps(h, ensure_ascii=False))

            prompt_parts.append(
                f"\n## TUR {turn}/{self._max}"
                f"\nSimdi THINK -> TOOL -> ARGS yap (JSON)."
            )

            raw = self._model_fn("\n".join(prompt_parts))
            decision = _parse_json_response(raw)

            if not decision:
                step.thought = "JSON parse basarisiz"
                step.observation = "Format hatasi"
                result.steps.append(step)
                break

            step.thought = decision.get("thought", "")
            tool_name = decision.get("tool", _FINISH_TOOL)
            step.args = decision.get("args", {}) or {}
            response = decision.get("response", "")

            if tool_name == _FINISH_TOOL:
                step.finished = True
                step.tool = _FINISH_TOOL
                step.observation = response[:100] or "Gorev tamamlandi"
                result.steps.append(step)
                result.final_response = response or "Gorev tamamlandi."
                result.success = True
                break

            step.tool = tool_name
            history.append({
                "turn": turn,
                "thought": step.thought,
                "tool": tool_name,
                "args": {k: str(v)[:60] for k, v in step.args.items()},
            })

            try:
                obs = await self._gated_execute(tool_name, step.args)
                step.observation = obs[:500]
            except asyncio.CancelledError:
                raise
            except Exception as e:
                step.observation = f"TOOL ERROR: {type(e).__name__}: {str(e)[:150]}"

            step.elapsed_ms = int((time.monotonic() - step_start) * 1000)
            result.steps.append(step)
            history.append({
                "turn": turn,
                "observation": step.observation[:300],
            })

        result.total_turns = len(result.steps)
        result.total_ms = int((time.monotonic() - t0) * 1000)
        if not result.success:
            result.final_response = (
                f"Maksimum {self._max} tur tamamlandi. "
                f"{result.total_turns} adim atildi. finish_task cagirilmadi."
            )

        summary = result.summary()
        self._ui_log(summary[:800])
        return summary

    async def _gated_execute(self, tool_name: str, args: dict) -> str:
        """İç LLM'in seçtiği aracı tek güvenlik kapısından geçirir (E3).

        ReAct iç turunda gerçek bir kullanıcı turu olamaz: onay gerektiren bir
        araç burada ÇALIŞMAZ (gözlem olarak kodsuz onay metni döner) ve dış
        çağrının onayı iç çağrılara MİRAS KALMAZ - eskiden
        ctx.dangerous_confirmed aynen geçiyordu ve DESTRUCTIVE bir araç iç
        döngüde onaysız çalışabiliyordu. Kayıtsız araç reddedilir; modelin
        confirm_code'u araca gitmez. Her karar denetim kaydına yazılır."""
        from jarvis import security_gate as gate
        decision = gate.authorize(tool_name, args, gate.Source.REACT)
        if decision.verdict is not gate.Verdict.ALLOW:
            gate.audit(decision, decision.model_message, executed=False)
            if decision.verdict is gate.Verdict.DENY:
                return decision.model_message
            return (f"{decision.model_message} (ReAct iç turunda kullanıcı onayı "
                    f"alınamaz; araç ÇALIŞTIRILMADI. finish_task ile kullanıcıya bildir.)")
        params = gate.prepare(decision, None)
        inner_ctx = replace(self._ctx, dangerous_confirmed=False)
        obs = await registry.execute(tool_name, params, ctx=inner_ctx)
        gate.audit(decision, obs, executed=True)
        return obs

    def _ui_log(self, msg: str) -> None:
        logger.info(f"[ReAct] {msg}")
        ui = self._ctx.ui
        if ui:
            try:
                ui.write_log(f"[ReAct] {msg[:200]}")
            except Exception:
                pass


async def react_solve(
    goal: str,
    ctx: ToolContext | None = None,
    model_fn: Callable[[str], str] | None = None,
    max_turns: int = MAX_REACT_TURNS,
) -> str:
    cache_key = goal.strip().lower()[:200]
    if cache_key in _react_cache:
        return _react_cache[cache_key]
    agent = ReactAgent(model_fn=model_fn, ctx=ctx, max_turns=max_turns)
    result_str = await agent.solve(goal)
    if len(_react_cache) >= _REACT_CACHE_MAX:
        oldest = next(iter(_react_cache))
        del _react_cache[oldest]
    _react_cache[cache_key] = result_str
    return result_str
