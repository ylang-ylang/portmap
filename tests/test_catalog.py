import http.client
import json
import os
import socket
import struct
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from portmap.catalog import (
    CatalogServer,
    catalog_worktree_from_path,
    collect_catalog,
    compose_down_project,
    compose_restart_project,
    compose_up_worktree,
    container_to_service,
    discover_catalog_worktrees,
    enrich_services_from_worktrees,
    merge_catalog_history,
    parse_host_rule,
    select_dns_server,
    worktree_status_from_raw,
)
from portmap.errors import PortmapError
from portmap.web_static import read_static_asset


GENERATED_AT = "2026-10-12T00:00:00+00:00"


@pytest.fixture(autouse=True)
def missing_agent_socket(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_AGENT_SOCKET", str(tmp_path / "missing-agent.sock"))


@pytest.fixture(autouse=True)
def isolated_access_config(tmp_path: Path, monkeypatch) -> None:
    # Never observe a developer machine's real controller snapshot; tests
    # start from a valid controller-owned local-only snapshot and opt into
    # failure cases by repointing the environment variable.
    path = tmp_path / "catalog-access.json"
    staging = tmp_path / "catalog-access.json.tmp"
    staging.write_text(
        json.dumps(
            {
                "version": 1,
                "owner": "catalog-owner",
                "dns": {"address": "169.254.254.53", "port": 1053},
                "local": {"domain": "laptop.portmap", "http_port": 80, "enabled": False},
                "machines": {},
            }
        ),
        encoding="utf-8",
    )
    staging.replace(path)
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))


def request_catalog(
    path: str,
    *,
    method: str = "GET",
    body: str | None = None,
    scope: str = "private",
    host: str | None = None,
) -> tuple[int, http.client.HTTPMessage, bytes]:
    server = CatalogServer(("127.0.0.1", 0), scope=scope)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        headers = {"content-type": "application/x-www-form-urlencoded"} if body is not None else {}
        if host is not None:
            headers["Host"] = host
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@contextmanager
def serving(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def local_catalog(monkeypatch):
    """A deterministic local machine catalog without Docker or the agent."""
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)
    monkeypatch.setattr(
        "portmap.catalog.agent_worktrees",
        lambda: {"ok": True, "message": "agent ready", "generated_at": GENERATED_AT, "worktrees": []},
    )


@pytest.fixture
def access_config(tmp_path, monkeypatch):
    def configure(machines=None, *, local=None, owner="catalog-owner", dns=None) -> Path:
        document = {
            "version": 1,
            "owner": owner,
            "dns": dns if dns is not None else {"address": "169.254.254.53", "port": 1053},
            "local": local if local is not None else {"domain": "laptop.portmap", "http_port": 80, "enabled": False},
            "machines": machines if machines is not None else {},
        }
        path = tmp_path / "catalog-access.json"
        staging = tmp_path / "catalog-access.json.tmp"
        staging.write_text(json.dumps(document), encoding="utf-8")
        staging.replace(path)
        return path

    return configure


def container_dns(marker: str, *, host_enabled: bool = False, address: str = "127.0.0.1") -> dict:
    """A controller snapshot dns object with real container probe metadata."""
    return {
        "host_enabled": host_enabled,
        "address": "169.254.254.53" if host_enabled else None,
        "port": 1053 if host_enabled else None,
        "container_address": address,
        "marker": marker,
    }


class MarkerDns:
    """A real UDP listener answering marker TXT queries like the container DNS."""

    def __init__(self, marker: str) -> None:
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(("127.0.0.1", 0))
        self.marker = marker
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while True:
            try:
                packet, peer = self.socket.recvfrom(4096)
            except OSError:
                return
            ident = struct.unpack_from("!H", packet)[0]
            answer = packet[12:] + b"\xc0\x0c" + struct.pack(
                "!HHIH", 16, 1, 0, len(self.marker) + 1
            ) + bytes([len(self.marker)]) + self.marker.encode()
            self.socket.sendto(struct.pack("!6H", ident, 0x8000, 1, 1, 0, 0) + answer, peer)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.socket.close()

    @property
    def port(self) -> int:
        return self.socket.getsockname()[1]


def closed_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port



class RemoteGatewayHandler(BaseHTTPRequestHandler):
    """A real local HTTP fixture standing in for one remote portmap gateway."""

    def do_GET(self) -> None:
        if self.path != "/registry.json":
            self.send_error(404)
            return
        body = json.dumps(self.server.registry).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(length)
        self.server.posts.append(
            {"path": self.path, "host": self.headers.get("Host"), "body": body.decode("utf-8")}
        )
        payload = json.dumps({**self.server.result, "path": self.path}).encode("utf-8")
        self.send_response(self.server.status)
        if 300 <= self.server.status < 400:
            self.send_header("Location", "http://attacker.invalid/registry.json")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:
        pass


class RemoteGateway(ThreadingHTTPServer):
    """The remote side of one saved machine: registry and action routes."""

    def __init__(
        self,
        domain: str,
        *,
        via: str = "direct",
        http_port: int = 8080,
        dns_server: str | None = "172.19.0.2",
        http_address: str | None = None,
    ) -> None:
        self.domain = domain
        self.via = via
        self.http_port = http_port
        self.posts: list[dict] = []
        self.status = 200
        self.result = {"ok": True, "message": "remote executed the action"}
        self.registry = {
            "generated_at": GENERATED_AT,
            "dns_domain": domain,
            "http_port": http_port,
            "services": [
                {
                    "repo_name": "sample",
                    "branch": "dev",
                    "compose_project": "sample_dev",
                    "endpoints": [{"kind": "http", "name": "web", "url": f"http://app.{domain}:{http_port}/"}],
                }
            ],
            "worktrees": [
                {
                    "repo_name": "sample",
                    "branch": "dev",
                    "worktree": "/remote/sample@dev",
                    "worktree_status": "ok",
                    "compose_project": "sample_dev",
                }
            ],
        }
        if dns_server is not None:
            self.registry["dns_server"] = dns_server
        if http_address is not None:
            self.registry["http_address"] = http_address
        super().__init__(("127.0.0.1", 0), RemoteGatewayHandler)

    def machine(self, **changes) -> dict:
        entry = {
            "domain": self.domain,
            "via": self.via,
            "target": "192.0.2.10" if self.via == "direct" else "deploy@box",
            "http_port": 80 if self.via == "direct" else 18080,
            "backend_url": f"http://127.0.0.1:{self.server_port}",
            "tcp_address": "192.0.2.10",
            "tcp_ports": [],
            "include_tcp": False,
            "available": True,
        }
        entry.update(changes)
        return entry


def write_git_compose_repo(path: Path, *, with_compose: bool = True, with_endpoints: bool = True) -> None:
    path.mkdir(parents=True)
    run(["git", "init", "-b", "dev"], cwd=path)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=path)
    run(["git", "config", "user.name", "portmap test"], cwd=path)
    (path / "README.md").write_text("sample\n", encoding="utf-8")
    if with_compose:
        (path / "docker-compose.yml").write_text(
            """
services:
  frontend:
    image: python:3.12-alpine
    expose:
      - "8000"
""".lstrip(),
            encoding="utf-8",
        )
    if with_endpoints:
        (path / ".portmap").mkdir()
        (path / ".portmap" / "endpoints.toml").write_text(
            """
[endpoints.frontend]
kind = "http"
service = "frontend"
container_port = 8000
""".lstrip(),
            encoding="utf-8",
        )
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "init sample compose repo"], cwd=path)


def run(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    result = subprocess.run(
        args,
        cwd=cwd,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(args)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def host_collected_catalog() -> dict:
    """The host-side worktree pipeline served by the unix-socket agent."""
    from portmap import catalog as catalog_module

    containers = catalog_module.docker_get("/containers/json?all=0")
    services = [
        service
        for container in containers
        if (service := container_to_service(container)) is not None
    ]
    worktrees = discover_catalog_worktrees(services)
    worktrees = merge_catalog_history(worktrees, services, generated_at=GENERATED_AT)
    enrich_services_from_worktrees(services, worktrees)
    return {"services": services, "worktrees": worktrees}


def test_container_to_service_uses_portmap_labels() -> None:
    service = container_to_service(
        {
            "Names": ["/sample-frontend-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "frontend",
                "com.docker.compose.project.working_dir": "/tmp/sample",
                "traefik.enable": "true",
                "traefik.docker.network": "portmap_gateway",
                "portmap.managed": "true",
                "portmap.repo_id": "sample-repo",
                "portmap.repo_name": "sample",
                "portmap.branch": "feat-example",
                "portmap.worktree": "/repo/sample",
                "portmap.endpoints.sample-feat-example-frontend.name": "frontend",
                "portmap.endpoints.sample-feat-example-frontend.order": "0",
                "portmap.endpoints.sample-feat-example-frontend.kind": "http",
                "portmap.endpoints.sample-feat-example-frontend.container_port": "5173",
                "portmap.endpoints.sample-feat-example-frontend.url": (
                    "http://frontend.feat-example.sample.debug.lan:8080"
                ),
            },
        }
    )

    assert service is not None
    assert service["repo_name"] == "sample"
    assert service["branch"] == "feat-example"
    assert service["compose_service"] == "frontend"
    assert service["portmap_order"] == 0
    assert service["endpoints"] == [
        {
            "id": "sample-feat-example-frontend",
            "name": "frontend",
            "order": 0,
            "kind": "http",
            "container_port": 5173,
            "url": "http://frontend.feat-example.sample.debug.lan:8080",
        }
    ]


def test_container_to_service_orders_portmap_endpoints_by_toml_order() -> None:
    service = container_to_service(
        {
            "Names": ["/sample-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "sample",
                "portmap.branch": "dev",
                "portmap.worktree": "/repo/sample",
                "portmap.endpoints.sample-dev-zeta.name": "zeta",
                "portmap.endpoints.sample-dev-zeta.order": "2",
                "portmap.endpoints.sample-dev-zeta.kind": "http",
                "portmap.endpoints.sample-dev-zeta.container_port": "9002",
                "portmap.endpoints.sample-dev-alpha.name": "alpha",
                "portmap.endpoints.sample-dev-alpha.order": "0",
                "portmap.endpoints.sample-dev-alpha.kind": "http",
                "portmap.endpoints.sample-dev-alpha.container_port": "9000",
                "portmap.endpoints.sample-dev-beta.name": "beta",
                "portmap.endpoints.sample-dev-beta.order": "1",
                "portmap.endpoints.sample-dev-beta.kind": "http",
                "portmap.endpoints.sample-dev-beta.container_port": "9001",
            },
        }
    )

    assert service is not None
    assert service["portmap_order"] == 0
    assert [endpoint["name"] for endpoint in service["endpoints"]] == ["alpha", "beta", "zeta"]


def test_container_to_service_requires_host_confirmation_of_deletion(tmp_path: Path) -> None:
    missing_worktree = tmp_path / "deleted-worktree"

    service = container_to_service(
        {
            "Names": ["/sample-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "sample",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "sample",
                "portmap.branch": "feat-deleted",
                "portmap.worktree": str(missing_worktree),
                "portmap.endpoints.sample-feat-deleted-api.name": "api",
                "portmap.endpoints.sample-feat-deleted-api.order": "0",
                "portmap.endpoints.sample-feat-deleted-api.kind": "http",
                "portmap.endpoints.sample-feat-deleted-api.container_port": "9000",
            },
        }
    )

    assert service is not None
    assert service["worktree"] == str(missing_worktree)
    assert service["worktree_exists"] is None
    assert service["worktree_status"] == ""
    host_status = worktree_status_from_raw(str(missing_worktree))
    enrich_services_from_worktrees([service], [{"worktree": str(missing_worktree), **host_status}])
    assert service["worktree_exists"] is False
    assert service["worktree_status"] == "deleted"
    assert service["worktree_status_message"] == "worktree directory not found"
    assert service["worktree_root"] is None


def test_container_to_service_uses_host_submodule_metadata(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child_source = tmp_path / "browserdeck-source"
    submodule_path = parent / "modules" / "browserdeck"

    child_source.mkdir()
    run(["git", "init", "-b", "main"], cwd=child_source)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=child_source)
    run(["git", "config", "user.name", "portmap test"], cwd=child_source)
    (child_source / "README.md").write_text("child\n", encoding="utf-8")
    run(["git", "add", "."], cwd=child_source)
    run(["git", "commit", "-m", "init child"], cwd=child_source)

    parent.mkdir()
    run(["git", "init", "-b", "main"], cwd=parent)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=parent)
    run(["git", "config", "user.name", "portmap test"], cwd=parent)
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    run(["git", "add", "."], cwd=parent)
    run(["git", "commit", "-m", "init parent"], cwd=parent)
    run(["git", "-c", "protocol.file.allow=always", "submodule", "add", str(child_source), "modules/browserdeck"], cwd=parent)
    run(["git", "commit", "-m", "add browserdeck submodule"], cwd=parent)

    service = container_to_service(
        {
            "Names": ["/browserdeck-api-1"],
            "Image": "sample:latest",
            "Labels": {
                "com.docker.compose.project": "browserdeck",
                "com.docker.compose.service": "api",
                "traefik.enable": "true",
                "portmap.managed": "true",
                "portmap.repo_name": "browserdeck",
                "portmap.branch": "case-comap-frontend-debug",
                "portmap.worktree": str(submodule_path),
                "portmap.endpoints.browserdeck-case-api.name": "api",
                "portmap.endpoints.browserdeck-case-api.order": "0",
                "portmap.endpoints.browserdeck-case-api.kind": "http",
                "portmap.endpoints.browserdeck-case-api.container_port": "9000",
            },
        }
    )

    assert service is not None
    assert service["worktree_exists"] is None
    assert service["worktree_status"] == ""
    host_worktree = catalog_worktree_from_path(submodule_path, {str(submodule_path.resolve()): [service]})
    assert host_worktree is not None
    enrich_services_from_worktrees([service], [host_worktree])
    assert service["worktree_exists"] is True
    assert service["worktree_status"] == "submodule"
    assert service["worktree_superproject"] == str(parent.resolve())
    assert service["worktree_status_message"] == f"submodule under {parent.resolve()}"


def test_container_to_service_can_fallback_to_traefik_labels() -> None:
    service = container_to_service(
        {
            "Names": ["/legacy-frontend-1"],
            "Image": "legacy:latest",
            "Labels": {
                "com.docker.compose.project": "legacy",
                "com.docker.compose.service": "frontend",
                "traefik.enable": "true",
                "traefik.http.routers.legacy-frontend.rule": "Host(`frontend.legacy.debug.lan`)",
                "traefik.http.routers.legacy-frontend.entrypoints": "web",
                "traefik.http.routers.legacy-frontend.service": "legacy-frontend",
                "traefik.http.services.legacy-frontend.loadbalancer.server.port": "5173",
            },
        }
    )

    assert service is not None
    assert service["endpoints"][0]["host"] == "frontend.legacy.debug.lan"
    assert service["endpoints"][0]["container_port"] == 5173


def test_parse_host_rule() -> None:
    assert parse_host_rule("Host(`frontend.example.test`)") == "frontend.example.test"
    assert parse_host_rule("PathPrefix(`/`)") is None


def test_select_dns_server_prefers_external_bind_ip() -> None:
    external_bind = "external-bind"
    external_target = "external-target"

    assert select_dns_server(external_bind, external_target) == external_bind
    assert select_dns_server("0.0.0.0", external_target) == external_target
    assert select_dns_server("127.0.0.1", external_target) == external_target


def test_collect_catalog_omits_host_dns_when_the_controller_disables_it(local_catalog, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_HOST_DNS_ENABLED", "0")
    monkeypatch.setenv("PORTMAP_DNS_SERVER", "169.254.254.53")
    monkeypatch.setenv("PORTMAP_DNS_BIND", "192.0.2.7")
    monkeypatch.setenv("PORTMAP_DNS_TARGET_IP", "192.0.2.8")

    catalog = collect_catalog()

    # Disabled host DNS must not fall back to DNS_BIND/DNS_TARGET_IP and
    # advertise a DNS endpoint that nobody publishes.
    assert "dns_server" not in catalog
    assert catalog["dns_domain"]


def test_collect_catalog_advertises_the_host_dns_endpoint_when_enabled(local_catalog, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_HOST_DNS_ENABLED", "1")
    monkeypatch.setenv("PORTMAP_DNS_SERVER", "169.254.254.53")

    catalog = collect_catalog()

    assert catalog["dns_server"] == "169.254.254.53"


def test_collect_catalog_publishes_the_configured_http_address(local_catalog, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_HTTP_ADDRESS", "127.0.0.1")

    catalog = collect_catalog()

    assert catalog["http_address"] == "127.0.0.1"


def test_collect_catalog_rejects_an_unusable_http_address(local_catalog, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_HTTP_ADDRESS", "http://127.0.0.1:8080/")

    with pytest.raises(PortmapError):
        collect_catalog()


def test_collect_catalog_uses_agent_worktrees_to_enrich_running_services(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-frontend.name": "frontend",
                        "portmap.endpoints.sample-dev-frontend.order": "0",
                        "portmap.endpoints.sample-dev-frontend.kind": "http",
                        "portmap.endpoints.sample-dev-frontend.container_port": "5173",
                    },
                }
            ]
        raise AssertionError(path)

    def fake_agent_worktrees():
        return {
            "ok": True,
            "generated_at": "now",
            "worktrees": [
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "dev",
                    "worktree": "/repo/sample@dev",
                    "worktree_title": "sample@dev",
                    "worktree_root": "/repo/linked-git/sample.git",
                    "worktree_root_title": "sample linked .git",
                    "worktree_exists": True,
                    "worktree_status": "ok",
                    "worktree_status_message": "",
                    "worktree_superproject": None,
                    "compose_project": "sample_dev",
                    "running": True,
                    "startable": True,
                },
                {
                    "repo_id": "sample-repo",
                    "repo_name": "sample",
                    "branch": "feat-example",
                    "worktree": "/repo/sample@feat-example",
                    "worktree_title": "sample@feat-example",
                    "worktree_root": "/repo/linked-git/sample.git",
                    "worktree_root_title": "sample linked .git",
                    "compose_project": "sample_feat_example",
                    "running": False,
                    "startable": True,
                },
            ],
        }

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.agent_worktrees", fake_agent_worktrees)

    catalog = collect_catalog()

    assert catalog["agent"]["available"] is True
    assert len(catalog["worktrees"]) == 2
    assert catalog["services"][0]["worktree_root"] == "/repo/linked-git/sample.git"
    assert catalog["services"][0]["worktree_root_title"] == "sample linked .git"
    assert catalog["services"][0]["worktree_exists"] is True
    assert catalog["services"][0]["worktree_status"] == "ok"
    assert catalog["services"][0]["worktree_status_message"] == ""


def test_collect_catalog_orders_services_by_portmap_endpoint_order(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-worker-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "worker",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-worker.name": "worker",
                        "portmap.endpoints.sample-dev-worker.order": "2",
                        "portmap.endpoints.sample-dev-worker.kind": "http",
                        "portmap.endpoints.sample-dev-worker.container_port": "9002",
                    },
                },
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_id": "sample-repo",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": "/repo/sample@dev",
                        "portmap.endpoints.sample-dev-frontend.name": "frontend",
                        "portmap.endpoints.sample-dev-frontend.order": "0",
                        "portmap.endpoints.sample-dev-frontend.kind": "http",
                        "portmap.endpoints.sample-dev-frontend.container_port": "9000",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    catalog = collect_catalog()

    assert [service["compose_service"] for service in catalog["services"]] == ["frontend", "worker"]
    assert [service["portmap_order"] for service in catalog["services"]] == [0, 2]


def test_catalog_static_asset_rejects_unknown_paths() -> None:
    assert read_static_asset("../catalog.py") is None
    assert read_static_asset("assets/../catalog.py") is None
    assert read_static_asset("missing.js") is None


def test_catalog_http_serves_vite_public_root_static_assets() -> None:
    status, headers, body = request_catalog("/favicon.svg")

    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert b"pM" in body


def test_catalog_http_rejects_missing_public_root_static_assets() -> None:
    status, headers, body = request_catalog("/missing.svg")

    assert status == 404
    assert headers["content-type"] == "text/plain; charset=utf-8"
    assert body == b"not found\n"


@pytest.mark.parametrize("scope", ["public", "private"])
def test_project_skill_is_versioned_and_available_without_control_state(tmp_path, monkeypatch, scope) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "missing-access.json"))
    monkeypatch.setattr("portmap.catalog.__version__", "99.7.0")
    path = "/skills/project-integration/SKILL.md"
    status, headers, body = request_catalog(
        path + "?token=private-query-canary&machine=private-machine-canary",
        scope=scope,
        host="private-host-canary.example",
    )
    assert status == 200
    assert headers["content-type"] == "text/markdown; charset=utf-8"
    assert headers["cache-control"] == "no-cache"
    assert headers["x-content-type-options"] == "nosniff"
    assert int(headers["content-length"]) == len(body)
    metadata = body.decode("utf-8").split("---", 2)[1]
    assert 'portmap-version: "99.7.0"' in metadata
    assert b"{{PORTMAP_VERSION}}" not in body
    assert b"private-query-canary" not in body
    assert b"private-machine-canary" not in body
    assert b"private-host-canary" not in body
    head_status, head_headers, head_body = request_catalog(path, method="HEAD", scope=scope)
    assert head_status == 200
    assert head_body == b""
    assert head_headers["content-length"] == headers["content-length"]


@pytest.mark.parametrize("path", [
    "/skills/",
    "/skills/project-integration/../SKILL.md",
    "/skills/project-integration/%2e%2e/catalog.py",
    "/skills/unknown/SKILL.md",
])
def test_project_skill_does_not_expose_arbitrary_paths(path) -> None:
    status, _, body = request_catalog(path, scope="public")
    assert status == 404
    assert body == b"not found\n"


def test_host_worktrees_includes_running_worktree_and_writes_history(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))

    def fake_docker_get(path: str):
        if path == "/containers/json?all=0":
            return [
                {
                    "Names": ["/sample-frontend-1"],
                    "Image": "sample:latest",
                    "Labels": {
                        "com.docker.compose.project": "sample_dev",
                        "com.docker.compose.service": "frontend",
                        "traefik.enable": "true",
                        "portmap.managed": "true",
                        "portmap.repo_name": "sample",
                        "portmap.branch": "dev",
                        "portmap.worktree": str(repo),
                        "portmap.endpoints.frontend.name": "frontend",
                        "portmap.endpoints.frontend.kind": "http",
                        "portmap.endpoints.frontend.container_port": "8000",
                        "portmap.endpoints.frontend.url": "http://frontend.dev.sample.debug.lan:8080",
                    },
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    catalog = host_collected_catalog()

    assert catalog["services"][0]["worktree"] == str(repo)
    assert catalog["services"][0]["worktree_root"] == str(repo / ".git")
    assert catalog["worktrees"][0]["worktree"] == str(repo)
    assert catalog["worktrees"][0]["worktree_root"] == str(repo / ".git")
    assert catalog["worktrees"][0]["branch"] == "dev"
    assert catalog["worktrees"][0]["running"] is True
    assert catalog["worktrees"][0]["startable"] is True
    assert catalog["worktrees"][0]["source"] == "current"

    payload = json.loads(history.read_text(encoding="utf-8"))
    assert payload["worktrees"][0]["worktree"] == str(repo)
    assert payload["worktrees"][0]["worktree_root"] == str(repo / ".git")


def test_host_worktrees_shows_unconfigured_git_worktrees_only_under_explicit_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "projects@wt"
    repo = root / "sample@dev"
    outside = tmp_path / "outside@case"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo, with_compose=False, with_endpoints=False)
    run(["git", "worktree", "add", "-b", "case-outside", str(outside)], cwd=repo)
    run(["git", "branch", "not-checked-out"], cwd=repo)
    (root / "not-a-project").mkdir()
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(root))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [])

    catalog = host_collected_catalog()

    record, = catalog["worktrees"]
    assert record["worktree"] == str(repo)
    assert record["running"] is False
    assert record["startable"] is False
    assert record["start_error"] == "missing compose file; missing .portmap/endpoints.toml"
    assert record["source"] == "current"
    assert [entry["worktree"] for entry in json.loads(history.read_text())["worktrees"]] == [str(repo)]

    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", "")
    historical, = host_collected_catalog()["worktrees"]
    assert historical["worktree"] == str(repo)
    assert historical["source"] == "history"
    assert historical["startable"] is False


@pytest.mark.parametrize("contract", ["", "[endpoints]\n"])
def test_host_worktrees_disables_start_for_empty_endpoint_contract(
    tmp_path: Path, monkeypatch, contract: str,
) -> None:
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    (repo / ".portmap/endpoints.toml").write_text(contract, encoding="utf-8")
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))

    record, = discover_catalog_worktrees([])

    assert record["startable"] is False
    assert ".portmap/endpoints.toml" in record["start_error"]
    assert "endpoints" in record["start_error"]


def test_host_worktrees_orders_worktrees_by_branch_tip_descending(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    feat = tmp_path / "sample@feat-new"
    write_git_compose_repo(repo)

    (repo / "dev.txt").write_text("dev\n", encoding="utf-8")
    run(["git", "add", "dev.txt"], cwd=repo)
    run(
        ["git", "commit", "-m", "dev tip"],
        cwd=repo,
        env={
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        },
    )
    run(["git", "worktree", "add", "-b", "feat-new", str(feat)], cwd=repo)
    (feat / "feat.txt").write_text("feat\n", encoding="utf-8")
    run(["git", "add", "feat.txt"], cwd=feat)
    run(
        ["git", "commit", "-m", "feat tip"],
        cwd=feat,
        env={
            "GIT_AUTHOR_DATE": "2026-02-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-02-01T00:00:00+00:00",
        },
    )

    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = host_collected_catalog()

    assert [worktree["branch"] for worktree in catalog["worktrees"]] == ["feat-new", "dev"]
    assert catalog["worktrees"][0]["branch_tip_epoch"] > catalog["worktrees"][1]["branch_tip_epoch"]


def test_host_worktrees_discovers_startable_submodules_across_superproject_worktrees(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_source = tmp_path / "browserdeck-source"
    parent = tmp_path / "comap@dev"
    sibling = tmp_path / "comap@feat"
    submodule_relative = Path("3rdparty/browserdeck")

    write_git_compose_repo(child_source)

    parent.mkdir()
    run(["git", "init", "-b", "dev"], cwd=parent)
    run(["git", "config", "user.email", "portmap-test@example.invalid"], cwd=parent)
    run(["git", "config", "user.name", "portmap test"], cwd=parent)
    (parent / "README.md").write_text("parent\n", encoding="utf-8")
    run(["git", "add", "."], cwd=parent)
    run(["git", "commit", "-m", "init parent"], cwd=parent)
    run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(child_source),
            submodule_relative.as_posix(),
        ],
        cwd=parent,
    )
    run(["git", "commit", "-m", "add browserdeck submodule"], cwd=parent)
    run(["git", "worktree", "add", "-b", "feat-sibling", str(sibling)], cwd=parent)
    run(["git", "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive"], cwd=sibling)

    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(parent))
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = host_collected_catalog()

    submodule_worktrees = {
        worktree["worktree"]: worktree
        for worktree in catalog["worktrees"]
        if worktree["repo_name"] == "browserdeck"
    }
    expected_paths = {
        str((parent / submodule_relative).resolve()),
        str((sibling / submodule_relative).resolve()),
    }
    assert set(submodule_worktrees) == expected_paths
    assert {worktree["repo_id"] for worktree in submodule_worktrees.values()} == {
        next(iter(submodule_worktrees.values()))["repo_id"]
    }
    assert all(worktree["startable"] is True for worktree in submodule_worktrees.values())
    assert all(worktree["worktree_status"] == "submodule" for worktree in submodule_worktrees.values())
    assert {
        worktree["worktree_superproject"]
        for worktree in submodule_worktrees.values()
    } == {str(parent.resolve()), str(sibling.resolve())}
    assert {worktree["display_worktree_root"] for worktree in submodule_worktrees.values()} == {
        next(iter(submodule_worktrees.values()))["display_worktree_root"]
    }
    assert {worktree["display_worktree_root_title"] for worktree in submodule_worktrees.values()} == {
        "comap / 3rdparty/browserdeck"
    }
    parent_submodule = submodule_worktrees[str((parent / submodule_relative).resolve())]
    sibling_submodule = submodule_worktrees[str((sibling / submodule_relative).resolve())]
    assert parent_submodule["display_branch"] == parent_submodule["submodule_branch"]
    assert sibling_submodule["display_branch"] == sibling_submodule["submodule_branch"]
    assert parent_submodule["superproject_branch"] == "dev"
    assert sibling_submodule["superproject_branch"] == "feat-sibling"
    assert all(worktree["submodule_branch"] for worktree in submodule_worktrees.values())
    assert all(worktree["submodule_sha"] for worktree in submodule_worktrees.values())


def test_host_worktrees_restores_existing_worktree_from_history(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_id": "old",
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = host_collected_catalog()

    assert catalog["services"] == []
    assert catalog["worktrees"][0]["worktree"] == str(repo)
    assert catalog["worktrees"][0]["running"] is False
    assert catalog["worktrees"][0]["startable"] is True
    assert catalog["worktrees"][0]["source"] == "history"


def test_host_worktrees_drops_history_branches_not_checked_out(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    },
                    {
                        "repo_name": "sample",
                        "branch": "feat-old",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-02T00:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setenv("PORTMAP_CATALOG_WORKTREE_ROOTS", str(tmp_path))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = host_collected_catalog()

    by_branch = {worktree["branch"]: worktree for worktree in catalog["worktrees"]}
    assert set(by_branch) == {"dev"}
    assert by_branch["dev"]["source"] == "current"
    assert by_branch["dev"]["startable"] is True

    payload = json.loads(history.read_text(encoding="utf-8"))
    assert [entry["branch"] for entry in payload["worktrees"]] == ["dev"]


def test_host_worktrees_preserves_non_startable_history_with_reason(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    history = tmp_path / "history.json"
    write_git_compose_repo(repo, with_endpoints=False)
    history.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-01T00:00:00+00:00",
                "worktrees": [
                    {
                        "repo_name": "sample",
                        "branch": "dev",
                        "worktree": str(repo),
                        "worktree_title": repo.name,
                        "last_seen_at": "2026-06-01T00:00:00+00:00",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PORTMAP_CATALOG_HISTORY_FILE", str(history))
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    catalog = host_collected_catalog()

    assert catalog["services"] == []
    record, = catalog["worktrees"]
    assert record["worktree"] == str(repo)
    assert record["source"] == "history"
    assert record["startable"] is False
    assert record["start_error"] == "missing .portmap/endpoints.toml"


def test_catalog_compose_up_action_uses_agent(monkeypatch) -> None:
    calls = {}

    def fake_agent_compose_up(worktree: str):
        calls["worktree"] = worktree
        return {"ok": True, "message": "started by agent", "worktree": worktree}

    monkeypatch.setattr("portmap.catalog.agent_compose_up_worktree", fake_agent_compose_up)

    status, _, body = request_catalog(
        "/actions/compose-up",
        method="POST",
        body="worktree=%2Frepo%2Fsample%40dev",
    )

    payload = json.loads(body.decode("utf-8"))
    assert status == 200
    assert calls["worktree"] == "/repo/sample@dev"
    assert payload["message"] == "started by agent"


def test_compose_up_worktree_runs_generated_compose_command(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo)
    calls = {}
    real_run = subprocess.run

    def fake_ensure(args, *, cwd, environ):
        calls["ensure"] = (args, cwd, environ)

    def fake_plan(args, *, cwd):
        calls["plan"] = (args, cwd)
        return SimpleNamespace(
            command=["docker", "compose", "-f", str(repo / "docker-compose.yml"), "up", "-d"],
            injected=True,
            compose_project="sample_dev",
        )

    def fake_run(command, *, cwd, env=None, text=True, stdout=None, stderr=None, check=False):
        if command and command[0] == "git":
            return real_run(
                command,
                cwd=cwd,
                env=env,
                text=text,
                stdout=stdout,
                stderr=stderr,
                check=check,
            )
        calls["run"] = {
            "command": command,
            "cwd": cwd,
            "env": env,
            "text": text,
            "stdout": stdout,
            "stderr": stderr,
            "check": check,
        }
        return SimpleNamespace(returncode=0, stdout="started\n", stderr="")

    monkeypatch.setattr("portmap.catalog.ensure_generated_override", fake_ensure)
    monkeypatch.setattr("portmap.catalog.plan_docker_compose_command", fake_plan)
    monkeypatch.setattr("portmap.catalog.subprocess.run", fake_run)

    result = compose_up_worktree(str(repo))

    assert calls["ensure"][0] == ["up", "-d"]
    assert calls["ensure"][1] == repo
    assert calls["plan"] == (["up", "-d"], repo)
    assert calls["run"]["command"] == ["docker", "compose", "-f", str(repo / "docker-compose.yml"), "up", "-d"]
    assert calls["run"]["cwd"] == repo
    assert calls["run"]["env"]["PORTMAP_BROKER_BYPASS"] == "1"
    assert calls["run"]["env"]["DOCKER_HOST"].startswith("unix://")
    assert result["ok"] is True
    assert result["compose_project"] == "sample_dev"


def test_compose_up_worktree_rejects_non_startable_repo(tmp_path: Path) -> None:
    repo = tmp_path / "sample@dev"
    write_git_compose_repo(repo, with_endpoints=False)

    with pytest.raises(ValueError, match="missing .portmap/endpoints.toml"):
        compose_up_worktree(str(repo))


def test_compose_down_project_removes_portmap_managed_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                        "portmap.managed": "true",
                    },
                },
                {
                    "Id": "container-two",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "container-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                        "portmap.managed": "true",
                    },
                },
            ]
        if path == "/networks":
            return [
                {
                    "Id": "network-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "network-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                    },
                },
            ]
        raise AssertionError(path)

    calls = []

    def fake_docker_request(method: str, path: str, *, ok_statuses=None):
        calls.append((method, path, ok_statuses))
        return b""

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.docker_request", fake_docker_request)

    result = compose_down_project("sample_project")

    assert result["ok"] is True
    assert result["containers_removed"] == 2
    assert result["networks_removed"] == 1
    assert ("POST", "/containers/container-one/stop?t=10", {204, 304, 404, 409}) in calls
    assert ("DELETE", "/containers/container-one?v=0&force=1", {204, 404, 409}) in calls
    assert ("POST", "/containers/container-two/stop?t=10", {204, 304, 404, 409}) in calls
    assert ("DELETE", "/containers/container-two?v=0&force=1", {204, 404, 409}) in calls
    assert ("DELETE", "/networks/network-one", {204, 404}) in calls


def test_compose_down_project_rejects_unmanaged_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    with pytest.raises(ValueError, match="not portmap-managed"):
        compose_down_project("sample_project")


def test_compose_restart_project_restarts_portmap_managed_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                        "portmap.managed": "true",
                    },
                },
                {
                    "Id": "container-two",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
                {
                    "Id": "container-other",
                    "Labels": {
                        "com.docker.compose.project": "other_project",
                        "portmap.managed": "true",
                    },
                },
            ]
        raise AssertionError(path)

    calls = []

    def fake_docker_request(method: str, path: str, *, ok_statuses=None):
        calls.append((method, path, ok_statuses))
        return b""

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)
    monkeypatch.setattr("portmap.catalog.docker_request", fake_docker_request)

    result = compose_restart_project("sample_project")

    assert result["ok"] is True
    assert result["containers_restarted"] == 2
    assert ("POST", "/containers/container-one/restart?t=10", {204, 404}) in calls
    assert ("POST", "/containers/container-two/restart?t=10", {204, 404}) in calls


def test_compose_restart_project_rejects_unmanaged_project(monkeypatch) -> None:
    def fake_docker_get(path: str):
        if path == "/containers/json?all=1":
            return [
                {
                    "Id": "container-one",
                    "Labels": {
                        "com.docker.compose.project": "sample_project",
                    },
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr("portmap.catalog.docker_get", fake_docker_get)

    with pytest.raises(ValueError, match="not portmap-managed"):
        compose_restart_project("sample_project")


def test_collect_catalog_without_agent_returns_no_worktrees_and_never_scans_host(monkeypatch) -> None:
    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [] if path == "/containers/json?all=0" else None)

    def forbidden(*args, **kwargs):
        raise AssertionError("in-container worktree scanning must not run")

    monkeypatch.setattr("portmap.catalog.discover_catalog_worktrees", forbidden)
    monkeypatch.setattr("portmap.catalog.merge_catalog_history", forbidden)

    catalog = collect_catalog()

    assert catalog["worktrees"] == []
    assert catalog["agent"]["available"] is False
    assert catalog["agent"]["message"]


def test_api_machines_lists_local_and_saved_remotes(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote, serving(RemoteGateway("box.portmap", via="ssh")) as box:
        access_config(
            {
                "server.portmap": remote.machine(),
                "box.portmap": box.machine(available=False),
            }
        )

        status, headers, body = request_catalog("/api/machines")

    payload = json.loads(body)
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert [machine["id"] for machine in payload["machines"]] == ["local", "server.portmap", "box.portmap"]
    assert payload["selected"] == "local"
    local = payload["machines"][0]
    assert local["label"] == "laptop"
    assert local["domain"] == "laptop.portmap"
    assert local["target"] == "local"
    assert local["via"] == "local"
    assert local["catalog_url"] == "http://laptop.portmap/"
    assert local["available"] is True
    assert local["publishing"] is False
    unavailable = payload["machines"][2]
    assert unavailable["via"] == "ssh"
    assert unavailable["target"] == "deploy@box"
    assert unavailable["catalog_url"] == "http://box.portmap:18080/"
    assert unavailable["available"] is False
    # Public descriptors never carry private controller data.
    for private in (b"backend_url", b"tcp_address", b"tcp_ports", b"catalog-owner", b"127.0.0.1"):
        assert private not in body


def test_api_machines_reflects_local_publication_state(local_catalog, access_config) -> None:
    access_config(local={"domain": "laptop.portmap", "http_port": 8080, "enabled": True})

    status, _, body = request_catalog("/api/machines")

    local = json.loads(body)["machines"][0]
    assert status == 200
    assert local["publishing"] is True
    assert local["catalog_url"] == "http://laptop.portmap:8080/"


def test_api_machines_selects_saved_apex_host(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh")) as box:
        access_config({"box.portmap": box.machine()})

        status, _, body = request_catalog("/api/machines", host="box.portmap:18080")

    assert status == 200
    assert json.loads(body)["selected"] == "box.portmap"


def test_api_machines_public_scope_lists_only_local(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh")) as box:
        access_config({"box.portmap": box.machine()})

        status, _, body = request_catalog("/api/machines", scope="public", host="box.portmap")

    payload = json.loads(body)
    assert status == 200
    assert [machine["id"] for machine in payload["machines"]] == ["local"]
    assert payload["selected"] == "local"
    assert b"box.portmap" not in body


def test_api_machines_missing_snapshot_fails_closed(local_catalog, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    status, _, body = request_catalog("/api/machines")

    assert status == 503
    assert json.loads(body) == {"error": "catalog access snapshot is unavailable"}


def test_api_machines_invalid_snapshot_fails_closed(local_catalog, tmp_path, monkeypatch) -> None:
    path = tmp_path / "catalog-access.json"
    path.write_text("{not a snapshot", encoding="utf-8")
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))

    status, _, body = request_catalog("/api/machines")

    assert status == 503
    assert json.loads(body) == {"error": "catalog access snapshot is unavailable"}


def test_registry_local_machine_keeps_current_catalog_shape(local_catalog, access_config) -> None:
    access_config()

    status, _, body = request_catalog("/registry.json?machine=local")

    payload = json.loads(body)
    assert status == 200
    assert payload["machine"] == {
        "id": "local",
        "label": "laptop",
        "domain": "laptop.portmap",
        "target": "local",
        "via": "local",
        "catalog_url": "http://laptop.portmap/",
        "available": True,
        "publishing": False,
    }
    assert payload["services"] == []
    assert payload["agent"]["available"] is True


def test_registry_defaults_to_local_without_machine_parameter(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog("/registry.json", host="laptop.portmap")

    payload = json.loads(body)
    assert status == 200
    assert payload["machine"]["id"] == "local"


def test_registry_fetches_and_projects_direct_machine(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap", http_port=8080)) as remote:
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog("/registry.json?machine=server.portmap")

    payload = json.loads(body)
    assert status == 200
    assert payload["machine"]["id"] == "server.portmap"
    assert payload["machine"]["via"] == "direct"
    assert payload["dns_server"] == "169.254.254.53"
    assert payload["dns_port"] == 1053
    assert payload["client_access"]["transport"] == "direct"
    # Direct endpoints keep the remote URL exactly as the remote advertised it.
    endpoint = payload["services"][0]["endpoints"][0]
    assert endpoint["url"] == "http://app.server.portmap:8080/"
    assert endpoint["transport_supported"] is True
    assert payload["worktrees"] == remote.registry["worktrees"]
    assert b"127.0.0.1" not in body



def test_registry_serves_a_remote_machine_without_any_dns_endpoint(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap", http_port=8080, dns_server=None, http_address="198.51.100.7")) as remote:
        access_config({"server.portmap": remote.machine()}, dns=container_dns("catalog-owner:ready1"))

        status, _, body = request_catalog("/registry.json?machine=server.portmap")

    payload = json.loads(body)
    assert status == 200
    # A server-only publication serves HTTP with no DNS endpoint at all: the
    # remote advertised none and the controller has no host DNS to project.
    assert "dns_server" not in payload
    assert "dns_port" not in payload
    assert "http_address" not in payload
    assert payload["services"][0]["endpoints"][0]["transport_supported"] is True


def test_registry_direct_projection_advertises_the_verified_machine_address(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap", http_port=8080, http_address="198.51.100.7")) as remote:
        access_config({"server.portmap": remote.machine(address="192.0.2.10")})

        status, _, body = request_catalog("/registry.json?machine=server.portmap")

    payload = json.loads(body)
    assert status == 200
    # Direct clients may use the controller-verified machine address; the
    # remote's own unverified advertisement is replaced by it.
    assert payload["http_address"] == "192.0.2.10"


def test_registry_ssh_projection_keeps_the_virtual_host_origin(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh", http_port=8080, http_address="198.51.100.7")) as box:
        access_config({"box.portmap": box.machine()})

        status, _, body = request_catalog("/registry.json?machine=box.portmap")

    payload = json.loads(body)
    assert status == 200
    # SSH views are served through the local listener under a virtual Host;
    # a physical remote address would not be reachable and is not advertised.
    assert "http_address" not in payload


def test_registry_projects_ssh_machine_onto_shared_listener(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh", http_port=8080)) as box:
        access_config({"box.portmap": box.machine()})

        status, _, body = request_catalog("/registry.json?machine=box.portmap")

    payload = json.loads(body)
    assert status == 200
    assert payload["machine"]["id"] == "box.portmap"
    assert payload["client_access"]["transport"] == "ssh"
    assert payload["http_port"] == 18080
    endpoint = payload["services"][0]["endpoints"][0]
    assert endpoint["url"] == "http://app.box.portmap:18080/"
    assert endpoint["transport_supported"] is True


def test_registry_selects_machine_from_request_host(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap", http_port=8080)) as remote:
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog("/registry.json", host="server.portmap")

    payload = json.loads(body)
    assert status == 200
    assert payload["machine"]["id"] == "server.portmap"


def test_registry_unknown_machine_fails_without_local_fallback(local_catalog, access_config) -> None:
    access_config()

    status, _, body = request_catalog("/registry.json?machine=ghost.portmap")

    payload = json.loads(body)
    assert status == 404
    assert payload == {"error": "unknown machine"}
    assert "services" not in payload


def test_registry_duplicate_machine_parameter_fails(local_catalog, access_config) -> None:
    access_config()

    status, _, body = request_catalog("/registry.json?machine=local&machine=local")

    assert status == 400
    assert json.loads(body) == {"error": "duplicate machine parameter"}


def test_registry_empty_machine_parameter_fails(local_catalog, access_config) -> None:
    access_config()

    status, _, body = request_catalog("/registry.json?machine=")

    assert status == 400
    assert json.loads(body) == {"error": "empty machine parameter"}


def test_registry_unavailable_machine_fails_without_local_fallback(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh")) as box:
        entry = box.machine(available=False)
        entry.pop("backend_url")
        access_config({"box.portmap": entry})

        status, _, body = request_catalog("/registry.json?machine=box.portmap")

    payload = json.loads(body)
    assert status == 503
    assert payload == {"error": "machine box.portmap is unavailable; choose another machine"}
    assert "services" not in payload
    assert box.posts == []


def test_registry_missing_snapshot_fails_before_local_collection(local_catalog, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    def forbidden(path: str):
        raise AssertionError("local catalog must not be collected without access data")

    monkeypatch.setattr("portmap.catalog.docker_get", forbidden)

    status, _, body = request_catalog("/registry.json")

    assert status == 503
    assert json.loads(body) == {"error": "catalog access snapshot is unavailable"}


def test_registry_remote_backend_failure_is_sanitized(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        entry = remote.machine()
    access_config({"server.portmap": entry | {"backend_url": "http://127.0.0.1:1"}})

    status, _, body = request_catalog("/registry.json?machine=server.portmap")

    payload = json.loads(body)
    assert status == 502
    assert payload == {"error": "remote catalog unavailable; check portmap connections"}
    assert "127.0.0.1" not in body.decode("utf-8")
    assert "refused" not in body.decode("utf-8")


def test_registry_public_scope_denies_remote_machines(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        access_config({"server.portmap": remote.machine()})

        denied = request_catalog("/registry.json?machine=server.portmap", scope="public")
        local = request_catalog("/registry.json?machine=local", scope="public")

    assert denied[0] == 404
    assert json.loads(denied[2]) == {"error": "unknown machine"}
    assert local[0] == 200
    assert json.loads(local[2])["machine"]["id"] == "local"
    assert remote.posts == []






def test_actions_unknown_machine_fails_before_forwarding(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog("/actions/compose-down?machine=ghost.portmap", method="POST", body="compose_project=x")

    assert status == 404
    assert json.loads(body) == {"error": "unknown machine"}
    assert remote.posts == []


def test_actions_public_scope_denies_remote_machines(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog(
            "/actions/compose-up?machine=server.portmap", scope="public", method="POST", body="worktree=%2Fw"
        )

    assert status == 404
    assert json.loads(body) == {"error": "unknown machine"}
    assert remote.posts == []


def test_actions_unavailable_machine_fails_before_forwarding(local_catalog, access_config) -> None:
    with serving(RemoteGateway("box.portmap", via="ssh")) as box:
        access_config({"box.portmap": box.machine(available=False)})

        status, _, body = request_catalog(
            "/actions/compose-up?machine=box.portmap", method="POST", body="worktree=%2Fw"
        )

    assert status == 503
    assert json.loads(body) == {"error": "machine box.portmap is unavailable; choose another machine"}
    assert box.posts == []


def test_actions_remote_failure_is_sanitized(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        entry = remote.machine()
    access_config({"server.portmap": entry | {"backend_url": "http://127.0.0.1:1"}})

    status, _, body = request_catalog("/actions/compose-up?machine=server.portmap", method="POST", body="worktree=%2Fw")

    payload = json.loads(body)
    assert status == 502
    assert payload == {"error": "remote action failed; check the remote machine"}
    assert "127.0.0.1" not in body.decode("utf-8")
    assert "refused" not in body.decode("utf-8")


def test_actions_do_not_follow_remote_redirects(local_catalog, access_config) -> None:
    with serving(RemoteGateway("server.portmap")) as remote:
        remote.status = 302
        access_config({"server.portmap": remote.machine()})

        status, _, body = request_catalog(
            "/actions/compose-up?machine=server.portmap", method="POST", body="worktree=%2Fw"
        )

    assert status == 502
    assert json.loads(body) == {"error": "remote action failed; check the remote machine"}
    assert remote.posts[0]["path"] == "/actions/compose-up"


def test_actions_missing_snapshot_fails_before_mutation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    def forbidden(*args, **kwargs):
        raise AssertionError("no mutation without controller access data")

    monkeypatch.setattr("portmap.catalog.agent_compose_up_worktree", forbidden)

    status, _, body = request_catalog("/actions/compose-up", method="POST", body="worktree=%2Fw")

    assert status == 503
    assert json.loads(body) == {"error": "catalog access snapshot is unavailable"}


def test_catalog_compose_up_without_agent_has_no_in_container_fallback(monkeypatch) -> None:
    def forbidden(worktree: str):
        raise AssertionError("in-container Compose execution must not run")

    monkeypatch.setattr("portmap.catalog.compose_up_worktree", forbidden)

    status, _, body = request_catalog("/actions/compose-up", method="POST", body="worktree=%2Frepo%2Fsample%40dev")

    payload = json.loads(body)
    assert status == 400
    assert payload["ok"] is False
    assert payload["worktree"] == "/repo/sample@dev"
    assert "agent unavailable" in payload["message"]


@pytest.mark.parametrize("host_enabled", [False, True])
def test_healthz_private_reports_owner_once_the_container_dns_matches(
    local_catalog, access_config, monkeypatch, host_enabled
) -> None:
    # The gate always verifies the container DNS marker, whether or not the
    # controller additionally publishes host DNS.
    with MarkerDns("catalog-owner:ready1") as dns:
        monkeypatch.setattr("portmap.catalog.READY_DNS_PORT", dns.port)
        access_config(dns=container_dns("catalog-owner:ready1", host_enabled=host_enabled))

        status, headers, body = request_catalog("/healthz")

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"ok": True, "owner": "catalog-owner", "dns_marker": "catalog-owner:ready1"}


def test_healthz_private_fails_on_a_mismatched_container_dns_marker(
    local_catalog, access_config, monkeypatch
) -> None:
    with MarkerDns("someone-else:ready1") as dns:
        monkeypatch.setattr("portmap.catalog.READY_DNS_PORT", dns.port)
        access_config(dns=container_dns("catalog-owner:ready1"))

        status, _, body = request_catalog("/healthz")

    assert status == 503
    assert json.loads(body) == {"ok": False}


def test_healthz_private_fails_when_the_container_dns_bridge_is_unreachable(
    local_catalog, access_config, monkeypatch
) -> None:
    monkeypatch.setattr("portmap.catalog.READY_DNS_PORT", closed_udp_port())
    access_config(dns=container_dns("catalog-owner:ready1"))

    status, _, body = request_catalog("/healthz")

    assert status == 503
    assert json.loads(body) == {"ok": False}


def test_healthz_private_never_claims_ready_without_probe_metadata(
    local_catalog, access_config, monkeypatch
) -> None:
    # A legacy snapshot without container probe data cannot be verified, and
    # readiness must fail closed before reaching for a probe target.
    def no_probe(*args, **kwargs):
        raise AssertionError("readiness probed DNS without snapshot probe metadata")

    monkeypatch.setattr("portmap.catalog.probe_marker", no_probe)
    access_config()

    status, _, body = request_catalog("/healthz")

    assert status == 503
    assert json.loads(body) == {"ok": False}


def test_healthz_public_stays_plain_with_a_ready_private_gate(
    local_catalog, access_config, monkeypatch
) -> None:
    with MarkerDns("catalog-owner:ready1") as dns:
        monkeypatch.setattr("portmap.catalog.READY_DNS_PORT", dns.port)
        access_config(dns=container_dns("catalog-owner:ready1"))

        status, headers, body = request_catalog("/readyz", scope="public")

    # The public scope keeps ordinary liveness; it can neither invoke the
    # private readiness gate nor observe its owner or marker data.
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert body == b"ok\n"


def test_healthz_public_stays_plain(local_catalog, access_config) -> None:
    access_config()

    status, headers, body = request_catalog("/healthz", scope="public")

    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert body == b"ok\n"


def test_healthz_private_fails_closed_without_valid_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    status, _, body = request_catalog("/healthz")

    assert status == 503
    assert json.loads(body) == {"ok": False}

    path = tmp_path / "catalog-access.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(path))

    status, _, body = request_catalog("/readyz")

    assert status == 503
    assert json.loads(body) == {"ok": False}


def test_healthz_public_stays_plain_without_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTMAP_ACCESS_CONFIG", str(tmp_path / "absent.json"))

    status, headers, body = request_catalog("/healthz", scope="public")

    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert body == b"ok\n"


@pytest.mark.parametrize(
    ("agent_state", "label_key", "catalog_visible"),
    [
        ("empty", "portmap.worktree", False),
        ("empty", "com.docker.compose.project.working_dir", True),
        ("unavailable", "portmap.worktree", False),
        ("unmatched", "com.docker.compose.project.working_dir", False),
        ("partial", "portmap.worktree", False),
    ],
)
def test_catalog_preserves_unknown_host_path_state(
    tmp_path: Path, monkeypatch, agent_state: str, label_key: str, catalog_visible: bool
) -> None:
    from portmap.agent_client import AgentUnavailable

    host_path = tmp_path / "voxmani"
    host_path.mkdir()
    assert host_path.exists() and not (host_path / ".git").exists()
    container = {
        "Names": ["/voxmani-app-1"],
        "Image": "app:latest",
        "Labels": {
            "portmap.managed": "true",
            "portmap.repo_name": "voxmani",
            "portmap.branch": "dev",
            label_key: str(host_path),
            "com.docker.compose.project": "voxmani_dev",
            "com.docker.compose.service": "app",
            "portmap.endpoints.voxmani-dev-app.name": "app",
            "portmap.endpoints.voxmani-dev-app.kind": "http",
            "portmap.endpoints.voxmani-dev-app.container_port": "8000",
        },
    }
    host_exists = Path.exists
    if not catalog_visible:
        # The same absolute path is absent from the catalog's namespace.
        monkeypatch.setattr(Path, "exists", lambda path: False if path == host_path else host_exists(path))

    def agent_worktrees():
        if agent_state == "unavailable":
            raise AgentUnavailable("host agent unavailable")
        if agent_state == "unmatched":
            return {"worktrees": [{
                "worktree": str(tmp_path / "different-project"),
                "worktree_exists": False,
                "worktree_status": "deleted",
            }]}
        if agent_state == "partial":
            return {"worktrees": [{"worktree": str(host_path), "branch": "dev"}]}
        return {"worktrees": []}

    monkeypatch.setattr("portmap.catalog.docker_get", lambda path: [container])
    monkeypatch.setattr("portmap.catalog.agent_worktrees", agent_worktrees)
    catalog = collect_catalog()
    assert catalog["agent"]["available"] is (agent_state != "unavailable")
    service, = catalog["services"]
    assert service["worktree"] == str(host_path)
    assert service["worktree_exists"] is None
    assert service["worktree_status"] == ""
    assert service["worktree_status_message"] == ""
    assert service["worktree_root"] is None
    assert service["worktree_superproject"] is None
    assert service["compose_project"] == "voxmani_dev"
    assert service["compose_service"] == "app"
    assert service["endpoints"][0]["kind"] == "http"
    assert service["endpoints"][0]["container_port"] == 8000
