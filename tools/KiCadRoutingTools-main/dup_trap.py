"""KICAD_DUP_TRAP=1: name the code path that puts the SAME copper object into
pcb_data twice.

The BOARD/FILE ledgers detect duplicate object entries but only AFTER the run,
so they say "net 38 has a duplicate" and nothing about who appended it. This
wraps pcb_data.segments / pcb_data.vias in a list subclass that prints the
offending stack the moment a re-append happens, turning a whole re-run into a
single pinpointed call site.

Why a duplicate object entry is always a bug: the write model holds the object
once, so the ledger reports phantom board-only copper; obstacles get
double-stamped (#208/#309); and gates treating the list as a node set are
defeated (#195).

NO FALSE POSITIVES FROM REMOVALS: code removes copper with slice assignment
(``pcb_data.segments[:] = [...]``) and ``.remove()``, which an id-set cache
would not see -- a later legitimate restore would then look like a duplicate.
So the id set is only a cheap PRE-FILTER; on a hit we confirm by scanning the
real list for an identical object. The O(n) scan runs only on suspicion.

Enable for one run:

    KICAD_DUP_TRAP=1 python3 route.py board.kicad_pcb out.kicad_pcb --nets '*'
"""
import os
import sys
import traceback


class DupTrapList(list):
    """A list that reports (does not block) re-appends of the same object."""

    def __init__(self, iterable=(), label='list', limit=12):
        super().__init__(iterable)
        self._label = label
        self._ids = {id(o) for o in self}
        self._limit = limit
        self._hits = 0

    def _is_really_present(self, obj):
        # Confirm against the LIVE list: the id cache never learns about
        # removals, so it over-reports on its own.
        for existing in self:
            if existing is obj:
                return True
        return False

    def _check(self, obj):
        if id(obj) in self._ids and self._is_really_present(obj):
            self._hits += 1
            if self._hits <= self._limit:
                what = getattr(obj, 'layer', None)
                where = (f"({getattr(obj, 'start_x', getattr(obj, 'x', '?'))},"
                         f"{getattr(obj, 'start_y', getattr(obj, 'y', '?'))})")
                print(f"\n[DUP_TRAP] {self._label}: re-append of the SAME "
                      f"object -- net {getattr(obj, 'net_id', '?')} {where} "
                      f"{what}", file=sys.stderr)
                traceback.print_stack(file=sys.stderr)
                sys.stderr.flush()
            elif self._hits == self._limit + 1:
                print(f"[DUP_TRAP] {self._label}: further hits suppressed",
                      file=sys.stderr)
        self._ids.add(id(obj))

    def append(self, obj):
        self._check(obj)
        super().append(obj)

    def extend(self, items):
        items = list(items)
        for o in items:
            self._check(o)
        super().extend(items)

    def insert(self, i, obj):
        self._check(obj)
        super().insert(i, obj)

    # -- the paths that bypass append/extend/insert -------------------------
    # A first version trapped only those three and reported ZERO hits on a
    # board whose ledger still found duplicates, which is what exposed these.

    def __iadd__(self, items):
        # `lst += items` runs list's C-level in-place concat and does NOT
        # call the extend() override above.
        items = list(items)
        for o in items:
            self._check(o)
        return super().__iadd__(items)

    def __setitem__(self, key, value):
        # Slice assignment (`pcb_data.segments[:] = [...]`) is how the
        # cleanup passes rebuild the list -- no element-wise hook fires, and
        # rebuilding from two overlapping sources lands the same object
        # twice. Cheapest correct check: do the assignment, then scan the
        # result for identity duplicates. Slice assigns are rare (a handful
        # of cleanup passes per run), so the O(n) scan is not hot.
        super().__setitem__(key, value)
        if isinstance(key, slice):
            self._rescan('slice assignment')
            self._ids = {id(o) for o in self}

    def _rescan(self, how):
        seen, dupes = set(), []
        for o in self:
            if id(o) in seen:
                dupes.append(o)
            seen.add(id(o))
        if not dupes:
            return
        self._hits += len(dupes)
        if self._hits <= self._limit:
            for o in dupes[:3]:
                print(f"\n[DUP_TRAP] {self._label}: {how} left the SAME "
                      f"object in the list twice -- net "
                      f"{getattr(o, 'net_id', '?')} "
                      f"({getattr(o, 'start_x', getattr(o, 'x', '?'))},"
                      f"{getattr(o, 'start_y', getattr(o, 'y', '?'))}) "
                      f"{getattr(o, 'layer', None)}", file=sys.stderr)
            traceback.print_stack(file=sys.stderr)
            sys.stderr.flush()


def _install_rebind_property(cls, name):
    """Make ``obj.<name> = [...]`` re-wrap instead of dropping the trap.

    ~40 sites rebind the whole list (``pcb_data.segments = [s for s in ...]``)
    rather than slice-assigning. Each one replaces the trap with a plain list,
    so without this the trap dies at the first filter pass and every later
    append is unwatched -- which is precisely how a run with real duplicates
    reported ZERO hits.

    Safe for OTHER PCBData instances (sub-runs parse their own boards): the
    storage is uniform (always the private slot), and wrapping happens only
    for instances explicitly armed via install().
    """
    priv = '_duptrap_' + name

    def getter(self):
        d = self.__dict__
        # `name` fallback: instances built BEFORE the property was installed
        # still carry the value in their instance dict.
        return d[priv] if priv in d else d.get(name)

    def setter(self, value):
        if (self.__dict__.get('_duptrap_armed')
                and not isinstance(value, DupTrapList)):
            old = self.__dict__.get(priv)
            lbl = getattr(old, '_label', name)
            hits = getattr(old, '_hits', 0)
            value = DupTrapList(value, lbl)
            value._hits = hits          # carry the tally across the rebind
            # A rebind can itself CREATE the duplicate -- e.g.
            # `pcb_data.segments = list(pcb_data.segments) + new` when `new`
            # overlaps. Scan the incoming list, not just later appends.
            value._rescan('rebind')
        self.__dict__[priv] = value

    setattr(cls, name, property(getter, setter))


def install(pcb_data, label=''):
    """Wrap pcb_data.segments/vias in traps. No-op unless KICAD_DUP_TRAP=1.

    Survives BOTH mutation styles: slice assignment (``segments[:] = [...]``)
    keeps the object, and whole-list rebinding is caught by a class-level
    property that re-wraps (see _install_rebind_property).
    """
    if os.environ.get('KICAD_DUP_TRAP', '') != '1':
        return pcb_data
    try:
        cls = type(pcb_data)
        if not getattr(cls, '_duptrap_props', False):
            for _n in ('segments', 'vias'):
                _install_rebind_property(cls, _n)
            cls._duptrap_props = True
        pcb_data._duptrap_armed = True
        # Assign through the new setter so both lists get wrapped.
        pcb_data.segments = DupTrapList(pcb_data.segments, f"{label}segments")
        pcb_data.vias = DupTrapList(pcb_data.vias, f"{label}vias")
        print(f"[DUP_TRAP] armed on {label or 'pcb_data'} "
              f"({len(pcb_data.segments)} segs, {len(pcb_data.vias)} vias); "
              f"rebind-proof")
    except Exception as e:      # a debug aid must never break a run
        print(f"[DUP_TRAP] could not arm: {e}")
    return pcb_data


def verify(pcb_data, label=''):
    """Report whether the traps are still installed, and any duplicate left.

    REBINDING (``pcb_data.segments = [...]``, as opposed to a slice assign)
    swaps in a plain list and silently disarms the trap -- so "0 hits" alone
    proves nothing. Intercepting that would mean a class-level property, and
    PCBData is a shared dataclass: a data descriptor there would hijack every
    OTHER instance (sub-runs parse their own boards). Detecting it after the
    fact is the safe half, and it tells us whether to believe a 0.
    """
    if os.environ.get('KICAD_DUP_TRAP', '') != '1':
        return
    for name in ('segments', 'vias'):
        lst = getattr(pcb_data, name, None)
        if isinstance(lst, DupTrapList):
            lst._rescan('final scan')
            print(f"[DUP_TRAP] {label}{name}: armed to the end, "
                  f"{lst._hits} hit(s)")
        else:
            print(f"[DUP_TRAP] {label}{name}: TRAP WAS REPLACED (rebound to "
                  f"a plain {type(lst).__name__}) -- any 0-hit result for "
                  f"this list is meaningless; find the assignment")
