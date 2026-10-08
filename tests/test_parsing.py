import pytest

from omini_proxmox.client import base_url
from omini_proxmox.collect import (
    agent_enabled,
    guest_nics,
    mac,
    netstat_counters,
    pve_version,
)


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("192.168.1.10", "https://192.168.1.10:8006"),
        ("https://pve.lan", "https://pve.lan:8006"),
        ("https://pve.lan:8006/", "https://pve.lan:8006"),
        ("https://pve.example.com:443", "https://pve.example.com:443"),  # reverse proxy
        ("https://pve.lan:8006/api2/json", "https://pve.lan:8006"),
    ],
)
def test_base_url(url, base):
    assert base_url(url) == base


@pytest.mark.parametrize(
    ("value", "normalized"),
    [
        ("BC:24:11:AA:00:01", "bc:24:11:aa:00:01"),
        ("bc-24-11-aa-00-01", "bc:24:11:aa:00:01"),
        ("00:00:00:00:00:00", None),
        ("dhcp", None),
        (None, None),
    ],
)
def test_mac_normalization(value, normalized):
    assert mac(value) == normalized


def test_vm_nics_with_any_model():
    nics = guest_nics(
        {
            "net2": "e1000=52:54:00:12:34:56,bridge=vmbr1",
            "net0": "virtio=BC:24:11:AA:00:01,bridge=vmbr0,tag=20,firewall=1",
            "net1": "model=vmxnet3,macaddr=BC:24:11:AA:00:03",  # no bridge: NAT
            "net3": "virtio,bridge=vmbr0",  # no MAC: skipped
            "netx": "virtio=BC:24:11:AA:00:09",
            "name": "vm",
        }
    )
    assert [(n.index, n.mac, n.bridge) for n in nics] == [
        (0, "bc:24:11:aa:00:01", "vmbr0"),
        (1, "bc:24:11:aa:00:03", None),
        (2, "52:54:00:12:34:56", "vmbr1"),
    ]


def test_container_nic_static_addresses():
    [nic] = guest_nics(
        {"net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:BB:00:01,ip=10.0.0.5/24,ip6=fd00::5/64"}
    )
    assert (nic.name, nic.ips) == ("eth0", ["10.0.0.5/24", "fd00::5/64"])


@pytest.mark.parametrize(
    ("value", "enabled"),
    [
        ("1", True),
        ("0", False),
        ("enabled=1,fstrim_cloned_disks=1", True),
        ("1,type=virtio", True),
        (None, False),
    ],
)
def test_agent_enabled(value, enabled):
    assert agent_enabled({"agent": value} if value is not None else {}) is enabled


def test_netstat_device_names():
    counters = netstat_counters(
        [
            {"dev": "tap101i1", "vmid": "101", "in": 10, "out": 20},
            {"dev": "net0", "vmid": 102, "in": "30", "out": "40"},
            {"dev": "fwln101i0", "vmid": "101", "in": 1, "out": 1},
        ]
    )
    assert counters == {("101", 1): (10, 20), ("102", 0): (30, 40)}


def test_pve_version():
    assert pve_version("pve-manager/9.0.3/025864202ebb6109") == "9.0.3"
    assert pve_version(None) is None
