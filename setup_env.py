"""True HQ Audio — self-bootstrapping environment installer.

The script is intentionally dependency-free (standard library only) so it can
run with a bare Python interpreter:

    python setup_env.py                 # full bootstrap (venv + deps + natives)
    python setup_env.py --check         # verify an existing installation
    python setup_env.py --install-native
    python setup_env.py --repair        # recreate the virtual environment
    python setup_env.py --run           # launch the application afterwards
    python setup_env.py --run --realtime

What it does
------------
1. Verifies the interpreter is CPython 3.15.2 on Windows x64.
2. Creates ``.venv`` next to this file and installs ``requirements.txt``.
3. Downloads the native audio binaries that Python wheels do not ship:
   ``libsndfile-1.dll`` (FLAC decoding) and ``ffmpeg``/``ffprobe`` (fallback
   decoder).  Every download is hashed with SHA-256 and recorded in
   ``resources/native/manifest.json`` so later runs can verify integrity.
4. Prints the exact command to launch the player.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
NATIVE_DIR = ROOT / "resources" / "native"
MANIFEST_PATH = NATIVE_DIR / "manifest.json"
REQUIREMENTS = ROOT / "requirements.txt"
TARGET_PYTHON = (3, 15, 2)
TARGET_ARCH = "64bit"
USER_AGENT = "TrueHQAudio-Setup/1.0 (+https://truehq.audio)"

LIBSNDFILE_URLS = (
    "https://github.com/libsndfile/libsndfile/releases/download/1.2.2/libsndfile-1.2.2-windows-x64.zip",
    "https://github.com/libsndfile/libsndfile/releases/download/1.2.2/libsndfile-1.2.2-win64.zip",
    "https://github.com/libsndfile/libsndfile/releases/latest/download/libsndfile-1.2.2-windows-x64.zip",
)

FFMPEG_URLS = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
)

DOWNLOAD_TIMEOUT = 120
CHUNK = 1 << 16


class SetupError(RuntimeError):
    pass


# --------------------------------------------------------------------------- output
class Console:
    RESET = "\x1b[0m"
    DIM = "\x1b[38;5;244m"
    OK = "\x1b[38;5;42m"
    WARN = "\x1b[38;5;214m"
    ERR = "\x1b[38;5;203m"
    STEP = "\x1b[1;38;5;45m"

    def __init__(self) -> None:
        self.colour = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

    def _paint(self, text: str, colour: str) -> str:
        return f"{colour}{text}{self.RESET}" if self.colour else text

    def step(self, text: str) -> None:
        print(self._paint(f"\n== {text}", self.STEP))

    def ok(self, text: str) -> None:
        print(self._paint(f"   OK   {text}", self.OK))

    def info(self, text: str) -> None:
        print(self._paint(f"   ..   {text}", self.DIM))

    def warn(self, text: str) -> None:
        print(self._paint(f"   WARN {text}", self.WARN))

    def error(self, text: str) -> None:
        print(self._paint(f"   FAIL {text}", self.ERR))


CONSOLE = Console()


def python_version_text() -> str:
    return ".".join(str(part) for part in sys.version_info[:3])


def verify_interpreter(strict: bool = True) -> None:
    CONSOLE.step("Verifying the interpreter")
    CONSOLE.info(f"python {python_version_text()} · {platform.architecture()[0]} · {platform.platform()}")
    if not sys.platform.startswith("win"):
        raise SetupError("True HQ Audio targets Windows only (x64).")
    if sys.version_info[:2] != TARGET_PYTHON[:2]:
        message = (
            f"Python {TARGET_PYTHON[0]}.{TARGET_PYTHON[1]} is required, found {python_version_text()}. "
            f"Install it from https://www.python.org/downloads/release/python-{TARGET_PYTHON[0]}{TARGET_PYTHON[1]}2/"
        )
        if strict:
            raise SetupError(message)
        CONSOLE.warn(message)
    elif sys.version_info[:3] != TARGET_PYTHON:
        CONSOLE.warn(
            f"Python {python_version_text()} works, but this project is validated against "
            f"{TARGET_PYTHON[0]}.{TARGET_PYTHON[1]}.{TARGET_PYTHON[2]}."
        )
    else:
        CONSOLE.ok(f"Python {python_version_text()} (expected version)")
    if platform.architecture()[0] != TARGET_ARCH:
        raise SetupError("A 64-bit interpreter is required (ASIO and WASAPI drivers are x64).")
    CONSOLE.ok("Windows x64 interpreter confirmed")


def venv_python() -> Path:
    return VENV_DIR / "Scripts" / "python.exe"


def venv_exists() -> bool:
    return venv_python().exists()


def run(command: List[str], description: str, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
    CONSOLE.info(description)
    result = subprocess.run(command, cwd=str(cwd or ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-8:])
        if check:
            raise SetupError(f"{description} failed (exit {result.returncode}).\n{tail}")
        CONSOLE.warn(f"{description} failed (exit {result.returncode})")
        if tail:
            print(tail)
    return result


def create_venv(repair: bool = False) -> None:
    CONSOLE.step("Preparing the isolated environment")
    if repair and VENV_DIR.exists():
        CONSOLE.info("removing the previous virtual environment")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    if venv_exists():
        CONSOLE.ok(f"reusing {VENV_DIR}")
        return
    run([sys.executable, "-m", "venv", str(VENV_DIR)], "creating .venv")
    if not venv_exists():
        raise SetupError("virtual environment creation failed")
    CONSOLE.ok(f"created {VENV_DIR}")


def pip_install(upgrade: bool = False, dev: bool = False) -> None:
    CONSOLE.step("Installing Python packages")
    python = str(venv_python())
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip", "setuptools", "wheel"], "upgrading pip")
    requirements = ROOT / ("requirements-dev.txt" if dev else "requirements.txt")
    if not requirements.exists():
        raise SetupError(f"missing {requirements.name}")
    result = run(
        [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(requirements)],
        f"installing {requirements.name}",
    )
    CONSOLE.ok("Python packages installed")
    if result.stdout:
        for line in result.stdout.strip().splitlines()[-6:]:
            if line.strip():
                CONSOLE.info(line.strip())


def import_check() -> None:
    CONSOLE.step("Verifying imports")
    scripts = (
        ("numpy", "import numpy; print(numpy.__version__)"),
        ("sounddevice", "import sounddevice; print(sounddevice.__version__)"),
        ("PySide6", "import PySide6; print(PySide6.__version__)"),
    )
    for name, snippet in scripts:
        result = run([str(venv_python()), "-c", snippet], f"checking {name}", check=False)
        if result.returncode == 0:
            CONSOLE.ok(f"{name} {result.stdout.strip()}")
        else:
            CONSOLE.warn(f"{name} is not importable (phonon backend or wheel missing)")
    result = run([str(venv_python()), "-c", "import sounddevice; print(len(sounddevice.query_devices()))"], "enumerating PortAudio devices", check=False)
    if result.returncode == 0:
        CONSOLE.ok(f"PortAudio reports {result.stdout.strip()} device(s)")
    else:
        CONSOLE.warn("PortAudio could not enumerate devices; check your audio drivers")


# --------------------------------------------------------------------------- native
def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest() -> Dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_manifest(manifest: Dict[str, dict]) -> None:
    NATIVE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = MANIFEST_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(MANIFEST_PATH)


def download(url: str, destination: Path) -> Path:
    CONSOLE.info(f"downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response, open(destination, "wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if total and sys.stdout.isatty():
                    percent = written * 100 // total
                    sys.stdout.write(f"\r   ..   {percent:3d}%  {written / 1048576:.1f} MiB")
                    sys.stdout.flush()
        if sys.stdout.isatty():
            sys.stdout.write("\r")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        destination.unlink(missing_ok=True)
        raise SetupError(f"download failed: {exc}") from exc
    CONSOLE.ok(f"downloaded {destination.name} ({written / 1048576:.1f} MiB)")
    return destination


def extract_member(archive: Path, predicate, destination_dir: Path) -> List[Path]:
    extracted: List[Path] = []
    destination_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name
            if not predicate(name):
                continue
            target = destination_dir / name
            with bundle.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted.append(target)
    return extracted


def vendor_libsndfile_from_soundfile() -> Optional[Path]:
    """Fallback: extract libsndfile from the ``soundfile`` wheel.

    The PyPI wheel for ``soundfile`` embeds a 64-bit libsndfile build on
    Windows, which makes it a reliable offline-friendly source when GitHub
    release downloads are blocked by a proxy.
    """

    python = str(venv_python()) if venv_exists() else sys.executable
    probe = (
        "import pathlib, soundfile; "
        "print(pathlib.Path(soundfile.__file__).resolve().parent)"
    )
    result = run([python, "-c", probe], "looking for a bundled libsndfile", check=False)
    if result.returncode != 0:
        CONSOLE.info("installing the soundfile wheel as a DLL source")
        run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "soundfile>=0.13"],
            "installing soundfile",
            check=False,
        )
        result = run([python, "-c", probe], "looking for a bundled libsndfile", check=False)
    if result.returncode != 0:
        return None
    package_root = Path(result.stdout.strip().splitlines()[-1])
    candidates = [
        path
        for path in package_root.rglob("*.dll")
        if "sndfile" in path.name.lower() and "32" not in path.name.lower()
    ]
    if not candidates:
        candidates = [path for path in package_root.rglob("*sndfile*.dll")]
    for candidate in sorted(candidates, key=lambda path: path.stat().st_size, reverse=True):
        target = NATIVE_DIR / "libsndfile-1.dll"
        try:
            shutil.copyfile(candidate, target)
        except OSError:
            continue
        if validate_libsndfile(target):
            return target
        target.unlink(missing_ok=True)
    return None


def validate_libsndfile(path: Path) -> bool:
    try:
        library = ctypes.WinDLL(str(path))
        library.sf_version_string.restype = ctypes.c_char_p
        version = library.sf_version_string()
        if version:
            CONSOLE.ok(f"libsndfile reports {version.decode('latin-1', 'replace')}")
        return True
    except (OSError, AttributeError) as exc:
        CONSOLE.warn(f"libsndfile validation failed: {exc}")
        return False


def validate_tool(path: Path, argument: str = "-version") -> bool:
    try:
        result = subprocess.run([str(path), argument], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        CONSOLE.warn(f"{path.name} could not start: {exc}")
        return False
    first = (result.stdout or result.stderr or "").strip().splitlines()
    if result.returncode == 0 and first:
        CONSOLE.ok(f"{path.name}: {first[0][:70]}")
        return True
    CONSOLE.warn(f"{path.name} returned {result.returncode}")
    return False


def install_native(force: bool = False, refresh_manifest: bool = False) -> None:
    CONSOLE.step("Installing native audio binaries")
    NATIVE_DIR.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    if refresh_manifest and MANIFEST_PATH.exists():
        CONSOLE.info("manifest refresh requested")
        manifest = {}

    libsndfile = NATIVE_DIR / "libsndfile-1.dll"
    if libsndfile.exists() and not force:
        recorded = manifest.get("libsndfile-1.dll")
        actual = sha256_of(libsndfile)
        if recorded and recorded.get("sha256") != actual:
            CONSOLE.warn("libsndfile-1.dll does not match the recorded hash (keeping the local copy)")
        else:
            CONSOLE.ok("libsndfile-1.dll already present")
        if validate_libsndfile(libsndfile):
            manifest.setdefault("libsndfile-1.dll", {"sha256": actual, "source": "local"})
    else:
        # Preferred source: the soundfile wheel embeds a signed, versioned
        # libsndfile build and is served by PyPI, which works behind most
        # corporate proxies where github.com release assets are blocked.
        vendored = vendor_libsndfile_from_soundfile()
        if vendored is not None:
            manifest["libsndfile-1.dll"] = {
                "sha256": sha256_of(vendored),
                "size": vendored.stat().st_size,
                "source": "soundfile wheel",
            }
            CONSOLE.ok("libsndfile installed from the soundfile wheel")
        else:
            with tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "libsndfile.zip"
                installed = False
                for url in LIBSNDFILE_URLS:
                    try:
                        download(url, archive)
                        extracted = extract_member(
                            archive, lambda name: name.lower() == "libsndfile-1.dll", NATIVE_DIR
                        )
                        if not extracted:
                            raise SetupError("archive does not contain libsndfile-1.dll")
                        if validate_libsndfile(extracted[0]):
                            manifest["libsndfile-1.dll"] = {
                                "sha256": sha256_of(extracted[0]),
                                "size": extracted[0].stat().st_size,
                                "source": url,
                            }
                            installed = True
                            break
                        raise SetupError("downloaded libsndfile-1.dll is not loadable")
                    except SetupError as exc:
                        CONSOLE.warn(str(exc))
                        continue
                if not installed:
                    CONSOLE.warn(
                        "libsndfile could not be installed automatically.\n"
                        "            Download 'libsndfile-1.dll' (x64) manually and copy it to:\n"
                        f"            {NATIVE_DIR}\n"
                        "            FLAC playback will otherwise fall back to ffmpeg."
                    )

    tools = {"ffmpeg.exe": None, "ffprobe.exe": None}
    missing = [name for name in tools if not (NATIVE_DIR / name).exists() or force]
    if missing:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "ffmpeg.zip"
            installed = False
            for url in FFMPEG_URLS:
                try:
                    download(url, archive)
                    extracted = extract_member(
                        archive,
                        lambda name: name.lower() in ("ffmpeg.exe", "ffprobe.exe"),
                        NATIVE_DIR,
                    )
                    if len(extracted) < 2:
                        raise SetupError("archive does not contain ffmpeg.exe and ffprobe.exe")
                    installed = all(validate_tool(NATIVE_DIR / name) for name in ("ffmpeg.exe", "ffprobe.exe"))
                    if installed:
                        for name in ("ffmpeg.exe", "ffprobe.exe"):
                            path = NATIVE_DIR / name
                            manifest[name] = {
                                "sha256": sha256_of(path),
                                "size": path.stat().st_size,
                                "source": url,
                            }
                        break
                except SetupError as exc:
                    CONSOLE.warn(str(exc))
                    continue
            if not installed:
                CONSOLE.warn(
                    "ffmpeg/ffprobe could not be installed automatically. "
                    "The native decoders still handle WAV/AIFF/DSF/DFF; copy ffmpeg.exe and "
                    f"ffprobe.exe into {NATIVE_DIR} for extended format support."
                )
    else:
        CONSOLE.ok("ffmpeg/ffprobe already present")

    save_manifest(manifest)
    CONSOLE.ok(f"native manifest written to {MANIFEST_PATH}")


def check_native() -> None:
    CONSOLE.step("Verifying native binaries")
    libsndfile = NATIVE_DIR / "libsndfile-1.dll"
    if libsndfile.exists():
        validate_libsndfile(libsndfile)
    else:
        CONSOLE.warn(f"missing {libsndfile}")
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        path = NATIVE_DIR / name
        if path.exists():
            validate_tool(path)
        else:
            CONSOLE.warn(f"missing {path}")


def check_project() -> int:
    CONSOLE.step("Verifying the project")
    python = str(venv_python()) if venv_exists() else sys.executable
    result = run([python, "main.py", "--diagnostics"], "running self-diagnostics", check=False)
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        CONSOLE.error("self-diagnostics failed")
        if result.stderr:
            print(result.stderr.rstrip())
        return result.returncode
    CONSOLE.ok("self-diagnostics passed")
    return 0


def launch(extra: Iterable[str] = ()) -> int:
    python = str(venv_python()) if venv_exists() else sys.executable
    command = [python, str(ROOT / "main.py"), *extra]
    CONSOLE.info("launching " + " ".join(command))
    return subprocess.call(command, cwd=str(ROOT))


def full_bootstrap(repair: bool, dev: bool, skip_native: bool) -> None:
    verify_interpreter()
    create_venv(repair=repair)
    pip_install(dev=dev)
    import_check()
    if not skip_native:
        install_native(force=False)
        check_native()
    CONSOLE.step("Done")
    CONSOLE.ok(f"environment ready at {VENV_DIR}")
    CONSOLE.info(f"start the player with:  {venv_python()} main.py")
    CONSOLE.info("or run:                 python setup_env.py --run")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="True HQ Audio environment bootstrap")
    parser.add_argument("--check", action="store_true", help="verify an existing installation and exit")
    parser.add_argument("--repair", action="store_true", help="delete and recreate the virtual environment")
    parser.add_argument("--dev", action="store_true", help="install requirements-dev.txt (pytest, pyinstaller)")
    parser.add_argument("--install-native", action="store_true", help="only install libsndfile/ffmpeg")
    parser.add_argument("--force-native", action="store_true", help="re-download native binaries")
    parser.add_argument("--refresh-manifest", action="store_true", help="forget recorded native hashes")
    parser.add_argument("--skip-native", action="store_true", help="skip the native binaries step")
    parser.add_argument("--run", action="store_true", help="launch the application when finished")
    parser.add_argument("--realtime", action="store_true", help="pass --realtime to the application")
    arguments = parser.parse_args(argv)

    try:
        if arguments.check:
            verify_interpreter(strict=False)
            check_native()
            return check_project()
        if arguments.install_native:
            verify_interpreter(strict=False)
            install_native(force=arguments.force_native, refresh_manifest=arguments.refresh_manifest)
            check_native()
            return 0
        full_bootstrap(
            repair=arguments.repair,
            dev=arguments.dev,
            skip_native=arguments.skip_native,
        )
        if arguments.run:
            extra = ["--realtime"] if arguments.realtime else []
            return launch(extra)
        return 0
    except SetupError as exc:
        CONSOLE.error(str(exc))
        return 1
    except KeyboardInterrupt:
        CONSOLE.warn("cancelled by the user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
