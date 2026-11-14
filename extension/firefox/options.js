/* SPDX-License-Identifier: MIT */
"use strict";

const byId = id => document.getElementById(id);
const fields = { id: "connection-id", name: "name", domain: "domain", proxyHost: "proxy-host",
  proxyPort: "proxy-port", username: "username", password: "password", enabled: "enabled" };
let state = { phase: "loading", connections: [] };
let busy = false;

function showError(message = "") {
  byId("error").textContent = message;
  byId("error").hidden = !message;
}

function clearEditor() {
  byId("connection-form").reset();
  byId("connection-id").value = "";
  byId("editor-heading").textContent = "Add connection";
  byId("cancel-edit").hidden = true;
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

function render() {
  const status = byId("status");
  status.dataset.state = busy ? "loading" : state.phase;
  status.textContent = busy ? "Updating the local connection list…"
    : state.phase === "ready" ? "Ready. Unmapped .portmap requests are blocked."
    : state.phase === "error" ? state.error : "Loading local connections. .portmap requests are blocked…";
  byId("save").disabled = busy || state.phase !== "ready";
  byId("empty").hidden = state.connections.length !== 0 || state.phase !== "ready";
  byId("connections").replaceChildren();
  for (const entry of state.connections) {
    const card = document.createElement("article");
    card.className = "connection";
    card.dataset.connectionId = entry.id;
    card.dataset.domain = entry.domain;
    const heading = document.createElement("h3");
    heading.textContent = entry.name;
    const detail = document.createElement("p");
    detail.className = "connection-detail";
    detail.textContent = `${entry.domain} · ${entry.enabled ? "Enabled" : "Disabled"}`;
    const endpoint = document.createElement("p");
    endpoint.className = "hint";
    endpoint.textContent = `SOCKS5 proxy ${entry.proxyHost}:${entry.proxyPort} · remote DNS`;
    const actions = document.createElement("div");
    actions.className = "actions";
    const open = button("Open node", "open", entry, () => run({ action: "open", id: entry.id, scheme: "http" }));
    const secureOpen = button("Open HTTPS", "open-https", entry, () => run({ action: "open", id: entry.id, scheme: "https" }));
    open.disabled = secureOpen.disabled = !entry.enabled || busy || state.phase !== "ready";
    const edit = button("Edit", "edit", entry, () => {
      showError();
      for (const [key, id] of Object.entries(fields)) {
        if (key === "enabled") byId(id).checked = entry[key];
        else byId(id).value = entry[key];
      }
      byId("editor-heading").textContent = "Edit connection";
      byId("cancel-edit").hidden = false;
      byId("name").focus();
      byId("connection-form").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    const toggle = button(entry.enabled ? "Disable" : "Enable", "toggle", entry,
      () => run({ action: "toggle", id: entry.id }));
    const remove = button("Delete", "delete", entry, async () => {
      if (confirm(`Delete connection “${entry.name}”? Its .portmap addresses will be blocked.`)) {
        if (await run({ action: "delete", id: entry.id }) && byId("connection-id").value === entry.id) clearEditor();
      }
    });
    for (const control of [edit, toggle, remove]) control.disabled = busy || state.phase !== "ready";
    actions.append(open, secureOpen, edit, toggle, remove);
    card.append(heading, detail, endpoint, actions);
    byId("connections").append(card);
  }
}

async function run(message) {
  if (busy) return false;
  busy = true;
  showError();
  render();
  try {
    const result = await browser.runtime.sendMessage({ channel: "portmap-connections", ...message });
    if (!result?.ok) throw new Error(result?.error || "The extension background is unavailable. Reload this page.");
    state = result.state;
    return true;
  } catch (failure) {
    showError(failure.message);
    return false;
  } finally {
    busy = false;
    render();
  }
}

byId("connection-form").addEventListener("submit", async event => {
  event.preventDefault();
  const connection = {};
  for (const [key, id] of Object.entries(fields)) {
    connection[key] = key === "enabled" ? byId(id).checked : byId(id).value;
  }
  connection.proxyPort = Number(connection.proxyPort);
  if (await run({ action: "save", connection })) clearEditor();
});
byId("cancel-edit").addEventListener("click", clearEditor);
byId("reset").addEventListener("click", async () => {
  if (confirm("Delete all local connections and saved passwords? All .portmap addresses will be blocked.")) {
    if (await run({ action: "reset" })) clearEditor();
  }
});
browser.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.config) {
    // A second options tab can edit storage. Refresh only after the current
    // command has completed; focus refresh below also reconciles missed events.
    if (!busy) run({ action: "get" });
  }
});
window.addEventListener("focus", () => { if (!busy) run({ action: "get" }); });
run({ action: "get" });
