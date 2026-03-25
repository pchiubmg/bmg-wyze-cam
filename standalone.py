from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
APP_DIR = REPO_ROOT / "app"
DATA_DIR = REPO_ROOT / ".standalone"
BIN_DIR = DATA_DIR / "bin"
RUNTIME_DIR = DATA_DIR / "runtime"
TOKEN_DIR = DATA_DIR / "tokens"
IMG_DIR = DATA_DIR / "img"
PIPE_DIR = RUNTIME_DIR / "pipes"
SSL_DIR = DATA_DIR / "ssl"
LOG_DIR = DATA_DIR / "logs"
BUILD_DATE_FILE = DATA_DIR / ".build_date"
MTX_TAG_FILE = DATA_DIR / "MTX_TAG"
MTX_CONFIG = RUNTIME_DIR / "mediamtx.yml"
MTX_EVENT_FILE = RUNTIME_DIR / "mtx_event.log"
VENV_DIR = DATA_DIR / ".venv"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bmg-wyze-cam without Docker.")
    parser.add_argument("command", nargs="?", default="run", choices={"bootstrap", "doctor", "run"})
    parser.add_argument("--force-download", action="store_true", help="Re-download MediaMTX and ffmpeg.")
    args = parser.parse_args()

    if args.command == "doctor":
        return doctor()

    ensure_supported_runtime()
    bootstrap(force_download=args.force_download)

    if args.command == "bootstrap":
        return 0

    return run_app()


def ensure_supported_runtime() -> None:
    if platform.system().lower() != "linux":
        raise SystemExit(
            "Native Windows is not supported by the bundled Wyze/TUTK library. "
            "Use WSL/Linux for the standalone app."
        )


def doctor() -> int:
    print(f"platform={platform.system()} {platform.machine()}")
    print(f"repo={REPO_ROOT}")
    print(f"data={DATA_DIR}")
    print(f"python={sys.executable}")
    print(f"wsl={bool(os.getenv('WSL_DISTRO_NAME'))}")
    if platform.system().lower() != "linux":
        print("status=unsupported-native-windows")
        print("hint=Use WSL/Linux for the standalone app.")
    else:
        print("status=supported")
    return 0


def bootstrap(force_download: bool = False) -> None:
    ensure_dirs()
    write_metadata()
    ensure_venv()
    install_requirements()
    ensure_mediamtx(force_download)
    ensure_ffmpeg(force_download)
    MTX_CONFIG.touch(exist_ok=True)
    MTX_EVENT_FILE.touch(exist_ok=True)


def ensure_dirs() -> None:
    for path in {BIN_DIR, RUNTIME_DIR, TOKEN_DIR, IMG_DIR, PIPE_DIR, SSL_DIR, LOG_DIR}:
        path.mkdir(parents=True, exist_ok=True)


def write_metadata() -> None:
    BUILD_DATE_FILE.write_text(
        f"BUILD_DATE={datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )
    MTX_TAG_FILE.write_text(f"{load_app_env().get('MTX_TAG', '')}\n", encoding="utf-8")


def ensure_venv() -> None:
    if venv_python().is_file():
        return
    subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)


def install_requirements() -> None:
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
    )
    subprocess.run(
        [str(venv_python()), "-m", "pip", "install", "-r", str(APP_DIR / "requirements.txt")],
        check=True,
    )


def ensure_mediamtx(force_download: bool) -> None:
    destination = BIN_DIR / "mediamtx"
    if destination.is_file() and not force_download:
        return
    env = load_app_env()
    version = env["MTX_TAG"]
    arch = linux_arch()
    url = (
        "https://github.com/bluenviron/mediamtx/releases/download/"
        f"v{version}/mediamtx_v{version}_linux_{arch}.tar.gz"
    )
    download_binary(url, destination, wanted_name="mediamtx")


def ensure_ffmpeg(force_download: bool) -> None:
    destination = BIN_DIR / "ffmpeg"
    if destination.is_file() and not force_download:
        return
    arch = ffmpeg_arch()
    url = (
        "https://github.com/homebridge/ffmpeg-for-homebridge/releases/latest/download/"
        f"ffmpeg-alpine-{arch}.tar.gz"
    )
    download_binary(url, destination, wanted_name="ffmpeg")


def linux_arch() -> str:
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64v8",
        "arm64": "arm64v8",
        "armv7l": "armv7",
        "armv6l": "armv7",
        "arm": "armv7",
    }
    machine = platform.machine().lower()
    if machine not in arch_map:
        raise SystemExit(f"Unsupported Linux architecture for MediaMTX: {machine}")
    return arch_map[machine]


def ffmpeg_arch() -> str:
    arch_map = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "armv7l": "arm32v7",
        "armv6l": "arm32v7",
        "arm": "arm32v7",
    }
    machine = platform.machine().lower()
    if machine not in arch_map:
        raise SystemExit(f"Unsupported Linux architecture for ffmpeg: {machine}")
    return arch_map[machine]


def download_binary(url: str, destination: Path, wanted_name: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        archive = temp_root / Path(urllib.parse.urlparse(url).path).name
        print(f"Downloading {wanted_name} from {url}")
        urllib.request.urlretrieve(url, archive)
        extract_binary(archive, destination, wanted_name)
        destination.chmod(destination.stat().st_mode | 0o111)


def extract_binary(archive: Path, destination: Path, wanted_name: str) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            member = next(
                item for item in zipped.namelist() if Path(item).name == wanted_name
            )
            with zipped.open(member) as src, open(destination, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return

    with tarfile.open(archive, "r:*") as tar:
        member = next(item for item in tar.getmembers() if Path(item.name).name == wanted_name)
        extracted = tar.extractfile(member)
        if extracted is None:
            raise RuntimeError(f"Could not extract {wanted_name} from {archive}")
        with extracted, open(destination, "wb") as dst:
            shutil.copyfileobj(extracted, dst)


def load_app_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (APP_DIR / ".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def venv_python() -> Path:
    return VENV_DIR / "bin" / "python"


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath = [str(APP_DIR)]
    if existing := env.get("PYTHONPATH"):
        pythonpath.append(existing)
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "PYTHONUNBUFFERED": "1",
            "WB_DATA_DIR": str(DATA_DIR),
            "WB_RUNTIME_DIR": str(RUNTIME_DIR),
            "WB_BIN_DIR": str(BIN_DIR),
            "WB_TOKEN_DIR": str(TOKEN_DIR),
            "WB_IMG_DIR": str(IMG_DIR),
            "WB_PIPE_DIR": str(PIPE_DIR),
            "WB_SSL_DIR": str(SSL_DIR),
            "WB_LOG_DIR": str(LOG_DIR),
            "WB_BUILD_DATE_FILE": str(BUILD_DATE_FILE),
            "WB_MTX_TAG_FILE": str(MTX_TAG_FILE),
            "WB_MTX_CONFIG": str(MTX_CONFIG),
            "WB_EVENT_FILE": str(MTX_EVENT_FILE),
            "WB_FFMPEG_BIN": str(BIN_DIR / "ffmpeg"),
            "WB_MEDIAMTX_BIN": str(BIN_DIR / "mediamtx"),
        }
    )
    return env


def run_app() -> int:
    process = subprocess.run(
        [str(venv_python()), str(APP_DIR / "frontend.py")],
        env=runtime_env(),
    )
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
