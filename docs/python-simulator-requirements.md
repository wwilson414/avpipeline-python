# Python Simulator Requirements

## Status

These requirements apply only to the Python simulator. They do not define or change the separate
C++ runtime requirements.

## Purpose

Use the Python simulator to provide encoded live streams and packaged HLS/DASH output for
downstream testing when production encoder and Packager components are unavailable.

## Scope and Boundaries

- Use [`mc2slicing.py`](../mc2slicing.py) as the implementation baseline.
- Changes may include directly related Python helpers, focused Python tests, and this document.
- Do not modify C++ source, C++ tests, CMake files, or other C++ runtime/build components.
- FFmpeg may perform software encoding, but it must not produce the final HLS/DASH segments or
  manifests.
- Shaka Packager is the sole owner of the simulator's HLS/DASH segments and manifests.
- The worktree is dedicated to this simulator requirement; unrelated work is out of scope.

## Functional Requirements

### Encoding and Packaging

1. Every enabled channel must explicitly set `transcode_mode` to `transcode` or `passthrough`.
   Missing or invalid values are configuration errors and prevent channel workers from launching.
   In `passthrough`, ignore `video_encoder` and `audio_encoder` and send the original source
   directly to Shaka without launching FFmpeg. In `transcode`, an absent `video_encoder` key
   defaults to `avc` (`libx264`) and an absent `audio_encoder` key defaults to `aac`. For either
   encoder, explicit JSON `null`, empty or whitespace-only values, the string `none`
   (case-insensitive), or `copy` are invalid transcode settings and prevent channel workers from
   launching; they must not silently select stream copy. Transcode encoder names are restricted to
   `avc`, `h264`, `h.264`, `libx264`, or `libx265` for video and `aac`, `libfdk_aac`, or `opus` for
   audio, case-insensitively. Any other name is an error before workers start.
2. In transcode mode, the manager's main control process owns and starts the TSDuck PSI/topology
   monitor and consumes its topology events; TSDuck's native TSProcessor may process asynchronously
   internally. FFmpeg independently opens the FFmpeg-normalized source. Multicast may have these
   two receivers concurrently; TSDuck must not forward media bytes to FFmpeg.
3. In passthrough mode, Shaka Packager reads the original configured MPEG-TS source directly, with
   no FFmpeg worker; encoder settings do not cause transcoding in this mode. Direct UDP input enables
   socket reuse so Shaka can consume the source concurrently with TSDuck topology monitoring.
4. In transcode mode, use one FFmpeg worker for the shared MPEG-TS source. It must handle all
   discovered audio and video streams in one pipeline using the configured encoders or the
   `avc`/`aac` defaults for omitted settings. The FFmpeg worker outputs one interleaved MPEG-TS
   stream over local UDP unicast to Shaka.
5. Use the Shaka Packager CLI at version 3.9.3 with the project
   `--hls_discontinuity_signal_file` patch. Before starting transcode workers, verify the executable
   version and required flag; missing or mismatched support is an explicit startup error. Shaka must
   produce live HLS and DASH manifests and CMAF media from either the original source (passthrough
   mode) or FFmpeg's local UDP output (encoding mode).
6. Shaka is the sole owner of HLS/DASH segments and manifests. Do not split video and audio into
   separate FFmpeg workers or implement a separate A/V synchronizer.
7. Preserve the A/V timestamp relationship carried by the source. Do not realign it to system
   wall-clock time or add separate video/audio timestamp offsets.

### FFmpeg Timestamp Handling

1. In encoding mode, let FFmpeg demux and process the shared input's PTS/DTS relationship in its
   single A/V pipeline. Passthrough mode must send the original source directly to Shaka.
2. Do not use `-use_wallclock_as_timestamps`, `-itsoffset`, or audio resampling options that reset
   the timeline (such as `aresample=async=1:first_pts=0`) to re-align the streams. Transcode mode
   uses `aresample=async=1` without `first_pts` to fill or trim decoded audio against its input PTS,
   preventing audio timestamp gaps while preserving the source-relative A/V timing.
3. In transcode mode, keep FFmpeg's MPEG-TS discontinuity correction enabled with
   `-dts_delta_threshold`; do not pass `-copyts`, which disables that correction. Do not impose
   finite UDP receive timeouts on the long-lived FFmpeg input or TSDuck PSI monitor.
4. Python must not synthesize timestamps, use wall-clock packet arrival as media time, or apply
   independent audio/video offsets. Let the shared FFmpeg process preserve the source A/V
   relationship while correcting a source timestamp discontinuity. Use video `-fps_mode passthrough`
   so output frame-rate synchronization does not drop decoded frames to force a constant frame rate.
5. An optional `debug_ffmpeg_timestamps` channel setting enables FFmpeg `-progress` summaries
   every ten seconds, including encoded video frame count and output time. Preserve the first
   non-monotonic DTS warning and summarize repeats; do not enable per-packet `-debug_ts` tracing for
   routine recovery diagnosis.

### HLS Program-Date-Time Retention

1. Shaka Packager 3.9.3 remains the sole writer of the simulator's public HLS playlists and owner
   of segment retention. The Python command passes `--add_program_date_time`, defaults
   `preserved_segments_outside_live_window` to `10`, and sets `--minimum_update_period` equal to
   `--segment_duration`.
2. Shaka must emit PDT before the first retained segment in each current playlist and after each
   discontinuity, including after sliding-window rotation. Generated PDT entries must not be stored
   in the playlist entry list consumed by `SlideWindow()`.
3. Preserve actual A/V media-time relationships. Equal `MEDIA-SEQUENCE` values do not prove equal
   presentation times and must not be used to align audio and video.
4. Do not modify `SlideWindow()` or its deletion logic. The existing cue-event
   `kExtPlacementOpportunity` interaction is out of scope for simulator paths that do not emit cue
   events.
5. This requirement does not promise absolute UTC accuracy or continuity after an FFmpeg or
   Packager process exit. It covers upstream input interruption/restart while the long-lived
   transcode and packaging processes remain alive. Do not modify the Shaka source or C++ runtime,
   add PDT behavior, deploy, or submit upstream changes as part of this scope.

### Progressive-Refresh Video Decoding

1. In encoding mode, the FFmpeg CLI video worker must accept progressive-refresh streams, including
   streams that begin decoding before the next IDR frame. Configure the decoder with
   `-err_detect:v ignore_err`, `-flags:v +output_corrupt`, and `-flags2:v +showall` so recoverable
   errors do not stop decoding and frames before the first keyframe can be emitted. Leave picture
   reconstruction and decoder reference-state handling to FFmpeg.
2. The application must not gate input on IDR, GDR, recovery-point, or clean-frame detection; it
   must not perform application-level picture restoration, NALU/frame splicing, or frame
   suppression based on perceived recovery progress.
3. FFmpeg CLI arguments must be reviewed and tested to ensure they do not prevent normal decoding
   of progressive-refresh input or discard frames needed by the decoder. Ordinary configured
   filtering and encoding remain separate from picture restoration.
4. Application-level state is limited to worker and stream-operational status. It must not infer
   visual recovery from decoded-frame content to control worker packet flow.

### Topology Discovery and Worker Selection

1. Before starting media workers, the manager must use the TSDuck Python API to discover the
   MPEG-TS program and elementary-stream topology from PAT/PMT.
2. Each channel must use one shared MPEG-TS `source` URL. The supported source topology is one
   program with one video elementary stream and zero or more audio elementary streams. The topology
   snapshot must retain stream order, type, PID, codec information, and descriptors needed for
   manifest metadata.
3. The manager must use the topology snapshot to construct Shaka stream descriptors in both modes
   and FFmpeg stream mappings in encoding mode; stream selection must not depend on configured
   `audio_N` entries.
4. Passthrough mode creates one Shaka Packager worker and no FFmpeg worker. Encoding mode creates
   one FFmpeg worker for the program and one Shaka Packager worker for the channel.
5. Every discovered audio elementary stream remains eligible for packaging regardless of whether
   it has descriptor `0xFD`. Associate each `0xFD` descriptor with its elementary stream by PID,
   decode its value as ASCII, and use that value as the audio manifest label when valid.
6. `discard_first_audio` is a boolean channel option defaulting to `false`. When `true`, omit only
   the first audio elementary stream in PMT/source order before FFmpeg mapping or Shaka packaging,
   in both transcode and passthrough modes. Do not renumber the remaining audio tracks: fallback
   labels retain their one-based source ordinals (`audio-2`, `audio-3`, ...), and valid `0xFD`
   descriptor values remain the labels for their respective retained tracks.
7. Publish topology changes through state-transition logs, including the differing old/new fields
   that trigger worker replacement. Log PAT/PMT version-only updates as ignored while retaining
   the existing workers. Do not write health or topology JSON files.
8. Use complete, parseable current PAT/PMT tables to form topology candidates. An effective
   media-topology change must not leave the FFmpeg mapping or Shaka descriptors bound to stale PIDs
   or labels. A PAT/PMT version-only update with unchanged program, PIDs, codecs, and stream
   descriptors is not an effective topology change and must not restart workers. When an active
   topology differs, retain the current workers until the same candidate remains unchanged for
   three seconds; cancel the candidate if it reverts or changes. Then keep TSDuck PSI monitoring
   active, stop and reap the FFmpeg/Packager worker thread, clean the retired generation's owned
   files, and start a replacement worker with mappings and descriptors from the stable PSI.

### Worker Supervision

1. A dedicated child Python worker thread supervises the FFmpeg and Shaka subprocesses in
   transcode mode. The manager's main control process owns the TSDuck PSI monitor and worker-thread
   lifecycle.
2. The channel manager must supervise the workers required by the selected mode and track each
   worker's process state, PID, exit status, restart count, and retry time.
3. Passthrough mode supervises Shaka as the direct source consumer; encoding mode starts Shaka
   before FFmpeg and starts the FFmpeg producer only after Shaka has launched.
4. In encoding mode, restart an unexpectedly exited FFmpeg worker with bounded backoff while Shaka
   remains healthy.
5. If Shaka exits or its output becomes stale beyond the configured threshold, stop and reap FFmpeg
   when present, then restart workers in the selected mode and required startup order.
6. After a replacement Shaka worker starts, remove all media and init files belonging to retired
   generations. Preserve the active generation, current manifests, and its configured
   `preserved_segments_outside_live_window` behavior; stop/restart cleanup removes generated files
   from every generation.
7. On requested shutdown, stop FFmpeg before Shaka when FFmpeg is present, wait for child exit, and
   reap all children. Escalate from graceful termination to forced termination after a bounded
   timeout.
8. Preserve explicit process ownership and prevent orphaned workers in either mode.
9. After a confirmed FFmpeg timestamp epoch reset, keep Shaka running, restart only FFmpeg, and
   signal Shaka's HLS notifier to insert `#EXT-X-DISCONTINUITY` before the next segment in each
   active media playlist. Keep the existing playlist and media-sequence state.

### Input Interruption and Runtime Exceptions

1. Temporary UDP packet absence alone must not terminate the manager or force an FFmpeg/Shaka worker
   restart. If FFmpeg reports an input timestamp discontinuity or non-monotonic output DTS, keep
   Shaka and TSDuck running; restart FFmpeg with an MPEG-TS discontinuity indicator and signal
   Shaka's HLS notifier to mark the next audio/video segments.
2. The manager must avoid a tight worker restart loop. Stale Shaka output during confirmed input
   absence is not, by itself, evidence that the Packager process has failed.
3. If the TSDuck monitor reports a transient unavailable condition after workers are active, preserve
   FFmpeg and Shaka and reattach the monitor using the last known topology. When input returns,
   rediscover topology with TSDuck. If unchanged, retain workers unless FFmpeg reports a timestamp
   discontinuity, in which case restart only FFmpeg and signal the active Shaka notifier; if changed,
   rebuild workers according to the topology-change policy. Recovery must not require a manual
   manager restart.
4. Network and child-process failures must be logged, cleaned up, and retried according to the
   worker recovery policy.
5. Python runtime exceptions at worker or manager boundaries must be logged with traceback and
   context. Recoverable exceptions must trigger cleanup and bounded recovery while keeping the
   manager alive. Unrecoverable invariant/configuration failures must enter an explicit fault state
   and clean up children rather than being silently swallowed.
6. Only an explicit stop request or a documented unrecoverable startup condition may terminate the
   manager process.

### Health and Diagnostics

1. Do not write `health.json` or `health_index.json`; no external program consumes them.
2. Keep the health state required for supervision in memory.
3. Record state transitions, child PIDs, exits, stale-output detection, failure reasons, and retry
   delays in the existing log system. Avoid repeating unchanged health state on every manager loop.

### Audio Descriptor Labels

1. Every discovered audio elementary stream remains eligible for packaging independently of
   whether it contains descriptor tag `0xFD`. The descriptor is metadata only and must not filter
   audio.
2. For each audio elementary stream, read the PMT ES descriptor loop and associate descriptor
   tag `0xFD` with the correct PID.
3. Decode the `0xFD` descriptor value as ASCII and use the resulting value as the HLS audio
   rendition name and DASH audio label. If the descriptor is missing, empty, or not valid ASCII,
   use `audio-N` as the default label, where `N` is the zero-based audio-stream ordinal in PMT
   order. If descriptor values collide, use the zero-based `audio-N` label for each colliding
   stream and log the collision.
4. Keep stable audio ordinals for internal stream identity; manifest labels must not change audio
   admission or renumber tracks.

### Audio Language Metadata

1. TSDuck must extract each audio elementary stream's language metadata from the original PMT
   before the packaging worker starts.
2. Python must validate and normalize the language. If it is absent, empty, invalid, or unknown,
   use `en` (English) for this simulator's HLS/DASH output. This Python-only value does not change
   the C++ runtime language contract.
3. Pass the resolved language explicitly in the Shaka stream configuration so Shaka writes it to
   the HLS and DASH manifests. Do not rely on Shaka to re-infer it from FFmpeg's output stream.
4. Language fallback is manifest metadata only and must not affect audio admission or worker
   selection.

## Validation Requirements

- Focused Python tests must cover PMT/descriptor parsing, ASCII label extraction, stream-to-label
  association, missing/invalid descriptor fallback, language fallback to English, and the agreed
  duplicate-label behavior.
- Manager tests must cover explicit and inferred mode selection, topology-driven mapping,
  direct-input mode with no FFmpeg worker, encoding mode with one FFmpeg worker per channel,
  startup order, mode-specific worker recovery, graceful shutdown, child reaping, backoff, and
  log-only health reporting.
- Recovery tests must cover input EOF, network loss and return, recoverable Python exceptions, and
  confirmation that the manager remains alive and does not spin-restart workers while input is
  absent.
- Validate that FFmpeg commands no longer request HLS/DASH output and that Shaka owns the
  generated manifests and segments.
- Run Python syntax/tests and `git diff --check`; no C++ build is in scope.

## Environment Constraint

The implementation must establish a compatible TSDuck Python binding and declare it as a runtime
dependency. The current environment has the TSDuck `tsp` executable, but does not have an
importable `tsduck` Python module. Dependency setup is part of this worktree's implementation
scope.
