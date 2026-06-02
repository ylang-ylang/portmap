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
from .repo_identity import RepoIdentity, git_branch, resolve_repo_identity
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
    entrypoint: str | None = None
    protocol: str = "tcp"
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
        (out_dir / "registry.json").unlink(missing_ok=True)
        traefik_file = out_dir / "traefik.generated.yml"
        if self.traefik_static.get("entryPoints"):
            traefik_file.write_text(dump_yaml(self.traefik_static), encoding="utf-8")
        else:
            traefik_file.unlink(missing_ok=True)


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
    allocator = PortAllocator(request.out_dir / "state.json", host_ip=request.host_ip)
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
    else:
        allocator.state_file.unlink(missing_ok=True)
    compose_override = build_compose_override(
        planned,
        compose=compose,
        gateway_network=request.gateway_network,
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
        compose_project=compose.name,
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
            labels=labels,
        )

    protocol = "udp" if endpoint.kind == EndpointKind.UDP else "tcp"
    port_start = request.udp_port_start if protocol == "udp" else request.tcp_port_start
    allocation_key = f"{identity.repo_id}:{branch}:{endpoint.name}:{protocol}"
    host_port = allocator.allocate(
        protocol=protocol,
        key=allocation_key,
        preferred=endpoint.host_port,
        start=port_start,
    )
    entrypoint = route_id(endpoint.name, identity.display_name, branch)
    if protocol == "udp":
        labels += (
            f"traefik.udp.routers.{router}.entrypoints={entrypoint}",
            f"traefik.udp.routers.{router}.service={traefik_service}",
            f"traefik.udp.services.{traefik_service}.loadbalancer.server.port={endpoint.container_port}",
        )
    else:
        labels += (
            f"traefik.tcp.routers.{router}.entrypoints={entrypoint}",
            f"traefik.tcp.routers.{router}.rule=HostSNI(`*`)",
            f"traefik.tcp.routers.{router}.service={traefik_service}",
            f"traefik.tcp.services.{traefik_service}.loadbalancer.server.port={endpoint.container_port}",
        )
    labels += portmap_endpoint_labels(
        router=router,
        endpoint=endpoint,
        fields={
            "host": request.host_ip,
            "host_port": str(host_port),
            "entrypoint": entrypoint,
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
        entrypoint=entrypoint,
        protocol=protocol,
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


def build_compose_override(
    planned: tuple[PlannedEndpoint, ...],
    *,
    compose: ComposeConfig,
    gateway_network: str,
) -> dict[str, Any]:
    services: dict[str, Any] = {}
    for endpoint in planned:
        service_config = services.setdefault(endpoint.service, {})
        labels = service_config.setdefault("labels", [])
        for label in endpoint.labels:
            if label not in labels:
                labels.append(label)
        service_config["networks"] = service_networks_with_gateway(
            compose=compose,
            service_name=endpoint.service,
            gateway_network=gateway_network,
        )
    return {
        "services": services,
        "networks": {
            gateway_network: {
                "external": True,
                "name": gateway_network,
            }
        },
    }


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
    entrypoints: dict[str, Any] = {}
    for endpoint in planned:
        if endpoint.kind == EndpointKind.HTTP or endpoint.entrypoint is None or endpoint.host_port is None:
            continue
        suffix = "/udp" if endpoint.kind == EndpointKind.UDP else "/tcp"
        entrypoints[endpoint.entrypoint] = {"address": f":{endpoint.host_port}{suffix}"}
    return {"entryPoints": entrypoints}


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
                "entrypoint": endpoint.entrypoint,
            }

    return {
        "repos": {
            identity.repo_id: {
                "display_name": identity.display_name,
                "instances": {
                    branch: {
                        "worktree": str(request.project_directory.resolve()),
                        "compose_project": compose.name,
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
