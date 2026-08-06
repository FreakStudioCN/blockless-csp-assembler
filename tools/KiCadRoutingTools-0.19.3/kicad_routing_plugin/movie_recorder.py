"""Routing movie recorder for the GUI (issue #506).

Turned on by the Advanced tab's **Make routing movie** debug checkbox (default
off). While it is on, the live board is snapshotted after every routing step and
rendered into a movie by the shared ``make_movie`` core -- the same movie
``python3 make_movie.py`` makes from a directory of step boards, and the same one
a stress run leaves.

Two scopes, which is the whole point of the design:

* **One routing step** (Route / Route Diff Pairs / Create Planes / Repair Planes
  / Fanout pressed on its own tab) -> that step gets its OWN movie, rendered as
  soon as it completes: baseline board -> the board it produced.
* **A plan run** (the AI tab's *Run Selected Steps* / *Run All Selected
  Steps*) -> ONE movie for all of the steps combined. ``begin_group()`` /
  ``end_group()`` bracket the run, so the per-step renders are held back and a
  single movie spans the whole plan.

The baseline is the board as it stood before the segment being recorded: taken
when the box is ticked, and reset to the end state after each movie, so
successive steps do not re-animate copper the previous movie already showed.

Rendering happens on a worker thread (parsing every step board and rasterizing
frames takes seconds), and the finished path is logged to the Log tab in GREEN
via the dialog's ANSI-aware log.
"""

import os
import shutil
import sys
import tempfile
import threading

import wx

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PLUGIN_DIR)
if ROOT_DIR not in sys.path:      # make_movie lives at the repo/plugin root
    sys.path.insert(0, ROOT_DIR)

GREEN = '\033[92m'
YELLOW = '\033[93m'
RESET = '\033[0m'


def record_movie_step(widget, action):
    """Snapshot the live board after a routing step, if recording is on.

    Called from every tab's completion path; resolves the dialog through the
    widget's top-level parent (the same idiom the completion popups use), so a
    tab needs no reference to the recorder and this is a no-op in any context
    without one (tests, a tab used standalone).
    """
    try:
        top = widget.GetTopLevelParent() if hasattr(widget, 'GetTopLevelParent') else None
        recorder = getattr(top, 'movie_recorder', None)
        if recorder is not None:
            recorder.step_done(action)
    except Exception as e:                       # never break a routing step
        print(f"Routing movie: capture skipped ({e})")


class MovieRecorder:
    """Per-dialog recorder. Owns a temp dir of step boards and renders movies."""

    def __init__(self, dialog):
        self.dialog = dialog
        self._dir = None
        self._baseline = None      # board as it was before this segment
        self._frames = []          # step boards captured since the baseline
        self._group = False        # inside a plan run: hold renders back
        self._count = 0            # steps captured, for snapshot names
        self._threads = []

    # ------------------------------------------------------------- enablement

    def enabled(self):
        """True when the Advanced tab's debug checkbox is ticked."""
        box = getattr(self.dialog, 'make_movie_check', None)
        try:
            return bool(box is not None and box.GetValue())
        except Exception:
            return False

    def on_toggle(self, event=None):
        """Checkbox handler: ticking it starts recording from the board as it
        stands now, so the first movie shows only what the next step adds."""
        if self.enabled():
            self._baseline = self._snapshot('baseline')
            self._frames = []
            self._log(f"Routing movie: ON - the next routing step writes a movie "
                      f"next to the board.")
        else:
            self._frames = []
        if event is not None:
            event.Skip()

    # --------------------------------------------------------------- capture

    def begin_group(self):
        """A plan run started: hold per-step renders back so the whole plan
        becomes ONE movie."""
        if not self.enabled():
            return
        self._group = True
        self._frames = []
        if self._baseline is None or not os.path.exists(self._baseline):
            self._baseline = self._snapshot('baseline')

    def end_group(self):
        """The plan run finished: render every step it ran as one movie."""
        if not self._group:
            return
        self._group = False
        self._render_and_reset()

    def step_done(self, action):
        """A routing step finished and the board has its new copper."""
        if not self.enabled():
            return
        self._count += 1
        snap = self._snapshot(f"step{self._count:02d}_{action or 'route'}")
        if snap is None:
            return
        self._frames.append(snap)
        if not self._group:            # a lone step renders its own movie now
            self._render_and_reset()

    # ---------------------------------------------------------------- render

    def _render_and_reset(self):
        frames = list(self._frames)
        self._frames = []
        if not frames:
            return
        boards = ([self._baseline] if self._baseline
                  and os.path.exists(self._baseline) else []) + frames
        # The end state is the baseline for whatever is recorded next, so the
        # following movie animates only ITS copper.
        self._baseline = frames[-1]
        out = self._output_path()
        self._log(f"Routing movie: rendering {len(boards)} board(s) -> "
                  f"{os.path.basename(out)} ...")

        def work():
            try:
                from make_movie import make_movie
                written = make_movie(boards, out=out, quiet=True)
            except ImportError as e:
                wx.CallAfter(self._log,
                             f"{YELLOW}Routing movie: not rendered - {e}. "
                             f"Install Pillow (and imageio + imageio-ffmpeg "
                             f"for .mp4).{RESET}")
                return
            except Exception as e:
                wx.CallAfter(self._log, f"{YELLOW}Routing movie: failed ({e}){RESET}")
                return
            if not written:
                wx.CallAfter(self._log,
                             f"{YELLOW}Routing movie: nothing to animate{RESET}")
                return
            note = ""
            if out.lower().endswith('.mp4') and written.lower().endswith('.gif'):
                note = ("  (.mp4 encoding needs imageio + imageio-ffmpeg; "
                        "wrote a GIF)")
            wx.CallAfter(self._log, f"{GREEN}Routing movie: {written}{RESET}{note}")

        thread = threading.Thread(target=work, daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

    def _output_path(self):
        """``<board>_routing.mp4`` beside the board, never overwriting an
        earlier movie (``_routing_2.mp4``, ``_routing_3.mp4``, ...) so every
        step of a session keeps its own."""
        board = getattr(self.dialog, 'board_filename', None)
        if board and os.path.isdir(os.path.dirname(os.path.abspath(board))):
            stem = os.path.splitext(os.path.abspath(board))[0] + '_routing'
        else:                       # unsaved board: keep it in the temp dir
            stem = os.path.join(self._temp_dir(), 'routing')
        out = f"{stem}.mp4"
        n = 1
        while os.path.exists(out) or os.path.exists(
                os.path.splitext(out)[0] + '.gif'):
            n += 1
            out = f"{stem}_{n}.mp4"
        return out

    def pending(self):
        """True while a render is still running (used by tests and teardown)."""
        return any(t.is_alive() for t in self._threads)

    # ---------------------------------------------------------------- helpers

    def _temp_dir(self):
        if not self._dir:
            self._dir = tempfile.mkdtemp(prefix='kicadrt_movie_')
        return self._dir

    def _snapshot(self, name):
        """Save the live board into the temp dir; None if that is not possible
        (no pcbnew, no board)."""
        try:
            import pcbnew
            board = pcbnew.GetBoard()
            if board is None:
                return None
            path = os.path.join(self._temp_dir(), f"{name}.kicad_pcb")
            pcbnew.SaveBoard(path, board)
            return path
        except Exception as e:
            self._log(f"{YELLOW}Routing movie: snapshot failed ({e}){RESET}")
            return None

    def _log(self, message):
        """Write to the Log tab (it renders ANSI colors), else to stdout."""
        append = getattr(self.dialog, '_append_log', None)
        if append is not None:
            try:
                append(message + "\n")
                return
            except Exception:
                pass
        print(message)

    def cleanup(self):
        """Drop the temp snapshots, unless a render is still reading them (the
        OS temp dir collects those)."""
        if self._dir and not any(t.is_alive() for t in self._threads):
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self._frames = []
        self._baseline = None
