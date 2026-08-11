# AI Modular PCB Backplane MVP

Windows-native demo for the AI modular PCB product concept.

[![Investor presentation](https://img.shields.io/badge/Investor_presentation-GitHub_Pages-43d69b?style=for-the-badge)](https://freakstudiocn.github.io/blockless-csp-assembler/presentation.html)
[![Local HTML](https://img.shields.io/badge/Local-presentation.html-67c7ff?style=for-the-badge)](presentation.html)

The app demonstrates:

- Natural-language prompt to module plan.
- Selection from a verified commercial module registry.
- Interface matching for Power, I2C, UART, and GND.
- Constraint-based placement and visual routing.
- Draggable PCB module placement with live rerouting.
- JLC EDA-like 3D board preview with board thickness, copper tracks, vias, pads, silkscreen labels, and real STL assets when available.
- Real public CAD asset harvesting from Adafruit CAD Parts, plus cached module review including source, extracted fields, confidence, and 3D strategy.
- BOM, ERC/DRC/DFM report, module asset manifest, and JLC/EasyEDA-oriented export ZIP.
- KiCad Host flow for real `.kicad_pcb` generation, KiCadRoutingTools autorouting, KiCad DRC, and KiCad 3D renders with STEP models.

## Run

```powershell
npm install
npm run harvest:assets
npm run dev -- --port 5173
```

Open:

```text
http://127.0.0.1:5173
```

Run the Windows-native KiCad autorouting demo:

```powershell
npm run kicad:ai-route
```

This generates plan JSON, builds unrouted KiCad boards, calls KiCadRoutingTools, runs KiCad DRC, repairs 3D model paths for nested output folders, and renders PNG screenshots.

## Supported Demo Cases

- Long-range battery-powered environment node: ESP32, LoRa, BME280, TP4056, battery connector, I2C expansion.
- Desktop air quality monitor: ESP32, SGP30, OLED, USB-C 5V input, I2C expansion.

## Current Scope

This is an MVP demo. The EasyEDA export is a structured demo subset and must be reviewed in JLC EDA before manufacturing.

The current real-CAD cache includes STL/STEP/JPG or STEP assets for ESP32 Feather HUZZAH, BME280, MicroLipo Charger, Radio FeatherWing, and OLED. Browser rendering uses STL geometry plus local material approximation. The KiCad Host demo now defaults to color-coded WRL presentation models for video clarity, while the real STEP/STL paths remain in each plan for later asset verification.

## KiCad Host Direction

The real EDA direction is in `kicad-host-mvp/`.

The React app should be treated as the AI console and concept UI. The real board editing experience should move into KiCad:

- KiCad PCB Editor hosts real footprint placement, routing, vias, DRC, zones, and 3D Viewer.
- The AI layer produces `plans/*.json`, placement constraints, keepouts, net priorities, and routing parameters.
- The KiCad Action Plugin reads a plan and creates a real KiCad board.
- KiCadRoutingTools performs the current open-source autorouting step. This is a deterministic Rust A* autorouter, not a trained AI router.
- Users can then drag modules, inspect ratsnest/DRC, edit routes, and open KiCad's real 3D preview.

Generate and validate KiCad-host plans:

```powershell
npm run kicad:plans
npm run kicad:validate
npm run kicad:ai-route
```

Check whether KiCad is installed:

```powershell
powershell -ExecutionPolicy Bypass -File .\kicad-host-mvp\scripts\check_kicad.ps1
```

Install the KiCad plugin after installing KiCad:

```powershell
powershell -ExecutionPolicy Bypass -File .\kicad-host-mvp\scripts\install_plugin.ps1
```

## Verification

```powershell
npm run typecheck
npm run build
npm run test:ui
```

`test:ui` opens the app in desktop and mobile Chromium, walks through the demo flow, captures the export page, and checks that the Three.js canvas has rendered nonblank pixels.

Current KiCad autorouter outputs are demo-ready but not production-clean: the two routed cases have 0 unconnected items, while DRC still reports rule violations that require layout/rule cleanup before fabrication.
