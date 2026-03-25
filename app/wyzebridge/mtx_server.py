import os
import shlex
from pathlib import Path
from subprocess import DEVNULL, Popen
from typing import Optional

import yaml
from wyze_runtime import MEDIAMTX_BIN, MTX_CONFIG, MTX_EVENT_FILE, SSL_DIR, ensure_runtime_dirs
from wyzebridge.bridge_utils import env_bool
from wyzebridge.logging import logger

RECORD_LENGTH = env_bool("RECORD_LENGTH", "60s")
RECORD_KEEP = env_bool("RECORD_KEEP", "0s")
rec_file = env_bool("RECORD_FILE_NAME", style="original").strip("/")
rec_path = env_bool("RECORD_PATH", "record/%path/%Y-%m-%d_%H-%M-%S", style="original")
RECORD_PATH = f"{Path('/') / Path(rec_path) / Path(rec_file)}".removesuffix(".mp4")


class MtxInterface:
    __slots__ = "data", "_modified"

    def __init__(self):
        self.data = {}
        self._modified = False

    def __enter__(self):
        self._load_config()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._modified:
            self._save_config()

    def _load_config(self):
        ensure_runtime_dirs()
        Path(MTX_CONFIG).touch(exist_ok=True)
        with open(MTX_CONFIG, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f) or {}

    def _save_config(self):
        with open(MTX_CONFIG, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f)

    def get(self, path: str):
        keys = path.split(".")
        current = self.data
        for key in keys:
            if current is None:
                return None
            current = current.get(key)
        return current

    def set(self, path: str, value):
        keys = path.split(".")
        current = self.data
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value
        self._modified = True

    def add(self, path: str, value):
        if not isinstance(value, list):
            value = [value]
        current = self.data.get(path)
        if isinstance(current, list):
            current.extend([item for item in value if item not in current])
        else:
            self.data[path] = value
        self._modified = True


class MtxServer:
    """Setup and interact with the backend mediamtx."""

    __slots__ = "sub_process"

    def __init__(self) -> None:
        self.sub_process: Optional[Popen] = None
        self._setup_path_defaults()

    def _setup_path_defaults(self):
        ensure_runtime_dirs()
        Path(MTX_EVENT_FILE).write_text("", encoding="utf-8")
        record_path = RECORD_PATH.format(cam_name="%path", CAM_NAME="%path")

        with MtxInterface() as mtx:
            mtx.set("paths", {})
            for event in {"Read", "Unread", "Ready", "NotReady"}:
                mtx.set(f"pathDefaults.runOn{event}", event_command(event))
            mtx.set("pathDefaults.runOnDemandStartTimeout", "30s")
            mtx.set("pathDefaults.runOnDemandCloseAfter", "60s")
            mtx.set("pathDefaults.recordPath", record_path)
            mtx.set("pathDefaults.recordSegmentDuration", RECORD_LENGTH)
            mtx.set("pathDefaults.recordDeleteAfter", RECORD_KEEP)

    def setup_auth(self, api: Optional[str], stream: Optional[str]):
        publisher = [
            {
                "ips": ["127.0.0.1"],
                "permissions": [{"action": "read"}, {"action": "publish"}],
            }
        ]
        with MtxInterface() as mtx:
            mtx.set("authInternalUsers", publisher)
            if api or not stream:
                client: dict = {"permissions": [{"action": "read"}]}
                if api:
                    client.update({"user": "wb", "pass": api})
                mtx.add("authInternalUsers", client)
            if stream:
                logger.info("[MTX] Custom stream auth enabled")
                for client in parse_auth(stream):
                    mtx.add("authInternalUsers", client)

    def add_path(self, uri: str, on_demand: bool = True):
        with MtxInterface() as mtx:
            if on_demand:
                mtx.set(f"paths.{uri}.runOnDemand", event_command("start"))
                mtx.set(f"paths.{uri}.runOnUnDemand", event_command("stop"))
            else:
                mtx.set(f"paths.{uri}", {})

    def add_source(self, uri: str, value: str):
        with MtxInterface() as mtx:
            mtx.set(f"paths.{uri}.source", value)

    def record(self, uri: str):
        record_path = RECORD_PATH.replace("%path", uri).format(
            cam_name=uri.lower(), CAM_NAME=uri.upper()
        )

        logger.info(f"[MTX] 📹 Will record {RECORD_LENGTH} clips to {record_path}.mp4")
        with MtxInterface() as mtx:
            mtx.set(f"paths.{uri}.record", True)
            mtx.set(f"paths.{uri}.recordPath", record_path)

    def start(self):
        if self.sub_process:
            return
        logger.info(f"[MTX] starting MediaMTX {os.getenv('MTX_TAG')}")
        self.sub_process = Popen([MEDIAMTX_BIN, str(MTX_CONFIG)])

    def stop(self):
        if not self.sub_process:
            return
        if self.sub_process.poll() is None:
            logger.info("[MTX] Stopping MediaMTX...")
            self.sub_process.kill()
            self.sub_process.communicate()
        self.sub_process = None

    def restart(self):
        self.stop()
        self.start()

    def health_check(self):
        if self.sub_process and self.sub_process.poll() is not None:
            logger.error(f"[MediaMTX] Process exited with {self.sub_process.poll()}")
            self.restart()

    def setup_webrtc(self, bridge_ip: Optional[str]):
        if not bridge_ip:
            logger.warning("SET WB_IP to allow WEBRTC connections.")
            return
        ips = bridge_ip.split(",")
        logger.debug(f"Using {' and '.join(ips)} for webrtc")
        with MtxInterface() as mtx:
            mtx.add("webrtcAdditionalHosts", ips)

    def setup_llhls(self, token_path: str = "/tokens/", hass: bool = False):
        logger.info("[MTX] Configuring LL-HLS")
        with MtxInterface() as mtx:
            mtx.set("hlsVariant", "lowLatency")
            mtx.set("hlsEncryption", True)
            if env_bool("mtx_hlsServerKey"):
                return

            key = "/ssl/privkey.pem"
            cert = "/ssl/fullchain.pem"
            if hass and Path(key).is_file() and Path(cert).is_file():
                logger.info(
                    "[MTX] 🔐 Using existing SSL certificate from Home Assistant"
                )
                mtx.set("hlsServerKey", key)
                mtx.set("hlsServerCert", cert)
                return

            cert_root = Path(token_path) if token_path else SSL_DIR
            cert_path = str(cert_root / "hls_server")
            generate_certificates(cert_path)
            mtx.set("hlsServerKey", f"{cert_path}.key")
            mtx.set("hlsServerCert", f"{cert_path}.crt")


def mtx_version() -> str:
    try:
        from wyze_runtime import MTX_TAG_FILE

        with open(MTX_TAG_FILE, "r", encoding="utf-8") as tag:
            return tag.read().strip()
    except FileNotFoundError:
        return ""


def generate_certificates(cert_path):
    if not Path(f"{cert_path}.key").is_file():
        logger.info("[MTX] 🔐 Generating key for LL-HLS")
        Popen(
            ["openssl", "genrsa", "-out", f"{cert_path}.key", "2048"],
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).wait()
    if not Path(f"{cert_path}.crt").is_file():
        logger.info("[MTX] 🔏 Generating certificate for LL-HLS")
        dns = os.getenv("SUBJECT_ALT_NAME")
        Popen(
            ["openssl", "req", "-new", "-x509", "-sha256"]
            + ["-key", f"{cert_path}.key"]
            + ["-subj", "/C=US/ST=WA/L=Kirkland/O=WYZE BRIDGE/CN=wyze-bridge"]
            + (["-addext", f"subjectAltName = DNS:{dns}"] if dns else [])
            + ["-out", f"{cert_path}.crt"]
            + ["-days", "3650"],
            stdout=DEVNULL,
            stderr=DEVNULL,
        ).wait()


def event_command(event: str) -> str:
    if os.name == "nt":
        event_path = str(MTX_EVENT_FILE).replace("/", "\\")
        return f'cmd /V:ON /C "(echo !MTX_PATH!,{event}!>>\\"{event_path}\\")"'

    event_path = shlex.quote(str(MTX_EVENT_FILE))
    return f'sh -c "printf \'%s,{event}!\' \\"$MTX_PATH\\" >> {event_path}"'


def parse_auth(auth: str) -> list[dict[str, str]]:
    entries = []
    for entry in auth.split("|"):
        creds, *endpoints = entry.split("@")
        if ":" not in creds:
            continue
        user, password, *ips = creds.split(":", 2)
        if ips:
            ips = ips[0].split(",")
        data = {"user": user or "any", "pass": password, "ips": ips, "permissions": []}
        if endpoints:
            paths = []
            for endpoint in endpoints[0].split(","):
                paths.append(endpoint)
                data["permissions"].append({"action": "read", "path": endpoint})
        else:
            paths = "all"
            data["permissions"].append({"action": "read"})
        logger.info(f"[MTX] Auth [{data['user']}:{data['pass']}] {paths=}")
        entries.append(data)
    return entries
