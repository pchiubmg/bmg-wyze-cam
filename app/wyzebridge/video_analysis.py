from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from wyze_runtime import ANALYSIS_DIR, APP_DIR, ensure_runtime_dirs
from wyzebridge.bridge_utils import env_bool
from wyzebridge.logging import logger

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
SUPPORTED_PROMPTS = [
    "Summarize this clip",
    "What bodyparts are tracked?",
    "Which bodypart moved the most?",
    "What is the average confidence?",
    "How visible was nose?",
    "When was tail visible?",
]


class VideoAnalysisService:
    __slots__ = (
        "enabled",
        "auto_analyze",
        "create_labeled_video",
        "project_config",
        "python_bin",
        "scan_interval",
        "min_file_age",
        "pcutoff",
        "recordings_root",
        "index_path",
        "runner_path",
        "lock",
        "index",
        "thread",
        "stop_event",
        "last_scan",
        "current_job",
    )

    def __init__(self) -> None:
        ensure_runtime_dirs()
        self.enabled = env_bool("DLC_ENABLED", style="bool")
        self.auto_analyze = bool(
            env_bool("DLC_AUTO_ANALYZE", "true", style="bool") if self.enabled else False
        )
        self.create_labeled_video = env_bool("DLC_CREATE_LABELED_VIDEO", style="bool")
        self.project_config = env_bool("DLC_PROJECT_CONFIG", style="original")
        self.python_bin = env_bool("DLC_PYTHON", sys.executable, style="original")
        self.scan_interval = max(env_bool("DLC_SCAN_INTERVAL", 60, style="int"), 5)
        self.min_file_age = max(env_bool("DLC_MIN_FILE_AGE", 30, style="int"), 0)
        self.pcutoff = max(min(float(env_bool("DLC_PCUTOFF", "0.6", style="float")), 1.0), 0.0)
        self.recordings_root = resolve_recordings_root()
        self.index_path = ANALYSIS_DIR / "index.json"
        self.runner_path = APP_DIR / "dlc_runner.py"
        self.lock = threading.Lock()
        self.index = {"clips": {}}
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.last_scan = 0.0
        self.current_job: dict[str, Any] | None = None
        self._load_index()

    def start(self) -> None:
        if self.thread or not self.enabled:
            return
        self.thread = threading.Thread(target=self._worker, name="video-analysis", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2)

    def overview(self) -> dict[str, Any]:
        with self.lock:
            clips = list(self.index["clips"].values())
            counts: dict[str, int] = {}
            for clip in clips:
                status = clip.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            return {
                "enabled": self.enabled,
                "configured": self.is_configured(),
                "auto_analyze": self.auto_analyze,
                "create_labeled_video": self.create_labeled_video,
                "project_config": self.project_config or None,
                "python_bin": self.python_bin,
                "recordings_root": str(self.recordings_root),
                "scan_interval": self.scan_interval,
                "min_file_age": self.min_file_age,
                "pcutoff": self.pcutoff,
                "current_job": self.current_job_summary(),
                "counts": counts,
                "supported_prompts": SUPPORTED_PROMPTS,
            }

    def list_clips(self, limit: int = 50) -> dict[str, Any]:
        self.trigger_scan()
        with self.lock:
            clips = sorted(
                (self._public_clip_data(clip) for clip in self.index["clips"].values()),
                key=lambda clip: clip.get("mtime", 0),
                reverse=True,
            )
            return {"clips": clips[:limit], "total": len(clips)}

    def get_clip(self, clip_id: str) -> dict[str, Any]:
        self.trigger_scan()
        with self.lock:
            if clip := self.index["clips"].get(clip_id):
                return self._public_clip_data(clip)
        return {"error": f"Clip [{clip_id}] not found"}

    def trigger_scan(self) -> dict[str, Any]:
        with self.lock:
            discovered = self._discover_clips_locked(force=True)
            self._save_index_locked()
        return {"status": "success", "discovered": discovered}

    def enqueue(self, clip_id: str, force: bool = False) -> dict[str, Any]:
        with self.lock:
            clip = self.index["clips"].get(clip_id)
            if not clip:
                return {"status": "error", "response": "clip not found"}
            if not self.enabled:
                return {"status": "error", "response": "video analysis is disabled"}
            if not self.is_configured():
                return {"status": "error", "response": "set DLC_PROJECT_CONFIG before analyzing"}
            clip["status"] = "queued"
            if force:
                clip["result"] = None
                clip["error"] = None
            clip["queued_at"] = time.time()
            self._save_index_locked()
            return {"status": "success", "clip_id": clip_id, "queued": True}

    def answer_prompt(self, clip_id: str, question: str) -> dict[str, Any]:
        if not question.strip():
            return {"status": "error", "response": "missing question"}

        with self.lock:
            clip = self.index["clips"].get(clip_id)
            if not clip:
                return {"status": "error", "response": "clip not found"}
            summary = self._load_summary_locked(clip)

        if not summary:
            return {"status": "error", "response": "clip has not been analyzed yet"}

        return {
            "status": "success",
            "clip_id": clip_id,
            "question": question,
            **answer_pose_prompt(question, summary),
        }

    def is_configured(self) -> bool:
        return bool(self.project_config and Path(self.project_config).is_file())

    def current_job_summary(self) -> dict[str, Any] | None:
        if not self.current_job:
            return None
        clip = self.index["clips"].get(self.current_job["clip_id"], {})
        return {
            "clip_id": self.current_job["clip_id"],
            "path": clip.get("path"),
            "started_at": clip.get("started_at"),
            "status": clip.get("status"),
        }

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                if time.time() - self.last_scan >= self.scan_interval:
                    self._discover_clips_locked(force=False)
                    self.last_scan = time.time()
                self._poll_current_job_locked()
                if not self.current_job and self.auto_analyze and self.is_configured():
                    if clip := self._next_ready_clip_locked():
                        self._launch_job_locked(clip)
                self._save_index_locked()
            time.sleep(1)

    def _discover_clips_locked(self, force: bool = False) -> int:
        if not self.recordings_root.exists():
            return 0

        discovered = 0
        existing_paths = set()
        for path in sorted(self.recordings_root.rglob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
                continue
            clip_id = clip_hash(path)
            existing_paths.add(str(path))
            stat = path.stat()
            clip = self.index["clips"].get(clip_id, {})
            changed = (
                clip.get("mtime") != stat.st_mtime
                or clip.get("size") != stat.st_size
                or clip.get("path") != str(path)
            )
            if not clip:
                discovered += 1
            status = clip.get("status", "discovered")
            if changed and status == "completed":
                status = "discovered"
            if changed and status == "failed":
                status = "discovered"
            clip.update(
                {
                    "id": clip_id,
                    "path": str(path),
                    "name": path.name,
                    "camera": path.parent.name,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                    "status": status,
                    "analysis_dir": str((ANALYSIS_DIR / clip_id).resolve()),
                }
            )
            if self.auto_analyze and self.enabled and self.is_configured() and changed:
                if self._clip_ready(clip):
                    clip["status"] = "queued"
            self.index["clips"][clip_id] = clip

        for clip in self.index["clips"].values():
            clip["missing"] = clip.get("path") not in existing_paths

        return discovered

    def _clip_ready(self, clip: dict[str, Any]) -> bool:
        return time.time() - clip.get("mtime", 0) >= self.min_file_age

    def _next_ready_clip_locked(self) -> dict[str, Any] | None:
        clips = sorted(
            self.index["clips"].values(),
            key=lambda clip: clip.get("mtime", 0),
            reverse=True,
        )
        for clip in clips:
            if clip.get("missing"):
                continue
            if clip.get("status") not in {"queued", "discovered"}:
                continue
            if not self._clip_ready(clip):
                continue
            clip["status"] = "queued"
            return clip
        return None

    def _launch_job_locked(self, clip: dict[str, Any]) -> None:
        analysis_dir = Path(clip["analysis_dir"])
        analysis_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.python_bin,
            str(self.runner_path),
            "analyze",
            "--config",
            self.project_config,
            "--video",
            clip["path"],
            "--destfolder",
            str(analysis_dir),
            "--pcutoff",
            str(self.pcutoff),
        ]
        if self.create_labeled_video:
            command.append("--create-labeled-video")

        logger.info(f"[DLC] analyzing {clip['path']}")
        clip["status"] = "running"
        clip["started_at"] = time.time()
        clip["error"] = None
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=APP_DIR.parent,
        )
        self.current_job = {"clip_id": clip["id"], "process": process}

    def _poll_current_job_locked(self) -> None:
        if not self.current_job:
            return
        process = self.current_job["process"]
        if process.poll() is None:
            return

        clip_id = self.current_job["clip_id"]
        clip = self.index["clips"].get(clip_id)
        stdout, stderr = process.communicate()
        self.current_job = None
        if not clip:
            return

        clip["finished_at"] = time.time()
        clip["stdout"] = trim_text(stdout)
        clip["stderr"] = trim_text(stderr)

        if process.returncode == 0:
            result = self._load_result_file(clip) or parse_json_stdout(stdout)
            clip["status"] = "completed"
            clip["result"] = result
            clip["summary_path"] = result.get("summary_path") if result else None
            clip["csv_path"] = result.get("csv_path") if result else None
            clip["labeled_video_path"] = result.get("labeled_video_path") if result else None
            clip["error"] = None
            logger.info(f"[DLC] completed {clip['path']}")
            return

        clip["status"] = "failed"
        clip["error"] = stderr.strip() or stdout.strip() or f"exit code {process.returncode}"
        logger.error(f"[DLC] failed for {clip['path']}: {clip['error']}")

    def _load_result_file(self, clip: dict[str, Any]) -> dict[str, Any] | None:
        result_file = Path(clip["analysis_dir"]) / "result.json"
        if not result_file.is_file():
            return None
        try:
            return json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            logger.error(ex)
            return None

    def _load_summary_locked(self, clip: dict[str, Any]) -> dict[str, Any] | None:
        summary_path = clip.get("summary_path")
        if not summary_path and clip.get("result"):
            summary_path = clip["result"].get("summary_path")
        if not summary_path:
            result = self._load_result_file(clip)
            if result:
                summary_path = result.get("summary_path")
                clip["result"] = result
                clip["summary_path"] = summary_path
        if not summary_path:
            return None
        try:
            return json.loads(Path(summary_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            logger.error(ex)
            return None

    def _public_clip_data(self, clip: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": clip.get("id"),
            "name": clip.get("name"),
            "camera": clip.get("camera"),
            "path": clip.get("path"),
            "mtime": clip.get("mtime"),
            "size": clip.get("size"),
            "status": clip.get("status"),
            "started_at": clip.get("started_at"),
            "finished_at": clip.get("finished_at"),
            "missing": clip.get("missing", False),
            "error": clip.get("error"),
            "summary_path": clip.get("summary_path"),
            "csv_path": clip.get("csv_path"),
            "labeled_video_path": clip.get("labeled_video_path"),
            "supported_prompts": SUPPORTED_PROMPTS,
        }

    def _load_index(self) -> None:
        if not self.index_path.is_file():
            return
        try:
            self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as ex:
            logger.error(ex)
            self.index = {"clips": {}}

    def _save_index_locked(self) -> None:
        self.index_path.write_text(json.dumps(self.index, indent=2), encoding="utf-8")


def resolve_recordings_root() -> Path:
    if video_root := env_bool("DLC_VIDEO_ROOT", style="original"):
        return Path(video_root).expanduser().resolve()

    record_path = env_bool(
        "RECORD_PATH",
        "record/%path/%Y-%m-%d_%H-%M-%S",
        style="original",
    ).strip("/\\")
    static_parts = []
    for part in Path(record_path).parts:
        if any(token in part for token in {"%", "{", "}"}):
            break
        static_parts.append(part)
    return (Path("/") / Path(*static_parts)).resolve() if static_parts else Path("/").resolve()


def clip_hash(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


def trim_text(value: str, limit: int = 4000) -> str:
    value = value.strip()
    return value[-limit:] if len(value) > limit else value


def parse_json_stdout(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def answer_pose_prompt(question: str, summary: dict[str, Any]) -> dict[str, Any]:
    normalized = question.lower().strip()
    track = match_track(normalized, summary.get("tracks", []))
    track_data = summary.get("track_summaries", {}).get(track, {}) if track else {}
    fps = summary.get("fps") or 0

    if "bodypart" in normalized or "tracked" in normalized:
        tracks = ", ".join(summary.get("tracks", [])) or "none"
        return {"intent": "tracks", "track": None, "answer": f"Tracked bodyparts: {tracks}."}

    if "how long" in normalized or "duration" in normalized:
        duration = summary.get("duration_seconds")
        if duration is None:
            return {"intent": "duration", "track": None, "answer": "Clip duration is unavailable."}
        return {"intent": "duration", "track": None, "answer": f"The clip is {duration:.2f} seconds long."}

    if ("most" in normalized and "move" in normalized) or "most active" in normalized:
        dominant = summary.get("dominant_track")
        if not dominant:
            return {"intent": "movement", "track": None, "answer": "No movement summary is available yet."}
        movement = summary["track_summaries"][dominant]["movement_px"]
        return {
            "intent": "movement",
            "track": dominant,
            "answer": f"{dominant} moved the most, with about {movement:.2f} pixels of total motion.",
        }

    if "confidence" in normalized or "likelihood" in normalized:
        if track and track_data:
            confidence = track_data.get("avg_likelihood", 0)
            return {
                "intent": "confidence",
                "track": track,
                "answer": f"{track} has an average confidence of {confidence:.2%}.",
            }
        confidence = summary.get("avg_likelihood", 0)
        return {
            "intent": "confidence",
            "track": None,
            "answer": f"The overall average DeepLabCut confidence is {confidence:.2%}.",
        }

    if "visible" in normalized or "present" in normalized:
        if not track or not track_data:
            return {
                "intent": "visibility",
                "track": None,
                "answer": "Ask about a specific bodypart, for example: 'How visible was nose?'",
            }
        ratio = track_data.get("visible_ratio", 0)
        first_seen = frame_time(track_data.get("first_visible_frame"), fps)
        last_seen = frame_time(track_data.get("last_visible_frame"), fps)
        return {
            "intent": "visibility",
            "track": track,
            "answer": (
                f"{track} was visible for {ratio:.2%} of analyzed frames"
                f"{format_visibility_window(first_seen, last_seen)}."
            ),
        }

    if "when" in normalized and track and track_data:
        first_seen = frame_time(track_data.get("first_visible_frame"), fps)
        last_seen = frame_time(track_data.get("last_visible_frame"), fps)
        return {
            "intent": "when",
            "track": track,
            "answer": f"{track} appears{format_visibility_window(first_seen, last_seen)}.",
        }

    tracks = summary.get("tracks", [])
    dominant = summary.get("dominant_track")
    duration = summary.get("duration_seconds")
    track_text = ", ".join(tracks[:6]) if tracks else "no tracks"
    duration_text = f"{duration:.2f}s" if duration is not None else "unknown duration"
    dominant_text = f" The most active bodypart was {dominant}." if dominant else ""
    if track and track_data:
        dominant_text += (
            f" {track} was visible for {track_data.get('visible_ratio', 0):.2%} of analyzed frames"
            f" with average confidence {track_data.get('avg_likelihood', 0):.2%}."
        )
    return {
        "intent": "summary",
        "track": track,
        "answer": (
            f"This clip is {duration_text}, with {len(tracks)} tracked bodyparts: {track_text}."
            f"{dominant_text}"
        ),
    }


def match_track(question: str, tracks: list[str]) -> str | None:
    normalized = question.replace("-", " ").replace("_", " ")
    for track in tracks:
        candidate = track.lower().replace(":", " ").replace("_", " ")
        if candidate in normalized:
            return track
    return None


def frame_time(frame_index: int | None, fps: float) -> float | None:
    if frame_index is None or not fps:
        return None
    return round(frame_index / fps, 3)


def format_visibility_window(first_seen: float | None, last_seen: float | None) -> str:
    if first_seen is None and last_seen is None:
        return ""
    if first_seen is not None and last_seen is not None:
        return f" from {first_seen:.2f}s to {last_seen:.2f}s"
    if first_seen is not None:
        return f" starting around {first_seen:.2f}s"
    return f" until about {last_seen:.2f}s"
