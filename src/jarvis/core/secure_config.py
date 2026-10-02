"""User configuration and machine-local TLS material.

Secrets are read from the explicit environment override first, then from the
user data directory.  Package files are never used as an implicit writable
configuration store; legacy package data requires explicit opt-in.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from jarvis.paths import data_dir, package_dir


_PLACEHOLDER_PREFIXES = ("REPLACE_", "YOUR_", "<", "CHANGE_ME")


def base_dir() -> Path:
    """Backward-compatible package directory accessor."""
    return package_dir()


def _user_api_keys_path() -> Path:
    # Do not call config_dir() here: callers import this module while building
    # module-level constants, and selecting a path should not create directories.
    return data_dir() / "config" / "api_keys.json"


def api_keys_path(for_write: bool = False) -> Path:
    """Return the configured API-key file without leaking package data.

    ``JARVIS_API_KEYS`` is an explicit file override and is used for both reads
    and writes.  Legacy package data is read only when
    ``JARVIS_USE_LEGACY_DATA=1`` is explicitly set; no migration or deletion is
    performed.
    """
    override = os.environ.get("JARVIS_API_KEYS", "").strip()
    if override:
        return Path(override).expanduser()

    user_path = _user_api_keys_path()
    if for_write or user_path.exists():
        return user_path

    # An explicit JARVIS_HOME is an isolation boundary; never mix it with
    # package data even if a legacy flag is inherited from the environment.
    if (not os.environ.get("JARVIS_HOME", "").strip()
            and os.environ.get("JARVIS_USE_LEGACY_DATA", "").strip() == "1"):
        legacy = package_dir() / "config" / "api_keys.json"
        if legacy.is_file():
            return legacy
    return user_path


def load_config() -> dict:
    """Load a mapping; malformed, unreadable, or non-object files are empty."""
    try:
        data = json.loads(api_keys_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(data: dict) -> Path:
    """Merge and atomically save configuration in the selected user file.

    Unknown existing settings are preserved so camera/OS preference writes do
    not erase credentials or provider settings.  A malformed existing file is
    treated as empty.  The temporary file is created beside the destination so
    ``os.replace`` is atomic on the target filesystem.
    """
    if not isinstance(data, dict):
        raise TypeError("configuration must be a mapping")
    path = api_keys_path(for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            existing_raw = json.loads(path.read_text(encoding="utf-8"))
            existing = existing_raw if isinstance(existing_raw, dict) else {}
        except (OSError, UnicodeError, ValueError, TypeError):
            existing = {}
    else:
        existing = {}
    merged = {**existing, **data}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(merged, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if os.name == "posix":
            path.chmod(0o600)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def _usable_secret(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or candidate.upper().startswith(_PLACEHOLDER_PREFIXES):
        return ""
    return candidate


def get_gemini_api_key() -> str:
    """Return a usable key from env or file, without exposing secret values."""
    key = _usable_secret(os.getenv("GEMINI_API_KEY", ""))
    if key:
        return key
    key = _usable_secret(load_config().get("gemini_api_key"))
    if key:
        return key
    raise RuntimeError(
        "Gemini API key is not configured; set GEMINI_API_KEY or configure the user data file."
    )


def get_cloud_llm_key() -> str:
    """Ücretsiz bulut modeli (Groq vb., OpenAI uyumlu) anahtarı; yoksa "".

    Anahtar yalnızca ortam değişkeninden ya da kullanıcının yerel ayar
    dosyasından okunur; hiçbir yere yazdırılmaz."""
    for env in ("JARVIS_CLOUD_LLM_KEY", "GROQ_API_KEY"):
        key = _usable_secret(os.getenv(env, ""))
        if key:
            return key
    return _usable_secret(load_config().get("cloud_llm_api_key"))


def tls_paths() -> tuple[Path, Path]:
    """Dashboard TLS key/certificate paths in the user data directory."""
    d = data_dir() / "config" / "certs"
    return d / "jarvis.key", d / "jarvis.crt"


def _atomic_bytes(path: Path, payload: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        if mode is not None and os.name == "posix":
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        if mode is not None and os.name == "posix":
            path.chmod(mode)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_self_signed_cert() -> tuple[Path, Path] | None:
    """Create a machine-local self-signed pair without overwriting a key.

    An existing complete pair is reused.  If only one half exists, generation
    stops rather than silently replacing an existing private key or pairing it
    with unrelated certificate material.
    """
    key_path, cert_path = tls_paths()
    if key_path.is_symlink() or cert_path.is_symlink():
        print("[TLS] Refusing symlinked TLS material.")
        return None
    key_exists = key_path.is_file()
    cert_exists = cert_path.is_file()
    if key_exists and cert_exists:
        if os.name == "posix":
            try:
                key_path.chmod(0o600)
            except OSError:
                return None
        return key_path, cert_path
    if key_exists or cert_exists:
        print("[TLS] Existing TLS material is incomplete; refusing to overwrite it.")
        return None
    try:
        import datetime
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MuratJARVIS Dashboard")])
        now = datetime.datetime.now(datetime.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
        _atomic_bytes(key_path, key_bytes, 0o600)
        _atomic_bytes(cert_path, cert_bytes, 0o644)
        return key_path, cert_path
    except Exception:
        print("[TLS] Could not create machine-local TLS material; using HTTP.")
        return None
