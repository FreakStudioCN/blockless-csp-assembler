# KiCad Host MVP

This folder turns the React concept demo into a KiCad-hosted MVP.

The direction is:

```text
React AI console -> module plan JSON -> KiCad board synthesis -> KiCadRoutingTools autoroute -> KiCad DRC / 3D viewer
```

Do not keep expanding the React/Three.js app into a fake EDA. KiCad should host the real board editing experience:

- Users drag footprints in KiCad.
- KiCad shows ratsnest, DRC, copper layers, vias, zones, and 3D preview.
- The plugin creates/updates module placements, board outline, tracks, vias, silkscreen notes, and constraints.
- KiCadRoutingTools is the current Windows-native autorouting backend.
- Freerouting can be added later through DSN/SES when export/import is stable in the local KiCad toolchain.

## Requirements

- KiCad 10 installed on Windows.
- KiCad's bundled Python and `pcbnew` module available inside KiCad.
- KiCad CLI available at `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe`.
- KiCadRoutingTools v0.19.3 unpacked under `tools/KiCadRoutingTools-0.19.3`.

This repo can generate plan JSON without KiCad. The plugin itself must run inside KiCad PCB Editor.

## Files

```text
kicad-host-mvp/
  plans/
    long-range-env-node.json
    desktop-air-monitor.json
  plugin/
    ai_modular_backplane/
      __init__.py
      action_plugin.py
      board_builder.py
  scripts/
    generate_plan.py
    install_plugin.ps1
    check_kicad.ps1
```

## Quick Start

Generate/validate plans first:

```powershell
npm run kicad:plans
npm run kicad:validate
npm run kicad:build
npm run kicad:ai-route
```

1. Install KiCad.
2. Copy the plugin into KiCad's scripting plugin folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\kicad-host-mvp\scripts\install_plugin.ps1
```

3. Open KiCad PCB Editor.
4. Use `Tools -> External Plugins -> AI Modular Backplane`.
5. Pick one plan JSON from `kicad-host-mvp/plans`.
6. The plugin creates a real KiCad board with module footprints, pads, copper tracks, vias, labels, and keepouts.

The one-command autorouter flow writes:

```text
kicad-host-mvp/generated/external-router-output/*-krt-routed.kicad_pcb
kicad-host-mvp/generated/*-krt-render.png
kicad-host-mvp/reports/*-krt-route.log
kicad-host-mvp/reports/*-krt-drc.rpt
```

## Current Scope

This is a KiCad-hosted MVP, not a production PCB generator. It uses real KiCad board objects, KRT-generated copper, and color-coded WRL presentation models for clear video capture. The original STEP/STL model paths are still kept in the plans, but the default render favors visual distinction over strict CAD material fidelity. Current routed demo outputs have 0 unconnected items after KRT rescue, while KiCad DRC still reports violations that need cleanup before fabrication.

The AI boundary is intentional:

- AI/LLM: module selection, interface matching, placement constraints, keepouts, net priorities, routing parameters, DRC feedback interpretation.
- Autorouter: KiCadRoutingTools Rust A* router writes actual copper.
- Future research path: closed-loop RL/tool-using LLM routing against KiCad DRC, using projects such as PCBWorld and PCBench as references.
