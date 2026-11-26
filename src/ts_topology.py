"""Discover a single-program MPEG-TS topology with TSDuck's Python bindings."""

import importlib
from pathlib import Path
import errno
import logging
import multiprocessing
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import NamedTuple, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET


class TopologyError(ValueError):
    """The PAT/PMT topology is unsupported or ambiguous."""


class TopologyUnavailableError(TopologyError):
    """The input did not provide PAT/PMT before the bounded probe expired."""


class _TopologyProbeCancelled(Exception):
    """A topology probe was cancelled while stopping its monitor."""


class Descriptor(NamedTuple):
    tag: Optional[int]
    name: str
    xml: str


class ElementaryStream(NamedTuple):
    pid: int
    stream_type: int
    media_type: str
    codec: str
    descriptors: Tuple[Descriptor, ...]
    languages: Tuple[str, ...]
    private_0xfd: Tuple[bytes, ...]
    audio_label: Optional[str]
    language: Optional[str]
    audio_ordinal: Optional[int] = None


class Topology(NamedTuple):
    transport_stream_id: int
    program_number: int
    pmt_pid: int
    pcr_pid: int
    video: ElementaryStream
    audio: Tuple[ElementaryStream, ...]
    streams: Tuple[ElementaryStream, ...]
    pat_version: int = 0
    pmt_version: int = 0


def same_media_topology(left, right):
    """Compare stream topology while ignoring PAT/PMT version counters."""
    if left is None or right is None:
        return left is right
    if isinstance(left, Topology) and isinstance(right, Topology):
        return left._replace(pat_version=0, pmt_version=0) == \
            right._replace(pat_version=0, pmt_version=0)
    return left == right


def describe_topology_change(previous, current):
    """Describe the fields that differ between topology snapshots."""
    if not isinstance(previous, Topology) or not isinstance(current, Topology):
        return f"{previous!r} -> {current!r}"

    changes = [
        f"{field}: {getattr(previous, field)!r} -> {getattr(current, field)!r}"
        for field in Topology._fields
        if field not in ("pat_version", "pmt_version")
        and getattr(previous, field) != getattr(current, field)
    ]
    version_changes = [
        f"{field}: {getattr(previous, field)} -> {getattr(current, field)}"
        for field in ("pat_version", "pmt_version")
        if getattr(previous, field) != getattr(current, field)
    ]
    if not changes:
        changes.append("no media fields changed")
    if version_changes:
        changes.append("PSI version counters (ignored): " +
                       ", ".join(version_changes))
    return "; ".join(changes)


class AudioSelection(NamedTuple):
    """Retained audio streams paired with their unchanged source topology."""

    topology: Topology
    audio: Tuple[ElementaryStream, ...]


def select_audio_streams(topology: Topology,
                         discard_first_audio: bool = False) -> AudioSelection:
    """Select retained audio without changing source stream identities or order."""
    audio = topology.audio
    if discard_first_audio:
        audio = tuple(stream for stream in audio if stream.audio_ordinal != 1)
    return AudioSelection(topology, audio)


_VIDEO = {0x01: "MPEG-1 Video", 0x02: "MPEG-2 Video", 0x10: "MPEG-4 Visual",
          0x1B: "H.264/AVC", 0x24: "H.265/HEVC"}
_AUDIO = {0x03: "MPEG-1 Audio", 0x04: "MPEG-2 Audio", 0x0F: "AAC",
          0x11: "AAC-LATM", 0x81: "AC-3", 0x87: "E-AC-3"}
_DESCRIPTOR_TAGS = {"ISO_639_language_descriptor": 0x0A,
                    "data_component_descriptor": 0xFD}
_LOG = logging.getLogger(__name__)
TOPOLOGY_PROBE_TIMEOUT_SECONDS = 10.0
TOPOLOGY_EXIT_GRACE_SECONDS = 0.25
TOPOLOGY_KILL_GRACE_SECONDS = 1.0
_ISO639_CODES = frozenset("""
aa aar ab abk ace ach ada ady ae af afa afh afr ain ak aka akk ale alg alt am amh an ang anp apa
ar ara arc arg arn arp art arw as asm ast ath aus av ava ave awa ay aym az aze ba bad bai bak bal
bam ban bas bat be bej bel bem ben ber bg bh bho bi bih bik bin bis bla bm bn bnt bo bod bos br bra
bre bs btk bua bug bul byn ca cad cai car cat cau ce ceb cel ces ch cha chb che chg chk chm chn cho
chp chr chu chv chy cmc cnr co cop cor cos cpe cpf cpp cr cre crh crp cs csb cu cus cv cy cym da
dak dan dar day de del den deu dgr din div doi dra dsb dua dum dv dyu dz dzo ee efi egy eka el ell
elx en eng enm eo epo es est et eu eus ewe ewo fa fan fao fas fat ff fi fij fil fin fiu fj fo fon fr
fra frm fro frr frs fry ful fur fy ga gaa gay gba gd gem gez gil gl gla gle glg glv gmh gn goh gon
gor got grb grc grn gsw gu guj gv gwi ha hai hat hau haw he heb her hi hil him hin hit hmn hmo ho hr
hrv hsb ht hu hun hup hy hye hz ia iba ibo id ido ie ig ii iii ijo ik iku ile ilo ina inc ind ine inh
io ipk ira iro is isl it ita iu ja jav jbo jpn jpr jrb jv ka kaa kab kac kal kam kan kar kas kat kau
kaw kaz kbd kg kha khi khm kho ki kik kin kir kj kk kl km kmb kn ko kok kom kon kor kos kpe kr krc
krl kro kru ks ku kua kum kur kut kv kw ky la lad lah lam lao lat lav lb lez lg li lim lin lit ln lo
lol loz lt ltz lu lua lub lug lui lun luo lus lv mad mag mah mai mak mal man map mar mas mdf mdr men
mg mga mh mi mic min mis mk mkd mkh ml mlg mlt mn mnc mni mno moh mon mos mr mri ms msa mt mul mun
mus mwl mwr my mya myn myv na nah nai nap nau nav nb nbl nd nde ndo nds ne nep new ng nia nic niu nl
nld nn nno no nob nog non nor nqo nr nso nub nv nwc ny nya nym nyn nyo nzi oc oci oj oji om ori or
orm os osa oss ota oto pa paa pag pal pam pan pap pau peo phi phn pi pl pli pol pon por pra pro ps pt
pus qu que raj rap rar rm rn ro roa roh rom ron rup ru run rus rw sa sad sag sah sai sal sam san sas
sat sc scn sco sd se sel sem sg sga sgn shn si sid sin sio sit sk sla slk slv sl sma sme smi sm smj
smn smo sms sn sna snd snk so sog som son sot spa sq sqi sr srd srn srp srr ss ssa ssw st su suk sun
sus sux sv sw swa swe syc syr ta tah tai tam tat te tel tem ter tet tg tgk tgl th tha ti tig tir tiv tk
tkl tl tlh tli tmh tn to tog ton tpi tr ts tsi tsn tso tt tuk tum tup tur tut tvl tw twi ty tyv udm ug
uga uig uk ukr umb und ur urd uz uzb vai ve ven vi vie vo vol vot wa wak wal war was wen wln wo wol
xal xh xho yao yap yi yid yo yor ypk za zap zbl zen zgh zh zha zho znd zu zul zun zxx zza
""".split())


def _number(value):
    if isinstance(value, str) and "," in value:
        value = value.strip()
        if not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+", value):
            raise ValueError(f"invalid comma-grouped integer: {value!r}")
        value = value.replace(",", "")
    return int(value, 0)


def _fd_payload(element):
    component_id = _number(element.get("data_component_id", "0"))
    extra = element.findtext("additional_data_component_info", "")
    return component_id.to_bytes(2, "big") + bytes.fromhex(extra)


def _audio_descriptor_label(stream, ordinal):
    for value in stream.private_0xfd:
        try:
            label = value.decode("ascii").strip()
        except UnicodeDecodeError:
            continue
        if label and label.isprintable():
            return label
    reason = "missing" if not stream.private_0xfd else "empty or invalid ASCII"
    fallback = f"audio-{ordinal}"
    _LOG.warning("Audio ES PID %d has %s 0xFD label; using %s",
                 stream.pid, reason, fallback)
    return None


def _normalize_language(value):
    code = value.strip().lower()
    if (code.isascii() and code.isalpha() and len(code) in (2, 3)
            and code in _ISO639_CODES and code != "und"):
        return code
    return "en"


def _normalize_audio(streams):
    audio = [stream for stream in streams if stream.media_type == "audio"]
    labels = [_audio_descriptor_label(stream, ordinal)
              for ordinal, stream in enumerate(audio, start=1)]
    while True:
        resolved = [label if label is not None else f"audio-{ordinal}"
                    for ordinal, label in enumerate(labels, start=1)]
        collisions = {}
        for index, label in enumerate(resolved):
            collisions.setdefault(label, []).append(index + 1)
        duplicates = {label: indices for label, indices in collisions.items()
                      if len(indices) > 1}
        if not duplicates:
            break
        for label, indices in duplicates.items():
            _LOG.warning("Audio label %r collides for ordinals %s; using audio-N fallbacks",
                         label, indices)
            for ordinal in indices:
                labels[ordinal - 1] = None

    normalized = []
    audio_ordinal = 0
    for stream in streams:
        if stream.media_type == "audio":
            language = _normalize_language(stream.languages[0]) if stream.languages else "en"
            ordinal = audio_ordinal + 1
            stream = stream._replace(audio_label=resolved[audio_ordinal], language=language,
                                     audio_ordinal=ordinal)
            audio_ordinal += 1
        normalized.append(stream)
    return normalized


def parse_topology_xml(xml_text: str) -> Topology:
    """Validate and model one PAT/PMT pair from TSDuck XML."""
    root = ET.fromstring(xml_text)
    pats, pmts = root.findall("PAT"), root.findall("PMT")
    if len(pats) != 1 or len(pmts) != 1:
        raise TopologyError("expected exactly one complete PAT and PMT")
    pat, pmt = pats[0], pmts[0]
    services = [s for s in pat.findall("service") if _number(s.get("service_id", "0"))]
    if len(services) != 1:
        raise TopologyError("expected exactly one non-network program in PAT")
    service = services[0]
    program = _number(service.get("service_id"))
    if _number(pmt.get("service_id", "0")) != program:
        raise TopologyError("PMT does not match the PAT program")
    pmt_metadata = pmt.findall("metadata")
    if len(pmt_metadata) != 1 or pmt_metadata[0].get("PID") is None:
        raise TopologyError("expected exactly one PMT PID")
    pmt_pid = _number(pmt_metadata[0].get("PID"))
    pat_pmt_pid = _number(service.get("program_map_PID"))
    if pmt_pid != pat_pmt_pid:
        raise TopologyError("PMT PID does not match the PAT program_map_PID")
    if pat.get("current", "true").lower() != "true" or pmt.get("current", "true").lower() != "true":
        raise TopologyError("PAT/PMT is not current")

    streams = []
    for component in pmt.findall("component"):
        stream_type = _number(component.get("stream_type", "0"))
        if stream_type == 0x05:
            continue
        desc_elements = list(component)
        descs = tuple(Descriptor(
            _DESCRIPTOR_TAGS.get(e.tag, _number(e.get("tag", "0")) if e.get("tag") else None),
            e.tag, ET.tostring(e, encoding="unicode")) for e in desc_elements)
        codec = _VIDEO.get(stream_type) or _AUDIO.get(stream_type)
        media = "video" if stream_type in _VIDEO else "audio" if stream_type in _AUDIO else None
        if stream_type == 0x06:
            names = " ".join(d.name.lower() for d in descs)
            matches = [("audio", name) for marker, name in
                       (("enhanced_ac3", "E-AC-3"), ("e_ac3", "E-AC-3"),
                        ("ac3", "AC-3"), ("dts", "DTS"), ("aac", "AAC"))
                       if marker in names]
            if len(matches) == 1:
                media, codec = matches[0]
        if media is None:
            raise TopologyError(f"unsupported or ambiguous stream type 0x{stream_type:02X}")
        languages = tuple(lang.get("code", "") for d in desc_elements
                          if d.tag == "ISO_639_language_descriptor"
                          for lang in d.findall("language"))
        private = tuple(_fd_payload(d) for d in desc_elements
                        if d.tag == "data_component_descriptor")
        streams.append(ElementaryStream(
            _number(component.get("elementary_PID", "0")), stream_type, media, codec,
            descs, languages, private, None, None))

    videos = [stream for stream in streams if stream.media_type == "video"]
    if len(videos) != 1:
        raise TopologyError(f"expected exactly one video elementary stream, found {len(videos)}")
    streams = _normalize_audio(streams)
    videos = [stream for stream in streams if stream.media_type == "video"]
    return Topology(_number(pat.get("transport_stream_id", "0")), program,
                    pat_pmt_pid,
                    _number(pmt.get("PCR_PID", "0")), videos[0],
                    tuple(s for s in streams if s.media_type == "audio"), tuple(streams),
                    _number(pat.get("version", "0")),
                    _number(pmt.get("version", "0")))


def _temporary_probe_directory():
    return tempfile.TemporaryDirectory()


def _tsduck_python_paths():
    candidates = []
    configured = os.environ.get("TSDUCK_PYTHON_PATH", "")
    candidates.extend(Path(path).expanduser() for path in configured.split(os.pathsep)
                     if path)

    tsp_path = shutil.which("tsp")
    if tsp_path:
        prefix = Path(tsp_path).resolve().parent.parent
        candidates.append(prefix / "share" / "tsduck" / "python")

    candidates.extend((
        Path("/usr/share/tsduck/python"),
        Path("/usr/local/share/tsduck/python"),
    ))

    unique = []
    seen = set()
    for candidate in candidates:
        path = str(candidate.resolve())
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _load_tsduck():
    try:
        return importlib.import_module("tsduck")
    except ModuleNotFoundError as error:
        if error.name != "tsduck":
            raise

    searched = []
    for path in _tsduck_python_paths():
        if not (Path(path) / "tsduck.py").is_file():
            continue
        searched.append(path)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            return importlib.import_module("tsduck")
        except ModuleNotFoundError as error:
            if error.name != "tsduck":
                raise

    locations = ", ".join(searched) if searched else "no standard binding directory found"
    raise RuntimeError(
        "TSDuck Python binding is unavailable. Install the matching TSDuck Python "
        "binding or set TSDUCK_PYTHON_PATH/PYTHONPATH; checked: "
        f"{locations}")


def _topology_probe_worker(input_plugin, input_args, xml_path, connection):
    """Run all potentially blocking TSDuck native work in the killable child."""
    native_objects = []
    try:
        tsduck = _load_tsduck()

        report = tsduck.NullReport()
        processor = tsduck.TSProcessor(report)
        duck = tsduck.DuckContext(report)
        section_file = tsduck.SectionFile(duck)
        native_objects.extend((section_file, processor, duck, report))
        processor.input = [input_plugin, *input_args]
        processor.plugins = [["tables", "--tid", "0", "--tid", "2",
                              "--max-tables", "2", "--xml-output", xml_path]]
        processor.output = ["drop"]
        processor.start()
        processor.waitForTermination()
        if not section_file.loadXML(xml_path):
            connection.send(("topology-error",
                             "TSDuck could not load collected PAT/PMT tables"))
        else:
            connection.send(("ok", Path(xml_path).read_text(encoding="utf-8")))
    except BaseException as error:
        try:
            connection.send(("error", f"{type(error).__name__}: {error}"))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        for native_object in native_objects:
            try:
                native_object.delete()
            except Exception:
                _LOG.exception("Failed deleting TSDuck native object in probe child")
        connection.close()


def _stop_and_reap_probe(process, terminate_first=False):
    """Stop the isolated probe and bound every wait, escalating to SIGKILL."""
    if not terminate_first:
        process.join(TOPOLOGY_EXIT_GRACE_SECONDS)
    if process.is_alive():
        try:
            process.terminate()
        except Exception:
            _LOG.exception("Could not send graceful termination to TSDuck probe")
        process.join(TOPOLOGY_EXIT_GRACE_SECONDS)
    if process.is_alive():
        try:
            killer = getattr(process, "kill", None)
            if killer is not None:
                killer()
            else:
                os.kill(process.pid, signal.SIGKILL)
        except Exception:
            _LOG.exception("Could not kill TSDuck probe process through multiprocessing")
            try:
                os.kill(process.pid, signal.SIGKILL)
            except OSError as error:
                if error.errno != errno.ESRCH:
                    _LOG.exception("Could not send SIGKILL to TSDuck probe")
        process.join(TOPOLOGY_KILL_GRACE_SECONDS)
    if process.is_alive():
        raise RuntimeError("TSDuck probe process could not be reaped after SIGKILL")


def _run_topology_probe(input_plugin, input_args, timeout=None,
                        worker_target=_topology_probe_worker, cancel_event=None):
    """Collect XML in a killable Python process, never in the manager process."""
    if cancel_event is not None and cancel_event.is_set():
        raise _TopologyProbeCancelled()
    timeout = TOPOLOGY_PROBE_TIMEOUT_SECONDS if timeout is None else timeout
    directory = _temporary_probe_directory()
    xml_path = str(Path(directory.name) / "tables.xml")
    receiver = sender = process = None
    started = False
    response = None
    timed_out = False
    cancelled = False
    try:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=worker_target,
            args=(input_plugin, list(input_args), xml_path, sender),
            name="tsduck-topology-probe")
        process.start()
        started = True
        sender.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                raise _TopologyProbeCancelled()
            if receiver.poll(min(0.05, max(0.0, deadline - time.monotonic()))):
                try:
                    message = receiver.recv()
                except EOFError:
                    break
                if message[0] == "ready":
                    continue
                response = message
                break
            if not process.is_alive():
                break
        timed_out = response is None and time.monotonic() >= deadline
        if timed_out:
            raise TopologyUnavailableError("TSDuck PAT/PMT topology probe timed out")
        if response is None:
            raise RuntimeError("TSDuck probe process exited without a result")
        if response[0] == "topology-error":
            raise TopologyError(response[1])
        if response[0] != "ok":
            raise RuntimeError(f"TSDuck probe failed: {response[1]}")
        return response[1]
    finally:
        if receiver is not None:
            receiver.close()
        if sender is not None:
            sender.close()
        try:
            if started:
                _stop_and_reap_probe(
                    process, terminate_first=timed_out or cancelled)
        finally:
            if started and not process.is_alive():
                process.close()
            directory.cleanup()


def discover_topology(input_plugin: str, input_args: Sequence[str], timeout=None,
                      cancel_event=None) -> Topology:
    """Collect PAT/PMT using TSDuck's Python TSProcessor and SectionFile APIs."""
    xml_text = _run_topology_probe(
        input_plugin, input_args, timeout=timeout, cancel_event=cancel_event)
    # SectionFile.toXML() omits the transport PID metadata required to
    # validate the PAT-to-PMT association.
    return parse_topology_xml(xml_text)


class TopologyMonitorEvent(NamedTuple):
    topology: Optional[Topology]
    error: Optional[Exception]


class _TSProcessorState:
    def __init__(self, done):
        self.name = "tsduck-input-pipeline"
        self._done = done

    def is_alive(self):
        return not self._done.is_set()


class TopologyMonitor:
    """Monitor PSI tables without forwarding MPEG-TS media bytes."""

    def __init__(self, input_plugin, input_args, initial_topology=None,
                 out_fp=None, poll_interval=0.1, stale_timeout=5.0):
        if poll_interval <= 0 or stale_timeout <= 0:
            raise ValueError("topology monitor timing values must be positive")
        self.input_plugin = input_plugin
        self.input_args = tuple(input_args)
        self.initial_topology = initial_topology
        self.poll_interval = poll_interval
        self.stale_timeout = stale_timeout
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._sequence = 0
        self._consumed_sequence = 0
        self._latest_event = None
        self._directory = _temporary_probe_directory()
        self.pat_path = str(Path(self._directory.name) / "pat.xml")
        self.pmt_path = str(Path(self._directory.name) / "pmt.xml")
        self._processor_objects = []
        self._processor_done = threading.Event()
        tsduck = _load_tsduck()
        report = tsduck.NullReport()
        duck = tsduck.DuckContext(report)
        section_file = tsduck.SectionFile(duck)
        processor = tsduck.TSProcessor(report)
        self._processor_objects.extend((section_file, processor, duck, report))
        processor.input = [self.input_plugin, *self.input_args]
        processor.plugins = [
            ["tables", "--tid", "0", "--rewrite-xml", "--xml-output", self.pat_path],
            ["tables", "--tid", "2", "--rewrite-xml", "--xml-output", self.pmt_path],
        ]
        processor.output = ["drop"]
        try:
            processor.start()
        except Exception as error:
            self._processor_error = error
            self._processor_done.set()
        self._processor = processor
        self._process = _TSProcessorState(self._processor_done)
        self.stdout = None
        self._termination_thread = None
        if self._process.is_alive():
            self._termination_thread = threading.Thread(
                target=self._wait_for_termination,
                name="tsduck-termination-waiter", daemon=True)
            self._termination_thread.start()
        self._thread = threading.Thread(
            target=self._run, name="mpegts-topology-monitor", daemon=True)
        self._thread.start()

    def _wait_for_termination(self):
        try:
            self._processor.waitForTermination()
        except Exception as error:
            self._processor_error = error
        finally:
            self._processor_done.set()

    def _publish(self, event):
        with self._lock:
            self._sequence += 1
            self._latest_event = event

    @staticmethod
    def _read_table(path, table_name):
        root = ET.parse(path).getroot()
        tables = root.findall(f".//{table_name}")
        if len(tables) != 1:
            raise TopologyError(f"expected exactly one current {table_name}")
        return ET.tostring(tables[0], encoding="unicode")

    def _read_topology(self, pat_path, pmt_path):
        pat_xml = self._read_table(pat_path, "PAT")
        pmt_xml = self._read_table(pmt_path, "PMT")
        return parse_topology_xml(f"<tsduck>{pat_xml}{pmt_xml}</tsduck>")

    def _run(self):
        previous_topology = self.initial_topology
        topology_accepted = previous_topology is not None
        published_error = None
        pending_error = None
        pending_error_since = None
        table_versions = {}
        last_table_time = time.monotonic()
        while not self._stop_event.is_set():
            if not self._process.is_alive():
                reason = getattr(self, "_processor_error", None) or \
                    "TSDuck input processor exited"
                self._publish(TopologyMonitorEvent(
                    None, TopologyUnavailableError(str(reason))))
                return

            now = time.monotonic()
            table_updated = False
            for path in (self.pat_path, self.pmt_path):
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue
                version = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                if table_versions.get(path) != version:
                    table_versions[path] = version
                    table_updated = True
            if table_updated:
                last_table_time = now

            if len(table_versions) == 2:
                try:
                    topology = self._read_topology(self.pat_path, self.pmt_path)
                except (ET.ParseError, OSError):
                    pass
                except ValueError as error:
                    error_key = (type(error), str(error))
                    if error_key != pending_error:
                        pending_error = error_key
                        pending_error_since = now
                    elif now - pending_error_since >= 2.0:
                        if error_key != published_error:
                            self._publish(TopologyMonitorEvent(None, error))
                        published_error = error_key
                else:
                    pending_error = None
                    pending_error_since = None
                    if topology != previous_topology or published_error is not None:
                        self._publish(TopologyMonitorEvent(topology, None))
                    previous_topology = topology
                    topology_accepted = True
                    published_error = None

            if (now - last_table_time >= self.stale_timeout
                    and not topology_accepted):
                error_key = (TopologyUnavailableError, "PAT/PMT updates stopped")
                if published_error != error_key:
                    self._publish(TopologyMonitorEvent(
                        None, TopologyUnavailableError("PAT/PMT updates stopped")))
                published_error = error_key
            self._stop_event.wait(self.poll_interval)

    def poll(self):
        with self._lock:
            if self._sequence == self._consumed_sequence:
                return None
            self._consumed_sequence = self._sequence
            return self._latest_event

    def close(self):
        self._stop_event.set()
        self._thread.join(TOPOLOGY_EXIT_GRACE_SECONDS +
                          TOPOLOGY_KILL_GRACE_SECONDS + 1.0)
        if self._thread.is_alive():
            raise RuntimeError("topology monitor thread did not stop")
        if self._process.is_alive():
            self._processor.abort()
        if self._termination_thread is not None:
            self._termination_thread.join(
                TOPOLOGY_EXIT_GRACE_SECONDS + TOPOLOGY_KILL_GRACE_SECONDS + 1.0)
            if self._termination_thread.is_alive():
                raise RuntimeError("TSDuck TSProcessor did not terminate")
        for native_object in self._processor_objects:
            native_object.delete()
        self._directory.cleanup()
