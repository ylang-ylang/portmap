import json
from pathlib import Path

import pytest

from portmap.catalog_access import (
    LOCAL_MACHINE_ID,
    host_selected_machine_id,
    load_access_snapshot,
    local_descriptor,
    machine_descriptor,
    machines_payload,
    parse_access_snapshot,
)
from portmap.errors import PortmapError


def contract_document(**changes) -> dict:
    document = {
        "version": 1,
        "owner": "opaque-owner-token",
        "dns": {"address": "169.254.254.53", "port": 1053},
        "local": {"domain": "laptop.portmap", "http_port": 80, "enabled": False},
        "machines": {
            "server.portmap": {
                "domain": "server.portmap",
                "via": "direct",
                "target": "192.0.2.10",
                "address": "192.0.2.10",
                "http_port": 80,
                "remote_http_port": 80,
                "backend_url": "http://192.0.2.10:80",
                "tcp_address": "192.0.2.10",
                "tcp_ports": [],
                "include_tcp": False,
                "available": True,
            },
            "box.portmap": {
                "domain": "box.portmap",
                "via": "ssh",
                "target": "deploy@box",
                "http_port": 18080,
                "backend_url": "http://10.88.0.7:39081",
                "tcp_address": "127.77.0.2",
                "tcp_ports": [18000, 18001],
                "include_tcp": True,
                "available": False,
            },
        },
    }
    document.update(changes)
    return document


def write_snapshot(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "catalog-access.json"
    staging = tmp_path / "catalog-access.json.tmp"
    staging.write_text(json.dumps(document), encoding="utf-8")
    staging.replace(path)
    return path


def test_parse_access_snapshot_accepts_contract_shape() -> None:
    snapshot = parse_access_snapshot(contract_document())

    assert snapshot.owner == "opaque-owner-token"
    assert snapshot.dns_address == "169.254.254.53"
    assert snapshot.dns_port == 1053
    assert snapshot.local.domain == "laptop.portmap"
    assert snapshot.local.enabled is False
    assert list(snapshot.machines) == ["server.portmap", "box.portmap"]

    direct = snapshot.machines["server.portmap"]
    assert (direct.via, direct.target, direct.available) == ("direct", "192.0.2.10", True)
    assert direct.backend_url == "http://192.0.2.10:80"

    ssh = snapshot.machines["box.portmap"]
    assert (ssh.via, ssh.available) == ("ssh", False)
    assert ssh.tcp_ports == (18000, 18001)
    assert ssh.profile() == {
        "domain": "box.portmap",
        "via": "ssh",
        "target": "deploy@box",
        "tcp_address": "127.77.0.2",
        "tcp_ports": [18000, 18001],
    }


def test_load_access_snapshot_reads_environment_path(tmp_path: Path, monkeypatch) -> None:
    path = write_snapshot(tmp_path, contract_document())
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))

    snapshot = load_access_snapshot()

    assert list(snapshot.machines) == ["server.portmap", "box.portmap"]


def test_load_access_snapshot_reloads_replaced_documents(tmp_path: Path, monkeypatch) -> None:
    path = write_snapshot(tmp_path, contract_document())
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))
    assert load_access_snapshot().machines

    write_snapshot(tmp_path, contract_document(machines={}))
    assert load_access_snapshot().machines == {}


def test_missing_snapshot_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    with pytest.raises(PortmapError, match="catalog access snapshot is unavailable"):
        load_access_snapshot()


def test_invalid_snapshot_content_fails_closed(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "catalog-access.json"
    path.write_text("{not a snapshot", encoding="utf-8")
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))

    with pytest.raises(PortmapError, match="catalog access snapshot is invalid"):
        load_access_snapshot()


@pytest.mark.parametrize(
    "document",
    [
        "not-an-object",
        contract_document(version=2),
        contract_document(owner=""),
        contract_document(dns={"address": "not-an-ip", "port": 1053}),
        contract_document(dns={"address": "169.254.254.53", "port": "53"}),
        contract_document(local={"domain": "laptop.portmap", "http_port": 80, "enabled": "yes"}),
        contract_document(machines={"server.portmap": {"domain": "other.portmap", "via": "direct", "http_port": 80, "backend_url": "http://192.0.2.10:80", "available": True}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"via": "vpn"}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"http_port": 0}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"backend_url": "https://192.0.2.10:80"}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"backend_url": "http://192.0.2.10:80/path"}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"backend_url": "http://user:pass@192.0.2.10:80"}}),
        contract_document(machines={"server.portmap": {key: value for key, value in contract_document()["machines"]["server.portmap"].items() if key != "backend_url"}}),
        contract_document(machines={"server.portmap": contract_document()["machines"]["server.portmap"] | {"available": "true"}}),
        contract_document(machines={"laptop.portmap": {"domain": "laptop.portmap", "via": "ssh", "http_port": 80, "backend_url": "http://10.0.0.9:80", "available": True}}),
    ],
)
def test_invalid_documents_are_rejected(document) -> None:
    with pytest.raises(PortmapError):
        parse_access_snapshot(document)


def test_duplicate_json_keys_are_rejected(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "catalog-access.json"
    document = json.dumps(contract_document())
    path.write_text(document[:-1] + "," + document[document.index('"box.portmap"'):], encoding="utf-8")
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))

    # A duplicated machine key must not silently alias another machine.
    with pytest.raises(PortmapError, match="catalog access snapshot is invalid"):
        load_access_snapshot()


def test_unavailable_machine_may_omit_backend_origin() -> None:
    document = contract_document()
    del document["machines"]["box.portmap"]["backend_url"]

    snapshot = parse_access_snapshot(document)

    box = snapshot.machines["box.portmap"]
    assert box.available is False
    assert box.backend_url is None
    assert box.http_port == 18080
    assert box.tcp_ports == (18000, 18001)


def test_machine_descriptor_hides_backend_and_tunnel_details() -> None:
    snapshot = parse_access_snapshot(contract_document())

    descriptor = machine_descriptor(snapshot.machines["box.portmap"])

    assert descriptor == {
        "id": "box.portmap",
        "label": "box",
        "domain": "box.portmap",
        "target": "deploy@box",
        "via": "ssh",
        "catalog_url": "http://box.portmap:18080/",
        "available": False,
    }


def test_local_descriptor_reflects_saved_publication_state() -> None:
    snapshot = parse_access_snapshot(contract_document())

    descriptor = local_descriptor(snapshot)
    publishing = local_descriptor(parse_access_snapshot(contract_document(local={"domain": "laptop.portmap", "http_port": 8080, "enabled": True})))

    assert descriptor == {
        "id": "local",
        "label": "laptop",
        "domain": "laptop.portmap",
        "target": "local",
        "via": "local",
        "catalog_url": "http://laptop.portmap/",
        "available": True,
        "publishing": False,
    }
    assert publishing["publishing"] is True
    assert publishing["catalog_url"] == "http://laptop.portmap:8080/"


def test_machines_payload_keeps_private_machines_out_of_public_scope() -> None:
    snapshot = parse_access_snapshot(contract_document())

    private = machines_payload(snapshot, selected=LOCAL_MACHINE_ID, private=True)
    public = machines_payload(snapshot, selected=LOCAL_MACHINE_ID, private=False)

    assert [machine["id"] for machine in private["machines"]] == ["local", "server.portmap", "box.portmap"]
    assert [machine["id"] for machine in public["machines"]] == ["local"]


def test_host_selection_prefers_saved_machines() -> None:
    snapshot = parse_access_snapshot(contract_document())

    assert host_selected_machine_id(snapshot, "box.portmap") == "box.portmap"
    assert host_selected_machine_id(snapshot, "BOX.PORTMAP") == LOCAL_MACHINE_ID
    assert host_selected_machine_id(snapshot, "laptop.portmap") == LOCAL_MACHINE_ID
    assert host_selected_machine_id(snapshot, "unregistered.portmap") == LOCAL_MACHINE_ID
    assert host_selected_machine_id(snapshot, None) == LOCAL_MACHINE_ID
