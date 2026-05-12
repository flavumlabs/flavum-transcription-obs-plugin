# Flavum Clipper — OBS Python script

Connects OBS to the [Flavum Clipper](https://flavum-clipper.com) backend for AI-powered stream-clipping. When a recording stops, the script extracts the audio, uploads it to the backend, waits for cut detection, and (optionally) runs FFmpeg locally to produce ready-to-publish video clips.

**Only audio leaves your machine.** The source video stays local — there's no full-video upload step.

## Status

Phase 8 (this commit): the script loads in OBS, shows a settings dialog, and verifies your API key against the backend. Recording hooks, audio extraction, upload, polling, and FFmpeg cutting land in phases 9 and 10.

## Requirements

- OBS Studio 28 or later
- FFmpeg on your `PATH` (used in phase 10 — not required to load the script)
- Python 3.10+ (Windows users must install Python and point OBS at it; Linux/macOS usually pick up the system Python)
- A Flavum Clipper account and API key from the dashboard

## Install

1. Download `flavum_clipper.py` from this repository (or clone the repo).
2. **Windows only:** install Python from python.org if you don't have it, then in OBS go to **Tools → Scripts → Python Settings** and point it at the install.
3. In OBS, open **Tools → Scripts**, click the **+** button at the bottom-left of the scripts list, and select `flavum_clipper.py`.
4. The script settings panel appears on the right. Paste your API key, set the backend URL (defaults to `http://localhost:4500` — fine for local dev, override for production), and click **Test connection**.

If the test succeeds you'll see something like:
```
Connected as you@example.com (free plan, 0/60 min used this month)
```

## Settings reference

| Setting | What it does |
|---------|--------------|
| **API key** | Authenticates uploads. Get one from your Flavum Clipper dashboard. |
| **Backend URL** | Where to send audio uploads. Default points at a local dev server; change for production. |
| **Auto-process recordings** | Upload + transcribe + detect cuts automatically when a recording ends. Off → manual trigger only. |
| **Auto-cut after analysis** | Run FFmpeg to produce video clips after cuts are detected. Off → just save `cuts.json`. |
| **Language hint** | Auto-detect by default. Picking a specific language slightly improves transcription accuracy. |
| **Target long-cut length** | The AI aims for cuts in this range (minutes). |
| **Upload audio bitrate** | 32 kbps is plenty for ASR and keeps uploads tiny. Lower = smaller upload. |
| **Cut output codec** | Auto-detect picks the best hardware encoder available. Override if you want a specific codec. |

## What happens when you record (phase 9+ behaviour)

1. Click Record in OBS as normal.
2. Record your stream.
3. Click Stop.
4. The script creates a folder `RECORDING-YYYY-MM-DD-HHMMSS/` next to your recording.
5. FFmpeg extracts the audio.
6. The audio is uploaded to the backend (audio only — your video stays local).
7. The backend transcribes the audio and detects cuts.
8. `cuts.json` is saved into the folder.
9. If **Auto-cut** is on, the script runs FFmpeg to produce one video file per cut under `cuts/`.

A system notification fires when cuts are ready. Click it to open the job in the web dashboard.

## Privacy

Only audio is uploaded. Your video stays on your machine. The backend deletes uploaded audio within 30 days. Review the backend's privacy policy before sharing recordings with sensitive material.

## License

MIT — see [`LICENSE`](LICENSE).
