/* SPDX-License-Identifier: MIT */
"use strict";

const portmap = PortmapController.createController(browser);

browser.runtime.onMessage.addListener((message, sender) => {
  if (sender.id !== browser.runtime.id || !sender.url?.startsWith(browser.runtime.getURL(""))) {
    return undefined;
  }
  if (!message || message.channel !== "portmap-connections") return undefined;
  return portmap.command(message).then(state => ({ ok: true, state }), failure => ({
    ok: false,
    // Import failures must never serialize code/credential fragments from an
    // unexpected decoder, storage or platform exception into the options page.
    error: message.action === "preview-import" || message.action === "import"
      ? "Connection code was not imported. Check the code and preview again; overlapping domains cannot be imported."
      : failure.message
  }));
});

browser.browserAction.onClicked.addListener(() => browser.runtime.openOptionsPage());
// Do not initialize or overwrite storage on install/update. The same validated
// storage load runs for first install, browser restart, reload and updates.
