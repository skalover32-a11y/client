# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['vlf_gui.py'],
    pathex=[],
    binaries=[
        ('_internal/sing-box.exe', '_internal'),
        ('_internal/wintun.dll', '_internal'),
    ],
    datas=[
        ('vlf_gui_config.json', '.'),
        ('vlf_logo.png', '.'),
        ('vlf.ico', '.'),
        ('profiles.json', '.'),
        ('config.json', '.'),
        ('_internal/base_config.json', '_internal'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VLF_VPN',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon='vlf.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VLF_VPN',
)
