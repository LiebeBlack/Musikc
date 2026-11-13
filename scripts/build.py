"""Build the Windows executable and the Inno Setup installer.

    python scripts/build.py                # tests + exe + installer
    python scripts/build.py --skip-tests   # faster iteration
    python scripts/build.py --no-installer  # only dist/TrueHQAudio

Requires the .venv created by ``python setup_env.py --dev`` and Inno Setup 6
for the installer step (``choco install innosetup``).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_DIR = DIST / "TrueHQAudio"
INSTALLER_OUTPUT = ROOT / "installer" / "Output"
VERSION_FILE = ROOT / "src" / "config.py"


def read_version() -> str:
    for line in VERSION_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("APP_VERSION"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def run(command: list[str], description: str) -> None:
    print(f"\n>>> {description}\n    {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=str(ROOT), check=True)


def find_iscc() -> Path | None:
    candidates = [
        os.environ.get("INNO_SETUP_HOME", ""),
        r"C:\Program Files (x86)\Inno Setup 6",
        r"C:\Program Files\Inno Setup 6",
    ]
    for base in candidates:
        if not base:
            continue
        candidate = Path(base) / "ISCC.exe"
        if candidate.exists():
            return candidate
    located = shutil.which("iscc") or shutil.which("ISCC")
    return Path(located) if located else None


def ensure_native() -> None:
    native = ROOT / "resources" / "native"
    native.mkdir(parents=True, exist_ok=True)
    dll = native / "libsndfile-1.dll"
    if not dll.exists():
        print(
            "warning: libsndfile-1.dll is not bundled; FLAC will fall back to ffmpeg.\n"
            "         run: python setup_env.py --install-native"
        )
    if not (native / "ffmpeg.exe").exists():
        print("warning: ffmpeg.exe is not bundled; only native WAV/AIFF/DSF/DFF decoding will be available.")


def zip_portable(version: str) -> Path:
    target = DIST / f"TrueHQAudio-{version}-portable-x64.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in APP_DIR.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(APP_DIR))
    print(f"portable archive: {target} ({target.stat().st_size / 1048576:.1f} MiB)")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build True HQ Audio")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--no-installer", action="store_true")
    parser.add_argument("--no-clean", action="store_true")
    arguments = parser.parse_args(argv)
    version = read_version()
    python = sys.executable

    print(f"True HQ Audio build {version} with {python}")
    ensure_native()
    run([python, str(ROOT / "scripts" / "make_icon.py")], "rendering the application icon")
    run([python, str(ROOT / "scripts" / "export_stylesheet.py")], "exporting the QSS theme")

    if not arguments.skip_tests:
        run([python, "-m", "pytest", "-q", "tests"], "running the test suite")

    command = [
        python,
        "-m",
        "PyInstaller",
        str(ROOT / "truehqaudio.spec"),
        "--noconfirm",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]
    if not arguments.no_clean:
        command.append("--clean")
    run(command, "freezing the application with PyInstaller")

    if not (APP_DIR / "TrueHQAudio.exe").exists():
        print("error: PyInstaller did not produce dist/TrueHQAudio/TrueHQAudio.exe", file=sys.stderr)
        return 1
    zip_portable(version)

    if arguments.no_installer:
        return 0

    iscc = find_iscc()
    if iscc is None:
        print(
            "Inno Setup was not found. Install it with 'choco install innosetup' and re-run,\n"
            "or compile installer/truehqaudio.iss manually.",
            file=sys.stderr,
        )
        return 2
    INSTALLER_OUTPUT.mkdir(parents=True, exist_ok=True)
    run(
        [str(iscc), f"/DAppVersion={version}", str(ROOT / "installer" / "truehqaudio.iss")],
        "compiling the installer with Inno Setup",
    )
    produced = sorted(INSTALLER_OUTPUT.glob("*.exe"))
    for artifact in produced:
        print(f"installer: {artifact} ({artifact.stat().st_size / 1048576:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
