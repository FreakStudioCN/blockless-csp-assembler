# Placement

Perturbative placement optimization for KiCad PCB files. Starts from an
existing (hand- or AI-made) placement and improves it for routability —
it does not place boards from scratch. Background research and experiment
results: [docs/placement-optimization.md](../docs/placement-optimization.md).

Two command-line tools sit on top of this module:

## place_optimize.py — greedy quench

Small nudges (capped by `--max-displacement`), 90° rotations (on each part's
own angular lattice, so a part placed at 45° rotates to 135/225/315 rather
than snapping to the axes), and same-footprint swaps (capped by
`--swap-max-displacement`, default: the same cap) that reduce airwire length
+ crossings + a whitespace (halo) penalty scaled by pin count + a soft
board-edge margin. Locked footprints never move.

```bash
# Conservative polish (recommended starting point)
python place_optimize.py input.kicad_pcb optimized.kicad_pcb \
    --max-displacement 3 --length-weight 0.3 --crossing-penalty 30 \
    --halo-coef 0.15 --halo-weight 2 --edge-halo 2 \
    --ignore-nets GND "+3.3V" \
    --lock "J*" "P30*" "*PORT*"
```

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `--max-displacement` | 10 mm | Max distance a part may move from its seed position; applies to nudges and swaps alike (3 mm recommended; large values can destroy the placement's macro structure) |
| `--swap-max-displacement` | = max-displacement | Displacement cap for swap moves; must be ≤ `--max-displacement` |
| `--ignore-nets` | – | Net patterns excluded from airwire scoring (plane-routed power nets) |
| `--lock` | – | Reference patterns to pin in place (connectors, mounting-critical parts) |
| `--halo-coef` | 0.25 | Extra whitespace per √(pin count); keep modest (~0.15) on dense boards |
| `--no-rotate` / `--no-swap` | off | Disable rotation / swap moves. `--no-rotate` freezes every part's angle: nudges keep the current rotation, and same-footprint swaps are restricted to pairs that already share one, since a swap exchanges full poses and a mixed-angle pair would rotate both parts |

## place_route_loop.py — router-in-the-loop repair

Routes the board with the real router, reads the failure diagnostics
(failed nets + the blocker nets named in the frontier analysis), and
micro-quenches only the small parts that could help those routes succeed,
weighting the failed nets so both their airwire length and any crossing they
take part in cost more. Re-routes and keeps the new placement only if
(failures, router effort) actually improves; otherwise reverts and widens the
search.

The tally counts the router's end-of-run reconciliation pass, so a net that
pass recovers is not treated as a failure in the next round. A rejected round
widens the **nudge** search 1.5×; the swap cap does not move. It stays at
`--swap-max-displacement` (default: the initial `--max-displacement`), so
widening the search can never turn into a long-range swap.
`--swap-max-displacement`, `--no-rotate` and `--no-swap` work here exactly as
they do in `place_optimize.py`, and `--verbose` surfaces each accepted quench
move plus the per-pass `swap-capped=N` count.

```bash
python place_route_loop.py input.kicad_pcb repaired.kicad_pcb \
    --route-args '--nets "/*" "Net-*" --track-width 0.2 --clearance 0.2 ...' \
    --ignore-nets GND "+3.3V" --lock "J*" --swap-max-displacement 2
```

On the kit-dev-coldfire demo board this repaired the hand placement from
3 failed nets to 0 with 4.8× less router effort, moving only
resistors/caps/jumpers.

## place_fanout_clearance.py — decoupling-cap clearance repair (issue #130)

Run **after** `bga_fanout.py`. Nudges decoupling caps near a BGA so their
pads clear every foreign-net fanout via, every foreign track on the cap's
own copper side, and every foreign component pad (#130/#278/#275 — a graze
already present at the seed placement is a violation to fix, not a baseline
to preserve), and pulls each pad toward the nearest **same-net** ball — so
a power/GND via dropped at that ball later also lands on the cap pad (one
shared via connects ball + cap + plane). Caps move as little as possible
(90° rotations allowed), never overlap each other or a locked part, and a cap
that can't clear within the (auto-grown) displacement budget is reported
unresolved for a manual nudge.

It reads each via's actual size from the board, so the only setting that
matters is `--clearance`, which must match the fanout / DRC floor:

```bash
# after: bga_fanout.py board.kicad_pcb -o fanned.kicad_pcb --clearance 0.1 ...
python place_fanout_clearance.py fanned.kicad_pcb capclean.kicad_pcb --clearance 0.1
```

| Option | Default | Description |
|--------|---------|-------------|
| `--clearance` | 0.25 mm | DRC clearance; **set to the fanout/DRC floor** |
| `--cap-prefix` | `C,R` | Comma-separated reference prefix(es) for movable passives near a BGA (caps **and** resistors by default). Only 2-copper-pad parts move, so RN-style arrays are auto-excluded; paste-only apertures are ignored when counting pads. |
| `--capture-radius` | 2 mm | Max distance over which a same-net ball attracts a pad |
| `--max-displacement` / `--max-displacement-cap` | 2 / 3 mm | Initial and grown move budget per cap |
| `--default-via-size` | 0.3 mm | Fallback only, for vias with no readable size |
| `--lock` | – | Extra reference patterns to pin in place |

On ulx3s U1 (22×22, 0.8 mm) this took the fanned board from 4 PAD-VIA to
fully DRC-clean, tidying 19 caps toward same-net balls (all ≤1.9 mm). In the
GUI, the **"Optimize decoupling cap placement"** checkbox on the BGA fanout
tab runs the same engine automatically right after fanout (off by default).
The advanced knobs above (capture radius, near margin, search step, max
displacement / cap / growth, max passes, cap-ref prefix, allow-rotation) are
exposed in that tab's **"Cap Placement (advanced)"** box; `--clearance`,
`--grid-step`, and the via size come from the Basic tab.

### animate_fanout_clearance.py — visualize the repair as a GIF

`animate_fanout_clearance.py` (repo root) runs the **same** repair engine and
records every accepted cap move via the engine's optional `on_move` hook, then
renders an animated GIF of the caps gliding from their seed placement to their
final, via-clearing positions. The view is framed to the BGA ball field (not
the whole board); fanout vias appear as net-coloured disks with their keep-out
ring, cap pads are coloured by net, and a faint ghost rectangle marks each
cap's seed position. It accepts all of `place_fanout_clearance.py`'s repair
options plus `--size`, `--fps`, and `--sub-frames` (motion smoothness).

![Decoupling caps gliding off foreign-net fanout vias on the glasgow revC BGA](../docs/fanout-cap-placement.gif)

```bash
# after: bga_fanout.py board.kicad_pcb -o fanned.kicad_pcb --escape-method underpad \
#            --via-size 0.3 --via-drill 0.2 --track-width 0.1 --clearance 0.1
python animate_fanout_clearance.py fanned.kicad_pcb capmove.gif --clearance 0.1
```

This is a read-only visualization tool: the `on_move` hook defaults to `None`,
so `place_fanout_clearance.py`, the GUI, and the engine itself behave exactly
as before when it is unused. Requires `pygame` (render) and `Pillow` (GIF
encode) — both already used elsewhere in this repo; no matplotlib/ffmpeg.

## Testing

The placement tests are standalone scripts (no pytest needed), all in
`tests/run_all.py`'s `--fast` lane:

```bash
python3 tests/test_quench_swap_cap.py        # swap displacement cap (#430)
python3 tests/test_quench_neighbor_lists.py  # pruned-scan bit-exactness (#430)
python3 tests/test_458_loop_steering.py      # loop caps, tally, summary merge
python3 tests/test_458_quench_net_weights.py # weighted crossings
python3 tests/test_458_quench_rotations.py   # rotation lattice, --no-rotate
python3 tests/test_fanout_clearance.py       # cap clearance repair (#130)
python3 tests/test_456_courtyard_parser.py   # courtyard shapes + silk bleed (#456)
python3 tests/test_456_side_and_outline.py   # board side, real outline, graders (#456)
python3 tests/test_459_groups.py             # block sources + parsing (#459)
python3 tests/test_459_group_moves.py        # rigid block translation (#459)
```

Quench output is reproducible across processes — the same board and arguments
give the same placement, with no `PYTHONHASHSEED` pinning (#457). It used to
depend on the hash seed: `net_refs` was a set of reference strings, its iteration
order became the MST's point order, and Prim's tie-break is first-index-wins, so
equidistant pads (uniform-pitch GND arrays, decaps on a grid, symmetric
connectors) built a different tree per process. `interf_u_unrouted` scored 447 /
457 / 450 crossings under three seeds before a single move was made. `net_refs`
now holds sorted lists, and `tests/test_457_determinism.py` pins it.

When comparing two placements, `hpwl` is the metric to reach for first: it reads
only each net's pad-position extremes, so unlike the MST length and the crossing
count it is invariant to airwire order. If HPWL agrees and crossings do not, the
two runs differ in tie-breaks rather than in placement quality.

## Ratsnest metrics, and the pre-route screen (#504)

The quench cost function computes airwire length and crossings on every pass, and
`hpwl()` is pure pad geometry. Those numbers used to be printed and discarded.
They are now exported:

- `quench(..., metrics_out=d)` fills `d` with `{'before', 'after', 'legality'}` —
  an out-param rather than a changed return, since the return is consumed
  positionally by both CLIs and four test files.
- `place_optimize.py` emits them as a `JSON_SUMMARY:` line, so a chain or grader
  can gate on what a run achieved instead of scraping stdout.
- `place_route_loop.py` records `ratsnest_crossings` / `ratsnest_hpwl` /
  `ratsnest_length` in each round's metrics dict, report-only beside the
  `pad_pairs_*` keys. `better()` is deliberately untouched — reworking the
  comparator is #458.

**Which numbers compare.** `crossings` (a raw count by contract) and `hpwl` are
unweighted, so they are comparable across runs. `length` and `total` are scaled
by `net_weights`, so they only compare between the `before` and `after` of the
*same* call — which is why the screen thresholds on the first two.

**`--ratsnest-screen N`** (percent, `0` = disabled, the default) skips the routing
run when a candidate's crossings or HPWL regress by more than N% against the board
it came from. Routing is the honest judge but an expensive one, often minutes per
round; a candidate whose ratsnest clearly got worse is very unlikely to route
better. The baseline is free — quench is handed the current best board, so its own
`before` *is* that board's ratsnest. Every decision is logged with its numbers, so
it is auditable whether the screen ever skipped a placement that would have won.

## Placement blocks (`groups.py`, #459)

The per-part nudge cannot express "these parts need to travel together": an IC
and its decoupling caps fight each other one at a time, because moving either
alone worsens the pair. `--group-by` gives a block a **rigid translate** move —
the whole body shifts by one offset.

Sources, in precedence order, first match wins per part (a part is in at most one
block):

| source | what it is | corpus evidence |
|---|---|---|
| `kicad` | KiCad `(group ...)` blocks | **0 of 27** in-repo boards have one. Exact when present; verified against a synthetic fixture. |
| `sheet` | schematic sheet path | **12 of 22** boards with `(path ...)` have >1 sheet. `ulx3s`: 10 blocks sized 83/34/23/20/20/12. The workhorse. |
| `netprefix` | net-name prefix (`AUDIO_*`) | The **weakest** source. Raw prefixes are dominated by KiCad's auto-generated `Net-(U1-Pad)` names — one bogus bucket of up to 92 refs spanning 75mm — so those and power nets are excluded and a 20mm coherence gate applied. What survives is small but real: ulx3s 9 blocks, tigard 0. |
| `decap` | 2-pad caps tethered to their IC | Strong. A cap sits 0.0–2.6mm (median) from the nearest IC and **shares a net with it 93–100%** of the time; the net check rejects the rest (13 of ulx3s' 70). |

`--group-by auto` means `kicad,sheet`. Default is `none`: grouping is opt-in, and
with it off the group phase never runs and output is byte-identical to the
ungrouped engine — which is what lets every existing bit-identity test stand.

**What actually moves.** Small blocks move; large ones do not. On
`splitflap_driver` with `--group-by decap`, 6 blocks of 2–3 parts translate and
the objective improves (total 4781.6 → 4689.7, crossings 211 → 208, HPWL
2468.0 → 2436.3 against the ungrouped run). Sheet blocks of 16–83 parts moved on
none of the boards tried — at a few mm of displacement a rigid shift of that many
parts rarely finds a legal, improving offset. That is the expected shape: #459's
80mm block *relocation* is separate, unimplemented work.

**Why the cap is per-member.** Every member must land within `--max-displacement`
of **its own seed**. That single rule is what keeps three existing guarantees
true: `build_neighbor_lists`' pruning stays exact rather than lossy, the outline
gate's per-ref cached reach cannot be outrun, and the no-stranding invariant
holds unchanged. Lifting it is precisely what makes the 80mm case hard.

## Module layout

| File | Purpose |
|------|---------|
| `quench.py` | The optimizer: cost terms, move generation, greedy quench |
| `fanout_clearance.py` | Post-fanout decoupling-cap clearance repair (#130) |
| `groups.py` | Placement blocks: which parts move as one rigid body (#459) |
| `legality.py` | Hard constraints shared by both engines: board side, real Edge.Cuts containment, and the OO/OoB graders (#456) |
| `parser.py` | Courtyard boundary and locked-footprint extraction |
| `writer.py` | Writes new positions/rotations (rotates pad angles with the footprint, as KiCad stores pad angle = footprint + pad rotation) |
| `utility.py` | Shared utilities (bbox from pads, grid snapping) |

## Legality model (`legality.py`, #456)

What counts as a *legal* placement is decided in one place, so the optimizer and
any grader cannot disagree:

- **Board side.** A part occupies its own side with its full courtyard, and the
  opposite side only with the bounding box of its **drilled** pads. So a
  back-side decoupling cap may sit under a front-side BGA (they overlap in XY,
  not in copper), but not inside a front-side connector's pin field. Cross-side
  pairs also pay no halo penalty — spreading them apart buys no routing room.
  On a single-sided board every test reduces to plain courtyard-vs-courtyard.
- **Board containment** measures against the real Edge.Cuts rings, not an inset
  of the axis-aligned `board_bounds`, so parts are not nudged into an L-shaped
  board's notch or an interior cutout. Three levels of short-circuit keep it
  cheap: the gate self-disables when the outline *is* its bounding box (or when
  the parser found no usable ring, where the bbox is all there is), then a cached
  per-part reachable-disk prune, then the exact ring test.
- **Off-board seeds are not frozen.** Only candidate poses are validated, never
  the incumbent, so a part sitting outside the board had every alternative
  rejected and could never move — not even toward the board. A part whose only
  violation is board containment may now take a pose that moves it strictly back
  toward the board without overlapping anything.

  This is deliberately limited to the board term. An *overlapping* part keeps the
  original rule (it may move only to a fully legal pose), because the violation
  measure is a distance while the thing at stake is an area: trading one deep
  narrow overlap for a shallow wide one lowers the distance and raises the area.
  Measured on `watchy`, where 81 of 82 parts start in violation — its hand
  placement is tighter than the 0.25 mm courtyard clearance quench asks for — a
  permissive rule took total courtyard overlap from 9.1 mm² to 37.9 mm²
  (strict-decrease: 16.8) where the board-only rule gets 0.23 while also walking
  10 of the 13 off-board parts back on.
- **Graders.** `placement_overlap_area` (OO, mm²) and `placement_out_of_board`
  (OoB) report the same geometry the optimizer gates on;
  `QuenchState.legality_metrics()` returns both for a live placement. All zero
  means fully legal. Intended for the placement scorecard in #411/#110.

**Cost on two-sided boards.** Side-awareness removes a large number of false
collisions, so many candidate poses that used to be rejected outright now reach
the cost function. On `glasgow_revC` (172 front / 92 back parts) a bounded 40-part
pass makes **3.4× more airwire cost evaluations** than before (9.3k → 31.4k
`_count_crossings_np` calls) and takes correspondingly longer. Nothing per-call
got slower — the optimizer is searching the space it was previously, wrongly,
skipping. Small boards are unaffected or faster (`watchy`: 69 s against 109 s
before).

Courtyard extraction (`parser.py`) reads `fp_line`/`fp_rect`/`fp_arc`/
`fp_circle`/`fp_poly` per side. A footprint with no courtyard on any layer falls
back to its pad bounding box — which carries no courtyard margin at all, so the
part is modelled smaller than it is; that fallback now warns and names the refs.

Note: an earlier from-scratch constructive placer (`place.py` +
`rust_placer/`) was removed after experiments showed hand placements beat it
by ~500× in router effort; see git history and
docs/placement-optimization.md for details.
