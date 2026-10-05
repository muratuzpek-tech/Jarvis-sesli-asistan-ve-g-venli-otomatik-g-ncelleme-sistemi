"""
tools/registry.py — Jarvis 2.0 Merkezi Tool Registry & Dispatcher

main.py'deki 250+ satırlık if/elif zincirinin YERİNE geçer.

Kullanım (YENİ tool ekleme = 3 satır):

    from tools.registry import registry, register

    @register(
        name="spotify_control",
        description="Control Spotify playback",
        parameters={"type": "OBJECT", "properties": {...}},
        security="normal",
        category="media",
        async_handler=True,
    )
    async def spotify_handler(args: dict, ctx: ToolContext) -> str:
        ...

    # main.py'de:
    result = await registry.execute(name, args, ctx=ctx)
"""

from __future__ import annotations

import asyncio
import functools
import logging
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from tools.security import SecurityLevel, security as security_manager
from tools.schemas import ToolSchema, register_schema

logger = logging.getLogger("tools.registry")


@dataclass
class ToolContext:
    """
    Her tool çağrısına eklenen bağlam nesnesi.
    Handler fonksiyonları bunu alır ve UI erişimi vb. için kullanır.
    """
    ui: Any = None                           # JarvisUI referansı
    speak: Callable[[str], None] | None = None  # TTS söyleme
    session: Any = None                      # Gemini Live session
    loop: Any = None                         # asyncio event loop
    dangerous_confirmed: bool = False         # Kullanıcı onayı verildi mi?
    pending_action: str | None = None         # Bekleyen tehlikeli aksiyon
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def player(self):
        """Backward compat: eski kodlarda player= olarak kullanılır."""
        return self.ui


@dataclass
class ToolEntry:
    """Kayıtlı tek bir tool."""
    name: str
    description: str
    parameters: dict[str, Any]
    security: SecurityLevel
    category: str
    handler: Callable[..., Any]
    is_async: bool
    requires_context: bool
    after_execute: Callable | None = None     # Post-processing (mirror to UI vb.)
    enabled: bool = True


class ToolRegistry:
    """
    Merkezi Tool Kayıt ve Dağıtım Merkezi.
    
    Flow:
        Gemini → function_call(name, args)
          → registry.execute(name, args, ctx)
            → security_manager.check(name, args)
            → entry.handler(args, ctx)
            → entry.after_execute(result, ctx)  ← optional
            → return result string
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        security: SecurityLevel | str = SecurityLevel.NORMAL,
        category: str = "general",
        handler: Callable[..., Any] | None = None,
        async_handler: bool = False,
        requires_context: bool = True,
        after_execute: Callable | None = None,
    ) -> Callable:
        """
        Tool kaydet. Decorator veya fonksiyonel olarak kullanılabilir.
        
        Args:
            name:              Gemini function_call'daki name ile aynı olmalı
            description:       Gemini'ye gösterilen açıklama
            parameters:        Gemini schema dict (type: OBJECT, properties: {...})
            security:          "read_only" | "normal" | "dangerous" | "destructive"
            category:          Gruplama: computer, filesystem, web, media, vb.
            handler:           Fonksiyon ya da decorator callable
            async_handler:     Handler async mi? (True → await ile çalışır)
            requires_context:  Handler ToolContext alıyor mu?
            after_execute:     Post-processing callback (result, ctx) → result
        """
        # String security → enum. Taninmayan bir ad sessizce NORMAL'a
        # dusuyordu: "destuctive" gibi bir yazim hatasi, destructive bir
        # araci onaysiz birakiyordu. Artik kayit sirasinda patlar.
        if isinstance(security, str):
            sec_map = {
                "read_only": SecurityLevel.READ_ONLY,
                "normal": SecurityLevel.NORMAL,
                "dangerous": SecurityLevel.DANGEROUS,
                "destructive": SecurityLevel.DESTRUCTIVE,
            }
            key = security.lower()
            if key not in sec_map:
                raise ValueError(
                    f"Bilinmeyen guvenlik seviyesi {security!r} (tool: {name}). "
                    f"Gecerli degerler: {', '.join(sorted(sec_map))}"
                )
            security = sec_map[key]

        def _add(fn: Callable) -> Callable:
            # HIGH FIX: Duplicate registration warn
            if name in self._tools:
                logger.warning(
                    f"[Registry] ⚠️ DUPLICATE: '{name}' yeniden kaydediliyor "
                    f"(eski: {self._tools[name].handler.__module__}.{self._tools[name].handler.__qualname__} → "
                    f"yeni: {fn.__module__}.{fn.__qualname__})"
                )
            self._tools[name] = ToolEntry(
                name=name,
                description=description,
                parameters=parameters,
                security=security,
                category=category,
                handler=fn,
                is_async=async_handler or asyncio.iscoroutinefunction(fn),
                requires_context=requires_context,
                after_execute=after_execute,
            )
            # Şemayı da kaydet
            register_schema(ToolSchema(
                name=name,
                description=description,
                parameters=parameters,
                security=security,
                category=category,
            ))
            logger.info(f"[Registry] ✅ {name} ({category}/{security.name})")
            return fn

        if handler is not None:
            _add(handler)
            return handler
        return _add

    async def execute(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ToolContext | None = None,
    ) -> str:
        """
        Tool'u güvenlik kontrolüyle çalıştır.
        
        Returns:
            str: Sonuç mesajı (her zaman string)
        """
        # CRITICAL FIX: None args guard
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return f"Invalid arguments: expected dict, got {type(args).__name__}"

        # HIGH FIX: Type coercion — tüm string parametreleri zorla str
        args = {k: str(v) if isinstance(v, (int, float)) else v for k, v in args.items()}

        entry = self._tools.get(name)
        if not entry:
            return f"Unknown tool: '{name}'"
        if not entry.enabled:
            return f"Tool '{name}' is currently disabled."

        # Güvenlik kontrolü
        user_confirmed = bool(ctx and ctx.dangerous_confirmed)
        verdict = security_manager.check(
            name, args,
            level=entry.security,
            user_confirmed=user_confirmed,
        )

        if verdict.requires_confirmation and not verdict.allowed:
            return f"CONFIRMATION_REQUIRED:{name}:{verdict.confirm_prompt}"

        if not verdict.allowed:
            return f"BLOCKED: {verdict.reason}"

        try:
            # Handler çağrısı
            if entry.requires_context:
                if entry.is_async:
                    result = await entry.handler(args, ctx or ToolContext())
                else:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        functools.partial(entry.handler, args, ctx or ToolContext()),
                    )
            else:
                if entry.is_async:
                    result = await entry.handler(args)
                else:
                    result = await asyncio.get_event_loop().run_in_executor(
                        None,
                        functools.partial(entry.handler, args),
                    )

            # Post-processing
            if entry.after_execute and result:
                result = entry.after_execute(result, ctx or ToolContext(), args)

            return result if isinstance(result, str) else (str(result) if result else "Done.")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Registry] ❌ {name}: {type(e).__name__}: {e}", exc_info=True)
            return f"Tool error ({name}): {type(e).__name__}: {str(e)[:120]}"

    # ── Introspection ─────────────────────────────────────────

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def list_tools(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_by_category(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for entry in self._tools.values():
            groups.setdefault(entry.category, []).append(entry.name)
        return {k: sorted(v) for k, v in sorted(groups.items())}

    def stats(self) -> dict[str, Any]:
        return {
            "total": len(self._tools),
            "enabled": sum(1 for t in self._tools.values() if t.enabled),
            "by_security": {
                lvl.name: sum(1 for t in self._tools.values() if t.security == lvl)
                for lvl in SecurityLevel
            },
            "by_category": self.list_by_category(),
        }

    def disable(self, name: str) -> bool:
        entry = self._tools.get(name)
        if entry:
            entry.enabled = False
            return True
        return False

    def enable(self, name: str) -> bool:
        entry = self._tools.get(name)
        if entry:
            entry.enabled = True
            return True
        return False


# ── Singleton ─────────────────────────────────────────────────
registry = ToolRegistry()

# Kısa alias
register = registry.register
