"""Wayland ekran görüntüsü: xdg-desktop-portal Screenshot arayüzü (jeepney ile).

GNOME 50'de mss siyah görüntü döndürüyor, gnome-screenshot ise kapatılmış;
Wayland'de ekran görüntüsünün resmi yolu bu portal. İlk kullanımda GNOME bir
izin penceresi gösterebilir.

Portal görüntüyü kullanıcının Resimler klasörüne kaydedip URI'sini döndürür;
biz dosyayı belleğe okuyup SİLERİZ, böylece klasör JARVIS görüntüleriyle dolmaz.
"""
from __future__ import annotations

import io
import secrets
from pathlib import Path
from urllib.parse import unquote, urlparse

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"


class PortalError(RuntimeError):
    pass


def _uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise PortalError(f"Beklenmeyen URI: {uri}")
    return Path(unquote(parsed.path))


def screenshot_png(timeout: float = 30.0, keep_file: bool = False) -> bytes:
    """Tüm ekranın PNG baytlarını döndürür. Kullanıcı reddederse PortalError."""
    try:
        from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
        from jeepney.io.blocking import Proxy, open_dbus_connection
    except ImportError as e:
        raise PortalError("jeepney kurulu değil: .venv/bin/pip install jeepney") from e

    conn = open_dbus_connection(bus="SESSION")
    try:
        token = "jarvis" + secrets.token_hex(6)
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request",
                         member="Response", path=handle)
        Proxy(message_bus, conn).AddMatch(rule)
        with conn.filter(rule) as queue:
            addr = DBusAddress(PORTAL_PATH, bus_name=PORTAL_BUS, interface="org.freedesktop.portal.Screenshot")
            msg = new_method_call(addr, "Screenshot", "sa{sv}",
                                  ("", {"handle_token": ("s", token), "interactive": ("b", False)}))
            conn.send_and_get_reply(msg, timeout=10)
            try:
                signal = conn.recv_until_filtered(queue, timeout=timeout)
            except TimeoutError as e:
                raise PortalError(f"Portal {timeout:.0f} sn içinde yanıt vermedi (izin penceresi açık kalmış olabilir).") from e
        code, results = signal.body
        if code != 0:
            raise PortalError("Ekran görüntüsü izni verilmedi." if code == 1 else f"Portal hata kodu: {code}")
        path = _uri_to_path(results["uri"][1])
        data = path.read_bytes()
        if not keep_file:
            path.unlink(missing_ok=True)
        return data
    finally:
        conn.close()


def screenshot_image(region: tuple[int, int, int, int] | None = None):
    """PIL.Image döndürür; region=(x, y, genişlik, yükseklik) verilirse kırpar."""
    from PIL import Image

    img = Image.open(io.BytesIO(screenshot_png()))
    img.load()
    if region:
        x, y, w, h = region
        img = img.crop((x, y, x + w, y + h))
    return img
