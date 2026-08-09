import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

type RouteState = {
  status: "idle" | "running" | "ok" | "error";
  code: number | null;
  output: string;
  startedAt: string | null;
  finishedAt: string | null;
};
type CamState = RouteState & {
  caseId: string | null;
  manifest: unknown | null;
};

function sendJson(response: ServerResponse, statusCode: number, body: unknown) {
  response.statusCode = statusCode;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.end(JSON.stringify(body));
}

function readJsonFile(filePath: string): unknown {
  return JSON.parse(fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, ""));
}

function kicadRouteApiPlugin(): Plugin {
  let state: RouteState = {
    status: "idle",
    code: null,
    output: "",
    startedAt: null,
    finishedAt: null,
  };
  let camState: CamState = {
    status: "idle",
    code: null,
    output: "",
    startedAt: null,
    finishedAt: null,
    caseId: null,
    manifest: null,
  };

  const appendOutput = (currentOutput: string, chunk: Buffer) => {
    const nextOutput = currentOutput + chunk.toString("utf8");
    return nextOutput.length > 16_000 ? nextOutput.slice(-16_000) : nextOutput;
  };

  return {
    name: "kicad-route-api",
    configureServer(server) {
      const runKiCadRoute = () => {
        const root = server.config.root;
        const script = path.join(root, "kicad-host-mvp", "scripts", "ai_route_with_kicad_routing_tools.ps1");
        const child = spawn(
          "powershell.exe",
          ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script],
          {
            cwd: root,
            windowsHide: true,
            env: process.env,
          },
        );

        state = {
          status: "running",
          code: null,
          output: "",
          startedAt: new Date().toISOString(),
          finishedAt: null,
        };

        const append = (chunk: Buffer) => {
          state.output = appendOutput(state.output, chunk);
        };

        child.stdout.on("data", append);
        child.stderr.on("data", append);
        child.on("error", (error) => {
          state = { ...state, status: "error", code: null, output: error.message, finishedAt: new Date().toISOString() };
        });
        child.on("close", (code) => {
          state = {
            ...state,
            status: code === 0 ? "ok" : "error",
            code,
            finishedAt: new Date().toISOString(),
          };
        });
      };

      const runCamExport = (caseId: string) => {
        const root = server.config.root;
        const script = path.join(root, "kicad-host-mvp", "scripts", "export_cnc_toolpaths.ps1");
        const child = spawn(
          "powershell.exe",
          ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Case", caseId],
          {
            cwd: root,
            windowsHide: true,
            env: process.env,
          },
        );

        camState = {
          status: "running",
          code: null,
          output: "",
          startedAt: new Date().toISOString(),
          finishedAt: null,
          caseId,
          manifest: null,
        };

        const append = (chunk: Buffer) => {
          camState.output = appendOutput(camState.output, chunk);
        };

        child.stdout.on("data", append);
        child.stderr.on("data", append);
        child.on("error", (error) => {
          camState = { ...camState, status: "error", code: null, output: error.message, finishedAt: new Date().toISOString() };
        });
        child.on("close", (code) => {
          const manifestPath = path.join(root, "public", "assets", "cam-real", caseId, "manifest.json");
          let manifest: unknown | null = null;
          if (code === 0 && fs.existsSync(manifestPath)) {
            try {
              manifest = readJsonFile(manifestPath);
            } catch (error) {
              const message = error instanceof Error ? error.message : String(error);
              camState.output = appendOutput(camState.output, Buffer.from(`\nFailed to read CAM manifest: ${message}`));
            }
          }
          camState = {
            ...camState,
            status: code === 0 ? "ok" : "error",
            code,
            manifest,
            finishedAt: new Date().toISOString(),
          };
        });
      };

      server.middlewares.use("/api/kicad/route", async (request: IncomingMessage, response: ServerResponse) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { ok: false, message: "Use POST to start KiCad/KRT routing." });
          return;
        }

        if (state.status === "running") {
          sendJson(response, 202, { ok: true, message: "KiCad/KRT routing is already running.", state });
          return;
        }

        runKiCadRoute();
        sendJson(response, 202, { ok: true, message: "KiCad/KRT routing started.", state });
      });

      server.middlewares.use("/api/kicad/status", async (_request: IncomingMessage, response: ServerResponse) => {
        sendJson(response, 200, { ok: state.status !== "error", state });
      });

      server.middlewares.use("/api/cam/generate", async (request: IncomingMessage, response: ServerResponse) => {
        if (request.method !== "POST") {
          sendJson(response, 405, { ok: false, message: "Use POST to start real CAM export." });
          return;
        }

        const requestUrl = new URL(request.url ?? "/api/cam/generate", "http://localhost");
        const caseId = requestUrl.searchParams.get("case") ?? "long-range-env-node";
        if (!["long-range-env-node", "desktop-air-monitor"].includes(caseId)) {
          sendJson(response, 400, { ok: false, message: `Unsupported CAM case: ${caseId}` });
          return;
        }

        if (camState.status === "running") {
          sendJson(response, 202, { ok: true, message: "CAM export is already running.", state: camState });
          return;
        }

        runCamExport(caseId);
        sendJson(response, 202, { ok: true, message: "Real CAM export started.", state: camState });
      });

      server.middlewares.use("/api/cam/status", async (request: IncomingMessage, response: ServerResponse) => {
        const requestUrl = new URL(request.url ?? "/api/cam/status", "http://localhost");
        const caseId = requestUrl.searchParams.get("case");
        if (caseId && camState.status !== "running") {
          const manifestPath = path.join(server.config.root, "public", "assets", "cam-real", caseId, "manifest.json");
          if (fs.existsSync(manifestPath)) {
            try {
              camState = {
                ...camState,
                status: "ok",
                code: 0,
                caseId,
                manifest: readJsonFile(manifestPath),
              };
            } catch {
              // Keep the previous state; the generate endpoint will surface real errors.
            }
          }
        }
        sendJson(response, 200, { ok: camState.status !== "error", state: camState });
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), kicadRouteApiPlugin()],
});
