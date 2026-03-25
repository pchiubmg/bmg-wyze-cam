"""
This module handles stream and client events from MediaMTX.
"""

from pathlib import Path

from wyze_runtime import MTX_EVENT_FILE, ensure_runtime_dirs
from wyzebridge.logging import logger
from wyzebridge.mqtt import update_mqtt_state


class RtspEvent:
    """
    Reads appended MediaMTX events from a local runtime file.
    """

    EVENT_FILE = Path(MTX_EVENT_FILE)
    __slots__ = "streams", "buf", "position"

    def __init__(self, streams):
        self.streams = streams
        self.buf: str = ""
        self.position = 0
        self._ensure_file()
        self.position = self.EVENT_FILE.stat().st_size

    def _ensure_file(self):
        ensure_runtime_dirs()
        self.EVENT_FILE.touch(exist_ok=True)

    def read(self, timeout: int = 1):
        self._ensure_file()
        try:
            size = self.EVENT_FILE.stat().st_size
            if size < self.position:
                self.position = 0
            if size == self.position:
                return
            with self.EVENT_FILE.open("r", encoding="utf-8", errors="ignore") as event_log:
                event_log.seek(self.position)
                data = event_log.read()
                self.position = event_log.tell()
            if data:
                self.process_data(data)
        except OSError as ex:
            logger.error(ex)

    def process_data(self, data):
        messages = data.split("!")
        if self.buf:
            messages[0] = self.buf + messages[0]
            self.buf = ""
        for msg in messages[:-1]:
            self.log_event(msg.strip())

        self.buf = messages[-1].strip()

    def log_event(self, event_data: str):
        try:
            uri, event = event_data.split(",")
        except ValueError:
            logger.error(f"Error parsing {event_data=}")
            return

        event = event.lower().strip()

        if event == "start":
            self.streams.get(uri).start()
        elif event == "stop":
            self.streams.get(uri).stop()
        elif event in {"read", "unread"}:
            read_event(uri, event)
        elif event in {"ready", "notready"}:
            if event == "notready":
                self.streams.get(uri).stop()
            ready_event(uri, event)


def read_event(camera: str, status: str):
    msg = f"ðŸ“• Client stopped reading from {camera}"
    if status == "read":
        msg = f"ðŸ“– New client reading from {camera}"
    logger.info(msg)


def ready_event(camera: str, status: str):
    msg = f"âŒ '/{camera}' stream is down"
    state = "disconnected"
    if status == "ready":
        msg = f"âœ… '/{camera} stream is UP! (3/3)"
        state = "online"

    update_mqtt_state(camera, state)
    logger.info(msg)
