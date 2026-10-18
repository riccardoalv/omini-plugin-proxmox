import json

from omini_sdk import ConfigBackup

from omini_proxmox.collect import backup, collect


def by_device(backups):
    return {b.device: b for b in backups}


def test_one_backup_per_device_collect_reports(pve, cfg):
    backups = by_device(backup(cfg))
    assert set(backups) == {d.key for d in collect(cfg)}
    assert all(b.format == "json" for b in backups.values())


def test_node_network_without_live_state(pve, cfg):
    node = json.loads(by_device(backup(cfg))["bc:24:11:00:00:0a"].content)
    assert node["node"] == "pve1"
    rows = node["network"]
    assert [r["iface"] for r in rows] == sorted(r["iface"] for r in rows)
    assert not any({"active", "exists", "priority"} & set(r) for r in rows)
    bond = next(r for r in rows if r["iface"] == "bond0")
    assert (bond["slaves"], bond["bond_mode"]) == ("enp2s0 enp3s0", "802.3ad")


def test_guest_config_without_secrets(pve, cfg):
    backups = by_device(backup(cfg))
    vm = json.loads(backups["bc:24:11:aa:00:01"].content)
    assert (vm["type"], vm["vmid"]) == ("qemu", 100)
    assert vm["config"]["net1"] == "virtio=BC:24:11:AA:00:02,bridge=vmbr1,tag=20,link_down=1"
    assert vm["config"]["ciuser"] == "admin"
    assert not {"digest", "cipassword", "sshkeys", "lock"} & set(vm["config"])
    text = backups["bc:24:11:aa:00:01"].content
    assert "$5$" not in text and "ssh-ed25519" not in text
    ct = json.loads(backups["bc:24:11:bb:00:01"].content)
    assert (ct["type"], ct["vmid"], ct["config"]["hostname"]) == ("lxc", 200, "jellyfin")


def test_backups_are_deterministic(pve, cfg):
    first = backup(cfg)
    # Live state changes (a port goes down, a backup job locks the VM, the
    # guest's config digest is recomputed): the backup does not.
    rows = pve.routes["/nodes/pve1/network"]["data"]
    rows[2]["active"] = 1
    rows.reverse()
    vm = pve.routes["/nodes/pve1/qemu/100/config"]["data"]
    vm["lock"] = "backup"
    vm["digest"] = "ffff"
    assert backup(cfg) == first
    # A configuration change does.
    vm["memory"] = "8192"
    second = by_device(backup(cfg))
    assert second["bc:24:11:aa:00:01"] != by_device(first)["bc:24:11:aa:00:01"]
    assert second["bc:24:11:00:00:0a"] == by_device(first)["bc:24:11:00:00:0a"]


def test_a_missing_privilege_skips_only_that_part(pve, cfg):
    pve.errors["/nodes/pve1/network"] = 403
    backups = by_device(backup(cfg))
    # Without the network, the node's key is unknown: no node backup.
    assert set(backups) == {"bc:24:11:aa:00:01", "bc:24:11:bb:00:01"}


def test_backup_never_writes(pve, cfg):
    backup(cfg)
    assert pve.methods == {"GET"}


def test_the_plugin_registers_backup():
    import main

    assert main.plugin._backup is backup


def test_backups_are_sdk_models(pve, cfg):
    assert all(isinstance(b, ConfigBackup) for b in backup(cfg))
