/* SPDX-License-Identifier: MIT */
"use strict";

const byId = id => document.getElementById(id);
let state = { phase: "loading", capability: { available: false, reason: "" }, connections: [], nodes: {} };
let busy = false;
let editingConnection = null;
let pendingKeyReads = 0;
const automaticAddresses = new Set(["domain", "host"]);
let qrScanner = null;
let draftEpoch = 0;
let inspectedHost = null;
let confirmedHost = null;
let setupResult = null;
let managedBusy = false;
let managedPort = null;
let managedSequence = 0;
const managedRequests = new Map();
const managedActions = new Set(["managed-public-key", "inspect-host", "confirm-host", "managed-save", "managed-install"]);

function managedRequest(message) {
  if (!managedPort) {
    const port = browser.runtime.connect({ name: "portmap-managed-ssh" });
    managedPort = port;
    port.onMessage.addListener(response => {
      const pending = managedRequests.get(response.requestId);
      if (!pending) return;
      managedRequests.delete(response.requestId);
      pending.resolve(response);
    });
    port.onDisconnect.addListener(() => {
      if (managedPort === port) managedPort = null;
      for (const pending of managedRequests.values()) pending.reject(new Error("The SSH setup editor disconnected. No abandoned draft will be saved. Reopen this page."));
      managedRequests.clear();
      inspectedHost = confirmedHost = null;
      byId("ssh-onetime-password").value = "";
      updateManagedFields();
    });
  }
  const requestId = ++managedSequence;
  return new Promise((resolve, reject) => {
    managedRequests.set(requestId, { resolve, reject });
    try { managedPort.postMessage({ channel: "portmap-connections", ...message, requestId }); }
    catch (failure) { managedRequests.delete(requestId); reject(failure); }
  });
}

function invalidateSetup() {
  draftEpoch++;
  inspectedHost = confirmedHost = null;
  byId("ssh-onetime-password").value = "";
  byId("host-ack").checked = false;
  if (managedPort) void managedRequest({ action: "cancel-setup" }).catch(() => {});
  updateManagedFields();
}

function currentEndpoint() {
  return PortmapCore.normalizeSshEndpoint({ host: byId("host").value, port: Number(byId("ssh-port").value) });
}

function hasManagedTrust() {
  try {
    const endpoint = currentEndpoint();
    const pin = PortmapCore.normalizeFingerprint(byId("ssh-host-key").value);
    const trusted = confirmedHost || (editingConnection?.mode === "ssh"
      ? { host: editingConnection.host, port: editingConnection.ssh.port, pin: editingConnection.ssh.hostKeySha256Hex } : null);
    return trusted?.host === endpoint.host && trusted.port === endpoint.port && trusted.pin === pin;
  } catch (_) { return false; }
}

function updateManagedFields() {
  const managed = byId("mode").value === "ssh" && byId("ssh-auth-type").value === "managed";
  const trusted = hasManagedTrust();
  byId("managed-fields").hidden = !managed;
  byId("manual-managed-key").hidden = !managed;
  byId("host-confirmation").hidden = !managed || !inspectedHost;
  byId("confirm-host").disabled = busy || !byId("host-ack").checked;
  byId("install-managed-key").disabled = busy || !trusted;
  byId("save").textContent = managed ? "Test and save" : "Save connection";
  byId("save").disabled = busy || pendingKeyReads !== 0 || state.phase !== "ready" ||
    !state.capability?.available || (managed && !trusted);
  byId("setup-status").textContent = managedBusy ? "Connecting securely…"
    : trusted ? "Server fingerprint pinned. Ready to test the browser key."
    : "Check and confirm the server fingerprint before signing in.";
}

function fillAddressesFromName() {
  const host = byId("name").value.trim().toLowerCase().replace(/\.$/, "").replace(/\.portmap$/, "");
  let domain = "";
  try {
    if (host) {
      PortmapCore.normalizeHost(host);
      domain = PortmapCore.normalizeDomain(`${host}.portmap`);
    }
  } catch (_) {
    // Display labels must not become invented network addresses.
  }
  if (automaticAddresses.has("domain")) byId("domain").value = domain;
  if (automaticAddresses.has("host")) byId("host").value = domain ? host : "";
}

function showError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

function keepsStoredCredentials() {
  return byId("mode").value === "ssh" && editingConnection?.mode === "ssh" &&
    editingConnection.ssh.auth.type !== "managed" &&
    editingConnection.ssh.auth.type === byId("ssh-auth-type").value &&
    !byId("replace-credentials").checked;
}

function updateModeFields() {
  const ssh = byId("mode").value === "ssh";
  const key = byId("ssh-auth-type").value === "key";
  const managed = byId("ssh-auth-type").value === "managed";
  const canKeep = ssh && !managed && editingConnection?.mode === "ssh" &&
    editingConnection.ssh.auth.type === byId("ssh-auth-type").value;
  const keep = keepsStoredCredentials();
  byId("host-label").textContent = ssh ? "SSH server host" : "Destination host";
  byId("ssh-fields").hidden = !ssh;
  byId("ssh-fields").disabled = !ssh;
  byId("replace-credentials-row").hidden = !canKeep;
  byId("credential-fields").hidden = keep || managed;
  byId("credential-fields").disabled = keep || managed;
  byId("password-fields").hidden = key || managed;
  byId("key-fields").hidden = !key;
  byId("ssh-password").disabled = key || managed;
  for (const id of ["ssh-private-key", "ssh-public-key", "ssh-passphrase",
    "ssh-private-key-file", "ssh-public-key-file"]) byId(id).disabled = !key;
  byId("ssh-password").required = ssh && !key && !managed && !keep;
  byId("ssh-private-key").required = byId("ssh-public-key").required = ssh && key && !keep;
  byId("credential-help").textContent = keep
    ? "Stored credentials will be kept."
    : canKeep
      ? "Enter the complete replacement password or key pair."
      : "";
  byId("ssh-host-key").required = ssh && !managed;
  updateManagedFields();
}

function clearEditor() {
  invalidateSetup();
  byId("connection-form").reset();
  byId("advanced-settings").open = false;
  byId("ssh-advanced-settings").open = false;
  byId("setup-password-section").open = false;
  byId("managed-public-key-output").hidden = true;
  byId("connection-id").value = "";
  editingConnection = null;
  byId("editor-heading").textContent = "Add connection";
  byId("cancel-edit").hidden = true;
  automaticAddresses.add("domain");
  automaticAddresses.add("host");
  updateModeFields();
}

function editConnection(entry) {
  invalidateSetup();
  showError();
  byId("connection-form").reset();
  byId("advanced-settings").open = false;
  editingConnection = entry;
  // Saved labels never drive saved endpoints, even if their values match.
  automaticAddresses.clear();
  for (const key of ["id", "name", "domain", "mode", "host"]) {
    byId(key === "id" ? "connection-id" : key).value = entry[key];
  }
  byId("http-port").value = entry.httpPort;
  byId("https-port").value = entry.httpsPort ?? "";
  if (entry.mode === "ssh") {
    byId("ssh-username").value = entry.ssh.username;
    byId("ssh-port").value = entry.ssh.port;
    byId("ssh-target-host").value = entry.ssh.targetHost;
    byId("ssh-host-key").value = entry.ssh.hostKeySha256Hex;
    byId("ssh-auth-type").value = entry.ssh.auth.type;
    byId("ssh-advanced-settings").open = entry.ssh.auth.type !== "managed";
  }
  // Do not copy stored passwords, private keys or passphrases into the DOM.
  updateModeFields();
  byId("editor-heading").textContent = "Edit connection";
  byId("cancel-edit").hidden = false;
  byId("name").focus();
  byId("connection-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

function button(text, action, entry, handler) {
  const control = document.createElement("button");
  control.type = "button";
  control.textContent = text;
  control.dataset.action = action;
  control.setAttribute("aria-label", `${text}: ${entry.name}`);
  control.addEventListener("click", handler);
  return control;
}

function displayHost(host) {
  return host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
}

function render() {
  const ready = state.phase === "ready" && state.capability?.available === true;
  const unsupported = state.phase === "unsupported";
  const status = byId("status");
  status.dataset.state = busy ? "loading" : state.phase;
  status.textContent = busy ? "Updating…"
    : ready ? "Ready"
    : unsupported ? "Networking capability unavailable. Direct and SSH connections are disabled."
    : state.phase === "error" ? "Local configuration could not be loaded or applied. .portmap traffic is blocked."
    : state.phase === "unavailable" ? "Cannot contact the extension background. Reload the extension and this page; your unsaved draft is kept."
    : "Checking networking capability and loading local connections. .portmap traffic is blocked…";
  byId("capability-guidance").hidden = !unsupported;
  byId("capability-reason").textContent = unsupported
    ? state.capability?.reason || "The required experimental networking capability self-check did not succeed." : "";
  byId("config-guidance").hidden = state.phase !== "error";
  byId("config-reason").textContent = state.phase === "error" ? state.error || "Local connections are unavailable." : "";
  byId("connection-fields").disabled = busy || !ready;
  byId("save").disabled = busy || pendingKeyReads !== 0 || !ready;
  byId("cancel-edit").disabled = busy && !managedBusy;
  byId("cancel-edit").hidden = !editingConnection && !byId("connection-id").value && !managedBusy &&
    byId("editor-heading").textContent === "Add connection";
  byId("reset").disabled = busy || state.phase === "loading";
  byId("empty").hidden = state.connections.length !== 0 || !ready;
  byId("connections").replaceChildren();
  for (const entry of state.connections) {
    const card = document.createElement("article");
    card.className = "connection";
    card.dataset.connectionId = entry.id;
    card.dataset.domain = entry.domain;
    card.dataset.mode = entry.mode;
    const heading = document.createElement("h3");
    heading.textContent = entry.name;
    const detail = document.createElement("p");
    detail.className = "connection-detail";
    detail.textContent = `${entry.domain} · ${entry.enabled ? "Enabled" : "Disabled"}`;
    const endpoint = document.createElement("p");
    endpoint.className = "hint";
    const ports = `HTTP ${entry.httpPort} · ${entry.httpsPort === null ? "HTTPS not configured" : `HTTPS ${entry.httpsPort}`}`;
    endpoint.textContent = entry.mode === "ssh"
      ? `SSH ${displayHost(entry.host)}:${entry.ssh.port} → ${displayHost(entry.ssh.targetHost)} · ${ports}`
      : `Direct ${displayHost(entry.host)} · ${ports}`;
    const node = state.nodes?.[entry.id];
    const nodeStatus = document.createElement("p");
    nodeStatus.className = "connection-status hint";
    nodeStatus.dataset.state = !entry.enabled ? "disabled" : !ready ? "blocked" : node?.phase || "idle";
    nodeStatus.textContent = !entry.enabled ? "Disabled. This namespace is blocked."
      : !ready ? "Blocked until networking capability and configuration are ready."
      : node?.phase === "error" ? "Connection failed. Check node reachability, ports and, for SSH, credentials and the verified host fingerprint. Other nodes remain available."
      : node?.phase === "connecting" ? "Connecting to this node…"
      : node?.phase === "ready" ? "Transport ready. Remote services are reached on demand."
      : "Ready to connect on demand. Saving does not verify remote reachability.";
    const actions = document.createElement("div");
    actions.className = "actions";
    const open = button("Open node", "open", entry, () => run({ action: "open", id: entry.id, scheme: "http" }));
    const secureOpen = button("Open HTTPS", "open-https", entry, () => run({ action: "open", id: entry.id, scheme: "https" }));
    open.disabled = !entry.enabled || busy || !ready;
    secureOpen.disabled = open.disabled || entry.httpsPort === null;
    if (entry.httpsPort === null) secureOpen.title = "Configure an HTTPS port to open HTTPS.";
    const edit = button("Edit", "edit", entry, () => editConnection(entry));
    const toggle = button(entry.enabled ? "Disable" : "Enable", "toggle", entry,
      () => run({ action: "toggle", id: entry.id }));
    const remove = button("Delete", "delete", entry, async () => {
      if (confirm(`Delete connection “${entry.name}”? Its .portmap addresses will be blocked.`)) {
        if (await run({ action: "delete", id: entry.id }) && byId("connection-id").value === entry.id) clearEditor();
      }
    });
    for (const control of [edit, toggle, remove]) control.disabled = busy || !ready;
    actions.append(open, secureOpen, edit, toggle, remove);
    card.append(heading, detail, endpoint, nodeStatus, actions);
    byId("connections").append(card);
  }
  updateManagedFields();
  qrScanner?.refresh();
}

async function run(message) {
  if (busy) return false;
  busy = true;
  managedBusy = managedActions.has(message.action);
  const epoch = draftEpoch;
  showError();
  render();
  try {
    const result = managedBusy
      ? await managedRequest(message)
      : await browser.runtime.sendMessage({ channel: "portmap-connections", ...message });
    if (!result?.ok) throw new Error(result?.error || "The extension background is unavailable. Reload this page.");
    const { setup, ...snapshot } = result.state;
    state = snapshot;
    if (managedBusy && epoch !== draftEpoch) return false;
    setupResult = setup || null;
    return true;
  } catch (failure) {
    showError(failure.message);
    if (message.action === "get") state = { ...state, phase: "unavailable" };
    return false;
  } finally {
    busy = false;
    if (managedBusy) byId("ssh-onetime-password").value = "";
    managedBusy = false;
    render();
  }
}

function editorConnection() {
  const connection = {
    id: byId("connection-id").value,
    name: byId("name").value,
    domain: byId("domain").value,
    enabled: editingConnection?.enabled ?? true,
    mode: byId("mode").value,
    host: byId("host").value,
    httpPort: Number(byId("http-port").value),
    httpsPort: byId("https-port").value === "" ? null : Number(byId("https-port").value)
  };
  if (connection.mode === "ssh") {
    const auth = byId("ssh-auth-type").value === "managed" ? { type: "managed" }
      : keepsStoredCredentials() ? { ...editingConnection.ssh.auth }
        : byId("ssh-auth-type").value === "password"
          ? { type: "password", password: byId("ssh-password").value }
          : { type: "key", publicKey: byId("ssh-public-key").value, privateKey: byId("ssh-private-key").value,
            passphrase: byId("ssh-passphrase").value };
    connection.ssh = {
      port: Number(byId("ssh-port").value),
      username: byId("ssh-username").value,
      targetHost: byId("ssh-target-host").value,
      hostKeySha256Hex: byId("ssh-host-key").value,
      auth
    };
  }
  // Use exactly the controller's validation, without inventing a saved ID.
  const normalized = PortmapCore.normalizeConnection({ ...connection, id: connection.id || "draft" });
  return { ...normalized, id: connection.id };
}

function qrConflict(draft) {
  if (state.connections.length >= 256) return "The local connection limit has been reached. Delete an existing connection before importing.";
  if (state.connections.some(entry => PortmapCore.matchesDomain(draft.domain, entry.domain) ||
      PortmapCore.matchesDomain(entry.domain, draft.domain))) {
    return "This domain overlaps an existing connection, including disabled connections. QR import never overwrites records. Cancel and use the existing connection's Edit action instead.";
  }
  return "";
}

async function confirmQrConnection(draft, isCurrent) {
  if (busy || state.phase !== "ready" || !state.capability?.available) return false;
  // Refresh the actual controller snapshot, not a browser-version assumption or
  // the configuration that happened to be present when scanning began.
  if (!await run({ action: "get" }) || !isCurrent()) return false;
  if (state.phase !== "ready" || !state.capability?.available) return false;
  const conflict = qrConflict(draft);
  if (conflict) { showError(conflict); return false; }
  if (draft.mode === "direct") {
    // Empty ID asks the existing controller to allocate a fresh identity.
    // In particular, never clear or reuse an unrelated unsaved editor.
    return run({ action: "save", connection: { ...draft, id: "", enabled: true } });
  }
  clearEditor();
  // Reset identity, all secrets and file inputs before changing destinations.
  // Any old async key-file read no longer matches the reset file selection.
  for (const id of ["ssh-password", "ssh-onetime-password", "ssh-private-key", "ssh-public-key", "ssh-passphrase",
    "ssh-private-key-file", "ssh-public-key-file"]) byId(id).value = "";
  editingConnection = null;
  byId("connection-id").value = "";
  byId("replace-credentials").checked = false;
  byId("ssh-auth-type").value = "managed";
  automaticAddresses.clear();
  for (const key of ["name", "domain", "mode", "host"]) byId(key).value = draft[key];
  byId("http-port").value = draft.httpPort;
  byId("https-port").value = draft.httpsPort ?? "";
  byId("ssh-username").value = draft.ssh.username;
  byId("ssh-port").value = draft.ssh.port;
  byId("ssh-target-host").value = draft.ssh.targetHost;
  byId("ssh-host-key").value = draft.ssh.hostKeySha256Hex;
  byId("editor-heading").textContent = "Complete scanned SSH connection";
  byId("cancel-edit").hidden = false;
  updateModeFields();
  byId(draft.ssh.username ? "inspect-host" : "ssh-username").focus();
  byId("connection-form").scrollIntoView({ behavior: "smooth", block: "start" });
  return true;
}

byId("connection-form").addEventListener("input", event => {
  if (!["ssh-onetime-password", "host-ack", "managed-public-key"].includes(event.target.id)) invalidateSetup();
});
for (const id of ["mode", "ssh-auth-type", "replace-credentials"]) {
  byId(id).addEventListener("change", invalidateSetup);
}
byId("host-ack").addEventListener("change", updateManagedFields);
byId("inspect-host").addEventListener("click", async () => {
  if (busy) return;
  invalidateSetup();
  try {
    const endpoint = currentEndpoint();
    if (!await run({ action: "inspect-host", endpoint, expectedPin: byId("ssh-host-key").value })) return;
    inspectedHost = setupResult;
    byId("detected-endpoint").textContent = `Server: ${displayHost(endpoint.host)}:${endpoint.port}`;
    byId("detected-fingerprint").textContent = inspectedHost.hostKeySha256Hex;
    updateManagedFields();
  } catch (failure) { showError(failure.message); }
});
byId("confirm-host").addEventListener("click", async () => {
  if (busy || !inspectedHost || !byId("host-ack").checked) return;
  const checked = inspectedHost;
  if (!await run({ action: "confirm-host", challenge: checked.challenge,
    endpoint: checked.endpoint, hostKeySha256Hex: checked.hostKeySha256Hex })) return;
  confirmedHost = { ...checked.endpoint, pin: checked.hostKeySha256Hex };
  byId("ssh-host-key").value = checked.hostKeySha256Hex;
  inspectedHost = null;
  updateManagedFields();
});
byId("copy-managed-public-key").addEventListener("click", async () => {
  if (!await run({ action: "managed-public-key" })) return;
  const publicKey = setupResult.publicKey;
  byId("managed-public-key").value = publicKey;
  byId("managed-public-key-output").hidden = false;
  try {
    await navigator.clipboard.writeText(publicKey);
    byId("setup-status").textContent = "Browser public key copied.";
  } catch (_) {
    byId("managed-public-key").select();
    byId("setup-status").textContent = "Select and copy the public key shown below.";
  }
});
byId("install-managed-key").addEventListener("click", async () => {
  if (busy || !hasManagedTrust() || !byId("connection-form").reportValidity()) return;
  try {
    const connection = editorConnection();
    const password = byId("ssh-onetime-password").value;
    if (!password) throw new Error("Enter the one-time SSH password, or install the public key manually.");
    if (await run({ action: "managed-install", connection, password })) clearEditor();
  } catch (failure) {
    byId("ssh-onetime-password").value = "";
    showError(failure.message);
  }
});
window.addEventListener("pagehide", () => {
  byId("ssh-onetime-password").value = "";
  managedPort?.disconnect();
});
byId("name").addEventListener("input", fillAddressesFromName);
for (const id of ["domain", "host"]) {
  byId(id).addEventListener("input", () => automaticAddresses.delete(id));
}
for (const id of ["mode", "ssh-auth-type", "replace-credentials"]) {
  byId(id).addEventListener("change", updateModeFields);
}
for (const kind of ["private", "public"]) {
  byId(`ssh-${kind}-key-file`).addEventListener("change", async event => {
    const file = event.target.files[0];
    if (!file) return;
    const editor = editingConnection;
    pendingKeyReads++;
    byId("save").disabled = true;
    try {
      if (file.size > 65536) throw new Error("Key files must be at most 64 KiB.");
      const text = await file.text();
      // A cancelled edit must not receive a late file read from an old editor.
      if (editingConnection === editor && event.target.files[0] === file) byId(`ssh-${kind}-key`).value = text;
    } catch (_) {
      showError("Could not load the key file. Select a readable text key file of at most 64 KiB, or paste its contents.");
    } finally {
      pendingKeyReads--;
      updateManagedFields();
    }
  });
}
// Reveal invalid advanced controls before the browser tries to focus them.
for (const id of ["advanced-settings", "ssh-advanced-settings"]) {
  byId(id).addEventListener("invalid", () => { byId(id).open = true; }, true);
}
byId("connection-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || pendingKeyReads !== 0 || state.phase !== "ready" || !state.capability?.available) return;
  showError();
  try {
    const connection = editorConnection();
    const action = connection.mode === "ssh" && connection.ssh.auth.type === "managed" ? "managed-save" : "save";
    if (await run({ action, connection })) clearEditor();
  } catch (failure) {
    showError(failure.message);
  }
});
byId("cancel-edit").addEventListener("click", () => { clearEditor(); showError(); });
byId("reset").addEventListener("click", async () => {
  if (confirm("Delete all local connections and SSH credentials, including this profile's browser key? Remove its public key from servers separately. All .portmap addresses will be blocked. This cannot be undone.")) {
    if (await run({ action: "reset" })) clearEditor();
  }
});
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.config && !busy) run({ action: "get" });
});
window.addEventListener("focus", () => { if (!busy) run({ action: "get" }); });
const manifest = browser.runtime.getManifest();
byId("installed-version").textContent = manifest.version;
byId("check-update").addEventListener("click", async () => {
  const button = byId("check-update");
  if (button.disabled) return;
  const status = byId("update-status");
  const download = byId("download-update");
  button.disabled = true;
  download.hidden = true;
  download.removeAttribute("href");
  const feed = manifest.browser_specific_settings?.gecko?.update_url;
  if (!feed) {
    status.dataset.state = "unavailable";
    status.textContent = "No compatible experimental update feed is configured. Follow this build's distribution instructions; the legacy signed extension cannot supply these networking APIs.";
    button.disabled = false;
    return;
  }
  status.dataset.state = "checking";
  status.textContent = "Checking the official compatible update feed…";
  try {
    const response = await fetch(feed, {
      cache: "no-store", credentials: "omit", referrerPolicy: "no-referrer",
      signal: AbortSignal.timeout(30000)
    });
    if (!response.ok) throw new Error("Update feed request failed.");
    const update = PortmapCore.findReleaseUpdate(await response.json(), browser.runtime.id, manifest.version);
    if (update) {
      download.href = update.url;
      download.textContent = `Download ${update.version} (.xpi)`;
      download.hidden = false;
      status.dataset.state = "available";
      status.textContent = `Version ${update.version} is available. Downloading does not install the update.`;
    } else {
      status.dataset.state = "current";
      status.textContent = `No newer compatible release found. Installed version: ${manifest.version}.`;
    }
  } catch (_) {
    status.dataset.state = "error";
    status.textContent = "Could not check for compatible updates. Check your connection and try again.";
  } finally {
    button.disabled = false;
  }
});

qrScanner = PortmapScanner.create({
  document, navigator, decode: PortmapConnectionCode.decode,
  canUse: () => state.phase === "ready" && state.capability?.available === true,
  isBusy: () => busy,
  conflict: qrConflict,
  onConfirm: confirmQrConnection
});

updateModeFields();
run({ action: "get" });
