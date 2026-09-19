# learn-helper-core.spec -- PyInstaller spec for the Python backend.
#
# Lives in native/ next to build_release.ps1 (which invokes it) so it is part of the
# tracked source: rebuilding the backend must not depend on untracked tooling.
#
# Why a spec at all (instead of the old one-liner in build_release.ps1):
# Playwright's own hook does `collect_data_files("playwright")`, which drags in the
# ENTIRE driver directory. Measured on this machine (2026-09-19):
#
#   playwright\driver\node.exe                      88.25 MB   <- by far the biggest item
#   playwright\driver\package\lib\vite\*             3.41 MB   <- trace viewer / recorder / html report
#   playwright\driver\package\types\*                1.85 MB   <- TypeScript definitions only
#   PIL/Pillow (excludes=)                          ~13.0 MB   <- only the optional test image
#   ------------------------------------------------------------------
#   backend onefile: 51.06 MB  ->  10.03 MB
#
# None of those are needed to drive a browser over CDP:
#   * node.exe  -> Playwright honours PLAYWRIGHT_NODEJS_PATH, so the release build uses
#                 the machine's own Node.js (verified: Node v24.14.0 drives the sandbox
#                 Edge fine). rthook_node.py sets it before playwright is imported.
#   * vite\*    -> only opened by `show-trace`, the recorder and the HTML reporter.
#   * types\*   -> .d.ts files for TypeScript consumers; the JS runtime never loads them.
#   * PIL       -> only `core.build_test_question_image` (the "test image" self-check),
#                 which already degrades with a "pip install pillow" message.
#
# Keep this file ASCII-only: PowerShell 5.1 misreads BOM-less UTF-8 as GBK, and a
# stray byte here silently changes the build inputs (rules/01 section 8.2).

import os

from PyInstaller.utils.hooks import collect_data_files

# ----------------------------------------------------------------- what to drop
# NOTE (measured): playwright's driver\node.exe is collected as a **BINARY**, not as a
# data file. Filtering `datas` alone therefore changes nothing -- the exe came out
# byte-identical at 53544224 B while the spec happily printed "excluded 93.51 MB".
# Both lists have to be filtered, and the counts are printed so this can never again
# look like it worked when it did not (see ERROR.md E68).
_EXCLUDE_MARKERS = (
    os.path.join('playwright', 'driver', 'node.exe'),
    os.path.join('playwright', 'driver', 'package', 'types') + os.sep,
    os.path.join('playwright', 'driver', 'package', 'lib', 'vite') + os.sep,
)

_dropped = []


def _keep(entry):
    """Keep `(src, dest)` entries whose source is not on the exclusion list."""
    src = entry[0]
    low = src.lower()
    for marker in _EXCLUDE_MARKERS:
        if marker.lower() in low:
            size = 0
            try:
                size = os.path.getsize(src)
            except OSError:
                pass
            _dropped.append((src, size))
            return False
    return True


datas = [d for d in collect_data_files('playwright') if _keep(d)]

# Runtime hook: sets PLAYWRIGHT_NODEJS_PATH before playwright is imported, so the slim
# build can use the machine's own Node.js (see rthook_node.py for the full reasoning).
_spec_dir = os.path.dirname(os.path.abspath(SPEC))  # noqa: F821
_rthook = os.path.join(_spec_dir, 'rthook_node.py')
_project_root = os.path.dirname(_spec_dir)

# ----------------------------------------------------------------- build graph
a = Analysis(  # noqa: F821  (injected by PyInstaller)
    [os.path.join(_project_root, 'backend', 'main.py')],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[_rthook] if os.path.isfile(_rthook) else [],
    # PIL/Pillow is only used by the optional "test image" self-check
    # (`core.build_test_question_image`, which already degrades with a readable
    # "pip install pillow" message when it is missing). It costs ~13 MB of the bundle
    # (measured: _avif 7.5 + _imaging 2.5 + _imagingft 2.1 + _webp 0.4 + _imagingcms 0.3),
    # so it is excluded here and the feature stays available in source/dev runs.
    excludes=['PIL', 'Pillow'],
    noarchive=False,
)

# Filter the *resolved* graph too: this is where node.exe actually arrives from.
a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

_total = sum(s for _, s in _dropped)
print('--- learn-helper-core.spec: filtering ---')
print('   datas kept      : %d' % len(datas))
print('   binaries kept   : %d' % len(a.binaries))
print('   dropped         : %d file(s), %.2f MB raw' % (len(_dropped), _total / 1024 / 1024))
for _src, _size in sorted(_dropped, key=lambda kv: -kv[1])[:6]:
    print('     - %8.2f MB  %s' % (_size / 1024 / 1024, _src))
print('-----------------------------------------')

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='learn-helper-core',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # console=True: the UI reads the backend's STDOUT handshake line
    # (`{"type":"ready",...}`), so the console subsystem must stay enabled.
    # This matches PyInstaller's default for the previous --onefile build.
    console=True,
    disable_windowed_traceback=False,
)
