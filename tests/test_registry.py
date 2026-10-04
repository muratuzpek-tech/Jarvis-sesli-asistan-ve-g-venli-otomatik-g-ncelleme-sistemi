"""tests/test_registry.py — Registry regression tests"""
import sys
import asyncio
sys.path.insert(0, '.')

from tools.registry import registry, register


def test_none_args():
    r = asyncio.run(registry.execute("fake_tool", None))
    assert isinstance(r, str)

def test_non_dict_args():
    r = asyncio.run(registry.execute("fake_tool", "not_a_dict"))
    assert "Invalid" in r

def test_unknown_tool():
    r = asyncio.run(registry.execute("no_such_tool", {}))
    assert "Unknown" in r

def test_duplicate_registration_warns():
    @register(name="_pytest_dup", description="a", parameters={}, security="normal", category="test")
    def _a(args, ctx): return "a"
    @register(name="_pytest_dup", description="b", parameters={}, security="normal", category="test")
    def _b(args, ctx): return "b"
    entry = registry.get("_pytest_dup")
    assert entry.handler.__qualname__.endswith("_b")

def test_disable_enable():
    @register(name="_pytest_toggle", description="t", parameters={}, security="normal", category="test")
    def _t(args, ctx): return "on"
    registry.disable("_pytest_toggle")
    r = asyncio.run(registry.execute("_pytest_toggle", {}))
    assert "disabled" in r.lower()
    registry.enable("_pytest_toggle")
    r = asyncio.run(registry.execute("_pytest_toggle", {}))
    assert r == "on"
