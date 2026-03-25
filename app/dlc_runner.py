from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze a single video with DeepLabCut.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="Run DeepLabCut on one video clip.")
    analyze.add_argument("--config", required=True, help="DeepLabCut config.yaml path.")
    analyze.add_argument("--video", required=True, help="Video file to analyze.")
    analyze.add_argument("--destfolder", required=True, help="Output directory for DeepLabCut artifacts.")
    analyze.add_argument("--pcutoff", type=float, default=0.6, help="Likelihood threshold for visibility stats.")
    analyze.add_argument(
        "--create-labeled-video",
        action="store_true",
        help="Generate a labeled output video after analysis.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "analyze":
        result = analyze_video(
            config_path=Path(args.config),
            video_path=Path(args.video),
            destfolder=Path(args.destfolder),
            pcutoff=args.pcutoff,
            create_labeled_video=args.create_labeled_video,
        )
        print(json.dumps(result))
        return 0
    raise ValueError(f"Unsupported command: {args.command}")


def analyze_video(
    config_path: Path,
    video_path: Path,
    destfolder: Path,
    pcutoff: float = 0.6,
    create_labeled_video: bool = False,
) -> dict[str, Any]:
    import deeplabcut

    destfolder.mkdir(parents=True, exist_ok=True)
    videotype = video_path.suffix

    deeplabcut.analyze_videos(
        str(config_path),
        [str(video_path)],
        videotype=videotype,
        save_as_csv=True,
        destfolder=str(destfolder),
    )

    if create_labeled_video:
        deeplabcut.create_labeled_video(
            str(config_path),
            [str(video_path)],
            videotype=videotype.lstrip("."),
            destfolder=str(destfolder),
        )

    csv_path = newest_match(destfolder, f"{video_path.stem}*.csv")
    h5_path = newest_match(destfolder, f"{video_path.stem}*.h5")
    labeled_video = newest_match(destfolder, f"{video_path.stem}*labeled*.mp4")

    if not csv_path:
        raise FileNotFoundError(f"DeepLabCut did not produce a CSV for {video_path}")

    summary = summarize_pose_csv(csv_path, video_path, pcutoff)
    summary["input_video"] = str(video_path.resolve())
    summary["config_path"] = str(config_path.resolve())
    summary["csv_path"] = str(csv_path.resolve())
    summary["h5_path"] = str(h5_path.resolve()) if h5_path else None
    summary["labeled_video_path"] = str(labeled_video.resolve()) if labeled_video else None
    summary["pcutoff"] = pcutoff

    summary_path = destfolder / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    result = {
        "status": "completed",
        "video_path": str(video_path.resolve()),
        "summary_path": str(summary_path.resolve()),
        "csv_path": str(csv_path.resolve()),
        "h5_path": str(h5_path.resolve()) if h5_path else None,
        "labeled_video_path": str(labeled_video.resolve()) if labeled_video else None,
        "summary": summary,
    }
    (destfolder / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def newest_match(folder: Path, pattern: str) -> Path | None:
    matches = sorted(folder.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def summarize_pose_csv(csv_path: Path, video_path: Path, pcutoff: float) -> dict[str, Any]:
    import cv2
    import pandas as pd

    header_levels = detect_header_levels(csv_path)
    pose_data = pd.read_csv(csv_path, header=list(range(header_levels)), index_col=0)

    tracks: dict[str, dict[str, Any]] = {}
    for column in pose_data.columns:
        column_labels = [str(item).strip() for item in column]
        coord = column_labels[-1].lower()
        if coord not in {"x", "y", "likelihood"}:
            continue
        track_name = ":".join(filter(None, column_labels[1:-1])) or column_labels[-2]
        series = pd.to_numeric(pose_data[column], errors="coerce")
        tracks.setdefault(track_name, {})[coord] = series

    capture = cv2.VideoCapture(str(video_path))
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or len(pose_data))
    capture.release()

    per_track: dict[str, dict[str, Any]] = {}
    total_confidence = 0.0
    confidence_count = 0

    for track_name, values in tracks.items():
        x = values.get("x")
        y = values.get("y")
        likelihood = values.get("likelihood")
        if x is None or y is None or likelihood is None:
            continue

        visible = x.notna() & y.notna() & (likelihood.fillna(0) >= pcutoff)
        visible_frames = int(visible.sum())
        visible_ratio = visible_frames / len(pose_data) if len(pose_data) else 0.0
        visible_xy = pd.DataFrame({"x": x[visible], "y": y[visible]}).dropna()

        movement_px = 0.0
        if len(visible_xy) > 1:
            diffs = visible_xy.diff().dropna()
            movement_px = float(((diffs["x"] ** 2 + diffs["y"] ** 2) ** 0.5).sum())

        avg_likelihood = float(likelihood.fillna(0).mean())
        total_confidence += float(likelihood.fillna(0).sum())
        confidence_count += len(likelihood)

        entry = {
            "visible_frames": visible_frames,
            "visible_ratio": round(visible_ratio, 4),
            "avg_likelihood": round(avg_likelihood, 4),
            "movement_px": round(movement_px, 2),
            "first_visible_frame": first_true_index(visible),
            "last_visible_frame": last_true_index(visible),
        }
        if not visible_xy.empty:
            entry["bbox"] = {
                "min_x": round(float(visible_xy["x"].min()), 2),
                "max_x": round(float(visible_xy["x"].max()), 2),
                "min_y": round(float(visible_xy["y"].min()), 2),
                "max_y": round(float(visible_xy["y"].max()), 2),
            }
            entry["avg_position"] = {
                "x": round(float(visible_xy["x"].mean()), 2),
                "y": round(float(visible_xy["y"].mean()), 2),
            }
        per_track[track_name] = entry

    dominant_track = None
    if per_track:
        dominant_track = max(per_track.items(), key=lambda item: item[1]["movement_px"])[0]

    duration_seconds = round(frame_count / fps, 3) if fps > 0 else None
    overall_confidence = round(total_confidence / confidence_count, 4) if confidence_count else 0.0

    return {
        "video_name": video_path.name,
        "frame_count": frame_count,
        "fps": round(fps, 4) if fps > 0 else None,
        "duration_seconds": duration_seconds,
        "track_count": len(per_track),
        "tracks": sorted(per_track),
        "dominant_track": dominant_track,
        "avg_likelihood": overall_confidence,
        "track_summaries": per_track,
    }


def detect_header_levels(csv_path: Path) -> int:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [next(csv.reader(handle)) for _ in range(4)]

    fourth_row = [cell.strip().lower() for cell in rows[3][1:4]]
    return 4 if any(cell in {"x", "y", "likelihood"} for cell in fourth_row) else 3


def first_true_index(series) -> int | None:
    matches = series[series].index.tolist()
    return int(matches[0]) if matches else None


def last_true_index(series) -> int | None:
    matches = series[series].index.tolist()
    return int(matches[-1]) if matches else None


if __name__ == "__main__":
    raise SystemExit(main())
