# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the TradeBook --onefile build.

Build:
    pyinstaller tradebook.spec --noconfirm

Output:
    dist/TradeBook.exe   (single executable)

Runtime layout (next to TradeBook.exe):
    TradeBook.exe
    data/
        tradebook.db
    backups/
        tradebook_YYYYMMDD_HHMMSS.db

Both folders are auto-created by `config.ensure_user_dirs()` on first
launch (called from `main.main()` before the DB is opened).

Bundled (read-only) resources live inside the exe and are unpacked to
sys._MEIPASS at runtime — `config.RESOURCE_ROOT` resolves to that path.
"""
from pathlib import Path

block_cipher = None

PROJECT_DIR = Path(SPECPATH)

# Conda-style Python installs put a bunch of stdlib support DLLs in
# `<prefix>/Library/bin/` rather than next to `python.exe`. PyInstaller's
# binary dependency resolver doesn't search there, so it emits
# "Library not found" warnings for libffi-8.dll, libssl, sqlite3, etc.
# and the resulting exe crashes at startup with "DLL load failed while
# importing _ctypes". Bundle them explicitly here.
import sys as _sys
_LIBRARY_BIN = Path(_sys.base_prefix) / "Library" / "bin"
_REQUIRED_DLLS = [
    "ffi-8.dll",          # _ctypes  → required by pyqtgraph at import time
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "libmpdec-4.dll",     # _decimal
    "liblzma.dll",        # _lzma
    "libbz2.dll",         # _bz2
    "libexpat.dll",       # pyexpat
    "sqlite3.dll",        # _sqlite3
]
binaries = []
if _LIBRARY_BIN.exists():
    for _name in _REQUIRED_DLLS:
        _src = _LIBRARY_BIN / _name
        if _src.exists():
            binaries.append((str(_src), "."))

# Files we need to ship inside the bundle (the writable data/backups
# folders are NOT included — they're created next to the exe at runtime).
datas = [
    (str(PROJECT_DIR / "gui" / "styles" / "dark_theme.qss"),
     "gui/styles"),
]

# PySide6 + pyqtgraph hidden imports — PyInstaller's hooks usually catch
# these, but list a few load-bearing ones explicitly to be safe.
hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "pyqtgraph",
    # python-docx — uses lxml under the hood; several submodules need
    # explicit inclusion because they're loaded dynamically at runtime.
    "docx",
    "lxml",
    "lxml.etree",
]


a = Analysis(
    ["main.py"],
    pathex=[str(PROJECT_DIR)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy unused stacks. Drop them to keep the exe small.
        "tkinter",
        "matplotlib",
        "scipy",
        "notebook",
        "IPython",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="TradeBook",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_DIR / "tradebook.ico"),
    # Stamps Properties → Details with the version from config.APP_VERSION.
    version=str(PROJECT_DIR / "version_info.txt"),
)
