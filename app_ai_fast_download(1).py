import os, uuid, shutil, zipfile, subprocess, threading, re
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "150"))
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024
WORK = Path(os.getenv("WORK_DIR", "/tmp/auto_shorts_ai_fast"))
WORK.mkdir(parents=True, exist_ok=True)
JOBS = {}
LOCK = threading.Lock()

def cmd(args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError((p.stderr or "FFmpeg processing failed")[-2200:])
    return p.stdout.strip()

def duration(path):
    return float(cmd(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)]))

def stamp(t):
    h=int(t//3600); m=int((t%3600)//60); s=t%60
    return f"{h}:{m:02d}:{s:05.2f}"

def transcribe_once(path, model_name):
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=2)
    segs, info = model.transcribe(str(path), word_timestamps=False, vad_filter=True, beam_size=1)
    result=[]
    for s in segs:
        txt=re.sub(r"[{}]", "", s.text.strip()).replace("\n"," ")
        if txt:
            result.append({"start":float(s.start),"end":float(s.end),"text":txt})
    return result

def write_ass(segments, start, end, path):
    lines=[
      "[Script Info]","ScriptType: v4.00+","PlayResX: 720","PlayResY: 1280","WrapStyle: 2","",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
      "Style: Default,Arial,48,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,-1,0,1,3,1,2,55,55,150,1","",
      "[Events]","Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
    ]
    for s in segments:
        a=max(start,s["start"]); b=min(end,s["end"])
        if b>a:
            lines.append(f"Dialogue: 0,{stamp(a-start)},{stamp(b-start)},Default,,0,0,0,,{s['text']}")
    Path(path).write_text("\n".join(lines),encoding="utf-8")

def run_job(jid, source, folder, count, seconds, model_name):
    try:
        with LOCK: JOBS[jid].update(status="processing",progress=2,message="वीडियो की जाँच हो रही है…")
        total=duration(source)
        if total < 3: raise ValueError("वीडियो बहुत छोटा है या पढ़ा नहीं जा सका।")
        with LOCK: JOBS[jid].update(progress=5,message="AI आवाज़ पहचान रहा है…")
        transcript=transcribe_once(source,model_name)
        with LOCK: JOBS[jid].update(progress=25,message="AI captions तैयार। अब clips बन रहे हैं…")
        possible=max(1,int(total//seconds))
        n=min(count,possible)
        clip_len=min(seconds,total)
        max_start=max(0,total-clip_len)
        made=[]
        for i in range(n):
            start=max_start*i/max(1,n-1)
            end=min(total,start+clip_len)
            out=folder/f"short_{i+1:02d}.mp4"
            ass=folder/f"captions_{i+1:02d}.ass"
            write_ass(transcript,start,end,ass)
            escaped=str(ass).replace("\\","\\\\").replace(":","\\:").replace("'","\\'")
            vf=f"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,ass='{escaped}'"
            cmd(["ffmpeg","-y","-ss",f"{start:.2f}","-i",str(source),"-t",f"{end-start:.2f}",
                 "-vf",vf,"-c:v","libx264","-preset","ultrafast","-crf","27","-threads","2",
                 "-c:a","aac","-b:a","96k","-movflags","+faststart",str(out)])
            made.append(out)
            with LOCK: JOBS[jid].update(progress=25+int((i+1)*70/n),message=f"AI captions के साथ clip {i+1}/{n} तैयार हो रही है…")
        archive=folder/"shorts.zip"
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
            for f in made: z.write(f,f.name)
        with LOCK: JOBS[jid].update(status="done",progress=100,message=f"तैयार! {n} AI-caption clips बन गईं।",download=f"/api/jobs/{jid}/download")
    except Exception as e:
        msg=str(e)
        if "No module named" in msg and "faster_whisper" in msg:
            msg="AI package install नहीं हुआ। Render में requirements.txt में faster-whisper होना चाहिए।"
        with LOCK: JOBS[jid].update(status="error",message=msg)

@app.get("/")
def home(): return render_template("index.html")

@app.post("/api/jobs")
def create():
    up=request.files.get("video")
    if not up or not up.filename: return jsonify(error="पहले वीडियो फ़ाइल चुनें।"),400
    jid=uuid.uuid4().hex; folder=WORK/jid; folder.mkdir(parents=True,exist_ok=True); source=folder/"input_video"
    try:
        up.save(source)
        if source.stat().st_size==0: raise ValueError("अपलोड की गई फ़ाइल खाली है।")
        count=int(request.form.get("count","5")); seconds=int(request.form.get("clip_length","20"))
        model=request.form.get("model","tiny")
        if count not in (3,5,8,10): count=5
        if seconds not in (10,15,20,30): seconds=20
        if model not in ("tiny","base"): model="tiny"
        with LOCK: JOBS[jid]={"status":"queued","progress":0,"message":"काम शुरू हो रहा है…"}
        threading.Thread(target=run_job,args=(jid,source,folder,count,seconds,model),daemon=True).start()
        return jsonify(job_id=jid)
    except Exception as e:
        shutil.rmtree(folder,ignore_errors=True); return jsonify(error=str(e)),400

@app.get("/api/jobs/<jid>")
def get_status(jid):
    with LOCK:
        j=JOBS.get(jid)
        if not j: return jsonify(error="Job नहीं मिला। सर्वर restart हुआ हो सकता है।"),404
        return jsonify(j)

@app.get("/api/jobs/<jid>/download")
def download(jid):
    f=WORK/jid/"shorts.zip"
    with LOCK: j=JOBS.get(jid)
    if not j or j.get("status")!="done" or not f.exists(): return jsonify(error="डाउनलोड अभी तैयार नहीं है।"),404
    return send_file(f,as_attachment=True,download_name="ai_shorts.zip")

@app.errorhandler(413)
def too_large(_): return jsonify(error=f"वीडियो {MAX_MB} MB से बड़ा है। छोटी फ़ाइल चुनें।"),413

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.getenv("PORT","5000")))
