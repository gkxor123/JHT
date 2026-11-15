# PyInstaller spec: builds a single-file Windows exe (pyinstaller SlideCap.spec)
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = [("slidecap/icon.ico", ".")]
binaries = []
hiddenimports = collect_submodules("av") + ["PIL.ImageTk"]
datas += collect_data_files("pptx")
for pkg in ("tkinterdnd2",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["slidecap_main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest"],
    noarchive=False,
    # python-pptx opens templates via "pptx/oxml/../templates": keep real folders
    module_collection_mode={"pptx": "py"},
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SlideCap",
    console=False,
    icon="slidecap/icon.ico",
    upx=False,
    runtime_tmpdir=None,
)
