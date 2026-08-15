#!/usr/bin/env python3
"""Safely batch-compress recordings with HandBrakeCLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Iterable


VIDEO_EXTENSIONS = {
    ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mts", ".ts", ".webm",
}
GENERATED_ENDINGS = (
    ".compressed.mkv", ".fast.mkv", ".editing.mp4", ".partial.mkv", ".partial.mp4",
)
EFFICIENT_CODECS = {"av1", "hevc", "h265", "vp9"}
SUPPORTED_FPS = {5, 10, 12, 15, 20, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60}


@dataclass(frozen=True)
class ModeConfig:
    name: str
    description: str
    encoder: str
    preset: str
    default_quality: float
    container: str
    output_ending: str
    expected_codec: str
    constant_framerate: bool = False
    gpu: bool = False


MODES = {
    "quality": ModeConfig(
        "quality", "CPU H.265: slowest, best compression", "x265_10bit", "medium",
        22.0, "av_mkv", ".compressed.mkv", "hevc",
    ),
    "fast": ModeConfig(
        "fast", "RTX GPU H.265: much faster, somewhat larger files", "nvenc_h265_10bit",
        "slow", 24.0, "av_mkv", ".fast.mkv", "hevc", gpu=True,
    ),
    "editing": ModeConfig(
        "editing", "H.264 MP4: larger, constant-frame-rate editing copy", "x264", "fast",
        18.0, "av_mp4", ".editing.mp4", "h264", constant_framerate=True,
    ),
}


@dataclass(frozen=True)
class MediaInfo:
    video_codec: str
    width: int
    height: int
    fps: float
    duration: float
    video_tracks: int
    audio_tracks: int
    subtitle_tracks: int
    audio_channels: tuple[int, ...]


@dataclass(frozen=True)
class Toolchain:
    """The local command-line tools needed by one uncomplicated batch."""

    handbrake: Path | None
    ffprobe: Path | None
    ffmpeg: Path | None
    handbrake_version: str = "unknown"
    available_encoders: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VideoTask:
    source: Path
    destination: Path


@dataclass
class BatchStats:
    encoded: int = 0
    existing_verified: int = 0
    efficient_skipped: int = 0
    other_skipped: int = 0
    failed: int = 0
    no_benefit: int = 0
    deletion_kept: int = 0
    originals_deleted: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    disk_change: int = 0  # Positive means space freed; negative means space used.


def quality_value(value: str) -> float:
    try:
        quality = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("quality must be a number") from exc
    if not 0 <= quality <= 51:
        raise argparse.ArgumentTypeError("quality must be between 0 and 51")
    return quality


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def fps_value(value: str) -> float:
    try:
        fps = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("FPS must be a number") from exc
    if fps not in SUPPORTED_FPS:
        choices = ", ".join(str(item) for item in sorted(SUPPORTED_FPS))
        raise argparse.ArgumentTypeError(f"FPS must be one of: {choices}")
    return fps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Compress a folder of recordings, validate video/audio, and keep originals "
            "unless deletion is explicitly requested."
        ),
    )
    parser.add_argument("input_folder", type=Path, help="Folder containing source videos")
    parser.add_argument(
        "--mode", choices=tuple(MODES), default="quality",
        help="quality=smallest, fast=RTX GPU, editing=compatible H.264 MP4",
    )
    parser.add_argument(
        "-o", "--output-folder", type=Path,
        help="Destination folder (default: INPUT_FOLDER/_compressed)",
    )
    parser.add_argument(
        "-q", "--quality", type=quality_value,
        help="Override mode quality; lower means better quality and larger files",
    )
    fps_group = parser.add_mutually_exclusive_group()
    fps_group.add_argument(
        "--fps-cap", type=fps_value, default=30.0,
        help="Reduce only videos above this frame rate",
    )
    fps_group.add_argument(
        "--no-fps-cap", action="store_true", help="Always preserve the source frame rate",
    )
    parser.add_argument(
        "--recompress-efficient", action="store_true",
        help="Also process files already using H.265, AV1, or VP9",
    )
    parser.add_argument(
        "--keep-larger", action="store_true",
        help="Keep newly created outputs even when they are not smaller",
    )
    parser.add_argument(
        "--overwrite-existing", action="store_true",
        help="Replace an existing generated output after a new encode validates",
    )
    parser.add_argument(
        "--delete-originals", action="store_true",
        help="Permanently delete each original only after strict validation and size reduction",
    )
    parser.add_argument("--limit", type=positive_int, help="Process only the first N videos")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Inspect files and show decisions without writing anything",
    )
    parser.add_argument("--handbrake", type=Path, help="Full path to HandBrakeCLI")
    parser.add_argument("--ffprobe", type=Path, help="Full path to ffprobe")
    parser.add_argument("--ffmpeg", type=Path, help="Full path to ffmpeg (required for deletion)")
    return parser.parse_args()


def find_executable(
    explicit: Path | None, names: tuple[str, ...], candidates: list[Path]
) -> Path | None:
    if explicit:
        path = explicit.expanduser().resolve()
        return path if path.is_file() else None
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found).resolve()
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def find_handbrake(explicit: Path | None) -> Path | None:
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / "tools" / "HandBrakeCLI.exe", script_dir / "tools" / "HandBrakeCLI"]
    configured = os.environ.get("HANDBRAKECLI")
    if configured:
        candidates.append(Path(configured).expanduser())
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            root = Path(base)
            candidates.extend([
                root / "HandBrake" / "HandBrakeCLI.exe",
                root / "Programs" / "HandBrake" / "HandBrakeCLI.exe",
                root / "Microsoft" / "WinGet" / "Links" / "HandBrakeCLI.exe",
            ])
    return find_executable(explicit, ("HandBrakeCLI", "HandBrakeCLI.exe"), candidates)


def find_ffprobe(explicit: Path | None) -> Path | None:
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / "tools" / "ffprobe.exe", script_dir / "tools" / "ffprobe"]
    configured = os.environ.get("FFPROBE")
    if configured:
        candidates.append(Path(configured).expanduser())
    return find_executable(explicit, ("ffprobe", "ffprobe.exe"), candidates)


def find_ffmpeg(explicit: Path | None) -> Path | None:
    script_dir = Path(__file__).resolve().parent
    candidates = [script_dir / "tools" / "ffmpeg.exe", script_dir / "tools" / "ffmpeg"]
    configured = os.environ.get("FFMPEG")
    if configured:
        candidates.append(Path(configured).expanduser())
    return find_executable(explicit, ("ffmpeg", "ffmpeg.exe"), candidates)


def command_text(command: list[str], timeout: int = 20) -> tuple[int, str]:
    """Run a harmless tool-information command and return combined text."""
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False,
    )
    return result.returncode, (result.stdout + "\n" + result.stderr).strip()


def inspect_toolchain(
    handbrake: Path | None = None, ffprobe: Path | None = None, ffmpeg: Path | None = None,
) -> Toolchain:
    """Find tools and discover encoder support without starting an encode."""
    handbrake = handbrake or find_handbrake(None)
    ffprobe = ffprobe or find_ffprobe(None)
    ffmpeg = ffmpeg or find_ffmpeg(None)
    if handbrake is None:
        return Toolchain(None, ffprobe, ffmpeg)
    try:
        _, version_text = command_text([str(handbrake), "--version"])
        _, help_text = command_text([str(handbrake), "--help"])
    except (OSError, subprocess.TimeoutExpired):
        return Toolchain(handbrake, ffprobe, ffmpeg)
    version = next((line.strip() for line in version_text.splitlines() if line.strip()), "unknown")
    encoders = frozenset(
        config.encoder for config in MODES.values()
        if re.search(rf"(?<![\w-]){re.escape(config.encoder)}(?![\w-])", help_text)
    )
    return Toolchain(handbrake, ffprobe, ffmpeg, version, encoders)


def preflight_mode(toolchain: Toolchain, mode: ModeConfig) -> list[str]:
    if toolchain.handbrake is None:
        return ["HandBrakeCLI was not found. Install it or choose its full path."]
    if mode.encoder not in toolchain.available_encoders:
        return [
            f"HandBrakeCLI does not report support for the {mode.name} encoder "
            f"({mode.encoder}). Choose a supported mode.",
        ]
    return []


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def output_location_error(input_root: Path, output_root: Path) -> str | None:
    """Reject output locations that could make the input look disposable."""
    if output_root == input_root or is_within(input_root, output_root):
        return "Output folder must not be the input folder or an ancestor of it."
    return None


def discover_videos(input_root: Path, output_root: Path) -> list[Path]:
    videos: list[Path] = []
    for current_dir, dir_names, file_names in os.walk(input_root):
        current = Path(current_dir).resolve()
        dir_names[:] = [
            name for name in dir_names
            if not is_within((current / name).resolve(), output_root)
        ]
        for name in file_names:
            path = current / name
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            lower_name = name.lower()
            if lower_name.endswith(GENERATED_ENDINGS) or ".partial." in lower_name:
                continue
            videos.append(path)
    return sorted(videos, key=lambda path: str(path).lower())


def build_tasks(
    videos: list[Path], input_root: Path, output_root: Path, mode: ModeConfig
) -> list[VideoTask]:
    """Keep ordinary names readable and only tag actual output collisions."""
    candidates: list[tuple[Path, Path]] = []
    for source in videos:
        relative = source.relative_to(input_root)
        candidates.append((source, output_root / relative.parent / f"{source.stem}{mode.output_ending}"))

    # Re-check after tagging because a user may already have a filename that
    # resembles a tagged collision output (the old second-order collision).
    for _ in range(4):
        groups: dict[str, list[int]] = {}
        for index, (_, destination) in enumerate(candidates):
            groups.setdefault(str(destination).casefold(), []).append(index)
        collisions = [indices for indices in groups.values() if len(indices) > 1]
        if not collisions:
            return [VideoTask(source, destination) for source, destination in candidates]
        for indices in collisions:
            for index in indices:
                source, _ = candidates[index]
                relative = source.relative_to(input_root)
                # Keep the source-path identity case-sensitive for Linux, while
                # the destination grouping above still protects Windows/macOS.
                digest = hashlib.sha256(str(relative).encode("utf-8")).hexdigest()[:10]
                destination = output_root / relative.parent / f"{source.stem}--{digest}{mode.output_ending}"
                candidates[index] = (source, destination)
    raise RuntimeError("could not make output names unique; rename one source file")


def format_size(byte_count: int) -> str:
    value = float(abs(byte_count))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def parse_rate(value: object) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        return float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def normalize_codec(codec: str) -> str:
    value = codec.lower().strip()
    return "hevc" if value in {"h265", "hevc"} else value


def probe_with_ffprobe(ffprobe: Path, path: Path) -> MediaInfo:
    command = [
        str(ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"ffprobe exited with {result.returncode}")
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and stream.get("disposition", {}).get("attached_pic", 0) != 1
    ]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    if not videos:
        raise RuntimeError("no video track found")
    video = videos[0]
    duration_value = data.get("format", {}).get("duration") or video.get("duration") or 0
    try:
        duration = float(duration_value)
    except (TypeError, ValueError):
        duration = 0.0
    fps = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))
    return MediaInfo(
        normalize_codec(str(video.get("codec_name", "unknown"))),
        int(video.get("width") or 0), int(video.get("height") or 0), fps, duration,
        len(videos), len(audios), len(subtitles),
        tuple(int(audio.get("channels") or 0) for audio in audios),
    )


def extract_json_after_marker(text: str, marker: str) -> dict:
    marker_index = text.find(marker)
    if marker_index < 0:
        raise RuntimeError(f"HandBrake scan did not contain {marker.rstrip(': ')}")
    value, _ = json.JSONDecoder().raw_decode(text[marker_index + len(marker):].lstrip())
    return value


def probe_with_handbrake(handbrake: Path, path: Path) -> MediaInfo:
    command = [str(handbrake), "--input", str(path), "--scan", "--json"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    data = extract_json_after_marker(result.stdout + "\n" + result.stderr, "JSON Title Set:")
    titles = data.get("TitleList", [])
    if not titles:
        raise RuntimeError("HandBrake found no valid video title")
    title = titles[0]
    rate = title.get("FrameRate", {})
    denominator = rate.get("Den") or 0
    fps = (rate.get("Num") or 0) / denominator if denominator else 0.0
    duration = float(title.get("Duration", {}).get("Ticks") or 0) / 90000
    return MediaInfo(
        normalize_codec(str(title.get("VideoCodec", "unknown"))),
        int(title.get("Geometry", {}).get("Width") or 0),
        int(title.get("Geometry", {}).get("Height") or 0),
        fps, duration, 1, len(title.get("AudioList", [])), len(title.get("SubtitleList", [])),
        tuple(int(audio.get("ChannelCount") or 0) for audio in title.get("AudioList", [])),
    )


def probe_media(path: Path, ffprobe: Path | None, handbrake: Path) -> MediaInfo:
    ffprobe_error: Exception | None = None
    if ffprobe:
        try:
            return probe_with_ffprobe(ffprobe, path)
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            ffprobe_error = error
    try:
        return probe_with_handbrake(handbrake, path)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        if ffprobe_error:
            raise RuntimeError(f"ffprobe failed ({ffprobe_error}); HandBrake scan failed ({error})") from error
        raise RuntimeError(f"HandBrake scan failed ({error})") from error


def describe_media(info: MediaInfo) -> str:
    fps = f"{info.fps:.2f}".rstrip("0").rstrip(".") if info.fps else "unknown"
    return f"{info.video_codec}, {info.width}x{info.height}, {fps} fps, audio tracks: {info.audio_tracks}"


def fps_arguments(info: MediaInfo, mode: ModeConfig, fps_cap: float | None) -> list[str]:
    above_cap = fps_cap is not None and info.fps > fps_cap + 0.01
    if mode.constant_framerate:
        return ["--rate", str(fps_cap), "--cfr"] if above_cap else ["--cfr"]
    return ["--rate", str(fps_cap), "--pfr"] if above_cap else ["--vfr"]


def build_command(
    handbrake: Path, task: VideoTask, temporary: Path, info: MediaInfo,
    mode: ModeConfig, quality: float, fps_cap: float | None,
) -> list[str]:
    command = [
        str(handbrake), "--input", str(task.source), "--output", str(temporary),
        "--format", mode.container, "--encoder", mode.encoder,
        "--encoder-preset", mode.preset, "--quality", str(quality),
        "--crop-mode", "none", "--all-audio", "--aencoder", "copy",
        "--audio-fallback", "av_aac", "--all-subtitles", "--markers",
        "--keep-metadata", "--keep-aname", "--keep-subname",
    ]
    command.extend(fps_arguments(info, mode, fps_cap))
    if mode.gpu:
        command.extend(["--enable-hw-decoding", "nvdec", "--encopts", "rc-lookahead=10"])
    return command


def validate_output(
    source: MediaInfo, output: MediaInfo, mode: ModeConfig, fps_cap: float | None,
) -> list[str]:
    """Require the encoded media to retain the properties this app promises."""
    errors: list[str] = []
    if normalize_codec(output.video_codec) != mode.expected_codec:
        errors.append(f"expected {mode.expected_codec} video, found {output.video_codec}")
    if output.video_tracks != source.video_tracks:
        errors.append(f"video tracks changed from {source.video_tracks} to {output.video_tracks}")
    if output.audio_tracks != source.audio_tracks:
        errors.append(f"audio tracks changed from {source.audio_tracks} to {output.audio_tracks}")
    for track_number, (source_channels, output_channels) in enumerate(
        zip(source.audio_channels, output.audio_channels), start=1
    ):
        if source_channels and output_channels != source_channels:
            errors.append(
                f"audio track {track_number} channels changed "
                f"from {source_channels} to {output_channels}"
            )
    if output.subtitle_tracks != source.subtitle_tracks:
        errors.append(f"subtitle tracks changed from {source.subtitle_tracks} to {output.subtitle_tracks}")
    if source.width and output.width != source.width:
        errors.append(f"width changed from {source.width} to {output.width}")
    if source.height and output.height != source.height:
        errors.append(f"height changed from {source.height} to {output.height}")
    if not source.duration:
        errors.append("source duration could not be confirmed")
    elif not output.duration:
        errors.append("output duration could not be confirmed")
    else:
        tolerance = max(1.5, source.duration * 0.005)
        if abs(source.duration - output.duration) > tolerance:
            errors.append(f"duration changed from {source.duration:.1f}s to {output.duration:.1f}s")
    if not source.fps or not output.fps:
        errors.append("frame rate could not be confirmed")
    else:
        expected_fps = fps_cap if fps_cap is not None and source.fps > fps_cap + 0.01 else source.fps
        if abs(output.fps - expected_fps) > 0.15:
            errors.append(
                f"frame rate changed from {source.fps:.2f} to {output.fps:.2f}; "
                f"expected about {expected_fps:.2f}"
            )
    return errors


class BatchLogger:
    """Keep the log open so temporary Windows file locks cannot stop a batch."""

    def __init__(self, preferred_path: Path, fallback_path: Path) -> None:
        self.path = preferred_path
        self._handle = None
        try:
            self._handle = preferred_path.open("a", encoding="utf-8")
        except OSError as preferred_error:
            self.path = fallback_path
            try:
                self._handle = fallback_path.open("a", encoding="utf-8")
                print(f"WARNING: Output-folder logging failed ({preferred_error}).")
                print(f"Logging to: {fallback_path}\n")
            except OSError as fallback_error:
                print(f"WARNING: Logging is disabled: {fallback_error}\n")

    def write(self, lines: Iterable[str]) -> None:
        if self._handle is None:
            return
        try:
            for line in lines:
                self._handle.write(line.rstrip() + "\n")
            self._handle.flush()
        except OSError as error:
            print(f"WARNING: Logging stopped, but encoding will continue: {error}\n")
            self.close()

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def run_encode(command: list[str], logger: BatchLogger) -> tuple[int, list[str]]:
    recent: deque[str] = deque(maxlen=80)
    progress_was_shown = False
    last_progress = ""
    popen_options: dict[str, object] = {}
    if os.name == "nt":
        popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_options["start_new_session"] = True
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, **popen_options,
    )
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            recent.append(line)
            progress_matches = re.findall(
                r"Encoding:\s*task\s+\d+\s+of\s+\d+,\s*\d+(?:\.\d+)?\s*%",
                line,
            )
            progress = progress_matches[-1] if progress_matches else ""
            if not progress and line.startswith("Muxing:"):
                progress = "Muxing output..."
            if progress and progress != last_progress:
                print(f"\r  {progress[:100]:<100}", end="", flush=True)
                progress_was_shown = True
                last_progress = progress
        return_code = process.wait()
    except KeyboardInterrupt:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        raise
    finally:
        if progress_was_shown:
            print()
    if return_code != 0:
        logger.write(["HandBrake recent output:", *recent, ""])
    return return_code, list(recent)


def print_recent_error(lines: list[str]) -> None:
    useful = [line for line in lines if "Encoding:" not in line]
    for line in useful[-6:]:
        print(f"    {line}")


def _stop_requested(_signum: int, _frame: object) -> None:
    """Turn a POSIX GUI stop request into the same safe path as Ctrl+C."""
    raise KeyboardInterrupt


def decode_integrity(ffmpeg: Path, output: Path, duration: float) -> tuple[bool, str]:
    """Decode every video and audio stream before a source can be removed."""
    timeout = max(180, min(7200, int(duration * 8)))
    command = [
        str(ffmpeg), "-nostdin", "-v", "error", "-xerror", "-i", str(output),
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"integrity decode could not complete: {error}"
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return False, f"integrity decode failed: {detail[-1] if detail else 'ffmpeg returned an error'}"
    return True, ""


def delete_source_safely(
    source: Path, input_root: Path, output_root: Path,
    original_size: int, original_mtime_ns: int,
) -> tuple[bool, str]:
    try:
        resolved = source.resolve(strict=True)
        current = source.stat()
    except OSError as error:
        return False, f"source could not be rechecked: {error}"
    if not is_within(resolved, input_root) or is_within(resolved, output_root):
        return False, "source path failed the safety boundary check"
    if current.st_size != original_size or current.st_mtime_ns != original_mtime_ns:
        return False, "source changed during encoding"
    try:
        source.unlink()
    except OSError as error:
        return False, f"source deletion failed: {error}"
    return True, ""


def maybe_delete_original(
    args: argparse.Namespace, task: VideoTask, input_root: Path, output_root: Path,
    source_size: int, source_mtime_ns: int, output_size: int, stats: BatchStats,
    logger: BatchLogger, output_was_created_now: bool, ffmpeg: Path | None,
    output_info: MediaInfo,
) -> bool:
    if not args.delete_originals:
        return False
    if not output_was_created_now:
        reason = "existing outputs never authorize deletion; re-encode in this run if needed"
    elif output_size >= source_size:
        reason = "compressed output is not smaller"
    elif ffmpeg is None:
        reason = "ffmpeg was not found; an integrity decode is required before deletion"
    else:
        integrity_ok, reason = decode_integrity(ffmpeg, task.destination, output_info.duration)
        if integrity_ok:
            reason = ""
    if reason:
        stats.deletion_kept += 1
        print(f"  ORIGINAL KEPT: {reason}\n")
        logger.write([f"ORIGINAL KEPT: {task.source}: {reason}"])
        return False
    deleted, reason = delete_source_safely(
        task.source, input_root, output_root, source_size, source_mtime_ns,
    )
    if not deleted:
        print(f"  ORIGINAL KEPT: {reason}\n")
        logger.write([f"ORIGINAL KEPT: {task.source}: {reason}"])
        return False
    stats.originals_deleted += 1
    stats.disk_change += source_size - output_size if output_was_created_now else source_size
    print("  Original permanently deleted after validation.\n")
    logger.write([f"ORIGINAL DELETED: {task.source}"])
    return True


def confirm_deletion(input_root: Path, task_count: int) -> bool:
    print("DANGER: --delete-originals permanently deletes source files.")
    print("Deletion happens only after validation and only when the output is smaller.")
    print(f"Folder: {input_root}")
    print(f"Videos considered: {task_count}")
    try:
        answer = input("Type DELETE to continue, or press Enter to cancel: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer == "DELETE"


def output_is_efficient(info: MediaInfo) -> bool:
    return normalize_codec(info.video_codec) in EFFICIENT_CODECS


def print_summary(stats: BatchStats, logger: BatchLogger, originals_kept_by_default: bool) -> None:
    print("Summary")
    print(f"  Encoded and validated: {stats.encoded}")
    print(f"  Existing outputs verified: {stats.existing_verified}")
    print(f"  Efficient sources skipped: {stats.efficient_skipped}")
    print(f"  No-size-benefit outputs discarded: {stats.no_benefit}")
    print(f"  Other skipped: {stats.other_skipped}")
    print(f"  Failed or needs attention: {stats.failed}")
    if stats.input_bytes:
        print(f"  Encoded input size:  {format_size(stats.input_bytes)}")
        print(f"  Encoded output size: {format_size(stats.output_bytes)}")
        difference = stats.input_bytes - stats.output_bytes
        percentage = abs(difference) / stats.input_bytes * 100
        label = "Compression reduction" if difference >= 0 else "Compression increase"
        print(f"  {label}: {format_size(difference)} ({percentage:.1f}%)")
    if stats.originals_deleted:
        print(f"  Originals deleted: {stats.originals_deleted}")
    if stats.deletion_kept:
        print(f"  Originals kept by a deletion safeguard: {stats.deletion_kept}")
    if stats.disk_change > 0:
        print(f"  Disk space freed this run: {format_size(stats.disk_change)}")
    elif stats.disk_change < 0:
        print(f"  Additional disk space used this run: {format_size(stats.disk_change)}")
    elif originals_kept_by_default and stats.encoded:
        print("  Disk space freed: 0 B (originals were kept)")
    print(f"  Log: {logger.path}")


def main() -> int:
    if os.name != "nt":
        signal.signal(signal.SIGTERM, _stop_requested)
    args = parse_args()
    mode = MODES[args.mode]
    quality = args.quality if args.quality is not None else mode.default_quality
    fps_cap = None if args.no_fps_cap else args.fps_cap
    input_root = args.input_folder.expanduser().resolve()
    if not input_root.is_dir():
        print(f"ERROR: Input folder does not exist: {input_root}", file=sys.stderr)
        return 2
    output_root = (
        args.output_folder.expanduser().resolve()
        if args.output_folder else input_root / "_compressed"
    ).resolve()
    output_error = output_location_error(input_root, output_root)
    if output_error:
        print(f"ERROR: {output_error}", file=sys.stderr)
        return 2
    toolchain = inspect_toolchain(
        find_handbrake(args.handbrake), find_ffprobe(args.ffprobe), find_ffmpeg(args.ffmpeg),
    )
    preflight_errors = preflight_mode(toolchain, mode)
    if preflight_errors:
        for error in preflight_errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    assert toolchain.handbrake is not None
    handbrake = toolchain.handbrake
    ffprobe = toolchain.ffprobe
    ffmpeg = toolchain.ffmpeg
    videos = discover_videos(input_root, output_root)
    if args.limit:
        videos = videos[:args.limit]
    tasks = build_tasks(videos, input_root, output_root, mode)

    print(f"Input:      {input_root}")
    print(f"Output:     {output_root}")
    print(f"Videos:     {len(tasks)}")
    print(f"Mode:       {mode.name} - {mode.description}")
    print(f"Quality:    RF {quality:g}")
    print(f"Frame rate: {'source' if fps_cap is None else f'maximum {fps_cap:g} fps'}")
    print(f"Validation: {'ffprobe + HandBrake result' if ffprobe else 'HandBrake scan'}")
    print(f"HandBrake:  {toolchain.handbrake_version} ({handbrake})")
    print(f"Integrity: {'ffmpeg available for deletion' if ffmpeg else 'ffmpeg unavailable; originals will be kept'}")
    print(f"Originals:  {'DELETE after validation' if args.delete_originals else 'keep'}")
    print()
    if not tasks:
        print("No supported video files were found.")
        return 0
    if args.delete_originals and args.dry_run:
        print("NOTE: Dry-run mode never deletes files.\n")
    elif args.delete_originals and not confirm_deletion(input_root, len(tasks)):
        print("Cancelled. No files were changed.")
        return 0

    logger: BatchLogger | None = None
    stats = BatchStats()
    try:
        if not args.dry_run:
            try:
                output_root.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                print(f"ERROR: Cannot create output folder: {error}", file=sys.stderr)
                return 2
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            logger = BatchLogger(
                output_root / f"compression_log_{timestamp}.txt",
                Path(__file__).resolve().parent / f"compression_log_{timestamp}.txt",
            )
            logger.write([
                f"Started: {datetime.now().isoformat(timespec='seconds')}",
                f"Input: {input_root}", f"Output: {output_root}", f"Mode: {mode.name}",
                f"Encoder: {mode.encoder}, preset={mode.preset}, quality={quality:g}",
                f"FPS cap: {fps_cap if fps_cap is not None else 'none'}",
                f"Delete originals: {args.delete_originals}", "",
            ])

        for index, task in enumerate(tasks, start=1):
            print(f"[{index}/{len(tasks)}] {task.source.name}")
            try:
                source_stat = task.source.stat()
                source_info = probe_media(task.source, ffprobe, handbrake)
            except (OSError, RuntimeError) as error:
                print(f"  FAILED TO INSPECT: {error}\n")
                if logger:
                    logger.write([f"FAILED TO INSPECT: {task.source}: {error}"])
                stats.failed += 1
                continue
            print(f"  Source: {format_size(source_stat.st_size)} - {describe_media(source_info)}")
            if fps_cap is not None and source_info.fps > fps_cap + 0.01:
                print(f"  Frame rate will be limited to {fps_cap:g} fps.")

            if task.destination.is_file() and not args.overwrite_existing:
                try:
                    output_info = probe_media(task.destination, ffprobe, handbrake)
                    validation_errors = validate_output(source_info, output_info, mode, fps_cap)
                except (OSError, RuntimeError) as error:
                    validation_errors = [f"output could not be inspected: {error}"]
                if validation_errors:
                    print("  EXISTING OUTPUT NEEDS ATTENTION:")
                    for error in validation_errors:
                        print(f"    - {error}")
                    print("  Use --overwrite-existing to create and validate a replacement.\n")
                    if logger:
                        logger.write([f"INVALID EXISTING: {task.destination}", *validation_errors])
                    stats.failed += 1
                    continue
                output_size = task.destination.stat().st_size
                print(f"  VERIFIED EXISTING: {format_size(output_size)}, video and audio checks passed.")
                stats.existing_verified += 1
                if logger:
                    logger.write([f"VERIFIED EXISTING: {task.destination}"])
                if not args.dry_run and logger and args.delete_originals:
                    maybe_delete_original(
                        args, task, input_root, output_root, source_stat.st_size,
                        source_stat.st_mtime_ns, output_size, stats, logger, False, ffmpeg, output_info,
                    )
                else:
                    print()
                continue

            if mode.name != "editing" and output_is_efficient(source_info) and not args.recompress_efficient:
                print(f"  SKIPPED: {source_info.video_codec} is already efficiently compressed.\n")
                stats.efficient_skipped += 1
                if logger:
                    logger.write([f"SKIPPED EFFICIENT: {task.source} ({source_info.video_codec})"])
                continue

            print(f"  Output: {task.destination}")
            if args.dry_run:
                action = "would overwrite" if task.destination.exists() else "would encode"
                print(f"  DRY RUN: {action}; no files changed.\n")
                continue

            assert logger is not None
            try:
                task.destination.parent.mkdir(parents=True, exist_ok=True)
                if task.destination.exists() and not task.destination.is_file():
                    raise OSError("destination exists but is not a regular file")
                nonce = secrets.token_hex(8)
                temporary = task.destination.with_name(
                    f"{task.destination.stem}.partial.{os.getpid()}.{nonce}{task.destination.suffix}"
                )
                free_space = shutil.disk_usage(task.destination.parent).free
            except OSError as error:
                print(f"  FAILED: Cannot prepare the output path: {error}\n")
                logger.write([f"FAILED PREPARING OUTPUT: {task.source}: {error}"])
                stats.failed += 1
                continue
            if free_space < source_stat.st_size:
                print(
                    f"  WARNING: Only {format_size(free_space)} is free; "
                    f"the source is {format_size(source_stat.st_size)}."
                )
            command = build_command(
                handbrake, task, temporary, source_info, mode, quality, fps_cap,
            )
            logger.write([f"STARTED: {task.source}", subprocess.list2cmdline(command)])
            try:
                return_code, recent_output = run_encode(command, logger)
            except KeyboardInterrupt:
                print("\nStopped by user. Completed outputs are safe; originals are unchanged unless noted.")
                logger.write(["STOPPED: Interrupted by user"])
                return 130
            except OSError as error:
                print(f"  FAILED: Could not start HandBrakeCLI: {error}\n")
                logger.write([f"FAILED: {task.source}: {error}"])
                stats.failed += 1
                continue
            if return_code != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
                print(f"  FAILED: HandBrake exited with code {return_code}.")
                print_recent_error(recent_output)
                print()
                logger.write([f"FAILED: {task.source}: exit code {return_code}"])
                stats.failed += 1
                continue
            try:
                output_info = probe_media(temporary, ffprobe, handbrake)
                validation_errors = validate_output(source_info, output_info, mode, fps_cap)
            except (OSError, RuntimeError) as error:
                validation_errors = [f"output could not be inspected: {error}"]
            if validation_errors:
                print("  VALIDATION FAILED; original kept:")
                for error in validation_errors:
                    print(f"    - {error}")
                print(f"  Partial output retained for inspection: {temporary}\n")
                logger.write([f"VALIDATION FAILED: {task.source}", *validation_errors])
                stats.failed += 1
                continue
            output_size = temporary.stat().st_size
            if (
                output_size >= source_stat.st_size
                and mode.name != "editing"
                and not args.keep_larger
            ):
                increase = (output_size / source_stat.st_size - 1) * 100
                try:
                    temporary.unlink()
                except OSError as error:
                    print(f"  FAILED: larger temporary output could not be removed: {error}\n")
                    logger.write([f"FAILED CLEANING TEMPORARY OUTPUT: {temporary}: {error}"])
                    stats.failed += 1
                    continue
                print(f"  NO SIZE BENEFIT: output was {increase:.1f}% larger and was discarded.")
                print("  Original kept.\n")
                logger.write([f"DISCARDED LARGER OUTPUT: {task.source} ({increase:.1f}% larger)"])
                stats.no_benefit += 1
                continue
            try:
                temporary.replace(task.destination)
            except OSError as error:
                print(f"  FAILED: Could not finalize validated output: {error}\n")
                logger.write([f"FAILED FINALIZING OUTPUT: {temporary}: {error}"])
                stats.failed += 1
                continue
            try:
                os.utime(task.destination, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
            except OSError:
                pass
            stats.encoded += 1
            stats.input_bytes += source_stat.st_size
            stats.output_bytes += output_size
            saving = (1 - output_size / source_stat.st_size) * 100
            size_result = (
                f"{saving:.1f}% smaller" if saving >= 0 else f"{abs(saving):.1f}% larger"
            )
            print(f"  VALIDATED: {format_size(output_size)} ({size_result}).")
            print(
                f"  Tracks preserved: video {output_info.video_tracks}, "
                f"audio {output_info.audio_tracks}, subtitles {output_info.subtitle_tracks}."
            )
            logger.write([
                f"DONE: {task.source} -> {task.destination}",
                f"Size: {source_stat.st_size} -> {output_size} ({size_result})",
                f"Tracks: video={output_info.video_tracks}, audio={output_info.audio_tracks}, "
                f"subtitles={output_info.subtitle_tracks}",
            ])
            deleted = maybe_delete_original(
                args, task, input_root, output_root, source_stat.st_size,
                source_stat.st_mtime_ns, output_size, stats, logger, True, ffmpeg, output_info,
            )
            if not deleted:
                stats.disk_change -= output_size
                print("  Original kept.\n")

        if args.dry_run:
            print("Dry run complete. No files were changed.")
            return 0
        assert logger is not None
        print_summary(stats, logger, originals_kept_by_default=not args.delete_originals)
        print("\nAll counted outputs passed video, duration, and audio-track validation.")
        return 1 if stats.failed else 0
    finally:
        if logger:
            logger.close()


if __name__ == "__main__":
    raise SystemExit(main())
