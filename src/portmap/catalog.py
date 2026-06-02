from __future__ import annotations

import datetime as dt
import html
import http.client
import json
import os
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


DOCKER_SOCKET = os.environ.get("PORTMAP_DOCKER_SOCKET", "/var/run/docker.sock")
HTTP_PORT = int(os.environ.get("PORTMAP_HTTP_PORT", "8080"))
DNS_DOMAIN = os.environ.get("PORTMAP_DNS_DOMAIN", "debug.lan").strip(".")
DNS_BIND = os.environ.get("PORTMAP_DNS_BIND", "127.0.0.1")
DNS_TARGET_IP = os.environ.get("PORTMAP_DNS_TARGET_IP", "127.0.0.1")

PORTMAP_ENDPOINT_RE = re.compile(r"^portmap\.endpoints\.([^.]+)\.([^.]+)$")
TRAEFIK_ROUTER_RE = re.compile(r"^traefik\.(http|tcp|udp)\.routers\.([^.]+)\.([^.]+)$")
TRAEFIK_SERVICE_PORT_RE = re.compile(
    r"^traefik\.(http|tcp|udp)\.services\.([^.]+)\.loadbalancer\.server\.port$"
)
HTTP_HOST_RE = re.compile(r"Host\(`([^`]+)`\)")


def select_dns_server(bind_ip: str, target_ip: str) -> str:
    for candidate in (bind_ip, target_ip):
        candidate = candidate.strip()
        if candidate and candidate not in {"0.0.0.0", "::", "127.0.0.1", "localhost"}:
            return candidate
    return target_ip.strip() or bind_ip.strip() or "127.0.0.1"


DNS_SERVER = os.environ.get("PORTMAP_DNS_SERVER") or select_dns_server(DNS_BIND, DNS_TARGET_IP)


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_get(path: str) -> Any:
    connection = UnixHTTPConnection(DOCKER_SOCKET)
    connection.request("GET", path)
    response = connection.getresponse()
    payload = response.read()
    if response.status >= 400:
        raise RuntimeError(f"Docker API returned {response.status}: {payload.decode(errors='replace')}")
    return json.loads(payload.decode("utf-8"))


def collect_catalog() -> dict[str, Any]:
    containers = docker_get("/containers/json?all=0")
    services = [
        service
        for container in containers
        if (service := container_to_service(container)) is not None
    ]
    services.sort(
        key=lambda item: (
            item.get("repo_name") or "",
            item.get("branch") or "",
            item.get("compose_service") or "",
            item.get("container") or "",
        )
    )
    return {
        "generated_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat(),
        "http_port": HTTP_PORT,
        "dns_domain": DNS_DOMAIN,
        "dns_server": DNS_SERVER,
        "services": services,
    }


def container_to_service(container: dict[str, Any]) -> dict[str, Any] | None:
    labels = container.get("Labels") or {}
    if labels.get("traefik.enable") != "true" and labels.get("portmap.managed") != "true":
        return None

    endpoints = parse_portmap_endpoints(labels)
    if not endpoints:
        endpoints = parse_traefik_endpoints(labels)
    if not endpoints:
        return None

    container_name = first_container_name(container)
    return {
        "container": container_name,
        "image": container.get("Image"),
        "repo_id": labels.get("portmap.repo_id"),
        "repo_name": labels.get("portmap.repo_name"),
        "branch": labels.get("portmap.branch"),
        "worktree": labels.get("portmap.worktree")
        or labels.get("com.docker.compose.project.working_dir"),
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "docker_network": labels.get("traefik.docker.network"),
        "endpoints": endpoints,
    }


def first_container_name(container: dict[str, Any]) -> str:
    names = container.get("Names") or []
    if not names:
        return container.get("Id", "")[:12]
    return str(names[0]).lstrip("/")


def parse_portmap_endpoints(labels: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for key, value in labels.items():
        match = PORTMAP_ENDPOINT_RE.match(key)
        if match is None:
            continue
        endpoint_id, field = match.groups()
        grouped.setdefault(endpoint_id, {"id": endpoint_id})[field] = normalize_label_value(field, value)

    endpoints = list(grouped.values())
    endpoints.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return endpoints


def normalize_label_value(field: str, value: str) -> Any:
    if field in {"container_port", "host_port"}:
        try:
            return int(value)
        except ValueError:
            return value
    return value


def parse_traefik_endpoints(labels: dict[str, str]) -> list[dict[str, Any]]:
    routers: dict[tuple[str, str], dict[str, Any]] = {}
    service_ports: dict[tuple[str, str], int] = {}

    for key, value in labels.items():
        router_match = TRAEFIK_ROUTER_RE.match(key)
        if router_match is not None:
            kind, router, field = router_match.groups()
            routers.setdefault((kind, router), {"id": router, "kind": kind, "router": router})[field] = value
            continue

        service_match = TRAEFIK_SERVICE_PORT_RE.match(key)
        if service_match is not None:
            kind, service_name = service_match.groups()
            try:
                service_ports[(kind, service_name)] = int(value)
            except ValueError:
                pass

    endpoints: list[dict[str, Any]] = []
    for (kind, router), values in routers.items():
        service_name = values.get("service")
        endpoint: dict[str, Any] = {
            "id": router,
            "name": router,
            "kind": kind,
            "router": router,
            "entrypoint": values.get("entrypoints"),
            "traefik_service": service_name,
            "container_port": service_ports.get((kind, service_name)),
        }
        if kind == "http":
            host = parse_host_rule(str(values.get("rule", "")))
            endpoint["host"] = host
            if host:
                endpoint["url"] = f"http://{host}:{HTTP_PORT}"
        endpoints.append(endpoint)

    endpoints.sort(key=lambda item: str(item.get("name") or item.get("id") or ""))
    return endpoints


def parse_host_rule(rule: str) -> str | None:
    match = HTTP_HOST_RE.search(rule)
    if match is None:
        return None
    return match.group(1)


class CatalogHandler(BaseHTTPRequestHandler):
    server_version = "portmap-catalog/0.1"

    def do_GET(self) -> None:
        self.handle_request(send_body=True)

    def do_HEAD(self) -> None:
        self.handle_request(send_body=False)

    def handle_request(self, *, send_body: bool) -> None:
        if self.path in {"/healthz", "/readyz"}:
            self.write_text("ok\n", content_type="text/plain", send_body=send_body)
            return

        try:
            catalog = collect_catalog()
        except Exception as exc:  # pragma: no cover - exercised through container runtime.
            self.send_response(500)
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.end_headers()
            if send_body:
                self.wfile.write(f"failed to read Docker catalog: {exc}\n".encode("utf-8"))
            return

        if self.path == "/registry.json":
            self.write_json(catalog, send_body=send_body)
            return
        if self.path == "/":
            self.write_text(render_html(catalog), content_type="text/html", send_body=send_body)
            return

        self.send_response(404)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        if send_body:
            self.wfile.write(b"not found\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def write_json(self, payload: dict[str, Any], *, send_body: bool) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def write_text(self, body: str, *, content_type: str, send_body: bool) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", f"{content_type}; charset=utf-8")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if send_body:
            self.wfile.write(payload)


def render_html(catalog: dict[str, Any]) -> str:
    catalog_tree = render_catalog_tree(catalog)
    split_dns_command = split_dns_setup_command(catalog)
    split_dns_test = split_dns_test_command(catalog)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>portmap catalog</title>
  <style>
    :root {{
      color-scheme: light dark;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    body {{
      margin: 0;
      padding: 24px;
      background: #f5f7fa;
      color: #18202a;
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 4px;
      font-size: 24px;
      font-weight: 700;
    }}
    .meta {{
      margin: 0 0 20px;
      color: #52606d;
      font-size: 14px;
    }}
    .utility-panel {{
      margin-top: 20px;
      display: grid;
      gap: 12px;
    }}
    .quick-setup {{
      border: 1px solid #d9e0e8;
      background: #ffffff;
    }}
    .quick-setup summary {{
      cursor: pointer;
      padding: 12px 14px;
      background: #eef2f6;
      font-size: 15px;
      font-weight: 700;
    }}
    .quick-setup-body {{
      padding: 14px;
      border-top: 1px solid #e6ebf1;
    }}
    .quick-setup-actions {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }}
    .quick-setup p {{
      margin: 0;
      color: #52606d;
      font-size: 14px;
    }}
    .copy-button {{
      border: 1px solid #9aa7b4;
      background: #f8fafc;
      color: #18202a;
      padding: 7px 10px;
      font-size: 13px;
      cursor: pointer;
    }}
    .copy-button:focus {{
      outline: 2px solid #0ea5e9;
      outline-offset: 2px;
    }}
    pre {{
      margin: 0;
      overflow-x: auto;
      background: #111827;
      color: #e5e7eb;
      padding: 12px;
      border: 1px solid #1f2937;
    }}
    .catalog-tree {{
      display: grid;
      gap: 16px;
    }}
    .directory-group {{
      border: 1px solid #d9e0e8;
      background: #ffffff;
    }}
    .directory-header {{
      padding: 12px 14px;
      background: #eef2f6;
      border-bottom: 1px solid #d9e0e8;
    }}
    .directory-header h2 {{
      margin: 0 0 4px;
      font-size: 16px;
      font-weight: 700;
    }}
    .repo-group {{
      padding: 14px;
      border-top: 1px solid #e6ebf1;
    }}
    .repo-group:first-of-type {{
      border-top: 0;
    }}
    .repo-header {{
      margin-bottom: 10px;
    }}
    .repo-header h3 {{
      margin: 0 0 4px;
      font-size: 15px;
      font-weight: 700;
    }}
    .branch-group {{
      margin-top: 12px;
      border: 1px solid #e6ebf1;
    }}
    .branch-header {{
      padding: 9px 10px;
      background: #f8fafc;
      border-bottom: 1px solid #e6ebf1;
      font-size: 14px;
      font-weight: 700;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
    }}
    th, td {{
      padding: 10px 12px;
      border-bottom: 1px solid #e6ebf1;
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      background: #eef2f6;
      color: #283544;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px;
      word-break: break-all;
    }}
    a {{
      color: #075985;
    }}
    .empty {{
      color: #697586;
      padding: 24px;
      text-align: center;
      border: 1px solid #d9e0e8;
      background: #ffffff;
    }}
    @media (prefers-color-scheme: dark) {{
      body {{
        background: #101418;
        color: #eef2f6;
      }}
      .meta {{
        color: #a7b2bf;
      }}
      .quick-setup {{
        background: #171d23;
        border-color: #2b3540;
      }}
      .quick-setup summary {{
        background: #202832;
      }}
      .quick-setup-body {{
        border-top-color: #2b3540;
      }}
      .quick-setup p {{
        color: #a7b2bf;
      }}
      .copy-button {{
        background: #202832;
        color: #eef2f6;
        border-color: #52606d;
      }}
      .directory-group {{
        background: #171d23;
        border-color: #2b3540;
      }}
      .directory-header {{
        background: #202832;
        border-bottom-color: #2b3540;
      }}
      .repo-group {{
        border-top-color: #2b3540;
      }}
      .branch-group {{
        border-color: #2b3540;
      }}
      .branch-header {{
        background: #202832;
        border-bottom-color: #2b3540;
      }}
      table {{
        background: #171d23;
      }}
      th {{
        background: #202832;
        color: #cbd5e1;
      }}
      th, td {{
        border-bottom-color: #2b3540;
      }}
      a {{
        color: #7dd3fc;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>portmap catalog</h1>
    <p class="meta">
      Generated at {escape(catalog["generated_at"])}.
      HTTP proxy port: {escape(catalog["http_port"])}.
      DNS domain: {escape(catalog["dns_domain"])}.
      DNS server: {escape(catalog["dns_server"])}.
      JSON: <a href="/registry.json">/registry.json</a>.
    </p>
    <section class="catalog-tree">
{catalog_tree}
    </section>
    <section class="utility-panel" aria-label="Setup commands">
      <details class="quick-setup">
        <summary>Split DNS quick setup</summary>
        <div class="quick-setup-body">
          <div class="quick-setup-actions">
            <p>Run this on the client machine that opens the generated debug URLs.</p>
            <button class="copy-button" type="button" data-copy-target="split-dns-command">Copy</button>
          </div>
          <pre><code id="split-dns-command">{escape(split_dns_command)}</code></pre>
        </div>
      </details>
      <details class="quick-setup">
        <summary>Test command</summary>
        <div class="quick-setup-body">
          <div class="quick-setup-actions">
            <p>Verify split DNS and the portmap catalog endpoint.</p>
            <button class="copy-button" type="button" data-copy-target="split-dns-test">Copy</button>
          </div>
          <pre><code id="split-dns-test">{escape(split_dns_test)}</code></pre>
        </div>
      </details>
    </section>
  </main>
  <script>
    async function copyText(text) {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(text);
        return;
      }}
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }}

    document.querySelectorAll("[data-copy-target]").forEach((button) => {{
      button.addEventListener("click", async () => {{
        const target = document.getElementById(button.dataset.copyTarget);
        if (!target) return;
        await copyText(target.textContent.trim() + "\\n");
        const original = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {{
          button.textContent = original;
        }}, 1200);
      }});
    }});
  </script>
</body>
</html>
"""


def build_catalog_tree(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    directories: dict[str, dict[str, Any]] = {}
    for service in catalog.get("services", []):
        if not isinstance(service, dict):
            continue
        worktree = str(service.get("worktree") or "unknown")
        repo_id = str(service.get("repo_id") or "unknown")
        branch = str(service.get("branch") or "unknown")

        directory = directories.setdefault(worktree, {"worktree": worktree, "repos": {}})
        repo = directory["repos"].setdefault(
            repo_id,
            {
                "repo_id": repo_id,
                "repo_name": service.get("repo_name") or repo_id,
                "branches": {},
            },
        )
        branch_payload = repo["branches"].setdefault(branch, {"branch": branch, "services": []})
        branch_payload["services"].append(service)

    result = list(directories.values())
    result.sort(key=lambda item: item["worktree"])
    for directory in result:
        repos = list(directory["repos"].values())
        repos.sort(key=lambda item: (str(item.get("repo_name") or ""), str(item.get("repo_id") or "")))
        directory["repos"] = repos
        for repo in repos:
            branches = list(repo["branches"].values())
            branches.sort(key=lambda item: item["branch"])
            repo["branches"] = branches
            for branch in branches:
                branch["services"].sort(
                    key=lambda item: (
                        str(item.get("compose_service") or ""),
                        str(item.get("container") or ""),
                    )
                )
    return result


def render_catalog_tree(catalog: dict[str, Any]) -> str:
    directories = build_catalog_tree(catalog)
    if not directories:
        return '      <div class="empty">No portmap-managed services are currently visible.</div>'
    return "\n".join(render_directory(directory) for directory in directories)


def render_directory(directory: dict[str, Any]) -> str:
    repos = "\n".join(render_repo(repo) for repo in directory["repos"])
    return f"""      <section class="directory-group">
        <div class="directory-header">
          <h2>Work Tree</h2>
          <code>{escape(directory["worktree"])}</code>
        </div>
{repos}
      </section>"""


def render_repo(repo: dict[str, Any]) -> str:
    branches = "\n".join(render_branch(branch) for branch in repo["branches"])
    return f"""        <section class="repo-group">
          <div class="repo-header">
            <h3>{escape(repo.get("repo_name") or repo.get("repo_id") or "")}</h3>
            <div class="meta">repo_id: <code>{escape(repo.get("repo_id") or "")}</code></div>
          </div>
{branches}
        </section>"""


def render_branch(branch: dict[str, Any]) -> str:
    rows = "\n".join(render_service_endpoint_rows(service) for service in branch["services"])
    return f"""          <section class="branch-group">
            <div class="branch-header">Branch: <code>{escape(branch["branch"])}</code></div>
            <table>
              <thead>
                <tr>
                  <th>Service</th>
                  <th>Endpoint</th>
                  <th>Kind</th>
                  <th>External</th>
                  <th>Container Port</th>
                  <th>Container</th>
                  <th>Image</th>
                </tr>
              </thead>
              <tbody>
{rows}
              </tbody>
            </table>
          </section>"""


def render_service_endpoint_rows(service: dict[str, Any]) -> str:
    return "\n".join(render_service_endpoint_row(service, endpoint) for endpoint in service["endpoints"])


def render_service_endpoint_row(service: dict[str, Any], endpoint: dict[str, Any]) -> str:
    external = endpoint_external(endpoint)
    external_cell = f'<a href="{escape_attr(external)}"><code>{escape(external)}</code></a>' if external else ""
    return f"""                <tr>
                  <td><code>{escape(service.get("compose_service") or "")}</code></td>
                  <td><code>{escape(endpoint.get("name") or endpoint.get("id") or "")}</code></td>
                  <td><code>{escape(endpoint.get("kind") or "")}</code></td>
                  <td>{external_cell}</td>
                  <td><code>{escape(endpoint.get("container_port") or "")}</code></td>
                  <td><code>{escape(service.get("container") or "")}</code></td>
                  <td><code>{escape(service.get("image") or "")}</code></td>
                </tr>"""


def split_dns_setup_command(catalog: dict[str, Any]) -> str:
    dns_server = shell_single_quote(str(catalog["dns_server"]))
    dns_domain = shell_single_quote(str(catalog["dns_domain"]))
    return f"""DNS_SERVER={dns_server}
DNS_DOMAIN={dns_domain}
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{{for (i = 1; i <= NF; i++) if ($i == "dev") {{print $(i + 1); exit}}}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
resolvectl query "portmap.$DNS_DOMAIN"
"""


def split_dns_test_command(catalog: dict[str, Any]) -> str:
    domain = str(catalog["dns_domain"])
    return f"""resolvectl query "portmap.{domain}"
curl -I "http://portmap.{domain}/"
"""


def shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def endpoint_external(endpoint: dict[str, Any]) -> str | None:
    if endpoint.get("url"):
        return str(endpoint["url"])
    if endpoint.get("kind") in {"tcp", "udp"} and endpoint.get("host") and endpoint.get("host_port"):
        return f"{endpoint['host']}:{endpoint['host_port']}"
    return None


def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def escape_attr(value: Any) -> str:
    return html.escape(str(value), quote=True)


def main() -> None:
    host = os.environ.get("PORTMAP_CATALOG_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("PORTMAP_CATALOG_LISTEN_PORT", "8081"))
    server = ThreadingHTTPServer((host, port), CatalogHandler)
    print(f"portmap catalog listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
