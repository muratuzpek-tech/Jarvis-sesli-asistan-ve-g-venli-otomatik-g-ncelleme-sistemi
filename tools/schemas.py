"""
tools/schemas.py — Gemini Function Declarations Merkezi

Her tool'un Gemini'ye tanıtım şeması BURADA saklanır.
registry.register() ile birleşik çalışır.

Çıktı:
    generate_declarations() → Gemini LiveConnectConfig.function_declarations
    get_all_schemas()       → dict[str, ToolSchema] (debug/test için)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tools.security import SecurityLevel


@dataclass
class ToolSchema:
    """Tek bir tool'un Gemini'ye tanıtım şeması."""
    name: str
    description: str
    parameters: dict[str, Any]
    security: SecurityLevel = SecurityLevel.NORMAL
    category: str = "general"

    def to_declaration(self) -> dict[str, Any]:
        """Gemini function_declarations formatına dönüştür."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


# ── Registry ──────────────────────────────────────────────────
_schemas: dict[str, ToolSchema] = {}


def register_schema(schema: ToolSchema) -> None:
    """Şemayı kaydet (registry.register() otomatik çağırır)."""
    _schemas[schema.name] = schema


def get_all_schemas() -> dict[str, ToolSchema]:
    """Tüm kayıtlı şemaları döndür."""
    return dict(_schemas)


def generate_declarations() -> list[dict[str, Any]]:
    """Gemini LiveConnectConfig.function_declarations formatında döndür."""
    return [s.to_declaration() for s in _schemas.values()]


def get_by_category(category: str) -> list[ToolSchema]:
    """Belirli kategorideki tool'ları döndür."""
    return [s for s in _schemas.values() if s.category == category]
