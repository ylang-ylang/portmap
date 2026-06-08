import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Boxes,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Folder,
  GitBranch,
  Play,
  Power,
  RefreshCw,
} from "lucide-react";
import "./style.css";

function text(value) {
  return value == null ? "" : String(value);
}

function numberValue(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : 0;
}

const MISSING_ORDER = 1000000;

function orderValue(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : MISSING_ORDER;
}

function branchTipEpoch(value) {
  return numberValue(value?.branch_tip_epoch);
}

function compareBranchTip(left, right) {
  const tipDelta = branchTipEpoch(right) - branchTipEpoch(left);
  if (tipDelta !== 0) return tipDelta;
  return text(left.branch).localeCompare(text(right.branch))
    || text(left.worktree_title).localeCompare(text(right.worktree_title))
    || text(left.worktree).localeCompare(text(right.worktree));
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

function compareEndpointOrder(left, right) {
  const orderDelta = orderValue(left.order) - orderValue(right.order);
  if (orderDelta !== 0) return orderDelta;
  return text(left.name).localeCompare(text(right.name)) || text(left.id).localeCompare(text(right.id));
}

function serviceOrder(service) {
  const explicitOrder = orderValue(service.portmap_order);
  if (explicitOrder !== MISSING_ORDER) return explicitOrder;
  const endpointOrders = (service.endpoints || []).map((endpoint) => orderValue(endpoint.order));
  return endpointOrders.length ? Math.min(...endpointOrders) : MISSING_ORDER;
}

function compareServiceOrder(left, right) {
  const orderDelta = serviceOrder(left) - serviceOrder(right);
  if (orderDelta !== 0) return orderDelta;
  return text(left.compose_service).localeCompare(text(right.compose_service)) || text(left.container).localeCompare(text(right.container));
}

function uniqueSorted(values) {
  return Array.from(new Set(values.filter(Boolean).map(text))).sort((left, right) => left.localeCompare(right));
}

function pathTitle(value) {
  const parts = text(value).split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : text(value);
}

function worktreeRootPath(seed) {
  return text(seed.display_worktree_root || seed.worktree_root || seed.worktree || "");
}

function worktreeRootTitle(seed) {
  return text(seed.display_worktree_root_title || seed.worktree_root_title || pathTitle(worktreeRootPath(seed)) || pathTitle(seed.worktree) || "wt root");
}

function branchName(seed) {
  if (worktreeSubmodule(seed)) {
    return text(seed.submodule_branch || seed.branch || seed.display_branch || "unknown");
  }
  return text(seed.display_branch || seed.branch || "unknown");
}

function submoduleContext(seed) {
  const repo = text(seed.superproject_repo_name || "superproject");
  const branch = text(seed.superproject_branch || seed.display_branch || "");
  if (repo && branch) return `${repo}@${branch}`;
  if (branch) return branch;
  return pathTitle(seed.superproject_worktree || seed.worktree_superproject || seed.worktree);
}

function branchNote(seed) {
  if (worktreeSubmodule(seed)) {
    const submoduleSha = text(seed.submodule_sha || seed.branch_tip_sha || "").slice(0, 7);
    const context = submoduleContext(seed);
    return submoduleSha ? `in ${context} (${submoduleSha})` : `in ${context}`;
  }
  return text(seed.worktree_title || pathTitle(seed.worktree));
}

function repoKey(entry) {
  return text(entry.repo_id || entry.repo_name || "unknown");
}

function worktreeDeleted(entry) {
  return entry?.worktree_exists === false || text(entry?.worktree_status) === "deleted";
}

function worktreeSubmodule(entry) {
  return text(entry?.worktree_status) === "submodule";
}

function statusRank(entry) {
  if (worktreeDeleted(entry)) return 2;
  if (worktreeSubmodule(entry)) return 1;
  return 0;
}

function statusPayload(seed) {
  const displayPayload = {
    display_branch: text(seed.display_branch || ""),
    display_worktree_root: text(seed.display_worktree_root || ""),
    display_worktree_root_title: text(seed.display_worktree_root_title || ""),
    submodule_branch: text(seed.submodule_branch || ""),
    submodule_relative_path: text(seed.submodule_relative_path || ""),
    submodule_sha: text(seed.submodule_sha || ""),
    superproject_branch: text(seed.superproject_branch || ""),
    superproject_repo_id: text(seed.superproject_repo_id || ""),
    superproject_repo_name: text(seed.superproject_repo_name || ""),
    superproject_worktree: text(seed.superproject_worktree || ""),
  };
  if (worktreeDeleted(seed)) {
    return {
      ...displayPayload,
      worktree_exists: false,
      worktree_status: "deleted",
      worktree_status_message: text(seed.worktree_status_message || "worktree directory not found"),
      worktree_superproject: text(seed.worktree_superproject || ""),
    };
  }
  if (worktreeSubmodule(seed)) {
    return {
      ...displayPayload,
      worktree_exists: true,
      worktree_status: "submodule",
      worktree_status_message: text(seed.worktree_status_message || "git submodule checkout"),
      worktree_superproject: text(seed.worktree_superproject || ""),
    };
  }
  return {
    ...displayPayload,
    worktree_exists: seed.worktree_exists === false ? false : seed.worktree_exists === true ? true : null,
    worktree_status: text(seed.worktree_status || ""),
    worktree_status_message: text(seed.worktree_status_message || ""),
    worktree_superproject: text(seed.worktree_superproject || ""),
  };
}

function applyWorktreeStatus(target, seed) {
  if (statusRank(seed) < statusRank(target)) return;
  Object.assign(target, statusPayload(seed));
}

function buildCatalogTree(catalog) {
  const projects = new Map();

  function ensureProject(repoId, repoName) {
    const cleanRepoId = text(repoId || "unknown");
    const cleanRepoName = text(repoName || cleanRepoId);
    const projectKey = repoKey({ repo_id: cleanRepoId, repo_name: cleanRepoName });
    if (!projects.has(projectKey)) {
      projects.set(projectKey, {
        repo_id: cleanRepoId,
        repo_name: cleanRepoName,
        worktrees: new Map(),
      });
    }
    return projects.get(projectKey);
  }

  function ensureWorktree(project, seed) {
    const rootPath = worktreeRootPath(seed);
    const rootTitle = worktreeRootTitle(seed);
    const key = text(rootPath || seed.id || seed.compose_project || `${seed.repo_id}:${seed.branch}`);
    if (!project.worktrees.has(key)) {
      project.worktrees.set(key, {
        id: text(seed.id || key),
        repo_id: project.repo_id,
        repo_name: project.repo_name,
        branch: text(seed.branch || "unknown"),
        worktree: rootPath,
        worktree_title: rootTitle,
        worktree_root: rootPath,
        worktree_root_title: rootTitle,
        compose_project: text(seed.compose_project || ""),
        running: Boolean(seed.running),
        status: text(seed.status || (seed.running ? "running" : "stopped")),
        startable: Boolean(seed.startable),
        start_error: text(seed.start_error || ""),
        ...statusPayload(seed),
        source: text(seed.source || "current"),
        last_seen_at: text(seed.last_seen_at || ""),
        branches: new Map(),
        dead_instances: [],
      });
    }
    const worktree = project.worktrees.get(key);
    worktree.running = worktree.running || Boolean(seed.running);
    worktree.status = worktree.running ? "running" : text(seed.status || worktree.status || "stopped");
    worktree.startable = worktree.startable || Boolean(seed.startable);
    worktree.start_error = worktree.start_error || text(seed.start_error || "");
    worktree.compose_project = worktree.compose_project || text(seed.compose_project || "");
    worktree.worktree = worktree.worktree || rootPath;
    worktree.worktree_title = worktree.worktree_title || rootTitle;
    worktree.worktree_root = worktree.worktree_root || rootPath;
    worktree.worktree_root_title = worktree.worktree_root_title || rootTitle;
    applyWorktreeStatus(worktree, seed);
    worktree.source = worktree.source === "current" || seed.source === "current" ? "current" : worktree.source;
    worktree.last_seen_at = text(seed.last_seen_at || worktree.last_seen_at || "");
    return worktree;
  }

  function ensureBranch(worktree, branchName, seed = {}) {
    const cleanBranch = text(branchName || worktree.branch || "unknown");
    if (!worktree.branches.has(cleanBranch)) {
      worktree.branches.set(cleanBranch, {
        branch: cleanBranch,
        raw_branch: text(seed.branch || cleanBranch),
        worktree: text(seed.worktree || ""),
        worktree_title: text(branchNote(seed) || cleanBranch),
        branch_tip_epoch: numberValue(seed.branch_tip_epoch),
        branch_tip_time: text(seed.branch_tip_time || ""),
        branch_tip_sha: text(seed.branch_tip_sha || ""),
        ...statusPayload(seed),
        services: [],
      });
    }
    const branch = worktree.branches.get(cleanBranch);
    branch.worktree = branch.worktree || text(seed.worktree || "");
    branch.worktree_title = branch.worktree_title || text(branchNote(seed) || cleanBranch);
    if (numberValue(seed.branch_tip_epoch) > numberValue(branch.branch_tip_epoch)) {
      branch.branch_tip_epoch = numberValue(seed.branch_tip_epoch);
      branch.branch_tip_time = text(seed.branch_tip_time || "");
      branch.branch_tip_sha = text(seed.branch_tip_sha || "");
    }
    applyWorktreeStatus(branch, seed);
    return branch;
  }

  for (const record of catalog?.worktrees || []) {
    if (!record.running && !record.startable) continue;
    const project = ensureProject(record.repo_id, record.repo_name);
    const worktree = ensureWorktree(project, record);
    if (!record.running) {
      worktree.dead_instances.push({
        ...record,
        branch: branchName(record),
        raw_branch: text(record.branch || ""),
        worktree_title: branchNote(record),
        dead: true,
      });
      continue;
    }
    ensureBranch(worktree, branchName(record), record);
  }

  for (const service of catalog?.services || []) {
    const repoId = text(service.repo_id || "unknown");
    const repoName = text(service.repo_name || repoId);
    const cleanBranchName = branchName(service);
    const project = ensureProject(repoId, repoName);
    const worktree = ensureWorktree(project, {
      repo_id: repoId,
      repo_name: repoName,
      branch: service.branch,
      display_branch: service.display_branch,
      worktree: service.worktree,
      worktree_title: branchNote(service),
      display_worktree_root: service.display_worktree_root,
      display_worktree_root_title: service.display_worktree_root_title,
      worktree_root: service.worktree_root,
      worktree_root_title: service.worktree_root_title,
      compose_project: service.compose_project,
      branch_tip_epoch: service.branch_tip_epoch,
      branch_tip_time: service.branch_tip_time,
      branch_tip_sha: service.branch_tip_sha,
      worktree_exists: service.worktree_exists,
      worktree_status: service.worktree_status,
      worktree_status_message: service.worktree_status_message,
      worktree_superproject: service.worktree_superproject,
      submodule_branch: service.submodule_branch,
      submodule_relative_path: service.submodule_relative_path,
      submodule_sha: service.submodule_sha,
      superproject_branch: service.superproject_branch,
      superproject_repo_id: service.superproject_repo_id,
      superproject_repo_name: service.superproject_repo_name,
      superproject_worktree: service.superproject_worktree,
      running: true,
      status: "running",
      startable: true,
      source: "current",
    });
    ensureBranch(worktree, cleanBranchName, {
      branch: service.branch,
      display_branch: service.display_branch,
      worktree: service.worktree,
      worktree_title: branchNote(service),
      branch_tip_epoch: service.branch_tip_epoch,
      branch_tip_time: service.branch_tip_time,
      branch_tip_sha: service.branch_tip_sha,
      worktree_exists: service.worktree_exists,
      worktree_status: service.worktree_status,
      worktree_status_message: service.worktree_status_message,
      worktree_superproject: service.worktree_superproject,
      submodule_branch: service.submodule_branch,
      submodule_relative_path: service.submodule_relative_path,
      submodule_sha: service.submodule_sha,
      superproject_branch: service.superproject_branch,
      superproject_repo_id: service.superproject_repo_id,
      superproject_repo_name: service.superproject_repo_name,
      superproject_worktree: service.superproject_worktree,
    }).services.push(service);
  }

  return Array.from(projects.values())
    .sort((left, right) => text(left.repo_name).localeCompare(text(right.repo_name)) || text(left.repo_id).localeCompare(text(right.repo_id)))
    .map((project) => ({
      ...project,
      worktrees: Array.from(project.worktrees.values())
        .sort((left, right) => text(left.worktree_title).localeCompare(text(right.worktree_title)) || text(left.worktree).localeCompare(text(right.worktree)))
        .map((worktree) => ({
          ...worktree,
          dead_instances: worktree.dead_instances.sort(compareBranchTip),
          branches: Array.from(worktree.branches.values())
            .sort(compareBranchTip)
            .map((branch) => ({
              ...branch,
              services: branch.services.sort(compareServiceOrder),
            })),
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

async function composeWorktreeAction({ worktree, failedText }) {
  const body = new URLSearchParams({ worktree });
  try {
    const response = await fetch("/actions/compose-up", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    const result = await parseActionResponse(response);
    if (!response.ok || !result.ok) {
      return { failed: true, message: result.message || failedText, result };
    }
    return { failed: false, message: `${result.message}: ${worktree}`, result };
  } catch (error) {
    return { failed: true, message: `${failedText}: ${error}` };
  }
}

function nowLabel() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function actionLabel(action) {
  if (action === "restart") return "Restart";
  if (action === "up") return "Start";
  return "Down";
}

function StatPill({ label }) {
  return <span className="stat-pill">{label}</span>;
}

function CodeText({ value }) {
  return <code>{text(value)}</code>;
}

function BranchName({ entry, className = "" }) {
  const classes = ["branch-name-label", className, worktreeDeleted(entry) ? "branch-name-deleted" : ""]
    .filter(Boolean)
    .join(" ");
  return <span className={classes}>{text(entry.branch || "")}</span>;
}

function WorktreeStatusBadges({ entry }) {
  const badges = [];
  if (worktreeDeleted(entry)) {
    badges.push({
      key: "deleted",
      label: "deleted",
      title: text(entry.worktree_status_message || "worktree directory not found"),
      className: "worktree-status-deleted",
    });
  }
  if (worktreeSubmodule(entry)) {
    badges.push({
      key: "submodule",
      label: "submodule",
      title: text(entry.worktree_status_message || entry.worktree_superproject || "git submodule checkout"),
      className: "worktree-status-submodule",
    });
  }
  if (!badges.length) return null;
  return (
    <span className="worktree-status-badges">
      {badges.map((badge) => (
        <span className={`worktree-status-badge ${badge.className}`} title={badge.title} key={badge.key}>
          {badge.label}
        </span>
      ))}
    </span>
  );
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
    <div className="running-actions">
      {projects.map((project) => (
        <div className="running-action" key={project}>
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

function RunningBranchesMenu({ branches, onAction }) {
  const [open, setOpen] = useState(false);
  const hasItems = branches.length > 0;
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="running-menu" data-running-branch-menu>
      <button
        className={`running-menu-button ${hasItems ? "has-items" : "is-empty"}`}
        type="button"
        title="running branches"
        disabled={!hasItems}
        aria-expanded={open}
        onClick={() => {
          if (hasItems) setOpen(!open);
        }}
      >
        <Chevron className="button-icon" aria-hidden="true" />
        <GitBranch className="button-icon" aria-hidden="true" />
        <span>{branches.length}</span>
        running
      </button>
      {open && hasItems ? (
        <div className="running-list" role="menu">
          {branches.map((branch) => (
            <div className="running-row" key={branch.branch}>
              <div className="running-main">
                <BranchName entry={branch} />
                <WorktreeStatusBadges entry={branch} />
                <span className="branch-path-note" title={branch.worktree}>{branch.worktree_title}</span>
              </div>
              <BranchActions branch={branch} onAction={onAction} />
            </div>
          ))}
        </div>
      ) : null}
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
  if (!services.length) {
    return <div className="branch-empty">No running services for this branch.</div>;
  }
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
            [...(service.endpoints || [])].sort(compareEndpointOrder).map((endpoint) => (
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
          <BranchName entry={branch} className="branch-name branch-running-name" />
          <WorktreeStatusBadges entry={branch} />
          <span className="branch-path-note" title={branch.worktree}>{branch.worktree_title}</span>
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
  const branches = project.worktrees.flatMap((worktree) => worktree.branches);
  const deadInstances = project.worktrees.flatMap((worktree) => worktree.dead_instances);
  return {
    worktreeTotal: project.worktrees.length,
    runningBranchTotal: branches.length,
    deadBranchTotal: deadInstances.length,
  };
}

function DeadInstancesMenu({ instances, onStart }) {
  const [open, setOpen] = useState(false);
  const hasItems = instances.length > 0;
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="dead-menu" data-dead-panel>
      <button
        className={`dead-menu-button ${hasItems ? "has-items" : "is-empty"}`}
        type="button"
        title="dead branches"
        disabled={!hasItems}
        aria-expanded={open}
        onClick={() => {
          if (hasItems) setOpen(!open);
        }}
      >
        <Chevron className="button-icon" aria-hidden="true" />
        <GitBranch className="button-icon" aria-hidden="true" />
        <span>{instances.length}</span>
        dead
      </button>
      {open && hasItems ? (
        <div className="dead-list" role="menu">
          {instances.map((instance) => (
            <div className="dead-row" key={`${instance.worktree}-${instance.branch}-${instance.compose_project || ""}`}>
              <div className="dead-main">
                <BranchName entry={instance} />
                <WorktreeStatusBadges entry={instance} />
                <span className="branch-path-note" title={instance.worktree}>{instance.worktree_title}</span>
                {instance.source === "history" ? <span className="dead-note">history</span> : null}
              </div>
              <button
                className="action-button start-button"
                title={`docker compose up -d in ${instance.worktree}`}
                type="button"
                onClick={() => onStart(instance)}
              >
                <Play className="button-icon" aria-hidden="true" />
                Start
              </button>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function WorktreePanel({ worktree, onAction, onStart }) {
  const [open, setOpen] = useState(true);
  const Chevron = open ? ChevronDown : ChevronRight;
  const hasBranches = worktree.branches.length > 0;
  return (
    <section className="worktree-group" data-worktree-group="true">
      <div className="worktree-header">
        <button className="worktree-toggle" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Chevron className="toggle-icon" aria-hidden="true" />
          <Boxes className="entity-icon worktree-icon" aria-hidden="true" />
          <span className="worktree-name">{worktree.worktree_title}</span>
          <span className="worktree-path" title={worktree.worktree}>{worktree.worktree}</span>
        </button>
        <div className="worktree-header-right">
          <RunningBranchesMenu branches={worktree.branches} onAction={onAction} />
          <DeadInstancesMenu instances={worktree.dead_instances} onStart={onStart} />
        </div>
      </div>
      {open && hasBranches ? (
        <div className="worktree-body">
          <BranchStack worktree={worktree} onAction={onAction} onStart={onStart} />
        </div>
      ) : null}
    </section>
  );
}

function ProjectPanel({ project, onAction, onStart }) {
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
            <StatPill label={`${stats.worktreeTotal} wt root`} />
            <StatPill label={`${stats.runningBranchTotal} running`} />
            {stats.deadBranchTotal ? <StatPill label={`${stats.deadBranchTotal} dead`} /> : null}
          </div>
        </div>
      </div>
      {open ? (
        <div className="project-body">
          {project.worktrees.map((worktree) => (
            <WorktreePanel
              worktree={worktree}
              key={worktree.id || worktree.worktree}
              onAction={onAction}
              onStart={onStart}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function BranchStack({ worktree, onAction, onStart }) {
  return (
    <>
      {worktree.branches.map((branch) => (
        <BranchPanel branch={branch} key={branch.branch} onAction={onAction} />
      ))}
    </>
  );
}

function CatalogTree({ catalog, onAction, onStart }) {
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
        <ProjectPanel
          project={project}
          key={project.repo_id || project.repo_name}
          onAction={onAction}
          onStart={onStart}
        />
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
  const statusLabel = status === "ok" ? "succ" : status === "failed" ? "failed" : "checking";
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

  async function handleStart(worktree) {
    const label = actionLabel("up");
    const target = worktree.worktree;
    if (!window.confirm(`Run docker compose up -d in ${target}?`)) return;
    const logId = `${Date.now()}-up-${target}`;
    const detail = `docker compose up -d`;
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
    const result = await composeWorktreeAction({
      worktree: target,
      failedText: "compose up failed",
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
        <CatalogTree catalog={catalog} onAction={handleAction} onStart={handleStart} />
      )}
      <SplitDnsTools catalog={catalog} />
    </main>
  );
}

createRoot(document.getElementById("root")).render(<CatalogApp />);
