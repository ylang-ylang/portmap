import "./style.css";

function text(value) {
  return value == null ? "" : String(value);
}

function createElement(tag, options = {}) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text != null) element.textContent = text(options.text);
  if (options.title != null) element.title = text(options.title);
  if (options.type != null) element.type = options.type;
  return element;
}

function codeElement(value) {
  return createElement("code", { text: value });
}

function clear(element) {
  while (element.firstChild) {
    element.removeChild(element.firstChild);
  }
}

function appendText(parent, value) {
  parent.appendChild(document.createTextNode(value));
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

function setupCopyButtons() {
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) return;
      await copyText(`${target.textContent.trim()}\n`);
      const original = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => {
        button.textContent = original;
      }, 1200);
    });
  });
}

function shellSingleQuote(value) {
  return "'" + text(value).replace(/'/g, "'\"'\"'") + "'";
}

function currentPortSuffix() {
  return window.location.port ? `:${window.location.port}` : "";
}

function splitDnsSetupCommand(catalog) {
  const dnsServer = shellSingleQuote(catalog.dns_server);
  const dnsDomain = shellSingleQuote(catalog.dns_domain);
  return `DNS_SERVER=${dnsServer}
DNS_DOMAIN=${dnsDomain}
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl dns "$DNS_IFACE" "$DNS_SERVER"
sudo resolvectl domain "$DNS_IFACE" "~$DNS_DOMAIN"
resolvectl query "portmap.$DNS_DOMAIN"
`;
}

function splitDnsUnsetCommand(catalog) {
  const dnsServer = shellSingleQuote(catalog.dns_server);
  const dnsDomain = shellSingleQuote(catalog.dns_domain);
  return `DNS_SERVER=${dnsServer}
DNS_DOMAIN=${dnsDomain}
DNS_IFACE="$(ip route get "$DNS_SERVER" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"

sudo resolvectl revert "$DNS_IFACE"
resolvectl query "portmap.$DNS_DOMAIN"
`;
}

function setProbeStatus(probe, state, label) {
  const status = probe.querySelector("[data-dns-probe-status]");
  if (!status) return;
  status.classList.remove("dns-probe-status-checking", "dns-probe-status-ok", "dns-probe-status-failed");
  status.classList.add(`dns-probe-status-${state}`);
  status.textContent = label;
}

function setupDnsProbe(catalog) {
  const probe = document.querySelector("[data-dns-probe]");
  if (!probe) return;

  const domain = text(catalog.dns_domain).replace(/\.$/, "");
  const host = `portmap.${domain}`;
  const image = probe.querySelector("[data-dns-probe-image]");
  const link = probe.querySelector("[data-dns-probe-url]");
  const linkCode = link ? link.querySelector("code") : null;
  const message = probe.querySelector("[data-dns-probe-message]");
  if (!image || !link || !linkCode || !message) return;

  const url = `${window.location.protocol}//${host}${currentPortSuffix()}/assets/dns-check.svg?ts=${Date.now()}`;
  link.href = url;
  linkCode.textContent = url;

  setProbeStatus(probe, "checking", "checking");
  message.innerHTML = "";
  appendText(message, "Checking whether this client can resolve ");
  message.appendChild(codeElement(host));
  appendText(message, ".");

  image.classList.remove("is-hidden");
  image.onload = () => {
    setProbeStatus(probe, "ok", "ok");
    message.innerHTML = "";
    appendText(message, "This browser can resolve and load ");
    message.appendChild(codeElement(host));
    appendText(message, ".");
  };
  image.onerror = () => {
    setProbeStatus(probe, "failed", "failed");
    image.classList.add("is-hidden");
    message.innerHTML = "";
    appendText(message, "This browser cannot load ");
    message.appendChild(codeElement(host));
    appendText(message, ". Configure split DNS for ");
    message.appendChild(codeElement(domain));
    appendText(message, " on this client machine.");
  };
  image.src = url;
}

function endpointCount(service) {
  return (service.endpoints || []).length;
}

function uniqueSorted(values) {
  return Array.from(new Set(values.filter(Boolean).map(text))).sort((left, right) => left.localeCompare(right));
}

function buildCatalogTree(catalog) {
  const projects = new Map();
  for (const service of catalog.services || []) {
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

function renderExternalCell(endpoint) {
  const cell = document.createElement("td");
  const external = endpointExternal(endpoint);
  if (!external) return cell;
  if (external.startsWith("http://") || external.startsWith("https://")) {
    const link = document.createElement("a");
    link.href = external;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.appendChild(codeElement(external));
    cell.appendChild(link);
    return cell;
  }
  cell.appendChild(codeElement(external));
  return cell;
}

function tableCellWithCode(value) {
  const cell = document.createElement("td");
  cell.appendChild(codeElement(value));
  return cell;
}

function renderServiceRows(service) {
  return (service.endpoints || []).map((endpoint) => {
    const row = document.createElement("tr");
    row.appendChild(tableCellWithCode(service.worktree || ""));
    row.appendChild(tableCellWithCode(service.compose_service || ""));
    row.appendChild(tableCellWithCode(endpoint.name || endpoint.id || ""));
    row.appendChild(tableCellWithCode(endpoint.kind || ""));
    row.appendChild(renderExternalCell(endpoint));
    row.appendChild(tableCellWithCode(endpoint.container_port || ""));
    row.appendChild(tableCellWithCode(service.container || ""));
    row.appendChild(tableCellWithCode(service.image || ""));
    return row;
  });
}

async function composeDownProject(composeProject) {
  await composeProjectAction({
    action: "down",
    composeProject,
    confirmText: `Run docker compose down for ${composeProject}?`,
    failedText: "compose down failed",
  });
}

async function composeRestartProject(composeProject) {
  await composeProjectAction({
    action: "restart",
    composeProject,
    confirmText: `Run docker compose restart for ${composeProject}?`,
    failedText: "compose restart failed",
  });
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

async function composeProjectAction({ action, composeProject, confirmText, failedText }) {
  if (!window.confirm(confirmText)) return;
  const body = new URLSearchParams({ compose_project: composeProject });
  let result;
  try {
    const response = await fetch(`/actions/compose-${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    result = await parseActionResponse(response);
    if (!response.ok || !result.ok) {
      showActionMessage(result.message || failedText, true);
      return;
    }
  } catch (error) {
    showActionMessage(`${failedText}: ${error}`, true);
    return;
  }
  showActionMessage(`${result.message}: ${composeProject}`, false);
  await loadCatalog();
}

function showActionMessage(message, failed) {
  let element = document.querySelector("[data-action-message]");
  if (!element) {
    element = createElement("p", { className: "action-message" });
    element.dataset.actionMessage = "true";
    const meta = document.querySelector("[data-catalog-meta]");
    if (meta && meta.parentNode) {
      meta.parentNode.insertBefore(element, meta.nextSibling);
    }
  }
  element.classList.toggle("action-message-error", failed);
  element.textContent = message;
}

function renderBranchActions(branch) {
  const projects = uniqueSorted(branch.services.map((service) => service.compose_project));
  if (!projects.length) return document.createElement("div");

  const wrapper = createElement("div", { className: "branch-actions" });
  for (const project of projects) {
    const action = createElement("div", { className: "project-action" });
    const restartButton = createElement("button", {
      className: "action-button restart-button",
      text: "Restart",
      title: `docker compose -p ${project} restart`,
      type: "button",
    });
    const downButton = createElement("button", {
      className: "action-button down-button",
      text: "Down",
      title: `docker compose -p ${project} down`,
      type: "button",
    });
    restartButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      composeRestartProject(project);
    });
    downButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      composeDownProject(project);
    });
    action.appendChild(codeElement(project));
    action.appendChild(restartButton);
    action.appendChild(downButton);
    wrapper.appendChild(action);
  }
  return wrapper;
}

function branchSummaryStats(branch) {
  const endpointTotal = branch.services.reduce((total, service) => total + endpointCount(service), 0);
  const worktrees = uniqueSorted(branch.services.map((service) => service.worktree));
  return { endpointTotal, worktrees };
}

function renderBranch(branch) {
  const section = createElement("details", { className: "branch-group" });
  section.open = true;
  section.setAttribute("data-branch-group", "true");

  const summary = createElement("summary", { className: "branch-summary" });
  const title = createElement("div", { className: "branch-title" });
  appendText(title, "Branch ");
  title.appendChild(codeElement(branch.branch));

  const stats = branchSummaryStats(branch);
  const meta = createElement("div", { className: "branch-meta" });
  appendText(meta, `${branch.services.length} services, ${stats.endpointTotal} endpoints`);
  if (stats.worktrees.length) {
    appendText(meta, ", worktrees ");
    meta.appendChild(codeElement(stats.worktrees.join(", ")));
  }
  title.appendChild(meta);

  summary.appendChild(title);
  summary.appendChild(renderBranchActions(branch));
  section.appendChild(summary);

  const tableWrap = createElement("div", { className: "table-wrap" });
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Worktree", "Service", "Endpoint", "Kind", "External", "Container Port", "Container", "Image"]) {
    headRow.appendChild(createElement("th", { text: label }));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const service of branch.services) {
    for (const row of renderServiceRows(service)) {
      tbody.appendChild(row);
    }
  }
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  section.appendChild(tableWrap);
  return section;
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

function renderProject(project) {
  const section = createElement("details", { className: "project-group" });
  section.open = true;
  section.setAttribute("data-project-group", "true");

  const summary = createElement("summary", { className: "project-summary" });
  const title = createElement("div", { className: "project-title" });
  title.appendChild(createElement("h2", { text: project.repo_name || project.repo_id }));
  const meta = createElement("div", { className: "project-meta" });
  appendText(meta, "repo_id ");
  meta.appendChild(codeElement(project.repo_id || ""));
  const stats = projectStats(project);
  appendText(meta, `, ${stats.branchTotal} branches, ${stats.serviceTotal} services, ${stats.endpointTotal} endpoints`);
  title.appendChild(meta);
  summary.appendChild(title);
  section.appendChild(summary);

  const body = createElement("div", { className: "project-body" });
  for (const branch of project.branches) {
    body.appendChild(renderBranch(branch));
  }
  section.appendChild(body);
  return section;
}

function renderCatalogTree(catalog) {
  const container = document.querySelector("[data-catalog-tree]");
  if (!container) return;
  clear(container);
  const projects = buildCatalogTree(catalog);
  if (!projects.length) {
    container.appendChild(createElement("div", {
      className: "empty",
      text: "No portmap-managed services are currently visible.",
    }));
    return;
  }
  for (const project of projects) {
    container.appendChild(renderProject(project));
  }
}

function renderMeta(catalog) {
  const meta = document.querySelector("[data-catalog-meta]");
  if (!meta) return;
  clear(meta);
  appendText(meta, `Generated at ${text(catalog.generated_at)}. `);
  appendText(meta, `HTTP proxy port: ${text(catalog.http_port)}. `);
  appendText(meta, `DNS domain: ${text(catalog.dns_domain)}. `);
  appendText(meta, `DNS server: ${text(catalog.dns_server)}. `);
  appendText(meta, "JSON: ");
  const link = document.createElement("a");
  link.href = "/registry.json";
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "/registry.json";
  meta.appendChild(link);
  appendText(meta, ".");
}

function renderCommands(catalog) {
  const setup = document.getElementById("split-dns-command");
  const unset = document.getElementById("split-dns-unset");
  if (setup) setup.textContent = splitDnsSetupCommand(catalog);
  if (unset) unset.textContent = splitDnsUnsetCommand(catalog);
}

function renderCatalog(catalog) {
  renderMeta(catalog);
  setupDnsProbe(catalog);
  renderCatalogTree(catalog);
  renderCommands(catalog);
}

async function loadCatalog() {
  try {
    const response = await fetch("/registry.json");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const catalog = await response.json();
    renderCatalog(catalog);
  } catch (error) {
    const meta = document.querySelector("[data-catalog-meta]");
    if (meta) meta.textContent = `Failed to load catalog: ${error}`;
    const tree = document.querySelector("[data-catalog-tree]");
    if (tree) {
      clear(tree);
      tree.appendChild(createElement("div", {
        className: "empty load-error",
        text: "Catalog data is unavailable.",
      }));
    }
  }
}

setupCopyButtons();
loadCatalog();
