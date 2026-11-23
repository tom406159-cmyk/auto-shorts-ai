
import os, re, uuid, time, shutil, zipfile, threading, subprocess, tempfile, ipaddress, socket
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, render_template, abort
import requests
from faster_whisper import WhisperModel

APP_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/auto_shorts_jobs"))
WORK_DIR.mkdir(parents=True, exist_ok=True)
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "500")) * 1024 * 1024
JOBS = {}
LOCK = threading.Lock()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BYTES

def update(job_id, **kwargs):
    with LOCK:
        JOBS.setdefault(job_id, {}).update(kwargs)

def run_cmd(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout)[-2500:])
    return p.stdout.strip()

def is_youtube_url(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be" or host.endswith(".youtu.be")

def validate_public_url(url):
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Enter a valid public HTTP/HTTPS video URL.")
    if is_youtube_url(url):
        raise ValueError("YouTube links cannot be downloaded by this app. For your own channel, export/download your original video using YouTube Studio, then upload the file here.")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                raise ValueError("Private or local network URLs are not allowed.")
    except socket.gaierror:
        raise ValueError("The video URL host could not be resolved.")
    return url

def download_direct_video(url, destination):
    validate_public_url(url)
    # No redirects: avoids redirect-based access to internal services.
    with requests.get(url, stream=True, timeout=(10, 60), allow_redirects=False,
                      headers={"User-Agent": "AutoShortsStudio/1.0"}) as r:
        if r.is_redirect:
            raise ValueError("This URL redirects. Use the final direct video-file URL.")
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        if not (ctype.startswith("video/") or "octet-stream" in ctype):
            raise ValueError("URL must point directly to a video file (MP4/MOV/WebM), not a webpage.")
        total = 0
        with open(destination, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"Video exceeds the {MAX_BYTES // (1024*1024)} MB limit.")
                f.write(chunk)

def video_duration(path):
    return float(run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", str(path)]))

def t_ass(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def esc_ass(s):
    return s.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", " ")

def write_ass(words, start, end, path):
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,66,&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,70,70,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    for i, w in enumerate(words):
        a, b = max(float(w["start"]), start), min(float(w["end"]), end)
        if b <= a:
            continue
        lo, hi = max(0, i-3), min(len(words), i+4)
        phrase = []
        for j in range(lo, hi):
            token = esc_ass(words[j]["word"].strip())
            phrase.append((r"{\c&H00D7FF&}" + token + r"{\c&HFFFFFF&}") if j == i else token)
        ev.append(f"Dialogue: 0,{t_ass(a-start)},{t_ass(b-start)},Default,,0,0,0,,{' '.join(phrase)}")
    Path(path).write_text(head + "\n".join(ev), encoding="utf-8")

HOOK_WORDS = {"लेकिन", "क्योंकि", "सबसे", "पहली", "secret", "amazing", "important", "देखिए",
              "सोचिए", "याद", "गलती", "सच", "कभी", "कैसे", "why", "best", "नहीं", "लेकिन"}

def choose_segments(words, duration, count, clip_len):
    # Rank candidate speech windows using speech density + hook-word bonus, then spread them out.
    if not words:
        raise ValueError("Speech was not detected. Use a video with clear spoken audio.")
    step = max(5, int(clip_len // 3))
    candidates = []
    start = 0.0
    while start < duration:
        end = min(duration, start + clip_len)
        inside = [w for w in words if w["start"] < end and w["end"] > start]
        if end - start >= 8 and inside:
            spoken = sum(max(0, min(w["end"], end)-max(w["start"], start)) for w in inside)
            density = spoken / max(1, end-start)
            hooks = sum(1 for w in inside if w["word"].strip(".,!?।").lower() in HOOK_WORDS)
            score = density + min(hooks, 5) * 0.035
            candidates.append((score, start, end))
        start += step
    candidates.sort(reverse=True)
    selected = []
    min_gap = max(5, clip_len * 0.55)
    for score, a, b in candidates:
        if all(abs(a-x[0]) >= min_gap for x in selected):
            selected.append((a,b,score))
        if len(selected) >= count:
            break
    selected.sort(key=lambda x:x[0])
    return [(a,b) for a,b,_ in selected]

def render_one(src, words, start, end, dest):
    with tempfile.TemporaryDirectory() as td:
        ass = Path(td) / "captions.ass"
        write_ass(words, start, end, ass)
        # ffmpeg subtitles filter requires escaping the path for filter syntax.
        ap = str(ass).replace("\\", "/").replace(":", r"\:")
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,subtitles='{ap}'"
        run_cmd(["ffmpeg", "-y", "-ss", str(start), "-i", str(src), "-t", str(end-start),
                 "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dest)])

def process_job(job_id, src, count, clip_len, model_name):
    folder = WORK_DIR / job_id
    try:
        update(job_id, status="processing", message="Reading video…", progress=5)
        duration = video_duration(src)
        if duration < 15:
            raise ValueError("Video is too short. Please use a video longer than 15 seconds.")
        update(job_id, message="Transcribing speech with AI…", progress=10)
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(str(src), word_timestamps=True, vad_filter=True)
        words = []
        for seg in segments:
            for w in (seg.words or []):
                words.append({"start": float(w.start), "end": float(w.end), "word": w.word})
        if not words:
            raise ValueError("No speech transcript found. The current version needs spoken audio for caption timing.")
        windows = choose_segments(words, duration, count, clip_len)
        if not windows:
            raise ValueError("Could not find enough suitable spoken sections. Try a longer video or shorter clip length.")
        outputs = []
        for i, (a,b) in enumerate(windows, 1):
            update(job_id, message=f"Rendering short {i}/{len(windows)}…",
                   progress=15 + int(80 * (i-1) / len(windows)))
            out = folder / f"short_{i:02d}.mp4"
            render_one(src, words, a, b, out)
            outputs.append(out)
        zip_path = folder / "auto_shorts_pack.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p in outputs:
                z.write(p, p.name)
        update(job_id, status="done", message=f"Created {len(outputs)} Shorts.",
               progress=100, clips=[p.name for p in outputs], zip=str(zip_path))
    except Exception as e:
        update(job_id, status="error", message=str(e), progress=100)
    finally:
        try: Path(src).unlink(missing_ok=True)
        except Exception: pass

@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/jobs")
def create_job():
    count = int(request.form.get("count", "10"))
    clip_len = int(request.form.get("clip_len", "35"))
    model_name = request.form.get("model", "tiny")
    if count < 10 or count > 20 or clip_len < 15 or clip_len > 60 or model_name not in ("tiny","base","small"):
        return jsonify(error="Invalid settings."), 400
    job_id = uuid.uuid4().hex
    folder = WORK_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "source_video"
    upload = request.files.get("video")
    url = (request.form.get("video_url") or "").strip()
    try:
        if upload and upload.filename:
            upload.save(src)
            if src.stat().st_size > MAX_BYTES:
                raise ValueError(f"Video exceeds {MAX_BYTES // (1024*1024)} MB.")
        elif url:
            download_direct_video(url, src)
        else:
            raise ValueError("Upload a video or provide a direct video-file URL.")
        # Verify the file is readable by ffprobe before accepting the job.
        video_duration(src)
    except Exception as e:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error=str(e)), 400
    with LOCK:
        JOBS[job_id] = {"status":"queued","message":"Queued…","progress":0}
    threading.Thread(target=process_job, args=(job_id, str(src), count, clip_len, model_name), daemon=True).start()
    return jsonify(job_id=job_id)

@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with LOCK:
        data = JOBS.get(job_id)
    if not data:
        abort(404)
    return jsonify(data)

@app.get("/api/jobs/<job_id>/download")
def download_job(job_id):
    with LOCK:
        data = JOBS.get(job_id)
    if not data or data.get("status") != "done":
        abort(404)
    path = Path(data["zip"])
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name="auto_shorts_pack.zip")

@app.errorhandler(413)
def too_large(_):
    return jsonify(error=f"Upload too large. Limit is {MAX_BYTES // (1024*1024)} MB."), 413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), debug=False)
