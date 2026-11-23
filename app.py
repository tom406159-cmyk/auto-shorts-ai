import os
import re
import uuid
import shutil
import zipfile
import socket
import ipaddress
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "250")) * 1024 * 1024

BASE = Path(os.getenv("WORK_DIR", "/tmp/auto_shorts_ai"))
BASE.mkdir(parents=True, exist_ok=True)
JOBS = {}
LOCK = threading.Lock()


def run_cmd(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError((result.stderr or "FFmpeg failed")[-2500:])
    return result.stdout


def video_duration(path):
    out = run_cmd([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]).strip()
    return max(0.0, float(out))


def safe_download(url, destination):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Direct video URL must use HTTPS.")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        for item in addresses:
            ip = ipaddress.ip_address(item[4][0])
            if not ip.is_global:
                raise ValueError("This URL is not allowed.")
    except OSError as exc:
        raise ValueError("Could not verify the video URL.") from exc

    with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=False) as response:
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if not (content_type.startswith("video/") or "octet-stream" in content_type):
            raise ValueError("The URL does not appear to point directly to a video file.")
        total = 0
        limit = int(os.getenv("MAX_UPLOAD_MB", "250")) * 1024 * 1024
        with open(destination, "wb") as f:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Video is larger than the configured upload limit.")
                    f.write(chunk)


def make_ass(segments, path):
    def ts(seconds):
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 2", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,Arial,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,1,4,2,2,80,80,230,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    for seg in segments:
        text = re.sub(r"[{}]", "", seg.get("text", "")).strip().replace("\n", " ")
        if text:
            lines.append(f"Dialogue: 0,{ts(seg['start'])},{ts(seg['end'])},Default,,0,0,0,,{text}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def transcribe(path, model_name):
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(path), word_timestamps=False, vad_filter=True)
        return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    except ImportError:
        return []
    except Exception:
        return []


def process_job(job_id, source, count, clip_len, model_name, job_dir):
    try:
        with LOCK:
            JOBS[job_id]["status"] = "processing"
            JOBS[job_id]["message"] = "Video is being analyzed..."
        duration = video_duration(source)
        if duration <= 1:
            raise ValueError("Video is too short or unreadable.")
        count = min(count, max(1, int(duration // max(clip_len, 1))))
        segments = transcribe(source, model_name)
        # Spread clips across the source so the output is not limited to its opening.
        max_start = max(0, duration - clip_len)
        starts = [max_start * i / max(1, count - 1) for i in range(count)]
        outputs = []
        for index, start in enumerate(starts, 1):
            end = min(duration, start + clip_len)
            out = job_dir / f"short_{index:02d}.mp4"
            ass = job_dir / f"captions_{index:02d}.ass"
            clip_segments = []
            for seg in segments:
                a, b = max(0, seg["start"] - start), min(end - start, seg["end"] - start)
                if b > a:
                    clip_segments.append({"start": a, "end": b, "text": seg["text"]})
            make_ass(clip_segments, ass)
            vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
            if clip_segments:
                escaped = str(ass).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
                vf += f",ass='{escaped}'"
            run_cmd([
                "ffmpeg", "-y", "-ss", str(start), "-i", str(source), "-t", str(end-start),
                "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)
            ])
            outputs.append(out)
            with LOCK:
                JOBS[job_id]["progress"] = int(index * 100 / count)
                JOBS[job_id]["message"] = f"Created clip {index} of {count}"
        archive = job_dir / "shorts.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for file in outputs:
                z.write(file, file.name)
        with LOCK:
            JOBS[job_id].update(status="done", progress=100, message="Your clips are ready.", download=f"/api/jobs/{job_id}/download")
    except Exception as exc:
        with LOCK:
            JOBS[job_id].update(status="error", message=str(exc))


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/api/jobs")
def create_job():
    job_id = uuid.uuid4().hex
    job_dir = BASE / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    source = job_dir / "source_video.mp4"

    upload = request.files.get("video")
    direct_url = (request.form.get("video_url") or "").strip()
    youtube_url = (request.form.get("youtube_url") or "").strip()
    if youtube_url and not upload and not direct_url:
        return jsonify(error="YouTube watch links cannot be downloaded here. Upload your own video file or use a creator-authorized direct video file URL."), 400
    try:
        if upload and upload.filename:
            upload.save(source)
        elif direct_url:
            safe_download(direct_url, source)
        else:
            return jsonify(error="Upload a video file or enter a direct video file URL."), 400

        count = int(request.form.get("count", "10"))
        clip_len = int(request.form.get("clip_length", "30"))
        model_name = request.form.get("model", "base")
        if count not in (10, 12, 15, 18, 20):
            count = 10
        if clip_len not in (20, 30, 35, 45, 60):
            clip_len = 30
        if model_name not in ("tiny", "base", "small"):
            model_name = "base"

        with LOCK:
            JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Queued"}
        thread = threading.Thread(target=process_job, args=(job_id, source, count, clip_len, model_name, job_dir), daemon=True)
        thread.start()
        return jsonify(job_id=job_id)
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify(error=str(exc)), 400


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Job not found. It may have expired after a server restart."), 404
        return jsonify(job)


@app.get("/api/jobs/<job_id>/download")
def download_job(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Download is not ready."), 404
    archive = BASE / job_id / "shorts.zip"
    if not archive.exists():
        return jsonify(error="The download file is no longer available."), 404
    return send_file(archive, as_attachment=True, download_name="auto_shorts.zip")


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="Video is too large. Try a smaller file."), 413


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
