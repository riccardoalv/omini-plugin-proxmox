from urllib.parse import parse_qs

import httpx
import pytest
from omini_sdk import Change, PluginError

import omini_proxmox.collect as collect_module

CONFIG = "/nodes/pve1/qemu/100/config"
CT_CONFIG = "/nodes/pve1/lxc/200/config"


def reprinted(value: str) -> str:
    """A NIC as Proxmox VE prints it back: its keys in its own order (a
    container's `type=veth` always last), not in the order they were sent."""
    return ",".join(sorted(value.split(","), key=lambda p: p.startswith("type=")))


def body_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


@pytest.fixture
def writable(pve):
    """The node takes PUTs on VM 100's and container 200's config, and keeps
    them as it prints them."""
    pve.puts = []
    inner = pve.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        if request.method == "PUT" and path in (CONFIG, CT_CONFIG):
            body = body_of(request)
            pve.puts.append(body)
            pve.routes[path]["data"].update({k: reprinted(v) for k, v in body.items()})
            return httpx.Response(200, json={"data": None})
        return inner(request)

    pve.handler = handler
    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    return pve


def vm(devices):
    return next(d for d in devices if d.name == "homeassistant")


def test_a_vms_nics_report_their_vlans(pve, cfg):
    nics = {i.name: i for i in vm(collect_module.collect(cfg)).interfaces}
    assert (nics["net1"].vlans.untagged, nics["net1"].vlans.tagged) == (20, None)
    # vmbr0 is not VLAN aware: its tag cannot be changed, so none is reported.
    assert nics["net0"].vlans is None


def test_nics_on_an_open_vswitch_bridge_report_their_vlans(pve, cfg):
    vmbr0 = next(r for r in pve.routes["/nodes/pve1/network"]["data"] if r["iface"] == "vmbr0")
    vmbr0["type"] = "OVSBridge"
    nics = {i.name: i for i in vm(collect_module.collect(cfg)).interfaces}
    assert nics["net0"].vlans.untagged == 1


def test_without_the_nodes_network_no_vlans_are_reported(pve, cfg):
    pve.errors["/nodes/pve1/network"] = 403
    assert all(i.vlans is None for i in vm(collect_module.collect(cfg)).interfaces)


def test_puts_a_nic_in_vlans(writable, cfg):
    change = Change(
        device="bc:24:11:aa:00:01",
        kind="port.vlans",
        port="net1",
        value={"untagged": 30, "tagged": [40, 50]},
    )
    devices = collect_module.apply(cfg, change)
    assert writable.puts == [
        {"net1": "virtio=BC:24:11:AA:00:02,bridge=vmbr1,link_down=1,tag=30,trunks=40;50"}
    ]
    net1 = next(i for i in vm(devices).interfaces if i.name == "net1")
    assert (net1.vlans.untagged, [t.root for t in net1.vlans.tagged]) == (30, [40, 50])
    # Back to the LAN: no tag, no trunks.
    collect_module.apply(
        cfg,
        Change(device="bc:24:11:aa:00:01", kind="port.vlans", port="net1", value={"untagged": 1}),
    )
    assert writable.puts[-1] == {"net1": "virtio=BC:24:11:AA:00:02,bridge=vmbr1,link_down=1"}


def test_a_bridge_must_be_vlan_aware(writable, cfg):
    change = Change(
        device="bc:24:11:aa:00:01", kind="port.vlans", port="net0", value={"untagged": 30}
    )
    with pytest.raises(PluginError, match="vmbr0 on pve1 is not VLAN aware"):
        collect_module.apply(cfg, change)
    assert not writable.puts


def test_with_vlans_keeps_the_rest():
    v = "virtio=BC:24:11:AA:00:02,bridge=vmbr1,firewall=1,tag=20,trunks=1;5"
    assert collect_module.with_vlans(v, {"untagged": 20, "tagged": [7]}) == (
        "virtio=BC:24:11:AA:00:02,bridge=vmbr1,firewall=1,tag=20,trunks=7"
    )


def test_a_change_printed_back_in_another_order_is_applied(writable, cfg):
    change = Change(
        device="bc:24:11:bb:00:01", kind="port.vlans", port="net1", value={"untagged": 40}
    )
    devices = collect_module.apply(cfg, change)
    sent = "name=eth1,bridge=vmbr1,hwaddr=BC:24:11:BB:00:02,ip=10.0.30.31/24,type=veth,tag=40"
    assert writable.puts == [{"net1": sent}]
    ct = next(d for d in devices if d.name == "jellyfin")
    assert next(i for i in ct.interfaces if i.name == "net1").vlans.untagged == 40


def test_a_change_waiting_for_a_restart_is_recognized_in_any_order(writable, cfg):
    """With hotplug off the config keeps the old value and the new one is
    pending, printed in Proxmox VE's own order."""
    inner = writable.handler
    pending = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        if request.method == "PUT" and path == CT_CONFIG:
            pending.extend({"key": k, "pending": reprinted(v)} for k, v in body_of(request).items())
            return httpx.Response(200, json={"data": None})
        if path == "/nodes/pve1/lxc/200/pending":
            return httpx.Response(200, json={"data": pending})
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    change = Change(
        device="bc:24:11:bb:00:01", kind="port.vlans", port="net1", value={"untagged": 40}
    )
    with pytest.raises(PluginError, match="changes only when the guest restarts"):
        collect_module.apply(cfg, change)


@pytest.mark.parametrize("port", ["name", "ostype", "scsi0", "net", "net0x", ""])
def test_only_a_network_device_can_be_changed(writable, cfg, port):
    change = Change(device="bc:24:11:aa:00:01", kind="port.vlans", port=port, value={"untagged": 1})
    with pytest.raises(PluginError, match="not a network device"):
        collect_module.apply(cfg, change)
    assert not writable.puts


@pytest.mark.parametrize(
    ("path", "status", "message"),
    [
        ("/nodes/pve1/qemu/100/config", 403, "VM.Config.Network"),
        ("/nodes/pve1/network", 403, "VM.Config.Network"),
        ("/nodes/pve1/network", 595, "could not read"),
        ("/nodes/pve1/qemu/100/config", 500, "could not read"),
    ],
)
def test_a_read_refused_during_a_change_says_why(writable, cfg, path, status, message):
    writable.errors[path] = status
    change = Change(
        device="bc:24:11:aa:00:01", kind="port.vlans", port="net1", value={"untagged": 30}
    )
    with pytest.raises(PluginError, match=message):
        collect_module.apply(cfg, change)
    assert not writable.puts


def test_an_open_vswitch_bridge_takes_vlans(writable, cfg):
    """An OVS bridge tags its ports without `bridge_vlan_aware`."""
    vmbr0 = next(r for r in writable.routes["/nodes/pve1/network"]["data"] if r["iface"] == "vmbr0")
    vmbr0["type"] = "OVSBridge"
    change = Change(
        device="bc:24:11:aa:00:01", kind="port.vlans", port="net0", value={"untagged": 30}
    )
    collect_module.apply(cfg, change)
    assert writable.puts == [{"net0": "virtio=BC:24:11:AA:00:01,bridge=vmbr0,firewall=1,tag=30"}]


def test_switches_a_nics_link_off_and_on(writable, cfg):
    off = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net0", value=False)
    devices = collect_module.apply(cfg, off)
    assert writable.puts == [
        {"net0": "virtio=BC:24:11:AA:00:01,bridge=vmbr0,firewall=1,link_down=1"}
    ]
    assert next(i for i in vm(devices).interfaces if i.name == "net0").enabled is False
    on = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net0", value=True)
    devices = collect_module.apply(cfg, on)
    assert writable.puts[-1] == {"net0": "virtio=BC:24:11:AA:00:01,bridge=vmbr0,firewall=1"}
    assert next(i for i in vm(devices).interfaces if i.name == "net0").enabled is True


def test_switching_on_a_nic_that_is_off_keeps_its_vlan(writable, cfg):
    on = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net1", value=True)
    collect_module.apply(cfg, on)
    assert writable.puts == [{"net1": "virtio=BC:24:11:AA:00:02,bridge=vmbr1,tag=20"}]


def test_a_link_already_as_wanted_is_not_written(writable, cfg):
    off = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net1", value=False)
    collect_module.apply(cfg, off)
    assert not writable.puts


def test_switches_a_containers_link_off(writable, cfg):
    off = Change(device="bc:24:11:bb:00:01", kind="port.enabled", port="net0", value=False)
    devices = collect_module.apply(cfg, off)
    assert collect_module.options(writable.puts[0]["net0"])["link_down"] == "1"
    ct = next(d for d in devices if d.name == "jellyfin")
    assert next(i for i in ct.interfaces if i.name == "net0").enabled is False


def test_a_link_change_needs_the_network_role(writable, cfg):
    inner = writable.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(403, json={"data": None})
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    off = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net0", value=False)
    with pytest.raises(PluginError, match=r"VM\.Config\.Network"):
        collect_module.apply(cfg, off)


def test_a_link_change_not_applied_is_said(writable, cfg):
    inner = writable.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json={"data": None})  # accepted, not kept
        return inner(request)

    collect_module.Client.keywords["transport"] = httpx.MockTransport(handler)
    off = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net0", value=False)
    with pytest.raises(PluginError, match="did not apply the change to net0"):
        collect_module.apply(cfg, off)


@pytest.mark.parametrize("value", [None, "off", 0, {"enabled": False}])
def test_port_enabled_needs_a_boolean(writable, cfg, value):
    change = Change(device="bc:24:11:aa:00:01", kind="port.enabled", port="net0", value=value)
    with pytest.raises(PluginError, match="true or false"):
        collect_module.apply(cfg, change)
    assert not writable.puts


def test_with_link_keeps_the_rest():
    v = "virtio=BC:24:11:AA:00:02,bridge=vmbr1,link_down=1,tag=20"
    assert collect_module.with_link(v, True) == "virtio=BC:24:11:AA:00:02,bridge=vmbr1,tag=20"
    assert collect_module.with_link(v, False) == (
        "virtio=BC:24:11:AA:00:02,bridge=vmbr1,tag=20,link_down=1"
    )
