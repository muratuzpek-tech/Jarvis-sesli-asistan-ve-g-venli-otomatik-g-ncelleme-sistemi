from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jarvis.core import update_manager as updates


ORIGIN = "https://updates.example.test"
MANIFEST_URL = f"{ORIGIN}/latest.json"
ARTIFACT_URL = f"{ORIGIN}/releases/jarvis-25.1.1.patch"
ARTIFACT = b"diff --git a/a.py b/a.py\n"


class FakeResponse:
    def __init__(self, body: bytes, url: str, status: int = 200, headers: dict | None = None):
        self.body = body
        self.url = url
        self.status_code = status
        self.headers = headers or {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def iter_content(self, chunk_size: int):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]


def _signed_manifest(artifact: bytes = ARTIFACT):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    fields = {
        "version": "25.1.1-qgfix.1",
        "source_url": ARTIFACT_URL,
        "sha256": hashlib.sha256(artifact).hexdigest(),
        "size_bytes": len(artifact),
        "artifact_type": "patch",
        "release_notes": "Quality gate patch; download only.",
    }
    canonical = json.dumps(
        fields,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest = {
        **fields,
        "signature": base64.b64encode(private_key.sign(canonical)).decode("ascii"),
    }
    return public_key, manifest


def _setup(monkeypatch, tmp_path: Path, artifact: bytes = ARTIFACT):
    monkeypatch.setattr(updates, "_last_check", 0.0)
    public_key, manifest = _signed_manifest(artifact)
    monkeypatch.setenv(updates.MANIFEST_ENV, MANIFEST_URL)
    monkeypatch.setenv(updates.ORIGIN_ENV, ORIGIN)
    monkeypatch.setenv(updates.PUBLIC_KEY_ENV, base64.b64encode(public_key).decode("ascii"))
    monkeypatch.setattr(updates, "data_dir", lambda: tmp_path / "jarvis-data")
    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    calls: list[str] = []
    responses = {
        MANIFEST_URL: FakeResponse(manifest_bytes, MANIFEST_URL),
        ARTIFACT_URL: FakeResponse(artifact, ARTIFACT_URL),
    }

    def fake_get(url: str, **_kwargs):
        calls.append(url)
        return responses[url]

    monkeypatch.setattr(updates.requests, "get", fake_get)
    return manifest, responses, calls


def test_signed_artifact_is_staged_outside_application_and_verified(monkeypatch, tmp_path):
    manifest, _responses, calls = _setup(monkeypatch, tmp_path)

    result = updates.check_and_stage_update(force=True)

    assert result.state == "staged"
    assert result.offer is not None
    assert result.offer.staged_path.is_file()
    assert result.offer.staged_path.read_bytes() == ARTIFACT
    assert result.offer.staged_path.is_relative_to(tmp_path / "jarvis-data" / "updates" / "staging")
    assert hashlib.sha256(result.offer.staged_path.read_bytes()).hexdigest() == manifest["sha256"]
    assert calls == [MANIFEST_URL, ARTIFACT_URL]


def test_repeat_check_reuses_same_verified_artifact(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    first = updates.check_and_stage_update(force=True)
    assert first.offer is not None
    calls: list[str] = []
    original_get = updates.requests.get

    def counted_get(url: str, **kwargs):
        calls.append(url)
        return original_get(url, **kwargs)

    monkeypatch.setattr(updates.requests, "get", counted_get)
    second = updates.check_and_stage_update(force=True)

    assert second.state == "already-staged"
    assert second.offer == first.offer
    assert calls == [MANIFEST_URL]


def test_bad_signature_fails_closed_without_download_or_staging(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    response = updates.requests.get(MANIFEST_URL)
    manifest = json.loads(response.body)
    manifest["release_notes"] = "tampered after signing"
    response.body = json.dumps(manifest).encode("utf-8")

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "imzası doğrulanamadı" in result.message
    assert not (tmp_path / "jarvis-data" / "updates" / "staging").exists()


def test_hash_mismatch_never_leaves_partial_artifact(monkeypatch, tmp_path):
    manifest, responses, _calls = _setup(monkeypatch, tmp_path)
    responses[ARTIFACT_URL] = FakeResponse(b"x" * len(ARTIFACT), ARTIFACT_URL)

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "SHA-256" in result.message
    staging = tmp_path / "jarvis-data" / "updates" / "staging"
    assert not list(staging.rglob("*.patch"))
    assert not list(staging.rglob("*.part"))
    assert manifest["sha256"] != hashlib.sha256(b"x" * len(ARTIFACT)).hexdigest()


def test_manifest_cannot_download_from_a_different_origin(monkeypatch, tmp_path):
    manifest, responses, calls = _setup(monkeypatch, tmp_path)
    manifest["source_url"] = "https://attacker.example.test/payload.patch"
    responses[MANIFEST_URL].body = json.dumps(manifest).encode("utf-8")

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "origin dışına" in result.message
    assert calls == [MANIFEST_URL]


def test_manifest_signature_binds_size_and_artifact_type(monkeypatch, tmp_path):
    manifest, responses, _calls = _setup(monkeypatch, tmp_path)
    manifest["size_bytes"] += 1
    responses[MANIFEST_URL].body = json.dumps(manifest).encode("utf-8")

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "imzası doğrulanamadı" in result.message


def test_redirects_are_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        updates.requests,
        "get",
        lambda url, **_kwargs: FakeResponse(b"", url, status=302),
    )

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "HTTP 302" in result.message


def test_user_approval_is_persisted_and_revalidated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = updates.check_and_stage_update(force=True)
    assert result.offer is not None
    offer = result.offer

    assert updates.get_user_decision(offer) is None
    decision_path = updates.record_user_decision(offer, approved=True)
    assert updates.get_user_decision(offer) == "approved"
    assert updates.is_update_approved(offer)
    assert json.loads(decision_path.read_text(encoding="utf-8"))["decision"] == "approved"

    offer.staged_path.write_bytes(b"changed after approval")
    assert not updates.is_update_approved(offer)


def test_deferred_decision_is_not_treated_as_approval(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = updates.check_and_stage_update(force=True)
    assert result.offer is not None

    updates.record_user_decision(result.offer, approved=False)

    assert updates.get_user_decision(result.offer) == "deferred"
    assert not updates.is_update_approved(result.offer)


def test_missing_trust_configuration_does_not_make_a_network_request(monkeypatch, tmp_path):
    for name in (updates.MANIFEST_ENV, updates.ORIGIN_ENV, updates.PUBLIC_KEY_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(updates, "data_dir", lambda: tmp_path / "jarvis-data")
    monkeypatch.setattr(updates.requests, "get", lambda *_a, **_k: pytest.fail("network must stay closed"))

    result = updates.check_and_stage_update(force=True)

    assert result.state == "not-configured"
    assert not (tmp_path / "jarvis-data").exists()


def test_background_startup_records_only_explicit_user_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    real_thread = updates.threading.Thread

    class ImmediateThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(updates.threading, "Thread", ImmediateThread)
    messages: list[str] = []
    prompts: list[updates.UpdateOffer] = []
    worker = updates.start_background_update_check(
        messages.append,
        lambda offer: prompts.append(offer) or True,
    )

    assert worker is not None
    assert len(prompts) == 1
    assert updates.get_user_decision(prompts[0]) == "approved"
    assert any("uygulama dosyalarını değiştirmez" in message for message in messages)
    monkeypatch.setattr(updates.threading, "Thread", real_thread)


def test_invalid_trust_origin_is_rejected_without_network(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setenv(updates.ORIGIN_ENV, "http://updates.example.test")
    monkeypatch.setattr(updates.requests, "get", lambda *_a, **_k: pytest.fail("network must stay closed"))

    result = updates.check_and_stage_update(force=True)

    assert result.state == "failed"
    assert "yapılandırması geçersiz" in result.message


def test_legacy_signed_update_entrypoint_only_stages(monkeypatch, tmp_path):
    from jarvis.core import agent as agent_module

    target = tmp_path / "staged.patch"
    target.write_bytes(ARTIFACT)
    offer = updates.UpdateOffer(
        version="25.1.1-qgfix.1",
        artifact_type="patch",
        filename="jarvis-25.1.1.patch",
        staged_path=target,
        sha256=hashlib.sha256(ARTIFACT).hexdigest(),
        size_bytes=len(ARTIFACT),
        release_notes="test",
    )
    expected = updates.UpdateCheckResult("staged", "staged", offer)
    monkeypatch.setattr(updates, "check_and_stage_update", lambda **_kwargs: expected)
    monkeypatch.setattr(
        agent_module,
        "_atomic_self_repair",
        lambda *_args, **_kwargs: pytest.fail("legacy update path must never replace agent.py"),
    )
    original = agent_module.SELF_REPAIR_TARGET.read_bytes()

    message = agent_module._check_signed_self_update(force=True)

    assert "staging" in message
    assert agent_module.SELF_REPAIR_TARGET.read_bytes() == original
