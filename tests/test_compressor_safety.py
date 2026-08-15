from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import espresso_compresso_cli as cli
from espresso_compresso import (
    activity_from_output,
    deletion_choice_for_scope,
    parse_terminal_result,
    removal_result_message,
    result_space_from_summary,
    technical_log_path_from_output,
)
from espresso_compresso_cli import (
    BatchLogger,
    BatchStats,
    MODES,
    MediaInfo,
    Toolchain,
    VideoTask,
    build_tasks,
    child_process_options,
    decode_integrity,
    discard_larger_temporary,
    delete_source_safely,
    discover_videos,
    emit_terminal_result,
    find_ffmpeg,
    fps_arguments,
    maybe_delete_original,
    observe_free_space,
    output_location_error,
    preflight_mode,
    terminal_result,
    validate_output,
)


def media(*, fps: float = 30.0, duration: float = 60.0) -> MediaInfo:
    return MediaInfo("hevc", 1920, 1080, fps, duration, 1, 1, 0, (2,))


class CompressorSafetyTests(unittest.TestCase):
    def test_child_process_options_hide_windows_tools_and_keep_groups(self) -> None:
        ordinary = child_process_options("nt")
        grouped = child_process_options("nt", grouped=True)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        self.assertEqual(ordinary, {"creationflags": no_window})
        self.assertEqual(grouped, {"creationflags": no_window | new_group})

    def test_child_process_options_keep_posix_inspection_ordinary(self) -> None:
        self.assertEqual(child_process_options("posix"), {})
        self.assertEqual(child_process_options("posix", grouped=True), {"start_new_session": True})

    def test_activity_filters_technical_output_and_keeps_meaningful_events(self) -> None:
        self.assertEqual(activity_from_output("[2/3] Holiday clip.mp4"), "File 2 of 3: Holiday clip.mp4")
        self.assertEqual(
            activity_from_output("VALIDATED: 1.0 GB (45.2% smaller)."),
            "Validated: 1.0 GB (45.2% smaller).",
        )
        self.assertEqual(
            activity_from_output("Tracks preserved: video 1, audio 2, subtitles 1."),
            "Tracks preserved: video 1, audio 2, subtitles 1.",
        )
        self.assertEqual(
            activity_from_output("  ORIGINAL KEPT: source changed during encoding"),
            "Original kept by safety checks.",
        )
        self.assertEqual(
            activity_from_output("FAILED: Could not start HandBrakeCLI: C:\\private\\tool.exe"),
            "A file needs attention. Open the technical log for details.",
        )
        self.assertIsNone(activity_from_output("HandBrakeCLI --input C:\\private\\clip.mp4 --verbose"))

    def test_activity_final_counts_and_technical_log_path_are_display_free(self) -> None:
        result_line = "RESULT_JSON: " + cli.json.dumps({
            "encoded": 2, "existing_verified": 1, "no_benefit": 1, "failed": 0,
        })
        self.assertEqual(
            activity_from_output(result_line),
            "Finished: 2 compressed • 1 already complete • 1 no-size-benefit • 0 need attention",
        )
        self.assertEqual(
            technical_log_path_from_output("  Log: C:\\Logs\\compression_log_20260815_132017.txt"),
            Path("C:\\Logs\\compression_log_20260815_132017.txt"),
        )
        self.assertIsNone(technical_log_path_from_output("Logging to: C:\\Logs\\fallback.txt"))

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
            stats = BatchStats()
            try:
                deleted = maybe_delete_original(
                    SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                    source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                    stats, logger, False, None, media(),
                )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertTrue(source.exists())
            self.assertEqual(stats.originals_deleted, 0)
            self.assertEqual(stats.deletion_kept, 1)

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
            stats = BatchStats()
            try:
                deleted = maybe_delete_original(
                    SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                    source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                    stats, logger, True, None, media(),
                )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertTrue(source.exists())
            self.assertEqual(stats.originals_deleted, 0)
            self.assertEqual(stats.deletion_kept, 1)

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

    def test_changed_source_stays_and_is_counted_as_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = root / "source.mp4"
            destination = output / "source.compressed.mkv"
            source.write_bytes(b"source data is larger")
            destination.write_bytes(b"small")
            source_stat = source.stat()
            source.write_bytes(b"source data changed after encoding")
            logger = BatchLogger(output / "test.log", output / "fallback.log")
            stats = BatchStats()
            try:
                with patch("espresso_compresso_cli.decode_integrity", return_value=(True, "")):
                    deleted = maybe_delete_original(
                        SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                        source_stat.st_size, source_stat.st_mtime_ns, destination.stat().st_size,
                        stats, logger, True, Path("ffmpeg"), media(),
                    )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertTrue(source.exists())
            self.assertEqual(stats.originals_deleted, 0)
            self.assertEqual(stats.deletion_kept, 1)

    def test_larger_output_stays_and_is_counted_as_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "_compressed"
            output.mkdir()
            source = root / "source.mp4"
            destination = output / "source.compressed.mkv"
            source.write_bytes(b"small")
            destination.write_bytes(b"larger output")
            stat = source.stat()
            logger = BatchLogger(output / "test.log", output / "fallback.log")
            stats = BatchStats()
            try:
                deleted = maybe_delete_original(
                    SimpleNamespace(delete_originals=True), VideoTask(source, destination), root, output,
                    stat.st_size, stat.st_mtime_ns, destination.stat().st_size,
                    stats, logger, True, Path("ffmpeg"), media(),
                )
            finally:
                logger.close()
            self.assertFalse(deleted)
            self.assertEqual(stats.originals_deleted, 0)
            self.assertEqual(stats.deletion_kept, 1)

    def test_larger_output_cleanup_failure_counts_the_kept_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temporary = root / "source.partial.mkv"
            source = root / "source.mp4"
            temporary.write_bytes(b"larger output")
            source.write_bytes(b"source")
            logger = BatchLogger(root / "test.log", root / "fallback.log")
            stats = BatchStats()
            try:
                with patch.object(Path, "unlink", side_effect=OSError("locked")):
                    discarded = discard_larger_temporary(
                        temporary, source, 20.0, SimpleNamespace(delete_originals=True), stats, logger,
                    )
            finally:
                logger.close()
            self.assertFalse(discarded)
            self.assertTrue(source.exists())
            self.assertEqual(stats.failed, 1)
            self.assertEqual(stats.deletion_kept, 1)

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
        self.assertEqual(result_space_from_summary("Compression reduction: 1.0 GB (50.0%)"), "Compression reduction: 1.0 GB (50.0%)")
        self.assertEqual(result_space_from_summary("Compression increase: 1.0 GB (50.0%)"), "Compression increase: 1.0 GB (50.0%)")
        self.assertEqual(result_space_from_summary("unrelated"), None)

    def test_three_file_scope_forces_keep_and_full_scope_requires_explicit_delete(self) -> None:
        self.assertEqual(deletion_choice_for_scope("test", "delete"), "keep")
        self.assertEqual(deletion_choice_for_scope("all", "keep"), "keep")
        self.assertEqual(deletion_choice_for_scope("all", "delete"), "delete")

    def test_structured_terminal_result_parsing_and_mixed_counts(self) -> None:
        result = {
            "outcome": "complete-with-issues",
            "deletion_requested": True,
            "encoded": 2,
            "existing_verified": 1,
            "originals_deleted": 1,
            "originals_retained_by_safeguards": 2,
            "failed": 1,
            "no_benefit": 1,
            "compression_reduction_bytes": 900,
            "known_output_bytes": 100,
            "free_space_volumes": [{"volume": "C:\\\\", "change_bytes": 50}],
        }
        line = "RESULT_JSON: " + cli.json.dumps(result)
        self.assertEqual(parse_terminal_result(line), result)
        title, message, attention = removal_result_message(result)
        self.assertEqual(title, "Original removal results")
        self.assertTrue(attention)
        self.assertIn("Originals deleted: 1", message)
        self.assertIn("Originals kept by safety checks: 2", message)
        self.assertIn("Available disk space increased", message)

    def test_removal_popup_words_positive_zero_and_negative_space_separately(self) -> None:
        base = {
            "outcome": "complete",
            "originals_deleted": 1,
            "originals_retained_by_safeguards": 0,
            "failed": 0,
            "known_output_bytes": 100,
            "compression_reduction_bytes": 900,
        }
        for change, expected in [
            (100, "Available disk space increased"),
            (0, "No net disk space was freed"),
            (-100, "additional space used"),
        ]:
            result = {**base, "free_space_volumes": [{"volume": "C:\\\\", "change_bytes": change}]}
            _, message, _ = removal_result_message(result)
            self.assertIn(expected, message)

    def test_stopped_result_uses_attention_wording(self) -> None:
        result = {
            "outcome": "stopped", "originals_deleted": 1,
            "originals_retained_by_safeguards": 0, "failed": 0,
            "known_output_bytes": 100, "compression_reduction_bytes": 900,
            "free_space_volumes": [{"volume": "C:\\\\", "change_bytes": 20}],
        }
        title, message, attention = removal_result_message(result)
        self.assertEqual(title, "Original removal results")
        self.assertTrue(attention)
        self.assertIn("Unprocessed originals remain unchanged", message)

    def test_posix_free_space_observations_keep_distinct_devices(self) -> None:
        first = unittest.mock.MagicMock()
        first.resolve.return_value = first
        first.stat.return_value = SimpleNamespace(st_dev=101)
        first.drive = ""
        first.anchor = "/"
        first.__str__.return_value = "/mnt/recordings"
        second = unittest.mock.MagicMock()
        second.resolve.return_value = second
        second.stat.return_value = SimpleNamespace(st_dev=202)
        second.drive = ""
        second.anchor = "/"
        second.__str__.return_value = "/mnt/archive"
        with patch("espresso_compresso_cli._existing_directory", side_effect=[first, second]), \
             patch("espresso_compresso_cli.shutil.disk_usage", side_effect=[
                 SimpleNamespace(free=100), SimpleNamespace(free=200),
             ]):
            observed = observe_free_space([Path("first"), Path("second")])
        self.assertEqual(observed, {"/mnt/recordings": 100, "/mnt/archive": 200})

    def test_terminal_result_has_stable_required_fields(self) -> None:
        stats = BatchStats(encoded=2, existing_verified=1, originals_deleted=1, deletion_kept=1,
                           failed=1, no_benefit=1, input_bytes=1000, output_bytes=100)
        with patch("espresso_compresso_cli.free_space_results", return_value=[]):
            result = terminal_result("complete-with-issues", stats, True, {}, [Path.cwd()])
        self.assertEqual(result["compression_reduction_bytes"], 900)
        self.assertEqual(result["known_output_bytes"], 100)
        self.assertEqual(result["originals_deleted"], 1)
        self.assertEqual(result["originals_retained_by_safeguards"], 1)

    def test_terminal_result_is_written_to_the_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logger = BatchLogger(root / "run.log", root / "fallback.log")
            try:
                with patch("espresso_compresso_cli.free_space_results", return_value=[]):
                    emit_terminal_result("complete", BatchStats(), True, {}, [root], logger)
            finally:
                logger.close()
            self.assertIn("RESULT_JSON:", (root / "run.log").read_text(encoding="utf-8"))

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
