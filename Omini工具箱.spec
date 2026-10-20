# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path

sys.path.insert(0, str(Path(SPECPATH)))
from release_packaging import EXCLUDED_QT_FRONTENDS, keep_resource

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=['socks'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook.py'],
    excludes=EXCLUDED_QT_FRONTENDS,
    noarchive=False,
    optimize=0,
)
a.datas = [entry for entry in a.datas if keep_resource(entry[0])]
a.binaries = [entry for entry in a.binaries if keep_resource(entry[0])]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Omini工具箱',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/omini_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Omini工具箱',
)
