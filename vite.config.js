import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";

const catalogTarget = process.env.PORTMAP_CATALOG_TARGET || "http://127.0.0.1:80";
const mockCatalog = process.env.PORTMAP_CATALOG_MOCK === "1";

function projectSkillPlugin() {
  const path = "/skills/project-integration/SKILL.md";
  const source = new URL("./skills/project-integration/SKILL.md", import.meta.url);
  return {
    name: "portmap-project-skill",
    writeBundle() {
      const destination = new URL("./src/portmap/catalog_static/skills/project-integration/", import.meta.url);
      mkdirSync(destination, { recursive: true });
      copyFileSync(source, new URL("SKILL.md", destination));
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if ((request.url || "").split("?")[0] !== path) return next();
        if (request.method !== "GET" && request.method !== "HEAD") {
          response.statusCode = 405;
          response.end();
          return;
        }
        const project = readFileSync(new URL("./pyproject.toml", import.meta.url), "utf8");
        const version = project.match(/^version = "([^"]+)"/m)?.[1];
        if (!version) throw new Error("Portmap project version is missing");
        const body = readFileSync(source, "utf8").replaceAll("{{PORTMAP_VERSION}}", version);
        response.setHeader("content-type", "text/markdown; charset=utf-8");
        response.setHeader("content-length", Buffer.byteLength(body));
        response.setHeader("cache-control", "no-cache");
        response.setHeader("x-content-type-options", "nosniff");
        response.end(request.method === "HEAD" ? undefined : body);
      });
    },
  };
}

function mockCatalogPlugin() {
  // Mock state for the Settings updates panel. Fixture data comes from
  // frontend/mock/updates.json; batches started via POST /api/updates advance
  // one step per poll so the progress UI has something to render. Mock
  // responses are development fixtures only, never an acceptance result.
  const mockToken = "mock-local-management-token";
  const liveBatches = new Map();
  let batchCounter = 0;

  function readFixture() {
    return JSON.parse(readFileSync(new URL("./frontend/mock/updates.json", import.meta.url), "utf8"));
  }

  function sendJson(response, statusCode, payload) {
    response.statusCode = statusCode;
    response.setHeader("content-type", "application/json; charset=utf-8");
    response.end(JSON.stringify(payload));
  }

  async function readJsonBody(request) {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    try {
      return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
    } catch {
      return null;
    }
  }

  function fixtureBatchList() {
    const fixture = readFixture();
    const fromFixture = Array.isArray(fixture.batches) ? fixture.batches : [];
    const live = Array.from(liveBatches.values());
    return live.concat(fromFixture).slice(0, 20);
  }

  function advanceBatch(batch, machineById) {
    const order = ["queued", "preflight", "installing", "restarting", "verifying", "succeeded"];
    for (const target of batch.targets) {
      if (["succeeded", "failed"].includes(target.state)) continue;
      const machine = machineById.get(target.machine_id);
      if (target.state === "queued" && (!machine || machine.can_update !== true || machine.supported === false)) {
        target.state = "failed";
        target.error = textOf(machine?.reason) || "machine is not eligible for a managed update";
        break;
      }
      if (target.state === "queued") target.job_id = `job-${batch.id}-${target.machine_id}`;
      if (target.state === "installing" && target.machine_id !== "local") {
        // remotes skip the restart step in the mock
        target.state = "verifying";
        target.message = "verifying new version";
        break;
      }
      const next = order[order.indexOf(target.state) + 1] || "succeeded";
      target.state = next;
      if (next === "succeeded") {
        target.message = "updated";
        target.current_version = target.target_version;
      } else {
        target.message = next;
      }
      break;
    }
    const pending = batch.targets.filter((target) => !["succeeded", "failed"].includes(target.state));
    const failed = batch.targets.filter((target) => target.state === "failed");
    if (!pending.length) {
      batch.state = failed.length === 0 ? "succeeded" : failed.length === batch.targets.length ? "failed" : "partial";
    } else {
      batch.state = "running";
    }
    batch.updated_at = new Date().toISOString();
    return batch;
  }

  function textOf(value) {
    return value == null ? "" : String(value);
  }

  return {
    name: "portmap-mock-catalog",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        const path = (request.url || "").split("?")[0];
        if (path === "/api/version") {
          const local = readFixture().machines.find((machine) => machine.id === "local");
          sendJson(response, 200, { catalog_version: local?.current_version });
          return;
        }
        if (path === "/registry.json") {
          const body = readFileSync(new URL("./frontend/mock/registry.json", import.meta.url));
          response.statusCode = 200;
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.end(body);
          return;
        }
        if (path.startsWith("/actions/compose-")) {
          response.statusCode = 200;
          response.setHeader("content-type", "application/json; charset=utf-8");
          response.end(JSON.stringify({
            ok: true,
            message: "mock action accepted",
          }));
          return;
        }
        if (path === "/api/updates" && request.method === "GET") {
          const fixture = readFixture();
          sendJson(response, 200, {
            ...fixture,
            machines: (fixture.machines || []).map((machine) => {
              const live = Array.from(liveBatches.values())
                .flatMap((batch) => batch.targets)
                .find((target) => target.machine_id === machine.id && !["succeeded", "failed"].includes(target.state));
              return live ? { ...machine, active_job: { id: live.job_id, state: live.state, message: live.message } } : machine;
            }),
            batches: fixtureBatchList(),
          });
          return;
        }
        if (path === "/api/updates" && request.method === "POST") {
          const body = await readJsonBody(request);
          const fixture = readFixture();
          const machineById = new Map((fixture.machines || []).map((machine) => [machine.id, machine]));
          const requested = Array.isArray(body?.machines) ? body.machines.map(textOf).filter(Boolean) : [];
          const ids = Array.from(new Set(requested));
          if (!ids.length) {
            sendJson(response, 400, { error: "machines list is empty" });
            return;
          }
          const unknown = ids.filter((id) => !machineById.has(id));
          if (unknown.length) {
            sendJson(response, 400, { error: `unknown machine(s): ${unknown.join(", ")}` });
            return;
          }
          const activeBatch = Array.from(liveBatches.values())
            .find((live) => !["succeeded", "failed", "partial"].includes(live.state));
          if (activeBatch) {
            // One active batch per manager, mirroring the real controller.
            sendJson(response, 409, {
              error: `an update batch is already active (${activeBatch.id})`,
              job: activeBatch,
            });
            return;
          }
          const now = new Date().toISOString();
          // Remotes in the given order; local always last, matching the real controller.
          const ordered = ids.filter((id) => id !== "local").concat(ids.filter((id) => id === "local"));
          const batch = {
            id: `mock-batch-${++batchCounter}`,
            state: "queued",
            created_at: now,
            updated_at: now,
            targets: ordered.map((id) => {
              const machine = machineById.get(id);
              return {
                machine_id: id,
                label: machine.label,
                state: "queued",
                job_id: null,
                target_version: machine.latest_version || "latest",
                current_version: machine.current_version || null,
                message: null,
                error: null,
                log_tail: null,
              };
            }),
          };
          liveBatches.set(batch.id, batch);
          sendJson(response, 202, { job: batch });
          return;
        }
        if (path.startsWith("/api/updates/jobs/") && request.method === "GET") {
          const id = path.slice("/api/updates/jobs/".length);
          const live = liveBatches.get(id);
          if (live) {
            const fixture = readFixture();
            const machineById = new Map((fixture.machines || []).map((machine) => [machine.id, machine]));
            sendJson(response, 200, { job: advanceBatch(live, machineById) });
            return;
          }
          const fixtureBatch = fixtureBatchList().find((batch) => batch.id === id);
          if (fixtureBatch) {
            sendJson(response, 200, { job: fixtureBatch });
            return;
          }
          sendJson(response, 404, { error: `unknown batch: ${id}` });
          return;
        }
        if (path === "/api/updates/token" && request.method === "POST") {
          sendJson(response, 200, { token: mockToken });
          return;
        }
        if (path === "/api/updates/credentials" && request.method === "POST") {
          const body = await readJsonBody(request);
          const fixture = readFixture();
          const machine = (fixture.machines || []).find((item) => item.id === body?.machine);
          if (!machine) {
            sendJson(response, 400, { error: `unknown machine: ${textOf(body?.machine)}` });
            return;
          }
          if (textOf(body?.token) !== mockToken) {
            sendJson(response, 400, { error: "verification failed: target rejected token" });
            return;
          }
          sendJson(response, 200, { ok: true });
          return;
        }
        if (path.startsWith("/api/updates")) {
          sendJson(response, 405, { error: `unsupported ${request.method} ${path}` });
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  root: "frontend",
  publicDir: "public",
  plugins: [react(), projectSkillPlugin(), ...(mockCatalog ? [mockCatalogPlugin()] : [])],
  build: {
    outDir: "../src/portmap/catalog_static",
    cssMinify: false,
    // Keep __init__.py so catalog_static remains included as package data.
    emptyOutDir: false,
    rollupOptions: {
      output: {
        entryFileNames: "assets/catalog.js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: (assetInfo) => {
          const name = assetInfo.name || "";
          if (name.endsWith(".css")) return "assets/catalog.css";
          return "assets/[name][extname]";
        },
      },
    },
  },
  server: {
    port: 5174,
    proxy: mockCatalog ? {} : {
      "/registry.json": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/actions": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/healthz": {
        target: catalogTarget,
        changeOrigin: true,
      },
      "/readyz": {
        target: catalogTarget,
        changeOrigin: true,
      },
    },
  },
});
