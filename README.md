# Auto Shorts AI — Online starter

A deployable Flask website that accepts (1) video uploads or (2) direct public video-file URLs, transcribes speech with Faster-Whisper, selects speech-rich windows, renders 9:16 clips with word-timed highlighted captions, and packages the clips into a ZIP.

## Important YouTube limitation
This app deliberately does **not** download arbitrary YouTube watch URLs. YouTube's terms and copyright permissions can restrict downloading/re-editing. If it is your own channel, use YouTube Studio to obtain your original video file and upload it. The URL option accepts direct MP4/MOV/WebM file URLs from a source you have permission to use.

## Run locally
Requirements: Python 3.11+, Docker or FFmpeg installed (`ffmpeg` and `ffprobe` must be on PATH).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:7860. First run downloads the Whisper model, so the server needs internet access.

## Deploy online with Render
1. Create a GitHub repository and upload all files in this folder.
2. Sign in to Render and choose **New > Blueprint**.
3. Connect the repository; Render reads `render.yaml` and builds the Docker image.
4. Open the deployed URL after build completes.

### Free-hosting reality
The website code is free/open-source. Free web hosts have CPU, memory, sleep, execution-time and temporary-storage limits. Whisper + FFmpeg video processing can exceed free-instance limits, especially for long/high-resolution videos or the `base`/`small` models. Start with `tiny`, short videos, and modest file sizes. A reliable public service for many users usually needs a paid CPU worker, persistent storage, job queue, and per-user limits. Render free instances may sleep and local files are ephemeral; treat this as a demo, not durable production storage.

## Current features / known limits
- Upload or direct video-file URL input.
- 10–20 vertical clips; 20–60 seconds.
- 1080x1920 centered crop.
- Word-timed captions with active word highlight.
- Speech-rich moment selection heuristic (not a guarantee of viral clips).
- ZIP download.
- In-memory job state; one app process is configured in Docker.
- No account system, billing, cloud database, face tracking, automatic speaker tracking, or guaranteed viral ranking yet.
- The first request can be slow because the selected Whisper model is downloaded and loaded.

## Production checklist
Add user authentication, rate limits, malware scanning, object storage, durable task queue (Redis/Celery), database-backed jobs, automatic cleanup, privacy policy, consent/rights checks, monitoring, and worker autoscaling before opening it to the public.
