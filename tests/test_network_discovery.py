from __future__ import annotations

import ipaddress

import pytest

from jarvis import network_discovery as nd


def local_network(prefix: int = 24) -> nd.LocalIPv4Network:
    network = ipaddress.IPv4Network(f"192.168.50.0/{prefix}")
    return nd.LocalIPv4Network("Ethernet", ipaddress.IPv4Address("192.168.50.10"), network)


def test_parse_scope_accepts_private_slash_24_and_normalizes_host_bits():
    assert nd.parse_scope("192.168.50.10/24") == ipaddress.IPv4Network("192.168.50.0/24")


@pytest.mark.parametrize("value", ["8.8.8.0/24", "192.168.0.0/16", "127.0.0.0/24", "not-a-cidr"])
def test_parse_scope_rejects_unsafe_scopes(value):
    with pytest.raises(nd.NetworkScopeError):
        nd.parse_scope(value)


def test_suggested_scope_and_service_labels_are_deterministic():
    assert nd.suggested_scope((local_network(),)) == "192.168.50.0/24"
    assert nd.service_labels((8080, 80, 80, 9999)) == ("HTTP:80", "HTTP:8080", "TCP:9999")


def test_scan_requires_an_active_matching_interface():
    with pytest.raises(nd.NetworkScopeError, match="eşleşmiyor"):
        nd.scan_lan("192.168.50.0/24", local_networks=())


def test_scan_reports_tcp_observations_and_progress(monkeypatch):
    local = local_network()
    progress: list[tuple[int, int]] = []

    def fake_probe(address, port, timeout):
        return str(address) == "192.168.50.2" and port == 80

    monkeypatch.setattr(nd, "_tcp_probe", fake_probe)
    monkeypatch.setattr(nd, "_gateway_for", lambda network: ipaddress.IPv4Address("192.168.50.1"))
    snapshot = nd.scan_lan(
        local.network,
        local_networks=(local,),
        ports=(80, 443),
        progress=lambda completed, total: progress.append((completed, total)),
    )

    assert snapshot.local_address == local.address
    assert snapshot.gateway == ipaddress.IPv4Address("192.168.50.1")
    assert snapshot.hosts == (
        nd.HostObservation(ipaddress.IPv4Address("192.168.50.2"), (80,), ("tcp",)),
    )
    assert progress[-1] == (253, 253)
    assert not snapshot.cancelled


def test_scan_honors_cancellation_before_next_probe(monkeypatch):
    local = local_network()
    calls = 0

    def fake_probe(address, port, timeout):
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(nd, "_tcp_probe", fake_probe)
    snapshot = nd.scan_lan(
        local.network,
        local_networks=(local,),
        ports=(80,),
        cancelled=lambda: calls >= 1,
    )

    assert snapshot.cancelled
    assert calls == 1


def test_scan_does_not_return_local_interface_as_discovered_host(monkeypatch):
    local = local_network()
    monkeypatch.setattr(nd, "_tcp_probe", lambda address, port, timeout: True)
    snapshot = nd.scan_lan(local.network, local_networks=(local,), ports=(80,))
    assert local.address not in {item.address for item in snapshot.hosts}
