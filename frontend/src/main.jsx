import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Folder,
  GitBranch,
  Power,
  RefreshCw,
} from "lucide-react";
import "./style.css";

function text(value) {
  return value == null ? "" : String(value);
}

function shellSingleQuote(value) {
  return "'" + text(value).replace(/'/g, "'\"'\"'") + "'";
}

function currentPortSuffix() {
  return window.location.port ? `:${window.location.port}` : "";
}

function splitDnsSetupCommand(catalog) {
  const dnsServer = shellSingleQuote(catalog?.dns_server);
  const dnsDomain = shellSingleQuote(catalog?.dns_domain);
  return `DNS_SERVER=${dnsServer}
DNS_DOMAIN=${dnsDomain}
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
resolvectl query "portmap.$DNS_DOMAIN"
`;
}

function splitDnsUnsetCommand(catalog) {
  const dnsServer = shellSingleQuote(catalog?.dns_server);
  const dnsDomain = shellSingleQuote(catalog?.dns_domain);
  return `DNS_SERVER=${dnsServer}
DNS_DOMAIN=${dnsDomain}
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl revert "$DNS_IFACE"
resolvectl query "portmap.$DNS_DOMAIN"
`;
}

function endpointCount(service) {
  return (service.endpoints || []).length;
}

function uniqueSorted(values) {
  return Array.from(new Set(values.filter(Boolean).map(text))).sort((left, right) => left.localeCompare(right));
}

function buildCatalogTree(catalog) {
  const projects = new Map();
  for (const service of catalog?.services || []) {
    const repoId = text(service.repo_id || "unknown");
    const repoName = text(service.repo_name || repoId);
    const projectKey = repoId || repoName;
    const branchName = text(service.branch || "unknown");

    if (!projects.has(projectKey)) {
      projects.set(projectKey, {
        repo_id: repoId,
        repo_name: repoName,
        branches: new Map(),
      });
    }
    const project = projects.get(projectKey);
    if (!project.branches.has(branchName)) {
      project.branches.set(branchName, {
        branch: branchName,
        services: [],
      });
    }
    project.branches.get(branchName).services.push(service);
  }

  return Array.from(projects.values())
    .sort((left, right) => text(left.repo_name).localeCompare(text(right.repo_name)) || text(left.repo_id).localeCompare(text(right.repo_id)))
    .map((project) => ({
      ...project,
      branches: Array.from(project.branches.values())
        .sort((left, right) => left.branch.localeCompare(right.branch))
        .map((branch) => ({
          ...branch,
          services: branch.services.sort((left, right) => text(left.compose_service).localeCompare(text(right.compose_service)) || text(left.container).localeCompare(text(right.container))),
        })),
    }));
}

function endpointExternal(endpoint) {
  if (endpoint.url) return text(endpoint.url);
  if ((endpoint.kind === "tcp" || endpoint.kind === "udp") && endpoint.host && endpoint.host_port) {
    return `${endpoint.host}:${endpoint.host_port}`;
  }
  if (endpoint.kind === "range" && endpoint.host && endpoint.host_port) {
    let rangeText = "";
    if (endpoint.range_start && endpoint.range_end) {
      rangeText = ` relay ${endpoint.range_start}-${endpoint.range_end}`;
    }
    return `${endpoint.host}:${endpoint.host_port}${rangeText}`;
  }
  return "";
}

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  document.execCommand("copy");
  document.body.removeChild(textarea);
}

async function parseActionResponse(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return response.json();
  }
  const body = await response.text();
  return {
    ok: false,
    message: body.trim() || `HTTP ${response.status}`,
  };
}

async function composeProjectAction({ action, composeProject, failedText }) {
  const body = new URLSearchParams({ compose_project: composeProject });
  try {
    const response = await fetch(`/actions/compose-${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const result = await parseActionResponse(response);
    if (!response.ok || !result.ok) {
      return { failed: true, message: result.message || failedText, result };
    }
    return { failed: false, message: `${result.message}: ${composeProject}`, result };
  } catch (error) {
    return { failed: true, message: `${failedText}: ${error}` };
  }
}

function nowLabel() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function actionLabel(action) {
  return action === "restart" ? "Restart" : "Down";
}

function StatPill({ label }) {
  return <span className="stat-pill">{label}</span>;
}

function CodeText({ value }) {
  return <code>{text(value)}</code>;
}

function OpenLink({ href, children }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
      <ExternalLink className="inline-icon" aria-hidden="true" />
    </a>
  );
}

function CopyButton({ targetId }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="copy-button"
      type="button"
      onClick={async () => {
        const target = document.getElementById(targetId);
        if (!target) return;
        await copyText(`${target.textContent.trim()}\n`);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
    >
      <Copy className="button-icon" aria-hidden="true" />
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function CatalogMeta({ catalog }) {
  if (!catalog) return <p className="meta">Loading catalog...</p>;
  return (
    <p className="meta" data-catalog-meta>
      Generated at {text(catalog.generated_at)}. HTTP proxy port: {text(catalog.http_port)}. DNS domain:{" "}
      {text(catalog.dns_domain)}. DNS server: {text(catalog.dns_server)}. JSON:{" "}
      <OpenLink href="/registry.json">/registry.json</OpenLink>.
    </p>
  );
}

function ActionMessage({ message }) {
  if (!message) return null;
  return (
    <p className={`action-message ${message.failed ? "action-message-error" : ""}`} data-action-message>
      {message.message}
    </p>
  );
}

function ActionLog({ entries }) {
  if (!entries.length) return null;
  return (
    <section className="action-log" aria-label="Action log">
      <div className="action-log-header">
        <h2>Action log</h2>
      </div>
      <ol className="action-log-list">
        {entries.map((entry) => (
          <li className={`action-log-entry action-log-${entry.status}`} key={entry.id}>
            <span className="action-log-time">{entry.time}</span>
            <span className="action-log-status">{entry.status}</span>
            <span className="action-log-message">
              {entry.message}
              {entry.detail ? <code>{entry.detail}</code> : null}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function BranchActions({ branch, onAction }) {
  const projects = uniqueSorted(branch.services.map((service) => service.compose_project));
  if (!projects.length) return null;
  return (
    <div className="branch-actions">
      {projects.map((project) => (
        <div className="project-action" key={project}>
          <CodeText value={project} />
          <button
            className="action-button restart-button"
            title={`docker compose -p ${project} restart`}
            type="button"
            onClick={() => onAction("restart", project)}
          >
            <RefreshCw className="button-icon" aria-hidden="true" />
            Restart
          </button>
          <button
            className="action-button down-button"
            title={`docker compose -p ${project} down`}
            type="button"
            onClick={() => onAction("down", project)}
          >
            <Power className="button-icon" aria-hidden="true" />
            Down
          </button>
        </div>
      ))}
    </div>
  );
}

function EndpointExternal({ endpoint }) {
  const external = endpointExternal(endpoint);
  if (!external) return null;
  if (external.startsWith("http://") || external.startsWith("https://")) {
    return (
      <OpenLink href={external}>
        <CodeText value={external} />
      </OpenLink>
    );
  }
  return <CodeText value={external} />;
}

function EndpointTable({ services }) {
  return (
    <div className="table-wrap">
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
          {services.flatMap((service) =>
            (service.endpoints || []).map((endpoint) => (
              <tr key={`${service.container}-${endpoint.id || endpoint.name}`}>
                <td><CodeText value={service.compose_service || ""} /></td>
                <td><CodeText value={endpoint.name || endpoint.id || ""} /></td>
                <td><CodeText value={endpoint.kind || ""} /></td>
                <td><EndpointExternal endpoint={endpoint} /></td>
                <td><CodeText value={endpoint.container_port || ""} /></td>
                <td><CodeText value={service.container || ""} /></td>
                <td><CodeText value={service.image || ""} /></td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function BranchPanel({ branch, onAction }) {
  const [open, setOpen] = useState(true);
  const endpointTotal = branch.services.reduce((total, service) => total + endpointCount(service), 0);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <section className="branch-group" data-branch-group="true">
      <div className="branch-header">
        <button className="branch-toggle" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Chevron className="toggle-icon" aria-hidden="true" />
          <GitBranch className="entity-icon" aria-hidden="true" />
          <span className="branch-name">{branch.branch}</span>
        </button>
        <div className="branch-header-right">
          <div className="summary-pills">
            <StatPill label={`${branch.services.length} services`} />
            <StatPill label={`${endpointTotal} endpoints`} />
          </div>
          <BranchActions branch={branch} onAction={onAction} />
        </div>
      </div>
      {open ? (
        <div className="branch-body">
          <EndpointTable services={branch.services} />
        </div>
      ) : null}
    </section>
  );
}

function projectStats(project) {
  const services = project.branches.flatMap((branch) => branch.services);
  const endpointTotal = services.reduce((total, service) => total + endpointCount(service), 0);
  return {
    branchTotal: project.branches.length,
    serviceTotal: services.length,
    endpointTotal,
  };
}

function ProjectPanel({ project, onAction }) {
  const [open, setOpen] = useState(true);
  const stats = projectStats(project);
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <section className="project-group" data-project-group="true">
      <div className="project-header">
        <button className="project-toggle" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Chevron className="toggle-icon" aria-hidden="true" />
          <Folder className="entity-icon" aria-hidden="true" />
          <span className="project-name">{project.repo_name || project.repo_id}</span>
        </button>
        <div className="project-header-right">
          <div className="project-id">repo_id <CodeText value={project.repo_id || ""} /></div>
          <div className="summary-pills">
            <StatPill label={`${stats.branchTotal} branches`} />
            <StatPill label={`${stats.serviceTotal} services`} />
            <StatPill label={`${stats.endpointTotal} endpoints`} />
          </div>
        </div>
      </div>
      {open ? (
        <div className="project-body">
          {project.branches.map((branch) => (
            <BranchPanel branch={branch} key={branch.branch} onAction={onAction} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function CatalogTree({ catalog, onAction }) {
  const projects = useMemo(() => buildCatalogTree(catalog), [catalog]);
  if (!projects.length) {
    return (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty">No portmap-managed services are currently visible.</div>
      </section>
    );
  }
  return (
    <section className="catalog-tree" data-catalog-tree>
      {projects.map((project) => (
        <ProjectPanel project={project} key={project.repo_id || project.repo_name} onAction={onAction} />
      ))}
    </section>
  );
}

function SplitDnsTools({ catalog }) {
  const setup = catalog ? splitDnsSetupCommand(catalog) : "Loading...";
  const unset = catalog ? splitDnsUnsetCommand(catalog) : "Loading...";
  return (
    <section className="utility-panel" aria-label="Setup commands">
      <details className="quick-setup">
        <summary>Split DNS setup</summary>
        <div className="quick-setup-body">
          <div className="quick-command">
            <div className="quick-setup-actions">
              <p>Apply split DNS on the client machine that opens the generated debug URLs.</p>
              <CopyButton targetId="split-dns-command" />
            </div>
            <pre><code id="split-dns-command">{setup}</code></pre>
          </div>
          <div className="quick-command">
            <div className="quick-setup-actions">
              <p>Remove the temporary split DNS override from the same client machine.</p>
              <CopyButton targetId="split-dns-unset" />
            </div>
            <pre><code id="split-dns-unset">{unset}</code></pre>
          </div>
        </div>
      </details>
    </section>
  );
}

function DnsStatus({ catalog }) {
  const [status, setStatus] = useState("checking");
  if (!catalog) {
    return (
      <span className="dns-status dns-status-checking">
        <span className="dns-status-label">DNS</span>
        <span className="dns-status-value">loading</span>
      </span>
    );
  }
  const domain = text(catalog.dns_domain).replace(/\.$/, "");
  const host = `portmap.${domain}`;
  const url = `${window.location.protocol}//${host}${currentPortSuffix()}/assets/dns-check.svg?ts=${Date.now()}`;
  const statusLabel = status === "ok" ? "ok" : status === "failed" ? "failed" : "checking";
  return (
    <span className={`dns-status dns-status-${status}`} data-dns-probe title={url}>
      <img
        className="dns-status-image"
        data-dns-probe-image
        src={url}
        alt=""
        onLoad={() => setStatus("ok")}
        onError={() => setStatus("failed")}
      />
      <span className="dns-status-label">DNS</span>
      <span className="dns-status-value" data-dns-probe-status>{statusLabel}</span>
      <OpenLink href={url}><CodeText value={host} /></OpenLink>
    </span>
  );
}

function CatalogApp() {
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState("");
  const [actionMessage, setActionMessage] = useState(null);
  const [actionLog, setActionLog] = useState([]);

  async function loadCatalog() {
    try {
      const response = await fetch("/registry.json");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setCatalog(await response.json());
      setError("");
    } catch (loadError) {
      setError(`Failed to load catalog: ${loadError}`);
    }
  }

  async function handleAction(action, composeProject) {
    const label = actionLabel(action);
    if (!window.confirm(`Run docker compose ${action} for ${composeProject}?`)) return;
    const logId = `${Date.now()}-${action}-${composeProject}`;
    const detail = `docker compose -p ${composeProject} ${action}`;
    setActionLog((entries) => [
      {
        id: logId,
        time: nowLabel(),
        status: "running",
        message: `${label} requested`,
        detail,
      },
      ...entries,
    ].slice(0, 8));
    const result = await composeProjectAction({
      action,
      composeProject,
      failedText: `compose ${action} failed`,
    });
    setActionMessage(result);
    setActionLog((entries) => entries.map((entry) => (
      entry.id === logId
        ? {
          ...entry,
          time: nowLabel(),
          status: result.failed ? "failed" : "ok",
          message: result.failed ? `${label} failed: ${result.message}` : `${label} completed: ${result.message}`,
        }
        : entry
    )));
    if (!result.failed) await loadCatalog();
  }

  useEffect(() => {
    loadCatalog();
  }, []);

  return (
    <main>
      <header className="app-header">
        <div>
          <div className="title-row">
            <h1>portmap catalog</h1>
            <DnsStatus catalog={catalog} />
          </div>
          <CatalogMeta catalog={catalog} />
        </div>
      </header>
      <ActionMessage message={actionMessage} />
      <ActionLog entries={actionLog} />
      {error ? (
        <section className="catalog-tree" data-catalog-tree>
          <div className="empty load-error">{error}</div>
        </section>
      ) : (
        <CatalogTree catalog={catalog} onAction={handleAction} />
      )}
      <SplitDnsTools catalog={catalog} />
    </main>
  );
}

createRoot(document.getElementById("root")).render(<CatalogApp />);
