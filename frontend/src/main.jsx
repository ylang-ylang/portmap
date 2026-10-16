import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Boxes,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Folder,
  GitBranch,
  GitFork,
  Link2,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  Trash2,
  Unlink2,
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

function worktreeDisplayPath(seed) {
  if (worktreeSubmodule(seed)) {
    const relative = text(seed.submodule_relative_path || "");
    if (relative) return `submodule path ${relative}`;
  }
  return text(seed.worktree || seed.worktree_root || worktreeRootPath(seed));
}

function worktreeDisplayTitle(seed) {
  if (worktreeSubmodule(seed)) {
    const repo = text(seed.superproject_repo_name || "");
    const relative = text(seed.submodule_relative_path || "");
    if (repo && relative) return `${repo} submodule path ${relative}`;
    if (relative) return `submodule path ${relative}`;
  }
  return text(seed.worktree || seed.worktree_root || worktreeRootPath(seed));
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
    const displayPath = worktreeDisplayPath(seed);
    const displayTitle = worktreeDisplayTitle(seed);
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
        worktree_path_label: displayPath,
        worktree_path_title: displayTitle,
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
    worktree.worktree_path_label = worktree.worktree_path_label || displayPath;
    worktree.worktree_path_title = worktree.worktree_path_title || displayTitle;
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
  const host = text(endpoint.host).includes(":") ? `[${endpoint.host}]` : endpoint.host;
  if (endpoint.url) return text(endpoint.url);
  if ((endpoint.kind === "tcp" || endpoint.kind === "udp") && endpoint.host && endpoint.host_port) {
    return `${host}:${endpoint.host_port}`;
  }
  if (endpoint.kind === "range" && endpoint.host && endpoint.host_port) {
    let rangeText = "";
    if (endpoint.range_start && endpoint.range_end) {
      rangeText = ` relay ${endpoint.range_start}-${endpoint.range_end}`;
    }
    return `${host}:${endpoint.host_port}${rangeText}`;
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

async function composeProjectAction({ action, composeProject, machineId, failedText }) {
  const url = `/actions/compose-${action}?machine=${encodeURIComponent(machineId || "local")}`;
  const body = new URLSearchParams({ compose_project: composeProject });
  try {
    const response = await fetch(url, {
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

async function composeWorktreeAction({ worktree, machineId, failedText }) {
  const url = `/actions/compose-up?machine=${encodeURIComponent(machineId || "local")}`;
  const body = new URLSearchParams({ worktree });
  try {
    const response = await fetch(url, {
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

function formatLatency(ms) {
  // em dash for null/unmeasured; never show a fake zero
  return ms == null ? "\u2014" : `${ms}\u2009ms`;
}

function formatCheckedAt(iso) {
  if (!iso) return "never";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso;
  }
}

const CONN_STATUS_LABEL = {
  configured: "saved",
  connecting: "connecting",
  reachable: "online",
  unreachable: "unreachable",
  disconnected: "offline",
  error: "error",
};

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
  if (endpoint.transport_supported === false) {
    return <span>{text(endpoint.reason) || "Unavailable from this client"}</span>;
  }
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
  const pathLabel = text(worktree.worktree_path_label || worktree.worktree);
  const pathTitle = text(worktree.worktree_path_title || worktree.worktree);
  return (
    <section className="worktree-group" data-worktree-group="true">
      <div className="worktree-header">
        <button className="worktree-toggle" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Chevron className="toggle-icon" aria-hidden="true" />
          <Boxes className="entity-icon worktree-icon" aria-hidden="true" />
          <span className="worktree-name">{worktree.worktree_title}</span>
          {worktreeSubmodule(worktree) ? (
            <span className="worktree-kind-badge" title="git submodule">
              <GitFork className="worktree-kind-icon" aria-hidden="true" />
              submodule
            </span>
          ) : null}
          {pathLabel ? <span className="worktree-path" title={pathTitle}>{pathLabel}</span> : null}
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

function CatalogTabs({ machines, selectedId, settingsSelected, onSelect, onSelectSettings }) {
  const tabRefs = useRef([]);

  function handleTabKeyDown(e, index) {
    const count = machines.length + 1;
    let next = -1;
    if (e.key === "ArrowRight") next = (index + 1) % count;
    else if (e.key === "ArrowLeft") next = (index - 1 + count) % count;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = count - 1;
    if (next === -1) return;
    e.preventDefault();
    tabRefs.current[next]?.focus();
    if (next === machines.length) onSelectSettings();
    else onSelect(machines[next].id);
  }

  return (
    <nav className="machine-nav" aria-label="Portmap navigation">
      <div className="machine-nav-scroll" role="tablist" aria-label="Machines and settings">
        {machines.map((machine, index) => {
          const isSelected = !settingsSelected && machine.id === selectedId;
          return (
            <button
              key={machine.id}
              id={`machine-tab-${machine.id}`}
              ref={(el) => { tabRefs.current[index] = el; }}
              role="tab"
              aria-selected={isSelected}
              aria-controls="page-panel-catalog"
              tabIndex={isSelected ? 0 : -1}
              type="button"
              title={!machine.available ? "Machine is offline" : undefined}
              className={[
                "machine-tab",
                isSelected ? "machine-tab-active" : "",
                !machine.available ? "machine-tab-unavailable" : "",
              ].filter(Boolean).join(" ")}
              onClick={() => onSelect(machine.id)}
              onKeyDown={(e) => handleTabKeyDown(e, index)}
            >
              <span className="machine-tab-name">{machine.label || machine.domain || machine.id}</span>
            </button>
          );
        })}
        <button
          ref={(el) => { tabRefs.current[machines.length] = el; }}
          id="page-tab-settings"
          role="tab"
          aria-selected={settingsSelected}
          aria-controls="page-panel-settings"
          tabIndex={settingsSelected || machines.length === 0 ? 0 : -1}
          type="button"
          className={`machine-tab machine-tab-settings${settingsSelected ? " machine-tab-active" : ""}`}
          onClick={onSelectSettings}
          onKeyDown={(e) => handleTabKeyDown(e, machines.length)}
        >
          Settings
        </button>
      </div>
    </nav>
  );
}


function detectPlatform() {
  const fingerprint = `${navigator.userAgent} ${navigator.platform || ""}`.toLowerCase();
  const mobile = /android|iphone|ipad/.test(fingerprint);
  const arch = /arm64|aarch64/.test(fingerprint) ? "arm64"
    : /x86_64|amd64|x64|win64|wow64/.test(fingerprint) ? "amd64" : null;
  const os = mobile ? null
    : /windows|win32|win64/.test(fingerprint) ? "windows"
    : /macintosh|macintel|darwin/.test(fingerprint) ? "darwin"
    : /linux/.test(fingerprint) ? "linux" : null;
  return { os, arch };
}

function formatSize(bytes) {
  if (bytes == null || bytes <= 0) return null;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function DownloadPanel({ catalog }) {
  const [release, setRelease] = useState(null);
  const [releaseError, setReleaseError] = useState("");
  const [releaseLoading, setReleaseLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function loadRelease() {
      try {
        const res = await fetch("/api/downloads");
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!cancelled) setRelease(data);
      } catch (err) {
        if (!cancelled) setReleaseError(`${err}`);
      } finally {
        if (!cancelled) setReleaseLoading(false);
      }
    }
    loadRelease();
    return () => { cancelled = true; };
  }, []);

  const downloadOrigin = new URL(window.location.origin);
  // A fresh machine cannot resolve .portmap before it has installed the client.
  const needsBootstrapAddress = downloadOrigin.hostname.endsWith(".portmap");
  if (needsBootstrapAddress && catalog?.http_address) {
    const address = catalog.http_address;
    downloadOrigin.hostname = address.includes(":") ? `[${address}]` : address;
  }
  const origin = needsBootstrapAddress && !catalog?.http_address ? "" : downloadOrigin.origin;
  const installCmd = origin
    ? `curl -fsSL ${shellSingleQuote(`${origin}/install.sh`)} | sh -s -- ${shellSingleQuote(origin)}`
    : "";
  const platforms = release?.platforms || [];
  const detected = detectPlatform();
  const suggested = platforms.find((p) => p.os === detected.os && p.arch === detected.arch) || null;

  return (
    <section className="utility-panel" aria-label="Download portmap">
      <details className="quick-setup portmap-download" open>
        <summary>
          <Download className="button-icon" aria-hidden="true" />
          {" "}Download portmap / 获取 portmap
        </summary>
        <div className="quick-setup-body">
          {releaseLoading && <p>Loading…</p>}
          {!releaseLoading && releaseError && (
            <p className="portmap-download-notice portmap-download-error">{releaseError}</p>
          )}
          {!releaseLoading && !releaseError && release && !release.available && (
            <p className="portmap-download-notice">
              {release.message || "portmap download is not available on this server."}
            </p>
          )}
          {!releaseLoading && !releaseError && release?.available && (
            <>
              <div className="quick-command">
                <div className="quick-setup-actions">
                  <p>
                    Install the standalone <code>portmap</code> command — detects OS/arch,
                    verifies the archive, installs <code>~/.local/share/portmap/&lt;version&gt;</code> and
                    links <code>~/.local/bin/portmap</code>. Installing the command needs no sudo or Python;
                    running it requires Docker or Podman.
                  </p>
                  {installCmd && <CopyButton targetId="portmap-install-cmd" />}
                </div>
                {installCmd
                  ? <pre><code id="portmap-install-cmd">{installCmd}</code></pre>
                  : <p className="portmap-download-notice">Open this page using the server's IP address to install on a machine without .portmap DNS.</p>}
              </div>
              {platforms.length > 0 && (
                <div className="quick-command">
                  <div className="portmap-platforms-label">Or download the archive directly:</div>
                  <ul className="portmap-platform-list">
                    {platforms.map((p) => {
                      const isSuggested = p === suggested;
                      const size = formatSize(p.size);
                      return (
                        <li
                          key={p.filename}
                          className={`portmap-platform-item${isSuggested ? " portmap-platform-suggested" : ""}`}
                        >
                          <code className="portmap-platform-badge">{p.os}/{p.arch}</code>
                          <a href={p.url} className="portmap-platform-link">{p.filename}</a>
                          {size && <span className="portmap-platform-size">{size}</span>}
                          {isSuggested && <span className="portmap-platform-tag">your platform</span>}
                          {p.requirements && <span className="portmap-platform-req" title={p.requirements}>{p.requirements}</span>}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </>
          )}
          <div className="quick-command">
            <div className="quick-setup-actions">
              <p>Homebrew installs the command. With a usable user service manager it also initializes access; otherwise it reports that automatic setup was not run. Docker or Podman is required.</p>
              <CopyButton targetId="portmap-brew-install" />
            </div>
            <pre><code id="portmap-brew-install">brew install ylang-ylang/tap/portmap</code></pre>
            <div className="quick-setup-actions">
              <p>Upgrade an existing brew installation:</p>
              <CopyButton targetId="portmap-brew-upgrade" />
            </div>
            <pre><code id="portmap-brew-upgrade">brew upgrade ylang-ylang/tap/portmap</code></pre>
          </div>
          <div className="quick-command">
            <p>Start with <code>portmap run</code> in the foreground, or <code>portmap setup</code> for background operation. Then add a remote machine if needed:</p>
            <pre><code>{`portmap connect <IP-or-SSH-target>\nportmap status --check`}</code></pre>
            <p>
              Setup uses your existing container runtime, preferring Docker when both
              Docker and Podman are available. It reports an unavailable API instead of
              installing or starting an engine. DNS and the local ingress gateway run
              in containers; no native CoreDNS or Traefik is installed on the host.
              Local DNS normally listens on <code>127.0.0.1:53</code>, and the private HTTP entry
              uses <code>127.0.0.1:80</code>. Ordinary DNS queries retain the original upstreams.
              Direct access goes to the remote IP; SSH and local projects share the private ingress.
              Setup does not start business projects or expose an entry to the LAN.
            </p>
            <div className="quick-setup-actions">
              <p>No systemd is required. Start the foreground runner:</p>
              <CopyButton targetId="portmap-run-foreground" />
            </div>
            <pre><code id="portmap-run-foreground">portmap run</code></pre>
            <p>
              Initialization explains changes to the current system resolver file and requests sudo
              when needed. Shared workspace resolver files may affect other containers sharing them.
              Unavailable ports or unsupported local DNS proxy policies are reported, not silently bypassed.
            </p>
            <p>
              To publish projects for remote HTTP or SSH access, <code>portmap up</code> does not
              require host DNS takeover or publicly exposed DNS. Access to remote machines does not
              require <code>portmap up</code>. Stopping the foreground runner preserves shared containers
              and saved connections. <code>portmap teardown</code> restores owned host DNS and removes
              remote connections; shared infrastructure remains while local publishing or project
              containers still need it.
            </p>
          </div>
        </div>
      </details>
    </section>
  );
}

function AccessStatus({ catalog }) {
  const [status, setStatus] = useState("checking");
  if (!catalog) {
    return (
      <span className="access-status access-status-checking">
        <span className="access-status-label">Access</span>
        <span className="access-status-value">loading</span>
      </span>
    );
  }
  const domain = text(catalog.dns_domain).replace(/\.$/, "");
  const fallbackHost = catalog.client_access ? text(catalog.client_access.domain) : `portmap.${domain}`;
  const catalogUrl = text(catalog.machine?.catalog_url)
    || `${window.location.protocol}//${fallbackHost}${currentPortSuffix()}/`;
  const probeUrl = new URL("/assets/dns-check.svg", catalogUrl);
  probeUrl.searchParams.set("ts", String(Date.now()));
  const host = probeUrl.host;
  const url = probeUrl.href;
  // This is a browser resource probe: a failure means this browser could not
  // load the URL (access failure), not a definitive DNS diagnosis. The probe
  // URL stays exposed via the tooltip and the link.
  const probeTitle = `Browser access check for ${url} — failure means this browser cannot load the resource, not a definitive DNS diagnosis.`;
  const statusLabel = status === "ok" ? "ok" : status === "failed" ? "no access" : "checking";
  return (
    <span className={`access-status access-status-${status}`} data-access-probe title={probeTitle}>
      <img
        className="access-status-image"
        data-access-probe-image
        src={url}
        alt=""
        onLoad={() => setStatus("ok")}
        onError={() => setStatus("failed")}
      />
      <span className="access-status-label">Access</span>
      <span className="access-status-value" data-access-probe-status>{statusLabel}</span>
      <OpenLink href={url} title={probeTitle}><CodeText value={host} /></OpenLink>
    </span>
  );
}


function ConnectionStatusBadge({ status }) {
  const label = CONN_STATUS_LABEL[status] || text(status) || "unknown";
  return (
    <span className={`conn-status-badge conn-status-${status || "configured"}`}>
      {label}
    </span>
  );
}

function ConnectionRow({ conn, onConnect, onDisconnect, onEdit, onDelete, mutating }) {
  const active = conn.has_connection === true;
  // Show actual_via when known (what's really used), fall back to requested via
  const via = conn.actual_via || conn.via;
  return (
    <div className="conn-row" role="listitem">
      <div className="conn-row-body">
        <div className="conn-row-main">
          <ConnectionStatusBadge status={conn.status} />
          <span className="conn-row-target">{conn.target}</span>
          {via && (
            <span
              className={`machine-via-badge machine-via-${
                via === "direct" || via === "ssh" ? via : "auto"
              }`}
            >
              {via}
            </span>
          )}
          {conn.domain && conn.domain !== conn.target && (
            <span className="conn-row-domain">{conn.domain}</span>
          )}
        </div>
        <div className="conn-row-meta">
          <span className="conn-latency" title="HTTP latency from last check">
            {formatLatency(conn.latency_ms)}
          </span>
          {conn.last_checked_at && (
            <span className="conn-checked">
              {"· checked "}
              {formatCheckedAt(conn.last_checked_at)}
            </span>
          )}
          {conn.error && (
            <span className="conn-error-inline" title={conn.error}>
              {conn.error}
            </span>
          )}
        </div>
      </div>
      <div className="conn-row-actions">
        {active ? (
          <button
            className="action-button conn-disconnect-btn"
            type="button"
            onClick={() => onDisconnect(conn.id)}
            disabled={mutating}
          >
            <Unlink2 className="button-icon" aria-hidden="true" />
            {" "}Disconnect
          </button>
        ) : (
          <button
            className="action-button conn-connect-btn"
            type="button"
            onClick={() => onConnect(conn.id)}
            disabled={mutating}
          >
            <Link2 className="button-icon" aria-hidden="true" />
            {" "}Connect
          </button>
        )}
        <button
          className="action-button"
          type="button"
          onClick={() => onEdit(conn)}
          disabled={mutating}
          title={active ? "Disconnect before editing" : `Edit ${conn.target || conn.id}`}
          aria-label={
            active
              ? `Edit ${conn.target || conn.id} — disconnect first`
              : `Edit ${conn.target || conn.id}`
          }
        >
          <Pencil className="button-icon" aria-hidden="true" />
        </button>
        <button
          className="action-button conn-delete-btn"
          type="button"
          onClick={() => onDelete(conn.id, conn.target)}
          disabled={mutating}
          aria-label={`Delete ${conn.target || conn.id}`}
        >
          <Trash2 className="button-icon" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

function ConnectionForm({ formState, onChange, onSave, onCancel, formError, mutating }) {
  const isEdit = Boolean(formState.id);
  function handleKeyDown(e) {
    if (e.key === "Enter") onSave();
    if (e.key === "Escape") onCancel();
  }
  return (
    <div
      className="conn-form"
      role="form"
      aria-label={isEdit ? "Edit connection" : "Add connection"}
    >
      <div className="conn-form-row">
        <label className="conn-form-label" htmlFor="conn-form-target">
          {isEdit ? "Edit host" : "New host"}
        </label>
        <input
          id="conn-form-target"
          className="conn-form-input"
          type="text"
          value={formState.target}
          onChange={(e) => onChange({ ...formState, target: e.target.value })}
          onKeyDown={handleKeyDown}
          placeholder="192.0.2.10 · user@host · SSH alias"
          autoComplete="off"
          autoFocus
          disabled={mutating}
        />
        <select
          className="conn-form-select"
          value={formState.via}
          onChange={(e) => onChange({ ...formState, via: e.target.value })}
          disabled={mutating}
          aria-label="Connection mode"
        >
          <option value="direct">direct</option>
          <option value="ssh">SSH</option>
        </select>
        <button
          className="action-button conn-save-btn"
          type="button"
          onClick={onSave}
          disabled={mutating || !formState.target.trim()}
        >
          {mutating ? "Saving\u2026" : isEdit ? "Update" : "Save"}
        </button>
        <button
          className="action-button"
          type="button"
          onClick={onCancel}
          disabled={mutating}
        >
          Cancel
        </button>
      </div>
      {formError && (
        <div className="conn-form-error" role="alert">
          {formError}
        </div>
      )}
    </div>
  );
}

function ConnectionManager({ canManageConnections, onMachinesRefresh }) {
  // null = not yet loaded; [] or [...] = loaded
  const [connections, setConnections] = useState(null);
  const [connectionsError, setConnectionsError] = useState("");
  const [checkLoading, setCheckLoading] = useState(false);
  // formState: null (hidden) | { id?, target, via }
  const [formState, setFormState] = useState(null);
  const [formError, setFormError] = useState("");
  // panelError: connect/disconnect/delete failures or edit-blocked messages
  const [panelError, setPanelError] = useState("");
  const [mutating, setMutating] = useState(false);

  // Load connection list once the private portal confirms management capability.
  useEffect(() => {
    if (!canManageConnections) return;
    loadConnections();
  }, [canManageConnections]);

  async function loadConnections({ check = false } = {}) {
    if (check) setCheckLoading(true);
    setConnectionsError("");
    try {
      const res = await fetch(check ? "/api/connections?check=1" : "/api/connections");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setConnections(Array.isArray(data.connections) ? data.connections : []);
    } catch (err) {
      setConnectionsError(`${err.message || err}`);
    } finally {
      if (check) setCheckLoading(false);
    }
  }

  // POST a connection action. On non-2xx, refetch the list (so any saved-but-failed
  // entry remains visible) then return the error for the caller to surface.
  async function postAction(payload) {
    setMutating(true);
    try {
      const res = await fetch("/api/connections", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      let data;
      try { data = await res.json(); } catch { data = {}; }
      if (!res.ok) {
        const errMsg = text(data.error) || `HTTP ${res.status}`;
        // Refetch so any saved-but-failed row is visible.
        await loadConnections();
        await onMachinesRefresh();
        return { failed: true, error: errMsg };
      }
      // Success: server returns updated list payload.
      setConnections(Array.isArray(data.connections) ? data.connections : []);
      setConnectionsError("");
      await onMachinesRefresh();
      return { failed: false };
    } catch (err) {
      // Network/parse error: refetch to show current state.
      await loadConnections();
      await onMachinesRefresh();
      return { failed: true, error: `${err.message || err}` };
    } finally {
      setMutating(false);
    }
  }

  async function handleSave() {
    if (!formState) return;
    const { id, target, via } = formState;
    const trimmed = target.trim();
    if (!trimmed) {
      setFormError("Host or IP address is required.");
      return;
    }
    const payload = { action: "save", target: trimmed, via };
    if (id) payload.id = id;
    const result = await postAction(payload);
    if (!result.failed) {
      setFormState(null);
      setFormError("");
    } else {
      setFormError(result.error);
    }
  }

  async function handleConnect(id) {
    setPanelError("");
    // Capture id before any async gap.
    const result = await postAction({ action: "connect", id });
    if (result.failed) setPanelError(result.error);
    else setPanelError("");
  }

  async function handleDisconnect(id) {
    setPanelError("");
    const result = await postAction({ action: "disconnect", id });
    if (result.failed) setPanelError(result.error);
    else setPanelError("");
  }

  async function handleDelete(id, target) {
    // Capture id and label before the async confirm gap.
    const label = target || id;
    if (
      !window.confirm(
        `Delete connection "${label}"?\nThis will disconnect it first if currently active.`,
      )
    ) return;
    setPanelError("");
    const result = await postAction({ action: "delete", id });
    if (result.failed) setPanelError(result.error);
    else setPanelError("");
  }

  function startEdit(conn) {
    // Contract: editing an active entry requires explicit disconnect first.
    const active = conn.has_connection === true || conn.status === "connecting";
    if (active) {
      setPanelError(`Disconnect "${text(conn.target || conn.id)}" before editing.`);
      return;
    }
    // Prefer explicit via for the form; auto/unknown defaults to direct.
    const via =
      conn.via === "direct" || conn.via === "ssh" ? conn.via
      : conn.actual_via === "direct" || conn.actual_via === "ssh" ? conn.actual_via
      : "direct";
    setFormState({ id: conn.id, target: text(conn.target), via });
    setFormError("");
    setPanelError("");
  }

  function startAdd() {
    setFormState({ target: "", via: "direct" });
    setFormError("");
    setPanelError("");
  }

  function cancelForm() {
    setFormState(null);
    setFormError("");
  }

  if (!canManageConnections) return null;

  return (
    <section className="utility-panel" aria-label="Connection management">
      <details className="quick-setup conn-section" open>
        <summary>
          <Link2 className="button-icon" aria-hidden="true" />
          {" "}Connections
        </summary>
        <div className="quick-setup-body conn-body">
          <div className="conn-toolbar">
            <button
              className="action-button"
              type="button"
              onClick={() => loadConnections({ check: true })}
              disabled={checkLoading || mutating}
              title="Probe all connections and measure HTTP latency"
            >
              <RefreshCw className="button-icon" aria-hidden="true" />
              {" "}{checkLoading ? "Checking\u2026" : "Check"}
            </button>
            <button
              className="action-button conn-add-btn"
              type="button"
              onClick={startAdd}
              disabled={mutating || formState !== null}
              aria-label="Add new connection"
            >
              <Plus className="button-icon" aria-hidden="true" />
              {" "}Add
            </button>
          </div>
          {connectionsError && (
            <div className="empty load-error" role="alert">
              {connectionsError}{" "}
              <button
                className="action-button"
                type="button"
                onClick={() => loadConnections()}
              >
                Retry
              </button>
            </div>
          )}
          {panelError && (
            <div className="conn-panel-error" role="alert">
              {panelError}
            </div>
          )}
          {connections === null && !connectionsError && (
            <div className="empty">Loading connections\u2026</div>
          )}
          {connections !== null && connections.length === 0 && !formState && (
            <div className="empty">
              No saved connections.{" "}
              <button
                className="action-button"
                type="button"
                onClick={startAdd}
                disabled={mutating}
              >
                Add a host
              </button>{" "}
              to connect to a remote machine.
            </div>
          )}
          {connections !== null && connections.length > 0 && (
            <div className="conn-list" role="list">
              {connections.map((conn) => (
                <ConnectionRow
                  key={conn.id ?? conn.target}
                  conn={conn}
                  onConnect={handleConnect}
                  onDisconnect={handleDisconnect}
                  onEdit={startEdit}
                  onDelete={handleDelete}
                  mutating={mutating}
                />
              ))}
            </div>
          )}
          {formState !== null && (
            <ConnectionForm
              formState={formState}
              onChange={setFormState}
              onSave={handleSave}
              onCancel={cancelForm}
              formError={formError}
              mutating={mutating}
            />
          )}
        </div>
      </details>
    </section>
  );
}

function CatalogApp() {
  const [activePage, setActivePage] = useState("catalog");
  // null = not yet fetched (loading or failed); [] or [...] = loaded
  const [machines, setMachines] = useState(null);
  const [machinesError, setMachinesError] = useState("");
  // null = waiting for /api/machines to resolve; never falls back silently
  const [selectedMachineId, setSelectedMachineId] = useState(null);
  const [catalog, setCatalog] = useState(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState("");
  const [actionMessage, setActionMessage] = useState(null);
  const [actionLog, setActionLog] = useState([]);
  const [canManageConnections, setCanManageConnections] = useState(false);
  // Shared abort ref: the controller stored here is the authoritative catalog request.
  // Both the useEffect and reloadCatalog write to this ref after aborting the prior value,
  // so whichever started last is the one that may apply its result.
  const catalogAbortRef = useRef(null);

  // Fetch machine list. keepSelection=true updates the list without changing selectedMachineId.
  // Failures are surfaced as errors; no silent fallback. /api/machines works in both private
  // (multi-machine) and public (single-local) scopes.
  async function loadMachines({ keepSelection = false } = {}) {
    if (!keepSelection) setMachinesError("");
    try {
      const res = await fetch("/api/machines");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const list = Array.isArray(data.machines) ? data.machines : [];
      setMachines(list);
      setCanManageConnections(data.can_manage_connections === true);
      setMachinesError("");
      if (!keepSelection) {
        setSelectedMachineId(data.selected || list[0]?.id || "local");
      }
    } catch (err) {
      setMachinesError(`Failed to load machines: ${err.message || err}`);
      // On initial failure: machines stays null, selectedMachineId stays null.
      // The catalog area renders a Retry button. No automatic fallback to local.
    }
  }

  // When selectedMachineId changes: abort any in-flight catalog request, clear the tree,
  // fetch the new machine's catalog. Always sends machine= — omitting it causes the server
  // to select by Host header, which returns the wrong catalog on SSH-apex URLs.
  useEffect(() => {
    if (selectedMachineId === null) return;
    if (catalogAbortRef.current) catalogAbortRef.current.abort();
    const controller = new AbortController();
    catalogAbortRef.current = controller;
    setCatalog(null);
    setCatalogLoading(true);
    setCatalogError("");
    setActionMessage(null);
    fetch(`/registry.json?machine=${encodeURIComponent(selectedMachineId)}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data) => {
        if (catalogAbortRef.current === controller) {
          setCatalog(data);
          setCatalogError("");
          setCatalogLoading(false);
        }
      })
      .catch((err) => {
        if (err.name !== "AbortError" && catalogAbortRef.current === controller) {
          setCatalogError(`Failed to load catalog: ${err.message || err}`);
          setCatalogLoading(false);
        }
      });
    return () => controller.abort();
  }, [selectedMachineId]);

  useEffect(() => { loadMachines(); }, []);

  // Post-action reload. Uses the same catalogAbortRef so the A→B→A race is handled:
  // switching B→A triggers a new useEffect fetch that becomes catalogAbortRef.current;
  // if an older reloadCatalog for A then finishes, its controller !== catalogAbortRef.current
  // and the result is silently discarded without touching state.
  async function reloadCatalog() {
    if (selectedMachineId === null) return;
    if (catalogAbortRef.current) catalogAbortRef.current.abort();
    const controller = new AbortController();
    catalogAbortRef.current = controller;
    try {
      const res = await fetch(
        `/registry.json?machine=${encodeURIComponent(selectedMachineId)}`,
        { signal: controller.signal },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (catalogAbortRef.current === controller) {
        setCatalog(data);
        setCatalogError("");
      }
    } catch (err) {
      if (err.name !== "AbortError" && catalogAbortRef.current === controller) {
        setCatalogError(`Failed to reload catalog: ${err.message || err}`);
      }
    }
    // Refresh machines list; never changes selection.
    try {
      const res = await fetch("/api/machines");
      if (res.ok) {
        const data = await res.json();
        setMachines(Array.isArray(data.machines) ? data.machines : []);
        setMachinesError("");
      }
    } catch {
      // Non-critical; existing list retained.
    }
  }

  // Poll only while the catalog is visible. Schedule after completion so slow
  // remote catalogs never accumulate requests; focus/visibility events coalesce.
  useEffect(() => {
    if (activePage !== "catalog" || catalogLoading) return;
    let stopped = false;
    let running = false;
    let timer;

    function schedule() {
      clearTimeout(timer);
      if (!stopped && document.visibilityState === "visible") {
        timer = setTimeout(refresh, 15000);
      }
    }

    async function refresh() {
      clearTimeout(timer);
      if (stopped || running || document.visibilityState !== "visible") return;
      running = true;
      try {
        if (selectedMachineId === null) await loadMachines();
        else await reloadCatalog();
      } finally {
        running = false;
        schedule();
      }
    }

    function visibilityChanged() {
      if (document.visibilityState === "visible") void refresh();
      else clearTimeout(timer);
    }

    schedule();
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      stopped = true;
      clearTimeout(timer);
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", visibilityChanged);
    };
  }, [activePage, selectedMachineId, catalogLoading]);

  function handleSelectMachine(id) {
    setActivePage("catalog");
    if (id === selectedMachineId) {
      if (!catalogLoading) void reloadCatalog();
      return;
    }
    setSelectedMachineId(id);
  }

  async function handleAction(action, composeProject) {
    // Capture machine ID synchronously before confirm() — no async gap possible.
    const machineId = selectedMachineId;
    const machine = machines ? machines.find((m) => m.id === machineId) : null;
    const machineLabel = machine
      ? (machine.label || machine.domain || machineId)
      : (machineId || "local");
    const label = actionLabel(action);
    if (!window.confirm(`Run docker compose ${action} for ${composeProject} on ${machineLabel}?`)) return;
    const logId = `${Date.now()}-${action}-${composeProject}`;
    const detail = `docker compose -p ${composeProject} ${action}`;
    setActionLog((entries) => [
      { id: logId, time: nowLabel(), status: "running", message: `${label} requested on ${machineLabel}`, detail },
      ...entries,
    ].slice(0, 8));
    const result = await composeProjectAction({
      action, composeProject, machineId, failedText: `compose ${action} failed`,
    });
    setActionMessage(result);
    setActionLog((entries) => entries.map((entry) => (
      entry.id === logId
        ? {
          ...entry,
          time: nowLabel(),
          status: result.failed ? "failed" : "ok",
          message: result.failed
            ? `${label} failed on ${machineLabel}: ${result.message}`
            : `${label} completed on ${machineLabel}: ${result.message}`,
        }
        : entry
    )));
    if (!result.failed) await reloadCatalog();
  }

  async function handleStart(worktree) {
    const machineId = selectedMachineId;
    const machine = machines ? machines.find((m) => m.id === machineId) : null;
    const machineLabel = machine
      ? (machine.label || machine.domain || machineId)
      : (machineId || "local");
    const label = actionLabel("up");
    const target = worktree.worktree;
    if (!window.confirm(`Run docker compose up -d in ${target} on ${machineLabel}?`)) return;
    const logId = `${Date.now()}-up-${target}`;
    setActionLog((entries) => [
      { id: logId, time: nowLabel(), status: "running", message: `${label} requested on ${machineLabel}`, detail: "docker compose up -d" },
      ...entries,
    ].slice(0, 8));
    const result = await composeWorktreeAction({
      worktree: target, machineId, failedText: "compose up failed",
    });
    setActionMessage(result);
    setActionLog((entries) => entries.map((entry) => (
      entry.id === logId
        ? {
          ...entry,
          time: nowLabel(),
          status: result.failed ? "failed" : "ok",
          message: result.failed
            ? `${label} failed on ${machineLabel}: ${result.message}`
            : `${label} completed on ${machineLabel}: ${result.message}`,
        }
        : entry
    )));
    if (!result.failed) await reloadCatalog();
  }

  const selectedMachine = machines ? machines.find((m) => m.id === selectedMachineId) : null;
  const machineDisappeared = machines !== null && machines.length > 0
    && selectedMachineId !== null && !selectedMachine;
  const machineUnavailable = selectedMachine && !selectedMachine.available;

  let catalogContent;
  if (machines === null) {
    // /api/machines not yet resolved: loading or failed.
    catalogContent = machinesError ? (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty load-error">
          {machinesError}{" "}
          <button className="action-button" type="button" onClick={() => loadMachines()}>Retry</button>
        </div>
      </section>
    ) : (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty">Loading…</div>
      </section>
    );
  } else if (machineDisappeared) {
    catalogContent = (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty load-error">
          Machine <code>{selectedMachineId}</code> is no longer connected.
          Select another machine above to continue.
        </div>
      </section>
    );
  } else if (machineUnavailable) {
    catalogContent = (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty load-error">
          <strong>{text(selectedMachine.label || selectedMachine.domain)}</strong>
          {selectedMachine.via && selectedMachine.via !== "local"
            ? ` (${selectedMachine.via}${selectedMachine.target ? ` → ${selectedMachine.target}` : ""})`
            : ""}
          {" "}is not currently connected. Choose another machine or reconnect.
        </div>
      </section>
    );
  } else if (catalogLoading) {
    catalogContent = (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty">
          {`Loading${selectedMachine && selectedMachine.via !== "local" ? ` ${text(selectedMachine.label || selectedMachine.domain)}` : ""} catalog…`}
        </div>
      </section>
    );
  } else if (catalogError) {
    catalogContent = (
      <section className="catalog-tree" data-catalog-tree>
        <div className="empty load-error">
          {catalogError}{" "}
          <button className="action-button" type="button" onClick={() => reloadCatalog()}>Retry</button>
        </div>
      </section>
    );
  } else {
    catalogContent = <CatalogTree catalog={catalog} onAction={handleAction} onStart={handleStart} />;
  }

  return (
    <main>
      <header className="app-header">
        <div className="title-row">
          <h1>portmap catalog</h1>
          {activePage === "catalog" && <AccessStatus catalog={catalog} />}
        </div>
        <CatalogTabs
          machines={machines || []}
          selectedId={selectedMachineId}
          settingsSelected={activePage === "settings"}
          onSelect={handleSelectMachine}
          onSelectSettings={() => setActivePage("settings")}
        />
      </header>
      <section
        id="page-panel-catalog"
        className="page-panel"
        role="tabpanel"
        aria-labelledby={selectedMachine ? `machine-tab-${selectedMachine.id}` : undefined}
        aria-label={selectedMachine ? undefined : "Service catalog"}
        hidden={activePage !== "catalog"}
        tabIndex={0}
      >
        {machinesError && machines !== null && (
          <p className="meta machine-nav-error">
            {machinesError}{" "}
            <button className="action-button" type="button" onClick={() => loadMachines({ keepSelection: true })}>Retry</button>
          </p>
        )}
        <ActionMessage message={actionMessage} />
        <ActionLog entries={actionLog} />
        {catalogContent}
      </section>
      <section
        id="page-panel-settings"
        className="page-panel settings-panel"
        role="tabpanel"
        aria-labelledby="page-tab-settings"
        hidden={activePage !== "settings"}
        tabIndex={0}
      >
        <ConnectionManager
          canManageConnections={canManageConnections}
          onMachinesRefresh={() => loadMachines({ keepSelection: true })}
        />
        <DownloadPanel catalog={catalog?.client_access ? null : catalog} />
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<CatalogApp />);
