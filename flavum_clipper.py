"""
Flavum Clipper — OBS Python script.

When you stop a recording, this script:
  1. Creates a sibling folder RECORDING-YYYY-MM-DD-HHMMSS/
  2. Extracts audio with FFmpeg (mono Opus, configurable bitrate)
  3. Uploads the audio to the Flavum Clipper backend
  4. Polls until cut detection finishes
  5. Writes cuts.json into the folder
  6. (If auto-cut is on) Re-encodes each cut into cuts/cut_NNN.mp4 with the
     best available hardware encoder, plus a sidecar cut_NNN.txt of titles +
     description + rationale.

Only audio leaves your machine; the source video stays local.

Pending jobs survive OBS restart — they're persisted to
~/.config/flavum-clipper/pending.json and resumed on script_load.

License: MIT — see LICENSE.
"""

import hashlib
import json
import os
import platform
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import obspython as obs


# ---------------------------------------------------------------------------
# Settings (mirrored from OBS on script_update)
# ---------------------------------------------------------------------------

_settings = {
    "api_key": "",
    "backend_url": "http://localhost:4500",
    "auto_process": True,
    "auto_cut": True,
    "language_hint": "auto",
    "target_long_cut_minutes": 8,
    "audio_bitrate_kbps": 32,
    "output_codec": "auto",
}

_test_status = 'Click "Test connection" once you\'ve pasted an API key.'


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------

_state_dir = Path.home() / ".config" / "flavum-clipper"
_state_file = _state_dir / "pending.json"

_queue_lock = threading.Lock()
_job_queue = []                # list of recording-folder paths waiting to process
_current_folder = None         # folder currently being processed (or None)
_worker_thread = None          # singleton worker (lazy-created)

_pipeline_status = "Idle"      # surfaced in the script properties pane
_log_queue = queue.Queue()     # worker → main-thread log marshaling

_detected_encoder = None       # cached HW encoder pick (lazy)


def _log(message):
    """Thread-safe pipeline log + status update.

    OBS's API is not thread-safe, so we enqueue the line here and drain it
    from a main-thread timer (`_drain_log_queue`).
    """
    global _pipeline_status
    _pipeline_status = message
    _log_queue.put(message)


def _drain_log_queue():
    """Main-thread timer callback — emits queued log lines to OBS."""
    while True:
        try:
            line = _log_queue.get_nowait()
        except queue.Empty:
            return
        obs.script_log(obs.LOG_INFO, f"[flavum] {line}")


# ---------------------------------------------------------------------------
# OBS lifecycle
# ---------------------------------------------------------------------------


def script_description():
    return (
        "<h2>Flavum Clipper</h2>"
        "<p>Upload finished recordings to Flavum Clipper for AI-powered "
        "cut detection. Audio is sent to the backend; your video stays "
        "on this machine.</p>"
        "<p>Get an API key from the Flavum Clipper dashboard, paste it "
        "below, and click <b>Test connection</b>.</p>"
    )


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "api_key", "")
    obs.obs_data_set_default_string(
        settings, "backend_url", "http://localhost:4500"
    )
    obs.obs_data_set_default_bool(settings, "auto_process", True)
    obs.obs_data_set_default_bool(settings, "auto_cut", True)
    obs.obs_data_set_default_string(settings, "language_hint", "auto")
    obs.obs_data_set_default_int(settings, "target_long_cut_minutes", 8)
    obs.obs_data_set_default_int(settings, "audio_bitrate_kbps", 32)
    obs.obs_data_set_default_string(settings, "output_codec", "auto")


def script_update(settings):
    _settings["api_key"] = obs.obs_data_get_string(settings, "api_key")
    _settings["backend_url"] = obs.obs_data_get_string(settings, "backend_url")
    _settings["auto_process"] = obs.obs_data_get_bool(settings, "auto_process")
    _settings["auto_cut"] = obs.obs_data_get_bool(settings, "auto_cut")
    _settings["language_hint"] = obs.obs_data_get_string(
        settings, "language_hint"
    )
    _settings["target_long_cut_minutes"] = obs.obs_data_get_int(
        settings, "target_long_cut_minutes"
    )
    _settings["audio_bitrate_kbps"] = obs.obs_data_get_int(
        settings, "audio_bitrate_kbps"
    )
    _settings["output_codec"] = obs.obs_data_get_string(
        settings, "output_codec"
    )


def script_load(settings):
    obs.obs_frontend_add_event_callback(_on_frontend_event)
    obs.timer_add(_drain_log_queue, 500)
    _state_dir.mkdir(parents=True, exist_ok=True)
    _restore_pending_state()


def script_unload():
    obs.timer_remove(_drain_log_queue)
    obs.obs_frontend_remove_event_callback(_on_frontend_event)


def script_save(settings):
    pass


def script_properties():
    props = obs.obs_properties_create()

    api_key_prop = obs.obs_properties_add_text(
        props, "api_key", "API key", obs.OBS_TEXT_PASSWORD
    )
    obs.obs_property_set_long_description(
        api_key_prop,
        "Bearer token used to authenticate uploads. Get one from "
        "Settings → API keys in the Flavum Clipper dashboard.",
    )

    backend_url_prop = obs.obs_properties_add_text(
        props, "backend_url", "Backend", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_property_set_long_description(
        backend_url_prop,
        "Base URL of the Flavum Clipper webservice. Defaults to a "
        "local dev server; change for production.",
    )

    obs.obs_properties_add_button(
        props, "test_connection", "Test connection", _on_test_connection
    )
    obs.obs_properties_add_text(
        props, "_test_status", _test_status, obs.OBS_TEXT_INFO
    )

    obs.obs_properties_add_text(
        props,
        "_pipeline_status",
        f"Pipeline: {_pipeline_status}",
        obs.OBS_TEXT_INFO,
    )

    auto_process_prop = obs.obs_properties_add_bool(
        props, "auto_process", "Auto-process"
    )
    obs.obs_property_set_long_description(
        auto_process_prop,
        "When a recording stops, extract audio and upload it to the "
        "backend automatically. Off → manual trigger only.",
    )

    auto_cut_prop = obs.obs_properties_add_bool(
        props, "auto_cut", "Auto-cut clips"
    )
    obs.obs_property_set_long_description(
        auto_cut_prop,
        "After cuts are detected, run FFmpeg to produce video clips. "
        "Off → just save cuts.json next to the recording.",
    )

    language_list = obs.obs_properties_add_list(
        props,
        "language_hint",
        "Language",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_set_long_description(
        language_list,
        "Hint passed to the transcriber. Auto-detect works in most cases.",
    )
    for label, value in [
        ("Auto-detect", "auto"),
        ("English", "en"),
        ("Português (BR)", "pt-BR"),
        ("Español", "es"),
    ]:
        obs.obs_property_list_add_string(language_list, label, value)

    cut_length_prop = obs.obs_properties_add_int_slider(
        props, "target_long_cut_minutes", "Cut length (min)", 1, 15, 1
    )
    obs.obs_property_set_long_description(
        cut_length_prop,
        "Target length the AI aims for when picking long-form cuts.",
    )

    bitrate_list = obs.obs_properties_add_list(
        props,
        "audio_bitrate_kbps",
        "Upload bitrate",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_INT,
    )
    obs.obs_property_set_long_description(
        bitrate_list,
        "Bitrate used when extracting audio for upload. 32 kbps is "
        "plenty for transcription; lower = smaller upload.",
    )
    for kbps in (16, 32, 64):
        obs.obs_property_list_add_int(bitrate_list, f"{kbps} kbps", kbps)

    codec_list = obs.obs_properties_add_list(
        props,
        "output_codec",
        "Output codec",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_set_long_description(
        codec_list,
        "Video codec FFmpeg uses when producing cut files. "
        "Auto-detect picks the best hardware encoder available.",
    )
    for label, value in [
        ("Auto-detect", "auto"),
        ("NVIDIA NVENC", "h264_nvenc"),
        ("Apple VideoToolbox", "h264_videotoolbox"),
        ("Intel QuickSync", "h264_qsv"),
        ("Linux VAAPI", "h264_vaapi"),
        ("AMD AMF", "h264_amf"),
        ("CPU libx264", "libx264"),
    ]:
        obs.obs_property_list_add_string(codec_list, label, value)

    return props


# ---------------------------------------------------------------------------
# Frontend events
# ---------------------------------------------------------------------------


def _on_frontend_event(event):
    if event == obs.OBS_FRONTEND_EVENT_RECORDING_STARTED:
        _log("Recording started")
    elif event == obs.OBS_FRONTEND_EVENT_RECORDING_STOPPED:
        _handle_recording_stopped()
    elif event == obs.OBS_FRONTEND_EVENT_EXIT:
        _log("OBS exiting; pending jobs will resume on next launch")


def _handle_recording_stopped():
    if not _settings["auto_process"]:
        _log("Recording stopped; auto-process is disabled")
        return

    recording_path = obs.obs_frontend_get_last_recording()
    if not recording_path:
        _log("No last-recording path available; skipping")
        return

    started_at = datetime.now(timezone.utc)
    folder = _make_recording_folder(recording_path, started_at)
    manifest = {
        "originalRecording": recording_path,
        "stoppedAt": started_at.isoformat(),
        "folder": str(folder),
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    _enqueue(folder)


def _make_recording_folder(recording_path, when):
    parent = Path(recording_path).parent
    stamp = when.strftime("%Y-%m-%d-%H%M%S")
    folder = parent / f"RECORDING-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _enqueue(folder):
    with _queue_lock:
        _job_queue.append(str(folder))
    _save_pending_state()
    _log(f"Queued: {folder.name}")
    _ensure_worker_running()


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


def _ensure_worker_running():
    global _worker_thread
    if _worker_thread is None or not _worker_thread.is_alive():
        _worker_thread = threading.Thread(
            target=_worker_loop, name="flavum-clipper-worker", daemon=True
        )
        _worker_thread.start()


def _worker_loop():
    global _current_folder
    while True:
        with _queue_lock:
            if not _job_queue:
                _current_folder = None
                _log("Idle")
                return
            _current_folder = _job_queue[0]

        folder = Path(_current_folder)
        try:
            _process_recording(folder)
        except Exception as err:  # pylint: disable=broad-except
            _log(f"Pipeline error for {folder.name}: {err}")
        finally:
            with _queue_lock:
                if _job_queue and _job_queue[0] == _current_folder:
                    _job_queue.pop(0)
            _save_pending_state()


def _process_recording(folder):
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        _log(f"Missing manifest in {folder.name}; dropping")
        return

    manifest = json.loads(manifest_path.read_text())
    recording = manifest.get("originalRecording")
    if not recording or not Path(recording).exists():
        _log(f"Source recording missing for {folder.name}; dropping")
        return

    audio_path = folder / "audio.opus"
    if not audio_path.exists():
        _log(f"Extracting audio from {Path(recording).name}")
        _extract_audio(recording, audio_path)

    duration = _ffprobe_duration(audio_path)
    sha = _sha256_file(audio_path)

    audio_kb = audio_path.stat().st_size // 1024
    _log(f"Uploading {audio_path.name} ({audio_kb} KB)")
    job_id = _upload_audio(audio_path, sha, duration)

    _log(f"Job {job_id} queued; polling")
    cuts = _poll_until_done(job_id)
    if cuts is None:
        return

    (folder / "cuts.json").write_text(json.dumps(cuts, indent=2))
    cut_list = cuts.get("cuts", [])
    _log(f"{folder.name}: {len(cut_list)} cut(s) saved")

    if cut_list and _settings["auto_cut"]:
        _produce_cut_files(folder, recording, cut_list)


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------


def _extract_audio(recording_path, audio_path):
    bitrate = _settings["audio_bitrate_kbps"]
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i", recording_path,
            "-vn",
            "-acodec", "libopus",
            "-b:a", f"{bitrate}k",
            "-ac", "1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg extract failed (exit {result.returncode}): "
            f"{result.stderr[-400:].strip()}"
        )


def _ffprobe_duration(audio_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return float(result.stdout.strip())


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Upload + poll
# ---------------------------------------------------------------------------


def _build_multipart(audio_path, sha, duration):
    boundary = "----flavum" + os.urandom(8).hex()
    sep = f"--{boundary}".encode()
    end = f"--{boundary}--".encode()
    crlf = b"\r\n"

    metadata = {"audioDurationSec": duration}
    language = _settings["language_hint"]
    if language and language != "auto":
        metadata["languageHint"] = language
    metadata["options"] = {
        "targetLongCutMinutes": _settings["target_long_cut_minutes"],
    }

    parts = []

    def push_text(name, value):
        parts.append(sep)
        parts.append(
            f'Content-Disposition: form-data; name="{name}"'.encode()
        )
        parts.append(b"")
        parts.append(str(value).encode())

    push_text("audioSha256", sha)
    push_text("metadata", json.dumps(metadata))

    parts.append(sep)
    parts.append(
        (
            f'Content-Disposition: form-data; name="audio"; '
            f'filename="{audio_path.name}"'
        ).encode()
    )
    parts.append(b"Content-Type: audio/opus")
    parts.append(b"")

    body = crlf.join(parts) + crlf
    with open(audio_path, "rb") as handle:
        body += handle.read()
    body += crlf + end + crlf

    return body, boundary


def _upload_audio(audio_path, sha, duration):
    backend = _settings["backend_url"].rstrip("/")
    body, boundary = _build_multipart(audio_path, sha, duration)
    req = urllib.request.Request(
        f"{backend}/api/v1/jobs",
        data=body,
        headers={
            "Authorization": f"Bearer {_settings['api_key']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())["jobId"]
    except urllib.error.HTTPError as err:
        # 409 = duplicate (same audioSha256). Body still contains existing jobId.
        if err.code == 409:
            payload = json.loads(err.read())
            return payload["jobId"]
        raise


def _poll_until_done(job_id, max_seconds=1800):
    backend = _settings["backend_url"].rstrip("/")
    headers = {"Authorization": f"Bearer {_settings['api_key']}"}
    deadline = time.time() + max_seconds
    last_status = None

    while time.time() < deadline:
        req = urllib.request.Request(
            f"{backend}/api/v1/jobs/{job_id}", headers=headers
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            doc = json.loads(resp.read())

        status = doc["status"]
        if status != last_status:
            _log(f"Job {job_id}: {status}")
            last_status = status

        if status == "COMPLETE":
            result_req = urllib.request.Request(
                f"{backend}/api/v1/jobs/{job_id}/result", headers=headers
            )
            with urllib.request.urlopen(result_req, timeout=30) as resp:
                return json.loads(resp.read())

        if status in ("FAILED", "CANCELLED"):
            _log(f"Job {job_id} {status.lower()}: {doc.get('error', '')}")
            return None

        time.sleep(5)

    _log(f"Job {job_id} polling timed out")
    return None


# ---------------------------------------------------------------------------
# State persistence — survive OBS restart with pending recordings
# ---------------------------------------------------------------------------


def _save_pending_state():
    payload = {"pendingFolders": list(_job_queue)}
    try:
        _state_file.write_text(json.dumps(payload, indent=2))
    except OSError as err:
        obs.script_log(
            obs.LOG_WARNING, f"[flavum] could not save state: {err}"
        )


def _restore_pending_state():
    global _job_queue
    if not _state_file.exists():
        return
    try:
        payload = json.loads(_state_file.read_text())
    except (OSError, json.JSONDecodeError) as err:
        obs.script_log(
            obs.LOG_WARNING, f"[flavum] could not load state: {err}"
        )
        return

    with _queue_lock:
        _job_queue = [
            folder
            for folder in payload.get("pendingFolders", [])
            if Path(folder).is_dir()
        ]

    if _job_queue:
        _log(f"Resuming {len(_job_queue)} pending recording(s)")
        _ensure_worker_running()


# ---------------------------------------------------------------------------
# FFmpeg cut production (phase 10)
# ---------------------------------------------------------------------------

# Encoder priority for the "auto" output codec setting. Falls back to libx264
# (always available) if none of the hardware encoders are present.
_ENCODER_PRIORITY = (
    "h264_nvenc",
    "h264_videotoolbox",
    "h264_qsv",
    "h264_amf",
    "h264_vaapi",
    "libx264",
)

# Extra flags appended to `-c:v <encoder>`. Quality knobs are tuned to a
# reasonable preview default (CRF/CQ ~20). Tweak per-encoder if needed.
_ENCODER_FLAGS = {
    "h264_nvenc": ["-preset", "fast", "-cq", "20"],
    "h264_videotoolbox": ["-b:v", "5M"],
    "h264_qsv": ["-preset", "fast", "-global_quality", "20"],
    "h264_amf": ["-quality", "balanced"],
    "h264_vaapi": ["-qp", "20"],
    "libx264": ["-preset", "fast", "-crf", "20"],
}


def _pick_video_encoder():
    """Pick the encoder for cut files. Honors the user's setting unless it's
    "auto", in which case we probe ffmpeg for available hardware encoders."""
    global _detected_encoder

    chosen = _settings["output_codec"]
    if chosen and chosen != "auto":
        _detected_encoder = chosen
        return chosen

    if _detected_encoder is not None:
        return _detected_encoder

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, check=True, timeout=10,
        )
        available_blob = result.stdout
    except Exception as err:  # pylint: disable=broad-except
        _log(f"Could not probe ffmpeg encoders ({err}); falling back to libx264")
        _detected_encoder = "libx264"
        return _detected_encoder

    for encoder in _ENCODER_PRIORITY:
        if f" {encoder} " in available_blob or f"\n{encoder} " in available_blob:
            _detected_encoder = encoder
            _log(f"Using video encoder: {encoder}")
            return encoder

    _detected_encoder = "libx264"
    return _detected_encoder


def _produce_cut_files(folder, recording_path, cuts):
    encoder = _pick_video_encoder()
    encoder_flags = _ENCODER_FLAGS.get(encoder, _ENCODER_FLAGS["libx264"])

    cuts_dir = folder / "cuts"
    cuts_dir.mkdir(exist_ok=True)

    total = len(cuts)
    produced = 0
    for index, cut in enumerate(cuts, start=1):
        _log(f"Cutting clip {index} of {total} ({encoder})")
        out_path = cuts_dir / f"cut_{index:03d}.mp4"
        sidecar_path = cuts_dir / f"cut_{index:03d}.txt"

        cmd = [
            "ffmpeg", "-hide_banner", "-y",
            "-ss", str(cut["start"]),
            "-to", str(cut["end"]),
            "-i", recording_path,
            "-c:v", encoder,
            *encoder_flags,
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(out_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            _log(
                f"Cut {index} failed (exit {result.returncode}): "
                f"{result.stderr[-300:].strip()}"
            )
            continue

        sidecar_path.write_text(_format_sidecar(cut, index))
        produced += 1

    _log(f"{produced}/{total} cut(s) saved to {cuts_dir}")
    _send_notification(
        f"Flavum Clipper — {produced} cut(s) ready",
        f"In {cuts_dir}",
    )


def _format_sidecar(cut, index):
    lines = [
        f"Cut #{index}",
        f"Time: {cut['start']} → {cut['end']}",
    ]
    confidence = cut.get("confidence")
    if confidence is not None:
        lines.append(f"Confidence: {confidence:.2f}")

    titles = cut.get("titleSuggestions") or []
    if titles:
        lines += ["", "Title suggestions:"]
        lines += [f"  - {title}" for title in titles]

    description = cut.get("description")
    if description:
        lines += ["", "Description:", description]

    tags = cut.get("tags") or []
    if tags:
        lines += ["", f"Tags: {', '.join(tags)}"]

    rationale = cut.get("rationale")
    if rationale:
        lines += ["", "Why this cut works:", rationale]

    return "\n".join(lines) + "\n"


def _send_notification(title, message):
    system = platform.system()
    try:
        if system == "Linux":
            subprocess.Popen(
                ["notify-send", title, message],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "Darwin":
            applescript = (
                f'display notification "{message}" with title "{title}"'
            )
            subprocess.Popen(
                ["osascript", "-e", applescript],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "Windows":
            # MessageBox is intrusive but ubiquitous. A proper Windows toast
            # needs BurntToast or a similar module — skip for MVP, log
            # instead so the user still sees it in the OBS scripts log.
            obs.script_log(obs.LOG_INFO, f"[flavum] {title} — {message}")
    except FileNotFoundError:
        # No notification binary available; fall back to the script log.
        obs.script_log(obs.LOG_INFO, f"[flavum] {title} — {message}")
    except Exception as err:  # pylint: disable=broad-except
        obs.script_log(obs.LOG_WARNING, f"[flavum] Notify failed: {err}")


# ---------------------------------------------------------------------------
# Test connection (phase 8)
# ---------------------------------------------------------------------------


def _on_test_connection(props, prop):
    global _test_status

    api_key = _settings["api_key"].strip()
    backend_url = _settings["backend_url"].rstrip("/")

    if not api_key:
        _test_status = "Enter an API key first."
    elif not backend_url:
        _test_status = "Enter a backend URL first."
    else:
        _test_status = _check_account(api_key, backend_url)

    status_prop = obs.obs_properties_get(props, "_test_status")
    if status_prop is not None:
        obs.obs_property_set_description(status_prop, _test_status)
    return True  # tells OBS to redraw the properties pane


def _check_account(api_key, backend_url):
    url = f"{backend_url}/api/v1/account"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        if err.code == 401:
            return "Authentication failed. Check your API key."
        if err.code == 402:
            return "Out of credits. Upgrade your plan to continue."
        return f"Backend error: HTTP {err.code}"
    except urllib.error.URLError as err:
        return f"Cannot reach backend at {backend_url}: {err.reason}"
    except Exception as err:  # pylint: disable=broad-except
        return f"Unexpected error: {err}"

    email = body.get("email", "(unknown email)")
    plan = body.get("plan", "?")
    used = body.get("minutesUsed", 0)
    limit = body.get("minutesLimit", 0)
    return (
        f"Connected as {email} "
        f"({plan} plan, {used}/{limit} min used this month)"
    )
