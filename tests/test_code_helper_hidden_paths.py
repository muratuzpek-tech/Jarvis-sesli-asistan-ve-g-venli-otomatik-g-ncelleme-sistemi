"""tests/test_code_helper_hidden_paths.py

resolve_write_target() ev dizini sinirini uyguluyordu ama ~/.bashrc,
~/.ssh/authorized_keys ve ~/.claude/settings.json da ev dizinindedir.
output_path model ciktisi oldugu icin gizli yollar kod yazma hedefi
olamaz; .bashrc'ye yazmak her kabuk acilisinda kod calistirmak demektir.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, 'src')

from jarvis.actions.code_helper import (
    UnsafeWriteTarget,
    resolve_write_target,
)

HOME = Path.home()


# ── Gizli hedefler reddedilmeli ───────────────────────────────────────

@pytest.mark.parametrize(
    "hedef",
    [
        HOME / ".bashrc",
        HOME / ".profile",
        HOME / ".ssh" / "authorized_keys",
        HOME / ".claude" / "settings.json",
        HOME / ".config" / "autostart" / "x.desktop",
        HOME / "projem" / ".git" / "hooks" / "pre-commit",
    ],
)
def test_hidden_targets_rejected(hedef):
    with pytest.raises(UnsafeWriteTarget):
        resolve_write_target(str(hedef), "python")


def test_hidden_rejection_message_is_actionable():
    with pytest.raises(UnsafeWriteTarget) as exc:
        resolve_write_target(str(HOME / ".bashrc"), "python")
    assert "gizli" in str(exc.value).lower()


# ── Normal hedefler calismaya devam etmeli ────────────────────────────

@pytest.mark.parametrize(
    "hedef",
    [
        HOME / "Desktop" / "script.py",
        HOME / "jarvis_programs" / "proje" / "main.py",
        HOME / "notlar.py",
    ],
)
def test_normal_targets_still_allowed(hedef):
    assert resolve_write_target(str(hedef), "python") == hedef


def test_relative_name_goes_to_desktop():
    sonuc = resolve_write_target("hesap.py", "python")
    assert sonuc.name == "hesap.py"
    assert not any(p.startswith(".") for p in sonuc.parts)


def test_default_target_when_no_path_given():
    sonuc = resolve_write_target("", "python")
    assert sonuc.suffix == ".py"


# ── Ev dizini disi hala reddedilmeli (regresyon korumasi) ─────────────

@pytest.mark.parametrize("hedef", ["/etc/cron.d/x", "/tmp/disarisi.py"])
def test_outside_home_still_rejected(hedef):
    with pytest.raises(UnsafeWriteTarget):
        resolve_write_target(hedef, "python")
