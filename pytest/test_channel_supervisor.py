import io
import importlib
import json
import logging
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from urllib.parse import parse_qsl, urlsplit
from unittest.mock import patch

import mc2slicing

from mc2slicing import (
    ChannelSupervisor, InputRecovery, check_packager_version, compute_backoff,
    main, resolve_transcode_mode, topology_input,
)
from ts_topology import (
    ElementaryStream, Topology, TopologyError, TopologyMonitorEvent,
    TopologyUnavailableError,
)


class LoggingIsolationTests(unittest.TestCase):
    def test_simulator_and_channel_logs_ignore_preconfigured_root_file_handler(self):
        root_logger = logging.getLogger()
        root_handlers = list(root_logger.handlers)
        named_logger = logging.getLogger("avpipeline")
        old_named_handlers = list(named_logger.handlers)
        old_level = named_logger.level
        old_propagate = named_logger.propagate

        with tempfile.TemporaryDirectory() as directory:
            main_log = os.path.join(directory, "avpipeline.log")
            unrelated_log = os.path.join(directory, "unrelated.log")
            channel_log = os.path.join(directory, "channel-1.log")
            unrelated_handler = logging.FileHandler(unrelated_log)
            root_logger.addHandler(unrelated_handler)
            for handler in old_named_handlers:
                named_logger.removeHandler(handler)

            original_file_handler = logging.FileHandler
            configured_paths = []

            def temporary_file_handler(configured_path, *args, **kwargs):
                configured_paths.append(configured_path)
                return original_file_handler(main_log, *args, **kwargs)

            try:
                with patch.object(mc2slicing.logging, "FileHandler",
                                  side_effect=temporary_file_handler), \
                        patch.object(mc2slicing.os, "makedirs"):
                    importlib.reload(mc2slicing)

                with patch.object(mc2slicing, "load_config",
                                  return_value={"multiviewChannels": []}), \
                        patch.object(mc2slicing, "generate_url_list"):
                    mc2slicing.start_tasks()

                with patch.object(mc2slicing.shutil, "which", return_value=None), \
                        redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit):
                    mc2slicing.check_dependencies()

                class FakeRecoverySupervisor:
                    input_lost = False

                    def step(self, now):
                        pass

                    def close(self):
                        pass

                discovery_count = [0]

                def discoverer(plugin, input_args):
                    discovery_count[0] += 1
                    if discovery_count[0] == 1:
                        raise TopologyUnavailableError("input not ready")
                    return ("topology",)

                with open(channel_log, "w", encoding="utf-8") as channel_output:
                    recovery = mc2slicing.InputRecovery(
                        "channel-1", "plugin", [], lambda topology: {},
                        channel_output, discoverer=discoverer,
                        supervisor_factory=lambda *args: FakeRecoverySupervisor())
                    recovery.step(now=0)
                    retry_at = recovery.next_probe_ts
                    recovery.step(now=retry_at)
                    recovery.close()

                for handler in named_logger.handlers:
                    handler.flush()
                unrelated_handler.flush()

                self.assertEqual(configured_paths, [
                    os.path.join(mc2slicing.LOG_DIR, "avpipeline.log")])
                with open(main_log, encoding="utf-8") as log_file:
                    main_contents = log_file.read()
                with open(channel_log, encoding="utf-8") as log_file:
                    channel_contents = log_file.read()
                with open(unrelated_log, encoding="utf-8") as log_file:
                    unrelated_contents = log_file.read()

                self.assertIn("Starting main channel avpipeline tasks", main_contents)
                self.assertIn("CRITICAL ERROR: Required tool(s) not found", main_contents)
                self.assertIn("input topology unavailable", main_contents)
                self.assertIn(
                    "[WARNING] channel-1 manager: waiting:", channel_contents)
                self.assertIn(
                    "[INFO] channel-1 manager: running:", channel_contents)
                self.assertEqual(unrelated_contents, "")
                self.assertIn(unrelated_handler, root_logger.handlers)
            finally:
                for handler in list(named_logger.handlers):
                    named_logger.removeHandler(handler)
                    handler.close()
                for handler in old_named_handlers:
                    named_logger.addHandler(handler)
                named_logger.setLevel(old_level)
                named_logger.propagate = old_propagate
                root_logger.removeHandler(unrelated_handler)
                unrelated_handler.close()
                self.assertEqual(root_logger.handlers, root_handlers)


class OutputCleanupTests(unittest.TestCase):
    def test_removes_generated_segments_manifests_and_init_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            channel_dir = os.path.join(directory, "ch1")
            os.makedirs(channel_dir)
            generated = (
                "ch1.m3u8", "ch1_master.m3u8", "ch1.mpd",
                "ch1_video.m3u8", "ch1_video_init.mp4", "ch1_video_12.m4s",
                "ch1_audio-0.m3u8", "ch1_audio-0_init.mp4",
                "ch1_audio-0_3.m4s", "ch1_video_gold_init.mp4",
                "ch1_video_gold_13.m4s", "ch1_audio-0_gold_init.mp4",
                "ch1_audio-0_gold_4.m4s",
            )
            for name in generated + ("notes.txt", "other_video_1.m4s",
                                     "ch1_unrelated.m4s"):
                with open(os.path.join(channel_dir, name), "w",
                          encoding="utf-8") as output_file:
                    output_file.write("test")

            removed = mc2slicing.cleanup_channel_output_files(directory, "ch1")

            self.assertEqual(removed, len(generated))
            self.assertFalse(any(os.path.exists(os.path.join(channel_dir, name))
                                 for name in generated))
            self.assertTrue(os.path.exists(os.path.join(channel_dir, "notes.txt")))
            self.assertTrue(os.path.exists(
                os.path.join(channel_dir, "other_video_1.m4s")))
            self.assertTrue(os.path.exists(
                os.path.join(channel_dir, "ch1_unrelated.m4s")))

    def test_shaka_start_retires_previous_generation_files_only(self):
        with tempfile.TemporaryDirectory() as directory:
            channel_dir = os.path.join(directory, "ch1")
            os.makedirs(channel_dir)
            old_files = (
                "ch1_video_gold_init.mp4", "ch1_video_gold_13.m4s",
                "ch1_audio-0_gold_init.mp4", "ch1_audio-0_gold_4.m4s",
            )
            active_files = (
                "ch1_video_gactive_init.mp4", "ch1_video_gactive_20.m4s",
                "ch1_audio-0_gactive_init.mp4", "ch1_audio-0_gactive_5.m4s",
            )
            for name in old_files + active_files + ("ch1.m3u8", "keep.txt"):
                with open(os.path.join(channel_dir, name), "w",
                          encoding="utf-8") as output_file:
                    output_file.write("test")

            command = [
                "packager",
                f"in=udp://127.0.0.1:12000,stream=0,"
                f"init_segment={os.path.join(channel_dir, 'ch1_video_gactive_init.mp4')},"
                f"segment_template={os.path.join(channel_dir, 'ch1_video_gactive_$Time$.m4s')}",
            ]

            def start_worker(process_command, **kwargs):
                self.assertTrue(all(os.path.exists(
                    os.path.join(channel_dir, name)) for name in old_files))
                return FakeProcess(process_command, [])

            supervisor = ChannelSupervisor(
                "ch1", {"shaka": command}, io.StringIO(),
                popen=start_worker)
            with patch("mc2slicing.WORKER_PID_DIR", directory):
                supervisor.step(now=0)
                try:
                    self.assertFalse(any(os.path.exists(
                        os.path.join(channel_dir, name))
                                         for name in old_files))
                    self.assertTrue(all(os.path.exists(
                        os.path.join(channel_dir, name))
                                        for name in active_files))
                    self.assertTrue(os.path.exists(
                        os.path.join(channel_dir, "ch1.m3u8")))
                    self.assertTrue(os.path.exists(
                        os.path.join(channel_dir, "keep.txt")))
                finally:
                    supervisor.close()

    def test_stop_tasks_cleans_configured_outputs_after_workers_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            channel_dir = os.path.join(directory, "ch1")
            os.makedirs(channel_dir)
            generated = os.path.join(channel_dir, "ch1_video_1.m4s")
            unrelated = os.path.join(channel_dir, "keep.txt")
            for path in (generated, unrelated):
                with open(path, "w", encoding="utf-8") as output_file:
                    output_file.write("test")
            config = {
                "baseDir": directory,
                "multiviewChannels": [{"id": "ch1", "enabled": False}],
            }
            with patch.object(mc2slicing, "load_config", return_value=config), \
                    patch.object(mc2slicing, "PID_DIR",
                                 os.path.join(directory, "pids")), \
                    patch.object(mc2slicing, "WORKER_PID_DIR",
                                 os.path.join(directory, "worker-pids")), \
                    patch.object(mc2slicing.time, "sleep"), \
                    patch.object(mc2slicing.subprocess, "run",
                                 return_value=subprocess.CompletedProcess(
                                     [], 1, stdout="", stderr="")):
                mc2slicing.stop_tasks()

            self.assertFalse(os.path.exists(generated))
            self.assertTrue(os.path.exists(unrelated))

    def test_rejects_channel_ids_that_escape_output_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "path component"):
                mc2slicing.cleanup_channel_output_files(directory, "../outside")


class ConfigArgumentTests(unittest.TestCase):
    def test_url_index_lists_both_manifests(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {
                "baseDir": directory,
                "multiviewChannels": [{"id": "ch1", "outputFormat": "dash"}],
            }
            with patch.object(mc2slicing, "load_config", return_value=config):
                mc2slicing.generate_url_list()

            with open(os.path.join(directory, "main_urls.json"),
                      encoding="utf-8") as url_file:
                urls = json.load(url_file)

        channel = urls["channels"][0]
        self.assertEqual(channel["format"], "cmaf")
        self.assertEqual(channel["playlist"], "ch1/ch1.m3u8")
        self.assertEqual(channel["master_playlist"], "ch1/ch1.m3u8")
        self.assertEqual(channel["dash_playlist"], "ch1/ch1.mpd")

    def test_config_argument_before_or_after_command(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "custom-mvc.json")
            for arguments in (
                    ["--config", config_path, "start"],
                    ["start", "--config", config_path]):
                selected = []
                with self.subTest(arguments=arguments), \
                        patch.object(mc2slicing, "JSON_FILE", "mvc.json"), \
                        patch.object(sys, "argv", ["mc2slicing.py", *arguments]), \
                        patch.object(mc2slicing, "check_dependencies"), \
                        patch.object(mc2slicing, "start_tasks",
                                     side_effect=lambda: selected.append(
                                         mc2slicing.JSON_FILE)):
                    mc2slicing.main()
                self.assertEqual(selected, [os.path.abspath(config_path)])

    def test_selected_config_is_passed_to_manager_process(self):
        config_path = "/tmp/custom-mvc.json"
        manager_process = type("ManagerProcess", (), {"pid": 4321})()
        with patch.object(mc2slicing, "JSON_FILE", "mvc.json"), \
                patch.object(sys, "argv", [
                    "mc2slicing.py", "--config", config_path, "start"]), \
                patch.object(mc2slicing, "check_dependencies"), \
                patch.object(mc2slicing, "load_config",
                             return_value={"multiviewChannels": [{
                                 "id": "ch1", "transcode_mode": "transcode"}]}), \
                patch.object(mc2slicing, "get_pid", return_value=None), \
                patch.object(mc2slicing, "save_pid"), \
                patch.object(mc2slicing, "generate_url_list"), \
                patch.object(mc2slicing.subprocess, "Popen",
                             return_value=manager_process) as popen:
            mc2slicing.main()

        command = popen.call_args.args[0]
        self.assertIn("--config", command)
        config_index = command.index("--config")
        self.assertEqual(command[config_index + 1], config_path)
        self.assertIn("--manager", command)
        self.assertEqual(command[-1], "ch1")

    def test_start_tasks_only_starts_enabled_channels(self):
        manager_process = type("ManagerProcess", (), {"pid": 4321})()
        config = {
            "multiviewChannels": [
                {
                    "id": "disabled",
                    "enabled": False,
                    "transcode_mode": "invalid",
                    "video_encoder": "hevc",
                    "audio_encoder": "vorbis",
                },
                {
                    "id": "enabled",
                    "enabled": True,
                    "transcode_mode": "transcode",
                },
            ]
        }
        with patch.object(mc2slicing, "load_config", return_value=config), \
                patch.object(mc2slicing, "get_pid", return_value=None), \
                patch.object(mc2slicing, "save_pid"), \
                patch.object(mc2slicing, "generate_url_list"), \
                patch.object(mc2slicing.subprocess, "Popen",
                             return_value=manager_process) as popen:
            mc2slicing.start_tasks()

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(popen.call_args.args[0][-1], "enabled")

    def test_start_tasks_rejects_invalid_enabled_config_before_any_launch(self):
        invalid_channels = (
            ("missing mode", {"id": "ch1"}),
            ("invalid mode", {"id": "ch1", "transcode_mode": "unknown"}),
            ("invalid video encoder", {
                "id": "ch1", "transcode_mode": "transcode",
                "video_encoder": "hevc",
            }),
            ("invalid audio encoder", {
                "id": "ch1", "transcode_mode": "transcode",
                "audio_encoder": "vorbis",
            }),
        )
        for label, channel in invalid_channels:
            with self.subTest(configuration=label), \
                    patch.object(mc2slicing, "load_config",
                                 return_value={"multiviewChannels": [channel]}), \
                    patch.object(mc2slicing.subprocess, "Popen") as popen:
                with self.assertRaises(SystemExit) as exit_error:
                    mc2slicing.start_tasks()

            self.assertEqual(exit_error.exception.code, 1)
            popen.assert_not_called()

    def test_start_tasks_later_invalid_enabled_channel_prevents_all_launches(self):
        config = {
            "multiviewChannels": [
                {"id": "valid", "transcode_mode": "transcode"},
                {
                    "id": "invalid", "transcode_mode": "transcode",
                    "audio_encoder": "vorbis",
                },
            ]
        }
        with patch.object(mc2slicing, "load_config", return_value=config), \
                patch.object(mc2slicing.subprocess, "Popen") as popen:
            with self.assertRaises(SystemExit) as exit_error:
                mc2slicing.start_tasks()

        self.assertEqual(exit_error.exception.code, 1)
        popen.assert_not_called()

    def test_start_tasks_preflights_valid_enabled_modes(self):
        manager_process = type("ManagerProcess", (), {"pid": 4321})()
        config = {
            "multiviewChannels": [
                {"id": "transcode", "transcode_mode": "transcode"},
                {
                    "id": "passthrough",
                    "transcode_mode": "passthrough",
                    "video_encoder": "hevc",
                    "audio_encoder": "vorbis",
                },
            ]
        }
        with patch.object(mc2slicing, "load_config", return_value=config), \
                patch.object(mc2slicing, "get_pid", return_value=None), \
                patch.object(mc2slicing, "save_pid"), \
                patch.object(mc2slicing, "generate_url_list"), \
                patch.object(mc2slicing.subprocess, "Popen",
                             return_value=manager_process) as popen:
            mc2slicing.start_tasks()

        self.assertEqual(
            [call.args[0][-1] for call in popen.call_args_list],
            ["transcode", "passthrough"],
        )

    def test_config_option_without_value_exits_before_startup(self):
        with patch.object(sys, "argv", ["mc2slicing.py", "start", "--config"]), \
                patch.object(mc2slicing, "check_dependencies") as check_dependencies, \
                patch.object(mc2slicing, "start_tasks") as start_tasks, \
                patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as exit_error:
                mc2slicing.main()

        self.assertEqual(exit_error.exception.code, 2)
        check_dependencies.assert_not_called()
        start_tasks.assert_not_called()


class FakeProcess:
    next_pid = 100

    def __init__(self, command, events):
        self.command = command
        self.events = events
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = None
        events.append(("start", command[0], self.pid))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.events.append(("terminate", self.command[0], self.pid))
        self.returncode = 0

    def kill(self):
        self.events.append(("kill", self.command[0], self.pid))
        self.returncode = -9


class ChannelSupervisorTests(unittest.TestCase):
    def setUp(self):
        FakeProcess.next_pid = 100
        self.directory = tempfile.TemporaryDirectory()
        self.events = []
        self.output = io.StringIO()
        self.supervisor = ChannelSupervisor(
            "test", {"shaka": ["packager"], "ffmpeg": ["ffmpeg"]},
            self.output,
            popen=lambda command, **kwargs: FakeProcess(command, self.events))
        self.pid_dir_patch = patch("mc2slicing.WORKER_PID_DIR", self.directory.name)
        self.pid_dir_patch.start()

    def tearDown(self):
        self.pid_dir_patch.stop()
        self.supervisor.close()
        self.directory.cleanup()

    def test_explicit_transcode_mode_is_returned(self):
        self.assertEqual(resolve_transcode_mode({
            "transcode_mode": "transcode",
            "video_encoder": "none",
            "audio_encoder": "none",
        }), "transcode")
        self.assertEqual(resolve_transcode_mode({
            "transcode_mode": "passthrough",
            "video_encoder": "avc",
            "audio_encoder": "aac",
        }), "passthrough")

    def test_rejects_invalid_transcode_mode_values(self):
        for value in ("", "automatic", "TRANSCODE", 1, None, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_transcode_mode({"transcode_mode": value})

    def test_shared_channel_resolver_requires_explicit_mode(self):
        for config in (
                {},
                {"video_encoder": "avc"},
                {"audio_encoder": "aac"},
                {"video_encoder": "none", "audio_encoder": "none"},
                {"transcode_mode": None},
                {"transcode_mode": "automatic"}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                mc2slicing.resolve_channel_config(config)

    def test_passthrough_supervises_only_shaka_and_retries_it(self):
        supervisor = ChannelSupervisor(
            "passthrough", {"shaka": ["packager"]}, self.output,
            popen=lambda command, **kwargs: FakeProcess(command, self.events))
        shaka_pid_file = os.path.join(
            self.directory.name, "passthrough_shaka.pid")
        ffmpeg_pid_file = os.path.join(
            self.directory.name, "passthrough_ffmpeg.pid")

        try:
            self.assertEqual(tuple(supervisor.workers), ("shaka",))
            supervisor.step(now=0)
            self.assertEqual([event[1] for event in self.events
                              if event[0] == "start"], ["packager"])
            self.assertTrue(os.path.exists(shaka_pid_file))
            self.assertFalse(os.path.exists(ffmpeg_pid_file))

            supervisor.workers["shaka"]["proc"].returncode = 2
            supervisor.step(now=1)
            state = supervisor.workers["shaka"]
            self.assertEqual(state["exit_status"], 2)
            self.assertEqual(state["restart_count"], 1)
            supervisor.step(now=state["next_retry_ts"])

            supervisor.close()
            self.assertFalse(os.path.exists(shaka_pid_file))
            self.assertFalse(os.path.exists(ffmpeg_pid_file))
            self.assertEqual([event[1] for event in self.events
                              if event[0] == "terminate"], ["packager"])
            self.assertNotIn("ffmpeg", self.output.getvalue())
        finally:
            supervisor.close()

    def test_starts_packager_before_single_ffmpeg_and_stops_in_reverse_order(self):
        self.supervisor.step(now=0)
        self.assertEqual([event[1] for event in self.events if event[0] == "start"],
                         ["packager", "ffmpeg"])
        self.assertEqual(self.supervisor.workers["shaka"]["pid"], 100)
        self.assertEqual(self.supervisor.workers["ffmpeg"]["pid"], 101)
        self.assertTrue(os.path.exists(os.path.join(self.directory.name, "test_shaka.pid")))
        self.assertTrue(os.path.exists(os.path.join(self.directory.name, "test_ffmpeg.pid")))

        self.supervisor.close()
        self.assertEqual([event[1] for event in self.events if event[0] == "terminate"],
                         ["ffmpeg", "packager"])
        self.assertIsNone(self.supervisor.workers["ffmpeg"]["proc"])
        self.assertIsNone(self.supervisor.workers["shaka"]["proc"])

    def test_ffmpeg_uses_independent_source_without_tsdduck_stdin(self):
        popen_options = {}

        def fake_popen(command, **kwargs):
            popen_options[command[0]] = kwargs
            return FakeProcess(command, self.events)

        supervisor = ChannelSupervisor(
            "independent", {"shaka": ["packager"], "ffmpeg": [
                "ffmpeg", "-i", "udp://@239.1.1.1:5000?localaddr=192.0.2.25"]},
            self.output, popen=fake_popen)
        try:
            supervisor.step(now=0)
            self.assertIs(popen_options["ffmpeg"]["stdin"],
                          mc2slicing.subprocess.DEVNULL)
            self.assertIs(popen_options["packager"]["stdin"],
                          mc2slicing.subprocess.DEVNULL)
        finally:
            supervisor.close()

    def test_ffmpeg_log_forwarder_keeps_dts_evidence_and_compacts_repeats(self):
        first_warning = (
            "[vost#0:0/libx264] Non-monotonic DTS; previous: 100000, "
            "current: 1000. This may result in incorrect timestamps.")
        repeated_warning = (
            "[vost#0:0/libx264] Non-monotonic DTS; previous: 100001, "
            "current: 2000. This may result in incorrect timestamps.")
        lines = "\n".join((
            "[vist#0:0/mpeg2video] timestamp discontinuity (stream id=65)",
            first_warning,
            repeated_warning,
            "frame=1163",
            "out_time=00:00:38.805433",
            "drop_frames=212",
            "dup_frames=0",
            "speed=0.97x",
            "progress=continue",
        ))

        self.supervisor._forward_ffmpeg_logs(io.StringIO(lines), True)
        output = self.output.getvalue()

        self.assertIn(first_warning, output)
        self.assertIn(
            "[WARNING] test ffmpeg: " + first_warning,
            output)
        self.assertNotIn(repeated_warning, output)
        self.assertIn(
            "[WARNING] test ffmpeg: "
            "[vist#0:0/mpeg2video] timestamp discontinuity (stream id=65)",
            output)
        self.assertIn("non_monotonic_dts_repeats=1", output)
        self.assertIn("latest_dts_previous=100001 latest_dts_current=2000", output)
        self.assertIn(
            "[DEBUG] test ffmpeg: frame=1163 out_time=00:00:38.805433 "
            "drop_frames=212 dup_frames=0 speed=0.97x",
            output)
        self.assertTrue(self.supervisor.timestamp_discontinuity_detected.is_set())

    def test_ffmpeg_exit_is_reaped_and_restarted_after_bounded_backoff(self):
        self.supervisor.step(now=0)
        process = self.supervisor.workers["ffmpeg"]["proc"]
        process.returncode = 1
        self.supervisor.step(now=1)
        state = self.supervisor.workers["ffmpeg"]
        self.assertEqual(state["exit_status"], 1)
        self.assertEqual(state["restart_count"], 1)
        self.assertEqual(state["next_retry_ts"], 1 + compute_backoff(1))

        self.supervisor.step(now=3)
        self.assertIsNone(state["proc"])
        self.supervisor.step(now=4)
        self.assertIsNotNone(state["proc"])
        self.assertEqual(state["pid"], 102)

    def test_packager_exit_stops_ffmpeg_before_restarting_both(self):
        self.supervisor.step(now=0)
        self.supervisor.workers["shaka"]["proc"].returncode = 2
        self.supervisor.step(now=1)
        self.assertEqual(self.events[-1][0:2], ("terminate", "ffmpeg"))
        self.assertIsNone(self.supervisor.workers["ffmpeg"]["proc"])
        retry_at = self.supervisor.workers["shaka"]["next_retry_ts"]

        self.supervisor.step(now=retry_at)
        starts = [event[1] for event in self.events if event[0] == "start"]
        self.assertEqual(starts, ["packager", "ffmpeg", "packager", "ffmpeg"])

    def test_manager_uses_json_local_multicast_addr_over_source_localaddr(self):
        config = {
            "baseDir": self.directory.name,
            "localMulticastAddr": "192.0.2.25",
            "multiviewChannels": [{
                "id": "test",
                "source": "udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9",
                "transcode_mode": "passthrough",
            }],
        }
        config_path = os.path.join(self.directory.name, "mvc.json")
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file)

        source_inputs = []
        original_source_input = mc2slicing.source_input

        def capture_source_input(*args, **kwargs):
            result = original_source_input(*args, **kwargs)
            source_inputs.append(result)
            return result

        class StopAfterFirstStep:
            def __init__(self, *args, **kwargs):
                pass

            def step(self):
                mc2slicing.signal.getsignal(mc2slicing.signal.SIGTERM)(None, None)

            def close(self):
                pass

        with patch.object(mc2slicing, "JSON_FILE", config_path), \
                patch.object(mc2slicing, "LOG_DIR", self.directory.name), \
                patch.object(mc2slicing, "source_input",
                             side_effect=capture_source_input), \
                patch.object(mc2slicing, "InputRecovery", StopAfterFirstStep):
            mc2slicing.run_manager_loop("test")

        self.assertEqual(len(source_inputs), 1)
        source_info = source_inputs[0]
        self.assertEqual(
            source_info["source"],
            config["multiviewChannels"][0]["source"],
        )
        self.assertEqual(
            source_info["topology"],
            ("ip", ["239.1.1.1:5000", "--local-address", "192.0.2.25"]),
        )

    def _run_manager_with_topology(self, channel):
        config = {
            "baseDir": self.directory.name,
            "localMulticastAddr": "192.0.2.25",
            "multiviewChannels": [channel],
        }
        config_path = os.path.join(self.directory.name, "mvc.json")
        with open(config_path, "w", encoding="utf-8") as config_file:
            json.dump(config, config_file)

        video = ElementaryStream(257, 0, "video", "H.264/AVC", (), (), (), None, None)
        audio = ElementaryStream(
            258, 0, "audio", "AAC", (), (), (), None, "en", 1)
        audio2 = ElementaryStream(
            259, 0, "audio", "AAC", (), (), (), None, "en", 2)
        topology = Topology(
            1, 42, 256, 257, video, (audio, audio2),
            (video, audio, audio2))
        events = []

        def discover(plugin, args):
            events.append(("topology", plugin, args))
            return topology

        class FakeSupervisor:
            input_lost = False

            def __init__(self, channel_id, commands, out):
                self.commands = commands
                events.append(("configured", commands))

            def step(self, now):
                events.append(("launch", self.commands))

            def close(self):
                pass

        class RecoveryAdapter:
            def __init__(self, channel_id, plugin, args, build_commands, out,
                         topology_monitor_factory=None):
                expected_monitor = (
                    mc2slicing.TopologyMonitor
                    if resolve_transcode_mode(channel) == "transcode" else None)
                assert topology_monitor_factory is expected_monitor
                self.recovery = InputRecovery(
                    channel_id, plugin, args, build_commands, out,
                    discoverer=discover, supervisor_factory=FakeSupervisor)

            def step(self):
                self.recovery.step(now=0)

            def close(self):
                self.recovery.close()

        def stop_loop(_delay):
            mc2slicing.signal.getsignal(mc2slicing.signal.SIGTERM)(None, None)

        with patch.object(mc2slicing, "JSON_FILE", config_path), \
                patch.object(mc2slicing, "LOG_DIR", self.directory.name), \
                patch.object(mc2slicing, "check_packager_discontinuity_support"), \
                patch.object(mc2slicing, "InputRecovery", RecoveryAdapter), \
                patch.object(mc2slicing.time, "sleep", side_effect=stop_loop):
            mc2slicing.run_manager_loop(channel["id"])
        return events

    def test_manager_passthrough_uses_direct_shaka_only_despite_encoder_settings(self):
        events = self._run_manager_with_topology({
            "id": "test", "source": "udp://@239.1.1.1:5000",
            "transcode_mode": "passthrough",
            "video_encoder": {"opaque": "invalid"},
            "audio_encoder": ["opaque", "invalid"],
            "packagerUdpPort": 1234,
        })

        self.assertEqual([event[0] for event in events],
                         ["topology", "configured", "launch"])
        commands = events[-1][1]
        self.assertEqual(tuple(commands), ("shaka",))
        descriptors = [arg for arg in commands["shaka"] if arg.startswith("in=")]
        self.assertEqual(len(descriptors), 3)
        self.assertTrue(all("in=udp://239.1.1.1:5000?reuse=1," in arg
                            for arg in descriptors))
        self.assertFalse(any("127.0.0.1" in arg for arg in descriptors))

    def test_manager_transcode_defaults_encoders_and_builds_loopback_commands(self):
        events = self._run_manager_with_topology({
            "id": "test", "source": "udp://@239.1.1.1:5000",
            "transcode_mode": "transcode",
            "video_average_bitrate": 8000000,
            "video_gop_size": 60, "video_keyint_min": 45,
            "packagerUdpPort": 1234,
        })

        self.assertEqual([event[0] for event in events],
                         ["topology", "configured", "launch"])
        commands = events[-1][1]
        self.assertEqual(tuple(commands), ("shaka", "ffmpeg"))
        self.assertIn("--hls_discontinuity_signal_file", commands["shaka"])
        ffmpeg = commands["ffmpeg"]
        self.assertEqual(ffmpeg[ffmpeg.index("-c:v") + 1], "libx264")
        self.assertEqual(ffmpeg[ffmpeg.index("-c:a") + 1], "aac")
        self.assertEqual(ffmpeg[ffmpeg.index("-b:v") + 1], "8000000")
        self.assertEqual(ffmpeg[ffmpeg.index("-g:v") + 1], "60")
        self.assertEqual(ffmpeg[ffmpeg.index("-keyint_min:v") + 1], "45")
        self.assertEqual(
            ffmpeg[ffmpeg.index("-i") + 1],
            "udp://@239.1.1.1:5000?localaddr=192.0.2.25")
        self.assertNotIn("pipe:0", ffmpeg)
        mapped = [ffmpeg[index + 1] for index, value in enumerate(ffmpeg[:-1])
                  if value == "-map"]
        self.assertEqual(mapped, ["0:i:257", "0:i:258", "0:i:259"])
        descriptors = [arg for arg in commands["shaka"] if arg.startswith("in=")]
        self.assertEqual(len(descriptors), 3)
        self.assertTrue(all("in=udp://127.0.0.1:1234," in arg
                            for arg in descriptors))
        self.assertEqual(ffmpeg[-1], "udp://127.0.0.1:1234")

    def test_manager_shared_audio_selection_in_both_modes(self):
        for mode in ("transcode", "passthrough"):
            for option in (None, False, True):
                with self.subTest(mode=mode, discard_first_audio=option):
                    channel = {
                        "id": "test", "source": "udp://@239.1.1.1:5000",
                        "transcode_mode": mode, "packagerUdpPort": 1234,
                    }
                    if option is not None:
                        channel["discard_first_audio"] = option
                    events = self._run_manager_with_topology(channel)
                    commands = events[-1][1]
                    expected = ["0:i:257", "0:i:259"] if option else [
                        "0:i:257", "0:i:258", "0:i:259"]

                    if mode == "transcode":
                        self.assertEqual(tuple(commands), ("shaka", "ffmpeg"))
                        ffmpeg = commands["ffmpeg"]
                        mapped = [
                            ffmpeg[index + 1]
                            for index, value in enumerate(ffmpeg[:-1])
                            if value == "-map"
                        ]
                        self.assertEqual(mapped, expected)
                    else:
                        self.assertEqual(tuple(commands), ("shaka",))

                    descriptors = [
                        arg for arg in commands["shaka"]
                        if arg.startswith("in=")]
                    expected_descriptor_count = 2 if option else 3
                    self.assertEqual(len(descriptors), expected_descriptor_count)
                    audio_descriptors = [
                        arg for arg in descriptors if "_audio-" in arg]
                    expected_names = (
                        ["_audio-2"] if option
                        else ["_audio-1", "_audio-2"])
                    for name in expected_names:
                        self.assertTrue(any(name in arg for arg in audio_descriptors))
                    if option:
                        self.assertFalse(any("_audio-1" in arg
                                             for arg in audio_descriptors))
                        expected_audio_index = "stream=2" if mode == "passthrough" \
                            else "stream=1"
                        self.assertTrue(any(
                            "_audio-2" in arg and expected_audio_index in arg
                            for arg in audio_descriptors))

    def test_manager_rejects_invalid_mode_and_encoders_before_startup(self):
        channel = {"id": "test", "source": "udp://239.1.1.1:5000"}
        invalid_channels = [
            ("missing mode", dict(channel)),
            ("invalid mode", {**channel, "transcode_mode": "invalid"}),
        ]
        for encoder_key in ("video_encoder", "audio_encoder"):
            unsupported_name = (
                "hevc" if encoder_key == "video_encoder" else "vorbis")
            for value in (None, "", "   ", "NoNe", "copy", unsupported_name):
                invalid_channels.append((
                    f"{encoder_key}={value!r}",
                    {**channel, "transcode_mode": "transcode",
                     encoder_key: value},
                ))

        config_path = os.path.join(self.directory.name, "mvc.json")
        for description, invalid_channel in invalid_channels:
            with self.subTest(configuration=description):
                config = {
                    "baseDir": self.directory.name,
                    "multiviewChannels": [invalid_channel],
                }
                with open(config_path, "w", encoding="utf-8") as config_file:
                    json.dump(config, config_file)

                with patch.object(mc2slicing, "JSON_FILE", config_path), \
                        patch.object(mc2slicing, "source_input") as source_input, \
                        patch.object(mc2slicing, "InputRecovery") as recovery, \
                        patch.object(mc2slicing, "TopologyMonitor") as monitor, \
                        self.assertRaises(SystemExit) as exit_error:
                    mc2slicing.run_manager_loop("test")

                self.assertEqual(exit_error.exception.code, 1)
                source_input.assert_not_called()
                recovery.assert_not_called()
                monitor.assert_not_called()

    def test_manager_rejects_non_boolean_discard_first_audio(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value):
                config = {
                    "baseDir": self.directory.name,
                    "multiviewChannels": [{
                        "id": "test", "source": "udp://239.1.1.1:5000",
                        "transcode_mode": "passthrough",
                        "discard_first_audio": value,
                    }],
                }
                config_path = os.path.join(self.directory.name, "mvc.json")
                with open(config_path, "w", encoding="utf-8") as config_file:
                    json.dump(config, config_file)

                with patch.object(mc2slicing, "JSON_FILE", config_path), \
                        patch.object(mc2slicing, "InputRecovery") as recovery:
                    mc2slicing.run_manager_loop("test")

                recovery.assert_not_called()

    def test_source_url_maps_to_one_tSDuck_input(self):
        self.assertEqual(topology_input("udp://@239.1.1.1:5000"),
                         ("ip", ["239.1.1.1:5000"]))
        self.assertEqual(topology_input("file:///tmp/input.ts"),
                         ("file", ["/tmp/input.ts"]))

    def test_source_input_preserves_shaka_source_and_normalizes_ffmpeg_source(self):
        source = (
            "udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9"
            "&token=a%2Fb"
        )
        source_info = mc2slicing.source_input(source, "192.0.2.25")

        self.assertEqual(source_info["source"], source)
        self.assertEqual(
            source_info["ffmpeg"],
            mc2slicing.normalize_source_url(source, "192.0.2.25"),
        )

    def test_multicast_source_input_keeps_tsduck_local_address_option(self):
        source_info = mc2slicing.source_input(
            "udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9",
            "192.0.2.25",
        )

        self.assertEqual(
            source_info["topology"],
            ("ip", ["239.1.1.1:5000", "--local-address", "192.0.2.25"]),
        )

    def test_file_and_udp_unicast_source_input_omit_multicast_options(self):
        sources = (
            ("file:///tmp/input.ts", ("file", ["/tmp/input.ts"])),
            ("udp://@192.0.2.10:5000?reuse=1",
             ("ip", ["192.0.2.10:5000"])),
        )
        for source, expected_topology in sources:
            with self.subTest(source=source):
                source_info = mc2slicing.source_input(source, "192.0.2.25")
                self.assertEqual(source_info["topology"], expected_topology)
                self.assertNotIn("--local-address", source_info["topology"][1])
                self.assertEqual(source_info["source"], source)

    def test_ffmpeg_transcode_command_uses_normalized_consumer_source(self):
        source = "udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9"
        source_info = mc2slicing.source_input(source, "192.0.2.25")
        video = type("Video", (), {"pid": 256})()
        topology = type("Topology", (), {"video": video, "audio": ()})()

        command = mc2slicing.build_ffmpeg_command(
            source_info["ffmpeg"],
            mc2slicing.select_audio_streams(topology), 1234
        )

        self.assertEqual(command[command.index("-i") + 1], source_info["ffmpeg"])
        self.assertNotEqual(source_info["ffmpeg"], source)

    def test_multicast_source_normalization_sets_authoritative_localaddr(self):
        normalize = getattr(mc2slicing, "normalize_source_url", None)
        self.assertTrue(callable(normalize),
                        "multicast input needs a shared source-normalization seam")
        source = ("udp://@239.1.1.1:5000?mode=listen&localaddr=192.0.2.9"
                  "&ttl=4&empty=")
        normalized = normalize(source, "192.0.2.25")

        parts = urlsplit(normalized)
        query = parse_qsl(parts.query, keep_blank_values=True)
        self.assertEqual((parts.scheme, parts.netloc, parts.path),
                         ("udp", "@239.1.1.1:5000", ""))
        self.assertEqual([value for key, value in query if key == "localaddr"],
                         ["192.0.2.25"])
        self.assertEqual([(key, value) for key, value in query if key != "localaddr"],
                         [("mode", "listen"), ("ttl", "4"), ("empty", "")])

    def test_non_multicast_sources_need_no_local_multicast_address(self):
        normalize = getattr(mc2slicing, "normalize_source_url", None)
        self.assertTrue(callable(normalize),
                        "source normalization must be available for all input kinds")
        sources = (
            "udp://@192.0.2.10:5000?reuse=1",
            "file:///tmp/input.ts",
            "239.1.1.1:5000?mode=listen",
        )
        for source in sources:
            with self.subTest(source=source):
                normalized = normalize(source, None)
                self.assertEqual(normalized, source)
                self.assertNotIn("localaddr", urlsplit(normalized).query)

    def test_non_multicast_sources_ignore_invalid_local_multicast_address(self):
        normalize = getattr(mc2slicing, "normalize_source_url", None)
        self.assertTrue(callable(normalize),
                        "source normalization must be available for all input kinds")
        for source in ("udp://@192.0.2.10:5000", "file:///tmp/input.ts",
                       "input.ts"):
            with self.subTest(source=source):
                self.assertEqual(normalize(source, "not-an-ip"), source)

    def test_multicast_requires_valid_local_ipv4_configuration(self):
        normalize = getattr(mc2slicing, "normalize_source_url", None)
        self.assertTrue(callable(normalize),
                        "multicast input needs a shared source-normalization seam")
        for address in (None, "not-an-ip", "2001:db8::1", "192.0.2.256"):
            with self.subTest(local_multicast_addr=address), \
                    self.assertRaisesRegex(ValueError, "(?i)local.?multicast.?addr|IPv4"):
                normalize("udp://@239.1.1.1:5000", address)

    def test_ffmpeg_exit_does_not_infer_source_loss_for_independent_input(self):
        first = ("topology-1",)
        discovered = []

        def discover(plugin, args):
            discovered.append((plugin, args))
            if len(discovered) == 1:
                raise TopologyError("expected exactly one complete PAT and PMT")
            return first

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"],
            lambda topology: {"shaka": ["packager", str(topology)],
                              "ffmpeg": ["ffmpeg", str(topology)]},
            self.output, discoverer=discover,
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(channel, commands, out,
                                  popen=lambda command, **kwargs:
                                      FakeProcess(command, self.events)))
        recovery.step(now=0)
        self.assertEqual(recovery.state, "waiting")
        recovery.step(now=2)
        self.assertEqual(len(discovered), 1)
        recovery.step(now=3)
        self.assertEqual(recovery.state, "running")
        self.assertEqual(len(self.events), 2)

        recovery.supervisor.workers["ffmpeg"]["proc"].returncode = 1
        recovery.step(now=4)
        self.assertEqual(recovery.state, "running")
        self.assertFalse(recovery.supervisor.input_lost)
        self.assertEqual(len(discovered), 2)
        retry_at = recovery.supervisor.workers["ffmpeg"]["next_retry_ts"]
        recovery.step(now=retry_at)
        self.assertEqual(recovery.state, "running")
        self.assertEqual(len(discovered), 2)
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "start"],
                         ["packager", "ffmpeg", "ffmpeg"])
        recovery.close()

    def test_transcode_mode_rebuilds_workers_when_topology_changes(self):
        first, changed = ("topology-1",), ("topology-2",)
        discovered = []

        def discover(plugin, args):
            discovered.append(True)
            return first if len(discovered) == 1 else changed

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"],
            lambda topology: {"shaka": ["packager", str(topology)],
                              "ffmpeg": ["ffmpeg", str(topology)]},
            self.output, discoverer=discover,
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(channel, commands, out,
                                  popen=lambda command, **kwargs:
                                      FakeProcess(command, self.events)))
        recovery.step(now=0)
        self.assertEqual(recovery.state, "running")
        recovery.step(now=mc2slicing.TRANSCODE_TOPOLOGY_PROBE_INTERVAL_SEC)
        self.assertEqual(recovery.topology, first)
        self.assertEqual(len(discovered), 2)
        self.assertEqual(
            [event[1] for event in self.events if event[0] == "start"],
            ["packager", "ffmpeg"])

        recovery.step(
            now=mc2slicing.TRANSCODE_TOPOLOGY_PROBE_INTERVAL_SEC
            + mc2slicing.TOPOLOGY_STABILITY_SEC - 0.1)
        self.assertEqual(recovery.topology, first)
        self.assertEqual(len(discovered), 2)

        recovery.step(
            now=mc2slicing.TRANSCODE_TOPOLOGY_PROBE_INTERVAL_SEC
            + mc2slicing.TOPOLOGY_STABILITY_SEC)

        self.assertEqual(recovery.topology, changed)
        self.assertEqual(len(discovered), 3)
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "start"],
                         ["packager", "ffmpeg", "packager", "ffmpeg"])
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "terminate"],
                         ["ffmpeg", "packager"])
        self.assertIn(
            "changes: ('topology-1',) -> ('topology-2',)",
            self.output.getvalue())
        recovery.close()

    def test_background_topology_update_rebuilds_workers(self):
        first, changed = ("topology-1",), ("topology-2",)
        monitors = []
        process_records = []
        reaped_processes = []

        class TrackedProcess(FakeProcess):
            def wait(self, timeout=None):
                result = super().wait(timeout)
                reaped_processes.append(self.pid)
                return result

        class FakeMonitor:
            def __init__(self, plugin, args, initial_topology, out_fp=None):
                self.event = None
                self.closed = False
                self.stdout = None
                monitors.append(self)

            def poll(self):
                event, self.event = self.event, None
                return event

            def close(self):
                self.closed = True

        def popen(command, **kwargs):
            process = TrackedProcess(command, self.events)
            process_records.append(
                (threading.get_ident(), command[0], process))
            return process

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"],
            lambda topology: {"shaka": ["packager", str(topology)],
                              "ffmpeg": ["ffmpeg", str(topology)]},
            self.output, discoverer=lambda plugin, args: first,
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(channel, commands, out, popen=popen),
            topology_monitor_factory=FakeMonitor)
        recovery.step(now=0)
        self.assertIsNone(monitors[0].stdout)
        monitors[0].event = TopologyMonitorEvent(first, None)
        recovery.step(now=1)
        deadline = time.monotonic() + 2
        while len([event for event in self.events if event[0] == "start"]) < 2 \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        recovery.step(now=1.5)
        self.assertEqual(recovery.topology, first)
        self.assertIsNotNone(recovery.supervisor)
        self.assertEqual([event[1] for event in self.events if event[0] == "start"],
                         ["packager", "ffmpeg"])
        first_worker_thread = recovery.worker_thread
        self.assertIsNotNone(first_worker_thread)
        self.assertNotEqual(first_worker_thread.ident, threading.get_ident())
        self.assertIn("transcode-worker", first_worker_thread.name)
        self.assertIsNone(monitors[0].stdout)
        self.assertEqual(
            [(thread_id, name) for thread_id, name, _ in process_records],
            [(first_worker_thread.ident, "packager"),
             (first_worker_thread.ident, "ffmpeg")])
        monitors[0].event = TopologyMonitorEvent(first, None)
        recovery.step(now=1.5)
        self.assertIs(recovery.worker_thread, first_worker_thread)
        self.assertEqual(len(process_records), 2)
        monitors[0].event = TopologyMonitorEvent(changed, None)
        recovery.step(now=2)
        self.assertIs(recovery.worker_thread, first_worker_thread)
        self.assertEqual(len(process_records), 2)
        self.assertIn("topology candidate pending", self.output.getvalue())

        monitors[0].event = TopologyMonitorEvent(first, None)
        recovery.step(now=2.5)
        self.assertIs(recovery.worker_thread, first_worker_thread)
        self.assertIn("topology candidate reverted", self.output.getvalue())

        monitors[0].event = TopologyMonitorEvent(changed, None)
        recovery.step(now=3)
        recovery.step(
            now=3 + mc2slicing.TOPOLOGY_STABILITY_SEC - 0.1)
        self.assertIs(recovery.worker_thread, first_worker_thread)
        self.assertEqual(len(process_records), 2)

        recovery.step(
            now=3 + mc2slicing.TOPOLOGY_STABILITY_SEC)
        deadline = time.monotonic() + 2
        while len([event for event in self.events if event[0] == "start"]) < 4 \
                and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(recovery.topology, changed)
        self.assertFalse(first_worker_thread.is_alive())
        self.assertIsNot(recovery.worker_thread, first_worker_thread)
        self.assertEqual(len(monitors), 1)
        self.assertFalse(monitors[0].closed)
        self.assertEqual(recovery.supervisor.commands["ffmpeg"],
                         ["ffmpeg", str(changed)])
        replacement_worker_thread = recovery.worker_thread
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "start"],
                         ["packager", "ffmpeg", "packager", "ffmpeg"])
        self.assertEqual(
            [(thread_id, name) for thread_id, name, _ in process_records],
            [(first_worker_thread.ident, "packager"),
             (first_worker_thread.ident, "ffmpeg"),
             (replacement_worker_thread.ident, "packager"),
             (replacement_worker_thread.ident, "ffmpeg")])
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "terminate"],
                         ["ffmpeg", "packager"])
        self.assertIn("input topology stable for 3s",
                      self.output.getvalue())
        recovery.close()
        self.assertEqual(
            set(reaped_processes),
            {process.pid for _, _, process in process_records})
        self.assertTrue(monitors[0].closed)

    def test_timestamp_discontinuity_restarts_ffmpeg_and_signals_live_shaka(self):
        topology = ("stable-topology",)
        monitors = []
        ffmpeg_commands = []
        with tempfile.TemporaryDirectory() as output_dir:
            class FakeMonitor:
                def __init__(self, plugin, args, initial_topology, out_fp=None):
                    self.event = None
                    self.closed = False
                    monitors.append(self)

                def poll(self):
                    event, self.event = self.event, None
                    return event

                def close(self):
                    self.closed = True

            def commands(current_topology):
                init_segment = os.path.join(output_dir, "test_video_g1_init.mp4")
                return {
                    "shaka": [
                        "packager",
                        "in=udp://127.0.0.1:12000,stream=video,"
                        f"init_segment={init_segment}",
                        "--hls_discontinuity_signal_file",
                        os.path.join(output_dir, "test_hls_discontinuity.signal"),
                    ],
                    "ffmpeg": [
                        "ffmpeg", str(current_topology), "-f", "mpegts",
                        "udp://127.0.0.1:12000",
                    ],
                }

            def popen(command, **kwargs):
                ffmpeg_commands.append(list(command))
                return FakeProcess(command, self.events)

            recovery = InputRecovery(
                "test", "ip", [], commands, self.output,
                discoverer=lambda plugin, args: topology,
                supervisor_factory=lambda channel, worker_commands, out:
                    ChannelSupervisor(
                        channel, worker_commands, out,
                        popen=popen),
                topology_monitor_factory=FakeMonitor)
            try:
                recovery.step(now=0)
                monitors[0].event = TopologyMonitorEvent(topology, None)
                recovery.step(now=1)
                deadline = time.monotonic() + 2
                while len([event for event in self.events if event[0] == "start"]) < 2 \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)

                first_supervisor = recovery.supervisor
                first_worker_thread = recovery.worker_thread
                shaka_process = first_supervisor.workers["shaka"]["proc"]
                ffmpeg_process = first_supervisor.workers["ffmpeg"]["proc"]
                generation = first_supervisor.commands["shaka"][1]
                first_supervisor.timestamp_discontinuity_detected.set()
                deadline = time.monotonic() + 2
                while len([event for event in self.events if event[0] == "start"]) < 3 \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)

                self.assertTrue(first_worker_thread.is_alive())
                self.assertIs(recovery.supervisor, first_supervisor)
                self.assertIs(
                    first_supervisor.workers["shaka"]["proc"], shaka_process)
                self.assertIsNot(
                    first_supervisor.workers["ffmpeg"]["proc"], ffmpeg_process)
                self.assertEqual(
                    first_supervisor.commands["shaka"][1], generation)
                self.assertIn(
                    ("-mpegts_flags", "+initial_discontinuity"),
                    list(zip(ffmpeg_commands[-1], ffmpeg_commands[-1][1:])))
                signal_path = first_supervisor.commands["shaka"][-1]
                with open(signal_path, encoding="ascii") as signal_file:
                    self.assertTrue(signal_file.read().strip())
                self.assertEqual(len(monitors), 1)
                self.assertFalse(monitors[0].closed)
                self.assertIn(
                    "timestamp discontinuity; signaling Shaka and restarting FFmpeg only",
                    self.output.getvalue())
            finally:
                recovery.close()

    def test_background_psi_version_only_update_preserves_workers(self):
        video = ElementaryStream(
            257, 0, "video", "H.264/AVC", (), (), (), None, None)
        first = Topology(1, 42, 256, 257, video, (), (video,), 0, 0)
        versioned = first._replace(pat_version=1, pmt_version=1)
        monitors = []

        class FakeMonitor:
            def __init__(self, plugin, args, initial_topology, out_fp=None):
                self.event = None
                monitors.append(self)

            def poll(self):
                event, self.event = self.event, None
                return event

            def close(self):
                pass

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"],
            lambda topology: {"shaka": ["packager", str(topology)],
                              "ffmpeg": ["ffmpeg", str(topology)]},
            self.output, discoverer=lambda plugin, args: first,
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(
                    channel, commands, out,
                    popen=lambda command, **kwargs:
                        FakeProcess(command, self.events)),
            topology_monitor_factory=FakeMonitor)
        try:
            recovery.step(now=0)
            monitors[0].event = TopologyMonitorEvent(first, None)
            recovery.step(now=1)
            deadline = time.monotonic() + 2
            while len([event for event in self.events if event[0] == "start"]) < 2 \
                    and time.monotonic() < deadline:
                time.sleep(0.01)

            supervisor = recovery.supervisor
            worker_pids = {
                name: state["pid"]
                for name, state in supervisor.workers.items()}
            monitors[0].event = TopologyMonitorEvent(versioned, None)
            recovery.step(now=2)

            self.assertIs(recovery.supervisor, supervisor)
            self.assertEqual(recovery.topology, versioned)
            self.assertEqual(
                {name: state["pid"]
                 for name, state in supervisor.workers.items()},
                worker_pids)
            self.assertEqual(
                [event[1] for event in self.events if event[0] == "start"],
                ["packager", "ffmpeg"])
            self.assertNotIn("input topology changed", self.output.getvalue())
            self.assertIn("PSI topology updated; media topology unchanged",
                          self.output.getvalue())
        finally:
            recovery.close()

    def test_worker_thread_retires_output_before_replacement_start(self):
        first, changed = ("topology-1",), ("topology-2",)
        starts = []
        with tempfile.TemporaryDirectory() as output_dir:
            retired = os.path.join(output_dir, "test_video_gold_init.mp4")
            with open(retired, "w", encoding="utf-8") as output_file:
                output_file.write("retired")

            def commands(topology):
                generation = "new" if topology == changed else "old"
                init_segment = os.path.join(
                    output_dir, f"test_video_g{generation}_init.mp4")
                return {
                    "shaka": ["packager",
                              f"in=udp://127.0.0.1:12000,stream=video,"
                              f"init_segment={init_segment}"],
                    "ffmpeg": ["ffmpeg", str(topology)],
                }

            class FakeMonitor:
                def __init__(self, *args, **kwargs):
                    self.event = None

                def poll(self):
                    event, self.event = self.event, None
                    return event

                def close(self):
                    pass

            def popen(command, **kwargs):
                starts.append((command[0], os.path.exists(retired)))
                return FakeProcess(command, self.events)

            recovery = InputRecovery(
                "test", "ip", ["239.1.1.1:5000"], commands, self.output,
                topology_monitor_factory=FakeMonitor,
                supervisor_factory=lambda channel, worker_commands, out:
                    ChannelSupervisor(channel, worker_commands, out, popen=popen))
            recovery.step(now=0)
            monitor = recovery.topology_monitor
            monitor.event = TopologyMonitorEvent(first, None)
            recovery.step(now=1)
            deadline = time.monotonic() + 2
            while len(starts) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            monitor.event = TopologyMonitorEvent(changed, None)
            recovery.step(now=2)
            self.assertEqual(len(starts), 2)
            recovery.step(
                now=2 + mc2slicing.TOPOLOGY_STABILITY_SEC)
            deadline = time.monotonic() + 2
            while len(starts) < 4 and time.monotonic() < deadline:
                time.sleep(0.01)
            recovery.close()

            self.assertFalse(os.path.exists(retired))
            self.assertEqual(starts[0:2], [("packager", True), ("ffmpeg", True)])
            self.assertEqual(starts[2:4], [("packager", False), ("ffmpeg", False)])

    def test_transient_monitor_timeout_preserves_workers_and_reattaches(self):
        first = ("topology-1",)
        monitors = []
        supervisors = []
        monitor_factory_attempts = []

        class FakeMonitor:
            def __init__(self, plugin, input_args, initial_topology, **kwargs):
                self.event = None
                self.closed = False
                self.initial_topology = initial_topology
                monitors.append(self)

            def poll(self):
                event, self.event = self.event, None
                return event

            def close(self):
                self.closed = True

        def supervisor_factory(channel, commands, out):
            supervisor = ChannelSupervisor(
                channel, commands, out,
                popen=lambda command, **kwargs:
                    FakeProcess(command, self.events))
            supervisors.append(supervisor)
            return supervisor

        def monitor_factory(plugin, input_args, initial_topology, **kwargs):
            monitor_factory_attempts.append(initial_topology)
            if len(monitor_factory_attempts) == 2:
                raise TopologyUnavailableError("TSDuck reattach failed")
            return FakeMonitor(plugin, input_args, initial_topology, **kwargs)

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"],
            lambda topology: {"shaka": ["packager"],
                              "ffmpeg": ["ffmpeg", str(topology)]},
            self.output,
            supervisor_factory=supervisor_factory,
            topology_monitor_factory=monitor_factory)
        recovery.step(now=0)
        monitors[0].event = TopologyMonitorEvent(first, None)
        recovery.step(now=1)
        deadline = time.monotonic() + 2
        while len(self.events) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        active_supervisor = recovery.supervisor
        worker_pids = {
            name: state["proc"].pid
            for name, state in active_supervisor.workers.items()
        }

        monitors[0].event = TopologyMonitorEvent(
            None, TopologyUnavailableError("TSDuck receive timeout"))
        recovery.step(now=2)
        self.assertEqual(recovery.state, "running")
        self.assertTrue(monitors[0].closed)
        self.assertIs(recovery.supervisor, active_supervisor)
        self.assertEqual(
            {name: state["proc"].pid
             for name, state in active_supervisor.workers.items()},
            worker_pids)

        retry_at = recovery.next_probe_ts
        recovery.step(now=retry_at)
        self.assertEqual(recovery.state, "running")
        self.assertIsNone(recovery.topology_monitor)
        self.assertIs(recovery.supervisor, active_supervisor)
        self.assertEqual(
            {name: state["proc"].pid
             for name, state in active_supervisor.workers.items()},
            worker_pids)

        retry_at = recovery.next_probe_ts
        recovery.step(now=retry_at)
        self.assertEqual(len(monitors), 2)
        self.assertEqual(monitors[1].initial_topology, first)
        self.assertIs(recovery.supervisor, active_supervisor)
        self.assertEqual(recovery.state, "running")
        monitors[1].event = TopologyMonitorEvent(first, None)
        recovery.step(now=retry_at + 0.1)
        self.assertEqual(recovery.supervisor, active_supervisor)
        self.assertEqual(
            {name: state["proc"].pid
             for name, state in active_supervisor.workers.items()},
            worker_pids)
        recovery.close()

    def test_topology_snapshots_retain_psi_versions(self):
        pat_v0 = ('<PAT transport_stream_id="1" version="0"><service '
                  'service_id="1" program_map_PID="256"/></PAT>')
        pmt_v0 = ('<PMT service_id="1" version="0" PCR_PID="257">'
                  '<metadata PID="256"/><component elementary_PID="257" '
                  'stream_type="0x1B"/></PMT>')
        pmt_v1 = pmt_v0.replace('version="0"', 'version="1"')
        from ts_topology import parse_topology_xml
        self.assertNotEqual(
            parse_topology_xml(f"<tsduck>{pat_v0}{pmt_v0}</tsduck>"),
            parse_topology_xml(f"<tsduck>{pat_v0}{pmt_v1}</tsduck>"))

    def test_tstoprocessor_is_started_in_manager_process_and_drops_media(self):
        created = []

        class FakeProcessor:
            def __init__(self, report):
                self.done = threading.Event()
                self.pid = os.getpid()
                created.append(self)

            def start(self):
                self.started_in = threading.get_ident()

            def waitForTermination(self):
                self.done.wait()

            def abort(self):
                self.done.set()

            def delete(self):
                pass

        class FakeReport:
            def delete(self):
                pass

        class FakeTSDuck:
            @staticmethod
            def NullReport():
                return FakeReport()

            DuckContext = staticmethod(lambda report: FakeReport())
            SectionFile = staticmethod(lambda duck: FakeReport())
            TSProcessor = FakeProcessor

        manager_thread = threading.get_ident()
        with patch("ts_topology._load_tsduck", return_value=FakeTSDuck()):
            monitor = mc2slicing.TopologyMonitor(
                "file", ["input.ts"], stale_timeout=30)
            try:
                self.assertEqual(created[0].started_in, manager_thread)
                self.assertEqual(created[0].pid, os.getpid())
                self.assertEqual(created[0].output, ["drop"])
                self.assertFalse(any(
                    child.name == "tsduck-input-pipeline"
                    for child in multiprocessing.active_children()))
            finally:
                monitor.close()

    def test_passthrough_probes_loss_and_restarts_only_after_topology_rediscovery(self):
        first, changed = ("topology-1",), ("topology-2",)
        discovered = []

        def discover(plugin, args):
            discovered.append((plugin, args))
            if len(discovered) == 2:
                raise TopologyUnavailableError("source unavailable")
            return first if len(discovered) == 1 else changed

        built = []

        def build(topology):
            built.append(topology)
            return {"shaka": ["packager", str(topology)]}

        recovery = InputRecovery(
            "test", "ip", [], build, self.output, discoverer=discover,
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(channel, commands, out,
                                  popen=lambda command, **kwargs:
                                      FakeProcess(command, self.events)))
        recovery.step(now=0)
        self.assertEqual(recovery.state, "running")
        recovery.step(now=mc2slicing.SOURCE_PROBE_INTERVAL_SEC - 1)
        self.assertEqual(len(discovered), 1)

        recovery.step(now=mc2slicing.SOURCE_PROBE_INTERVAL_SEC)
        self.assertEqual(recovery.state, "waiting")
        self.assertIsNone(recovery.supervisor)
        self.assertEqual(
            [event[0:2] for event in self.events],
            [("start", "packager"), ("terminate", "packager")])
        recovery.step(now=recovery.next_probe_ts - 1)
        self.assertEqual(len(discovered), 2)

        recovery.step(now=recovery.next_probe_ts)
        self.assertEqual(recovery.state, "running")
        self.assertEqual(built, [first, changed])
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "start"],
                         ["packager", "packager"])
        self.assertEqual(tuple(recovery.supervisor.workers), ("shaka",))
        recovery.close()

    def test_shaka_exit_does_not_mark_direct_source_unavailable(self):
        discovered = []
        recovery = InputRecovery(
            "test", "ip", [], lambda topology: {"shaka": ["packager"]},
            self.output,
            discoverer=lambda plugin, args: discovered.append(True) or ("topology",),
            supervisor_factory=lambda channel, commands, out:
                ChannelSupervisor(channel, commands, out,
                                  popen=lambda command, **kwargs:
                                      FakeProcess(command, self.events)))
        recovery.step(now=0)
        recovery.supervisor.workers["shaka"]["proc"].returncode = 2
        recovery.step(now=1)
        self.assertEqual(recovery.state, "running")
        self.assertFalse(recovery.supervisor.input_lost)
        self.assertEqual(recovery.supervisor.workers["shaka"]["restart_count"], 1)
        self.assertEqual(len(discovered), 1)
        recovery.step(now=4)
        self.assertEqual(recovery.state, "running")
        self.assertEqual(len(discovered), 1)
        self.assertEqual([event[1] for event in self.events
                          if event[0] == "start"], ["packager", "packager"])
        recovery.close()

    def test_worker_exception_logs_traceback_cleans_up_and_keeps_manager_alive(self):
        closed = []

        class RaisingSupervisor:
            input_lost = False

            def step(self, now):
                raise RuntimeError("worker supervision failed")

            def close(self):
                closed.append(True)

        recovery = InputRecovery(
            "test", "ip", [], lambda topology: {},
            self.output, discoverer=lambda plugin, args: ("topology",),
            supervisor_factory=lambda channel, commands, out: RaisingSupervisor())
        with self.assertLogs("avpipeline", level="ERROR") as logs:
            recovery.step(now=0)
        self.assertIn("Traceback", "\n".join(logs.output))
        self.assertEqual(recovery.state, "waiting")
        self.assertEqual(closed, [True])
        recovery.step(now=2)
        self.assertEqual(recovery.state, "waiting")
        recovery.close()

    def test_packager_version_accepts_exact_393(self):
        result = subprocess.CompletedProcess(
            ["packager", "--version"], 0, stdout="packager version 3.9.3\n", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=result) as run:
            check_packager_version()
        run.assert_called_once_with(
            ["/usr/bin/packager", "--version"],
            capture_output=True, text=True, timeout=15)

    def test_packager_discontinuity_support_requires_signal_flag(self):
        help_result = subprocess.CompletedProcess(
            ["packager", "--help"], 1,
            stdout="--hls_discontinuity_signal_file path\n", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=help_result) as run:
            mc2slicing.check_packager_discontinuity_support()
        run.assert_called_once_with(
            ["/usr/bin/packager", "--help"],
            capture_output=True, text=True, timeout=15)

        unsupported = subprocess.CompletedProcess(
            ["packager", "--help"], 0, stdout="standard Shaka help\n", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=unsupported), \
        self.assertRaisesRegex(RuntimeError, "lacks runtime HLS discontinuity"):
            mc2slicing.check_packager_discontinuity_support()

    def test_packager_version_accepts_build_suffix(self):
        result = subprocess.CompletedProcess(
            ["packager", "--version"], 0,
            stdout="packager version v3.9.3-0a8ba4f-release\n", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=result):
            check_packager_version()

    def test_packager_version_rejects_mismatch_and_unknown_output(self):
        for output in (
                "packager version 3.9.2\n",
                "packager version 3.9.30\n",
                "packager version 3.10.3\n",
                "unrecognized output"):
            result = subprocess.CompletedProcess(
                ["packager", "--version"], 0, stdout=output, stderr="")
            with self.subTest(output=output), \
                    patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                    patch("mc2slicing.subprocess.run", return_value=result), \
                    self.assertRaisesRegex(RuntimeError, "version|unrecognized"):
                check_packager_version()

    def test_packager_version_rejects_empty_output(self):
        result = subprocess.CompletedProcess(
            ["packager", "--version"], 0, stdout="", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=result), \
                self.assertRaisesRegex(RuntimeError, "empty or unrecognized"):
            check_packager_version()

    def test_packager_version_reports_missing_executable(self):
        with patch("mc2slicing.shutil.which", return_value=None), \
                self.assertRaisesRegex(RuntimeError, "not found in PATH"):
            check_packager_version()

    def test_packager_version_reports_nonzero_command_status(self):
        result = subprocess.CompletedProcess(
            ["packager", "--version"], 2, stdout="bad version output", stderr="")
        with patch("mc2slicing.shutil.which", return_value="/usr/bin/packager"), \
                patch("mc2slicing.subprocess.run", return_value=result), \
                self.assertRaisesRegex(RuntimeError, "exit status 2"):
            check_packager_version()

    def test_startup_dependency_failures_log_and_prevent_manager_workers(self):
        failures = (
            ("version mismatch", "/usr/bin/packager",
             subprocess.CompletedProcess(
                 ["packager", "--version"], 0,
                 stdout="packager version 3.9.2\n", stderr=""),
             "version mismatch"),
            ("missing executable", None, None, "not found in PATH"),
            ("nonzero version command", "/usr/bin/packager",
             subprocess.CompletedProcess(
                 ["packager", "--version"], 2,
                 stdout="bad version output", stderr=""),
             "exit status 2"),
        )
        for label, packager_path, result, error_text in failures:
            with self.subTest(failure=label), \
                    patch.object(sys, "argv", ["mc2slicing.py", "--manager", "test"]), \
                    patch("mc2slicing.shutil.which",
                          side_effect=lambda name: (packager_path if name == "packager"
                                                    else f"/usr/bin/{name}")), \
                    patch("mc2slicing.subprocess.run", return_value=result) as run, \
                    patch("mc2slicing.run_manager_loop") as run_manager, \
                    patch("mc2slicing.subprocess.Popen") as popen:
                with self.assertLogs("avpipeline", level="ERROR") as logs, \
                        self.assertRaises(SystemExit) as exit_error:
                    main()
            self.assertEqual(exit_error.exception.code, 1)
            self.assertTrue(any("CRITICAL ERROR" in message for message in logs.output))
            self.assertTrue(any(error_text in message for message in logs.output))
            run_manager.assert_not_called()
            popen.assert_not_called()
            if result is None:
                run.assert_not_called()

    def test_startup_accepts_suffixed_version_and_hands_off_to_manager(self):
        result = subprocess.CompletedProcess(
            ["packager", "--version"], 0,
            stdout="packager version v3.9.3-0a8ba4f-release\n", stderr="")
        with patch.object(sys, "argv", ["mc2slicing.py", "--manager", "test"]), \
                patch("mc2slicing.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"), \
                patch("mc2slicing.subprocess.run", return_value=result), \
                patch("mc2slicing.run_manager_loop") as run_manager, \
                patch("mc2slicing.subprocess.Popen") as popen:
            with self.assertRaises(SystemExit) as exit_error:
                main()
        self.assertEqual(exit_error.exception.code, 0)
        run_manager.assert_called_once()
        popen.assert_not_called()

    def test_unsupported_topology_enters_explicit_fault_state(self):
        def discover(plugin, args):
            raise TopologyError("unsupported stream type")

        recovery = InputRecovery(
            "test", "ip", [], lambda topology: {},
            self.output, discoverer=discover)
        recovery.step(now=0)
        self.assertEqual(recovery.state, "fault")
        recovery.step(now=100)
        self.assertIn("manager: fault:", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
