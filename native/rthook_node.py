# rthook_node.py -- PyInstaller runtime hook for the learn-helper backend.
#
# Runs BEFORE any user code (and before `import playwright`), which is exactly when
# PLAYWRIGHT_NODEJS_PATH has to be set: playwright's driver launcher reads it in
# `compute_driver_executable()` (playwright/_impl/_driver.py) at import/start time.
#
# Background: the release build deliberately does NOT ship playwright's bundled
# `driver\node.exe` (88 MB). Playwright needs *some* node to start its driver, so we
# point it at the machine's own Node.js. If there is none we leave the variable alone
# and playwright falls back to its bundled copy -- which exists in development and in
# the "full" build, and simply fails loudly (with a readable message) in the slim build.
#
# Keep this file ASCII-only (PS 5.1 / GBK discipline).

import os
import shutil
import sys


def _find_node():
    # 1) explicit override wins
    override = os.environ.get('LH_NODE_PATH')
    if override and os.path.isfile(override):
        return override

    candidates = [
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'nodejs', 'node.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     'nodejs', 'node.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'nodejs', 'node.exe'),
        os.path.join(os.environ.get('APPDATA', ''), 'npm', 'node.exe'),
    ]
    found = shutil.which('node')
    if found:
        candidates.append(found)

    for path in candidates:
        try:
            if path and os.path.isfile(path):
                return path
        except OSError:
            continue
    return None


def _base_dir():
    """Where the app's runtime files live (config.json / logs / native-diag.log).

    NOTE: `sys.executable` alone is NOT it: in a PyInstaller **onefile** build the child
    process runs from a temporary `_MEIxxxx` directory that is deleted on exit, so a log
    written next to `sys.executable` (or next to `sys._MEIPASS`) vanishes and can never be
    inspected -- that was the original bug (see ERROR.md E72).

    Order: the app's own `LH_BASE_DIR` override -> `learn_helper.config.BASE_DIR`
    (single source of truth; it already handles frozen vs source) -> beside the exe.
    """
    override = os.environ.get('LH_BASE_DIR')
    if override:
        return override
    try:
        from learn_helper.config import BASE_DIR           # single source of truth
        return BASE_DIR
    except Exception:
        pass
    try:
        exe = os.path.abspath(sys.executable)
        if getattr(sys, 'frozen', False):
            # onefile: sys.executable is the launcher exe (real location);
            # _MEIPASS would be the throwaway extraction dir, so never use it.
            return os.path.dirname(exe)
        return os.path.dirname(os.path.dirname(exe))
    except Exception:
        return os.getcwd()


def _banner(message):
    """Write a diagnostics line where the operator can actually find it."""
    try:
        base = _base_dir()
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            pass
        with open(os.path.join(base, 'native-diag.log'), 'a', encoding='utf-8') as fh:
            fh.write('[rthook] %s\n' % message)
    except Exception:
        pass
    # stderr is still useful: when frozen, the UI captures the backend's stderr and
    # copies it into its own native-diag.log, so the message survives that way too.
    try:
        sys.stderr.write('[rthook] %s\n' % message)
        sys.stderr.flush()
    except Exception:
        pass


try:
    if not os.environ.get('PLAYWRIGHT_NODEJS_PATH'):
        node = _find_node()
        if node:
            os.environ['PLAYWRIGHT_NODEJS_PATH'] = node
            _banner('using system node: %s' % node)
        else:
            _banner('no system node found; playwright will use its bundled runtime')
except Exception as exc:  # never break startup because of this
    _banner('node detection failed: %r' % (exc,))
