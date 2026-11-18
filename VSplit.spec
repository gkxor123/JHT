# PyInstaller spec: single-file Windows exe (pyinstaller VSplit.spec)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries = [], []
hiddenimports = collect_submodules("av")
for pkg in ("tkinterdnd2",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["vsplit_main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest", "cv2", "pptx"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="VSplit",
    console=False,
    upx=False,
    runtime_tmpdir=None,
)
