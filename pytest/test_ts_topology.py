import io
import multiprocessing
import os
import signal
import socket
import sys
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from avpipeline import InputRecovery
import ts_topology
from ts_topology import (
    ElementaryStream, Topology, TopologyError, TopologyMonitor,
    TopologyUnavailableError, describe_topology_change,
    discover_topology, parse_topology_xml, select_audio_streams,
)


def _crc(data):
    value = 0xffffffff
    for byte in data:
        value ^= byte << 24
        for _ in range(8):
            value = ((value << 1) ^ 0x04c11db7) & 0xffffffff if value & 0x80000000 else (value << 1) & 0xffffffff
    return value.to_bytes(4, "big")


def _section(table_id, header, body):
    data = bytes([table_id, 0xb0, 0]) + header + body
    length = len(data) - 3 + 4
    data = data[:1] + bytes([0xb0 | ((length >> 8) & 15), length & 255]) + data[3:]
    return data + _crc(data)


def _packet(pid, section, continuity_counter=0):
    packet = bytes([0x47, 0x40 | (pid >> 8), pid & 255,
                    0x10 | continuity_counter, 0]) + section
    return packet + bytes([0xff]) * (188 - len(packet))


def _parse_audio_components(audio_components):
    components = '<component elementary_PID="257" stream_type="0x1B"/>'
    components += "".join(audio_components)
    xml = ('<tsduck><PAT transport_stream_id="1"><service service_id="1" '
           'program_map_PID="256"/></PAT><PMT service_id="1" PCR_PID="257">'
           '<metadata PID="256"/>' + components + '</PMT></tsduck>')
    return parse_topology_xml(xml)


def _stubborn_probe_worker(plugin, args, xml_path, connection):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    connection.send(("ready",))
    threading.Event().wait()


def _nonreturning_probe_worker(plugin, args, xml_path, connection):
    threading.Event().wait()


class TopologyTests(unittest.TestCase):
    def test_describe_topology_change_identifies_effective_fields(self):
        video = ElementaryStream(
            257, 0x1B, "video", "H.264/AVC", (), (), (), None, None)
        previous = Topology(1, 42, 256, 257, video, (), (video,), 0, 0)
        changed_video = video._replace(pid=258)
        current = previous._replace(
            pmt_pid=259, pcr_pid=258, video=changed_video,
            streams=(changed_video,), pat_version=1, pmt_version=1)

        description = describe_topology_change(previous, current)

        self.assertIn("pmt_pid: 256 -> 259", description)
        self.assertIn("video:", description)
        self.assertIn("PSI version counters (ignored):", description)

    def test_describe_topology_change_marks_version_only_update_ignored(self):
        video = ElementaryStream(
            257, 0x1B, "video", "H.264/AVC", (), (), (), None, None)
        previous = Topology(1, 42, 256, 257, video, (), (video,), 0, 0)
        current = previous._replace(pat_version=1, pmt_version=1)

        description = describe_topology_change(previous, current)

        self.assertEqual(
            description,
            "no media fields changed; PSI version counters (ignored): "
            "pat_version: 0 -> 1, pmt_version: 0 -> 1")

    def test_accepted_ip_monitor_waits_through_udp_silence(self):
        processors = []

        class FakeObject:
            def __init__(self, *args):
                pass

            def delete(self):
                pass

        class FakeProcessor(FakeObject):
            def __init__(self, *args):
                super().__init__(*args)
                self.done = threading.Event()

            def start(self):
                pass

            def waitForTermination(self):
                self.done.wait()

            def abort(self):
                self.done.set()

        class FakeTSDuck:
            NullReport = FakeObject
            DuckContext = FakeObject
            SectionFile = FakeObject

            @staticmethod
            def TSProcessor(*args):
                processor = FakeProcessor(*args)
                processors.append(processor)
                return processor

        with patch.object(ts_topology, "_load_tsduck", return_value=FakeTSDuck):
            monitor = TopologyMonitor(
                "ip", ["239.1.1.1:5000"], ("existing topology",),
                poll_interval=0.01, stale_timeout=0.15)
            try:
                self.assertEqual(
                    processors[0].input, ["ip", "239.1.1.1:5000"])
                time.sleep(0.4)
                self.assertTrue(monitor._process.is_alive())
                self.assertIsNone(monitor.poll())
            finally:
                monitor.close()

    def test_live_ip_monitor_ignores_unchanged_tables_and_sees_version_change(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        pat = _section(0, bytes.fromhex("0001c10000"), bytes.fromhex("0001e100"))
        pmt_v0 = _section(2, bytes.fromhex("0001c10000e101f000"),
                          bytes.fromhex("1be101f000"))
        pmt_v1 = _section(2, bytes.fromhex("0001c30000e102f000"),
                          bytes.fromhex("1be102f000"))
        payload = [_packet(0, pat) + _packet(0x100, pmt_v0)]
        payload_lock = threading.Lock()
        sender_stop = threading.Event()
        packets_sent = [0]

        def send_packets():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                while not sender_stop.is_set():
                    with payload_lock:
                        packets = payload[0]
                    sender.sendto(packets, ("127.0.0.1", port))
                    packets_sent[0] += 1
                    sender_stop.wait(0.05)

        sender_thread = threading.Thread(target=send_packets, daemon=True)
        sender_thread.start()
        monitor = None
        try:
            monitor = TopologyMonitor(
                "ip", [str(port)], poll_interval=0.05, stale_timeout=5.0)
            self.assertIsNone(monitor.stdout)
            deadline = time.monotonic() + 8
            event = None
            while event is None and time.monotonic() < deadline:
                event = monitor.poll()
                time.sleep(0.02)
            self.assertIsNotNone(event)
            self.assertIsNone(event.error)
            self.assertEqual(event.topology.video.pid, 257)

            sent_before = packets_sent[0]
            deadline = time.monotonic() + 5.2
            while time.monotonic() < deadline:
                self.assertIsNone(monitor.poll())
                self.assertTrue(monitor._process.is_alive())
                time.sleep(0.05)
            self.assertGreater(packets_sent[0], sent_before)

            with payload_lock:
                payload[0] = _packet(0, pat) + _packet(0x100, pmt_v1, 1)
            deadline = time.monotonic() + 3
            event = None
            while event is None and time.monotonic() < deadline:
                event = monitor.poll()
                time.sleep(0.02)
            self.assertIsNotNone(event)
            self.assertIsNone(event.error)
            self.assertEqual(event.topology.video.pid, 258)
        finally:
            if monitor is not None:
                monitor.close()
            sender_stop.set()
            sender_thread.join(1)

    def test_initial_psi_acquisition_still_obeys_xml_stale_deadline(self):
        class Process:
            @staticmethod
            def is_alive():
                return True

        class Receiver:
            @staticmethod
            def poll():
                return False

        with tempfile.TemporaryDirectory() as directory:
            monitor = object.__new__(TopologyMonitor)
            monitor.input_plugin = "ip"
            monitor.initial_topology = None
            monitor.poll_interval = 0.01
            monitor.stale_timeout = 0.15
            monitor._stop_event = threading.Event()
            monitor._lock = threading.Lock()
            monitor._sequence = 0
            monitor._consumed_sequence = 0
            monitor._latest_event = None
            monitor.pat_path = os.path.join(directory, "pat.xml")
            monitor.pmt_path = os.path.join(directory, "pmt.xml")
            monitor._receiver = Receiver()
            monitor._process = Process()
            monitor._thread = threading.Thread(target=monitor._run)
            started = time.monotonic()
            monitor._thread.start()
            try:
                deadline = started + 1
                event = None
                while event is None and time.monotonic() < deadline:
                    event = monitor.poll()
                    time.sleep(0.01)
                self.assertIsNotNone(event)
                self.assertIsInstance(event.error, TopologyUnavailableError)
                self.assertEqual(str(event.error), "PAT/PMT updates stopped")
                self.assertLess(time.monotonic() - started, 1)
            finally:
                monitor._stop_event.set()
                monitor._thread.join(1)
            self.assertFalse(monitor._thread.is_alive())

    def test_unavailable_ip_source_enters_bounded_recovery(self):
        monitor = Mock()
        monitor.poll.side_effect = [
            None,
            ts_topology.TopologyMonitorEvent(
                None, TopologyUnavailableError("PAT/PMT updates stopped")),
        ]
        monitor_factory = Mock(return_value=monitor)

        output = io.StringIO()
        recovery = InputRecovery(
            "test", "ip", ["0"], lambda topology: {},
            output, topology_monitor_factory=monitor_factory)
        started = time.monotonic()
        start_time = time.time()
        try:
            recovery.step(now=start_time)
            deadline = started + 5
            while recovery.next_probe_ts == 0 and time.monotonic() < deadline:
                recovery.step(now=time.time())
                time.sleep(0.02)
            elapsed = time.monotonic() - started
            self.assertEqual(recovery.state, "waiting")
            self.assertGreater(recovery.next_probe_ts, start_time)
            self.assertIsNone(recovery.supervisor)
            self.assertIsNone(recovery.topology_monitor)
            monitor_factory.assert_called_once()
            monitor.close.assert_called_once()
            self.assertEqual(monitor.poll.call_count, 2)
            self.assertLess(elapsed, 5)
        finally:
            recovery.close()

    def test_parses_thousands_separators_in_tsduck_numeric_fields(self):
        xml = ('<tsduck><PAT transport_stream_id="4,096">'
               '<service service_id="1" program_map_PID="4,096"/></PAT>'
               '<PMT service_id="1" PCR_PID="257"><metadata PID="4,096"/>'
               '<component elementary_PID="257" stream_type="0x1B"/>'
               '</PMT></tsduck>')

        topology = parse_topology_xml(xml)

        self.assertEqual(topology.transport_stream_id, 4096)
        self.assertEqual(topology.pmt_pid, 4096)

    def test_rejects_malformed_thousands_separators(self):
        xml = ('<tsduck><PAT transport_stream_id="1"><service service_id="1" '
               'program_map_PID="4,09"/></PAT><PMT service_id="1" PCR_PID="257">'
               '<metadata PID="256"/><component elementary_PID="257" '
               'stream_type="0x1B"/></PMT></tsduck>')

        with self.assertRaisesRegex(ValueError, "invalid comma-grouped integer"):
            parse_topology_xml(xml)

    def test_loads_tsduck_binding_from_tsp_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "prefix"
            tsp_path = prefix / "bin" / "tsp"
            binding_path = prefix / "share" / "tsduck" / "python"
            tsp_path.parent.mkdir(parents=True)
            tsp_path.touch()
            binding_path.mkdir(parents=True)
            (binding_path / "tsduck.py").write_text(
                "BINDING_MARKER = 'found'\n", encoding="utf-8")

            paths = [entry for entry in sys.path
                     if "tsduck/python" not in str(entry)]
            with patch.dict(sys.modules):
                sys.modules.pop("tsduck", None)
                with patch.object(ts_topology.shutil, "which",
                                  return_value=str(tsp_path)), \
                        patch.dict(os.environ, {"TSDUCK_PYTHON_PATH": ""}), \
                        patch.object(ts_topology.sys, "path", paths):
                    module = ts_topology._load_tsduck()

            self.assertEqual(module.BINDING_MARKER, "found")
            self.assertIn(str(binding_path.resolve()), paths)

    def test_timeout_kills_nonreturning_probe_and_cleans_parent_resources(self):
        created_directories = []
        create_directory = ts_topology._temporary_probe_directory

        def track_directory():
            directory = create_directory()
            created_directories.append(directory)
            return directory

        started = time.monotonic()
        with patch.object(ts_topology, "_temporary_probe_directory", track_directory):
            with self.assertRaises(TopologyUnavailableError):
                ts_topology._run_topology_probe(
                    "ip", ["239.1.1.1:5000"], timeout=1.5,
                    worker_target=_nonreturning_probe_worker)

        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(created_directories), 1)
        self.assertFalse(os.path.exists(created_directories[0].name))
        self.assertFalse(any(child.name == "tsduck-topology-probe"
                             for child in multiprocessing.active_children()))

    def test_cancelled_probe_reaps_child_process(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.1, cancel_event.set)
        timer.start()
        try:
            with self.assertRaises(ts_topology._TopologyProbeCancelled):
                ts_topology._run_topology_probe(
                    "ip", ["239.1.1.1:5000"], timeout=5,
                    worker_target=_nonreturning_probe_worker,
                    cancel_event=cancel_event)
        finally:
            timer.join()

        self.assertFalse(any(child.name == "tsduck-topology-probe"
                             for child in multiprocessing.active_children()))

    def test_background_monitor_publishes_topology_updates_and_stops(self):
        pat = _section(0, bytes.fromhex("0001c10000"), bytes.fromhex("0001e100"))
        pmt = _section(2, bytes.fromhex("0001c10000e101f000"),
                       bytes.fromhex("1be101f000"))
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "input.ts")
            with open(source, "wb") as stream:
                stream.write(_packet(0, pat) + _packet(0x100, pmt))
            monitor = TopologyMonitor(
                "file", [source, "--infinite"], ("initial topology",),
                poll_interval=0.01, stale_timeout=2)
            try:
                self.assertIsNone(monitor.stdout)
                self.assertEqual(monitor._process.name, "tsduck-input-pipeline")
                deadline = time.monotonic() + 5
                event = None
                while event is None and time.monotonic() < deadline:
                    event = monitor.poll()
                    time.sleep(0.01)
                self.assertIsNotNone(event)
                self.assertEqual(event.topology.video.pid, 257)
                self.assertEqual(event.topology.pmt_pid, 256)
                self.assertIsNone(event.error)
            finally:
                monitor.close()

        self.assertFalse(monitor._thread.is_alive())
        self.assertIsNone(monitor.stdout)
        self.assertFalse(any(child.name == "tsduck-input-pipeline"
                             for child in multiprocessing.active_children()))

    def test_sigkill_reaps_probe_that_ignores_terminate(self):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_stubborn_probe_worker,
            args=("ip", [], "/unused/tables.xml", sender),
            name="tsduck-topology-probe")
        process.start()
        sender.close()
        try:
            self.assertTrue(receiver.poll(3), "probe did not start")
            self.assertEqual(receiver.recv(), ("ready",))
            ts_topology._stop_and_reap_probe(process, terminate_first=True)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, -signal.SIGKILL)
        finally:
            receiver.close()
            sender.close()
            if process.is_alive():
                ts_topology._stop_and_reap_probe(process, terminate_first=True)
            if not process.is_alive():
                process.close()

    def test_probe_timeout_enters_manager_wait_and_recovers_on_retry(self):
        topology = ("available topology",)
        discoveries = []

        def discover(plugin, args):
            discoveries.append((plugin, args))
            if len(discoveries) == 1:
                raise TopologyUnavailableError("TSDuck PAT/PMT topology probe timed out")
            return topology

        class Supervisor:
            input_lost = False

            def __init__(self, channel, commands, out):
                pass

            def step(self, now):
                pass

            def close(self):
                pass

        recovery = InputRecovery(
            "test", "ip", ["239.1.1.1:5000"], lambda found: {},
            io.StringIO(), discoverer=discover, supervisor_factory=Supervisor)
        recovery.step(now=0)
        self.assertEqual(recovery.state, "waiting")
        self.assertEqual(discoveries, [("ip", ["239.1.1.1:5000"])])
        recovery.step(now=recovery.next_probe_ts)
        self.assertEqual(recovery.state, "running")
        self.assertEqual(recovery.topology, topology)
        self.assertEqual(len(discoveries), 2)
        recovery.close()

    def test_tsduck_api_discovers_order_and_es_metadata(self):
        pat = _section(0, bytes.fromhex("0001c10000"), bytes.fromhex("0001e100"))
        pmt_body = bytes.fromhex(
            "1be101f000"                  # H.264 video, PID 0x101.
            "0fe102f00b0a04656e6700fd03414243"  # AAC, language, private 0xFD.
            "0fe103f000"                  # Second audio ES without descriptors.
        )
        pmt = _section(2, bytes.fromhex("0001c10000e101f000"), pmt_body)
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "input.ts")
            with open(source, "wb") as stream:
                stream.write(_packet(0, pat) + _packet(0x100, pmt))
            topology = discover_topology("file", [source])

        self.assertEqual((topology.transport_stream_id, topology.program_number), (1, 1))
        self.assertEqual((topology.pmt_pid, topology.pcr_pid), (0x100, 0x101))
        self.assertEqual((topology.video.pid, topology.video.stream_type, topology.video.codec),
                         (0x101, 0x1B, "H.264/AVC"))
        self.assertEqual([stream.pid for stream in topology.audio], [0x102, 0x103])
        self.assertEqual([stream.audio_ordinal for stream in topology.audio], [1, 2])
        self.assertIsNone(topology.video.audio_ordinal)
        self.assertEqual(topology.audio[0].languages, ("eng",))
        self.assertEqual(topology.audio[0].private_0xfd, (b"ABC",))
        self.assertIn(0xFD, [descriptor.tag for descriptor in topology.audio[0].descriptors])
        self.assertEqual([stream.pid for stream in topology.streams], [0x101, 0x102, 0x103])

    def test_audio_selection_defaults_to_all_and_false_keeps_all(self):
        topology = _parse_audio_components([
            '<component elementary_PID="258" stream_type="0x0F"/>',
            '<component elementary_PID="259" stream_type="0x0F"/>',
        ])

        default_selection = select_audio_streams(topology)
        false_selection = select_audio_streams(topology, discard_first_audio=False)

        for selection in (default_selection, false_selection):
            self.assertIs(selection.topology, topology)
            self.assertIs(selection.audio, topology.audio)
            self.assertEqual(selection.audio, topology.audio)

    def test_audio_selection_discards_only_source_ordinal_one(self):
        topology = _parse_audio_components([
            '<component elementary_PID="258" stream_type="0x0F">'
            '<data_component_descriptor data_component_id="0x4142">'
            '<additional_data_component_info>43</additional_data_component_info>'
            '</data_component_descriptor>'
            '<ISO_639_language_descriptor><language code="eng"/></ISO_639_language_descriptor>'
            '</component>',
            '<component elementary_PID="259" stream_type="0x0F">'
            '<data_component_descriptor data_component_id="0x4445">'
            '<additional_data_component_info>46</additional_data_component_info>'
            '</data_component_descriptor>'
            '<ISO_639_language_descriptor><language code="fra"/></ISO_639_language_descriptor>'
            '</component>',
            '<component elementary_PID="260" stream_type="0x0F">'
            '<data_component_descriptor data_component_id="0x4748">'
            '<additional_data_component_info>49</additional_data_component_info>'
            '</data_component_descriptor>'
            '<ISO_639_language_descriptor><language code="spa"/></ISO_639_language_descriptor>'
            '</component>',
        ])
        source_audio = topology.audio
        source_streams = topology.streams

        selection = select_audio_streams(topology, discard_first_audio=True)

        self.assertIs(selection.topology, topology)
        self.assertIs(topology.audio, source_audio)
        self.assertIs(topology.streams, source_streams)
        self.assertEqual(
            [(stream.pid, stream.audio_ordinal, stream.audio_label, stream.language)
             for stream in selection.audio],
            [(259, 2, "DEF", "fra"), (260, 3, "GHI", "spa")])
        self.assertEqual([stream.private_0xfd for stream in selection.audio],
                         [(b"DEF",), (b"GHI",)])
        self.assertIs(selection.audio[0], source_audio[1])
        self.assertIs(selection.audio[1], source_audio[2])
        with self.assertRaises(AttributeError):
            selection.audio = ()

    def test_audio_selection_with_no_audio_is_empty(self):
        topology = _parse_audio_components([])

        selection = select_audio_streams(topology, discard_first_audio=True)

        self.assertIs(selection.topology, topology)
        self.assertEqual(selection.audio, ())
        self.assertEqual([stream.pid for stream in topology.streams], [257])

    def test_audio_selection_can_leave_no_audio(self):
        topology = _parse_audio_components([
            '<component elementary_PID="258" stream_type="0x0F"/>',
        ])

        selection = select_audio_streams(topology, discard_first_audio=True)

        self.assertIs(selection.topology, topology)
        self.assertEqual(selection.audio, ())
        self.assertEqual([stream.pid for stream in topology.audio], [258])
        self.assertEqual([stream.pid for stream in topology.streams], [257, 258])

    def test_rejects_multiple_programs_and_unsupported_streams(self):
        base = '<tsduck><PAT transport_stream_id="0x1"><service service_id="0x1" program_map_PID="0x100"/>{services}</PAT><PMT service_id="0x1" PCR_PID="0x101"><metadata PID="256"/>{components}</PMT></tsduck>'
        with self.assertRaisesRegex(TopologyError, "exactly one"):
            parse_topology_xml(base.format(
                services='<service service_id="0x2" program_map_PID="0x200"/>',
                components='<component elementary_PID="0x101" stream_type="0x1B"/>'))
        with self.assertRaisesRegex(TopologyError, "unsupported or ambiguous"):
            parse_topology_xml(base.format(
                services="", components='<component elementary_PID="0x101" stream_type="0x1B"/><component elementary_PID="0x102" stream_type="0x99"/>'))

    def test_ignores_private_section_stream_type(self):
        xml = ('<tsduck><PAT transport_stream_id="1"><service service_id="1" '
               'program_map_PID="256"/></PAT><PMT service_id="1" PCR_PID="257">'
               '<metadata PID="256"/><component elementary_PID="257" '
               'stream_type="0x1B"/><component elementary_PID="258" '
               'stream_type="0x05"/></PMT></tsduck>')

        topology = parse_topology_xml(xml)

        self.assertEqual([stream.pid for stream in topology.streams], [257])
        self.assertEqual(topology.video.pid, 257)

    def test_rejects_multiple_video_streams(self):
        xml = '<tsduck><PAT transport_stream_id="1"><service service_id="1" program_map_PID="256"/></PAT><PMT service_id="1" PCR_PID="257"><metadata PID="256"/><component elementary_PID="257" stream_type="0x1B"/><component elementary_PID="258" stream_type="0x02"/></PMT></tsduck>'
        with self.assertRaisesRegex(TopologyError, "exactly one video"):
            parse_topology_xml(xml)

    def test_rejects_pmt_pid_mismatched_with_pat(self):
        xml = '<tsduck><PAT transport_stream_id="1"><service service_id="1" program_map_PID="256"/></PAT><PMT service_id="1" PCR_PID="257"><metadata PID="257"/><component elementary_PID="257" stream_type="0x1B"/></PMT></tsduck>'
        with self.assertRaisesRegex(TopologyError, "PMT PID does not match the PAT program_map_PID"):
            parse_topology_xml(xml)

    def test_normalizes_descriptor_labels_and_known_languages(self):
        topology = _parse_audio_components([
            '<component elementary_PID="258" stream_type="0x0F">'
            '<data_component_descriptor data_component_id="0x4142">'
            '<additional_data_component_info>43</additional_data_component_info>'
            '</data_component_descriptor>'
            '<ISO_639_language_descriptor><language code="eng"/></ISO_639_language_descriptor>'
            '</component>',
            '<component elementary_PID="259" stream_type="0x0F">'
            '<ISO_639_language_descriptor><language code="fra"/></ISO_639_language_descriptor>'
            '</component>',
        ])
        self.assertEqual([stream.audio_label for stream in topology.audio], ["ABC", "audio-2"])
        self.assertEqual([stream.audio_ordinal for stream in topology.audio], [1, 2])
        self.assertEqual(
            {stream.pid: stream.audio_label for stream in topology.audio},
            {258: "ABC", 259: "audio-2"})
        self.assertEqual([stream.language for stream in topology.audio], ["eng", "fra"])
        self.assertEqual(len(topology.audio), 2)

    def test_falls_back_for_missing_empty_non_ascii_and_unknown_metadata(self):
        with self.assertLogs("ts_topology", level="WARNING") as logs:
            topology = _parse_audio_components([
                '<component elementary_PID="258" stream_type="0x0F"/>',
                '<component elementary_PID="259" stream_type="0x0F">'
                '<data_component_descriptor data_component_id="0x0000"/>'
                '<ISO_639_language_descriptor><language code="zzz"/></ISO_639_language_descriptor>'
                '</component>',
                '<component elementary_PID="260" stream_type="0x0F">'
                '<data_component_descriptor data_component_id="0x4142">'
                '<additional_data_component_info>ff</additional_data_component_info>'
                '</data_component_descriptor>'
                '<ISO_639_language_descriptor><language code="!"/></ISO_639_language_descriptor>'
                '</component>',
                '<component elementary_PID="261" stream_type="0x0F">'
                '<ISO_639_language_descriptor><language code="und"/></ISO_639_language_descriptor>'
                '</component>',
            ])
        self.assertEqual([stream.audio_label for stream in topology.audio],
                         ["audio-1", "audio-2", "audio-3", "audio-4"])
        self.assertEqual([stream.audio_ordinal for stream in topology.audio], [1, 2, 3, 4])
        self.assertEqual([stream.language for stream in topology.audio],
                         ["en", "en", "en", "en"])
        self.assertEqual([stream.pid for stream in topology.audio], [258, 259, 260, 261])
        self.assertTrue(any("using audio-1" in message for message in logs.output))

    def test_all_colliding_descriptor_labels_fall_back_and_log(self):
        component = ('<component elementary_PID="{pid}" stream_type="0x0F">'
                     '<data_component_descriptor data_component_id="0x4142">'
                     '<additional_data_component_info>43</additional_data_component_info>'
                     '</data_component_descriptor></component>')
        with self.assertLogs("ts_topology", level="WARNING") as logs:
            topology = _parse_audio_components([component.format(pid=258),
                                                component.format(pid=259)])
        self.assertEqual([stream.audio_label for stream in topology.audio],
                         ["audio-1", "audio-2"])
        self.assertEqual([stream.audio_ordinal for stream in topology.audio], [1, 2])
        self.assertTrue(any("collides" in message for message in logs.output))


if __name__ == "__main__":
    unittest.main()
