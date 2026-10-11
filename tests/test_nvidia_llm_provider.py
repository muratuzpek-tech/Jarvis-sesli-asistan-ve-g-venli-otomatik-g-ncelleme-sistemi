import json

import pytest

import jarvis.core.llm_client as llm


def test_nvidia_provider_uses_nim_defaults(monkeypatch, tmp_path):
    config = tmp_path / "api_keys.json"
    config.write_text(json.dumps({"llm_provider": "nvidia", "llm_model": "test-model"}), encoding="utf-8")
    monkeypatch.setattr(llm, "CONFIG_PATH", config)
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-test")
    assert llm.get_llm_provider() == "nvidia"
    assert llm._openai_base_url("nvidia", "http://ignored") == "https://integrate.api.nvidia.com/v1"
    assert llm._openai_headers("nvidia")["Authorization"] == "Bearer nvapi-test"


def test_nvidia_provider_requires_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="NVIDIA_API_KEY"):
        llm._openai_headers("nvidia")
