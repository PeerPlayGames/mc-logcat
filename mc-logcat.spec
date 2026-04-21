# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Merge Cruise Logcat Viewer.
Run:  pyinstaller mc-logcat.spec
Output: dist/MergeCruiseLogcat.app
"""

import os
block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('templates', 'templates'),   # HTML frontend
        ('server.py', '.'),           # imported by launcher
    ],
    hiddenimports=[
        'flask',
        'flask_socketio',
        'engineio',
        'socketio',
        'engineio.async_drivers.threading',
        'socketio.async_drivers.threading',
        'anthropic',
        'bidict',
        'dns',
        'dns.resolver',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MergeCruiseLogcat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # No terminal window
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='MergeCruiseLogcat',
)

app = BUNDLE(
    coll,
    name='MergeCruiseLogcat.app',
    icon=None,
    bundle_identifier='com.peerplay.mc-logcat',
    info_plist={
        'CFBundleName':             'MergeCruiseLogcat',
        'CFBundleDisplayName':      'Merge Cruise Logcat',
        'CFBundleVersion':          '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable':  True,
        'LSUIElement':              False,   # Show in Dock
        'NSRequiresAquaSystemAppearance': False,
    },
)
