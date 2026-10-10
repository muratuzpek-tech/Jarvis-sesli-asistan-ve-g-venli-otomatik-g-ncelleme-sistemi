"""Explicit, visible PowerShell typing bridge.

This module never invokes a shell itself.  It types a user-provided command in
an already visible terminal through the existing computer-control backend.  The
caller must separately verify that the current user request explicitly asked
for PowerShell; this module also rejects control characters and oversized input.
"""
from __future__ import annotations

import re


_EXPLICIT = re.compile(r"\b(power\s*shell|powershell|pwsh)\b", re.IGNORECASE)
_MAX_COMMAND_LENGTH = 8192


def explicitly_requested(text: str) -> bool:
    """Return whether the current user request names PowerShell explicitly."""
    return bool(_EXPLICIT.search(str(text or "")))


def powershell_control(parameters: dict | None = None, player=None) -> str:
    """Type one explicit PowerShell command into the visible active terminal."""
    params = parameters or {}
    command = params.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return "NOT RUN: a concrete PowerShell command is required."
    command = command.strip()
    if len(command) > _MAX_COMMAND_LENGTH:
        return "NOT RUN: PowerShell command is too long."
    if any(char in command for char in "\r\n\x00"):
        return "NOT RUN: multiline or control-character commands are not allowed."

    from jarvis.actions.computer_control import computer_control

    typed = computer_control(
        {"action": "type", "text": command},
        player=player,
    )
    if str(typed).startswith(("No action", "Error", "Failed")):
        return str(typed)
    pressed = computer_control({"action": "press", "key": "enter"}, player=player)
    return f"{typed}; {pressed}"
