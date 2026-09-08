# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path


ROOT = Path(SPECPATH).resolve().parents[2]
ENTRYPOINT = ROOT / "agent" / "packaging" / "entrypoints" / "cli.py"
PATHEX = [str(ROOT / "agent" / "src"), str(ROOT / "packages" / "protocol" / "src")]
VERSION_FILE = os.environ.get("CODITO_VERSION_FILE")
EXCLUDES = [
    "PySide6",
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
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="codito-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version=VERSION_FILE,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="codito-agent",
)
