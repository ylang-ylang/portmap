/* SPDX-License-Identifier: MIT */
"use strict";

const portmap = PortmapController.createController(browser);

const optionsSender = sender => sender.id === browser.runtime.id &&
  sender.url?.split(/[?#]/)[0] === browser.runtime.getURL("options.html");
const failureResponse = failure => ({ ok: false, error: failure.message,
  possiblyInstalled: failure.possiblyInstalled === true });

browser.runtime.onMessage.addListener((message, sender) => {
  if (!optionsSender(sender) || !message || message.channel !== "portmap-connections") return undefined;
  return portmap.command(message).then(state => ({ ok: true, state }), failureResponse);
});

// A live options-page lease is required for operations which might install a
// public key or save a newly authenticated managed node. Closing/navigating the
// page aborts its work instead of letting an abandoned draft save in background.
browser.runtime.onConnect.addListener(port => {
  if (port.name !== "portmap-managed-ssh" || !optionsSender(port.sender)) { port.disconnect(); return; }
  const owner = portmap.openEditor();
  let connected = true;
  port.onDisconnect.addListener(() => { connected = false; portmap.cancelOwner(owner, true); });
  port.onMessage.addListener(message => {
    if (!Number.isSafeInteger(message?.requestId) || message.requestId < 1 ||
        message.channel !== "portmap-connections") return;
    portmap.command(message, owner).then(state => ({ ok: true, state }), failureResponse).then(result => {
      if (connected) port.postMessage({ requestId: message.requestId, ...result });
    });
  });
});

browser.browserAction.onClicked.addListener(() => browser.runtime.openOptionsPage());
// Do not initialize or overwrite storage on install/update. The same validated
// storage load runs for first install, browser restart, reload and updates.
