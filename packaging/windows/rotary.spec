# PyInstaller recipe for Rotary.exe. From the repo root, after get-tools.ps1:
#   pyinstaller --noconfirm packaging/windows/rotary.spec
# Produces dist/Rotary/ (Rotary.exe plus its _internal folder).
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

here = Path(SPECPATH)

datas = collect_data_files("rotary")  # the web interface
datas += [(str(exe), "tools") for exe in (here / "tools").glob("*.exe")]  # FFmpeg, Pandoc
binaries = []
hiddenimports = []
# Packages with native libraries or plugins that static analysis can miss.
for package in ("pillow_heif", "pypdfium2", "pypdfium2_raw"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    [str(here / "launch.py")],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    # The signing server's libraries: the desktop app never imports them.
    excludes=["tkinter", "pytest", "_pytest", "playwright", "flask", "werkzeug", "jinja2", "waitress", "pyhanko", "pyhanko_certvalidator"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Rotary",
    icon=str(here.parent / "rotary.ico"),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Rotary", upx=False)
