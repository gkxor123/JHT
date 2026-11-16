# PyInstaller spec: single-file Windows exe (cd ppt2pdf && pyinstaller PPT2PDF.spec)
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], ["win32com.client.dynamic", "pythoncom", "pywintypes"]
d, b, h = collect_all("tkinterdnd2")
datas += d; binaries += b; hiddenimports += h

a = Analysis(
    ["ppt2pdf_main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["pptx", "lxml", "PIL", "numpy", "matplotlib", "scipy", "pandas", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name="PPT2PDF", console=False, upx=False, runtime_tmpdir=None)
