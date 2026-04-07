#!/usr/bin/env python3
"""Whisper Transcritor — FastAPI + HTML frontend"""
from __future__ import annotations
import os, json, shutil, threading, uuid, datetime, re, zipfile
import whisper
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
import uvicorn
import tqdm

# ── Paths ──────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(SCRIPT_DIR, ".whisper_data")
RESULTS_DIR  = os.path.join(DATA_DIR, "results")
UPLOAD_DIR   = os.path.join(DATA_DIR, "uploads")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HTML_FILE    = os.path.join(SCRIPT_DIR, "index.html")

for d in (RESULTS_DIR, UPLOAD_DIR):
    os.makedirs(d, exist_ok=True)

# ── Model cache ────────────────────────────────────────────────
_models: dict = {}
_models_lock  = threading.Lock()
_transcribe_sem = threading.Semaphore(1)  # apenas 1 transcrição por vez
_history_lock = threading.Lock()           # protege escrita no history.json

_thread_local = threading.local()

_orig_tqdm_init = tqdm.tqdm.__init__
_orig_tqdm_update = tqdm.tqdm.update

def _custom_tqdm_init(self, *args, **kwargs):
    _orig_tqdm_init(self, *args, **kwargs)
    self._task_id = getattr(_thread_local, 'task_id', None)

def _custom_tqdm_update(self, n=1):
    _orig_tqdm_update(self, n)
    if hasattr(self, '_task_id') and self._task_id:
        if self.total and self.total > 0:
            pct = 25 + (self.n / self.total) * 57
            _set_task(self._task_id, progress=pct)

tqdm.tqdm.__init__ = _custom_tqdm_init
tqdm.tqdm.update = _custom_tqdm_update

def _load_model(name: str):
    with _models_lock:
        if name not in _models:
            _models[name] = whisper.load_model(name)
        return _models[name]

# ── Helpers ────────────────────────────────────────────────────
def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s   = divmod(rem, 60)
    if h:  return f"{h}h {m}m {s}s"
    if m:  return f"{m}m {s}s"
    return f"{s}s"

_FILLER_RE = re.compile(
    r'\b(né+|não é|então né|tipo assim|sabe|é+\.{2,}|ã+\.{2,}|hm+|ah+|eh+|uh+|mm+|tá bom)\b',
    re.IGNORECASE
)

def _apply_filler_filter(text: str) -> str:
    text = _FILLER_RE.sub("", text)
    return re.sub(r"  +", " ", text).strip()

def _fmt_ts(t: float, srt: bool = False) -> str:
    h, rem = divmod(int(t), 3600)
    m, s   = divmod(rem, 60)
    ms     = int((t - int(t)) * 1000)
    sep    = "," if srt else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"

# ── Transcription core ─────────────────────────────────────────
def _transcribe_one(path: str, model, language: str, task_type: str) -> dict:
    lang   = None if language == "auto" else language
    result = model.transcribe(path, language=lang, task=task_type, verbose=False)

    text     = result["text"].strip()
    segments = result["segments"]
    ts_lines, srt_parts = [], []

    for i, seg in enumerate(segments):
        s, e = seg["start"], seg["end"]
        t    = seg["text"].strip()
        ts_lines.append(f"[{_fmt_ts(s)}] {t}")
        srt_parts.append(f"{i+1}\n{_fmt_ts(s,True)} --> {_fmt_ts(e,True)}\n{t}\n")

    dur_secs = segments[-1]["end"] if segments else 0

    return {
        "text":         text,
        "timestamped":  "\n".join(ts_lines),
        "srt":          "\n".join(srt_parts),
        "json_data":    {"text": text, "segments": segments,
                         "language": result.get("language", language)},
        "duration":     _fmt_duration(dur_secs),
        "duration_secs": dur_secs,
        "words":        len(text.split()),
        "segments":     len(segments),
        "lang":         result.get("language", language or "?"),
    }

# ── File persistence ───────────────────────────────────────────
def _result_base(filename: str):
    return os.path.splitext(filename)[0]

def _result_dir(filename: str) -> str:
    d = os.path.join(RESULTS_DIR, _result_base(filename))
    os.makedirs(d, exist_ok=True)
    return d

def _save_result_files(filename: str, result: dict):
    base = _result_base(filename)
    d    = _result_dir(filename)
    for fname, content in [
        (f"{base}.txt",            result["text"]),
        (f"{base}_timestamps.txt", result["timestamped"]),
        (f"{base}.srt",            result["srt"]),
    ]:
        with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
            f.write(content)
    with open(os.path.join(d, f"{base}.json"), "w", encoding="utf-8") as f:
        json.dump(result["json_data"], f, ensure_ascii=False, indent=2)

def _load_result_files(filename: str) -> dict | None:
    base = _result_base(filename)
    d    = os.path.join(RESULTS_DIR, base)
    if not os.path.isdir(d):
        return None
    out = {}
    for key, fname in [("text", f"{base}.txt"),
                        ("timestamped", f"{base}_timestamps.txt"),
                        ("srt", f"{base}.srt")]:
        p = os.path.join(d, fname)
        out[key] = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    return out

# ── History ────────────────────────────────────────────────────
def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []

def _save_to_history(filename: str, result: dict, model_name: str,
                     status: str = "done", error: str | None = None,
                     task_id: str | None = None):
    history = _load_history()
    # Preserve original date if entry already exists
    existing = next((h for h in history if h.get("file") == filename), {})
    entry = {
        "id":           filename,
        "file":         filename,
        "name":         _result_base(filename),
        "lang":         result.get("lang", "?") if result else "?",
        "duration":     result.get("duration", "?") if result else "—",
        "duration_secs":result.get("duration_secs", 0) if result else 0,
        "words":        result.get("words", 0) if result else 0,
        "segments":     result.get("segments", 0) if result else 0,
        "mode":         model_name,
        "status":       status,
        "error":        error,
        "task_id":      task_id,
        "date":         existing.get("date") or datetime.datetime.now().strftime("%d de %b. de %Y, %H:%M"),
    }
    history = [h for h in history if h.get("file") != filename]
    history.insert(0, entry)
    with _history_lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

def _update_history_status(filename: str, status: str,
                           error: str | None = None, **extra):
    """Patch an existing history entry in-place (no re-insert)."""
    history = _load_history()
    for entry in history:
        if entry.get("file") == filename:
            entry["status"] = status
            if error is not None:
                entry["error"] = error
            entry.update(extra)
            break
    with _history_lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

# ── Task tracking ──────────────────────────────────────────────
_tasks:      dict = {}
_tasks_lock        = threading.Lock()

def _set_task(task_id: str, **kw):
    with _tasks_lock:
        _tasks.setdefault(task_id, {}).update(kw)

def _get_task(task_id: str) -> dict | None:
    with _tasks_lock:
        return dict(_tasks.get(task_id, {}))

def _run_transcription(task_id, file_path, filename, model_name,
                       language, task_type, do_filter):
    _thread_local.task_id = task_id
    with _transcribe_sem:
        try:
            _set_task(task_id, status="processing", progress=10)
            _update_history_status(filename, "processing", task_id=task_id)

            model = _load_model(model_name)
            _set_task(task_id, progress=25)

            result = _transcribe_one(file_path, model, language, task_type)
            _set_task(task_id, progress=82)

            if do_filter:
                result["text"]        = _apply_filler_filter(result["text"])
                result["timestamped"] = "\n".join(
                    _apply_filler_filter(l) for l in result["timestamped"].split("\n"))

            _save_result_files(filename, result)
            _save_to_history(filename, result, model_name,
                             status="done", task_id=task_id)

            _set_task(task_id, status="done", progress=100,
                      filename=filename, lang=result["lang"],
                      duration=result["duration"], words=result["words"])

        except Exception as exc:
            error_msg = str(exc)
            _set_task(task_id, status="error", progress=0, error=error_msg)
            _update_history_status(filename, "error", error=error_msg)
        finally:
            if os.path.exists(file_path):
                os.remove(file_path)

# ── FastAPI ────────────────────────────────────────────────────
app = FastAPI(title="Whisper Transcritor")

@app.get("/", response_class=HTMLResponse)
async def serve_html():
    with open(HTML_FILE, encoding="utf-8") as f:
        return f.read()

# -- History & stats
@app.get("/api/history")
async def api_history():
    return _load_history()

@app.get("/api/stats")
async def api_stats():
    history = _load_history()
    total_secs = sum(h.get("duration_secs", 0) for h in history)
    h, rem = divmod(int(total_secs), 3600)
    m      = rem // 60
    return {
        "total":    len(history),
        "duration": f"{h}h {m}m" if h else (f"{m}m" if m else "0m"),
    }

# -- Transcription
@app.post("/api/transcribe")
async def api_transcribe(
    background_tasks: BackgroundTasks,
    file:            UploadFile = File(...),
    model:           str        = Form("turbo"),
    language:        str        = Form("pt"),
    task:            str        = Form("transcribe"),
    filter_fillers:  str        = Form("false"),
):
    filename = file.filename or f"audio_{uuid.uuid4().hex[:8]}.mp3"
    task_id  = str(uuid.uuid4())

    upload_path = os.path.join(UPLOAD_DIR, f"{task_id}_{filename}")
    with open(upload_path, "wb") as f:
        f.write(await file.read())

    _set_task(task_id, status="queued", progress=0,
              name=filename, filename=filename)

    # Register immediately in history so it survives refresh
    _save_to_history(filename, {}, model, status="queued", task_id=task_id)

    t = threading.Thread(
        target=_run_transcription,
        args=(task_id, upload_path, filename, model,
              language, task, filter_fillers == "true"),
        daemon=True,
    )
    t.start()
    return {"task_id": task_id, "filename": filename}

@app.get("/api/progress/{task_id}")
async def api_progress(task_id: str):
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task

@app.get("/api/active-tasks")
async def api_active_tasks():
    """Return all in-memory tasks that are still queued or processing."""
    with _tasks_lock:
        return {
            tid: dict(t)
            for tid, t in _tasks.items()
            if t.get("status") in ("queued", "processing")
        }

@app.post("/api/reset-stale")
async def api_reset_stale():
    """Mark queued/processing history entries whose task is no longer in memory as interrupted.
    Called by the frontend on page load after a server restart."""
    history = _load_history()
    changed = 0
    with _tasks_lock:
        active_ids = set(_tasks.keys())
    for entry in history:
        if entry.get("status") in ("queued", "processing"):
            tid = entry.get("task_id")
            if not tid or tid not in active_ids:
                entry["status"] = "error"
                entry["error"]  = (
                    "Transcrição interrompida — o servidor foi reiniciado "
                    "durante o processamento. Envie o arquivo novamente."
                )
                changed += 1
    if changed:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    return {"reset": changed}

# -- Results
@app.get("/api/result/{filename}")
async def api_result(filename: str):
    result = _load_result_files(filename)
    if result is None:
        raise HTTPException(404, "Resultado não encontrado")
    return result

# -- Downloads
@app.get("/api/download/{filename}/{fmt}")
async def api_download(filename: str, fmt: str):
    base = _result_base(filename)
    d    = os.path.join(RESULTS_DIR, base)
    MAP  = {
        "txt":        (f"{base}.txt",            "text/plain"),
        "srt":        (f"{base}.srt",            "text/plain"),
        "json":       (f"{base}.json",           "application/json"),
        "timestamps": (f"{base}_timestamps.txt", "text/plain"),
    }
    if fmt not in MAP:
        raise HTTPException(400, "Formato inválido")
    fname, media = MAP[fmt]
    path = os.path.join(d, fname)
    if not os.path.exists(path):
        raise HTTPException(404, "Arquivo não encontrado")
    return FileResponse(path, media_type=media, filename=fname)

@app.get("/api/download-all")
async def api_download_all():
    zip_path = os.path.join(DATA_DIR, "todas_transcricoes.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in os.listdir(RESULTS_DIR):
            full_d = os.path.join(RESULTS_DIR, d)
            if os.path.isdir(full_d):
                for fname in os.listdir(full_d):
                    zf.write(os.path.join(full_d, fname), os.path.join(d, fname))
    return FileResponse(zip_path, filename="todas_transcricoes.zip")

# -- Delete
@app.delete("/api/delete/{filename}")
async def api_delete(filename: str):
    history = [h for h in _load_history() if h.get("file") != filename]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    d = os.path.join(RESULTS_DIR, _result_base(filename))
    if os.path.isdir(d):
        shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/gaps/{filename}")
async def api_gaps(filename: str, min_gap: float = 1.0):
    """Detecta silêncios/respiros entre segmentos de fala e gera texto intercalado."""
    base = _result_base(filename)
    json_path = os.path.join(RESULTS_DIR, base, f"{base}.json")
    if not os.path.exists(json_path):
        raise HTTPException(404, "Resultado não encontrado")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments", [])
    gaps = []
    
    full_text_lines = []
    
    for i, seg in enumerate(segments):
        start_fmt = _fmt_ts(seg["start"])
        end_fmt = _fmt_ts(seg["end"])
        
        # Checa respiro antes de adicionar o segmento atual
        if i > 0:
            prev_end = segments[i-1]["end"]
            curr_start = seg["start"]
            duration = round(curr_start - prev_end, 2)
            if duration >= min_gap:
                gaps.append({
                    "index": i,
                    "start": round(prev_end, 2),
                    "end": round(curr_start, 2),
                    "duration": duration,
                    "start_fmt": _fmt_ts(prev_end),
                    "end_fmt": _fmt_ts(curr_start),
                    "before": segments[i-1]["text"].strip()[-60:],
                    "after": segments[i]["text"].strip()[:60],
                })
                # Insere linha de respiro
                full_text_lines.append(f"{_fmt_ts(prev_end)} → {_fmt_ts(curr_start)}【silêncio {duration}s】")
        
        # Insere linha de fala
        full_text_lines.append(f"{start_fmt} → {end_fmt}\n{seg['text'].strip()}")

    out_text = "\n\n".join(full_text_lines)
    
    return {
        "filename": filename, 
        "total_gaps": len(gaps), 
        "min_gap": min_gap, 
        "gaps": gaps,
        "full_text": out_text
    }

# ── Entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    print("✅  Whisper Transcritor → http://127.0.0.1:7860")
    uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
