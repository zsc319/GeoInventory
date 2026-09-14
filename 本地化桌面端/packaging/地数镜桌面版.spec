# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

DESKTOP_ROOT = Path(SPECPATH).parent.parent
PROJECT_ROOT = DESKTOP_ROOT.parent

a = Analysis(
    [str(DESKTOP_ROOT / "tk_desktop.py")],
    pathex=[str(PROJECT_ROOT), str(DESKTOP_ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["tkinter", "openpyxl", "pyodbc"],
    excludes=["IPython", "jupyter", "pytest", "matplotlib", "numpy", "pandas", "scipy"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="地数镜桌面版",
    debug=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="地数镜桌面版",
)
