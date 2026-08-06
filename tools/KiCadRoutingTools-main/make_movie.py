#!/usr/bin/env python3
"""Make a routing movie (.mp4) from boards you already have (issue #506).

The stress harness renders one per run; this is the same movie, on demand, from
anything: a run/output directory, an explicit list of step boards, or a single
board that draws itself.

    python3 make_movie.py RUNDIR                 # whole chain -> RUNDIR/routing.mp4
    python3 make_movie.py step1.kicad_pcb step2.kicad_pcb step3.kicad_pcb
    python3 make_movie.py board.kicad_pcb        # one board reveals its copper
    python3 make_movie.py RUNDIR -o out.mp4 --size 1400 --fps 8 --png

New copper flashes white, reroutes/restores green, rips flash red on the frame
before they vanish. Steps that recorded a fine trace (``KICAD_ROUTE_TRACE=1``
leaves ``<board>_routetrace.json``) animate per segment/via; steps without one
reveal their board-to-board delta in chunks.

**Output format follows the extension**: ``.mp4`` (H.264, small, plays
everywhere) needs ``imageio`` + ``imageio-ffmpeg`` and falls back to a sibling
``.gif`` when they are missing; ``.gif`` is native Pillow, no dependency.

This module is the shared core: ``tests/stress/render_run.py`` (stress runs) and
the GUI's "Routing Movie..." button both call ``make_movie()`` rather than
re-implementing it, so all three produce the same movie.
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Defaults shared by the CLI, the stress renderer and the GUI button, so the
# movie looks the same wherever it is made from.
DEFAULT_SIZE = 1000
DEFAULT_FPS = 6.0
DEFAULT_SUPERSAMPLE = 1
DEFAULT_LAYER_ALPHA = 150
DEFAULT_RIP_HOLD = 2
DEFAULT_CHUNKS = 6
DEFAULT_END_HOLD = 1.5


def resolve_inputs(inputs):
    """(steps, final_board) for whatever was passed.

    * a single directory  -> the chain discovered in it (stepN order, else
      write-time order), exactly as a stress run's movie sees it
    * one or more boards  -> that sequence, in the order given (already the
      chain order for GUI snapshots / shell globs like ``step*.kicad_pcb``)
    """
    import animate_route as a
    if len(inputs) == 1 and os.path.isdir(inputs[0]):
        return a.discover_steps(inputs[0])
    boards = [os.path.abspath(b) for b in inputs]
    missing = [b for b in boards if not os.path.exists(b)]
    if missing:
        raise FileNotFoundError(missing[0])
    return a.steps_for_boards(boards), (boards[-1] if boards else None)


def default_output(inputs):
    """Where the movie lands when no -o is given: inside a run dir, else next to
    the last board."""
    if len(inputs) == 1 and os.path.isdir(inputs[0]):
        return os.path.join(os.path.abspath(inputs[0]), 'routing.mp4')
    return os.path.splitext(os.path.abspath(inputs[-1]))[0] + '_routing.mp4'


def make_movie(inputs, out=None, size=DEFAULT_SIZE, fps=DEFAULT_FPS,
               supersample=DEFAULT_SUPERSAMPLE, layer_alpha=DEFAULT_LAYER_ALPHA,
               rip_hold=DEFAULT_RIP_HOLD, chunks=DEFAULT_CHUNKS,
               end_hold=DEFAULT_END_HOLD, png_dir=None, quiet=False):
    """Render the movie. ``inputs`` is a run dir (one entry) or a board sequence.

    Returns the path actually written -- which is a sibling ``.gif`` when an
    ``.mp4`` was asked for and imageio-ffmpeg is unavailable -- or None when
    there was nothing to animate.
    """
    import animate_route as a
    if isinstance(inputs, str):
        inputs = [inputs]
    if not inputs:
        return None
    steps, final = resolve_inputs(inputs)
    if not final:
        if not quiet:
            print(f"make_movie: no boards found in {inputs[0]}", file=sys.stderr)
        return None
    frames = a.build_boards(steps, final, size, supersample, layer_alpha,
                            rip_hold, chunks)
    if not frames:
        if not quiet:
            print("make_movie: no frames (nothing routed?)", file=sys.stderr)
        return None
    out = out or default_output(inputs)
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    if not a.save_movie(frames, out, fps=fps, end_hold=end_hold, png_dir=png_dir):
        return None
    # save_movie falls back .mp4 -> .gif when imageio-ffmpeg is missing; report
    # the file that actually exists so callers (and the GUI) point at it.
    if out.lower().endswith('.mp4') and not os.path.exists(out):
        out = os.path.splitext(out)[0] + '.gif'
    return out


def board_snapshot(board_path, out=None, size=1600, quiet=False):
    """Still PNG of a board (the movie's last frame, at full resolution)."""
    from route_render import render_board_file
    out = out or (os.path.splitext(os.path.abspath(board_path))[0] + '.png')
    return render_board_file(board_path, out, size=size, quiet=quiet)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='+',
                    help='a run/output DIRECTORY, or step boards in chain order, '
                         'or a single board')
    ap.add_argument('-o', '--output', default=None,
                    help='output path; the extension picks the format (.mp4 default, '
                         '.gif for a dependency-free animated GIF). '
                         'Default: <rundir>/routing.mp4 or <last-board>_routing.mp4')
    ap.add_argument('--size', type=int, default=DEFAULT_SIZE,
                    help=f'longest frame dimension in px (default {DEFAULT_SIZE})')
    ap.add_argument('--fps', type=float, default=DEFAULT_FPS,
                    help=f'frames per second (default {DEFAULT_FPS:g})')
    ap.add_argument('--supersample', type=int, default=DEFAULT_SUPERSAMPLE,
                    help='anti-aliasing factor (1 = fastest, 2 = crisp)')
    ap.add_argument('--layer-alpha', type=int, default=DEFAULT_LAYER_ALPHA,
                    help='per-layer copper opacity 1-255 (<255 blends crossings)')
    ap.add_argument('--rip-hold', type=int, default=DEFAULT_RIP_HOLD,
                    help='frames to hold ripped copper red before it vanishes')
    ap.add_argument('--chunks', type=int, default=DEFAULT_CHUNKS,
                    help='reveal batches for a step with no fine trace')
    ap.add_argument('--end-hold', type=float, default=DEFAULT_END_HOLD,
                    help='seconds to hold the final frame')
    ap.add_argument('--png-dir', default=None,
                    help='also dump the raw PNG frames here')
    ap.add_argument('--png', action='store_true',
                    help='also write a full-resolution still of the final board')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    try:
        out = make_movie(args.inputs, out=args.output, size=args.size, fps=args.fps,
                         supersample=args.supersample, layer_alpha=args.layer_alpha,
                         rip_hold=args.rip_hold, chunks=args.chunks,
                         end_hold=args.end_hold, png_dir=args.png_dir,
                         quiet=args.quiet)
    except FileNotFoundError as e:
        print(f"make_movie: no such board: {e}", file=sys.stderr)
        return 1
    except ImportError as e:
        print(f"make_movie: missing dependency ({e}). Pillow is required; "
              f"for .mp4 also: pip install imageio imageio-ffmpeg", file=sys.stderr)
        return 1
    if not out:
        return 1
    if args.png:
        _steps, final = resolve_inputs(args.inputs)
        if final:
            still = board_snapshot(final, quiet=args.quiet)
            if still and not args.quiet:
                print(f"make_movie: wrote {still}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
