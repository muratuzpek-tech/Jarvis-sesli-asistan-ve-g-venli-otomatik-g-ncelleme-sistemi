"""Signed update staging with an explicit user-approval record.

This module never installs or applies an update. A separate installer must
consume an approved, revalidated staged artifact.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests

from jarvis.paths import data_dir

MANIFEST_ENV = "JARVIS_UPDATE_MANIFEST_URL"
ORIGIN_ENV = "JARVIS_UPDATE_ORIGIN"
PUBLIC_KEY_ENV = "JARVIS_UPDATE_PUBLIC_KEY_B64"
MANIFEST_MAX_BYTES = 128 * 1024
UPDATE_MAX_BYTES = 256 * 1024 * 1024
REQUEST_TIMEOUT = (5, 20)
_ALLOWED_TYPES = {
    ".exe": "exe",
    ".msi": "msi",
    ".patch": "patch",
    ".whl": "wheel",
    ".zip": "zip",
}
_SIGNED_FIELDS = (
    "version",
    "source_url",
    "sha256",
    "size_bytes",
    "artifact_type",
    "release_notes",
)
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_last_check: float | None = None
_check_lock = threading.Lock()


@dataclass(frozen=True)
class UpdateOffer:
    version: str
    artifact_type: str
    filename: str
    staged_path: Path
    sha256: str
    size_bytes: int
    release_notes: str


@dataclass(frozen=True)
class UpdateCheckResult:
    state: str
    message: str
    offer: UpdateOffer | None = None


@dataclass(frozen=True)
class _Settings:
    manifest_url: str
    origin: str
    public_key: bytes


class UpdateError(ValueError):
    """An update failed validation or could not be staged safely."""


def _normalized_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise UpdateError("Güncelleme kaynağı geçerli bir HTTPS origin olmalıdır.")
    return f"https://{parsed.netloc}".lower()


def _validate_url(url: str, origin: str, *, allow_query: bool = False) -> str:
    parsed = urlsplit(url)
    actual_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    if (
        parsed.scheme.lower() != "https"
        or actual_origin != origin
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise UpdateError("Manifest veya artifact URL’si güvenilen HTTPS origin dışına çıkıyor.")
    return url


def _settings() -> _Settings | None:
    manifest_url = os.environ.get(MANIFEST_ENV, "").strip()
    origin_value = os.environ.get(ORIGIN_ENV, "").strip()
    public_key_value = os.environ.get(PUBLIC_KEY_ENV, "").strip()
    if not (manifest_url and origin_value and public_key_value):
        return None

    origin = _normalized_origin(origin_value)
    _validate_url(manifest_url, origin)
    try:
        public_key = base64.b64decode(public_key_value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UpdateError("Ed25519 public key Base64 biçiminde değil.") from exc
    if len(public_key) != 32:
        raise UpdateError("Ed25519 public key tam 32 bayt olmalıdır.")
    return _Settings(manifest_url, origin, public_key)


def update_check_configured() -> bool:
    """Return whether the signed-update trust configuration is complete."""
    try:
        return _settings() is not None
    except UpdateError:
        return False


def _response_bytes(url: str, limit: int, *, expected_origin: str) -> bytes:
    _validate_url(url, expected_origin)
    try:
        response = requests.get(
            url,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "MuratJARVIS-Update/1.0"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Güncelleme sunucusuna bağlanılamadı: {exc}") from exc

    content = bytearray()
    with response:
        if response.status_code != 200 or getattr(response, "url", url) != url:
            raise UpdateError(f"Güncelleme sunucusu HTTP {response.status_code} döndürdü.")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > limit:
                    raise UpdateError("İndirilen içerik boyut sınırını aşıyor.")
            except ValueError as exc:
                raise UpdateError("Sunucunun Content-Length başlığı geçersiz.") from exc
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            content.extend(chunk)
            if len(content) > limit:
                raise UpdateError("İndirilen içerik boyut sınırını aşıyor.")
    return bytes(content)


def _canonical_signed_fields(manifest: dict) -> bytes:
    payload = {key: manifest[key] for key in _SIGNED_FIELDS}
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _verify_manifest(manifest: object, public_key: bytes, origin: str) -> tuple[str, str, str, int, str, str]:
    if not isinstance(manifest, dict):
        raise UpdateError("Güncelleme manifesti JSON nesnesi değil.")
    required = set(_SIGNED_FIELDS) | {"signature"}
    if set(manifest) != required:
        raise UpdateError("Güncelleme manifesti alanları beklenen şemayla eşleşmiyor.")

    version = manifest["version"]
    source_url = manifest["source_url"]
    digest = manifest["sha256"]
    size = manifest["size_bytes"]
    artifact_type = manifest["artifact_type"]
    notes = manifest["release_notes"]
    signature_text = manifest["signature"]
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version) or version in {".", ".."}:
        raise UpdateError("Güncelleme sürüm kimliği geçersiz.")
    if not isinstance(source_url, str):
        raise UpdateError("Güncelleme artifact URL’si geçersiz.")
    _validate_url(source_url, origin)
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise UpdateError("Manifest SHA-256 özeti geçersiz.")
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= UPDATE_MAX_BYTES:
        raise UpdateError("Güncelleme boyutu izin verilen sınırın dışında.")
    if not isinstance(artifact_type, str) or artifact_type not in set(_ALLOWED_TYPES.values()):
        raise UpdateError("Güncelleme artifact türü desteklenmiyor.")
    if not isinstance(notes, str) or len(notes) > 1024 or any(ord(ch) < 32 and ch not in "\n\t" for ch in notes):
        raise UpdateError("Sürüm notları geçersiz veya çok uzun.")
    if not isinstance(signature_text, str):
        raise UpdateError("Manifest imzası geçersiz.")

    filename = unquote(urlsplit(source_url).path.rsplit("/", 1)[-1])
    if not _FILENAME_RE.fullmatch(filename) or filename in {".", ".."}:
        raise UpdateError("Artifact dosya adı güvenli değil.")
    if _ALLOWED_TYPES.get(Path(filename).suffix.lower()) != artifact_type:
        raise UpdateError("Artifact türü dosya uzantısıyla eşleşmiyor.")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature,
            _canonical_signed_fields(manifest),
        )
    except ImportError as exc:
        raise UpdateError("İmza doğrulaması için cryptography paketi gerekli.") from exc
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise UpdateError("Güncelleme manifestinin Ed25519 imzası doğrulanamadı.") from exc
    return version, source_url, filename, size, digest, notes


def _staging_root() -> Path:
    user_root = Path(data_dir()).expanduser().resolve()
    staging_root = user_root / "updates" / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    resolved = staging_root.resolve()
    if not resolved.is_relative_to(user_root):
        raise UpdateError("Staging klasörü kullanıcı veri alanının dışına yönleniyor.")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_artifact(
    url: str,
    target: Path,
    *,
    origin: str,
    expected_size: int,
    expected_digest: str,
) -> None:
    _validate_url(url, origin)
    try:
        response = requests.get(
            url,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "MuratJARVIS-Update/1.0"},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Güncelleme dosyası indirilemedi: {exc}") from exc

    temporary_name: str | None = None
    digest = hashlib.sha256()
    total = 0
    try:
        with response:
            if response.status_code != 200 or getattr(response, "url", url) != url:
                raise UpdateError(f"Artifact sunucusu HTTP {response.status_code} döndürdü.")
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) != expected_size:
                        raise UpdateError("Artifact Content-Length manifest boyutuyla eşleşmiyor.")
                except ValueError as exc:
                    raise UpdateError("Artifact Content-Length başlığı geçersiz.") from exc

            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".part",
                dir=target.parent,
            )
            with os.fdopen(file_descriptor, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > expected_size or total > UPDATE_MAX_BYTES:
                        raise UpdateError("Artifact manifestteki boyutu aşıyor.")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        if total != expected_size:
            raise UpdateError("İndirilen artifact boyutu manifestle eşleşmiyor.")
        if digest.hexdigest() != expected_digest:
            raise UpdateError("İndirilen artifact SHA-256 özeti manifestle eşleşmiyor.")
        os.replace(temporary_name, target)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            if os.name == "posix":
                os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def check_and_stage_update(*, force: bool = False) -> UpdateCheckResult:
    """Fetch and verify a signed update, then save it outside the app directory."""
    global _last_check
    try:
        settings = _settings()
    except UpdateError as exc:
        return UpdateCheckResult("failed", f"Güncelleme yapılandırması geçersiz: {exc}")
    if settings is None:
        return UpdateCheckResult("not-configured", "İmzalı güncelleme kaynağı yapılandırılmamış.")

    with _check_lock:
        now = time.monotonic()
        if not force and _last_check is not None and now - _last_check < 6 * 60 * 60:
            return UpdateCheckResult("recent-check", "Güncelleme denetimi bu oturumda yakın zamanda yapıldı.")
        _last_check = now

    try:
        manifest_bytes = _response_bytes(
            settings.manifest_url,
            MANIFEST_MAX_BYTES,
            expected_origin=settings.origin,
        )
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdateError("Güncelleme manifesti geçerli UTF-8 JSON değil.") from exc
        version, source_url, filename, size, digest, notes = _verify_manifest(
            manifest,
            settings.public_key,
            settings.origin,
        )
        staging_root = _staging_root()
        version_dir = staging_root / version
        if version_dir.is_symlink():
            raise UpdateError("Sürüm staging klasörü symlink olamaz.")
        version_dir.mkdir(parents=True, exist_ok=True)
        if not version_dir.resolve().is_relative_to(staging_root):
            raise UpdateError("Sürüm staging klasörü izin verilen alanın dışına çıkıyor.")
        target = version_dir / f"{digest[:12]}-{filename}"
        if target.is_symlink():
            raise UpdateError("Staging artifact’i symlink olamaz.")

        offer = UpdateOffer(version, manifest["artifact_type"], filename, target, digest, size, notes)
        if target.exists():
            if not target.is_file() or _sha256_file(target) != digest or target.stat().st_size != size:
                raise UpdateError("Aynı sürüm için staging dosyası beklenen özete sahip değil.")
            return UpdateCheckResult("already-staged", "Doğrulanmış güncelleme staging alanında zaten mevcut.", offer)

        _stage_artifact(
            source_url,
            target,
            origin=settings.origin,
            expected_size=size,
            expected_digest=digest,
        )
        _atomic_write_json(version_dir / "manifest.json", manifest)
        return UpdateCheckResult("staged", "İmza ve SHA-256 doğrulandı; güncelleme staging alanına kaydedildi.", offer)
    except (OSError, requests.RequestException, UpdateError) as exc:
        return UpdateCheckResult("failed", f"Güncelleme staging işlemi başarısız: {exc}")


def _decision_path(offer: UpdateOffer) -> Path:
    staging_root = _staging_root()
    path = offer.staged_path.resolve()
    if not path.is_relative_to(staging_root) or path.is_symlink():
        raise UpdateError("Onay kaydı yalnızca staging alanındaki dosyalar için tutulabilir.")
    return path.parent / "approval.json"


def get_user_decision(offer: UpdateOffer) -> str | None:
    try:
        decision_path = _decision_path(offer)
        data = json.loads(decision_path.read_text(encoding="utf-8"))
        if data.get("version") != offer.version or data.get("sha256") != offer.sha256:
            return None
        decision = data.get("decision")
        return decision if decision in {"approved", "deferred"} else None
    except (OSError, ValueError, UpdateError, json.JSONDecodeError):
        return None


def record_user_decision(offer: UpdateOffer, approved: bool) -> Path:
    """Persist explicit approval/deferment; never installs the staged artifact."""
    if not offer.staged_path.is_file() or offer.staged_path.is_symlink():
        raise UpdateError("Onaylanacak staging dosyası bulunamadı veya güvenli değil.")
    if offer.staged_path.stat().st_size != offer.size_bytes or _sha256_file(offer.staged_path) != offer.sha256:
        raise UpdateError("Onay öncesi staging dosyasının SHA-256 özeti yeniden doğrulanamadı.")
    path = _decision_path(offer)
    _atomic_write_json(
        path,
        {
            "version": offer.version,
            "sha256": offer.sha256,
            "decision": "approved" if approved else "deferred",
            "decided_at": datetime.now(UTC).isoformat(),
        },
    )
    return path


def is_update_approved(offer: UpdateOffer) -> bool:
    """Check a saved approval and revalidate the staged bytes before installation."""
    if get_user_decision(offer) != "approved":
        return False
    try:
        return (
            offer.staged_path.is_file()
            and not offer.staged_path.is_symlink()
            and offer.staged_path.stat().st_size == offer.size_bytes
            and _sha256_file(offer.staged_path) == offer.sha256
        )
    except OSError:
        return False


def start_background_update_check(
    notify: Callable[[str], None],
    request_approval: Callable[[UpdateOffer], bool],
) -> threading.Thread | None:
    """Start a non-blocking startup check; ask before recording any approval."""
    if not update_check_configured():
        return None

    def _run() -> None:
        result = check_and_stage_update()
        if result.state == "not-configured" or result.state == "recent-check":
            return
        if result.state == "failed":
            notify(f"SYS: {result.message}")
            return
        offer = result.offer
        if offer is None:
            return
        if get_user_decision(offer) is not None:
            return
        notify(f"SYS: İmzalı güncelleme staging alanında hazır: {offer.version}.")
        try:
            approved = bool(request_approval(offer))
        except Exception as exc:  # A UI closing must never affect normal startup.
            notify(f"SYS: Güncelleme onayı alınamadı; işlem ertelendi ({type(exc).__name__}).")
            approved = False
        try:
            record_user_decision(offer, approved)
        except (OSError, UpdateError) as exc:
            notify(f"SYS: Güncelleme kararı kaydedilemedi: {exc}")
            return
        if approved:
            notify("SYS: Kullanıcı onayı kaydedildi. Bu bileşen güncellemeyi kurmaz veya uygulama dosyalarını değiştirmez.")
        else:
            notify("SYS: Güncelleme daha sonraya bırakıldı; uygulama dosyaları değiştirilmedi.")

    worker = threading.Thread(target=_run, name="JarvisUpdateCheck", daemon=True)
    worker.start()
    return worker
