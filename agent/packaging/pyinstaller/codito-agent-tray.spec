# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[2]
ENTRYPOINT = ROOT / "agent" / "packaging" / "entrypoints" / "tray.py"
PATHEX = [str(ROOT / "agent" / "src"), str(ROOT / "packages" / "protocol" / "src")]
VERSION_FILE = os.environ.get("CODITO_VERSION_FILE")
EXCLUDES = [
    "codito_relay",
    "django",
    "hypothesis",
    "mcp",
    "mypy",
    "oauth2_provider",
    "psycopg",
    "pytest",
    "redis",
    "starlette",
    "tkinter",
    "uvicorn",
]

a = Analysis(
    [str(ENTRYPOINT)],
    pathex=PATHEX,
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=1,
)
# Qt intentionally links to the Windows system ICU shim (`System32\\icuuc.dll`).
# A developer PATH can contain an unrelated Poppler ICU DLL with the same generic
# name; bundling that DLL causes QtCore to fail with ERROR_PROC_NOT_FOUND. Keep
# environment-provided ICU binaries out of the deterministic desktop package.
a.binaries = [
    entry
    for entry in a.binaries
    if Path(entry[0]).name.lower() != "icuuc.dll"
    and not Path(entry[0]).name.lower().startswith("icudt")
]
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="codito-agent-tray",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    version=VERSION_FILE,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="codito-agent-tray",
)
