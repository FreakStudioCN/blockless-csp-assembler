import { spawn } from "node:child_process";
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

function sendJson(response: ServerResponse, statusCode: number, body: unknown) {
  response.statusCode = statusCode;
  response.setHeader("Content-Type", "application/json; charset=utf-8");
  response.end(JSON.stringify(body));
}

function kicadRouteApiPlugin(): Plugin {
  let state: RouteState = {
    status: "idle",
    code: null,
    output: "",
    startedAt: null,
    finishedAt: null,
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
          state.output += chunk.toString("utf8");
          if (state.output.length > 16_000) state.output = state.output.slice(-16_000);
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
    },
  };
}

export default defineConfig({
  plugins: [react(), kicadRouteApiPlugin()],
});
