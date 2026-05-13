# Flavum Clipper — OBS Python script

Connects OBS to the [Flavum Clipper](https://clipper.flavumlabs.com) backend for AI-powered stream-clipping. When a recording stops, the script extracts the audio, uploads it to the backend, waits for cut detection, and (optionally) runs FFmpeg locally to produce ready-to-publish video clips.

**Only audio leaves your machine.** The source video stays local — there's no full-video upload step. The audio upload is streamed in 64 KB chunks, so a multi-hour recording doesn't sit in OBS's memory.

## Requirements

- OBS Studio 28 or later
- FFmpeg on your `PATH` (used to extract audio from your recording and to produce the final video cuts)
- Python 3.10+ (Windows users must install Python and point OBS at it; Linux/macOS usually pick up the system Python)
- A Flavum Clipper account and API key from the dashboard

## Install

The Flavum Clipper dashboard always serves the latest supported plugin version. Don't clone this repo to get the script — use the dashboard so you stay in sync with the backend's compatibility floor.

1. Sign in at [clipper.flavumlabs.com](https://clipper.flavumlabs.com) and open **Install OBS** in the nav.
2. Create an API key on the dashboard if you don't have one yet.
3. Click **Download flavum_clipper.py** on the install page.
4. **Windows only:** install Python from python.org if you don't have it, then in OBS go to **Tools → Scripts → Python Settings** and point it at the install.
5. In OBS, open **Tools → Scripts**, click the **+** at the bottom-left of the scripts list, and select the downloaded `flavum_clipper.py`.
6. The script settings panel appears on the right. Paste your API key, leave the backend URL as-is, and click **Test connection**.

If the test succeeds you'll see something like:
```
Connected as you@example.com (free plan, 0/60 min used this month)
```

## Settings reference

| Setting | What it does |
|---------|--------------|
| **API key** | Authenticates uploads. Get one from your Flavum Clipper dashboard. |
| **Backend URL** | Where to send audio uploads. Defaults to the production backend; change only if you're running a local dev server. |
| **Auto-process recordings** | Upload + transcribe + detect cuts automatically when a recording ends. Off → manual trigger only. |
| **Auto-cut after analysis** | Run FFmpeg to produce video clips after cuts are detected. Off → just save `cuts.json`. |
| **Language hint** | Auto-detect by default. Picking a specific language slightly improves transcription accuracy. |
| **Target long-cut length** | The AI aims for cuts in this range (minutes). |
| **Upload audio bitrate** | 32 kbps is plenty for ASR and keeps uploads tiny. Lower = smaller upload. |
| **Cut output codec** | Auto-detect picks the best hardware encoder available. Override if you want a specific codec. |

## What happens when you record

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

## Keeping the plugin up to date

The script sends `X-Flavum-Plugin-Version` on every backend call. If the backend ever rolls its compatibility floor past your installed version, you'll see a "plugin out of date" message in the OBS scripts log with a link back to the install page — re-download from the dashboard to get the latest tagged release. In-the-wild plugins never auto-update.

## Privacy

Only audio is uploaded. Your video stays on your machine.

## License

MIT — see [`LICENSE`](LICENSE).
