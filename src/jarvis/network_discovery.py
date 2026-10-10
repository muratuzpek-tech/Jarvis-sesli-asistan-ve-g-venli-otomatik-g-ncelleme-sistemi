"""Bounded, local-only IPv4 discovery for the JARVIS LAN view.

The scanner deliberately reports observations rather than pretending to be a
complete inventory: only hosts that answer ARP/ICMP/TCP probes are returned.
Scopes are restricted to an active private interface and at most a /24.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

try:
    import psutil
except ImportError:  # pragma: no cover - declared runtime dependency
    psutil = None


class NetworkScopeError(ValueError):
    """Raised when a requested CIDR is unsafe or not attached locally."""


@dataclass(frozen=True, slots=True)
class HostObservation:
    address: ipaddress.IPv4Address
    open_ports: tuple[int, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LocalIPv4Network:
    interface_name: str
    address: ipaddress.IPv4Address
    network: ipaddress.IPv4Network


@dataclass(frozen=True, slots=True)
class ScanSnapshot:
    network: ipaddress.IPv4Network
    local_address: ipaddress.IPv4Address
    gateway: ipaddress.IPv4Address | None
    hosts: tuple[HostObservation, ...]
    scanned_at: str
    elapsed_seconds: float
    cancelled: bool = False


# Conservative, useful service probes; this is not a port scanner for arbitrary ports.
DEFAULT_PORTS = (22, 53, 80, 443, 445, 3389, 5000, 8000, 8080)
_SERVICE_NAMES = {
    22: "SSH:22",
    53: "DNS:53",
    80: "HTTP:80",
    443: "HTTPS:443",
    445: "SMB:445",
    3389: "RDP:3389",
    5000: "HTTP:5000",
    8000: "HTTP:8000",
    8080: "HTTP:8080",
}


def _private_ipv4(value: str) -> ipaddress.IPv4Address | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address) and address.is_private and not address.is_loopback:
        return address
    return None


def active_local_networks() -> tuple[LocalIPv4Network, ...]:
    """Return active, private IPv4 interfaces with a /24-or-smaller scope."""
    if psutil is None:
        return ()
    result: list[LocalIPv4Network] = []
    for interface, addresses in psutil.net_if_addrs().items():
        for item in addresses:
            if getattr(item, "family", None) != socket.AF_INET:
                continue
            address = _private_ipv4(item.address)
            if address is None or not item.netmask:
                continue
            try:
                network = ipaddress.IPv4Network(f"{address}/{item.netmask}", strict=False)
            except ValueError:
                continue
            if network.prefixlen < 24 or network.prefixlen > 30:
                continue
            if psutil.net_if_stats().get(interface) and not psutil.net_if_stats()[interface].isup:
                continue
            result.append(LocalIPv4Network(interface, address, network))
    return tuple(dict.fromkeys(result))


def parse_scope(value: str) -> ipaddress.IPv4Network:
    """Parse a private IPv4 CIDR and reject oversized or non-host scopes."""
    try:
        network = ipaddress.ip_network(str(value).strip(), strict=False)
    except ValueError as exc:
        raise NetworkScopeError("Geçerli bir IPv4 CIDR girin.") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise NetworkScopeError("Yalnızca IPv4 ağları taranabilir.")
    if not network.is_private or network.is_loopback:
        raise NetworkScopeError("Yalnızca özel IPv4 ağları taranabilir.")
    if network.prefixlen < 24:
        raise NetworkScopeError("Tarama kapsamı /24 veya daha dar olmalıdır.")
    return network


def suggested_scope(networks: tuple[LocalIPv4Network, ...] | list[LocalIPv4Network]) -> str:
    return str(networks[0].network) if networks else "192.168.1.0/24"


def service_labels(ports: tuple[int, ...] | list[int]) -> tuple[str, ...]:
    return tuple(_SERVICE_NAMES.get(int(port), f"TCP:{int(port)}") for port in sorted(set(ports)))


def _gateway_for(network: ipaddress.IPv4Network) -> ipaddress.IPv4Address | None:
    """Best-effort route lookup; gateway is informational and never a host result."""
    try:
        if os.name == "nt":
            output = subprocess.run(
                ["route", "print", "-4"], capture_output=True, text=True, timeout=2, check=False
            ).stdout
            pattern = rf"^\s*{re.escape(str(network.network_address))}\s+([\d.]+)\s+"
        else:
            output = subprocess.run(
                ["ip", "-4", "route", "show", "dev"],
                capture_output=True, text=True, timeout=2, check=False,
            ).stdout
            pattern = r"default via ([\d.]+)"
        match = re.search(pattern, output, re.MULTILINE)
        if match:
            candidate = ipaddress.IPv4Address(match.group(1))
            return candidate if candidate in network else None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _tcp_probe(address: ipaddress.IPv4Address, port: int, timeout: float) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((str(address), port)) == 0
    except OSError:
        return False


def scan_lan(
    cidr: str | ipaddress.IPv4Network,
    *,
    local_networks: tuple[LocalIPv4Network, ...] | list[LocalIPv4Network] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    timeout: float = 0.12,
) -> ScanSnapshot:
    """Probe a validated local scope and return only observed hosts."""
    network = parse_scope(str(cidr))
    interfaces = tuple(local_networks) if local_networks is not None else active_local_networks()
    matching = tuple(item for item in interfaces if network == item.network)
    if not matching:
        raise NetworkScopeError("CIDR etkin yerel ağ arayüzüyle eşleşmiyor.")
    local_address = matching[0].address
    addresses = tuple(address for address in network.hosts() if address != local_address)
    total = len(addresses)
    started = time.monotonic()
    observations: list[HostObservation] = []
    was_cancelled = False
    for completed, address in enumerate(addresses, start=1):
        if cancelled and cancelled():
            was_cancelled = True
            break
        open_ports = tuple(port for port in ports if _tcp_probe(address, port, timeout))
        if open_ports:
            observations.append(HostObservation(address, open_ports, ("tcp",)))
        if progress:
            progress(completed, total)
    return ScanSnapshot(
        network=network,
        local_address=local_address,
        gateway=_gateway_for(network),
        hosts=tuple(observations),
        scanned_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        elapsed_seconds=max(0.0, time.monotonic() - started),
        cancelled=was_cancelled,
    )
