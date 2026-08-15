from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import espresso_compresso_cli as cli
from espresso_compresso import result_space_from_summary
from espresso_compresso_cli import (
    BatchLogger,
    BatchStats,
    MODES,
    MediaInfo,
    Toolchain,
    VideoTask,
    build_tasks,
    decode_integrity,
    delete_source_safely,
    discover_videos,
    find_ffmpeg,
    fps_arguments,
    maybe_delete_original,
    output_location_error,
    preflight_mode,
    validate_output,
)


def media(*, fps: float = 30.0, duration: float = 60.0) -> MediaInfo:
    return MediaInfo("hevc", 1920, 1080, fps, duration, 1, 1, 0, (2,))


class CompressorSafetyTests(unittest.TestCase):
    def test_destinations_are_stable_and_collision_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            output = root / "_compressed"
            simple_task = build_tasks([root / "plain.mp4"], root, output, MODES["quality"])[0]
            clip = root / "clip.mp4"
            clip_digest = cli.hashlib.sha256("clip.mp4".encode("utf-8")).hexdigest()[:10]
            # This source's simple destination collides with clip.mp4 after that
            # file receives a tag for its .mp4/.mkv collision.
            second_order = root / f"clip--{clip_digest}.avi"
            sources = [clip, root / "clip.mkv", second_order]
            tasks = build_tasks(sources, root, output, MODES["quality"])
            second_run = build_tasks(sources, root, output, MODES["quality"])
            names = [str(task.destination).casefold() for task in tasks]
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(tasks, second_run)
            self.assertEqual(simple_task.destination.name, "plain.compressed.mkv")
            self.assertEqual(tasks[0].destination.name, f"clip--{clip_digest}.compressed.mkv")
            self.assertTrue(tasks[2].destination.name.startswith(f"clip--{clip_digest}--"))

    def test_collision_hashes_keep_case_distinct_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            output = root / "_compressed"
            tasks = build_tasks(
                [root / "clip.mp4", root / "Clip.mkv"], root, output, MODES["quality"],
            )
            self.assertEqual(len({task.destination.name.casefold() for task in tasks}), 2)
            self.assertNotEqual(tasks[0].destination.name.casefold(), tasks[1].destination.name.casefold())
            self.assertNotEqual(tasks[0].destination.name, tasks[1].destination.name)

    def test_discovery_skips_output_and_partial_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            (root / "source.mp4").write_bytes(b"source")
            (root / "stopped.partial.123.mkv").write_bytes(b"partial")
            (output / "result--123.compressed.mkv").write_bytes(b"result")
            self.assertEqual(discover_videos(root, output), [root / "source.mp4"])

    def test_validation_requires_known_duration_and_honours_fps_cap(self) -> None:
        capped = validate_output(media(fps=60), media(fps=60), MODES["quality"], 30)
        self.assertTrue(any("frame rate changed" in error for error in capped))
        unknown_duration = validate_output(media(duration=0), media(duration=0), MODES["quality"], None)
        self.assertTrue(any("duration" in error for error in unknown_duration))

    def test_existing_output_never_authorizes_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = root / "source.mp4"
            destination = output / "source--123.compressed.mkv"
            source.write_bytes(b"source data")
            destination.write_bytes(b"small")
            source_stat = source.stat()
            logger = BatchLogger(output / "test.log", output / "fallback.log")
            try:
                deleted = maybe_delete_original(
                    SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                    source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                    BatchStats(), logger, False, None, media(),
                )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertTrue(source.exists())

    def test_new_output_without_ffmpeg_never_authorizes_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = root / "source.mp4"
            destination = output / "source--123.compressed.mkv"
            source.write_bytes(b"source data")
            destination.write_bytes(b"small")
            source_stat = source.stat()
            logger = BatchLogger(output / "test.log", output / "fallback.log")
            try:
                deleted = maybe_delete_original(
                    SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                    source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                    BatchStats(), logger, True, None, media(),
                )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertTrue(source.exists())

    def test_new_smaller_integrity_approved_output_deletes_only_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = root / "source.mp4"
            untouched = root / "other.mp4"
            destination = output / "source.compressed.mkv"
            source.write_bytes(b"source data is larger")
            untouched.write_bytes(b"must remain")
            destination.write_bytes(b"small")
            source_stat = source.stat()
            logger = BatchLogger(output / "test.log", output / "fallback.log")
            try:
                with patch("espresso_compresso_cli.decode_integrity", return_value=(True, "")):
                    deleted = maybe_delete_original(
                        SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                        source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                        BatchStats(), logger, True, Path("ffmpeg"), media(),
                    )
            finally:
                logger.close()
            self.assertTrue(deleted)
            self.assertFalse(source.exists())
            self.assertTrue(untouched.exists())

    def test_deletion_boundary_rejects_output_folder_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = output / "not-a-source.mp4"
            source.write_bytes(b"data")
            stat = source.stat()
            deleted, reason = delete_source_safely(
                source, root, output, stat.st_size, stat.st_mtime_ns,
            )
            self.assertFalse(deleted)
            self.assertIn("boundary", reason)
            self.assertTrue(source.exists())

    def test_output_cannot_be_input_or_an_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "input"
            self.assertIsNone(output_location_error(root, root / "_compressed"))
            self.assertIsNotNone(output_location_error(root, root))
            self.assertIsNotNone(output_location_error(root, root.parent))

    def test_preflight_does_not_silently_fallback_from_fast(self) -> None:
        toolchain = Toolchain(Path("HandBrakeCLI"), None, None, "test", frozenset({"x265_10bit"}))
        self.assertTrue(preflight_mode(toolchain, MODES["fast"]))
        self.assertFalse(preflight_mode(toolchain, MODES["quality"]))

    def test_fps_command_decisions(self) -> None:
        self.assertEqual(fps_arguments(media(fps=60), MODES["quality"], 30), ["--rate", "30", "--pfr"])
        self.assertEqual(fps_arguments(media(fps=24), MODES["editing"], 30), ["--cfr"])

    def test_result_space_summary_handles_reduction_and_increase(self) -> None:
        self.assertEqual(result_space_from_summary("Compression reduction: 1.0 GB (50.0%)"), "Saved: 1.0 GB (50.0%)")
        self.assertEqual(result_space_from_summary("Compression increase: 1.0 GB (50.0%)"), "Used: 1.0 GB (50.0%)")
        self.assertEqual(result_space_from_summary("unrelated"), None)

    def test_integrity_decode_with_generated_temporary_media(self) -> None:
        ffmpeg = find_ffmpeg(None)
        if ffmpeg is None:
            self.skipTest("ffmpeg is not installed")
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fixture.mp4"
            subprocess.run(
                [
                    str(ffmpeg), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=16x16:rate=1", "-t", "1", "-c:v", "mpeg4", "-y", str(fixture),
                ],
                check=True,
            )
            valid, reason = decode_integrity(ffmpeg, fixture, 1.0)
            self.assertTrue(valid, reason)


if __name__ == "__main__":
    unittest.main()
