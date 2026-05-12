"""
Flavum Clipper — OBS Python script (phase 8 scaffold).

Connects OBS to the Flavum Clipper backend. Upcoming phases (9, 10) add
recording hooks, audio extraction, upload, polling, and FFmpeg cutting.
Phase 8 just gets the settings dialog + a working "Test connection" call.

Audio leaves the machine; the source video stays local.

License: MIT — see LICENSE.
"""

import json
import urllib.error
import urllib.request

import obspython as obs


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

# Settings are mirrored here from OBS on script_update so the button callback
# (which receives no settings argument) can read the latest values.
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
    # Recording hooks will be registered here in phase 9.
    pass


def script_unload():
    pass


def script_save(settings):
    # OBS auto-persists property values. Nothing extra to do.
    pass


def script_properties():
    props = obs.obs_properties_create()

    obs.obs_properties_add_text(
        props, "api_key", "API key", obs.OBS_TEXT_PASSWORD
    )
    obs.obs_properties_add_text(
        props, "backend_url", "Backend URL", obs.OBS_TEXT_DEFAULT
    )

    obs.obs_properties_add_button(
        props, "test_connection", "Test connection", _on_test_connection
    )
    obs.obs_properties_add_text(
        props, "_test_status", _test_status, obs.OBS_TEXT_INFO
    )

    obs.obs_properties_add_bool(
        props, "auto_process", "Auto-process recordings when they stop"
    )
    obs.obs_properties_add_bool(
        props,
        "auto_cut",
        "Auto-cut clips after analysis (re-encoded, HW-accelerated)",
    )

    language_list = obs.obs_properties_add_list(
        props,
        "language_hint",
        "Language hint",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    for label, value in [
        ("Auto-detect", "auto"),
        ("English", "en"),
        ("Português (BR)", "pt-BR"),
        ("Español", "es"),
    ]:
        obs.obs_property_list_add_string(language_list, label, value)

    obs.obs_properties_add_int_slider(
        props,
        "target_long_cut_minutes",
        "Target long-cut length (minutes)",
        1,
        15,
        1,
    )

    bitrate_list = obs.obs_properties_add_list(
        props,
        "audio_bitrate_kbps",
        "Upload audio bitrate (kbps)",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_INT,
    )
    for kbps in (16, 32, 64):
        obs.obs_property_list_add_int(bitrate_list, f"{kbps} kbps", kbps)

    codec_list = obs.obs_properties_add_list(
        props,
        "output_codec",
        "Cut output codec",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    for label, value in [
        ("Auto-detect (recommended)", "auto"),
        ("NVIDIA NVENC", "h264_nvenc"),
        ("Apple VideoToolbox", "h264_videotoolbox"),
        ("Intel QuickSync", "h264_qsv"),
        ("Linux VAAPI", "h264_vaapi"),
        ("AMD AMF", "h264_amf"),
        ("CPU libx264 (fallback)", "libx264"),
    ]:
        obs.obs_property_list_add_string(codec_list, label, value)

    return props


# ---------------------------------------------------------------------------
# Test connection
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
    except Exception as err:
        return f"Unexpected error: {err}"

    email = body.get("email", "(unknown email)")
    plan = body.get("plan", "?")
    used = body.get("minutesUsed", 0)
    limit = body.get("minutesLimit", 0)
    return (
        f"Connected as {email} "
        f"({plan} plan, {used}/{limit} min used this month)"
    )
