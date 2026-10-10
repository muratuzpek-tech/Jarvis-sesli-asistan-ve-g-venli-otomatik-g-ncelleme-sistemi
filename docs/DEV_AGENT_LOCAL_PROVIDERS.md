# Local dev-agent providers

This branch adds an optional provider interface for free local coding agents.
The supported providers are:

- `ollama` (default), using `qwen2.5-coder:7b`.
- `open-interpreter`, only when its CLI is installed and explicitly selected.

Configure on Windows PowerShell:

```powershell
$env:JARVIS_DEV_AGENT_PROVIDERS = "ollama,open-interpreter"
$env:JARVIS_OLLAMA_MODEL = "qwen2.5-coder:7b"
$env:JARVIS_WORKSPACE = "$env:USERPROFILE\Desktop\JarvisProjects"
```

Install and download the local model separately:

```powershell
ollama pull qwen2.5-coder:7b
pip install open-interpreter
```

The provider layer only returns untrusted text. It does not apply patches,
write `src/jarvis`, install dependencies, or execute generated code. The
existing `dev_agent` confirmation, workspace, validation, and review flow must
remain the only path that applies changes.

Claude Code, Continue, and OpenDevin are not included as fake adapters because
they have different CLI/API contracts and may require separate credentials or
runtime services. They can be added later behind this same interface after a
specific local installation contract is selected.
