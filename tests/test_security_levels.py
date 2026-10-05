"""tests/test_security_levels.py — guvenlik seviyesi regresyon testleri

Iki acigi kapatiyor:

1. DANGEROUS seviyesi allowed=True donuyordu. registry.execute her iki
   kontrolunde de `not verdict.allowed` aradigi icin requires_confirmation
   sessizce dusuyor ve arac onaysiz calisiyordu.
2. register(security="destuctive") gibi bir yazim hatasi sessizce NORMAL
   kabul ediliyordu, yani destructive bir arac onaysiz kaliyordu.
"""
import sys
import asyncio

import pytest

sys.path.insert(0, '.')

from tools.registry import registry, register, ToolContext
from tools.security import SecurityLevel, security


# ── 1. DANGEROUS onay olmadan gecmemeli ───────────────────────────────

def test_dangerous_without_confirmation_is_not_allowed():
    verdict = security.check(
        "_pytest_danger_check", {}, level=SecurityLevel.DANGEROUS, user_confirmed=False
    )
    assert verdict.allowed is False
    assert verdict.requires_confirmation is True


def test_dangerous_with_confirmation_is_allowed():
    verdict = security.check(
        "_pytest_danger_ok", {}, level=SecurityLevel.DANGEROUS, user_confirmed=True
    )
    assert verdict.allowed is True


def test_destructive_without_confirmation_is_not_allowed():
    verdict = security.check(
        "_pytest_destroy_check", {}, level=SecurityLevel.DESTRUCTIVE, user_confirmed=False
    )
    assert verdict.allowed is False
    assert verdict.requires_confirmation is True


def test_normal_level_passes():
    verdict = security.check("_pytest_normal_check", {}, level=SecurityLevel.NORMAL)
    assert verdict.allowed is True
    assert verdict.requires_confirmation is False


# ── 2. registry.execute dangerous araci onaysiz calistirmamali ────────

def test_registry_blocks_dangerous_tool_without_confirmation():
    ran = []

    @register(
        name="_pytest_dangerous_tool",
        description="d",
        parameters={},
        security="dangerous",
        category="test",
    )
    def _d(args, ctx):
        ran.append(True)
        return "calisti"

    result = asyncio.run(registry.execute("_pytest_dangerous_tool", {}))

    assert result.startswith("CONFIRMATION_REQUIRED:"), result
    assert not ran, "dangerous arac onay alinmadan calisti"


def test_registry_runs_dangerous_tool_after_confirmation():
    ran = []

    @register(
        name="_pytest_dangerous_tool_ok",
        description="d",
        parameters={},
        security="dangerous",
        category="test",
    )
    def _d(args, ctx):
        ran.append(True)
        return "calisti"

    ctx = ToolContext(dangerous_confirmed=True)
    result = asyncio.run(registry.execute("_pytest_dangerous_tool_ok", {}, ctx=ctx))

    assert result == "calisti"
    assert ran


# ── 3. Taninmayan seviye adi sessizce NORMAL olmamali ─────────────────

def test_unknown_security_level_raises():
    with pytest.raises(ValueError) as exc:
        @register(
            name="_pytest_typo_tool",
            description="t",
            parameters={},
            security="destuctive",   # kasitli yazim hatasi
            category="test",
        )
        def _t(args, ctx):
            return "x"

    assert "destuctive" in str(exc.value)
    assert registry.get("_pytest_typo_tool") is None


@pytest.mark.parametrize(
    "name,level",
    [
        ("read_only", SecurityLevel.READ_ONLY),
        ("normal", SecurityLevel.NORMAL),
        ("dangerous", SecurityLevel.DANGEROUS),
        ("destructive", SecurityLevel.DESTRUCTIVE),
        ("DESTRUCTIVE", SecurityLevel.DESTRUCTIVE),   # buyuk harf de gecerli
    ],
)
def test_known_security_levels_still_accepted(name, level):
    tool = f"_pytest_level_{name.lower()}"

    @register(
        name=tool,
        description="l",
        parameters={},
        security=name,
        category="test",
    )
    def _l(args, ctx):
        return "x"

    assert registry.get(tool).security is level
