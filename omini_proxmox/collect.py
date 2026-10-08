"""Turns Proxmox VE API answers into Omini devices: one per node, plus one per
running guest (VM or container), hung under the node that runs it."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

from omini_sdk import (
    Config,
    Device,
    Firmware,
    Gateway,
    Interface,
    Neighbor,
    PluginError,
    Storage,
    log,
)

from omini_proxmox.client import Client, Forbidden, NotFound, Unavailable, segment

T = TypeVar("T")

MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Privilege (and where the PVEAuditor role must be granted) per piece of data.
PRIVILEGES = {
    "node": "Sys.Audit on /nodes (PVEAuditor on /)",
    "guests": "VM.Audit on /vms (PVEAuditor on /)",
    "storage": "Datastore.Audit on /storage (PVEAuditor on /)",
    "agent": "VM.GuestAgent.Audit on /vms (guest IPs from the QEMU guest agent)",
}

# Guest reads run a few at a time: a cluster may run dozens of guests.
WORKERS = 6


def client_from(cfg: Config) -> Client:
    url, token_id, secret = cfg.str("url"), cfg.str("token_id"), cfg.str("token_secret")
    if not url or not token_id or not secret:
        raise PluginError("the address, API token ID and API token secret are required")
    if "!" not in token_id or "@" not in token_id.split("!", 1)[0]:
        raise PluginError("the API token ID looks like user@realm!name, e.g. omini@pve!omini")
    return Client(url, token_id.strip(), secret.strip(), verify_tls=cfg.bool("verify_tls", False))


# --- parsing helpers -------------------------------------------------------


def mac(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("-", ":")
    return v if MAC.match(v) and v != "00:00:00:00:00:00" else None


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def counter(value: Any) -> int | None:
    n = number(value)
    return int(n) if n is not None and n >= 0 else None


def flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str) and value.strip() in ("0", "1"):
        return value.strip() == "1"
    return None


def pct(used: Any, total: Any) -> float | None:
    u, t = number(used), number(total)
    if u is None or not t or t <= 0:
        return None
    return round(min(max(u / t * 100, 0), 100), 1)


def fraction_pct(value: Any) -> float | None:
    """CPU usage as Proxmox reports it (0.0 … 1.0) → percent."""
    n = number(value)
    return None if n is None else round(min(max(n * 100, 0), 100), 1)


def newer_fields(model: type, **values: Any) -> dict[str, Any]:
    """Fields an older SDK does not have are left out, so the plugin keeps
    working on an older Omini."""
    return {k: v for k, v in values.items() if v is not None and k in model.model_fields}


def pve_version(text: Any) -> str | None:
    """'pve-manager/8.2.4/faa83925c9641325' → '8.2.4'."""
    if not isinstance(text, str):
        return None
    m = re.search(r"pve-manager/([^/\s]+)", text)
    return m.group(1) if m else None


def options(text: str) -> dict[str, str]:
    """A property string: 'virtio=AA:BB:..,bridge=vmbr0,tag=20' → dict."""
    out: dict[str, str] = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def usable_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    return not (a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast)


def plain_ips(cidrs: list[str]) -> list[str]:
    """IPv4 first, then IPv6, without prefixes and duplicates."""
    seen: list[str] = []
    for c in cidrs:
        ip = c.split("/")[0]
        if ip not in seen:
            seen.append(ip)
    return sorted(seen, key=lambda ip: (":" in ip, seen.index(ip)))


def optional(
    what: str, missing: list[str], fn: Callable[[], T], default: T, report: bool = True
) -> T:
    """Optional data: a missing privilege or feature never fails the collection."""
    try:
        return fn()
    except Forbidden:
        if report:
            log.warning("no privilege for %s (%s)", what, PRIVILEGES.get(what, what))
            missing.append(PRIVILEGES.get(what, what))
        else:
            log.info("no privilege for %s", what)
    except NotFound:
        log.info("%s not available on this Proxmox VE", what)
    except Unavailable as e:
        log.info("%s not available: %s", what, e)
    except PluginError:
        raise
    except Exception:
        log.exception("could not read %s", what)
    return default


# --- nodes -----------------------------------------------------------------

IFACE_TYPES = {
    "eth": "ethernet",
    "bridge": "bridge",
    "OVSBridge": "bridge",
    "bond": "lag",
    "OVSBond": "lag",
    "vlan": "vlan",
}


def iface_mac(row: dict[str, Any]) -> str | None:
    """The API exposes no MAC addresses, except a `hwaddress` line set in
    /etc/network/interfaces, which it returns among the `options`."""
    for opt in row.get("options") or []:
        parts = str(opt).split()
        if parts and parts[0] in ("hwaddress", "hwaddr"):
            for p in parts[1:]:
                if m := mac(p):
                    return m
    return mac(row.get("hwaddress"))


def iface_ips(row: dict[str, Any]) -> list[str]:
    ips = []
    for cidr_key, addr_key, mask_key in (
        ("cidr", "address", "netmask"),
        ("cidr6", "address6", "netmask6"),
    ):
        cidr = row.get(cidr_key)
        if not cidr and row.get(addr_key):
            try:
                cidr = str(
                    ipaddress.ip_interface(f"{row[addr_key]}/{row.get(mask_key) or ''}".rstrip("/"))
                )
            except ValueError:
                cidr = row[addr_key]
        if cidr and usable_ip(cidr):
            ips.append(cidr)
    return ips


def node_interfaces(rows: list[dict[str, Any]]) -> list[Interface]:
    out = []
    for row in sorted(rows, key=lambda r: str(r.get("iface"))):
        name = row.get("iface")
        if not name or name == "lo":
            continue
        kind = IFACE_TYPES.get(str(row.get("type")), "other")
        members = []
        for key in ("bridge_ports", "slaves", "ovs_ports", "ovs_bonds"):
            members += [m for m in str(row.get(key) or "").split() if m != "none"]
        parent, vlan = row.get("vlan-raw-device"), counter(row.get("vlan-id"))
        if m := re.match(r"^(.+)\.(\d{1,4})$", name):  # eno1.20, vmbr0.20
            parent, vlan, kind = parent or m.group(1), vlan or int(m.group(2)), "vlan"
        out.append(
            Interface(
                name=name,
                description=(row.get("comments") or "").strip() or None,
                type=kind,
                mac=iface_mac(row),
                ips=iface_ips(row) or None,
                parent=parent or None,
                members=members or None,
                up=flag(row.get("active")),
                vlan=vlan if vlan and 1 <= vlan <= 4094 else None,
            )
        )
    return out


def management(
    ifaces: list[Interface], rows: list[dict[str, Any]], ip_hint: str | None
) -> Interface | None:
    """The interface the node is reached on: the one with the cluster address
    (or the address Omini connects to), else the one with the default gateway."""
    if ip_hint:
        for i in ifaces:
            if any(c.split("/")[0] == ip_hint for c in i.ips or []):
                return i
    with_gw = {r.get("iface") for r in rows if r.get("gateway") or r.get("gateway6")}
    return next((i for i in ifaces if i.name in with_gw), None)


def physical_mac(
    iface: Interface | None, by_name: dict[str, Interface], depth: int = 0
) -> str | None:
    """The MAC of an interface, else of the physical port behind it (bridge →
    bond → port, VLAN → its parent)."""
    if iface is None or depth > 4:
        return None
    if iface.mac:
        return iface.mac
    for name in [*(iface.members or []), *([iface.parent] if iface.parent else [])]:
        if m := physical_mac(by_name.get(name), by_name, depth + 1):
            return m
    return None


def gateways(rows: list[dict[str, Any]]) -> list[Gateway]:
    """The node's default gateways. A hypervisor is not a router: its uplinks
    are not marked as WANs, the gateway is only described."""
    out = []
    for row in rows:
        for key, label in (("gateway", "default gateway"), ("gateway6", "default IPv6 gateway")):
            if row.get(key):
                out.append(
                    Gateway(
                        name=f"{row.get('iface')} {label}",
                        interface=row.get("iface"),
                        address=row[key],
                        status="unknown",
                    )
                )
    return out


def storage(status: dict[str, Any], stores: list[dict[str, Any]]) -> list[Storage]:
    out = []
    root = status.get("rootfs") or {}
    if counter(root.get("total")):
        out.append(
            Storage(
                mount="/",
                total_bytes=counter(root.get("total")),
                used_bytes=counter(root.get("used")),
            )
        )
    for s in sorted(stores, key=lambda s: str(s.get("storage"))):
        if flag(s.get("active")) is False or flag(s.get("enabled")) is False:
            continue
        if not s.get("storage") or not counter(s.get("total")):
            continue
        out.append(
            Storage(
                mount=str(s["storage"]),
                fs_type=s.get("type"),
                total_bytes=counter(s.get("total")),
                used_bytes=counter(s.get("used")),
            )
        )
    return out


def firmware(version: str | None, updates: list[dict[str, Any]] | None) -> Firmware | None:
    """The installed version, and the updates Proxmox VE already knows about
    (its own last `apt update`): the list is only read, never refreshed."""
    if version is None and updates is None:
        return None
    if updates is None:
        return Firmware(current=version)
    latest = next((u.get("Version") for u in updates if u.get("Package") == "pve-manager"), None)
    return Firmware(
        current=version,
        latest=latest or (version if not updates else None),
        update_available=bool(updates),
        updates=len(updates),
    )


# --- guests ----------------------------------------------------------------

QEMU_OS = {
    "l26": "Linux",
    "l24": "Linux 2.4",
    "win11": "Windows 11",
    "win10": "Windows 10",
    "win8": "Windows 8",
    "win7": "Windows 7",
    "wvista": "Windows Vista",
    "wxp": "Windows XP",
    "w2k8": "Windows Server 2008",
    "w2k3": "Windows Server 2003",
    "w2k": "Windows 2000",
    "solaris": "Solaris",
}
LXC_OS = {
    "debian": "Debian",
    "devuan": "Devuan",
    "ubuntu": "Ubuntu",
    "centos": "CentOS",
    "fedora": "Fedora",
    "opensuse": "openSUSE",
    "archlinux": "Arch Linux",
    "alpine": "Alpine Linux",
    "gentoo": "Gentoo",
    "nixos": "NixOS",
}


@dataclass
class Nic:
    index: int
    mac: str
    bridge: str | None = None
    up: bool | None = None
    name: str | None = None  # inside the guest (containers: eth0)
    ips: list[str] = field(default_factory=list)


def guest_nics(config: dict[str, Any]) -> list[Nic]:
    """net0..netN of a VM ('virtio=AA:BB:..,bridge=vmbr0,tag=20') or a container
    ('name=eth0,bridge=vmbr0,hwaddr=AA:BB:..,ip=192.168.1.50/24')."""
    nics = []
    for key, value in config.items():
        m = re.match(r"^net(\d+)$", key)
        if not m or not isinstance(value, str):
            continue
        opts = options(value)
        found = mac(opts.get("hwaddr")) or mac(opts.get("macaddr"))
        if not found:  # VMs: the model is the key, the MAC its value
            found = next((mac(v) for v in opts.values() if mac(v)), None)
        if not found:
            continue
        nic = Nic(
            index=int(m.group(1)),
            mac=found,
            bridge=opts.get("bridge") or None,
            up=opts.get("link_down") != "1",
            name=opts.get("name") or None,
        )
        for ip_key in ("ip", "ip6"):
            ip = opts.get(ip_key, "")
            if "/" in ip and usable_ip(ip):  # static addresses (not dhcp / manual / auto)
                nic.ips.append(ip)
        nics.append(nic)
    return sorted(nics, key=lambda n: n.index)


def agent_enabled(config: dict[str, Any]) -> bool:
    """`agent: 1` or `agent: enabled=1,fstrim_cloned_disks=1`."""
    value = str(config.get("agent") or "")
    first = value.split(",", 1)[0]
    return first == "1" or options(value).get("enabled") == "1"


def guest_addresses(rows: Any) -> dict[str, list[str]]:
    """IPs per MAC, from the guest agent's network-get-interfaces (VMs) or
    the container's interfaces: both use `hardware-address` / `ip-addresses`."""
    if isinstance(rows, dict):
        rows = rows.get("result")
    out: dict[str, list[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        m = mac(row.get("hardware-address")) or mac(row.get("hwaddr"))
        if not m:
            continue
        ips = []
        for a in row.get("ip-addresses") or []:
            ip, prefix = a.get("ip-address"), a.get("prefix")
            if ip and usable_ip(ip):
                ips.append(f"{ip}/{prefix}" if prefix is not None else ip)
        for key in ("inet", "inet6"):  # containers also give them as plain fields
            if row.get(key) and usable_ip(row[key]) and row[key] not in ips:
                ips.append(row[key])
        if ips:
            out.setdefault(m, []).extend(ip for ip in ips if ip not in out.get(m, []))
    return out


def netstat_counters(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, int], tuple[int | None, int | None]]:
    """Byte counters of each guest NIC, from the host's tap/veth devices.
    `in` is what the guest received, `out` what it sent."""
    out = {}
    for row in rows:
        dev = str(row.get("dev") or "")
        m = re.match(r"^(?:tap|veth)(\d+)i(\d+)$", dev)
        if m:
            vmid, idx = m.group(1), int(m.group(2))
        elif (m := re.match(r"^net(\d+)$", dev)) and row.get("vmid") is not None:
            vmid, idx = str(row["vmid"]), int(m.group(1))
        else:
            continue
        out[(vmid, idx)] = (counter(row.get("in")), counter(row.get("out")))
    return out


@dataclass
class Host:
    """What a guest needs to know about the node it runs on."""

    node: str
    mac: str | None
    ip: str | None


def guest_device(
    c: Client,
    kind: str,
    row: dict[str, Any],
    host: Host,
    counters: dict[tuple[str, int], tuple[int | None, int | None]],
    missing: list[str],
) -> Device | None:
    vmid = row.get("vmid")
    base = f"/nodes/{segment(host.node)}/{kind}/{segment(vmid)}"
    config = optional("guests", missing, lambda: c.get(base + "/config"), None)
    if not isinstance(config, dict):
        return None
    nics = guest_nics(config)
    if not nics:
        log.info("guest %s has no network device: not reported", vmid)
        return None
    if kind == "qemu" and agent_enabled(config):
        addrs = optional(
            "agent",
            missing,
            lambda: guest_addresses(c.get(base + "/agent/network-get-interfaces")),
            {},
        )
    elif kind == "lxc":
        addrs = optional(
            "guests", missing, lambda: guest_addresses(c.get(base + "/interfaces")), {}
        )
    else:
        addrs = {}
    for nic in nics:
        for ip in addrs.get(nic.mac, []):
            if ip not in nic.ips:
                nic.ips.append(ip)

    interfaces = []
    for nic in nics:
        rx, tx = counters.get((str(vmid), nic.index), (None, None))
        interfaces.append(
            Interface(
                name=f"net{nic.index}",
                description=nic.name,
                type="other",  # a virtual NIC: no physical jack
                mac=nic.mac,
                ips=nic.ips or None,
                up=nic.up,
                rx_bytes=rx,
                tx_bytes=tx,
            )
        )
    ips = plain_ips([ip for nic in nics for ip in nic.ips])
    uplink = next((n for n in nics if n.bridge), nics[0])
    neighbor = Neighbor(
        local_port=f"net{uplink.index}",
        protocol="other",
        remote_name=host.node,
        remote_port=uplink.bridge,
        remote_mac=host.mac,
        remote_ip=host.ip,
        remote_platform="Proxmox VE",
    )
    ostype = str(config.get("ostype") or "")
    disk = []
    if kind == "lxc" and counter(row.get("maxdisk")):
        disk = [
            Storage(
                mount="/",
                total_bytes=counter(row.get("maxdisk")),
                used_bytes=counter(row.get("disk")),
            )
        ]
    return Device(
        key=nics[0].mac,
        name=str(
            row.get("name") or config.get("name") or config.get("hostname") or f"{kind} {vmid}"
        ),
        host=next((ip for ip in ips if ":" not in ip), ips[0] if ips else None),
        role="server",
        model="LXC container" if kind == "lxc" else "QEMU/KVM virtual machine",
        os_version=(LXC_OS if kind == "lxc" else QEMU_OS).get(ostype),
        uptime_s=counter(row.get("uptime")),
        cpu_pct=fraction_pct(row.get("cpu")),
        mem_pct=pct(row.get("mem"), row.get("maxmem")),
        storage=disk or None,
        macs=sorted({n.mac for n in nics}),
        ips=ips or None,
        interfaces=interfaces,
        neighbors=[neighbor],
        **newer_fields(
            Device,
            cpu_count=counter(row.get("cpus") or row.get("maxcpu")) or None,
            mem_total_bytes=counter(row.get("maxmem")),
            mem_used_bytes=counter(row.get("mem")),
        ),
    )


def running_guests(c: Client, node: str, missing: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for kind in ("qemu", "lxc"):
        rows = optional("guests", missing, lambda k=kind: c.get(f"/nodes/{segment(node)}/{k}"), [])
        for row in sorted(rows or [], key=lambda r: counter(r.get("vmid")) or 0):
            if row.get("status") == "running" and not flag(row.get("template")):
                out.append((kind, row))
    return out


# --- a node and its guests ---------------------------------------------------


def node_devices(
    c: Client, node: str, ip_hint: str | None, missing: list[str], pool: ThreadPoolExecutor
) -> list[Device]:
    path = f"/nodes/{segment(node)}"
    status = optional("node", missing, lambda: c.get(path + "/status"), {}) or {}
    rows = optional("node", missing, lambda: c.get(path + "/network"), []) or []
    stores = optional("storage", missing, lambda: c.get(path + "/storage"), []) or []
    # Needs Sys.Modify, which PVEAuditor does not have: usually skipped, quietly.
    updates = optional("updates", missing, lambda: c.get(path + "/apt/update"), None, report=False)
    counters = netstat_counters(
        optional("node", missing, lambda: c.get(path + "/netstat"), []) or []
    )

    ifaces = node_interfaces(rows)
    by_name = {i.name: i for i in ifaces}
    mgmt = management(ifaces, rows, ip_hint)
    node_mac = physical_mac(mgmt, by_name)
    if not node_mac:
        log.info("node %s exposes no MAC address (no hwaddress in its network config)", node)
    mgmt_ip = ip_hint
    if not mgmt_ip and mgmt:
        mgmt_ip = next((ip.split("/")[0] for ip in mgmt.ips or [] if "." in ip), None)
    all_ips = plain_ips([ip for i in ifaces for ip in i.ips or []])
    memory, swap = status.get("memory") or {}, status.get("swap") or {}
    load = [n for n in (number(v) for v in status.get("loadavg") or []) if n is not None][:3]
    version = pve_version(status.get("pveversion"))
    cpuinfo = status.get("cpuinfo") or {}

    host_device = Device(
        key=node_mac or f"proxmox:{node}",
        name=node,
        host=mgmt_ip,
        role="server",
        vendor="Proxmox",
        model=cpuinfo.get("model") or None,
        os_version=f"Proxmox VE {version}" if version else None,
        uptime_s=counter(status.get("uptime")),
        cpu_pct=fraction_pct(status.get("cpu")),
        mem_pct=pct(memory.get("used"), memory.get("total")),
        swap_pct=pct(swap.get("used"), swap.get("total")),
        load_avg=load or None,
        storage=storage(status, stores) or None,
        firmware=firmware(version, updates),
        macs=sorted({i.mac for i in ifaces if i.mac}) or None,
        ips=all_ips or None,
        interfaces=ifaces or None,
        gateways=gateways(rows) or None,
        **newer_fields(
            Device,
            cpu_count=counter(cpuinfo.get("cpus")) or None,
            mem_total_bytes=counter(memory.get("total")),
            mem_used_bytes=counter(memory.get("used")),
        ),
    )
    host = Host(node=node, mac=node_mac, ip=mgmt_ip)
    guests = running_guests(c, node, missing)
    found = pool.map(lambda g: guest_device(c, g[0], g[1], host, counters, missing), guests)
    return [host_device, *(g for g in found if g is not None)]


def read_version(c: Client) -> dict[str, Any]:
    try:
        version = c.get("/version")
    except (NotFound, Forbidden, Unavailable) as e:
        raise PluginError(f"{c.base} does not look like Proxmox VE (no version API)") from e
    if not isinstance(version, dict):
        raise PluginError(f"{c.base} does not look like Proxmox VE (no version API)")
    return version


def cluster_nodes(c: Client, missing: list[str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The nodes (one for a single host) and the address of each one."""
    try:
        nodes = c.get("/nodes")
    except (NotFound, Forbidden, Unavailable) as e:
        raise PluginError(f"could not list the nodes of {c.base}") from e
    members = optional("node", missing, lambda: c.get("/cluster/status"), []) or []
    ips = {
        m["name"]: m["ip"]
        for m in members
        if m.get("type") == "node" and m.get("name") and m.get("ip")
    }
    return sorted(nodes or [], key=lambda n: str(n.get("node"))), ips


def connected_ip(c: Client) -> str | None:
    host = urlsplit(c.base).hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


# --- plugin entry points -------------------------------------------------------


# Proxmox VE filters guest lists by permission: a token without VM.Audit gets
# an empty list, not an error.
NO_GUESTS = (
    "No running VMs or containers visible: if there are some, the API token cannot "
    "see them (it needs VM.Audit: role PVEAuditor on / with Propagate, given to the "
    "token itself when privilege separation is on)"
)


def collect(cfg: Config) -> list[Device]:
    c = client_from(cfg)
    try:
        read_version(c)
        missing: list[str] = []
        nodes, ips = cluster_nodes(c, missing)
        devices: list[Device] = []
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for n in nodes:
                name = n.get("node")
                if not name:
                    continue
                if n.get("status") == "offline":
                    log.info("node %s is offline: skipped", name)
                    continue
                # A single host is reached at the address Omini connects to.
                hint = ips.get(name) or (connected_ip(c) if len(nodes) == 1 else None)
                try:
                    devices += node_devices(c, name, hint, missing, pool)
                except PluginError:
                    raise
                except Exception:  # one node failing never breaks the others
                    log.exception("could not read node %s", name)
        if not any(d.neighbors for d in devices):
            log.warning(NO_GUESTS)
        if missing:
            log.warning("missing privileges: %s", ", ".join(sorted(set(missing))))
        return devices
    finally:
        c.close()


def test(cfg: Config) -> str:
    c = client_from(cfg)
    try:
        version = read_version(c)
        missing: list[str] = []
        nodes, _ = cluster_nodes(c, missing)
        online = [n["node"] for n in nodes if n.get("node") and n.get("status") != "offline"]
        guests = 0
        for name in online:
            optional("node", missing, lambda n=name: c.get(f"/nodes/{segment(n)}/status"), None)
            guests += len(running_guests(c, name, missing))
        label = f"Proxmox VE {version.get('version') or version.get('release') or ''}".strip()
        msg = (
            f"Connected to {label}: {len(online)} node{'s' if len(online) != 1 else ''}, "
            f"{guests} running guest{'s' if guests != 1 else ''}"
        )
        if not guests:
            msg += ". " + NO_GUESTS
        if missing:
            msg += ". Missing privileges: " + "; ".join(sorted(set(missing)))
        return msg
    finally:
        c.close()
