/* SPDX-License-Identifier: MIT */
"use strict";
(function (root) {
  const MAX_FILE_BYTES = 8 * 1024 * 1024;
  const MAX_IMAGE_PIXELS = 16 * 1024 * 1024;
  const MAX_IMAGE_EDGE = 16384;
  const FRAME_EDGE = 1280;
  const FRAME_INTERVAL = 125;

  // Inspect raster dimensions before the browser allocates a decoded image.
  // Rewrap accepted bytes with the verified MIME type: never load SVG/HTML/URLs.
  function imageMetadata(bytes) {
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    let width, height, type;
    if (bytes.length >= 33 && view.getUint32(0) === 0x89504e47 && view.getUint32(4) === 0x0d0a1a0a &&
        view.getUint32(8) === 13 && view.getUint32(12) === 0x49484452) {
      width = view.getUint32(16);
      height = view.getUint32(20);
      type = "image/png";
    } else if (bytes.length >= 4 && bytes[0] === 0xff && bytes[1] === 0xd8) {
      let offset = 2;
      while (offset + 3 < bytes.length) {
        if (bytes[offset++] !== 0xff) break;
        while (offset < bytes.length && bytes[offset] === 0xff) offset++;
        const marker = bytes[offset++];
        if (marker === 0xda || marker === 0xd9 || marker === undefined) break;
        if (marker === 0x01 || marker >= 0xd0 && marker <= 0xd7) continue;
        if (offset + 2 > bytes.length) break;
        const length = view.getUint16(offset);
        if (length < 2 || offset + length > bytes.length) break;
        if ([0xc0, 0xc1, 0xc2].includes(marker) && length >= 8) {
          height = view.getUint16(offset + 3);
          width = view.getUint16(offset + 5);
          type = "image/jpeg";
          break;
        }
        offset += length;
      }
    }
    if (!type || !width || !height || width > MAX_IMAGE_EDGE || height > MAX_IMAGE_EDGE ||
        width * height > MAX_IMAGE_PIXELS) {
      throw new Error("Choose a PNG or JPEG image of at most 16 megapixels and 16384 pixels per side.");
    }
    return { width, height, type };
  }

  function create({ document, navigator, decode, canUse, isBusy, conflict, onConfirm }) {
    const element = id => document.getElementById(id);
    const panel = element("qr-panel");
    const video = element("qr-video");
    const canvas = element("qr-canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const status = element("qr-status");
    const preview = element("qr-preview");
    const camera = element("qr-camera");
    const chooser = element("qr-image");
    const confirm = element("qr-confirm");
    let epoch = 0;
    let stream = null;
    let timer = null;
    let deadline = null;
    let imageLoad = null;
    let draft = null;
    let committing = false;
    let endedListeners = [];

    function message(text, phase) {
      status.textContent = text;
      status.dataset.state = phase;
    }
    function stop() {
      clearTimeout(timer);
      clearTimeout(deadline);
      timer = deadline = null;
      for (const [track, listener] of endedListeners) track.removeEventListener("ended", listener);
      endedListeners = [];
      if (stream) for (const track of stream.getTracks()) track.stop();
      stream = null;
      video.pause();
      video.srcObject = null;
      video.hidden = true;
      canvas.width = canvas.height = 1;
      if (imageLoad) {
        const pending = imageLoad;
        imageLoad = null;
        pending.image.onload = pending.image.onerror = null;
        pending.image.src = "";
        root.URL.revokeObjectURL(pending.url);
        pending.resolve(null);
      }
    }
    function resetOperation() {
      epoch++;
      stop();
      draft = null;
      preview.replaceChildren();
      preview.hidden = true;
      confirm.hidden = true;
      chooser.value = "";
      return epoch;
    }
    function current(token) {
      return epoch === token && !panel.hidden && !document.hidden && canUse();
    }
    function controls() {
      const allowed = canUse() && !isBusy() && !committing && !document.hidden;
      element("qr-open").disabled = !allowed;
      camera.disabled = !allowed || ["requesting", "reading", "scanning"].includes(status.dataset.state);
      chooser.disabled = !allowed || status.dataset.state === "reading";
      confirm.disabled = !allowed || !draft || Boolean(draft && conflict(draft));
      element("qr-cancel").disabled = committing;
    }
    function fail(text) {
      resetOperation();
      message(text, "error");
      controls();
    }
    function close() {
      resetOperation();
      panel.hidden = true;
      message("Scanner closed. No unconfirmed settings were changed.", "closed");
      controls();
    }
    function refresh() {
      if (!panel.hidden && !canUse() && !committing) {
        fail("Scanning stopped because networking capability or local configuration is unavailable. Your editor is unchanged.");
      } else if (draft && !committing) {
        const reason = conflict(draft);
        message(reason || "Verify these public settings before confirming. Nothing has been saved.", reason ? "error" : "preview");
      }
      controls();
    }
    function showPreview(text, token) {
      if (!current(token)) return;
      let decoded;
      try { decoded = decode(text); }
      catch (_) {
        fail("This is not a valid supported Portmap connection QR. No settings were changed. Scan a trusted node's QR or choose another image.");
        return;
      }
      resetOperation();
      draft = decoded;
      const details = document.createElement("dl");
      const fields = [["Name", draft.name], ["Portmap domain", draft.domain],
        ["Mode", draft.mode === "ssh" ? "SSH tunnel" : "Direct TCP"],
        [draft.mode === "ssh" ? "SSH server host" : "Destination host", draft.host],
        ["HTTP port", draft.httpPort], ["HTTPS port", draft.httpsPort ?? "Not configured"]];
      if (draft.mode === "ssh") fields.push(["SSH port", draft.ssh.port],
        ["SSH username", draft.ssh.username || "Not supplied — enter your username after import"],
        ["Target host inside SSH", draft.ssh.targetHost], ["Trusted host SHA256 (hex)", draft.ssh.hostKeySha256Hex]);
      for (const [label, value] of fields) {
        const term = document.createElement("dt");
        const detail = document.createElement("dd");
        term.textContent = label;
        detail.textContent = String(value);
        details.append(term, detail);
      }
      const warning = document.createElement("p");
      warning.className = "notice";
      warning.textContent = draft.mode === "ssh"
        ? "Confirming replaces your entire current editor draft, including any unsaved edits. All previous credentials and saved connection identity are cleared. Only public SSH settings are imported; enter your own username and authentication, then use Save connection. Imported addresses will not follow name changes."
        : "Confirming adds a new enabled connection with a fresh identity. Your existing unsaved editor draft is preserved. Saving does not open a connection or verify reachability. Direct HTTP is not encrypted; use a trusted encrypted network.";
      preview.append(details, warning);
      preview.hidden = false;
      confirm.textContent = draft.mode === "ssh" ? "Replace editor with public SSH settings" : "Add direct connection";
      confirm.hidden = false;
      refresh();
      confirm.focus();
    }
    function scan(source, width, height) {
      const ratio = Math.min(1, FRAME_EDGE / Math.max(width, height));
      canvas.width = Math.max(1, Math.round(width * ratio));
      canvas.height = Math.max(1, Math.round(height * ratio));
      context.drawImage(source, 0, 0, canvas.width, canvas.height);
      const frame = context.getImageData(0, 0, canvas.width, canvas.height);
      return root.jsQR(frame.data, frame.width, frame.height, { inversionAttempts: "attemptBoth" });
    }
    function frame(token) {
      if (!current(token)) { if (token === epoch) close(); return; }
      try {
        if (video.readyState >= 2 && video.videoWidth && video.videoHeight) {
          const result = scan(video, video.videoWidth, video.videoHeight);
          if (result) { showPreview(result.data, token); return; }
        }
        timer = setTimeout(() => frame(token), FRAME_INTERVAL);
      } catch (_) {
        fail("Camera frame could not be read. The camera has stopped; choose a PNG or JPEG QR image instead.");
      }
    }
    camera.addEventListener("click", async () => {
      if (!canUse() || isBusy() || committing || panel.hidden || document.hidden || camera.disabled) return;
      const token = resetOperation();
      message("Waiting for camera permission. Allow camera access, or cancel and choose a QR image.", "requesting");
      controls();
      if (!context || typeof root.jsQR !== "function" || !navigator.mediaDevices?.getUserMedia) {
        fail("Camera capture is unavailable in this Firefox context. Open connection settings in a tab and allow camera access, or choose a PNG or JPEG QR image.");
        return;
      }
      deadline = setTimeout(() => {
        if (token === epoch) fail("Camera permission timed out. The camera will not start from a late response. Try again or choose a QR image.");
      }, 30000);
      try {
        const captured = await navigator.mediaDevices.getUserMedia({ audio: false,
          video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } } });
        if (!current(token)) {
          for (const track of captured.getTracks()) track.stop();
          return;
        }
        clearTimeout(deadline);
        stream = captured;
        for (const track of stream.getTracks()) {
          const ended = () => { if (token === epoch) fail("The camera stopped. Try again or choose a QR image."); };
          track.addEventListener("ended", ended);
          endedListeners.push([track, ended]);
        }
        video.srcObject = stream;
        video.hidden = false;
        deadline = setTimeout(() => {
          if (token === epoch) fail("Camera preview did not start. The camera stopped; try again or choose a QR image.");
        }, 15000);
        await video.play();
        if (!current(token)) { if (token === epoch) close(); return; }
        clearTimeout(deadline);
        message("Camera is live. Center the complete trusted Portmap QR in the preview. Camera stops after a code is read or after two minutes.", "scanning");
        deadline = setTimeout(() => {
          if (token === epoch) fail("Scanning timed out and the camera stopped. Try again or choose a QR image.");
        }, 120000);
        controls();
        frame(token);
      } catch (_) {
        if (token === epoch) fail("Camera access failed or was denied. Allow camera access in Firefox and your device's privacy settings, check that a camera is available, or choose a PNG or JPEG QR image.");
      }
    });
    chooser.addEventListener("change", async () => {
      const file = chooser.files[0];
      if (!file || !canUse() || isBusy() || committing || panel.hidden || document.hidden) return;
      const token = resetOperation();
      message("Reading QR image locally. Nothing is uploaded or saved.", "reading");
      controls();
      deadline = setTimeout(() => {
        if (token === epoch) fail("Image decoding timed out. Choose another PNG or JPEG QR image.");
      }, 15000);
      try {
        if (file.size > MAX_FILE_BYTES) throw new Error("size");
        const bytes = new Uint8Array(await file.arrayBuffer());
        if (!current(token)) return;
        const metadata = imageMetadata(bytes);
        const image = await new Promise((resolve, reject) => {
          const picture = new root.Image();
          const url = root.URL.createObjectURL(new Blob([bytes], { type: metadata.type }));
          imageLoad = { image: picture, url, resolve };
          picture.onload = () => {
            imageLoad = null;
            picture.onload = picture.onerror = null;
            root.URL.revokeObjectURL(url);
            resolve(picture);
          };
          picture.onerror = () => {
            imageLoad = null;
            picture.onload = picture.onerror = null;
            root.URL.revokeObjectURL(url);
            reject(new Error("image"));
          };
          picture.src = url;
        });
        if (!current(token) || !image) return;
        if (image.naturalWidth !== metadata.width || image.naturalHeight !== metadata.height) throw new Error("dimensions");
        const result = scan(image, image.naturalWidth, image.naturalHeight);
        if (!result) { fail("No readable QR was found. Choose a clear PNG or JPEG showing the complete QR, including its border."); return; }
        showPreview(result.data, token);
      } catch (_) {
        if (token === epoch) fail("Could not read the QR image. Choose a PNG or JPEG of at most 8 MiB, 16 megapixels and 16384 pixels per side. SVG and other file types are not accepted.");
      }
    });
    element("qr-open").addEventListener("click", () => {
      if (!canUse() || isBusy() || committing || document.hidden) return;
      resetOperation();
      panel.hidden = false;
      message("Start the camera or choose a PNG or JPEG QR image. No settings change until you review and confirm.", "idle");
      controls();
      camera.focus();
    });
    element("qr-cancel").addEventListener("click", () => {
      if (!committing) { close(); element("qr-open").focus(); }
    });
    confirm.addEventListener("click", async () => {
      if (!draft || !canUse() || isBusy() || committing || conflict(draft) || document.hidden) return;
      const selected = draft;
      const token = epoch;
      committing = true;
      message("Checking current local configuration before applying your confirmed settings…", "saving");
      controls();
      try {
        const applied = await onConfirm(selected, () => current(token));
        if (token !== epoch) return;
        if (applied) close();
        else {
          message("The confirmed settings could not be applied. Check the page's error message. No editor replacement was made.", "error");
        }
      } catch (_) {
        if (token === epoch) message("The confirmed settings could not be applied. Check capability and local configuration before trying again.", "error");
      } finally {
        committing = false;
        controls();
      }
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        resetOperation();
        if (!panel.hidden) message("Capture stopped while this page was hidden. Start the camera again or choose a QR image.", "idle");
      }
      controls();
    });
    root.addEventListener("pagehide", close);
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && !panel.hidden && !committing) { close(); element("qr-open").focus(); }
    });
    video.addEventListener("error", () => { if (stream) fail("Camera preview failed. Choose a PNG or JPEG QR image instead."); });
    controls();
    return Object.freeze({ refresh, close });
  }
  const api = Object.freeze({ create });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortmapScanner = api;
})(globalThis);
