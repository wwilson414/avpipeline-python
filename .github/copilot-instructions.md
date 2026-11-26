# Project-Specific Copilot Instructions

## Project Scope

This repository contains a lightweight, Linux-oriented manager for live MPEG-TS channels. It
discovers each input's stream topology with TSDuck and produces live HLS/DASH output with Shaka
Packager. It is not a general-purpose media server or workflow engine.

Preserve the supported channel model: one MPEG-TS source, one program, exactly one video stream,
and zero or more supported audio streams. Do not introduce multi-program selection, transcoding
ladders, or unrelated service/UI features unless requested.

## Code Layout

- `src/avpipeline.py`: CLI, channel configuration loading, process supervision, and recovery.
- `src/channel_config.py`: validation and normalization of channel options.
- `src/ts_topology.py`: TSDuck-based topology discovery and monitoring.
- `src/ffmpeg_command.py`: FFmpeg command construction for transcoding.
- `src/shaka_command.py`: Shaka Packager command construction.
- `pytest/`: unit tests, written with `unittest` and runnable with Python's test runner.
- `mvc.json`: example runtime configuration.
- `scripts/`: Shaka Packager patch/build helpers.
- `patches/`: maintained patch for Shaka Packager v3.9.3.

Treat `build/` as generated build output or a third-party checkout; do not make project changes
there. If a change to the Shaka patch is required, update the maintained patch under `patches/`
and keep it compatible with the provided helper scripts.

## Behavior and Compatibility Constraints

- Preserve the distinction between `transcode` and `passthrough`: transcode sends FFmpeg output to
  Shaka Packager over local UDP; passthrough sends the original source to Shaka without launching
  FFmpeg.
- Keep invalid enabled-channel configuration fail-fast. In particular, do not silently treat an
  unsupported or explicitly empty encoder value as stream copy or as a default.
- Preserve source audio ordinals when filtering audio streams. Respect labels and language
  metadata parsed from supported MPEG-TS descriptors.
- Maintain bounded worker supervision, orderly shutdown/reaping, and the existing PID/log
  conventions. Avoid broad process-management or output-cleanup changes without focused tests.
- Keep Shaka Packager v3.9.3 compatibility and the HLS discontinuity-signal patch behavior.
- Runtime tools (including TSDuck, FFmpeg/ffprobe, and patched Shaka Packager) are external system
  dependencies; do not add them as Python package dependencies.

## Development and Verification

The project supports Python 3.9 and newer. Run the unit suite from the repository root with:

```bash
PYTHONPATH=src python3 -m unittest discover -s pytest -v
```

For a focused test, select its file, for example:

```bash
PYTHONPATH=src python3 -m unittest discover -s pytest -p 'test_channel_config.py' -v
```

For Python syntax checks, use `python3 -m py_compile` on the changed modules. For changes to shell
scripts, run `bash -n <script>` and `shellcheck <script>` when ShellCheck is available. The release
workflow builds distributions with `python -m build`; use that check when changing packaging
metadata or build behavior.

Unit tests should not require a live media source or launch system services. Running the manager's
`start`, `stop`, or `restart` commands can interact with `/run/avpipeline`, `/var/log/avpipeline`,
and configured output directories; do not run them as validation without explicit authorization
and a safe test configuration.

## Workflow Records

For ordinary repository work, do not load or edit `.github/ai/` workflow state. Follow those
records only when handling a formal multi-agent workflow task or when the user explicitly
references `TASK-*` or `.github/ai/`. When applicable, follow the repository's role ownership and
single-writer rules.
