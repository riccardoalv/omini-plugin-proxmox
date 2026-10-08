# omini-plugin-proxmox

[Omini](https://github.com/riccardoalv/omini) plugin for **Proxmox VE**, through its official REST API with a read-only API token. It reads a single host or a whole cluster, and reports every running VM and container as its own device, **placed under the node that runs it**.

Read-only: it only sends `GET` requests, and never starts, stops, changes or refreshes anything (not even the package list).

> **Experimental.** This plugin was built from the official API documentation and tested against recorded answers shaped like the documented ones, not on a real Proxmox VE host yet. Reports from real hosts are welcome.

## What it reads

| Data | Used for |
|---|---|
| Nodes of the cluster (or the single host) and their addresses | One device per node |
| Per node: CPU (% and number of CPUs), memory (% and used of installed), swap, load, uptime, Proxmox VE version, CPU model | Node health |
| Per node: network interfaces — NICs, bridges, bonds, VLANs, with status, members, IPs, comments, default gateway | Ports of the node |
| Per node: storage usage (root filesystem + each active storage) | Node health (disks) |
| Per node: pending updates — only if the token may read them (see below) | Node health |
| Running VMs (QEMU) and containers (LXC): name, CPU (% and vCPUs), memory (assigned; % and used for containers only — a VM's memory as the host sees it includes the guest's caches, so it is not reported as use), uptime, OS type, root disk (containers) | One device per guest, with its own health in the device panel |
| Each guest's network devices (`net0`…`netN`): MAC, bridge, link up/down | Guest MACs, and which node it hangs under |
| Guest IPs: QEMU guest agent (VMs), container interfaces (LXC), static IPs in the config | Guest addresses |
| Byte counters of each guest NIC (the host's `tap`/`veth` devices) | Guest traffic |

Stopped guests and templates are not reported. Works with Proxmox VE 7 and later (8.x and 9.x documented).

### How guests are placed

Each running guest is reported as a device (role `server`, key = the MAC of its first NIC) with a neighbor of protocol `other`:

```json
{"local_port": "net0", "protocol": "other", "remote_name": "pve1", "remote_port": "vmbr0",
 "remote_mac": "<the node's MAC, when known>", "remote_ip": "192.168.1.10", "remote_platform": "Proxmox VE"}
```

so Omini draws it under the node that runs it, on the bridge it is attached to, even with several Proxmox hosts on the network.

## Install

**Integrations → Add → Proxmox VE** (when it is in Omini's plugin catalog), or install it from its address, `https://github.com/riccardoalv/omini-plugin-proxmox`.

## Create a read-only API token

Use a dedicated user and an API token with the built-in **PVEAuditor** role (read-only) on `/`, with **privilege separation** on — the token then has only the permissions given to it, never more than its user. See [User Management → API tokens](https://pve.proxmox.com/pve-docs/chapter-pveum.html#pveum_tokens) in the Proxmox VE docs.

From the shell of any node:

```bash
pveum user add omini@pve --comment "Omini (read-only)"
pveum acl modify / --users omini@pve --roles PVEAuditor
pveum user token add omini@pve omini --privsep 1
pveum acl modify / --tokens 'omini@pve!omini' --roles PVEAuditor
```

The third command prints the token **secret** once: copy it.

Or in the web interface:

1. **Datacenter → Permissions → Users → Add**: user `omini`, realm *Proxmox VE authentication server* (`pve`). No password is needed.
2. **Datacenter → Permissions → Add → User Permission**: path `/`, user `omini@pve`, role **PVEAuditor**, *Propagate* on.
3. **Datacenter → Permissions → API Tokens → Add**: user `omini@pve`, token ID `omini`, **Privilege Separation** checked. Copy the secret shown.
4. **Datacenter → Permissions → Add → API Token Permission**: path `/`, token `omini@pve!omini`, role **PVEAuditor**, *Propagate* on. (With privilege separation, a token without its own permission sees nothing.)

PVEAuditor on `/` gives what the plugin needs: `Sys.Audit` (node status, network, cluster), `VM.Audit` (guests and their configuration), `Datastore.Audit` (storage). **Test connection** in Omini lists any missing privilege.

### Optional extras

- **Guest IPs of VMs** come from the QEMU guest agent (installed and enabled in the VM). The API asks for `VM.GuestAgent.Audit` (Proxmox VE 9; check that your PVEAuditor role has it, else add a role with it on `/vms`). Without it — or without the agent — VMs are still placed under their node, only without IPs; Omini's network scan usually finds them by MAC.
- **Pending updates**: the update list (`GET /nodes/{node}/apt/update`, only read, never refreshed) requires `Sys.Modify`, which also allows changing a node's network and services. It is **not** recommended for Omini, and PVEAuditor does not have it: the node then shows its version only.

## Form fields

| Field | Example | Notes |
|---|---|---|
| Address | `https://192.168.1.10:8006` | Any node of a cluster: the others are read through it. Port 8006 is used when none is given (write `:443` explicitly behind a reverse proxy). |
| API token ID | `omini@pve!omini` | `user@realm!token` |
| API token secret | `6f6a4f3c-…` | Stored encrypted by Omini, never logged |
| Verify the TLS certificate | off | Proxmox VE uses a self-signed certificate by default |

## How it works

Every request is a `GET` to `https://<host>:8006/api2/json/…` with the header `Authorization: PVEAPIToken=<user>@<realm>!<tokenid>=<secret>` ([Proxmox VE API](https://pve.proxmox.com/wiki/Proxmox_VE_API), [API viewer](https://pve.proxmox.com/pve-docs/api-viewer/)). Guests are read a few at a time; each request times out after 10 s; one node or guest failing never fails the others.

| Endpoint | Privilege | Read for |
|---|---|---|
| [`/version`](https://pve.proxmox.com/pve-docs/api-viewer/#/version) | any | Checks it is Proxmox VE |
| [`/nodes`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes) | any | Nodes and whether they are online (offline nodes are skipped) |
| [`/cluster/status`](https://pve.proxmox.com/pve-docs/api-viewer/#/cluster/status) | Sys.Audit | Address of each node (also on a single host) |
| [`/nodes/{node}/status`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/status) | Sys.Audit | `cpu`, `memory`, `swap`, `rootfs`, `loadavg`, `uptime`, `pveversion`, `cpuinfo.model` |
| [`/nodes/{node}/network`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/network) | any | `iface`, `type` (eth/bridge/bond/vlan/OVS…), `active`, `bridge_ports`, `slaves`, `vlan-raw-device`, `vlan-id`, `cidr`/`cidr6`, `gateway`, `comments`, `options` (`hwaddress`) |
| [`/nodes/{node}/storage`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/storage) | Datastore.Audit | `total` / `used` of each active storage |
| [`/nodes/{node}/apt/update`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/apt/update) (GET only) | Sys.Modify (optional) | Pending updates already known to the node |
| [`/nodes/{node}/netstat`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/netstat) | Sys.Audit | Byte counters of each guest NIC (`in` = received by the guest) |
| [`/nodes/{node}/qemu`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/qemu), [`/nodes/{node}/lxc`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/lxc) | VM.Audit | Guests: `vmid`, `name`, `status`, `template`, `cpu`, `mem`/`maxmem`, `uptime`, `disk`/`maxdisk` |
| [`/nodes/{node}/qemu/{vmid}/config`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/qemu/{vmid}/config), [`/nodes/{node}/lxc/{vmid}/config`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/lxc/{vmid}/config) | VM.Audit | `net[n]` (`virtio=MAC,bridge=vmbr0,tag=…,link_down=…` / `name=eth0,hwaddr=MAC,bridge=…,ip=…`), `ostype`, `agent` |
| [`/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces) | VM.GuestAgent.Audit | VM IPs, matched to NICs by MAC (only when the agent is enabled in the config) |
| [`/nodes/{node}/lxc/{vmid}/interfaces`](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/lxc/{vmid}/interfaces) | VM.Audit | Container IPs |

CPU of nodes and guests is reported by the API as a fraction (0–1) and turned into a percentage; guest CPU is relative to the guest's own vCPUs.

## Known limitations

- **No MAC addresses for the node's own NICs.** The API does not expose them. The plugin uses a `hwaddress` line from the node's `/etc/network/interfaces` when there is one (on the management bridge, else on the physical port behind it); otherwise the node's key is `proxmox:<node name>` and its guests point to it by IP and name. Adding `hwaddress <MAC of the port>` to `vmbr0` (described in the [Proxmox VE 7 upgrade guide](https://pve.proxmox.com/wiki/Upgrade_from_6.x_to_7.0#Linux_Bridge_MAC-Address_Change)) makes the node match the MAC Omini's network scan sees. Not verified on a real host: that the API returns this line among `options`.
- **No node traffic counters, link speeds or temperatures**: the API only offers averaged rates (RRD), which are not counters, and no sensors. They are left empty, never estimated.
- **Physical disks** (`/nodes/{node}/disks/list`) are not read: their SMART health and wear have no place in Omini's data model yet; storage usage is reported instead.
- **Default gateway** is reported as a gateway with status `unknown` (Proxmox VE does not monitor it); the node is a server, so its uplink is not drawn as a WAN.
- **Guests without a network device** are not reported (nothing to place them by). Guest VLAN tags are not reported yet.
- A guest that moves to another node (migration) moves with the next collection.

## Development

The plugin uses [uv](https://docs.astral.sh/uv/) and expects the Omini repository next to it (the SDK is in `../omini/sdk/python`):

```bash
uv run pytest          # tests, against answers shaped like the API docs (tests/fixtures)
uv run ruff check .    # lint
uv run ruff format .   # format
```

Run it from a local Omini without installing it: `OMINI_PLUGIN_DIRS=../omini-plugin-proxmox make run` in the Omini repository; every collection uses the current code.

## License

MIT
