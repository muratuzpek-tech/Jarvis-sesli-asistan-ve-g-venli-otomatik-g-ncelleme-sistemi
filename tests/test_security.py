"""tests/test_security.py — Security regression tests"""
import sys
sys.path.insert(0, '.')
from tools.security import security, SecurityLevel


def test_destructive_no_approval():
    v = security.check("t", {"cmd": "rm"}, level=SecurityLevel.DESTRUCTIVE, user_confirmed=False)
    assert not v.allowed and v.requires_confirmation

def test_destructive_with_approval():
    v = security.check("t2", {"cmd": "safe"}, level=SecurityLevel.DESTRUCTIVE, user_confirmed=True)
    assert v.allowed

def test_path_traversal():
    v = security.check("f", {"path": "../../../etc/passwd"}, level=SecurityLevel.NORMAL)
    assert not v.allowed

def test_null_byte():
    v = security.check("f", {"name": "test\x00.exe"}, level=SecurityLevel.NORMAL)
    assert not v.allowed

def test_shell_injection():
    v = security.check("t", {"cmd": "ls && rm -rf /"}, level=SecurityLevel.NORMAL)
    assert not v.allowed

def test_safe_passes():
    v = security.check("t", {"text": "hello"}, level=SecurityLevel.READ_ONLY)
    assert v.allowed
