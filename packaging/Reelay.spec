# PyInstaller spec for Reelay (macOS .app and Windows .exe).
#
#   pyinstaller --noconfirm --clean packaging/Reelay.spec
#
# Build on the platform you're targeting: PyInstaller can't cross-compile.
# macOS output:   dist/Reelay.app
# Windows output: dist/Reelay/Reelay.exe

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent
APP_NAME = "Reelay"
sys.path.insert(0, str(ROOT))
from reelay import __version__  # noqa: E402

datas = [
    (str(ROOT / "packaging" / "assets" / "icon.png"), "packaging/assets"),
]
binaries = []
hiddenimports = []

# curl_cffi powers TikTok browser impersonation (native libs + data files).
for pkg in ("curl_cffi",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# yt-dlp ships its own PyInstaller hook; its JS challenge solver is data-only.
datas += collect_data_files("yt_dlp_ejs")

# keyring finds its OS backends through package metadata + submodules.
from PyInstaller.utils.hooks import collect_submodules, copy_metadata  # noqa: E402

hiddenimports += collect_submodules("keyring.backends")
datas += copy_metadata("keyring")

# Bundled ffmpeg (used when the user has no system ffmpeg).
import imageio_ffmpeg  # noqa: E402

binaries.append((imageio_ffmpeg.get_ffmpeg_exe(), "imageio_ffmpeg/binaries"))

a = Analysis(
    [str(ROOT / "run_app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "PySide6.QtWebEngineCore",
              "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "packaging" / "assets" / ("icon.icns" if sys.platform == "darwin" else "icon.ico"))

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    console=False,
    icon=icon,
    upx=False,
    argv_emulation=False,
    target_arch=None,  # build for the machine's own architecture
)

coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME, upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=icon,
        bundle_identifier="com.reelay.app",
        version=__version__,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            "LSApplicationCategoryType": "public.app-category.video",
        },
    )
