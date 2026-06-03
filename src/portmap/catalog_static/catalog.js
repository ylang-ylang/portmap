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

function buildCatalogTree(catalog) {
  const directories = new Map();
  for (const service of catalog.services || []) {
    const worktree = text(service.worktree || "unknown");
    const repoId = text(service.repo_id || "unknown");
    const branchName = text(service.branch || "unknown");

    if (!directories.has(worktree)) {
      directories.set(worktree, { worktree, repos: new Map() });
    }
    const directory = directories.get(worktree);

    if (!directory.repos.has(repoId)) {
      directory.repos.set(repoId, {
        repo_id: repoId,
        repo_name: service.repo_name || repoId,
        branches: new Map(),
      });
    }
    const repo = directory.repos.get(repoId);

    if (!repo.branches.has(branchName)) {
      repo.branches.set(branchName, { branch: branchName, services: [] });
    }
    repo.branches.get(branchName).services.push(service);
  }

  return Array.from(directories.values())
    .sort((left, right) => left.worktree.localeCompare(right.worktree))
    .map((directory) => ({
      ...directory,
      repos: Array.from(directory.repos.values())
        .sort((left, right) => text(left.repo_name).localeCompare(text(right.repo_name)) || text(left.repo_id).localeCompare(text(right.repo_id)))
        .map((repo) => ({
          ...repo,
          branches: Array.from(repo.branches.values())
            .sort((left, right) => left.branch.localeCompare(right.branch))
            .map((branch) => ({
              ...branch,
              services: branch.services.sort((left, right) => text(left.compose_service).localeCompare(text(right.compose_service)) || text(left.container).localeCompare(text(right.container))),
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

function renderExternalCell(endpoint) {
  const cell = document.createElement("td");
  const external = endpointExternal(endpoint);
  if (!external) return cell;
  if (external.startsWith("http://") || external.startsWith("https://")) {
    const link = document.createElement("a");
    link.href = external;
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
  if (!window.confirm(`Run docker compose down for ${composeProject}?`)) return;
  const body = new URLSearchParams({ compose_project: composeProject });
  let result;
  try {
    const response = await fetch("/actions/compose-down", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
    result = await response.json();
    if (!response.ok || !result.ok) {
      showActionMessage(result.message || "compose down failed", true);
      return;
    }
  } catch (error) {
    showActionMessage(`compose down failed: ${error}`, true);
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
  const projects = Array.from(new Set(branch.services.map((service) => service.compose_project).filter(Boolean))).sort();
  if (!projects.length) return document.createElement("div");

  const wrapper = createElement("div", { className: "branch-actions" });
  for (const project of projects) {
    const action = createElement("div", { className: "down-form" });
    const button = createElement("button", {
      className: "down-button",
      text: "Down",
      title: `docker compose -p ${project} down`,
      type: "button",
    });
    button.addEventListener("click", () => composeDownProject(project));
    action.appendChild(codeElement(project));
    action.appendChild(button);
    wrapper.appendChild(action);
  }
  return wrapper;
}

function renderBranch(branch) {
  const section = createElement("section", { className: "branch-group" });
  const header = createElement("div", { className: "branch-header" });
  const title = document.createElement("div");
  appendText(title, "Branch: ");
  title.appendChild(codeElement(branch.branch));
  header.appendChild(title);
  header.appendChild(renderBranchActions(branch));
  section.appendChild(header);

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of ["Service", "Endpoint", "Kind", "External", "Container Port", "Container", "Image"]) {
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
  section.appendChild(table);
  return section;
}

function renderRepo(repo) {
  const section = createElement("section", { className: "repo-group" });
  const header = createElement("div", { className: "repo-header" });
  header.appendChild(createElement("h3", { text: repo.repo_name || repo.repo_id }));
  const meta = createElement("div", { className: "meta" });
  appendText(meta, "repo_id: ");
  meta.appendChild(codeElement(repo.repo_id || ""));
  header.appendChild(meta);
  section.appendChild(header);
  for (const branch of repo.branches) {
    section.appendChild(renderBranch(branch));
  }
  return section;
}

function renderDirectory(directory) {
  const section = createElement("section", { className: "directory-group" });
  const header = createElement("div", { className: "directory-header" });
  header.appendChild(createElement("h2", { text: "Work Tree" }));
  header.appendChild(codeElement(directory.worktree));
  section.appendChild(header);
  for (const repo of directory.repos) {
    section.appendChild(renderRepo(repo));
  }
  return section;
}

function renderCatalogTree(catalog) {
  const container = document.querySelector("[data-catalog-tree]");
  if (!container) return;
  clear(container);
  const directories = buildCatalogTree(catalog);
  if (!directories.length) {
    container.appendChild(createElement("div", {
      className: "empty",
      text: "No portmap-managed services are currently visible.",
    }));
    return;
  }
  for (const directory of directories) {
    container.appendChild(renderDirectory(directory));
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
