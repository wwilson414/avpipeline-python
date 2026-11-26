import ast
import unittest
from pathlib import Path

from ffmpeg_command import build_ffmpeg_command
from ts_topology import ElementaryStream, Topology, select_audio_streams


def _stream(pid, media_type, codec, audio_ordinal=None):
    return ElementaryStream(
        pid, 0, media_type, codec, (), (), (), None, None, audio_ordinal)


def _topology(audio_pids=(258, 259)):
    video = _stream(257, "video", "H.264/AVC")
    audio = tuple(
        _stream(pid, "audio", "AAC", ordinal)
        for ordinal, pid in enumerate(audio_pids, 1))
    return Topology(1, 42, 256, video.pid, video, audio, (video,) + audio)


def _selection(audio_pids=(258, 259), discard_first_audio=False):
    return select_audio_streams(
        _topology(audio_pids), discard_first_audio=discard_first_audio)


class FFmpegCommandTests(unittest.TestCase):
    def test_builds_one_program_pipeline_for_discovered_video_and_all_audio(self):
        source = "udp://239.1.2.3:5000"
        command = build_ffmpeg_command(source, _selection(), 12000)

        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(command[command.index("-i") + 1], source)
        mapped_pids = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-map"
        ]
        self.assertEqual(mapped_pids, ["0:i:257", "0:i:258", "0:i:259"])
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(
            command[command.index("-af") + 1], "aresample=async=1")
        self.assertEqual(
            command[command.index("-dts_delta_threshold") + 1], "1")
        self.assertEqual(
            command[command.index("-fps_mode:v") + 1], "passthrough")
        self.assertNotIn("-timeout", command)
        self.assertNotIn("-copyts", command)
        self.assertNotIn("-use_wallclock_as_timestamps", command)
        self.assertNotIn("-itsoffset", command)
        self.assertNotIn("aresample=async=1:first_pts=0", command)
        self.assertNotIn("-r", command)
        self.assertNotIn("cfr", command)
        self.assertFalse(any(value.startswith("fps=") for value in command))
        self.assertEqual(command[-3:], ["-f", "mpegts", "udp://127.0.0.1:12000"])

    def test_udp_timestamp_discontinuity_correction_is_active_without_copyts_or_timeout(
            self):
        command = build_ffmpeg_command(
            "udp://239.1.2.3:5000", _selection(), 12000)

        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(
            command[command.index("-dts_delta_threshold") + 1], "1")
        self.assertNotIn("-copyts", command)
        self.assertNotIn("-timeout", command)
        mapped_pids = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-map"
        ]
        self.assertEqual(mapped_pids, ["0:i:257", "0:i:258", "0:i:259"])

    def test_file_inputs_also_keep_timestamp_discontinuity_correction_enabled(self):
        command = build_ffmpeg_command("file:input.ts", _selection(), 12000)

        self.assertEqual(
            command[command.index("-dts_delta_threshold") + 1], "1")
        self.assertNotIn("-copyts", command)
        self.assertNotIn("-timeout", command)

    def test_debug_ffmpeg_timestamps_enables_low_volume_progress_reporting(self):
        command = build_ffmpeg_command(
            "file:input.ts", _selection(), 12000,
            {"debug_ffmpeg_timestamps": True})

        self.assertEqual(command[command.index("-loglevel") + 1], "warning")
        self.assertEqual(command[command.index("-stats_period") + 1], "10")
        self.assertEqual(command[command.index("-progress") + 1], "pipe:2")
        self.assertIn("-nostats", command)
        self.assertNotIn("-debug_ts", command)

    def test_debug_ffmpeg_timestamps_is_disabled_by_default_and_requires_boolean(self):
        command = build_ffmpeg_command("file:input.ts", _selection(), 12000)

        self.assertEqual(command[command.index("-loglevel") + 1], "warning")
        self.assertNotIn("-progress", command)
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError, "debug_ffmpeg_timestamps must be a boolean"):
                build_ffmpeg_command(
                    "file:input.ts", _selection(), 12000,
                    {"debug_ffmpeg_timestamps": value})

    def test_transcode_command_uses_normalized_direct_input_and_local_udp_output(self):
        source = "udp://@239.1.2.3:5000?localaddr=192.0.2.25"
        command = build_ffmpeg_command(source, _selection(), 12000)

        self.assertEqual(command[command.index("-i") + 1], source)
        self.assertNotIn("pipe:0", command)
        self.assertEqual(command[-3:], ["-f", "mpegts", "udp://127.0.0.1:12000"])

    def test_uses_channel_encoder_bitrate_and_gop_settings(self):
        config = {
            "video_encoder": "avc",
            "audio_encoder": "aac",
            "video_average_bitrate": 8000000,
            "video_gop_size": 60,
            "video_keyint_min": 60,
        }
        command = build_ffmpeg_command(
            "file:input.ts", _selection(), 12000, config)

        expected = {
            "-c:v": "libx264",
            "-c:a": "aac",
            "-b:v": "8000000",
            "-maxrate:v": "11294118",
            "-bufsize:v": "22588236",
            "-g:v": "60",
            "-keyint_min:v": "60",
        }
        for option, value in expected.items():
            self.assertEqual(command[command.index(option) + 1], value)

    def test_omitted_video_and_audio_encoders_default_independently(self):
        for config in ({"audio_encoder": "aac"}, {"video_encoder": "avc"}):
            with self.subTest(config=config):
                command = build_ffmpeg_command(
                    "file:input.ts", _selection(), 12000, config)
                self.assertEqual(command[command.index("-c:v") + 1], "libx264")
                self.assertEqual(command[command.index("-c:a") + 1], "aac")

    def test_uses_configured_ffmpeg_encoder_names(self):
        command = build_ffmpeg_command(
            "file:input.ts", _selection(), 12000,
            {"video_encoder": "libx265", "audio_encoder": "libfdk_aac"})

        self.assertEqual(command[command.index("-c:v") + 1], "libx265")
        self.assertEqual(command[command.index("-c:a") + 1], "libfdk_aac")
        self.assertIn("-b:v", command)
        self.assertIn("-g:v", command)

    def test_accepts_all_encoder_aliases_with_normalized_ffmpeg_mappings(self):
        accepted = (
            ("video_encoder", "avc", "libx264"),
            ("video_encoder", " H264 ", "libx264"),
            ("video_encoder", "H.264", "libx264"),
            ("video_encoder", " LiBx264 ", "libx264"),
            ("video_encoder", "LIBX265", "libx265"),
            ("audio_encoder", " AaC ", "aac"),
            ("audio_encoder", "LIBFDK_AAC", "libfdk_aac"),
            ("audio_encoder", " OpUs ", "opus"),
        )
        for key, value, expected in accepted:
            with self.subTest(key=key, value=value):
                command = build_ffmpeg_command(
                    "file:input.ts", _selection(), 12000, {key: value})
                option = "-c:v" if key == "video_encoder" else "-c:a"
                self.assertEqual(command[command.index(option) + 1], expected)

    def test_rejects_invalid_explicit_encoder_values_independently(self):
        invalid_values = (
            None, "", " ", "\t", "none", "NONE", " NoNe ", "copy",
            "unsupported",
        )
        for encoder_key in ("video_encoder", "audio_encoder"):
            for value in invalid_values:
                config = {encoder_key: value}
                with self.subTest(config=config), self.assertRaises(ValueError):
                    build_ffmpeg_command(
                        "file:input.ts", _selection(), 12000, config)

    def test_rejects_non_string_encoder_values(self):
        for encoder_key in ("video_encoder", "audio_encoder"):
            for value in (0, False, [], {}):
                with self.subTest(encoder_key=encoder_key, value=value), \
                        self.assertRaises(ValueError):
                    build_ffmpeg_command(
                        "file:input.ts", _selection(), 12000,
                        {encoder_key: value})

    def test_legacy_per_track_ffmpeg_builders_and_launchers_are_removed(self):
        source_path = Path(__file__).resolve().parents[1] / "mc2slicing.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        referenced_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        self.assertNotIn("build_video_ffmpeg_cmd", function_names)
        self.assertNotIn("try_start_video_worker", function_names)
        self.assertNotIn("build_audio_ffmpeg_cmd", function_names)
        self.assertNotIn("try_start_audio_worker", function_names)
        self.assertNotIn("build_video_ffmpeg_cmd", referenced_names)
        self.assertNotIn("try_start_video_worker", referenced_names)
        self.assertNotIn("build_audio_ffmpeg_cmd", referenced_names)
        self.assertNotIn("try_start_audio_worker", referenced_names)
        for legacy_option in (
                "-use_wallclock_as_timestamps",
                "aresample=async=1:first_pts=0",
                "-af",
                "-hls_time",
                "-hls_list_size",
                "-hls_segment_type",
                "-hls_flags",
                "-hls_segment_filename",
                "-hls_start_number_source",
                "-seg_duration",
                "-window_size",
                "-extra_window_size",
                "-remove_at_exit",
                "-use_template",
                "-use_timeline",
                "-utc_timing_url",
                "-dash_segment_type",
                "-init_seg_name",
                "-media_seg_name",
                '"-f", "hls"',
                '"-f", "dash"'):
            self.assertNotIn(legacy_option, source)

    def test_decoder_ignores_errors_and_emits_pre_keyframe_frames(self):
        command = build_ffmpeg_command("file:input.ts", _selection(()), 12001)

        input_index = command.index("-i")
        self.assertLess(command.index("-err_detect:v"), input_index)
        self.assertLess(command.index("-flags:v"), input_index)
        self.assertLess(command.index("-flags2:v"), input_index)
        self.assertEqual(command[command.index("-err_detect:v") + 1],
                         "ignore_err")
        self.assertEqual(command[command.index("-flags:v") + 1],
                         "+output_corrupt")
        self.assertEqual(command[command.index("-flags2:v") + 1], "+showall")
        self.assertEqual(command.count("-map"), 1)
        self.assertEqual(command[command.index("-map") + 1], "0:i:257")
        self.assertFalse(any(option in command for option in
                             ("-skip_frame", "-discard", "-vf", "-filter:v")))

    def test_emits_only_interleaved_mpegts_udp_not_hls_or_dash(self):
        command = build_ffmpeg_command("file:input.ts", _selection(), 12002)

        self.assertEqual(command[-3:], ["-f", "mpegts", "udp://127.0.0.1:12002"])
        self.assertFalse(any("hls" in value.lower() or "dash" in value.lower()
                             for value in command))

    def test_rejects_invalid_udp_port(self):
        with self.assertRaisesRegex(ValueError, "UDP port"):
            build_ffmpeg_command(
                "udp://239.1.2.3:5000", _selection(), 65536)

    def test_maps_only_retained_audio_in_source_order(self):
        selection = _selection((258, 259, 260), discard_first_audio=True)
        command = build_ffmpeg_command("file:input.ts", selection, 12003)
        mapped_pids = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-map"
        ]

        self.assertEqual(mapped_pids, ["0:i:257", "0:i:259", "0:i:260"])

    def test_discarding_only_audio_maps_video_only(self):
        selection = _selection((258,), discard_first_audio=True)
        command = build_ffmpeg_command("file:input.ts", selection, 12004)
        mapped_pids = [
            command[index + 1]
            for index, value in enumerate(command[:-1])
            if value == "-map"
        ]

        self.assertEqual(mapped_pids, ["0:i:257"])


if __name__ == "__main__":
    unittest.main()
