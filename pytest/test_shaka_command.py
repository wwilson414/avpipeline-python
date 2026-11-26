import unittest

from shaka_command import build_shaka_command
from ts_topology import ElementaryStream, Topology, select_audio_streams


def _stream(pid, media_type, audio_label=None, language=None):
    return ElementaryStream(pid, 0, media_type, "AAC" if media_type == "audio" else "H.264",
                            (), (), (), audio_label, language)


def _topology(audio=()):
    video = _stream(257, "video")
    audio = tuple(stream._replace(audio_ordinal=ordinal)
                  for ordinal, stream in enumerate(audio, 1))
    return Topology(1, 42, 256, video.pid, video, audio, (video,) + audio)


def _descriptors(command):
    return [arg for arg in command if arg.startswith("in=")]


class ShakaCommandTests(unittest.TestCase):
    def test_includes_discontinuity_signal_file_when_configured(self):
        signal_file = "/run/avpipeline/workers/channel-1_hls_discontinuity.signal"
        command = build_shaka_command(
            {"id": "channel-1", "transcode_mode": "transcode"},
            _topology(), 12000, "/streams/channel-1",
            discontinuity_signal_file=signal_file)

        self.assertEqual(
            command[command.index("--hls_discontinuity_signal_file") + 1],
            signal_file)
    def test_hls_and_dash_map_video_and_every_audio_with_explicit_metadata(self):
        topology = _topology((
            _stream(258, "audio", "English Main", "en"),
            _stream(259, "audio", "commentary", "fr"),
        ))
        command = build_shaka_command(
            {"id": "channel-1", "outputFormat": "hls",
             "transcode_mode": "transcode",
             "segment_duration": 2, "live_window_size": 10},
            topology, 12000, "/streams/channel-1")

        self.assertEqual(command[0], "packager")
        self.assertEqual(sum(arg == "packager" for arg in command), 1)
        self.assertFalse(any(arg.startswith("ffmpeg") for arg in command))
        descriptors = _descriptors(command)
        self.assertEqual(len(descriptors), 3)
        self.assertIn("in=udp://127.0.0.1:12000,stream=0", descriptors[0])
        self.assertIn("in=udp://127.0.0.1:12000,stream=1", descriptors[1])
        self.assertIn("in=udp://127.0.0.1:12000,stream=2", descriptors[2])
        self.assertIn(
            "segment_template=/streams/channel-1/channel-1_video_$Time$.m4s",
            descriptors[0])
        self.assertIn(
            "segment_template=/streams/channel-1/channel-1_audio-1_$Time$.m4s",
            descriptors[1])
        self.assertNotIn("stream_id=", " ".join(descriptors))
        self.assertIn("hls_name=English Main", descriptors[1])
        self.assertIn("dash_label=English Main", descriptors[1])
        self.assertIn("language=en", descriptors[1])
        self.assertIn("hls_name=commentary", descriptors[2])
        self.assertIn("dash_label=commentary", descriptors[2])
        self.assertIn("language=fr", descriptors[2])
        self.assertIn("playlist_name=channel-1_audio-1.m3u8", descriptors[1])
        self.assertIn("--add_program_date_time", command)
        self.assertEqual(command[command.index("--hls_playlist_type") + 1], "LIVE")
        self.assertEqual(command[command.index("--segment_duration") + 1], "2")
        self.assertEqual(
            command[command.index("--minimum_update_period") + 1],
            command[command.index("--segment_duration") + 1])
        self.assertEqual(command[command.index("--time_shift_buffer_depth") + 1], "20")
        self.assertEqual(
            command[command.index("--preserved_segments_outside_live_window") + 1],
            "10")
        self.assertEqual(command[command.index("--hls_master_playlist_output") + 1],
                         "/streams/channel-1/channel-1.m3u8")
        self.assertEqual(command[command.index("--mpd_output") + 1],
                         "/streams/channel-1/channel-1.mpd")
        self.assertEqual(command[-6:], [
            "--hls_playlist_type", "LIVE",
            "--hls_master_playlist_output",
            "/streams/channel-1/channel-1.m3u8",
            "--mpd_output", "/streams/channel-1/channel-1.mpd",
        ])
        self.assertTrue(all("segment_template=" in arg and "init_segment=" in arg
                            for arg in descriptors))

    def test_legacy_dash_format_still_creates_both_manifests(self):
        topology = _topology((_stream(258, "audio", "audio-0", "en"),))
        command = build_shaka_command(
            {"id": "channel-2", "outputFormat": "DASH",
             "transcode_mode": "transcode",
             "segment_duration": 2, "live_window_size": 10,
             "preserved_segments_outside_live_window": 4},
            topology, 12001, "/streams/channel-2")

        descriptors = _descriptors(command)
        self.assertEqual(len(descriptors), 2)
        self.assertIn("in=udp://127.0.0.1:12001,stream=0", descriptors[0])
        self.assertIn("in=udp://127.0.0.1:12001,stream=1", descriptors[1])
        self.assertNotIn("stream_id=", " ".join(descriptors))
        self.assertIn("hls_name=audio-0", descriptors[1])
        self.assertIn("dash_label=audio-0", descriptors[1])
        self.assertIn("language=en", descriptors[1])
        self.assertIn("playlist_name=channel-2_audio-1.m3u8", descriptors[1])
        self.assertIn("--hls_master_playlist_output", command)
        self.assertIn("--mpd_output", command)
        self.assertEqual(
            command[command.index("--preserved_segments_outside_live_window") + 1],
            "4")

    def test_video_only_topology_creates_one_stream_descriptor(self):
        command = build_shaka_command(
            {"id": "video-only", "transcode_mode": "transcode"},
            _topology(), 12002, "/streams/video-only")
        self.assertEqual(len(_descriptors(command)), 1)
        self.assertIn("--hls_master_playlist_output", command)
        self.assertIn("--mpd_output", command)

    def test_generation_id_namespaces_init_and_media_segments(self):
        topology = _topology((_stream(258, "audio", "English Main", "en"),))
        command = build_shaka_command(
            {"id": "channel-3", "transcode_mode": "transcode"},
            topology, 12005, "/streams/channel-3",
            generation_id="resume-2")

        descriptors = _descriptors(command)
        self.assertIn(
            "init_segment=/streams/channel-3/channel-3_video_gresume-2_init.mp4",
            descriptors[0])
        self.assertIn(
            "segment_template=/streams/channel-3/channel-3_video_gresume-2_$Time$.m4s",
            descriptors[0])
        self.assertIn(
            "init_segment=/streams/channel-3/channel-3_audio-1_gresume-2_init.mp4",
            descriptors[1])
        self.assertIn(
            "segment_template=/streams/channel-3/channel-3_audio-1_gresume-2_$Time$.m4s",
            descriptors[1])

    def test_transcode_uses_retained_output_indexes_and_source_ordinals(self):
        topology = _topology((
            _stream(258, "audio"),
            _stream(259, "audio", "Director commentary", "fr"),
            _stream(260, "audio"),
        ))

        for discard_first, expected_indexes, expected_names, expected_tracks in (
                (False, (1, 2, 3), ("audio-1", "Director commentary", "audio-3"),
                 ("audio-1", "audio-2", "audio-3")),
                (True, (1, 2), ("Director commentary", "audio-3"),
                 ("audio-2", "audio-3"))):
            with self.subTest(discard_first=discard_first):
                selection = select_audio_streams(topology, discard_first)
                command = build_shaka_command(
                    {"id": "transcode", "transcode_mode": "transcode"},
                    topology, 12006, "/streams/transcode", "file:///original.ts",
                    audio_selection=selection)
                descriptors = _descriptors(command)
                self.assertEqual(len(descriptors), len(expected_indexes) + 1)
                self.assertTrue(all(
                    descriptor.startswith("in=udp://127.0.0.1:12006,")
                    for descriptor in descriptors))
                self.assertNotIn("file:///original.ts", " ".join(descriptors))
                for descriptor, index, label, track in zip(
                        descriptors[1:], expected_indexes, expected_names,
                        expected_tracks):
                    self.assertIn(f"stream={index}", descriptor)
                    self.assertIn(f"hls_name={label}", descriptor)
                    self.assertIn(f"dash_label={label}", descriptor)
                    self.assertIn(f"playlist_name=transcode_{track}.m3u8", descriptor)

    def test_missing_or_invalid_mode_is_rejected(self):
        configs = (
            {"id": "missing-mode"},
            {"id": "none-mode", "transcode_mode": None},
            {"id": "empty-mode", "transcode_mode": ""},
            {"id": "unknown-mode", "transcode_mode": "automatic"},
        )
        for config in configs:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "transcode_mode"):
                    build_shaka_command(
                        config, _topology(), 12009, "/streams/invalid",
                        "file:///original.ts")

    def test_passthrough_ignores_opaque_encoders_and_uses_original_source(self):
        topology = _topology((_stream(258, "audio", "English Main", "en"),))
        invalid_values = (None, "", "none", "copy", {"opaque": "value"})
        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                command = build_shaka_command(
                    {"id": "passthrough", "transcode_mode": "passthrough",
                     "video_encoder": invalid_value,
                     "audio_encoder": invalid_value},
                    topology, 12010, "/streams/passthrough", "file:///original.ts")
                descriptors = _descriptors(command)
                self.assertEqual(len(descriptors), 2)
                self.assertTrue(all(
                    descriptor.startswith("in=file:///original.ts,")
                    for descriptor in descriptors))
                self.assertNotIn("127.0.0.1", " ".join(descriptors))
                self.assertIn("stream=0", descriptors[0])
                self.assertIn("stream=1", descriptors[1])
                self.assertIn("hls_name=English Main", descriptors[1])
                self.assertIn("dash_label=English Main", descriptors[1])
                self.assertIn("playlist_name=passthrough_audio-1.m3u8",
                              descriptors[1])

    def test_passthrough_uses_original_indexes_for_selected_audio(self):
        topology = _topology((
            _stream(258, "audio"),
            _stream(259, "audio", "Director commentary", "fr"),
            _stream(260, "audio"),
        ))
        topology = topology._replace(
            streams=(topology.audio[0], topology.video, topology.audio[1],
                     topology.audio[2]))

        for discard_first, expected_indexes, expected_names, expected_tracks in (
                (False, (0, 2, 3), ("audio-1", "Director commentary", "audio-3"),
                 ("audio-1", "audio-2", "audio-3")),
                (True, (2, 3), ("Director commentary", "audio-3"),
                 ("audio-2", "audio-3"))):
            with self.subTest(discard_first=discard_first):
                selection = select_audio_streams(topology, discard_first)
                command = build_shaka_command(
                    {"id": "passthrough", "transcode_mode": "passthrough"},
                    topology, 12007, "/streams/passthrough", "file:///input.ts",
                    audio_selection=selection)
                descriptors = _descriptors(command)
                self.assertIn("stream=1", descriptors[0])
                self.assertEqual(len(descriptors), len(expected_indexes) + 1)
                for descriptor, index, label, track in zip(
                        descriptors[1:], expected_indexes, expected_names,
                        expected_tracks):
                    self.assertIn(f"stream={index}", descriptor)
                    self.assertIn(f"hls_name={label}", descriptor)
                    self.assertIn(f"dash_label={label}", descriptor)
                    self.assertIn(
                        f"playlist_name=passthrough_{track}.m3u8", descriptor)

    def test_empty_audio_selection_is_video_only_in_both_modes(self):
        topologies = (_topology(), _topology((_stream(258, "audio"),)))
        for mode in ("transcode", "passthrough"):
            for topology in topologies:
                with self.subTest(mode=mode, audio_count=len(topology.audio)):
                    selection = select_audio_streams(
                        topology, bool(topology.audio))
                    command = build_shaka_command(
                        {"id": "video-only", "transcode_mode": mode},
                        topology, 12008, "/streams/video-only",
                        "file:///input.ts" if mode == "passthrough" else None,
                        audio_selection=selection)
                    descriptors = _descriptors(command)
                    self.assertEqual(len(descriptors), 1)
                    self.assertIn("stream=", descriptors[0])

    def test_direct_multicast_uses_topology_order_and_configured_interface(self):
        topology = _topology((_stream(258, "audio", "English Main", "en"),))
        topology = Topology(
            topology.transport_stream_id, topology.program_number, topology.pmt_pid,
            topology.pcr_pid, topology.video, topology.audio,
            (topology.audio[0], topology.video))
        command = build_shaka_command(
            {"id": "direct", "transcode_mode": "passthrough"},
            topology, 12003, "/streams/direct",
            "udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9",
            "192.0.2.25")

        descriptors = _descriptors(command)
        self.assertIn(
            "in=udp://239.1.1.1:5000?mode=listen&reuse=1,stream=1",
            descriptors[0])
        self.assertIn(
            "in=udp://239.1.1.1:5000?mode=listen&reuse=1,stream=0",
            descriptors[1])
        self.assertIn("hls_name=English Main", descriptors[1])
        self.assertIn("language=en", descriptors[1])
        self.assertNotIn("127.0.0.1", " ".join(descriptors))
        interface_index = command.index("--udp_interface_address")
        self.assertEqual(command[interface_index + 1], "192.0.2.25")
        self.assertEqual(command.count("--udp_interface_address"), 1)

    def test_direct_file_and_unicast_inputs_omit_multicast_interface(self):
        for source in ("file:///tmp/input.ts?localaddr=192.0.2.9",
                       "udp://@192.0.2.10:5000?localaddr=192.0.2.9"):
            with self.subTest(source=source):
                command = build_shaka_command(
                    {"id": "direct", "transcode_mode": "passthrough"},
                    _topology(), 12004, "/streams/direct", source, "192.0.2.25")
                descriptor = _descriptors(command)[0]
                expected_source = source.replace("@", "").split("?")[0]
                if source.startswith("udp://"):
                    expected_source += "?reuse=1"
                self.assertTrue(descriptor.startswith(f"in={expected_source},"))
                self.assertNotIn("localaddr", descriptor)
                self.assertNotIn("--udp_interface_address", command)


if __name__ == "__main__":
    unittest.main()
