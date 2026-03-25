from __future__ import annotations

import os
import platform
from pathlib import Path
from shutil import which

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent

IS_WINDOWS = os.name == "nt"
IN_WSL = bool(os.getenv("WSL_DISTRO_NAME")) or "microsoft" in platform.release().lower()
IS_CONTAINER_LAYOUT = APP_DIR.as_posix() == "/app"


def _path_from_env(env_name: str, default: Path) -> Path:
    value = os.getenv(env_name)
    return Path(value).expanduser().resolve() if value else default.resolve()


DEFAULT_DATA_DIR = PROJECT_DIR / ".standalone"
IMG_DIR_NAME = os.getenv("IMG_DIR", "img").strip("/\\") or "img"

DATA_DIR = _path_from_env("WB_DATA_DIR", Path("/") if IS_CONTAINER_LAYOUT else DEFAULT_DATA_DIR)
RUNTIME_DIR = _path_from_env(
    "WB_RUNTIME_DIR",
    Path("/tmp/wyze-bridge") if IS_CONTAINER_LAYOUT else DATA_DIR / "runtime",
)
BIN_DIR = _path_from_env("WB_BIN_DIR", APP_DIR if IS_CONTAINER_LAYOUT else DATA_DIR / "bin")
TOKEN_DIR = _path_from_env("WB_TOKEN_DIR", Path("/tokens") if IS_CONTAINER_LAYOUT else DATA_DIR / "tokens")
IMG_DIR = _path_from_env("WB_IMG_DIR", Path("/") / IMG_DIR_NAME if IS_CONTAINER_LAYOUT else DATA_DIR / IMG_DIR_NAME)
PIPE_DIR = _path_from_env("WB_PIPE_DIR", Path("/tmp") if IS_CONTAINER_LAYOUT else RUNTIME_DIR / "pipes")
SSL_DIR = _path_from_env("WB_SSL_DIR", Path("/ssl") if IS_CONTAINER_LAYOUT else DATA_DIR / "ssl")
LOG_DIR = _path_from_env("WB_LOG_DIR", Path("/logs") if IS_CONTAINER_LAYOUT else DATA_DIR / "logs")
ANALYSIS_DIR = _path_from_env("WB_ANALYSIS_DIR", Path("/analysis") if IS_CONTAINER_LAYOUT else DATA_DIR / "analysis")
MTX_CONFIG = _path_from_env("WB_MTX_CONFIG", APP_DIR / "mediamtx.yml" if IS_CONTAINER_LAYOUT else RUNTIME_DIR / "mediamtx.yml")
MTX_EVENT_FILE = _path_from_env(
    "WB_EVENT_FILE",
    Path("/tmp/mtx_event") if IS_CONTAINER_LAYOUT else RUNTIME_DIR / "mtx_event.log",
)
BUILD_DATE_FILE = _path_from_env("WB_BUILD_DATE_FILE", Path("/.build_date") if IS_CONTAINER_LAYOUT else DATA_DIR / ".build_date")
MTX_TAG_FILE = _path_from_env("WB_MTX_TAG_FILE", Path("/MTX_TAG") if IS_CONTAINER_LAYOUT else DATA_DIR / "MTX_TAG")


def ensure_runtime_dirs() -> None:
    for path in {DATA_DIR, RUNTIME_DIR, BIN_DIR, TOKEN_DIR, IMG_DIR, PIPE_DIR, SSL_DIR, LOG_DIR, ANALYSIS_DIR}:
        path.mkdir(parents=True, exist_ok=True)


def dir_string(path: Path) -> str:
    return f"{path.as_posix().rstrip('/')}/"


def exe_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def resolve_binary(name: str, env_name: str) -> str:
    if configured := os.getenv(env_name):
        return configured
    bundled = BIN_DIR / exe_name(name)
    if bundled.is_file():
        return str(bundled)
    return which(name) or name


FFMPEG_BIN = resolve_binary("ffmpeg", "WB_FFMPEG_BIN")
MEDIAMTX_BIN = resolve_binary("mediamtx", "WB_MEDIAMTX_BIN")


def audio_pipe_path(name: str, pipe_type: str = "audio") -> Path:
    ensure_runtime_dirs()
    return PIPE_DIR / f"{name}_{pipe_type}.pipe"


def bundled_iotc_library() -> str:
    for env_name in ("WB_IOTC_LIB", "IOTC_LIBRARY"):
        if configured := os.getenv(env_name):
            return configured

    if IS_WINDOWS and not IN_WSL:
        raise OSError(
            "Native Windows is not supported by the bundled Wyze/TUTK library. "
            "Run the standalone app inside Linux or WSL, or set WB_IOTC_LIB to a compatible library."
        )

    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "lib.amd64",
        "amd64": "lib.amd64",
        "aarch64": "lib.arm64",
        "arm64": "lib.arm64",
        "armv7l": "lib.arm",
        "armv6l": "lib.arm",
        "arm": "lib.arm",
    }
    lib_name = arch_map.get(machine)
    if not lib_name:
        raise OSError(f"Unsupported architecture for bundled IOTC library: {machine}")

    lib_path = APP_DIR / "lib" / lib_name
    if not lib_path.is_file():
        raise FileNotFoundError(f"Bundled IOTC library not found: {lib_path}")

    return str(lib_path)
