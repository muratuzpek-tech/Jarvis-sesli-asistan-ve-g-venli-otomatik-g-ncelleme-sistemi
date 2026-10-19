"""Güvenli, onaylı ve doğrulanabilir dosya indirme aracı."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from jarvis.core.approval_service import approval_service

_MAX_BYTES = 500 * 1024 * 1024
_CHUNK_SIZE = 1024 * 128
_TIMEOUT = (10, 60)
_EXECUTABLE_SUFFIXES = frozenset({
    ".bat", ".cmd", ".com", ".exe", ".msi", ".ps1", ".sh", ".appimage",
    ".deb", ".rpm", ".run", ".jar", ".dll", ".scr", ".vbs", ".js",
})
_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(value: str) -> str:
    """Tek bir dosya adına indirger; traversal ve platform özel karakterlerini reddeder."""
    name = Path(str(value or "")).name.strip().strip(". ")
    if not name or name in {".", ".."} or _INVALID_FILENAME.search(name):
        raise ValueError("Geçersiz dosya adı. Yalnızca tek bir güvenli dosya adı kullanın.")
    if len(name) > 180:
        raise ValueError("Dosya adı 180 karakterden kısa olmalı.")
    return name


def _validate_url(raw: str) -> str:
    parsed = urlparse(str(raw or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Yalnızca geçerli HTTPS URL'leri kabul edilir.")
    if parsed.username or parsed.password:
        raise ValueError("Kullanıcı adı/parola içeren URL'ler güvenlik nedeniyle reddedildi.")
    # HTTP yalnızca yerel entegrasyon testi için kabul edilir; internet indirmeleri HTTPS olmalı.
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("İnternet indirmelerinde HTTPS zorunludur.")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))}
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            local_http_test = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            if not local_http_test and (
                ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
            ):
                raise ValueError("URL yerel/özel bir ağ adresine çözülüyor; indirme reddedildi.")
    except socket.gaierror as exc:
        raise ValueError(f"URL alan adı çözümlenemedi: {parsed.hostname}") from exc
    return parsed.geturl()


def _resolve_target(parameters: dict) -> tuple[Path, str]:
    from jarvis.actions.file_controller import _is_safe_write_file, _resolve_path

    folder_raw = str(parameters.get("path") or "downloads").strip()
    folder = _resolve_path(folder_raw).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f"Hedef klasör bulunamadı: {folder}")
    raw_name = str(parameters.get("filename") or parameters.get("name") or "").strip()
    if not raw_name:
        raw_name = Path(urlparse(str(parameters.get("url", ""))).path).name or "download.bin"
        raw_name = unquote(raw_name)
    filename = _safe_filename(raw_name)
    target = folder / filename
    if not _is_safe_write_file(target):
        raise ValueError("Hedef yol güvenli kullanıcı yazma klasörleri içinde değil.")
    return target, filename


def _executable_warning(target: Path) -> str:
    if target.suffix.lower() in _EXECUTABLE_SUFFIXES:
        return "UYARI: Bu dosya çalıştırılabilir olabilir; JARVIS indirme sonrasında otomatik çalıştırmayacak."
    return ""


def _preview(url: str, target: Path, overwrite: bool, warning: str) -> str:
    summary = f"URL: {url} | Hedef: {target} | Üzerine yaz: {'evet' if overwrite else 'hayır'}"
    item = approval_service.request("download_file", summary)
    extra = f"\n{warning}" if warning else ""
    return (
        "ONAY GEREKLİ (indirme henüz başlamadı).\n"
        f"Kaynak: {url}\nHedef: {target}\n"
        f"Mevcut dosyanın üzerine yaz: {'evet' if overwrite else 'hayır'}"
        f"{extra}\nOnay kodu: {item.code}\n"
        "Kullanıcı açıkça onayladıktan sonra aynı parametrelerle confirm_code gönderin."
    )


def download_file(parameters: dict | None = None, *, player=None) -> str:
    """URL'den dosyayı güvenli kullanıcı klasörüne atomik olarak indirir."""
    params = dict(parameters or {})
    try:
        url = _validate_url(str(params.get("url", "")).strip())
        target, _filename = _resolve_target(params)
        overwrite = str(params.get("overwrite", "false")).lower() in {"1", "true", "yes", "evet"}
        warning = _executable_warning(target)
        summary = f"URL: {url} | Hedef: {target} | Üzerine yaz: {'evet' if overwrite else 'hayır'}"
        code = str(params.get("confirm_code", "")).strip()
        if not code or not approval_service.consume(code, "download_file", summary):
            return _preview(url, target, overwrite, warning)
        if target.exists() and not overwrite:
            return f"İndirme durduruldu: hedef dosya zaten var ({target}). Üzerine yazmak için overwrite=true ile yeniden isteyin."
        if target.is_dir():
            return f"İndirme durduruldu: hedef bir klasör ({target})."
    except (ValueError, OSError) as exc:
        return f"İndirme başlatılmadı: {exc}"

    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    received = 0
    try:
        current_url = url
        for redirect_count in range(4):
            with requests.get(current_url, stream=True, timeout=_TIMEOUT, allow_redirects=False,
                              headers={"User-Agent": "JARVIS-secure-downloader/1.0"}) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("Location")
                    if not location or redirect_count == 3:
                        raise ValueError("güvenli yönlendirme sınırı aşıldı veya hedef belirtilmedi")
                    current_url = _validate_url(urljoin(current_url, location))
                    continue
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                expected = int(content_length) if content_length and content_length.isdigit() else None
                if expected is not None and expected > _MAX_BYTES:
                    return f"İndirme reddedildi: dosya boyutu sınırı {_MAX_BYTES // (1024 * 1024)} MB'ı aşıyor."
                with part.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > _MAX_BYTES:
                            raise ValueError(f"dosya boyutu sınırı {_MAX_BYTES // (1024 * 1024)} MB'ı aşıldı")
                        output.write(chunk)
                        if player is not None and hasattr(player, "write_log") and received % (4 * _CHUNK_SIZE) < len(chunk):
                            player.write_log(f"[İNDİRME] {target.name}: {received / (1024 * 1024):.1f} MB alındı")
                break
        else:
            raise ValueError("çok fazla yönlendirme")
        if expected is not None and received != expected:
            raise ValueError(f"boyut doğrulaması başarısız (beklenen {expected}, alınan {received})")
        os.replace(part, target)
        size = target.stat().st_size
        if size != received:
            raise ValueError("kayıt sonrası boyut doğrulaması başarısız")
        warning_text = f" {warning}" if warning else ""
        return f"İndirme başarılı: {target} ({size / (1024 * 1024):.2f} MB).{warning_text}"
    except requests.Timeout:
        return "İndirme hatası: ağ zaman aşımına uğradı; kısmi dosya kaydedilmedi."
    except requests.RequestException as exc:
        return f"İndirme hatası: sunucuya erişilemedi ({exc}). Kısmi dosya kaydedilmedi."
    except (OSError, ValueError) as exc:
        return f"İndirme hatası: {exc}. Kısmi dosya kaydedilmedi."
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
