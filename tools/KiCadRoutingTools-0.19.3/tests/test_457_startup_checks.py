"""
Tests for startup_checks raising rather than exiting (issue #457 item 3).

`startup_checks` called `sys.exit(1)` when numpy/scipy/shapely were missing or the
Rust router was absent or stale, and route.py / route_diff.py / route_planes.py /
route_disconnected_planes.py all call it at MODULE scope. A `SystemExit` raised
during pytest collection escapes as an INTERNALERROR rather than a per-file
collect error, so on a checkout with no built router the 8 test files that import
a routing module at module level took the whole ~200-test suite down with them.

The checks now raise `StartupCheckError`. The CLI behaviour is deliberately
unchanged — `exit_on_error_if_main(__name__)` prints the same message to stdout
and exits 1 when the module IS the program — but an importer gets a catchable
exception instead of a dead interpreter.

This module had ZERO test coverage before.
"""

import io
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import startup_checks
from startup_checks import (MIN_PYTHON, StartupCheckError, check_python_dependencies,
                           check_rust_library, exit_on_error_if_main, run_all_checks)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _NoModule:
    """Import hook that makes specific top-level modules unimportable."""

    def __init__(self, *names):
        self.names = set(names)

    def find_module(self, name, path=None):     # py2-style, still consulted
        return self if name.split('.')[0] in self.names else None

    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in self.names:
            raise ImportError(f"blocked for test: {name}")
        return None


def _without(*names):
    """Context: those modules raise ImportError, and are evicted from the cache."""
    class Ctx:
        def __enter__(self):
            self.hook = _NoModule(*names)
            sys.meta_path.insert(0, self.hook)
            self.saved = {k: v for k, v in sys.modules.items()
                          if k.split('.')[0] in names}
            for k in self.saved:
                del sys.modules[k]
            return self

        def __exit__(self, *exc):
            sys.meta_path.remove(self.hook)
            sys.modules.update(self.saved)
            return False
    return Ctx()


# --- the checks raise, and say something useful ------------------------------

def test_missing_python_dep_raises_not_exits():
    with _without('scipy'):
        try:
            check_python_dependencies()
        except StartupCheckError as exc:
            msg = str(exc)
        except SystemExit:                       # the bug
            raise AssertionError(
                "check_python_dependencies still calls sys.exit; pytest "
                "collection cannot survive that")
        else:
            raise AssertionError("expected StartupCheckError")
    assert 'scipy' in msg, f"missing library not named: {msg!r}"
    assert 'pip install' in msg, f"no install instruction: {msg!r}"


def test_startup_check_error_is_a_runtime_error():
    """So `except Exception` sites keep working, and it is not a SystemExit."""
    assert issubclass(StartupCheckError, RuntimeError)
    assert not issubclass(StartupCheckError, SystemExit)


def test_missing_rust_router_raises_and_keeps_the_import_error_text():
    """The dlopen message is the ONLY place the real cause appears; a bare
    `except ImportError` used to discard it."""
    with _without('grid_router'):
        try:
            check_rust_library()
        except StartupCheckError as exc:
            msg = str(exc)
        except SystemExit:
            raise AssertionError("check_rust_library still calls sys.exit")
        else:
            raise AssertionError("expected StartupCheckError")
    assert 'Rust router module not found' in msg, msg
    assert 'blocked for test' in msg, \
        f"the underlying ImportError text was swallowed: {msg!r}"
    assert 'build_router.py' in msg, msg


def test_run_all_checks_propagates():
    with _without('shapely'):
        try:
            run_all_checks()
        except StartupCheckError:
            return
        except SystemExit:
            raise AssertionError("run_all_checks still exits")
    raise AssertionError("expected StartupCheckError")


def test_python_floor_matches_the_abi3_pin():
    """MIN_PYTHON must track rust_router/Cargo.toml's abi3 feature, or the
    message tells users the wrong version."""
    cargo = open(os.path.join(ROOT, 'rust_router', 'Cargo.toml'),
                 encoding='utf-8').read()
    tag = f"abi3-py{MIN_PYTHON[0]}{MIN_PYTHON[1]}"
    assert tag in cargo, \
        f"MIN_PYTHON={MIN_PYTHON} implies {tag}, not found in Cargo.toml"


# --- CLI behaviour is unchanged ----------------------------------------------

_PROBE = textwrap.dedent('''
    import sys
    import startup_checks


    def boom():
        raise startup_checks.StartupCheckError("SIMULATED: router missing")


    startup_checks.check_rust_library = boom
    startup_checks.exit_on_error_if_main(sys.argv[1])
    print("REACHED-END")
''')


def _run_probe(module_name):
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, '-c', _PROBE, module_name],
                          capture_output=True, text=True, cwd=ROOT, env=env)


def test_as_main_prints_and_exits_one():
    """A user running `python3 route.py` must see exactly what they saw before."""
    r = _run_probe('__main__')
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}"
    assert 'SIMULATED: router missing' in r.stdout, \
        f"message not printed to stdout: {r.stdout!r}"
    assert 'Traceback' not in r.stderr, \
        f"a CLI user should not see a traceback: {r.stderr!r}"
    assert 'REACHED-END' not in r.stdout


def test_as_import_raises_instead():
    """An importer gets the exception, so pytest can attribute it to a file."""
    r = _run_probe('route')
    assert r.returncode == 1
    assert 'StartupCheckError' in r.stderr, \
        f"expected the exception to propagate: {r.stderr!r}"
    assert 'REACHED-END' not in r.stdout


def test_routing_clis_use_the_guard_at_module_scope():
    """All four entry points must go through the helper, and must still do it
    ABOVE their heavy imports so a missing dep is reported before numpy or
    grid_router fails with something cryptic."""
    for name in ('route.py', 'route_diff.py', 'route_planes.py',
                 'route_disconnected_planes.py'):
        src = open(os.path.join(ROOT, name), encoding='utf-8').read()
        assert 'exit_on_error_if_main(__name__)' in src, \
            f"{name} does not use the import-safe guard"
        assert 'run_all_checks()' not in src, \
            f"{name} still calls run_all_checks() directly, which exits on import"
        guard = src.index('exit_on_error_if_main(__name__)')
        for heavy in ('\nimport numpy', '\nfrom kicad_parser', '\nimport grid_router'):
            if heavy in src:
                assert src.index(heavy) > guard, \
                    f"{name}: {heavy.strip()} is imported before the startup guard"


def test_gui_catches_the_new_exception_type():
    """CLAUDE.md's CLI/GUI rule: swig_gui catches what `import route` throws.
    Catching only SystemExit would turn its dependency dialog into a traceback.
    (wx/pcbnew-only code, so this is a source assertion, not an execution.)"""
    src = open(os.path.join(ROOT, 'kicad_routing_plugin', 'swig_gui.py'),
               encoding='utf-8').read()
    assert '_STARTUP_FAILURES' in src, \
        "swig_gui does not reference the startup-failure tuple"
    assert 'except _STARTUP_FAILURES' in src, \
        "swig_gui still catches only SystemExit around `import route`"
    assert 'StartupCheckError' in src


TESTS = [
    test_missing_python_dep_raises_not_exits,
    test_startup_check_error_is_a_runtime_error,
    test_missing_rust_router_raises_and_keeps_the_import_error_text,
    test_run_all_checks_propagates,
    test_python_floor_matches_the_abi3_pin,
    test_as_main_prints_and_exits_one,
    test_as_import_raises_instead,
    test_routing_clis_use_the_guard_at_module_scope,
    test_gui_catches_the_new_exception_type,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
