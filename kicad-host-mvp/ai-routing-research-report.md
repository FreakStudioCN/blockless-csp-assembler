# AI PCB Routing MVP Research Report

Date: 2026-08-05

## MVP Decision

Use KiCad as the real EDA host and KiCadRoutingTools as the first autorouting backend.

For a one-day demo, the credible architecture is:

```text
prompt -> AI module/constraint plan -> KiCad .kicad_pcb synthesis -> KiCadRoutingTools autoroute -> KiCad DRC -> AI feedback loop -> KiCad 3D render
```

Do not claim the LLM directly draws copper. In this MVP, AI plans constraints and retries; the router writes copper.

## Open-Source / Research Findings

### KiCadRoutingTools

Source: https://github.com/drandyhaas/KiCadRoutingTools

Best current fit. It works directly on KiCad PCB files, has a command-line interface, supports KiCad 9/10, and uses a Rust-accelerated A* core with multi-layer routing, automatic vias, net ordering, rip-up/reroute, power net widths, DRC/connectivity helpers, and AI workflow hooks.

MVP use:

- Input: generated `.kicad_pcb` with modules, pads, nets, keepouts, and no copper.
- Output: routed `.kicad_pcb`.
- Verification: `kicad-cli pcb drc`.
- Visual proof: `kicad-cli pcb render` with color-coded WRL presentation models. Real STEP/STL assets are retained in the module plan for later CAD verification.

Status in this repo:

- `long-range-env-node`: KRT routed, 0 unconnected, 16 DRC violations.
- `desktop-air-monitor`: KRT routed, 0 unconnected, 12 DRC violations.

### PCBWorld

Source: https://arxiv.org/html/2607.05915v1

Best future AI-routing reference. PCBWorld frames routing as an engine-grounded environment over KiCad, where agents interact with KiCad-native operations and DRC feedback. It supports RL policies and tool-using LLM agents. This validates the same product direction: the AI should operate inside an EDA feedback loop instead of generating a PCB file in one shot.

MVP implication:

- Use KiCad DRC as the source of truth.
- Let AI decide constraints/actions.
- Keep deterministic tools responsible for geometry until an RL/agent router is trained and benchmarked.

### PCBench

Source: https://github.com/PCBench/PCBench

Useful dataset/reference for ML-based PCB routing. It includes 164 PCB routing boards, augmentation scripts, PCB-RDL JSON, and an RL environment. It is relevant for a later trained router, not necessary for the one-day demo.

### DeepPCB / InstaDeep

Source: https://instadeep.com/2019/11/instadeep-launches-first-pure-ai-powered-printed-circuit-board-pcb-router/

Evidence that RL-based PCB routing is a plausible research direction, but not an immediately reusable open-source Windows-native backend for this MVP.

### Freerouting

Source: https://github.com/freerouting/freerouting

Mature autorouter with Specctra/Electra DSN input and SES output. It remains a strong fallback once DSN/SES export-import is reliable in the local KiCad version. It was not selected for this one-day flow because the installed KiCad 10 CLI here does not expose a DSN export subcommand.

### tscircuit Autorouter

Source: https://github.com/tscircuit/tscircuit-autorouter

Useful web/TypeScript ecosystem reference. It is less direct for this KiCad-host MVP because the demo needs real KiCad routing, DRC, and 3D render output.

## Demo Scope

The demo should show two cases only:

- Long-range environment node: ESP32 Feather, Radio FeatherWing, BME280, MicroLipo, I2C expansion.
- Desktop air monitor: ESP32 Feather, OLED, BME280/air sensor module, I2C expansion.

Recording flow:

1. Show React AI console and draggable 3D/browser preview.
2. Run `npm run kicad:ai-route`.
3. Open generated routed `.kicad_pcb` in KiCad.
4. Show actual copper, vias, DRC report, and KiCad 3D Viewer / generated PNG.
5. Be explicit: demo is routed and connected, but DRC is not clean yet.

## Next Algorithm Work

Short term:

- Let AI adjust placement when KRT reports rescue-heavy routes or DRC violations.
- Parse KRT `JSON_SUMMARY` and KiCad DRC into a concise feedback object.
- Add case-specific keepout/routing profiles.

Medium term:

- Add an AI loop: route -> DRC -> diagnose -> modify placement/rules -> reroute.
- Compare KRT and Freerouting once DSN/SES is available.
- Use PCBench/PCBWorld style board tasks to benchmark route quality.

Long term:

- Train or fine-tune an agent policy over KiCad/PCBWorld-style actions.
- Keep final signoff in KiCad/JLC DRC/DFM, not in the model.
