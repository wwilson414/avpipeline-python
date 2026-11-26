"""Pure validation and resolution for Python channel processing configuration."""

_ENCODER_MAPPINGS = {
    "video_encoder": {
        "avc": "libx264",
        "h264": "libx264",
        "h.264": "libx264",
        "libx264": "libx264",
        "libx265": "libx265",
    },
    "audio_encoder": {
        "aac": "aac",
        "libfdk_aac": "libfdk_aac",
        "opus": "opus",
    },
}


def resolve_channel_config(channel_config):
    """Return the enabled channel's mode and effective transcode encoders.

    Disabled channels return only their disabled status and are exempt from
    mode and encoder validation. Passthrough results deliberately omit encoder
    fields.
    """
    if not isinstance(channel_config, dict):
        raise ValueError("channel configuration must be a mapping")

    if not channel_config.get("enabled", True):
        return {"enabled": False}

    mode = channel_config.get("transcode_mode")
    if not isinstance(mode, str) or mode not in ("transcode", "passthrough"):
        raise ValueError(
            "transcode_mode must be 'transcode' or 'passthrough'")

    resolved = {"enabled": True, "transcode_mode": mode}
    if mode == "passthrough":
        return resolved

    for key, default in (("video_encoder", "avc"), ("audio_encoder", "aac")):
        if key not in channel_config:
            resolved[key] = default
            continue

        value = channel_config[key]
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a non-empty encoder name")
        normalized = value.strip().lower()
        if normalized not in _ENCODER_MAPPINGS[key]:
            raise ValueError(f"{key} must be a supported encoder name")
        resolved[key] = _ENCODER_MAPPINGS[key][normalized]

    return resolved
