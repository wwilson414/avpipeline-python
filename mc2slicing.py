#!/usr/bin/env python3
"""
===============================================================================
HLS/DASH Main Channel Manager - Stable Simulator Edition
(Demuxed HLS/DASH + Wallclock/RTC + Dynamic Master + Video First)
===============================================================================

Positioning:
- Stable simulator for feeding downstream systems in testing/integration
- Not for formal broadcast production
- Prioritizes predictable behavior, observability, and stable output

Features:
- One TSDuck-discovered program topology and shared source URL per channel
- One FFmpeg A/V worker feeding one Shaka Packager worker over localhost UDP
- Shaka-owned HLS/DASH output, with bounded worker restart backoff
- Transition-oriented worker logs and ordered, reaped shutdown


Usage:
    ./mc2slicing.py {start|stop|restart|status|urls}
===============================================================================
"""

import os
import sys
import json
import time
import signal
import logging
import subprocess
import threading
import shutil
import re
import ipaddress
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from channel_config import resolve_channel_config
from ffmpeg_command import (
    FFMPEG_PROGRESS_INTERVAL_SEC, build_ffmpeg_command,
)
from shaka_command import build_shaka_command
from ts_topology import (
    TopologyError, TopologyMonitor, TopologyMonitorEvent,
    TopologyUnavailableError,
    describe_topology_change, discover_topology, same_media_topology,
    select_audio_streams,
)

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

JSON_FILE = "mvc.json"
PID_DIR = "/run/avpipeline"
WORKER_PID_DIR = "/run/avpipeline/workers"
LOG_DIR = "/var/log/avpipeline"

os.makedirs(PID_DIR, exist_ok=True)
os.makedirs(WORKER_PID_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("avpipeline")
logger.setLevel(logging.INFO)
logger.propagate = False
log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s")
for handler_type, handler_factory in (
        (logging.FileHandler, lambda: logging.FileHandler(
            os.path.join(LOG_DIR, "avpipeline.log"))),
        (logging.StreamHandler, lambda: logging.StreamHandler(sys.stdout))):
    if not any(
            type(handler) is handler_type
            and getattr(handler, "_avpipeline_handler", False)
            for handler in logger.handlers):
        handler = handler_factory()
        handler.setLevel(logging.INFO)
        handler.setFormatter(log_formatter)
        handler._avpipeline_handler = True
        logger.addHandler(handler)


def _write_channel_log(out_fp, channel_id, component, level, message):
    out_fp.write(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] "
        f"{channel_id} {component}: {message}\n")
    out_fp.flush()

WORKER_RESTART_BACKOFF_SEC = 3
WORKER_RESTART_MAX_BACKOFF_SEC = 60
WORKER_STABLE_THRESHOLD_SEC = 30  # Worker must run this long to reset backoff
SOURCE_PROBE_INTERVAL_SEC = 30
TRANSCODE_TOPOLOGY_PROBE_INTERVAL_SEC = 10
TOPOLOGY_STABILITY_SEC = 3.0
_NON_MONOTONIC_DTS = re.compile(
    r"Non-monotonic DTS; previous:\s*(-?\d+), current:\s*(-?\d+)")
_FFMPEG_PROGRESS_FIELDS = frozenset((
    "frame", "out_time", "drop_frames", "dup_frames", "speed", "total_size",
    "progress",
))

# ------------------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------------------

def check_dependencies():
    missing_tools = []
    if shutil.which("ffmpeg") is None:
        missing_tools.append("ffmpeg")
    if shutil.which("ffprobe") is None:
        missing_tools.append("ffprobe")
    if missing_tools:
        error_msg = f"CRITICAL ERROR: Required tool(s) not found in PATH: {', '.join(missing_tools)}"
        print(error_msg, file=sys.stderr)
        logger.error(error_msg)
        sys.exit(1)
    try:
        check_packager_version()
    except RuntimeError as exc:
        error_msg = f"CRITICAL ERROR: {exc}"
        print(error_msg, file=sys.stderr)
        logger.error(error_msg)
        sys.exit(1)


def check_packager_version():
    executable = shutil.which("packager")
    if executable is None:
        raise RuntimeError("Required Shaka Packager executable 'packager' not found in PATH")

    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not run Shaka Packager '{executable}' --version: {exc}") from exc

    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    if result.returncode != 0:
        detail = output or "no version output"
        raise RuntimeError(
            f"Shaka Packager '{executable}' --version failed "
            f"(exit status {result.returncode}): {detail}")

    version_lines = re.findall(
        r"(?im)^\s*(?:packager\s+)?version\s*[:=]?\s*v?"
        r"([0-9]+)\.([0-9]+)\.([0-9]+)(?:[-+][A-Za-z0-9][A-Za-z0-9.+-]*)?\s*$",
        output)
    if len(version_lines) != 1:
        raise RuntimeError(
            f"Shaka Packager '{executable}' returned empty or unrecognized "
            f"--version output: {output or '<empty>'}")
    version = tuple(int(component) for component in version_lines[0])
    if version != (3, 9, 3):
        found = ".".join(version_lines[0])
        raise RuntimeError(
            f"Shaka Packager version mismatch: required 3.9.3, "
            f"found {found!r}")


def check_packager_discontinuity_support():
    executable = shutil.which("packager")
    if executable is None:
        raise RuntimeError("Required Shaka Packager executable 'packager' not found in PATH")
    try:
        result = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"Could not inspect Shaka Packager '{executable}' features: {exc}") from exc

    help_output = "\n".join(
        part for part in (result.stdout, result.stderr) if part)
    if "--hls_discontinuity_signal_file" not in help_output:
        raise RuntimeError(
            "Shaka Packager 3.9.3 lacks runtime HLS discontinuity support; "
            "build and use the patched Packager executable")


def load_config():
    if not os.path.exists(JSON_FILE):
        logger.error(f"Configuration file {JSON_FILE} not found.")
        sys.exit(1)
    with open(JSON_FILE, "r") as f:
        return json.load(f)


def resolve_transcode_mode(channel_config: dict) -> str:
    """Resolve the channel's authoritative or encoder-inferred transcode mode."""
    if not isinstance(channel_config, dict):
        raise ValueError("channel configuration must be a mapping")

    if "transcode_mode" in channel_config:
        mode = channel_config["transcode_mode"]
        if not isinstance(mode, str) or mode not in ("transcode", "passthrough"):
            raise ValueError(
                "transcode_mode must be 'transcode' or 'passthrough'")
        return mode

    def encoder_is_unset(value: object) -> bool:
        return value is None or (
            isinstance(value, str) and value.strip().lower() == "none")

    if (encoder_is_unset(channel_config.get("video_encoder"))
            and encoder_is_unset(channel_config.get("audio_encoder"))):
        return "passthrough"
    return "transcode"


def is_running(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def save_pid(task_name, pid):
    with open(os.path.join(PID_DIR, f"{task_name}.pid"), "w") as f:
        f.write(str(pid))


def get_pid(task_name):
    pid_file = os.path.join(PID_DIR, f"{task_name}.pid")
    if os.path.exists(pid_file):
        with open(pid_file, "r") as f:
            try:
                return int(f.read().strip())
            except ValueError:
                return None
    return None


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def compute_backoff(restart_count):
    return min(
        WORKER_RESTART_BACKOFF_SEC * (2 ** max(0, restart_count - 1)),
        WORKER_RESTART_MAX_BACKOFF_SEC
    )


# ------------------------------------------------------------------------------
# Main manager loop
# ------------------------------------------------------------------------------

def source_input(source_url, local_multicast_addr=None,
                 require_multicast_interface=False):
    """Resolve the configured source once for its TSDuck and FFmpeg consumers."""
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("source URL must be a non-empty string")
    parsed = urlsplit(source_url)
    if parsed.scheme == "udp":
        address = parsed.netloc.lstrip("@")
        try:
            multicast = ipaddress.ip_address(parsed.hostname).is_multicast
        except (ValueError, TypeError):
            multicast = False

        topology_args = [address]
        ffmpeg_source = source_url
        if multicast:
            source_local_address = next(
                (value for key, value in parse_qsl(
                    parsed.query, keep_blank_values=True)
                 if key == "localaddr"),
                None)
            if require_multicast_interface and local_multicast_addr is None:
                raise ValueError(
                    "localMulticastAddr must be a valid IPv4 address "
                    "for multicast UDP input")
            if require_multicast_interface or local_multicast_addr is not None:
                try:
                    if (not isinstance(local_multicast_addr, str)
                            or not local_multicast_addr):
                        raise ValueError
                    local_address = str(ipaddress.IPv4Address(
                        local_multicast_addr))
                except (ipaddress.AddressValueError, ValueError):
                    raise ValueError(
                        "localMulticastAddr must be a valid IPv4 address "
                        "for multicast UDP input")
                topology_args.extend(("--local-address", local_address))

                query = [(key, value) for key, value in parse_qsl(
                    parsed.query, keep_blank_values=True)
                    if key != "localaddr"]
                query.append(("localaddr", local_address))
                ffmpeg_source = urlunsplit((
                    parsed.scheme, parsed.netloc, parsed.path,
                    urlencode(query), parsed.fragment))
            elif source_local_address:
                topology_args.extend(("--local-address", source_local_address))
        return {
            "kind": "multicast" if multicast else "udp-unicast",
            "source": source_url,
            "topology": ("ip", topology_args),
            "ffmpeg": ffmpeg_source,
        }

    if parsed.scheme == "file":
        return {
            "kind": "file",
            "source": source_url,
            "topology": ("file", [parsed.path]),
            "ffmpeg": source_url,
        }
    if parsed.scheme:
        raise ValueError(f"unsupported source URL scheme: {parsed.scheme}")
    return {
        "kind": "file",
        "source": source_url,
        "topology": ("file", [source_url]),
        "ffmpeg": source_url,
    }


def topology_input(source_url):
    """Translate a configured source URL to the TSDuck input plugin."""
    return source_input(source_url)["topology"]


def normalize_source_url(source_url, local_multicast_addr):
    """Return the FFmpeg-specific source representation."""
    return source_input(
        source_url, local_multicast_addr,
        require_multicast_interface=True)["ffmpeg"]


class ChannelSupervisor:
    """Own and supervise the active Packager and optional FFmpeg workers."""

    def __init__(self, channel_id, commands, out_fp, popen=subprocess.Popen):
        self.channel_id = channel_id
        self.commands = commands
        self.out_fp = out_fp
        self.popen = popen
        self.input_lost = False
        self.worker_roles = tuple(
            name for name in ("shaka", "ffmpeg") if name in commands)
        if "shaka" not in self.worker_roles:
            raise ValueError("commands must include a shaka worker")
        self.workers = {
            name: {"proc": None, "pid": None, "exit_status": None,
                   "restart_count": 0, "next_retry_ts": 0, "started_at": 0,
                   "last_logged": None, "log_thread": None}
            for name in self.worker_roles
        }
        self.timestamp_discontinuity_detected = threading.Event()

    def transition(self, name, message):
        state = self.workers[name]
        if state["last_logged"] != message:
            _write_channel_log(
                self.out_fp, self.channel_id, name, "INFO", message)
            state["last_logged"] = message

    def start(self, name, now, command=None):
        state = self.workers[name]
        options = {
            "stdin": subprocess.DEVNULL,
            "stdout": self.out_fp,
            "stderr": self.out_fp,
            "start_new_session": True,
        }
        if name == "ffmpeg":
            options.update(stderr=subprocess.PIPE, text=True, bufsize=1)
        proc = self.popen(
            self.commands[name] if command is None else command, **options)
        try:
            with open(os.path.join(
                    WORKER_PID_DIR, f"{self.channel_id}_{name}.pid"), "w") as pid_file:
                pid_file.write(str(proc.pid))
        except OSError:
            pass
        state.update(proc=proc, pid=proc.pid, started_at=now,
                     exit_status=None)
        self.transition(name, f"running pid={proc.pid}")
        if name == "ffmpeg" and getattr(proc, "stderr", None) is not None:
            log_thread = threading.Thread(
                target=self._forward_ffmpeg_logs,
                args=(proc.stderr, "-progress" in self.commands[name]),
                name=f"{self.channel_id}-ffmpeg-log", daemon=True)
            state["log_thread"] = log_thread
            log_thread.start()
        if name == "shaka":
            generation = _shaka_output_generation(
                self.commands[name], self.channel_id)
            if generation is not None:
                output_dir, active_generation = generation
                cleanup_retired_channel_generation_files(
                    output_dir, self.channel_id, active_generation)

    def _forward_ffmpeg_logs(self, stream, progress_enabled):
        progress = {}
        dts_warning_seen = False
        dts_warning_repeats = 0
        latest_dts_warning = None
        last_dts_summary = time.monotonic()

        def write_line(line, level="INFO"):
            _write_channel_log(
                self.out_fp, self.channel_id, "ffmpeg", level, line)

        def write_dts_summary(repeat_count, warning):
            if repeat_count <= 0:
                return
            match = _NON_MONOTONIC_DTS.search(warning or "")
            details = ""
            if match:
                details = (f"; latest previous={match.group(1)}"
                           f" current={match.group(2)}")
            write_line(
                f"suppressed {repeat_count} repeated Non-monotonic DTS "
                f"warnings{details}",
                "WARNING")

        try:
            for raw_line in stream:
                line = raw_line.rstrip("\r\n")
                key, separator, value = line.partition("=")
                if (separator and key
                        and key.replace("_", "").isalnum()):
                    progress[key] = value
                    if key == "progress":
                        if progress_enabled:
                            fields = (
                                "frame", "out_time", "drop_frames", "dup_frames",
                                "speed", "total_size",
                            )
                            summary = " ".join(
                                f"{field}={progress[field]}"
                                for field in fields if field in progress)
                            if dts_warning_seen:
                                summary += (
                                    f" non_monotonic_dts_repeats="
                                    f"{dts_warning_repeats}")
                                match = _NON_MONOTONIC_DTS.search(
                                    latest_dts_warning or "")
                                if match:
                                    summary += (
                                        f" latest_dts_previous={match.group(1)}"
                                        f" latest_dts_current={match.group(2)}")
                            write_line(summary, "DEBUG")
                            dts_warning_repeats = 0
                            last_dts_summary = time.monotonic()
                        progress.clear()
                    continue

                if "timestamp discontinuity" in line.lower():
                    self.timestamp_discontinuity_detected.set()
                    write_line(line, "WARNING")
                    continue

                if "Non-monotonic DTS" in line:
                    self.timestamp_discontinuity_detected.set()
                    latest_dts_warning = line
                    if not dts_warning_seen:
                        dts_warning_seen = True
                        write_line(line, "WARNING")
                    else:
                        dts_warning_repeats += 1
                        now = time.monotonic()
                        if (not progress_enabled
                                and now - last_dts_summary
                                >= FFMPEG_PROGRESS_INTERVAL_SEC):
                            write_dts_summary(
                                dts_warning_repeats, latest_dts_warning)
                            dts_warning_repeats = 0
                            last_dts_summary = now
                    continue

                if line:
                    lower_line = line.lower()
                    level = ("ERROR" if "error" in lower_line else
                             "WARNING" if "warning" in lower_line else "INFO")
                    write_line(line, level)
        except Exception:
            logger.exception("Channel [%s] FFmpeg log forwarding failed",
                             self.channel_id)
        finally:
            if dts_warning_repeats:
                write_dts_summary(dts_warning_repeats, latest_dts_warning)
            stream.close()

    @staticmethod
    def _join_ffmpeg_log_thread(state):
        log_thread = state.get("log_thread")
        if log_thread is not None:
            log_thread.join()
            state["log_thread"] = None

    def _shaka_discontinuity_signal_file(self):
        command = self.commands["shaka"]
        try:
            index = command.index("--hls_discontinuity_signal_file")
        except ValueError as error:
            raise RuntimeError(
                "Shaka command has no HLS discontinuity signal file") from error
        if index + 1 >= len(command):
            raise RuntimeError("Shaka discontinuity signal file path is missing")
        return command[index + 1]

    @staticmethod
    def _ffmpeg_discontinuity_command(command):
        command = list(command)
        output_format = next(
            (index for index in range(len(command) - 1, -1, -1)
             if command[index] == "-f"),
            -1)
        if (output_format < 0 or output_format + 1 >= len(command)
                or command[output_format + 1] != "mpegts"):
            raise RuntimeError("FFmpeg command has no MPEG-TS output")
        flags = next(
            (index for index in range(output_format - 1, -1, -1)
             if command[index] == "-mpegts_flags"),
            -1)
        if flags >= 0:
            if flags + 1 >= output_format:
                raise RuntimeError("FFmpeg MPEG-TS flags value is missing")
            current_flags = command[flags + 1]
            if "initial_discontinuity" not in current_flags:
                command[flags + 1] = f"{current_flags}+initial_discontinuity"
        else:
            command[output_format:output_format] = [
                "-mpegts_flags", "+initial_discontinuity"]
        return command

    def _signal_shaka_discontinuity(self):
        signal_file = self._shaka_discontinuity_signal_file()
        directory = os.path.dirname(signal_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        temporary_file = f"{signal_file}.{uuid4().hex}.tmp"
        try:
            with open(temporary_file, "w", encoding="ascii") as signal:
                signal.write(f"{uuid4().hex}\n")
            os.replace(temporary_file, signal_file)
        finally:
            try:
                os.remove(temporary_file)
            except FileNotFoundError:
                pass

    def _restart_ffmpeg_for_timestamp_discontinuity(self, now):
        state = self.workers.get("ffmpeg")
        if state is None:
            return
        self.transition(
            "ffmpeg",
            "timestamp discontinuity; signaling Shaka and restarting FFmpeg only")
        if state["proc"] is not None:
            self.stop("ffmpeg")
        self.timestamp_discontinuity_detected.clear()
        self._signal_shaka_discontinuity()
        command = self._ffmpeg_discontinuity_command(
            self.commands["ffmpeg"])
        self.start("ffmpeg", now, command)

    def failed(self, name, returncode, now):
        state = self.workers[name]
        state["proc"].wait()
        if name == "ffmpeg":
            self._join_ffmpeg_log_thread(state)
        state.update(proc=None, exit_status=returncode)
        pid_file = os.path.join(WORKER_PID_DIR, f"{self.channel_id}_{name}.pid")
        try:
            os.remove(pid_file)
        except OSError:
            pass
        if now - state["started_at"] >= WORKER_STABLE_THRESHOLD_SEC:
            state["restart_count"] = 0
        state["restart_count"] += 1
        delay = compute_backoff(state["restart_count"])
        state["next_retry_ts"] = now + delay
        self.transition(name, f"exited pid={state['pid']} status={returncode}; retry in {delay}s")

    def stop(self, name):
        state = self.workers[name]
        proc = state["proc"]
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if name == "ffmpeg":
            self._join_ffmpeg_log_thread(state)
        state["exit_status"] = proc.returncode
        state["proc"] = None
        self.transition(name, f"stopped pid={state['pid']} status={state['exit_status']}")
        pid_file = os.path.join(WORKER_PID_DIR, f"{self.channel_id}_{name}.pid")
        try:
            os.remove(pid_file)
        except OSError:
            pass

    def step(self, now=None):
        now = time.time() if now is None else now
        shaka = self.workers["shaka"]
        ffmpeg = self.workers.get("ffmpeg")
        if shaka["proc"] is not None and shaka["proc"].poll() is not None:
            self.failed("shaka", shaka["proc"].returncode, now)
            if ffmpeg is not None:
                # A producer must not run without the consumer it feeds.
                self.stop("ffmpeg")
                ffmpeg["next_retry_ts"] = 0
        if ffmpeg is not None and ffmpeg["proc"] is not None \
                and ffmpeg["proc"].poll() is not None:
            self.failed("ffmpeg", ffmpeg["proc"].returncode, now)
        if self.input_lost:
            return
        if (ffmpeg is not None and shaka["proc"] is not None
                and shaka["proc"].poll() is None
                and self.timestamp_discontinuity_detected.is_set()):
            self._restart_ffmpeg_for_timestamp_discontinuity(now)
            return
        if shaka["proc"] is None and now >= shaka["next_retry_ts"]:
            self.start("shaka", now)
        if ffmpeg is not None and shaka["proc"] is not None \
                and shaka["proc"].poll() is None and ffmpeg["proc"] is None \
                and now >= ffmpeg["next_retry_ts"]:
            self.start("ffmpeg", now)

    def close(self):
        for name in reversed(self.worker_roles):
            try:
                self.stop(name)
            except Exception:
                logger.exception("Channel [%s] failed cleaning up %s worker",
                                 self.channel_id, name)
        if ("ffmpeg" in self.worker_roles
                and "--hls_discontinuity_signal_file"
                in self.commands.get("shaka", [])):
            try:
                os.remove(self._shaka_discontinuity_signal_file())
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception(
                    "Channel [%s] failed removing Shaka discontinuity signal file",
                    self.channel_id)


class InputRecovery:
    """Keep the manager resident while input is absent and rebuild on return."""

    def __init__(self, channel_id, plugin, input_args, build_commands, out_fp,
                 discoverer=discover_topology, supervisor_factory=ChannelSupervisor,
                 topology_monitor_factory=None):
        self.channel_id = channel_id
        self.plugin = plugin
        self.input_args = input_args
        self.build_commands = build_commands
        self.out_fp = out_fp
        self.discoverer = discoverer
        self.supervisor_factory = supervisor_factory
        self.topology_monitor_factory = topology_monitor_factory
        self.supervisor = None
        self.worker_thread = None
        self.worker_stop = None
        self.topology_monitor = None
        self.worker_roles = ()
        self.topology = None
        self.state = "starting"
        self.failures = 0
        self.next_probe_ts = 0
        self.source_probe_failures = 0
        self.next_source_probe_ts = 0
        self.pending_topology = None
        self.pending_topology_since = None

    def transition(self, state, reason):
        if self.state != state:
            level = {"fault": "ERROR", "waiting": "WARNING"}.get(
                state, "INFO")
            _write_channel_log(
                self.out_fp, self.channel_id, "manager", level,
                f"{state}: {reason}")
        self.state = state

    def _source_probe_interval(self):
        if "ffmpeg" in self.worker_roles:
            return TRANSCODE_TOPOLOGY_PROBE_INTERVAL_SEC
        return SOURCE_PROBE_INTERVAL_SEC

    def _cleanup(self):
        monitor = self.topology_monitor
        self._clear_pending_topology()
        try:
            if self.worker_thread is not None:
                self._stop_worker()
            elif self.supervisor is not None:
                self.supervisor.close()
                self.supervisor = None
        finally:
            if monitor is not None:
                self.topology_monitor = None
                monitor.close()

    def _clear_pending_topology(self):
        self.pending_topology = None
        self.pending_topology_since = None

    def _cancel_pending_topology(self):
        if self.pending_topology is not None:
            _write_channel_log(
                self.out_fp, self.channel_id, "manager", "INFO",
                "topology candidate reverted; retaining current workers")
            self._clear_pending_topology()

    def _topology_candidate_is_stable(self, topology, now):
        if (self.pending_topology is None
                or not same_media_topology(topology, self.pending_topology)):
            self.pending_topology = topology
            self.pending_topology_since = now
            _write_channel_log(
                self.out_fp, self.channel_id, "manager", "INFO",
                "topology candidate pending; retaining current workers "
                f"for {TOPOLOGY_STABILITY_SEC:g}s; changes: "
                f"{describe_topology_change(self.topology, topology)}")
            return False

        self.pending_topology = topology
        return now - self.pending_topology_since >= TOPOLOGY_STABILITY_SEC

    def _run_worker(self, supervisor, stop_event):
        try:
            while not stop_event.is_set():
                supervisor.step()
                stop_event.wait(0.1)
        except Exception:
            supervisor.input_lost = True
            logger.exception("Channel [%s] worker thread failed", self.channel_id)
        finally:
            supervisor.close()

    def _start_worker(self, commands):
        self.supervisor = self.supervisor_factory(
            self.channel_id, commands, self.out_fp)
        self.worker_stop = threading.Event()
        self.worker_thread = threading.Thread(
            target=self._run_worker,
            args=(self.supervisor, self.worker_stop),
            name=f"{self.channel_id}-transcode-worker", daemon=True)
        self.worker_thread.start()

    def _stop_worker(self):
        if self.worker_stop is not None:
            self.worker_stop.set()
        if self.worker_thread is not None:
            self.worker_thread.join()
        self.worker_thread = None
        self.worker_stop = None
        self.supervisor = None

    def _wait(self, now, reason):
        self._cleanup()
        self.failures += 1
        delay = compute_backoff(self.failures)
        self.next_probe_ts = now + delay
        self.transition("waiting", f"{reason}; retry topology probe in {delay}s")

    def _fault(self, reason):
        self._cleanup()
        self.transition("fault", reason)

    def _step_with_topology_monitor(self, now):
        if self.supervisor is not None:
            if self.supervisor.input_lost:
                self._wait(now, "TSDuck/FFmpeg/Packager pipeline exited")
                return

        if self.topology_monitor is None:
            try:
                initial_topology = (
                    self.topology if self.supervisor is not None else None)
                self.topology_monitor = self.topology_monitor_factory(
                    self.plugin, self.input_args, initial_topology,
                    out_fp=self.out_fp)
            except Exception as error:
                logger.exception("Channel [%s] TSDuck startup failed", self.channel_id)
                if self.supervisor is None:
                    self._wait(now, f"TSDuck unavailable: {error}")
                else:
                    self.failures += 1
                    delay = compute_backoff(self.failures)
                    self.next_probe_ts = now + delay
                    self.transition(
                        "running",
                        f"TSDuck monitor unavailable; workers retained; retry in {delay}s")
                return
            if self.supervisor is None:
                self.transition("waiting", "waiting for initial PAT/PMT topology")
            else:
                self.transition(
                    "running", "TSDuck monitor reattached; workers retained")
            self.next_probe_ts = 0

        event = self.topology_monitor.poll()
        if event is None:
            if (self.pending_topology is None
                    or now - self.pending_topology_since < TOPOLOGY_STABILITY_SEC):
                return
            event = TopologyMonitorEvent(self.pending_topology, None)
        if event.error is not None:
            self._cancel_pending_topology()
            error = event.error
            if isinstance(error, TopologyUnavailableError) \
                    and self.supervisor is not None:
                logger.warning(
                    "Channel [%s] TSDuck monitor unavailable: %s; "
                    "retaining FFmpeg/Shaka workers",
                    self.channel_id, error)
                self.topology_monitor.close()
                self.topology_monitor = None
                self.failures += 1
                delay = compute_backoff(self.failures)
                self.next_probe_ts = now + delay
                self.transition(
                    "running",
                    f"TSDuck monitor unavailable; retry in {delay}s")
                return
            if isinstance(error, TopologyError) \
                    and not isinstance(error, TopologyUnavailableError) \
                    and "expected exactly one complete PAT and PMT" not in str(error):
                logger.error("Channel [%s] unsupported input topology: %s",
                             self.channel_id, error)
                self._fault(str(error))
            else:
                logger.warning("Channel [%s] TSDuck input unavailable: %s",
                               self.channel_id, error)
                self._wait(now, "input topology unavailable")
            return
        if event.topology is None:
            self._cancel_pending_topology()
            self._fault("TSDuck returned an empty topology event")
            return

        if self.supervisor is not None:
            if not same_media_topology(event.topology, self.topology):
                if not self._topology_candidate_is_stable(event.topology, now):
                    return
                _write_channel_log(
                    self.out_fp, self.channel_id, "manager", "INFO",
                    f"input topology stable for {TOPOLOGY_STABILITY_SEC:g}s; "
                    "replacing FFmpeg and Shaka "
                    f"workers; changes: "
                    f"{describe_topology_change(self.topology, event.topology)}")
                self._clear_pending_topology()
                self._stop_worker()
            else:
                self._cancel_pending_topology()
                if event.topology != self.topology:
                    _write_channel_log(
                        self.out_fp, self.channel_id, "manager", "INFO",
                        "PSI topology updated; media topology unchanged; "
                        "keeping FFmpeg and Shaka workers; changes: "
                        f"{describe_topology_change(self.topology, event.topology)}")
                self.topology = event.topology
                return

        try:
            commands = self.build_commands(event.topology)
        except (ValueError, TypeError) as error:
            logger.exception("Channel [%s] invalid worker configuration", self.channel_id)
            self._fault(str(error))
            return
        self.worker_roles = tuple(
            name for name in ("shaka", "ffmpeg") if name in commands)
        self.topology = event.topology
        try:
            shaka_generation = _shaka_output_generation(
                commands.get("shaka", []), self.channel_id)
            if shaka_generation is not None:
                output_dir, active_generation = shaka_generation
                cleanup_retired_channel_generation_files(
                    output_dir, self.channel_id, active_generation)
            self._start_worker(commands)
            self.failures = 0
            self.source_probe_failures = 0
            self.transition("running", "TSDuck topology accepted; workers configured")
        except Exception:
            logger.exception("Channel [%s] worker startup failed", self.channel_id)
            self._wait(now, "worker startup exception")

    def step(self, now=None):
        now = time.time() if now is None else now
        if self.state == "fault" or now < self.next_probe_ts:
            return
        if self.topology_monitor_factory is not None:
            self._step_with_topology_monitor(now)
            return
        topology = None
        topology_discovered = False
        if self.supervisor is not None and self.state == "running":
            try:
                self.supervisor.step(now)
            except Exception:
                logger.exception("Channel [%s] supervisor step failed", self.channel_id)
                self._wait(now, "worker/manager exception")
                return
            if not self.supervisor.input_lost:
                if self.topology_monitor is not None:
                    event = self.topology_monitor.poll()
                    if event is None:
                        return
                    if event.error is not None:
                        error = event.error
                        if isinstance(error, TopologyUnavailableError) \
                                or "expected exactly one complete PAT and PMT" in str(error):
                            logger.warning(
                                "Channel [%s] input topology unavailable: %s",
                                self.channel_id, error)
                            self._wait(now, "input unavailable")
                        elif isinstance(error, TopologyError):
                            logger.exception(
                                "Channel [%s] unrecoverable topology error",
                                self.channel_id,
                                exc_info=(type(error), error, error.__traceback__))
                            self._fault(str(error))
                        else:
                            logger.warning(
                                "Channel [%s] background topology probe failed: %s",
                                self.channel_id, error)
                        return
                    if event.topology is None:
                        self._fault("topology monitor returned an empty update")
                        return
                    topology = event.topology
                    topology_discovered = True
                    if same_media_topology(topology, self.topology):
                        self._cancel_pending_topology()
                        if topology != self.topology:
                            _write_channel_log(
                                self.out_fp, self.channel_id, "manager", "INFO",
                                "PSI topology updated; media topology unchanged; "
                                "keeping workers; changes: "
                                f"{describe_topology_change(self.topology, topology)}")
                        self.topology = topology
                        return
                else:
                    if now < self.next_source_probe_ts:
                        return
                    try:
                        topology = self.discoverer(self.plugin, self.input_args)
                        topology_discovered = True
                    except TopologyError as error:
                        if isinstance(error, TopologyUnavailableError) \
                                or "expected exactly one complete PAT and PMT" in str(error):
                            logger.warning("Channel [%s] input topology unavailable: %s",
                                           self.channel_id, error)
                            self._wait(now, "input unavailable")
                        else:
                            logger.exception("Channel [%s] unrecoverable topology error",
                                             self.channel_id)
                            self._fault(str(error))
                        return
                    except Exception as error:
                        self.source_probe_failures += 1
                        delay = compute_backoff(self.source_probe_failures)
                        self.next_source_probe_ts = now + delay
                        logger.warning(
                            "Channel [%s] source availability probe failed: %s; retry in %ss",
                            self.channel_id, error, delay)
                        return
                    if same_media_topology(topology, self.topology):
                        self._cancel_pending_topology()
                        if topology != self.topology:
                            _write_channel_log(
                                self.out_fp, self.channel_id, "manager", "INFO",
                                "PSI topology updated; media topology unchanged; "
                                "keeping workers; changes: "
                                f"{describe_topology_change(self.topology, topology)}")
                        self.topology = topology
                        self.source_probe_failures = 0
                        self.next_source_probe_ts = now + self._source_probe_interval()
                        return
            else:
                self._wait(now, "input EOF, timeout, or network loss")
                return

        if not topology_discovered:
            try:
                topology = self.discoverer(self.plugin, self.input_args)
            except TopologyError as error:
                if isinstance(error, TopologyUnavailableError) \
                        or "expected exactly one complete PAT and PMT" in str(error):
                    logger.warning("Channel [%s] input topology unavailable: %s",
                                   self.channel_id, error)
                    self._wait(now, "input unavailable")
                else:
                    logger.exception("Channel [%s] unrecoverable topology error",
                                     self.channel_id)
                    self._fault(str(error))
                return
            except Exception:
                logger.exception("Channel [%s] topology discovery failed", self.channel_id)
                self._wait(now, "topology discovery exception")
                return
        changed = (self.topology is not None
                   and not same_media_topology(topology, self.topology))
        if self.supervisor is not None and changed:
            if not self._topology_candidate_is_stable(topology, now):
                next_probe = now + self._source_probe_interval()
                candidate_deadline = (
                    self.pending_topology_since + TOPOLOGY_STABILITY_SEC)
                self.next_source_probe_ts = min(next_probe, candidate_deadline)
                return
        elif self.supervisor is not None:
            self._cancel_pending_topology()
        try:
            commands = self.build_commands(topology)
        except (ValueError, TypeError) as error:
            logger.exception("Channel [%s] invalid worker configuration", self.channel_id)
            self._fault(str(error))
            return
        self.worker_roles = tuple(
            name for name in ("shaka", "ffmpeg") if name in commands)
        if changed:
            _write_channel_log(
                self.out_fp, self.channel_id, "manager", "INFO",
                f"input topology stable for {TOPOLOGY_STABILITY_SEC:g}s; "
                "rebuilding workers; changes: "
                f"{describe_topology_change(self.topology, topology)}")
            self._clear_pending_topology()
            self._cleanup()
        self.topology = topology
        if self.supervisor is None:
            self.supervisor = self.supervisor_factory(
                self.channel_id, commands, self.out_fp)
        else:
            self.supervisor.commands = commands
            self.supervisor.input_lost = False
        self.failures = 0
        self.next_probe_ts = 0
        self.source_probe_failures = 0
        self.next_source_probe_ts = now + self._source_probe_interval()
        self.transition("running", "input available; workers configured")
        try:
            self.supervisor.step(now)
            if self.topology_monitor_factory is not None \
                    and self.topology_monitor is None:
                self.topology_monitor = self.topology_monitor_factory(
                    self.plugin, self.input_args, self.topology)
        except Exception:
            logger.exception("Channel [%s] worker startup failed", self.channel_id)
            self._wait(now, "worker startup exception")

    def close(self):
        self._cleanup()


def run_manager_loop(main_id):
    config = load_config()
    base_dir = config.get("baseDir", "/home/www/html/hls_stream")
    channels = config.get("multiviewChannels", [])
    channel_config = next((ch for ch in channels if ch.get("id") == main_id), None)
    if not channel_config:
        logger.error("Channel [%s] fault: not found in configuration", main_id)
        return
    try:
        resolved_config = resolve_channel_config(channel_config)
    except ValueError as error:
        logger.error("Channel [%s] configuration error: %s", main_id, error)
        raise SystemExit(1) from error
    transcode_mode = resolved_config["transcode_mode"]
    if transcode_mode == "transcode":
        try:
            check_packager_discontinuity_support()
        except RuntimeError as error:
            logger.error("Channel [%s] Shaka feature check failed: %s",
                         main_id, error)
            return
    discard_first_audio = channel_config.get("discard_first_audio", False)
    if not isinstance(discard_first_audio, bool):
        logger.error(
            "Channel [%s] configuration error: "
            "discard_first_audio must be a boolean", main_id)
        return
    channel_config["transcode_mode"] = transcode_mode
    if transcode_mode == "transcode":
        try:
            check_packager_discontinuity_support()
        except RuntimeError as error:
            logger.error("Channel [%s] Shaka feature check failed: %s",
                         main_id, error)
            return
        channel_config["video_encoder"] = resolved_config["video_encoder"]
        channel_config["audio_encoder"] = resolved_config["audio_encoder"]

    source_url = channel_config.get("source")
    if not isinstance(source_url, str) or not source_url.strip():
        logger.error("Channel [%s] fault: requires one non-empty source URL", main_id)
        return
    try:
        inputs = source_input(
            source_url, config.get("localMulticastAddr"),
            require_multicast_interface=True)
    except ValueError as error:
        logger.error("Channel [%s] configuration error: %s", main_id, error)
        return
    plugin, input_args = inputs["topology"]
    out_dir = os.path.join(base_dir, main_id)
    ensure_dir(out_dir)
    channel_index = channels.index(channel_config)
    udp_port = int(channel_config.get("packagerUdpPort", 12000 + channel_index))

    def build_commands(topology):
        audio_selection = select_audio_streams(topology, discard_first_audio)
        commands = {
            "shaka": build_shaka_command(
                channel_config, topology, udp_port, out_dir,
                inputs["source"], config.get("localMulticastAddr"),
                uuid4().hex[:12], audio_selection=audio_selection,
                discontinuity_signal_file=(
                    os.path.join(
                        WORKER_PID_DIR,
                        f"{main_id}_hls_discontinuity.signal")
                    if transcode_mode == "transcode" else None)),
        }
        if transcode_mode == "transcode":
            commands["ffmpeg"] = build_ffmpeg_command(
                inputs["ffmpeg"], audio_selection, udp_port, channel_config)
        return commands

    stop_requested = [False]
    def request_stop(signum, frame):
        stop_requested[0] = True

    old_term = signal.signal(signal.SIGTERM, request_stop)
    old_int = signal.signal(signal.SIGINT, request_stop)
    task_log = os.path.join(LOG_DIR, f"{main_id}.log")
    recovery = None
    try:
        with open(task_log, "a") as out:
            topology_monitor_factory = (
                TopologyMonitor if transcode_mode == "transcode" else None)
            recovery = InputRecovery(
                main_id, plugin, input_args, build_commands, out,
                topology_monitor_factory=topology_monitor_factory)
            while not stop_requested[0]:
                try:
                    recovery.step()
                except Exception:
                    logger.exception("Channel [%s] manager loop exception", main_id)
                    recovery._wait(time.time(), "manager loop exception")
                time.sleep(1)
    finally:
        if recovery is not None:
            recovery.close()
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


# ------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------

def start_tasks():
    logger.info("Starting main channel avpipeline tasks...")
    config = load_config()
    channels = config.get("multiviewChannels", [])
    for channel in channels:
        try:
            resolve_channel_config(channel)
        except ValueError as error:
            main_id = channel.get("id") if isinstance(channel, dict) else "<unknown>"
            logger.error("Channel [%s] configuration error: %s", main_id, error)
            raise SystemExit(1) from error

    for channel in channels:
        main_id = channel.get("id")
        if not channel.get("enabled", True):
            logger.info("Skipping disabled channel [%s]", main_id)
            continue
        pid = get_pid(main_id)
        if pid and is_running(pid):
            # Verify it's actually our manager, not a recycled PID
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "--manager" in cmdline and main_id in cmdline:
                    logger.info(f"Main task [{main_id}] already running (PID {pid}), skipping.")
                    continue
                else:
                    logger.warning(f"PID {pid} for [{main_id}] is not our manager, removing stale PID file.")
                    try:
                        os.remove(os.path.join(PID_DIR, f"{main_id}.pid"))
                    except OSError:
                        pass
            except (OSError, IOError):
                # Can't read /proc — assume stale
                logger.warning(f"Cannot verify PID {pid} for [{main_id}], removing stale PID file.")
                try:
                    os.remove(os.path.join(PID_DIR, f"{main_id}.pid"))
                except OSError:
                    pass

        logger.info(f"Starting main task manager [{main_id}]")
        manager_proc = subprocess.Popen(
            [sys.executable, os.path.abspath(sys.argv[0]),
             "--config", os.path.abspath(JSON_FILE), "--manager", main_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        save_pid(main_id, manager_proc.pid)

    generate_url_list()
    logger.info("Start process completed.")


def cleanup_channel_output_files(base_dir, channel_id):
    if not isinstance(base_dir, str) or not base_dir:
        raise ValueError("baseDir must be a non-empty path")
    if not isinstance(channel_id, str) or not channel_id \
            or Path(channel_id).name != channel_id or channel_id in (".", ".."):
        raise ValueError("channel id must be a single non-empty path component")

    channel_dir = Path(base_dir).resolve() / channel_id
    if channel_dir.is_symlink():
        raise ValueError(f"refusing to clean symlinked channel directory {channel_dir}")
    if not channel_dir.exists():
        return 0
    if not channel_dir.is_dir():
        raise NotADirectoryError(f"channel output path is not a directory: {channel_dir}")

    owned_output = re.compile(
        rf"{re.escape(channel_id)}_(?:video|audio-\d+)"
        r"(?:_g[A-Za-z0-9_-]+)?(?:_init\.mp4|_\d+\.m4s|\.m3u8)$")
    manifests = {
        f"{channel_id}.m3u8",
        f"{channel_id}_master.m3u8",
        f"{channel_id}.mpd",
    }
    removed = 0
    for output in channel_dir.iterdir():
        if output.name not in manifests and not owned_output.fullmatch(output.name):
            continue
        if not output.is_file() and not output.is_symlink():
            continue
        output.unlink()
        removed += 1
    return removed


def _shaka_output_generation(command, channel_id):
    init_segment_field = re.compile(r"(?:^|,)init_segment=([^,]+)")
    generated_init = re.compile(
        rf"{re.escape(channel_id)}_(?:video|audio-\d+)_g"
        r"([A-Za-z0-9_-]+)_init\.mp4$")
    for argument in command:
        for init_segment in init_segment_field.findall(argument):
            output_path = Path(init_segment)
            match = generated_init.fullmatch(output_path.name)
            if match:
                return str(output_path.parent), match.group(1)
    return None


def cleanup_retired_channel_generation_files(output_dir, channel_id,
                                             active_generation):
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output directory must be a non-empty path")
    if not isinstance(channel_id, str) or not channel_id \
            or Path(channel_id).name != channel_id or channel_id in (".", ".."):
        raise ValueError("channel id must be a single non-empty path component")
    if not isinstance(active_generation, str) \
            or re.fullmatch(r"[A-Za-z0-9_-]+", active_generation) is None:
        raise ValueError("active generation id contains invalid characters")

    channel_dir = Path(output_dir)
    if channel_dir.is_symlink():
        raise ValueError(f"refusing to clean symlinked channel directory {channel_dir}")
    if not channel_dir.exists():
        return 0
    if not channel_dir.is_dir():
        raise NotADirectoryError(f"channel output path is not a directory: {channel_dir}")

    retired_output = re.compile(
        rf"{re.escape(channel_id)}_(?:video|audio-\d+)_g"
        r"([A-Za-z0-9_-]+)(?:_init\.mp4|_\d+\.m4s)$")
    removed = 0
    for output in channel_dir.iterdir():
        match = retired_output.fullmatch(output.name)
        if match is None or match.group(1) == active_generation:
            continue
        if not output.is_file() and not output.is_symlink():
            continue
        output.unlink()
        removed += 1
    return removed


def stop_tasks():
    logger.info("Stopping main channel avpipeline tasks...")

    # Signal managers only; each manager stops FFmpeg before Packager and reaps both.
    manager_pids = {}  # task_name -> (pid, pgid)
    for pid_file in Path(PID_DIR).glob("*.pid"):
        task_name = pid_file.stem
        try:
            with open(pid_file, "r") as f:
                pid = int(f.read().strip())
            pgid = pid
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pass
            manager_pids[task_name] = (pid, pgid)
        except Exception as e:
            logger.warning(f"Error reading PID for [{task_name}]: {e}")
        finally:
            try:
                os.remove(pid_file)
            except Exception:
                pass

    # SIGTERM all manager process groups.
    for task_name, (pid, pgid) in manager_pids.items():
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    # Allow the manager's two sequential bounded worker shutdowns to complete.
    deadline = time.time() + 12
    while time.time() < deadline:
        all_dead = True
        for task_name, (pid, pgid) in manager_pids.items():
            try:
                os.killpg(pgid, 0)  # check if ANY process in group is alive
                all_dead = False
            except OSError:
                pass
        if all_dead:
            break
        time.sleep(0.2)

    # SIGKILL entire process groups — manager + all workers
    for task_name, (pid, pgid) in manager_pids.items():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        logger.info(f"Stopped [{task_name}] (pid={pid}, pgid={pgid})")

    time.sleep(1)

    # Phase 2 (safety net): Kill any orphaned workers whose PID files remain.
    # This handles edge cases where the manager died before workers were added
    # to its process group, or PID files from a previous unclean shutdown.
    for wpid_file in Path(WORKER_PID_DIR).glob("*.pid"):
        worker_name = wpid_file.stem
        try:
            with open(wpid_file, "r") as f:
                wpid = int(f.read().strip())
            try:
                os.kill(wpid, signal.SIGKILL)
                logger.info(f"Killed orphaned worker [{worker_name}] (pid={wpid})")
            except OSError:
                pass
        except Exception:
            pass
        finally:
            try:
                os.remove(wpid_file)
            except Exception:
                pass

    time.sleep(1)

    # Phase 3 (aggressive cleanup): Find any remaining manager/worker processes by name.
    # This handles the case where PID files were lost or stale.
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"{os.path.basename(sys.argv[0])}.*--manager"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().splitlines():
            try:
                rpid = int(line.strip())
                if rpid != os.getpid():
                    os.kill(rpid, signal.SIGKILL)
                    logger.info(f"Killed remaining manager process (pid={rpid})")
            except (ValueError, OSError):
                pass
    except Exception:
        pass

    # Phase 4: Kill orphaned ffmpeg workers that were children of our managers.
    # Match ffmpeg processes reading from UDP multicast (our signature pattern).
    killed_ffmpeg = 0
    try:
        result = subprocess.run(
            ["pgrep", "-a", "-f", "ffmpeg.*udp://@"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().splitlines():
            try:
                rpid = int(line.strip().split()[0])
                os.kill(rpid, signal.SIGKILL)
                killed_ffmpeg += 1
            except (ValueError, OSError, IndexError):
                pass
    except Exception:
        pass
    if killed_ffmpeg:
        logger.info(f"Killed {killed_ffmpeg} orphaned ffmpeg worker(s)")

    time.sleep(1)

    config = load_config()
    base_dir = config.get("baseDir", "/home/www/html/hls_stream")
    cleanup_failures = []
    for channel in config.get("multiviewChannels", []):
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        try:
            removed = cleanup_channel_output_files(base_dir, channel_id)
        except (OSError, ValueError) as error:
            logger.error("Could not clean output for channel [%s]: %s",
                         channel_id, error)
            cleanup_failures.append(channel_id)
            continue
        if removed:
            logger.info("Removed %d generated media/manifest file(s) for channel [%s]",
                        removed, channel_id)

    if cleanup_failures:
        raise OSError(
            "Could not clean generated output for channel(s): "
            + ", ".join(str(channel_id) for channel_id in cleanup_failures))
    logger.info("All main channel services stopped and generated outputs cleaned.")


def generate_url_list():
    config = load_config()
    base_dir = config.get("baseDir", "/home/www/html/hls_stream")
    url_file = os.path.join(base_dir, "main_urls.json")

    channels_info = []
    for channel in config.get("multiviewChannels", []):
        main_id = channel.get("id")
        hls_playlist = f"{main_id}/{main_id}.m3u8"
        dash_playlist = f"{main_id}/{main_id}.mpd"
        channels_info.append({
            "id": main_id,
            "format": "cmaf",
            "playlist": hls_playlist,
            "master_playlist": hls_playlist,
            "dash_playlist": dash_playlist,
        })

    output = {"channels": channels_info}
    os.makedirs(base_dir, exist_ok=True)
    with open(url_file, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    logger.info(f"Main channel URL list generated: {url_file}")
    print(json.dumps(output, indent=2, ensure_ascii=False))


def status_tasks():
    print("========== Main Channel Status ==========")
    config = load_config()
    for channel in config.get("multiviewChannels", []):
        main_id = channel.get("id")
        pid = get_pid(main_id)
        if pid and is_running(pid):
            print(f"[RUNNING]\tTask: {main_id}\t(PID: {pid})")
        else:
            print(f"[STOPPED]\tTask: {main_id}")
    print("==========================================")


def parse_cli_arguments(arguments):
    config_path = JSON_FILE
    config_seen = False
    command_arguments = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--config":
            if config_seen or index + 1 >= len(arguments):
                raise ValueError("--config requires exactly one path")
            config_path = arguments[index + 1]
            if not config_path:
                raise ValueError("--config requires a non-empty path")
            config_seen = True
            index += 2
            continue
        if argument.startswith("--config="):
            if config_seen:
                raise ValueError("--config may be specified only once")
            config_path = argument.split("=", 1)[1]
            if not config_path:
                raise ValueError("--config requires a non-empty path")
            config_seen = True
            index += 1
            continue
        command_arguments.append(argument)
        index += 1
    return command_arguments, os.path.abspath(config_path)


def main():
    global JSON_FILE

    usage = (f"Usage: {sys.argv[0]} [--config PATH] "
             "{start|stop|restart|status|urls}\n"
             f"       {sys.argv[0]} [--config PATH] --manager CHANNEL")
    try:
        arguments, JSON_FILE = parse_cli_arguments(sys.argv[1:])
    except ValueError as error:
        print(f"{error}\n{usage}", file=sys.stderr)
        sys.exit(2)

    if arguments in (["--help"], ["-h"]):
        print(usage)
        sys.exit(0)

    if arguments and arguments[0] == "--manager":
        if len(arguments) != 2:
            print(usage, file=sys.stderr)
            sys.exit(2)
        check_dependencies()
        run_manager_loop(arguments[1])
        sys.exit(0)

    if len(arguments) != 1 or arguments[0] not in [
            "start", "stop", "restart", "status", "urls"]:
        print(usage, file=sys.stderr)
        sys.exit(1)

    cmd = arguments[0]
    if cmd in ["start", "restart"]:
        check_dependencies()

    if cmd == "start":
        start_tasks()
    elif cmd == "stop":
        stop_tasks()
    elif cmd == "restart":
        stop_tasks()
        time.sleep(2)
        start_tasks()
    elif cmd == "status":
        status_tasks()
    elif cmd == "urls":
        generate_url_list()


if __name__ == "__main__":
    main()
