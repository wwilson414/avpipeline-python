# Simple AV Pipeline

This project is a lightweight, channel-oriented AV pipeline managed by
[`avpipeline.py`](./avpipeline.py). It is intended for straightforward live MPEG-TS ingest and
HLS/DASH packaging, not as a general-purpose media server. This guide covers the Python manager and
its helper modules.

The manager reads a single-program MPEG-TS source per enabled channel, discovers its topology with
TSDuck, then runs one of two paths:

- **Transcode**: FFmpeg independently reads the source and writes one interleaved MPEG-TS stream
  over local UDP; Shaka Packager creates HLS and DASH output.
- **Passthrough**: Shaka Packager reads the source directly; no FFmpeg worker is launched.

## Supported Features and Scope

- **Inputs:** One UDP multicast/unicast MPEG-TS source or local MPEG-TS file per channel.
- **Stream layout:** One program with exactly one video stream and zero or more supported audio
  streams. TSDuck reads the transport-stream tables to identify the streams.
- **Transcoding:** Optional FFmpeg video encoding with H.264/AVC (`libx264`) or H.265/HEVC
  (`libx265`), and audio encoding with AAC, `libfdk_aac`, or Opus.
- **Packaging:** Live HLS and DASH using CMAF media segments. Passthrough packages the selected
  source streams without launching FFmpeg; transcode mode encodes them first.
- **Audio selection and labels:** Optionally discard the first audio stream. Audio labels and
  language metadata are taken from supported MPEG-TS descriptors, with documented defaults.
- **Channel operations:** Start, stop, restart, and check channel managers; generate a JSON index
  of the HLS/DASH manifest paths. Managers supervise their media workers and log their activity.

The pipeline handles one configured source and one video stream per channel. It does not provide
multi-program selection, multi-bitrate encoding ladders, a user interface, or a general media
workflow engine.

## Runtime Dependencies

| Requirement | Details |
| --- | --- |
| Operating system | Linux. Runtime paths use `/run` and `/var/log`, and process supervision uses Linux/POSIX facilities. |
| Python | Python 3. The project README documents Python 3.6 or newer. |
| TSDuck | System TSDuck Python binding (`tsduck.py`) and `libtsduck`; use the binding and native library from the same package/version as `tsp`. There is no PyPI `tsduck` package. |
| FFmpeg | `ffmpeg` and `ffprobe` CLI programs available in `PATH`; the project README documents FFmpeg 4.0 or newer. |
| Shaka Packager | A `packager` CLI, version 3.9.3, built with the HLS discontinuity-signal patch included at [`patches/shaka-v3.9.3-hls-discontinuity.patch`](./patches/shaka-v3.9.3-hls-discontinuity.patch). The manager checks the version and `--hls_discontinuity_signal_file` support before transcode workers start; put the executable on `PATH`. |
| Network | Access to the configured UDP multicast/unicast input and an IPv4 interface address for multicast. |
| Writable paths | The service account needs access to `/run/avpipeline`, `/var/log/avpipeline`, and `baseDir`. |

Install the system packages using the package manager for your distribution. For example:

```bash
# ffmpeg-8.1.2, tsduck-3.45 and above.
# RHEL / Oracle Linux / Rocky Linux
sudo dnf install -y ffmpeg tsduck

# Debian / Ubuntu
sudo apt install -y ffmpeg tsduck
```

TSDuck's Python module directory is distribution-specific. If it is not already on Python's import
path, add the directory containing the module. A common RPM location is:

```bash
export PYTHONPATH="/usr/share/tsduck/python${PYTHONPATH:+:$PYTHONPATH}"
python3 -c 'import tsduck; print(tsduck.version())'
```

The project can clone Shaka Packager from the maintainer's GitHub repository, apply this project's
patch, and build the standalone executable:

```bash
scripts/build-shaka-packager.sh --parallel "$(nproc)"
```

By default the script clones `v3.9.3` from
`https://github.com/shaka-packager/shaka-packager`, stores the source and build under `build/`, and
prints the resulting executable path. It also accepts `v3.9.3` with `--version v3.9.3` when the
selected repository provides that tag. Use `--help` to see options for the repository, version,
source directory, build directory, and parallel job count. It applies the patch idempotently and
does not install the executable.

To patch an existing checkout without building:

```bash
scripts/apply-shaka-packager-patch.sh /path/to/shaka-packager
```

Put the resulting `packager` executable on `PATH`. Check the runtime tools before starting:

```bash
command -v ffmpeg ffprobe packager
packager --version
```

The Python manager also imports TSDuck through its system binding. Do not mix `tsduck.py` and
`libtsduck` from different TSDuck installations.

## Configuration

The default configuration file is `mvc.json` in the current working directory. Select another
file with `--config PATH`; the option may appear before or after the command.

Top-level settings:

| Key | Meaning |
| --- | --- |
| `baseDir` | Output root. Defaults to `/home/www/html/hls_stream`. |
| `localMulticastAddr` | IPv4 address assigned to the host's multicast-receiving interface; required for multicast input. |
| `multiviewChannels` | Array of channel objects. |

Channel settings:

| Key | Meaning |
| --- | --- |
| `id` | Channel identifier; used for the manager name and output directory. |
| `enabled` | Whether `start` launches the channel. Defaults to `true`. |
| `source` | One MPEG-TS input URL, such as `udp://@239.10.0.1:5000`, or a local file path/`file://` URL. The supported elementary-stream topology is one program with one video and zero or more audio streams. |
| `transcode_mode` | Required for every enabled channel: `transcode` or `passthrough`. Missing or invalid values stop startup before channel workers launch. |
| `discard_first_audio` | Optional boolean, default `false`. When `true`, excludes only the first audio stream in source order in either mode. Remaining audio labels retain their original one-based source ordinals, not renumbered after filtering; valid private descriptor `0xFD` labels override the defaults. |
| `video_encoder`, `audio_encoder` | In transcode mode, omitted keys default to `avc` (`libx264`) and `aac`. Video accepts `avc`, `h264`, `h.264`, `libx264`, or `libx265`; audio accepts `aac`, `libfdk_aac`, or `opus`. Names are matched case-insensitively after trimming whitespace and map to their corresponding FFmpeg encoders. Explicit `null`, empty/whitespace, `none`, `copy`, or any other name is invalid and fails configuration before any manager or worker starts. Passthrough ignores both fields. |
| `segment_duration`, `live_window_size` | Positive integers controlling Shaka segment duration and live-window depth. Defaults are 4 seconds and 15 segments. |
| `preserved_segments_outside_live_window` | Positive segment-retention count; defaults to `live_window_size`. |
| `video_average_bitrate`, `video_gop_size`, `video_keyint_min` | Video rate-control and keyframe settings when video encoding is enabled. Defaults are 8,500,000, 60, and the GOP size. |
| `debug_ffmpeg_timestamps` | Optional boolean, default `false`. In transcode mode, emits one FFmpeg progress summary every 10 seconds, including encoded video frame count and output time. Repeated non-monotonic DTS warnings are summarized while the first warning and FFmpeg errors remain visible. |
| `packagerUdpPort` | Optional local UDP port for the transcode FFmpeg-to-Shaka connection. Defaults to `12000` plus the channel's array index. |

Example `mvc.json`:

```json
{
  "baseDir": "/home/www/html/streams",
  "localMulticastAddr": "192.168.100.47",
  "multiviewChannels": [
    {
      "id": "mv-101",
      "enabled": true,
      "transcode_mode": "transcode",
      "discard_first_audio": false,
      "video_encoder": "H.264",
      "audio_encoder": "AAC",
      "source": "udp://@239.10.0.1:5000",
      "segment_duration": 4,
      "live_window_size": 15,
      "video_average_bitrate": 8500000,
      "video_gop_size": 60,
      "video_keyint_min": 60
    }
  ]
}
```

Every enabled channel must set `transcode_mode`; missing or invalid modes prevent channel workers
from launching. In passthrough mode, both encoder fields are ignored, the original MPEG-TS source
goes directly to Shaka, and FFmpeg is not launched. In transcode mode, only absent encoder keys
receive the `avc` (`libx264`) and `aac` defaults shown above. Explicit `null`, empty or whitespace-only
values, `none`, `copy`, and any unsupported encoder name fail configuration before any manager or
worker starts; they do not select stream copy.

In both HLS and DASH, audio labels default to `audio-N`: HLS uses this as the audio `name`, and DASH
uses it as the `label`. `N` is the original one-based audio source ordinal and is not renumbered when
`discard_first_audio` filters an earlier stream. A valid ASCII value in private ES descriptor tag
`0xFD` overrides both labels.

## Usage

From the Python project root:

```bash
python3 avpipeline.py --help
python3 avpipeline.py --config mvc.json start
python3 avpipeline.py --config mvc.json status
python3 avpipeline.py --config mvc.json urls
python3 avpipeline.py --config mvc.json stop
```

The same configuration option may follow the command, for example:

```bash
python3 avpipeline.py start --config mvc.json
```

| Command | Behavior |
| --- | --- |
| `start` | Checks required tools and starts a manager for each enabled channel. |
| `status` | Reports whether configured channel managers are running. |
| `urls` | Writes and prints `main_urls.json` with the HLS and DASH manifest paths. |
| `stop` | Stops all managers for the selected configuration and removes their generated media and manifests. |
| `restart` | Stops managers, waits two seconds, and starts them again. Generated outputs are cleaned during stop. |

## Output and Logs

For channel ID `mv-101`, output is written under `<baseDir>/mv-101/`. Shaka Packager owns the
HLS/DASH manifests and CMAF media. `main_urls.json` is written at the `baseDir` level.

The manager stores PID files and worker PID files under `/run/avpipeline`, and logs under
`/var/log/avpipeline`. The manager log is `avpipeline.log`; channel and worker logs are written
under the same log directory. Ensure the account running the manager can create and update these
paths and the configured output directory.

### Log Rotation

The manager and its FFmpeg/Shaka workers write to the same channel log file. To bound log growth,
install a logrotate rule such as `/etc/logrotate.d/avpipeline`:

```conf
/var/log/avpipeline/*.log {
    daily
    maxsize 128M
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

Run logrotate at least hourly so `maxsize` is checked promptly; it only takes effect when logrotate
runs. `copytruncate` keeps the shared file inode in place because child workers retain the open log
file descriptor. A rename-and-create rule would leave them writing to the renamed file. `copytruncate`
can lose a small number of lines during the copy/truncate interval; use a centralized log writer or
systemd journal if lossless rotation is required. Adjust `maxsize` and `rotate` to fit available
storage and retention requirements.

## Related Python Modules

| File | Responsibility |
| --- | --- |
| [`avpipeline.py`](./avpipeline.py) | CLI entry point, configuration, per-channel supervision, and recovery. |
| [`ts_topology.py`](./ts_topology.py) | TSDuck PSI/topology discovery and topology-change monitoring. |
| [`ffmpeg_command.py`](./ffmpeg_command.py) | FFmpeg argument construction for transcode mode. |
| [`shaka_command.py`](./shaka_command.py) | Shaka Packager argument construction for both modes. |
| [`mvc.json`](./mvc.json) | Example channel configuration. |

The helper modules are imported by `avpipeline.py`; they are not standalone command-line programs.
