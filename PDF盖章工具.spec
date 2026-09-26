# -*- mode: python ; coding: utf-8 -*-
# 注意: 打包参数的单一来源(single source of truth)是 build_exe.py。
# 本 spec 仅供手动调试；修改参数请先改 build_exe.py，再同步到这里。

import sys

_icon = 'assets/stamp.ico' if sys.platform == 'win32' else None

a = Analysis(
    ['pdf_stamper.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['PIL._tkinter_finder', 'PIL.Image', 'PIL.ImageTk'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PDF盖章工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)
