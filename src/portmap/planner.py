from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compose import load_compose_config
from .config import load_endpoint_declarations
from .errors import ComposeError
from .model import ComposeConfig, EndpointDeclaration, EndpointKind, GenerateRequest
from .ports import PortAllocator
from .repo_identity import RepoIdentity, git_branch, resolve_repo_identity, stable_hash
from .slug import route_id, slugify
from .yaml_writer import dump_yaml


@dataclass(frozen=True)
class PlannedEndpoint:
    name: str
    kind: EndpointKind
    service: str
    container_port: int
    router_name: str
    service_name: str
    host: str | None = None
    url: str | None = None
    host_ip: str | None = None
    host_port: int | None = None
    range_start: int | None = None
    range_end: int | None = None
    entrypoint: str | None = None
    protocol: str | None = None
    ports: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedPlan:
    compose_override: dict[str, Any]
    traefik_static: dict[str, Any]
    registry: dict[str, Any]
    endpoints: tuple[PlannedEndpoint, ...]
    repo_identity: RepoIdentity
    branch: str
    compose_project: str | None

    def write(self, out_dir: Path) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "docker-compose.override.generated.yml").write_text(
            dump_yaml(self.compose_override),
            encoding="utf-8",
        )
        state_file = out_dir / "state.json"
        state_file.write_text(json.dumps(self.metadata(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out_dir / "registry.json").unlink(missing_ok=True)
        (out_dir / "traefik.generated.yml").unlink(missing_ok=True)

    def metadata(self) -> dict[str, Any]:
        return {
            "version": 1,
            "repo_id": self.repo_identity.repo_id,
            "repo_name": self.repo_identity.display_name,
            "branch": self.branch,
            "compose_project": self.compose_project,
            "endpoints": {
                endpoint.name: endpoint_state(endpoint)
                for endpoint in self.endpoints
            },
        }


def generate_plan(request: GenerateRequest) -> GeneratedPlan:
    compose = load_compose_config(
        compose_file=request.compose_file,
        compose_json_file=request.compose_json_file,
        project_directory=request.project_directory,
    )
    endpoints = load_endpoint_declarations(request.config_file)
    identity = resolve_repo_identity(
        project_directory=request.project_directory,
        repo_root=request.repo_root,
        repo_id=request.repo_id,
        repo_name=request.repo_name,
    )
    branch = slugify(request.branch or git_branch(identity.git_root or request.project_directory) or "detached")
    compose_project = request.compose_project or default_compose_project(identity, branch, request.project_directory)
    local_allocation_state = request.allocation_state_file is None
    allocator = PortAllocator(request.allocation_state_file or request.out_dir / "allocations.json", host_ip=request.host_ip)
    with allocator:
        planned = tuple(
            plan_endpoint(
                endpoint=endpoint,
                compose=compose,
                identity=identity,
                branch=branch,
                request=request,
                allocator=allocator,
            )
            for endpoint in endpoints
        )
        if any(endpoint.kind != EndpointKind.HTTP for endpoint in planned):
            allocator.save()
        elif local_allocation_state and allocator.state_file is not None:
            allocator.state_file.unlink(missing_ok=True)
    compose_override = build_compose_override(
        planned,
        compose=compose,
        gateway_network=request.gateway_network,
        container_dns_server=request.container_dns_server,
    )
    traefik_static = build_traefik_static(planned)
    registry = build_registry(
        planned=planned,
        identity=identity,
        branch=branch,
        compose=compose,
        request=request,
    )
    return GeneratedPlan(
        compose_override=compose_override,
        traefik_static=traefik_static,
        registry=registry,
        endpoints=planned,
        repo_identity=identity,
        branch=branch,
        compose_project=compose_project,
    )


def plan_endpoint(
    *,
    endpoint: EndpointDeclaration,
    compose: ComposeConfig,
    identity: RepoIdentity,
    branch: str,
    request: GenerateRequest,
    allocator: PortAllocator,
) -> PlannedEndpoint:
    if endpoint.service not in compose.services:
        raise ComposeError(f"endpoint {endpoint.name!r} references unknown service: {endpoint.service}")

    instance = instance_slug(identity=identity, branch=branch, project_directory=request.project_directory)
    router = route_id(identity.display_name, branch, endpoint.name)
    traefik_service = router
    service = compose.services[endpoint.service]
    if service.network_mode == "host":
        raise ComposeError(f"service uses network_mode=host and cannot be managed: {endpoint.service}")
    network = request.gateway_network
    labels = base_labels(network) + portmap_service_labels(identity, branch, request)

    if endpoint.kind == EndpointKind.HTTP:
        host = endpoint.host or default_http_host(endpoint.name, branch, identity.display_name, request.domain_suffix)
        url = f"http://{host}:{request.http_port}"
        middleware_labels = http_host_middleware_labels(router, endpoint)
        labels += (
            f"traefik.http.routers.{router}.entrypoints=web",
            f"traefik.http.routers.{router}.rule=Host(`{host}`)",
            f"traefik.http.routers.{router}.service={traefik_service}",
            f"traefik.http.services.{traefik_service}.loadbalancer.server.port={endpoint.container_port}",
            *middleware_labels,
            *portmap_endpoint_labels(
                router=router,
                endpoint=endpoint,
                fields={
                    "host": host,
                    "url": url,
                    "preserve_host": str(endpoint.preserve_host).lower(),
                    "upstream_host": upstream_host_header(endpoint),
                },
            ),
        )
        return PlannedEndpoint(
            name=endpoint.name,
            kind=endpoint.kind,
            service=endpoint.service,
            container_port=endpoint.container_port,
            router_name=router,
            service_name=traefik_service,
            host=host,
            url=url,
            host_port=request.http_port,
            labels=labels,
        )

    if endpoint.kind == EndpointKind.RANGE:
        labels = portmap_service_labels(identity, branch, request)
        protocol = endpoint.protocol or "udp"
        entry_host_port = allocator.allocate(
            protocol=protocol,
            key=f"{instance}:{endpoint.name}:{protocol}:entry",
            preferred=endpoint.host_port,
            start=request.udp_port_start if protocol == "udp" else request.tcp_port_start,
        )
        range_size = endpoint.range_size or 1
        range_start, range_end = allocator.allocate_range(
            protocol=protocol,
            key=f"{instance}:{endpoint.name}:{protocol}:range",
            preferred_start=endpoint.range_start,
            start=request.range_port_start,
            size=range_size,
        )
        labels += portmap_endpoint_labels(
            router=router,
            endpoint=endpoint,
            fields={
                "host": request.host_ip,
                "host_port": str(entry_host_port),
                "protocol": protocol,
                "range_start": str(range_start),
                "range_end": str(range_end),
                "range_size": str(range_size),
            },
        )
        return PlannedEndpoint(
            name=endpoint.name,
            kind=endpoint.kind,
            service=endpoint.service,
            container_port=endpoint.container_port,
            router_name=router,
            service_name=traefik_service,
            host_ip=request.host_ip,
            host_port=entry_host_port,
            range_start=range_start,
            range_end=range_end,
            protocol=protocol,
            ports=(
                f"{entry_host_port}:{endpoint.container_port}/{protocol}",
                f"{range_start}-{range_end}:{range_start}-{range_end}/{protocol}",
            ),
            labels=labels,
        )

    protocol = "udp" if endpoint.kind == EndpointKind.UDP else "tcp"
    labels = portmap_service_labels(identity, branch, request)
    port_start = request.udp_port_start if protocol == "udp" else request.tcp_port_start
    allocation_key = f"{instance}:{endpoint.name}:{protocol}"
    host_port = allocator.allocate(
        protocol=protocol,
        key=allocation_key,
        preferred=endpoint.host_port,
        start=port_start,
    )
    labels += portmap_endpoint_labels(
        router=router,
        endpoint=endpoint,
        fields={
            "host": request.host_ip,
            "host_port": str(host_port),
            "protocol": protocol,
        },
    )
    return PlannedEndpoint(
        name=endpoint.name,
        kind=endpoint.kind,
        service=endpoint.service,
        container_port=endpoint.container_port,
        router_name=router,
        service_name=traefik_service,
        host_ip=request.host_ip,
        host_port=host_port,
        protocol=protocol,
        ports=(f"{host_port}:{endpoint.container_port}/{protocol}",),
        labels=labels,
    )


def base_labels(network: str) -> tuple[str, ...]:
    return (
        "traefik.enable=true",
        f"traefik.docker.network={network}",
    )


def http_host_middleware_labels(router: str, endpoint: EndpointDeclaration) -> tuple[str, ...]:
    if endpoint.preserve_host:
        return ()
    middleware = f"{router}-host"
    return (
        f"traefik.http.routers.{router}.middlewares={middleware}",
        f"traefik.http.middlewares.{middleware}.headers.customrequestheaders.Host={upstream_host_header(endpoint)}",
    )


def upstream_host_header(endpoint: EndpointDeclaration) -> str:
    return endpoint.upstream_host or f"127.0.0.1:{endpoint.container_port}"


def portmap_service_labels(identity: RepoIdentity, branch: str, request: GenerateRequest) -> tuple[str, ...]:
    return (
        "portmap.managed=true",
        f"portmap.repo_id={identity.repo_id}",
        f"portmap.repo_name={identity.display_name}",
        f"portmap.branch={branch}",
        f"portmap.worktree={request.project_directory.resolve()}",
    )


def portmap_endpoint_labels(
    *,
    router: str,
    endpoint: EndpointDeclaration,
    fields: dict[str, str],
) -> tuple[str, ...]:
    base = {
        "name": endpoint.name,
        "kind": endpoint.kind.value,
        "service": endpoint.service,
        "container_port": str(endpoint.container_port),
    }
    base.update(fields)
    return tuple(f"portmap.endpoints.{router}.{key}={value}" for key, value in base.items())


def default_http_host(endpoint: str, branch: str, repo_name: str, domain_suffix: str) -> str:
    return ".".join(
        [
            slugify(endpoint),
            slugify(branch),
            slugify(repo_name),
            domain_suffix.strip("."),
        ]
    )


def endpoint_state(endpoint: PlannedEndpoint) -> dict[str, Any]:
    state: dict[str, Any] = {
        "kind": endpoint.kind.value,
        "service": endpoint.service,
        "container_port": endpoint.container_port,
    }
    if endpoint.host is not None:
        state["host"] = endpoint.host
    if endpoint.url is not None:
        state["url"] = endpoint.url
    if endpoint.host_ip is not None:
        state["host"] = endpoint.host_ip
    if endpoint.host_port is not None:
        state["host_port"] = endpoint.host_port
    if endpoint.protocol is not None:
        state["protocol"] = endpoint.protocol
    if endpoint.range_start is not None and endpoint.range_end is not None:
        state["range"] = {
            "min_port": endpoint.range_start,
            "max_port": endpoint.range_end,
        }
    return state


def instance_slug(*, identity: RepoIdentity, branch: str, project_directory: Path) -> str:
    worktree_hash = stable_hash(str(project_directory.resolve()))[:8]
    return route_id(identity.repo_id, branch, worktree_hash)


def default_compose_project(identity: RepoIdentity, branch: str, project_directory: Path) -> str:
    worktree_hash = stable_hash(str(project_directory.resolve()))[:8]
    value = route_id(identity.display_name, branch, identity.repo_id[:8], worktree_hash)
    normalized = value.replace("-", "_")
    if not normalized[0].isalnum():
        normalized = f"p_{normalized}"
    return normalized[:63].rstrip("_-") or "portmap_project"


def build_compose_override(
    planned: tuple[PlannedEndpoint, ...],
    *,
    compose: ComposeConfig,
    gateway_network: str,
    container_dns_server: str | None = None,
) -> dict[str, Any]:
    services: dict[str, Any] = {}
    managed_service_names = tuple(dict.fromkeys(endpoint.service for endpoint in planned))
    gateway_service_names = {
        endpoint.service
        for endpoint in planned
        if endpoint.kind == EndpointKind.HTTP
    }
    environment = portmap_environment(planned)
    for endpoint in planned:
        service_config = services.setdefault(endpoint.service, {})
        labels = service_config.setdefault("labels", [])
        for label in endpoint.labels:
            if label not in labels:
                labels.append(label)
        if endpoint.ports:
            ports = service_config.setdefault("ports", [])
            for port in endpoint.ports:
                if port not in ports:
                    ports.append(port)
        if endpoint.service in gateway_service_names:
            service_config["networks"] = service_networks_with_gateway(
                compose=compose,
                service_name=endpoint.service,
                gateway_network=gateway_network,
            )
    for service_name in managed_service_names:
        service_config = services.setdefault(service_name, {})
        if container_dns_server:
            dns = service_config.setdefault("dns", [])
            if container_dns_server not in dns:
                dns.append(container_dns_server)
        service_environment = service_config.setdefault("environment", {})
        service_environment.update(environment)
    override: dict[str, Any] = {
        "services": services,
    }
    if gateway_service_names:
        override["networks"] = {
            gateway_network: {
                "external": True,
                "name": gateway_network,
            }
        }
    return override


def portmap_environment(planned: tuple[PlannedEndpoint, ...]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for endpoint in planned:
        prefix = f"PORTMAP_{env_name(endpoint.name)}"
        environment[f"{prefix}_KIND"] = endpoint.kind.value
        environment[f"{prefix}_SERVICE"] = endpoint.service
        environment[f"{prefix}_CONTAINER_PORT"] = str(endpoint.container_port)
        if endpoint.url:
            environment[f"{prefix}_URL"] = endpoint.url
        if endpoint.host:
            environment[f"{prefix}_HOST"] = endpoint.host
        if endpoint.host_ip:
            environment[f"{prefix}_HOST"] = endpoint.host_ip
        if endpoint.host_port is not None:
            environment[f"{prefix}_PORT"] = str(endpoint.host_port)
        if endpoint.protocol:
            environment[f"{prefix}_PROTOCOL"] = endpoint.protocol
        if endpoint.range_start is not None:
            environment[f"{prefix}_RANGE_MIN_PORT"] = str(endpoint.range_start)
        if endpoint.range_end is not None:
            environment[f"{prefix}_RANGE_MAX_PORT"] = str(endpoint.range_end)
        if endpoint.range_start is not None and endpoint.range_end is not None:
            environment[f"{prefix}_RANGE_SIZE"] = str(endpoint.range_end - endpoint.range_start + 1)
    return environment


def env_name(value: str) -> str:
    return slugify(value).replace("-", "_").upper()


def service_networks_with_gateway(
    *,
    compose: ComposeConfig,
    service_name: str,
    gateway_network: str,
) -> dict[str, Any]:
    service = compose.services[service_name]
    network_keys = list(service.networks or ("default",))
    if gateway_network not in network_keys:
        network_keys.append(gateway_network)
    return {network_key: None for network_key in network_keys}


def build_traefik_static(planned: tuple[PlannedEndpoint, ...]) -> dict[str, Any]:
    return {"entryPoints": {}}


def build_registry(
    *,
    planned: tuple[PlannedEndpoint, ...],
    identity: RepoIdentity,
    branch: str,
    compose: ComposeConfig,
    request: GenerateRequest,
) -> dict[str, Any]:
    endpoint_entries: dict[str, Any] = {}
    for endpoint in planned:
        if endpoint.kind == EndpointKind.HTTP:
            endpoint_entries[endpoint.name] = {
                "kind": endpoint.kind.value,
                "service": endpoint.service,
                "container_port": endpoint.container_port,
                "url": endpoint.url,
                "host": endpoint.host,
            }
        else:
            endpoint_entries[endpoint.name] = {
                "kind": endpoint.kind.value,
                "service": endpoint.service,
                "container_port": endpoint.container_port,
                "host": endpoint.host_ip,
                "port": endpoint.host_port,
            }
            if endpoint.entrypoint is not None:
                endpoint_entries[endpoint.name]["entrypoint"] = endpoint.entrypoint
            if endpoint.range_start is not None and endpoint.range_end is not None:
                endpoint_entries[endpoint.name]["range"] = {
                    "min_port": endpoint.range_start,
                    "max_port": endpoint.range_end,
                }

    return {
        "repos": {
            identity.repo_id: {
                "display_name": identity.display_name,
                "instances": {
                    branch: {
                        "worktree": str(request.project_directory.resolve()),
                        "compose_project": request.compose_project or default_compose_project(identity, branch, request.project_directory),
                        "endpoints": endpoint_entries,
                    }
                },
            }
        }
    }


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"repos": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"repos": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("repos"), dict):
        return {"repos": {}}
    return payload


def merge_registry(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    repos = dict(result.get("repos", {}))
    for repo_id, repo_payload in update.get("repos", {}).items():
        current_repo = dict(repos.get(repo_id, {}))
        current_repo["display_name"] = repo_payload.get("display_name", current_repo.get("display_name"))
        instances = dict(current_repo.get("instances", {}))
        instances.update(repo_payload.get("instances", {}))
        current_repo["instances"] = instances
        repos[repo_id] = current_repo
    result["repos"] = repos
    return result
