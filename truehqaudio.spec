# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for True HQ Audio (one-folder build).

The one-folder layout is deliberate: Qt loads plugins from disk, and the native
audio DLLs (libsndfile, ffmpeg) must remain replaceable by the user without
rebuilding the executable.  Inno Setup packs the whole tree into a single
installer, so the end user still gets one file.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve()
RESOURCES = ROOT / "resources"
ICON = RESOURCES / "icons" / "truehq.ico"
VERSION_FILE = ROOT / "installer" / "version_info.txt"

datas = [
    (str(RESOURCES), "resources"),
]
for extra in ("README.md", "LICENSE"):
    candidate = ROOT / extra
    if candidate.exists():
        datas.append((str(candidate), "."))

hiddenimports = [
    "sounddevice",
    "numpy",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "src.core.decoders.wav",
    "src.core.decoders.aiff",
    "src.core.decoders.dsf",
    "src.core.decoders.dff",
    "src.core.decoders.sndfile_backend",
    "src.core.decoders.ffmpeg_backend",
]

excludes = [
    "tkinter",
    "unittest",
    "pytest",
    "pydoc",
    "doctest",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "notebook",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtMultimedia",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtSerialPort",
    "PySide6.QtSql",
    "PySide6.QtTest",
]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

# The sounddevice wheel ships every portaudio build it supports (macOS, 32-bit
# x86, arm64).  On Windows x64 sounddevice only ever loads libportaudio64bit.dll
# -- or libportaudio64bit-asio.dll when SD_ENABLE_ASIO is set -- so drop the
# rest before COLLECT; they only bloat the installer and the portable archive.
_PORTAUDIO_KEEP = {
    "libportaudio64bit.dll",
    "libportaudio64bit-asio.dll",
    "README.md",  # attribution/license notes shipped by the portaudio project
}


def _prune_foreign_portaudio(toc):
    kept = []
    for entry in toc:
        dest = entry[0].replace("\\", "/")
        if "portaudio-binaries" in dest and Path(dest).name not in _PORTAUDIO_KEEP:
            continue
        kept.append(entry)
    return kept


a.datas = _prune_foreign_portaudio(a.datas)
a.binaries = _prune_foreign_portaudio(a.binaries)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TrueHQAudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TrueHQAudio",
)
