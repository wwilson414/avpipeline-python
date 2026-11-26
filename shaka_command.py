"""Build a single Shaka Packager command for one channel."""

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from channel_config import resolve_channel_config
from ts_topology import select_audio_streams


def _positive_integer(channel_config, name, default):
    value = channel_config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _direct_source(source, interface_address):
    parsed = urlsplit(source)
    multicast = False
    if parsed.scheme == "udp":
        try:
            multicast = ipaddress.ip_address(parsed.hostname).is_multicast
        except (ValueError, TypeError):
            pass

    netloc = (parsed.netloc[1:] if parsed.scheme == "udp"
              and parsed.netloc.startswith("@") else parsed.netloc)
    query = [(key, value) for key, value in parse_qsl(
        parsed.query, keep_blank_values=True)
        if key != "localaddr" and not (parsed.scheme == "udp" and key == "reuse")]
    if parsed.scheme == "udp":
        query.append(("reuse", "1"))
    normalized = urlunsplit((
        parsed.scheme, netloc, parsed.path, urlencode(query), parsed.fragment))
    if not multicast:
        return normalized, None
    try:
        interface_address = str(ipaddress.IPv4Address(interface_address))
    except (ipaddress.AddressValueError, TypeError):
        raise ValueError(
            "a valid IPv4 interface address is required for direct multicast input")
    return normalized, interface_address


def build_shaka_command(channel_config, topology, udp_port, output_dir,
                        source_url=None, interface_address=None,
                        generation_id=None, audio_selection=None,
                        discontinuity_signal_file=None):
    """Return Packager argv for the video and retained audio streams."""
    if not isinstance(channel_config, dict):
        raise ValueError("channel configuration must be a mapping")
    channel_id = channel_config.get("id")
    if not isinstance(channel_id, str) or not channel_id.strip():
        raise ValueError("channel id must be a non-empty string")
    if isinstance(udp_port, bool) or not isinstance(udp_port, int) or not 1 <= udp_port <= 65535:
        raise ValueError("UDP port must be an integer from 1 through 65535")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output directory must be a non-empty string")
    if generation_id is not None and (
            not isinstance(generation_id, str) or not generation_id
            or not all(character.isascii()
                       and (character.isalnum() or character in "_-")
                       for character in generation_id)):
        raise ValueError("generation id must contain only ASCII letters, digits, '_' or '-'")
    if discontinuity_signal_file is not None and (
            not isinstance(discontinuity_signal_file, str)
            or not discontinuity_signal_file):
        raise ValueError("discontinuity signal file must be a non-empty path")
    audio_selection = audio_selection or select_audio_streams(topology)
    topology = audio_selection.topology
    resolved_config = resolve_channel_config(channel_config)
    mode = resolved_config.get("transcode_mode")
    if mode not in ("transcode", "passthrough"):
        raise ValueError("transcode_mode must be 'transcode' or 'passthrough'")
    direct = mode == "passthrough"

    segment_duration = _positive_integer(channel_config, "segment_duration", 4)
    live_window_size = _positive_integer(channel_config, "live_window_size", 15)
    preserved_segments = _positive_integer(
        channel_config, "preserved_segments_outside_live_window", 10)

    if direct:
        source_url = source_url or channel_config.get("source")
        if not isinstance(source_url, str) or not source_url:
            raise ValueError("a direct source URL is required for passthrough")
        source, multicast_interface = _direct_source(source_url, interface_address)
        if (topology.video.media_type != "video"
                or topology.streams.count(topology.video) != 1):
            raise ValueError("topology must contain exactly one video stream")
        if (len({stream.pid for stream in topology.streams}) != len(topology.streams)
                or len({stream.pid for stream in topology.audio}) != len(topology.audio)
                or set(stream.pid for stream in topology.audio)
                != {stream.pid for stream in topology.streams
                    if stream.media_type == "audio"}
                or any(stream.media_type not in ("video", "audio")
                       for stream in topology.streams)):
            raise ValueError("topology cannot unambiguously select video and audio streams")
        if sum(stream.media_type == "video" for stream in topology.streams) != 1:
            raise ValueError("topology must contain exactly one video stream")
        stream_indexes = {
            stream.pid: ordinal for ordinal, stream in enumerate(topology.streams)}
        if multicast_interface:
            interface_args = ["--udp_interface_address", multicast_interface]
        else:
            interface_args = []
    else:
        source = f"udp://127.0.0.1:{udp_port}"
        stream_indexes = None
        interface_args = []

    command = ["packager"]

    video_prefix = f"{output_dir}/{channel_id}_video"
    generation_suffix = f"_g{generation_id}" if generation_id is not None else ""
    video_index = stream_indexes.get(topology.video.pid, 0) if direct else 0
    video = [
        f"in={source}", f"stream={video_index}",
        f"init_segment={video_prefix}{generation_suffix}_init.mp4",
        f"segment_template={video_prefix}{generation_suffix}_$Time$.m4s",
        f"playlist_name={channel_id}_video.m3u8",
    ]
    command.append(",".join(video))

    for output_ordinal, stream in enumerate(audio_selection.audio):
        source_ordinal = stream.audio_ordinal or output_ordinal + 1
        track_name = f"audio-{source_ordinal}"
        prefix = f"{output_dir}/{channel_id}_{track_name}"
        prefix += generation_suffix
        label = stream.audio_label or track_name
        language = stream.language or "en"
        stream_index = (stream_indexes[stream.pid]
                        if direct else output_ordinal + 1)
        descriptor = [
            f"in={source}", f"stream={stream_index}",
            f"init_segment={prefix}_init.mp4",
            f"segment_template={prefix}_$Time$.m4s",
            f"hls_name={label}", f"dash_label={label}", f"language={language}",
            f"playlist_name={channel_id}_{track_name}.m3u8",
        ]
        command.append(",".join(descriptor))

    command.extend(interface_args)
    command.extend((
        "--segment_duration", str(segment_duration),
        "--minimum_update_period", str(segment_duration),
        "--time_shift_buffer_depth", str(segment_duration * live_window_size),
        "--preserved_segments_outside_live_window", str(preserved_segments),
        "--add_program_date_time",
        "--hls_playlist_type", "LIVE",
        "--hls_master_playlist_output", f"{output_dir}/{channel_id}.m3u8",
        "--mpd_output", f"{output_dir}/{channel_id}.mpd",
    ))
    if discontinuity_signal_file is not None:
        command.extend((
            "--hls_discontinuity_signal_file", discontinuity_signal_file,
        ))

    return command
