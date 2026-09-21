# learn-helper-ui.spec -- PyInstaller spec for the PySide6 (Qt6) frontend.
#
# Lives in native/ next to build_release.ps1 (which invokes it) so rebuilding the UI
# does not depend on untracked tooling -- same convention as learn-helper-core.spec.
#
# Why a spec instead of a command line: Qt ships a LOT of optional modules, and the
# PySide6 PyInstaller hook pulls in several of them. We only use QtCore/QtGui/QtWidgets,
# so the heavy ones are excluded explicitly. Measured on this machine (2026-09-21):
#
#   onefile frontend: ~40 MB with the exclusions below (PySide6 itself is the bulk)
#
# Excluded on purpose (none of them are imported by frontend/app.py):
#   QtWebEngine*  -> a whole Chromium; the project drives a REAL Edge over CDP instead
#   QtQml/QtQuick/Qt3D/QtCharts/QtDataVisualization/QtMultimedia -> not used
#   QtNetwork     -> we talk HTTP with `requests`, not QtNetwork
#   QtSql/QtTest/QtDesigner/QtHelp/QtOpenGL* -> not used
#   tkinter       -> the old Tk client is NOT the product UI (and the user rejected it)
#
# Keep this file ASCII-only (PowerShell 5.1 misreads BOM-less UTF-8 as GBK, which would
# silently change the build inputs -- rules/01 section 8.2).

import os
import sys

_REPO = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    # ⚠️ 入口必须是 frontend/main.py，**不能**是 frontend/app.py：
    # app.py 里用的是相对导入，而 PyInstaller 会把入口当顶层脚本执行
    # （没有 __package__）⇒ "attempted relative import with no known parent package"，
    # 打出来的 exe 双击就闪退（console=False，连报错都看不见）。本轮实测踩到。
    [os.path.join(_REPO, 'frontend', 'main.py')],
    pathex=[_REPO],
    binaries=[],
    datas=[],
    hiddenimports=['frontend', 'frontend.app', 'frontend.backend_client'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Qt modules we never import
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineQuick',
        'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets', 'PySide6.QtQuick3D',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DAnimation',
        'PySide6.Qt3DExtras', 'PySide6.Qt3DInput', 'PySide6.Qt3DLogic',
        'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtGraphs',
        'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets',
        'PySide6.QtNetwork', 'PySide6.QtNetworkAuth',
        'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner', 'PySide6.QtHelp',
        'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtSvgWidgets',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
        'PySide6.QtSerialPort', 'PySide6.QtSensors', 'PySide6.QtRemoteObjects',
        'PySide6.QtScxml', 'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech',
        'PySide6.QtUiTools', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
        'PySide6.QtHttpServer', 'PySide6.QtSpatialAudio',
        # The old Tk client is not the product UI (user explicitly rejected Tk).
        'tkinter', 'turtle',
        # Backend-only deps must NOT be pulled into the UI exe.
        'playwright', 'PIL', 'Pillow',
        # Test frameworks
        'pytest', 'unittest',
    ],
    noarchive=False,
)

# Drop Qt translations/locales we do not ship and any leftover test binaries, then report
# what was dropped -- a silent filter is exactly how the backend "slimming" once looked
# like it worked while the exe stayed byte-identical (ERROR.md E68).
_dropped = 0
_dropped_bytes = 0
_keep = []
_DROP_PARTS = (
    os.path.join('PySide6', 'translations'),
    os.path.join('PySide6', 'qml'),
    os.path.join('PySide6', 'Assistant'),
    os.path.join('PySide6', 'Designer'),
    os.path.join('PySide6', 'linguist'),
)
for _entry in a.datas:
    _path = _entry[0]
    if any(_p in _path for _p in _DROP_PARTS):
        _dropped += 1
        continue
    _keep.append(_entry)
a.datas = _keep
print(f'[ui-spec] dropped {_dropped} data file(s) (Qt translations/qml/tools)')

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LearnHelperUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,              # GUI app: no console window
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, 'logo.ico') if os.path.exists(
        os.path.join(SPECPATH, 'logo.ico')) else None,
    onefile=True,
)
