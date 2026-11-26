"""Build the single-process FFmpeg pipeline for one channel."""


_ENCODER_MAPPINGS = {
    "video": {
        "avc": "libx264",
        "h264": "libx264",
        "h.264": "libx264",
        "libx264": "libx264",
        "libx265": "libx265",
    },
    "audio": {
        "aac": "aac",
        "libfdk_aac": "libfdk_aac",
        "opus": "opus",
    },
}

FFMPEG_PROGRESS_INTERVAL_SEC = 10


def _positive_integer(config, name, default):
    value = config.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _encoder_name(value, media_type):
    if not isinstance(value, str):
        raise ValueError(f"{media_type}_encoder must be a non-empty encoder name")
    normalized = value.strip().lower()
    try:
        return _ENCODER_MAPPINGS[media_type][normalized]
    except KeyError:
        raise ValueError(
            f"{media_type}_encoder must be a supported encoder name") from None


def build_ffmpeg_command(source_url, selection, udp_port, channel_config=None):
    """Return FFmpeg argv for the selected program streams and shared input."""
    if not isinstance(source_url, str) or not source_url.strip():
        raise ValueError("source URL must be a non-empty string")
    if isinstance(udp_port, bool) or not isinstance(udp_port, int) or not 1 <= udp_port <= 65535:
        raise ValueError("UDP port must be an integer from 1 through 65535")
    if channel_config is None:
        channel_config = {}
    if not isinstance(channel_config, dict):
        raise ValueError("channel configuration must be a mapping")

    video_encoder = _encoder_name(
        channel_config.get("video_encoder", "avc"), "video")
    audio_encoder = _encoder_name(
        channel_config.get("audio_encoder", "aac"), "audio")
    video_average_bitrate = _positive_integer(
        channel_config, "video_average_bitrate", 8500000)
    video_gop_size = _positive_integer(channel_config, "video_gop_size", 60)
    video_keyint_min = min(
        _positive_integer(channel_config, "video_keyint_min", video_gop_size),
        video_gop_size)
    debug_progress = channel_config.get("debug_ffmpeg_timestamps", False)
    if not isinstance(debug_progress, bool):
        raise ValueError("debug_ffmpeg_timestamps must be a boolean")

    pids = [selection.topology.video.pid] + [
        stream.pid for stream in selection.audio]
    if any(not isinstance(pid, int) or not 0 <= pid <= 0x1FFF for pid in pids):
        raise ValueError("topology contains an invalid elementary-stream PID")
    if len(set(pids)) != len(pids):
        raise ValueError("topology contains duplicate elementary-stream PIDs")

    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel",
        "warning",
        # Normalize MPEG-TS timestamp jumps before Shaka segments the output.
        "-dts_delta_threshold", "1",
        # Continue from damaged startup frames and expose frames before the first keyframe.
        "-err_detect:v", "ignore_err",
        "-flags:v", "+output_corrupt",
        "-flags2:v", "+showall",
    ]
    if debug_progress:
        command.extend((
            "-nostats",
            "-stats_period", str(FFMPEG_PROGRESS_INTERVAL_SEC),
            "-progress", "pipe:2",
        ))
    command.extend(("-i", source_url))
    # PID selectors avoid mapping unrelated programs, unrecognized transport
    # streams, or audio excluded by the shared selection.
    for pid in pids:
        command.extend(("-map", f"0:i:{pid}"))

    command.extend((
        "-c:v", video_encoder, "-c:a", audio_encoder,
        "-af", "aresample=async=1",
    ))
    if video_encoder != "none":
        video_peak_bitrate = max(
            2000000, (video_average_bitrate * 120 + 42) // 85)
        command.extend((
            "-preset", "veryfast", "-sc_threshold", "0",
            "-b:v", str(video_average_bitrate),
            "-maxrate:v", str(video_peak_bitrate),
            "-bufsize:v", str(video_peak_bitrate * 2),
            "-g:v", str(video_gop_size),
            "-keyint_min:v", str(video_keyint_min),

            # Predictable low-delay encoded output for Shaka.
            "-bf:v", "0",
        ))
    command.extend((
        # Preserve decoded frames across timestamp discontinuities.
        "-fps_mode:v", "passthrough",
        "-f", "mpegts",
        # "/tmp/encoded.ts",
        f"udp://127.0.0.1:{udp_port}",
    ))
    return command