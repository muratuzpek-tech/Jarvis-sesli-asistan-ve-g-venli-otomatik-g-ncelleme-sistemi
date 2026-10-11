"""Optional local coding-agent providers for dev_agent.

Providers return untrusted text only. This module never applies patches, installs
packages, writes source files, or executes generated code.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests


class ProviderError(RuntimeError):
    """A local provider is unavailable or returned an invalid response."""


class DevAgentProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def generate(self, prompt: str, workspace: Path) -> str: ...


@dataclass(frozen=True)
class OllamaProvider:
    name: str = "ollama"
    model: str = "qwen2.5-coder:7b"
    base_url: str = "http://127.0.0.1:11434"
    timeout: float = 120.0

    def available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/version", timeout=3)
            return response.ok
        except requests.RequestException:
            return False

    def generate(self, prompt: str, workspace: Path) -> str:
        if not workspace.is_dir():
            raise ProviderError(f"Workspace does not exist: {workspace}")
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Ollama request failed: {exc}") from exc
        text = payload.get("response") if isinstance(payload, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Ollama returned an empty response")
        return text.strip()


@dataclass(frozen=True)
class OpenInterpreterProvider:
    """Optional CLI adapter; generated output is treated as untrusted text."""

    name: str = "open-interpreter"
    command: str = "interpreter"
    timeout: float = 120.0

    def available(self) -> bool:
        return shutil.which(self.command) is not None

    def generate(self, prompt: str, workspace: Path) -> str:
        if not workspace.is_dir():
            raise ProviderError(f"Workspace does not exist: {workspace}")
        if not self.available():
            raise ProviderError("Open Interpreter is not installed or not on PATH")
        try:
            result = subprocess.run(
                [self.command, "--local"],
                input=prompt,
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError(f"Open Interpreter invocation failed: {exc}") from exc
        output = (result.stdout or result.stderr).strip()
        if result.returncode != 0 or not output:
            raise ProviderError(output or "Open Interpreter returned no output")
        return output


def provider_order() -> list[str]:
    raw = os.getenv("JARVIS_DEV_AGENT_PROVIDERS", "ollama").strip()
    return [item.strip().lower() for item in raw.split(",") if item.strip()] or ["ollama"]


def build_provider(name: str) -> DevAgentProvider:
    normalized = name.strip().lower()
    if normalized == "ollama":
        return OllamaProvider(model=os.getenv("JARVIS_OLLAMA_MODEL", "qwen2.5-coder:7b"))
    if normalized in {"open-interpreter", "open_interpreter", "openinterpreter"}:
        return OpenInterpreterProvider()
    raise ProviderError(f"Unknown local provider: {name}")


def generate_with_local_provider(prompt: str, workspace: Path) -> tuple[str, str]:
    """Return ``(provider_name, text)`` using configured local fallbacks."""
    errors: list[str] = []
    for name in provider_order():
        try:
            provider = build_provider(name)
            if not provider.available():
                errors.append(f"{name}: unavailable")
                continue
            return provider.name, provider.generate(prompt, workspace)
        except ProviderError as exc:
            errors.append(f"{name}: {exc}")
    raise ProviderError("No local coding provider succeeded: " + "; ".join(errors))
