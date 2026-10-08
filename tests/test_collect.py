import copy

import pytest
from omini_sdk import PluginError

from omini_proxmox.collect import collect
from omini_proxmox.collect import test as connection_test


def by_name(devices):
    return {d.name: d for d in devices}


def test_reads_the_node(pve, cfg):
    devices = by_name(collect(cfg))
    assert list(devices) == ["pve1", "homeassistant", "jellyfin"]  # running guests only
    node = devices["pve1"]
    assert node.key == "bc:24:11:00:00:0a"  # hwaddress of vmbr0, normalized
    assert node.role == "server" and node.vendor == "Proxmox"
    assert node.host == "192.168.1.10"
    assert node.model == "Intel(R) Core(TM) i5-8500T CPU @ 2.10GHz"
    assert node.os_version == "Proxmox VE 8.2.4"
    assert node.uptime_s == 1209600
    assert node.cpu_pct == 3.1 and node.mem_pct == 25.0 and node.swap_pct == 10.0
    assert node.load_avg == [0.52, 0.61, 0.58]
    assert node.ips == ["192.168.1.10", "10.0.30.10", "fd00::10"]
    assert [m.root for m in node.macs] == ["bc:24:11:00:00:0a"]


def test_node_interfaces(pve, cfg):
    node = by_name(collect(cfg))["pve1"]
    ports = {i.name: i for i in node.interfaces}
    assert list(ports) == ["bond0", "enp1s0", "enp2s0", "enp3s0", "vmbr0", "vmbr1", "vmbr1.30"]
    assert ports["enp1s0"].type == "ethernet" and ports["enp1s0"].up is True
    assert ports["enp3s0"].up is None  # not active: unknown, never invented
    assert ports["bond0"].type == "lag" and ports["bond0"].members == ["enp2s0", "enp3s0"]
    vmbr0 = ports["vmbr0"]
    assert (vmbr0.type, vmbr0.members, vmbr0.description) == ("bridge", ["enp1s0"], "Management")
    assert vmbr0.ips == ["192.168.1.10/24", "fd00::10/64"]
    assert vmbr0.wan is None  # a hypervisor is not a router: no WAN node
    vlan = ports["vmbr1.30"]
    assert (vlan.type, vlan.parent, vlan.vlan) == ("vlan", "vmbr1", 30)
    assert all(i.rx_bytes is None for i in node.interfaces)  # the API has no node counters
    [gw] = node.gateways
    assert (gw.interface, gw.address, gw.status) == ("vmbr0", "192.168.1.1", "unknown")


def test_node_storage_and_version(pve, cfg):
    node = by_name(collect(cfg))["pve1"]
    disks = {s.mount: s for s in node.storage}
    assert list(disks) == ["/", "local", "local-lvm"]  # inactive NFS left out
    assert disks["local-lvm"].fs_type == "lvmthin"
    assert disks["local-lvm"].total_bytes == 1862454607872
    assert disks["/"].used_bytes == 12884901888
    # PVEAuditor cannot read the update list (Sys.Modify): only the version is known.
    assert node.firmware.current == "8.2.4"
    assert node.firmware.update_available is None and node.firmware.updates is None


def test_pending_updates_when_the_token_may_read_them(pve, cfg):
    del pve.errors["/nodes/pve1/apt/"]
    fw = by_name(collect(cfg))["pve1"].firmware
    assert (fw.current, fw.latest, fw.update_available, fw.updates) == ("8.2.4", "8.2.7", True, 2)


def test_vm_hangs_under_its_node(pve, cfg):
    vm = by_name(collect(cfg))["homeassistant"]
    assert vm.key == "bc:24:11:aa:00:01"  # first NIC, normalized
    assert vm.role == "server" and vm.model == "QEMU/KVM virtual machine"
    assert vm.os_version == "Linux"
    assert [m.root for m in vm.macs] == ["bc:24:11:aa:00:01", "bc:24:11:aa:00:02"]
    assert vm.ips == ["192.168.1.20"] and vm.host == "192.168.1.20"  # from the guest agent
    assert vm.cpu_pct == 4.2 and vm.mem_pct == 50.0 and vm.uptime_s == 86400
    [nb] = vm.neighbors
    assert (nb.protocol, nb.local_port, nb.remote_port) == ("other", "net0", "vmbr0")
    assert (nb.remote_name, nb.remote_mac, nb.remote_ip) == (
        "pve1",
        "bc:24:11:00:00:0a",
        "192.168.1.10",
    )
    net0, net1 = vm.interfaces
    assert (net0.name, net0.type, net0.up) == ("net0", "other", True)
    assert (net0.rx_bytes, net0.tx_bytes) == (987654321, 123456789)  # counters, from netstat
    assert net1.up is False and net1.ips is None  # link_down=1


def test_container_hangs_under_its_node(pve, cfg):
    ct = by_name(collect(cfg))["jellyfin"]
    assert ct.key == "bc:24:11:bb:00:01"
    assert ct.model == "LXC container" and ct.os_version == "Debian"
    assert ct.ips == ["192.168.1.30", "10.0.30.31"]
    assert ct.cpu_pct == 10.5 and ct.mem_pct == 25.0
    eth0, eth1 = ct.interfaces
    assert (eth0.description, eth0.rx_bytes, eth0.tx_bytes) == ("eth0", 5555, 4444)
    assert eth1.ips == ["10.0.30.31/24"]  # static in the config, also seen live: once
    assert ct.storage[0].mount == "/" and ct.storage[0].used_bytes == 4294967296
    [nb] = ct.neighbors
    assert (nb.local_port, nb.remote_port, nb.remote_name) == ("net0", "vmbr0", "pve1")


def test_only_reads(pve, cfg):
    collect(cfg)
    connection_test(cfg)
    assert pve.methods == {"GET"}


def test_guest_agent_not_running_does_not_fail(pve, cfg):
    pve.errors["/nodes/pve1/qemu/100/agent/"] = 500  # "QEMU guest agent is not running"
    vm = by_name(collect(cfg))["homeassistant"]
    assert vm.ips is None and vm.host is None
    assert vm.neighbors[0].remote_name == "pve1"  # still placed under its node


def test_missing_privileges_skip_data_and_are_reported(pve, cfg):
    pve.errors["/nodes/pve1/storage"] = 403
    pve.errors["/nodes/pve1/qemu/100/agent/"] = 403
    node = by_name(collect(cfg))["pve1"]
    assert {s.mount for s in node.storage} == {"/"}
    assert node.interfaces  # the rest still works
    pve.errors["/nodes/pve1/status"] = 403
    msg = connection_test(cfg)
    assert msg.startswith("Connected to Proxmox VE 8.2.4: 1 node, 2 running guests. Missing")
    assert "Sys.Audit" in msg
    assert "Sys.Modify" not in msg  # never asked for: it allows changes


def test_without_a_mac_the_node_is_found_by_its_address(pve, cfg):
    net = pve.routes["/nodes/pve1/network"]["data"]
    for row in net:
        row.pop("options", None)
    devices = by_name(collect(cfg))
    assert devices["pve1"].key == "proxmox:pve1" and devices["pve1"].macs is None
    nb = devices["homeassistant"].neighbors[0]
    assert nb.remote_mac is None and nb.remote_ip == "192.168.1.10"


def test_mac_of_the_physical_port_behind_the_bridge(pve, cfg):
    net = pve.routes["/nodes/pve1/network"]["data"]
    for row in net:
        row.pop("options", None)
    net[0]["options"] = ["hwaddress 00-1B-21-AA-BB-CC"]  # enp1s0, under vmbr0
    node = by_name(collect(cfg))["pve1"]
    assert node.key == "00:1b:21:aa:bb:cc"


def test_cluster_reads_every_online_node(pve, cfg):
    pve.routes["/nodes"]["data"] += [
        {"node": "pve2", "status": "online", "type": "node", "id": "node/pve2"},
        {"node": "pve3", "status": "offline", "type": "node", "id": "node/pve3"},
    ]
    pve.routes["/cluster/status"]["data"] = [
        {"type": "cluster", "id": "cluster", "name": "homelab", "nodes": 3, "quorate": 1},
        *pve.routes["/cluster/status"]["data"],
        {"type": "node", "id": "node/pve2", "name": "pve2", "ip": "192.168.1.11", "online": 1},
        {"type": "node", "id": "node/pve3", "name": "pve3", "ip": "192.168.1.12", "online": 0},
    ]
    for path in list(pve.routes):
        if path.startswith("/nodes/pve1/") and "apt" not in path:
            body = copy.deepcopy(pve.routes[path])
            pve.routes[path.replace("/nodes/pve1/", "/nodes/pve2/", 1)] = body
    pve.routes["/nodes/pve2/network"]["data"][4]["options"] = ["hwaddress bc:24:11:00:00:0b"]
    pve.routes["/nodes/pve2/network"]["data"][4]["cidr"] = "192.168.1.11/24"
    pve.routes["/nodes/pve2/qemu"]["data"] = []
    pve.routes["/nodes/pve2/lxc"]["data"][0]["name"] = "jellyfin2"
    cfg_net = pve.routes["/nodes/pve2/lxc/200/config"]["data"]
    cfg_net["net0"] = "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:CC:00:01,ip=dhcp"
    pve.routes["/nodes/pve2/lxc/200/interfaces"]["data"] = []

    devices = by_name(collect(cfg))
    assert list(devices) == ["pve1", "homeassistant", "jellyfin", "pve2", "jellyfin2"]
    assert devices["pve2"].host == "192.168.1.11" and devices["pve2"].key == "bc:24:11:00:00:0b"
    nb = devices["jellyfin2"].neighbors[0]
    assert (nb.remote_name, nb.remote_mac) == ("pve2", "bc:24:11:00:00:0b")
    assert not any(c.startswith("/nodes/pve3/") for c in pve.calls)  # offline: skipped


def test_one_node_failing_does_not_break_the_others(pve, cfg):
    pve.routes["/nodes"]["data"].append({"node": "pve2", "status": "online"})
    pve.errors["/nodes/pve2/"] = 595  # no route to the node
    devices = by_name(collect(cfg))
    assert devices["pve2"].key == "proxmox:pve2" and devices["pve2"].interfaces is None
    assert "homeassistant" in devices


def test_connection_test(pve, cfg):
    assert connection_test(cfg) == "Connected to Proxmox VE 8.2.4: 1 node, 2 running guests"


def test_errors_the_user_can_act_on(pve, cfg):
    good, pve.token = pve.token, "PVEAPIToken=omini@pve!omini=other"
    with pytest.raises(PluginError, match="rejected the API token") as e:
        connection_test(cfg)
    assert cfg["token_secret"] not in str(e.value)
    pve.token = good
    cfg["token_id"] = "omini"
    with pytest.raises(PluginError, match="user@realm!name"):
        collect(cfg)
    cfg["url"] = ""
    with pytest.raises(PluginError, match="required"):
        collect(cfg)


def test_not_proxmox(pve, cfg):
    pve.routes.pop("/version")
    with pytest.raises(PluginError, match="does not look like Proxmox VE"):
        connection_test(cfg)


def test_unreachable_host(cfg):
    cfg["url"] = "https://127.0.0.1:9"
    with pytest.raises(PluginError, match="cannot connect"):
        connection_test(cfg)


def test_no_visible_guests_says_why(pve, cfg):
    # Proxmox VE filters guest lists by permission: without VM.Audit they are empty.
    pve.routes["/nodes/pve1/qemu"] = {"data": []}
    pve.routes["/nodes/pve1/lxc"] = {"data": []}
    assert list(by_name(collect(cfg))) == ["pve1"]
    msg = connection_test(cfg)
    assert "0 running guests. No running VMs or containers visible" in msg
    assert "VM.Audit" in msg
