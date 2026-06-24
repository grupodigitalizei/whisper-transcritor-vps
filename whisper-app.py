#!/usr/bin/env python3
"""Whisper Transcritor — FastAPI + HTML frontend"""
from __future__ import annotations
import os, json, shutil, threading, uuid, datetime, re, zipfile, time
import whisper
try:
    import yt_dlp
    YT_DLP_OK = True
except ImportError:
    YT_DLP_OK = False
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import tqdm

# ── Paths ──────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR     = os.path.join(SCRIPT_DIR, ".whisper_data")
RESULTS_DIR  = os.path.join(DATA_DIR, "results")
UPLOAD_DIR   = os.path.join(DATA_DIR, "uploads")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MEDIA_FILE   = os.path.join(DATA_DIR, "media.json")
FOLDERS_FILE = os.path.join(DATA_DIR, "folders.json")
HTML_FILE    = os.path.join(SCRIPT_DIR, "index.html")
STATIC_DIR   = os.path.join(SCRIPT_DIR, "static")

for d in (RESULTS_DIR, UPLOAD_DIR, STATIC_DIR):
    os.makedirs(d, exist_ok=True)

# ── Model cache ────────────────────────────────────────────────
_models: dict = {}
_models_lock  = threading.Lock()
_history_lock = threading.Lock()           # protege escrita no history.json
_media_lock   = threading.Lock()           # protege escrita no media.json
_folders_lock = threading.Lock()           # protege escrita no folders.json
_settings_lock = threading.Lock()          # protege escrita no settings.json

# ── User-configurable concurrency settings ─────────────────────
SETTINGS_FILE = os.path.join(SCRIPT_DIR, ".whisper_data", "settings.json")
_DEFAULT_SETTINGS = {
    "download_concurrent":   3,   # quantos yt-dlp em paralelo
    "transcribe_concurrent": 1,   # quantas chamadas Whisper em paralelo
}
_SETTINGS_CACHE: dict = {}

def _load_settings() -> dict:
    """Returns the merged (defaults + persisted) settings dict.
    Reads from disk once and caches; settings changes invalidate via _save_settings."""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE:
        return _SETTINGS_CACHE
    merged = dict(_DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            stored = json.load(f) or {}
        for k, default in _DEFAULT_SETTINGS.items():
            v = stored.get(k)
            if isinstance(v, int) and v >= 1:
                merged[k] = v
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    _SETTINGS_CACHE = merged
    return merged

def _save_settings(new: dict) -> dict:
    """Validates + persists settings. Clamps each value to [1, 16] to avoid
    accidental hangs from a 0 or surprise CPU storms from a 100."""
    global _SETTINGS_CACHE
    with _settings_lock:
        current = _load_settings()
        out = dict(current)
        for k in _DEFAULT_SETTINGS.keys():
            if k in new:
                try:
                    v = int(new[k])
                except (TypeError, ValueError):
                    continue
                out[k] = max(1, min(16, v))
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        _SETTINGS_CACHE = out
    return out

class _DynamicSem:
    """Semaphore-like context manager whose limit is read live from settings
    every time someone tries to enter. Lets the user change concurrency from
    the UI without restarting the server — in-flight work finishes naturally;
    new work respects the new ceiling."""
    def __init__(self, setting_key: str):
        self._setting_key = setting_key
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._count = 0

    def _max(self) -> int:
        return _load_settings().get(self._setting_key, 1)

    def __enter__(self):
        with self._cond:
            while self._count >= self._max():
                self._cond.wait()
            self._count += 1

    def __exit__(self, *exc):
        with self._cond:
            self._count -= 1
            self._cond.notify_all()

_transcribe_sem = _DynamicSem("transcribe_concurrent")  # was Semaphore(1) — now user-tunable
_download_sem   = _DynamicSem("download_concurrent")    # NEW — gate yt-dlp jobs

_thread_local = threading.local()

class _UserCancelled(Exception):
    """Raised inside yt-dlp progress hooks to abort an in-flight download.
    Distinct exception type so generic try/except blocks don't swallow it
    silently — we want it to propagate to the runner, which marks the task
    as 'cancelled' instead of 'error'."""
    pass

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
            # Whisper may create multiple tqdm instances per transcription
            # (VAD pass, decode loop, etc.). Each new one starts at n=0, which
            # would *decrease* the reported progress. Clamp to monotonically
            # increasing values within the 25–82 band.
            current = (_get_task(self._task_id) or {}).get('progress', 0) or 0
            updates = {}
            if pct > current:
                updates['progress'] = pct
            # phase_progress is the live 0–100 of the CURRENT phase (transcription);
            # also clamped monotonically so multiple tqdm instances don't reset it.
            phase_pct = (self.n / self.total) * 100
            cur_phase = (_get_task(self._task_id) or {}).get('phase_progress', 0) or 0
            if phase_pct > cur_phase:
                updates['phase_progress'] = phase_pct
            if updates:
                _set_task(self._task_id, **updates)

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

def _original_name_for(filename: str) -> str:
    """Return the user-facing name (without extension) for a stored filename.
    Looks up history first (name stored without ext), then media.json
    (name stored with ext — strip it), then falls back to the internal base."""
    try:
        for entry in _load_history():
            if entry.get("file") == filename and entry.get("name"):
                return entry["name"]
        for entry in _load_media():
            if entry.get("file") == filename and entry.get("name"):
                return os.path.splitext(entry["name"])[0]
    except Exception:
        pass
    return _result_base(filename)

def _original_media_name_for(filename: str) -> str:
    """Return the media's original full filename (with extension) for download."""
    ext = os.path.splitext(filename)[1]
    try:
        # History is the most reliable source (name stored as original basename without ext)
        for entry in _load_history():
            if entry.get("file") == filename and entry.get("name"):
                name = entry["name"]
                return name if os.path.splitext(name)[1] else name + ext
        for entry in _load_media():
            if entry.get("file") == filename and entry.get("name"):
                name = entry["name"]
                return name if os.path.splitext(name)[1] else name + ext
    except Exception:
        pass
    return filename

def _safe_filename(filename: str) -> str:
    """Reject filenames that try to escape their directory (path traversal).
    Allows only a basename — no separators, no '..', no absolute paths.
    Raises HTTPException(400) on any suspicious input."""
    if not filename or not filename.strip():
        raise HTTPException(400, "Filename inválido (vazio)")
    # Reject any path separator or parent traversal
    if "/" in filename or "\\" in filename or "\x00" in filename:
        raise HTTPException(400, "Filename inválido (separadores não permitidos)")
    if filename in (".", "..") or filename.startswith(".."):
        raise HTTPException(400, "Filename inválido")
    # basename should equal original (catches anything os.path.basename would strip)
    if os.path.basename(filename) != filename:
        raise HTTPException(400, "Filename inválido")
    return filename

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
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                out[key] = fh.read()
        else:
            out[key] = ""
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
                     task_id: str | None = None, original_name: str | None = None,
                     folder: str | None = None, source: str | None = None):
    # Atomic read-modify-write under a single lock to prevent lost updates
    with _history_lock:
        history = _load_history()
        # Preserve original date, folder, and timing fields if entry already exists
        existing = next((h for h in history if h.get("file") == filename), {})
        # New 'folder' / 'source' args win on first insert; otherwise preserve.
        folder_to_use = folder if folder is not None else existing.get("folder", "")
        source_to_use = source if source is not None else existing.get("source")
        name_to_use = original_name or existing.get("name") or _result_base(filename)
        entry = {
            "id":           filename,
            "file":         filename,
            "name":         name_to_use,
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
            # Sortable timestamp + folder, both preserved across updates
            "queued_at":    existing.get("queued_at") or time.time(),
            "folder":       folder_to_use,
            "source":       source_to_use,
            # Timing fields (filled during transcription by _update_history_status)
            "started_at":   existing.get("started_at"),
            "completed_at": existing.get("completed_at"),
            "processing_secs": existing.get("processing_secs"),
        }
        history = [h for h in history if h.get("file") != filename]
        history.insert(0, entry)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

# ── Media Tracking ─────────────────────────────────────────────
def _load_media() -> list:
    if not os.path.exists(MEDIA_FILE):
        return []
    try:
        with open(MEDIA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []

def _save_media(filename: str, original_name: str, url: str | None = None, is_transcribed: bool = False, status: str = "done"):
    # Atomic read-modify-write under a single lock to prevent lost updates
    with _media_lock:
        media = _load_media()
        existing = next((m for m in media if m.get("file") == filename), {})

        path = os.path.join(UPLOAD_DIR, filename)
        size_bytes = os.path.getsize(path) if os.path.exists(path) else 0

        # Preserve the name stored on first save (the true original filename).
        # Later calls from _run_transcription pass the hashed base, which would
        # otherwise overwrite the good name.
        name_to_use = existing.get("name") or original_name
        entry = {
            "id": filename,
            "file": filename,
            "name": name_to_use,
            "url": url or existing.get("url"),
            "size_bytes": size_bytes,
            "is_transcribed": is_transcribed or existing.get("is_transcribed", False),
            "status": status,
            "date": existing.get("date") or datetime.datetime.now().strftime("%d de %b. de %Y, %H:%M"),
            "queued_at": existing.get("queued_at") or time.time(),
            "folder": existing.get("folder", ""),
        }

        media = [m for m in media if m.get("file") != filename]
        media.insert(0, entry)

        with open(MEDIA_FILE, "w", encoding="utf-8") as f:
            json.dump(media, f, ensure_ascii=False, indent=2)

def _update_history_status(filename: str, status: str,
                           error: str | None = None, **extra):
    """Patch an existing history entry in-place (no re-insert).
    Atomic read-modify-write under a single lock to prevent lost updates."""
    with _history_lock:
        history = _load_history()
        for entry in history:
            if entry.get("file") == filename:
                entry["status"] = status
                if error is not None:
                    entry["error"] = error
                entry.update(extra)
                break
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
        # Early-out for tasks cancelled while still queued. The actual transcription
        # below cannot be interrupted (it's a single blocking call into Whisper), but
        # queued tasks can be skipped entirely.
        if _get_task(task_id) and _get_task(task_id).get("cancel_requested"):
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled")
            return
        started_at = time.time()
        try:
            # Transition into the transcribe phase. phase_progress=0 here so the UI
            # can show a fresh "Transcrevendo 0%" right after the download phase ended.
            _set_task(task_id, status="processing", progress=10,
                      phase="transcribe", phase_progress=0, started_at=started_at)
            _update_history_status(filename, "processing", task_id=task_id,
                                   started_at=started_at)

            model = _load_model(model_name)
            _set_task(task_id, progress=25)

            result = _transcribe_one(file_path, model, language, task_type)
            _set_task(task_id, progress=82, phase_progress=100)

            # If user requested cancel while the (uninterruptible) transcription
            # was running, discard the result rather than committing it.
            if _get_task(task_id).get("cancel_requested"):
                _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                          completed_at=time.time())
                _update_history_status(filename, "cancelled",
                                       completed_at=time.time())
                return

            # Saving phase — brief but distinct from transcription
            _set_task(task_id, phase="saving", phase_progress=0)
            if do_filter:
                result["text"]        = _apply_filler_filter(result["text"])
                result["timestamped"] = "\n".join(
                    _apply_filler_filter(l) for l in result["timestamped"].split("\n"))

            _save_result_files(filename, result)
            completed_at   = time.time()
            processing_secs = round(completed_at - started_at, 2)
            _save_to_history(filename, result, model_name,
                             status="done", task_id=task_id)
            # Patch timing into the just-saved "done" entry
            _update_history_status(filename, "done",
                                   started_at=started_at,
                                   completed_at=completed_at,
                                   processing_secs=processing_secs)

            _set_task(task_id, status="done", progress=100, phase="done", phase_progress=100,
                      filename=filename, lang=result["lang"],
                      duration=result["duration"], words=result["words"],
                      completed_at=completed_at, processing_secs=processing_secs)
            _save_media(filename, _result_base(filename), is_transcribed=True, status="done")

        except Exception as exc:
            error_msg = str(exc)
            completed_at = time.time()
            processing_secs = round(completed_at - started_at, 2)
            _set_task(task_id, status="error", progress=0, phase="error", error=error_msg,
                      completed_at=completed_at, processing_secs=processing_secs)
            _update_history_status(filename, "error", error=error_msg,
                                   completed_at=completed_at,
                                   processing_secs=processing_secs)
            _save_media(filename, _result_base(filename), is_transcribed=False, status="error")

# ── FastAPI ────────────────────────────────────────────────────
app = FastAPI(title="Whisper Transcritor")

# Serve CSS / JS / fonts locally so the UI works offline and avoids CDN dependency.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_html():
    with open(HTML_FILE, encoding="utf-8") as f:
        html = f.read()
    # Cache-busting: append ?v=<mtime> to local CSS/JS so the browser always
    # fetches the latest version after a file change (no manual hard-reload).
    def _ver(rel_path: str) -> str:
        try:
            return str(int(os.path.getmtime(os.path.join(SCRIPT_DIR, rel_path))))
        except OSError:
            return "0"
    html = html.replace('href="/static/style.css"',
                        f'href="/static/style.css?v={_ver("static/style.css")}"')
    html = html.replace('src="/static/app.js"',
                        f'src="/static/app.js?v={_ver("static/app.js")}"')
    return html

# -- History & stats
@app.get("/api/history")
async def api_history():
    """Returns the history with two computed fields injected per entry:
       - has_original: True if the original audio/video upload is still on disk.
                       Lets the UI badge each row as 'Original disponível' vs
                       'Original apagado' (e.g. after the 7-day cleanup ran).
       - source: how the entry first entered the system. Legacy rows without
                 a stored source default to 'upload' as a best-guess fallback
                 so the UI doesn't render a blank chip for them."""
    history = _load_history()
    # Single fast listdir + set membership beats one stat per entry on big histories
    try:
        on_disk = set(os.listdir(UPLOAD_DIR))
    except OSError:
        on_disk = set()
    # Build a {file -> has_url} index from media.json so legacy entries
    # (no stored source) can be classified by whether they had a yt-dlp URL.
    media_url_map = {m.get("file"): bool(m.get("url")) for m in _load_media()}
    out = []
    for h in history:
        entry = dict(h)
        entry["has_original"] = entry.get("file") in on_disk
        if not entry.get("source"):
            entry["source"] = "url" if media_url_map.get(entry.get("file")) else "upload"
        out.append(entry)
    return out

@app.get("/api/settings")
async def api_get_settings():
    """Returns current concurrency settings."""
    return _load_settings()

@app.post("/api/settings")
async def api_set_settings(
    download_concurrent:   str = Form(None),
    transcribe_concurrent: str = Form(None),
):
    """Updates concurrency settings. Values are clamped to [1, 16].
    Changes take effect on the NEXT acquire of each semaphore — in-flight
    work isn't interrupted but new work respects the new limit."""
    new = {}
    if download_concurrent   is not None: new["download_concurrent"]   = download_concurrent
    if transcribe_concurrent is not None: new["transcribe_concurrent"] = transcribe_concurrent
    if not new:
        raise HTTPException(400, "Nenhuma configuração informada")
    return _save_settings(new)

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

# -- Media
@app.get("/api/media-history")
async def api_media_history():
    return _load_media()

@app.get("/api/download-media/{filename}")
async def api_download_media(filename: str):
    filename = _safe_filename(filename)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Mídia não encontrada no servidor")
    return FileResponse(path, filename=_original_media_name_for(filename))

@app.delete("/api/delete-media/{filename}")
async def api_delete_media(filename: str):
    filename = _safe_filename(filename)
    path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            raise HTTPException(500, f"Erro ao deletar: {e}")

    with _media_lock:
        media = _load_media()
        media = [m for m in media if m.get("file") != filename]
        with open(MEDIA_FILE, "w", encoding="utf-8") as f:
            json.dump(media, f, ensure_ascii=False, indent=2)
    return {"status": "ok"}


# ── Cleanup of old media files (retention policy) ─────────────
#
# Policy: the audio/video originals in .whisper_data/uploads/ are kept on disk
# so the user can re-download or re-transcribe. After 7 days they are flagged
# for cleanup. The user may ignore the warning or confirm the deletion — it is
# never automatic. **Transcriptions are NEVER touched by this** — they live in
# results/ and history.json and remain available even after the media is gone.

def _audit_old_media(min_age_days: float = 7.0) -> dict:
    """Return all media uploads older than `min_age_days`, plus totals."""
    cutoff_secs = min_age_days * 86400
    now = time.time()
    items: list[dict] = []
    total_bytes = 0
    for entry in _load_media():
        filename = entry.get("file") or ""
        if not filename:
            continue
        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path):
            continue  # file already gone (no point listing it for cleanup)
        # Prefer queued_at (when first ingested); fall back to file mtime.
        ts = entry.get("queued_at") or os.path.getmtime(path)
        age = now - ts
        if age < cutoff_secs:
            continue
        size = os.path.getsize(path)
        total_bytes += size
        items.append({
            "file":            filename,
            "name":            entry.get("name") or filename,
            "size_bytes":      size,
            "age_days":        round(age / 86400, 1),
            "is_transcribed":  bool(entry.get("is_transcribed")),
            "date":            entry.get("date") or "",
            "url":             entry.get("url"),
        })
    return {
        "count":       len(items),
        "total_bytes": total_bytes,
        "min_age_days": min_age_days,
        "items":       items,
    }

@app.get("/api/media/older-than")
async def api_media_older_than(days: float = 7.0):
    """List media files older than N days (default 7) so the UI can prompt
    cleanup. Transcriptions are NEVER included — only the audio/video originals
    in uploads/ that have aged past the retention window."""
    days = max(0.0, float(days))
    return _audit_old_media(days)

@app.post("/api/media/cleanup")
async def api_media_cleanup(files: str = Form(...)):
    """Bulk-delete a list of media uploads (audio/video originals only).
    `files` is a comma-separated list of safe filenames. Transcriptions linked
    to these files are preserved — they keep working from results/."""
    requested = [_safe_filename(f.strip()) for f in (files or "").split(",") if f.strip()]
    if not requested:
        raise HTTPException(400, "Nenhum arquivo informado")

    deleted = 0
    freed_bytes = 0
    failed: list[str] = []
    for filename in requested:
        path = os.path.join(UPLOAD_DIR, filename)
        # Path-traversal guard: ensure final path is still inside UPLOAD_DIR
        if os.path.commonpath([os.path.realpath(path), os.path.realpath(UPLOAD_DIR)]) != os.path.realpath(UPLOAD_DIR):
            failed.append(filename); continue
        if os.path.exists(path):
            try:
                freed_bytes += os.path.getsize(path)
                os.remove(path)
                deleted += 1
            except OSError:
                failed.append(filename)

    # Drop those entries from media.json (transcriptions stay in history.json untouched)
    with _media_lock:
        media = [m for m in _load_media() if m.get("file") not in set(requested)]
        with open(MEDIA_FILE, "w", encoding="utf-8") as f:
            json.dump(media, f, ensure_ascii=False, indent=2)

    return {"deleted": deleted, "freed_bytes": freed_bytes, "failed": failed}


# -- Transcription
@app.post("/api/transcribe")
async def api_transcribe(
    background_tasks: BackgroundTasks,
    file:            UploadFile = File(...),
    model:           str        = Form("turbo"),
    language:        str        = Form("pt"),
    task:            str        = Form("transcribe"),
    filter_fillers:  str        = Form("false"),
    folder:          str        = Form(""),
):
    original_name = file.filename or f"audio_{uuid.uuid4().hex[:8]}.mp3"
    task_id  = str(uuid.uuid4())
    filename = f"{task_id[:8]}_{original_name}"

    # Validate/normalize destination folder (auto-create ancestors so the tree shows it)
    folder = _validate_folder_name(folder) if folder else ""
    if folder:
        with _folders_lock:
            paths = set(_load_folders_paths())
            for a in _ancestors_of(folder):
                paths.add(a)
            _save_folders_paths(sorted(paths))

    upload_path = os.path.join(UPLOAD_DIR, filename)
    with open(upload_path, "wb") as f:
        f.write(await file.read())

    _set_task(task_id, status="queued", progress=0,
              name=original_name, filename=filename)

    # Register immediately in history so it survives refresh
    _save_to_history(filename, {}, model, status="queued", task_id=task_id, original_name=_result_base(original_name), folder=folder, source="upload")
    _save_media(filename, original_name, is_transcribed=True, status="queued")

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
    # task_id is a UUID; reject anything else to prevent abuse
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task

@app.delete("/api/transcribe/{task_id}")
async def api_cancel_transcribe(task_id: str):
    """Request cancellation of any task (download-only, URL→transcribe, or
    file→transcribe). Queued tasks are skipped immediately. In-flight downloads
    abort at the next yt-dlp progress tick. In-flight transcriptions finish the
    current Whisper call (uninterruptible) but discard the result."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _set_task(task_id, cancel_requested=True)
    # Mark BOTH catalogs so the UI reflects the cancel instantly, regardless of
    # which tab the user is looking at (transcriptions / media library).
    filename = task.get("filename")
    if filename:
        _update_history_status(filename, "cancelled")
        with _media_lock:
            media = _load_media()
            for entry in media:
                if entry.get("file") == filename:
                    entry["status"] = "cancelled"
                    break
            with open(MEDIA_FILE, "w", encoding="utf-8") as f:
                json.dump(media, f, ensure_ascii=False, indent=2)
    return {"status": "cancel_requested", "task_id": task_id}

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
    with _tasks_lock:
        active_ids = set(_tasks.keys())
    with _history_lock:
        history = _load_history()
        changed = 0
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
    filename = _safe_filename(filename)
    result = _load_result_files(filename)
    if result is None:
        raise HTTPException(404, "Resultado não encontrado")
    return result

# -- Downloads
@app.get("/api/download/{filename}/{fmt}")
async def api_download(filename: str, fmt: str):
    filename = _safe_filename(filename)
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
    original = _original_name_for(filename)
    ext_map = {"txt": ".txt", "srt": ".srt", "json": ".json", "timestamps": "_timestamps.txt"}
    download_name = f"{original}{ext_map[fmt]}"
    return FileResponse(path, media_type=media, filename=download_name)

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

@app.post("/api/download-selected-zip")
async def api_download_selected_zip(files: str = Form(...), formats: str = Form("txt,srt,json,timestamps")):
    """Download only the selected transcriptions as a ZIP.
    `files` is a JSON array of filenames (history ids) to include.
    `formats` is a comma-separated list of formats to include:
    any subset of {txt, srt, json, timestamps}."""
    try:
        filenames = json.loads(files)
        if not isinstance(filenames, list):
            raise ValueError("files deve ser uma lista")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Parâmetro 'files' inválido: {e}")

    if not filenames:
        raise HTTPException(400, "Nenhum arquivo selecionado")

    # Parse format whitelist
    valid_formats = {"txt", "srt", "json", "timestamps"}
    chosen = {f.strip().lower() for f in formats.split(",") if f.strip()}
    chosen &= valid_formats
    if not chosen:
        raise HTTPException(400, "Selecione ao menos um formato (txt, srt, json, timestamps)")

    # Map each format to the suffix that appears after the internal base filename
    suffix_for = {
        "txt":        ".txt",
        "srt":        ".srt",
        "json":       ".json",
        "timestamps": "_timestamps.txt",
    }
    allowed_suffixes = {suffix_for[f] for f in chosen}

    # Validate each filename (prevents path traversal) and collect existing dirs
    results_real = os.path.realpath(RESULTS_DIR)
    entries = []
    for fn in filenames:
        if not isinstance(fn, str):
            raise HTTPException(400, "Cada item de 'files' deve ser string")
        fn = _safe_filename(fn)
        base = _result_base(fn)
        d = os.path.join(RESULTS_DIR, base)
        # Defense-in-depth: ensure path stays inside RESULTS_DIR
        if os.path.commonpath([os.path.realpath(d), results_real]) != results_real:
            continue
        if os.path.isdir(d):
            entries.append((base, d, _original_name_for(fn)))

    if not entries:
        raise HTTPException(404, "Nenhum resultado encontrado para os arquivos selecionados")

    # Deduplicate on the *final flat filename* (name+suffix), since the ZIP
    # has no folders. Two entries sharing a display name only collide for
    # suffixes that both produce — e.g. both end up wanting "aula.txt".
    used_flat_names: set[str] = set()

    def _unique_name(display: str, suffix: str) -> str:
        candidate = display + suffix
        if candidate not in used_flat_names:
            used_flat_names.add(candidate)
            return candidate
        # Keep the extension intact; insert " (N)" before it
        stem, ext = os.path.splitext(candidate)
        n = 2
        while True:
            cand = f"{stem} ({n}){ext}"
            if cand not in used_flat_names:
                used_flat_names.add(cand)
                return cand
            n += 1

    zip_name = f"transcricoes_selecionadas_{uuid.uuid4().hex[:8]}.zip"
    zip_path = os.path.join(DATA_DIR, zip_name)
    files_written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, d, display in entries:
            for fname in os.listdir(d):
                # Only include files whose suffix (relative to base) matches the
                # user's chosen formats. e.g. base="abc12345_aula",
                # fname="abc12345_aula_timestamps.txt" -> suffix="_timestamps.txt".
                if fname.startswith(base):
                    suffix = fname[len(base):]
                    if suffix not in allowed_suffixes:
                        continue
                    out_name = _unique_name(display, suffix)
                else:
                    # Unexpected file — skip entirely; we can't safely apply a suffix
                    continue
                # Flat layout: no enclosing folder in the ZIP
                zf.write(os.path.join(d, fname), out_name)
                files_written += 1

    if not files_written:
        raise HTTPException(404, "Nenhum arquivo dos formatos escolhidos foi encontrado")
    return FileResponse(zip_path, filename="transcricoes_selecionadas.zip")

# -- Delete
@app.delete("/api/delete/{filename}")
async def api_delete(filename: str):
    """User-initiated delete from the transcriptions screen.
    Cascades: history entry + transcription result files + the original upload
    file + the media.json catalog entry. (The user explicitly asked for this
    cascade so a single 'Excluir' frees everything related to that item.)"""
    filename = _safe_filename(filename)
    with _history_lock:
        history = [h for h in _load_history() if h.get("file") != filename]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    base = _result_base(filename)
    if not base:
        raise HTTPException(400, "Filename inválido")
    d = os.path.join(RESULTS_DIR, base)
    # Defense-in-depth: ensure final path is inside RESULTS_DIR
    if os.path.commonpath([os.path.realpath(d), os.path.realpath(RESULTS_DIR)]) != os.path.realpath(RESULTS_DIR):
        raise HTTPException(400, "Filename inválido")
    if os.path.isdir(d):
        shutil.rmtree(d)

    # Cascade: delete the original audio/video upload tied to this transcription
    upload_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.commonpath([os.path.realpath(upload_path), os.path.realpath(UPLOAD_DIR)]) == os.path.realpath(UPLOAD_DIR):
        if os.path.exists(upload_path):
            try: os.remove(upload_path)
            except OSError: pass  # best-effort; we still drop the history+media entries

    # Cascade: drop the media catalog entry too
    with _media_lock:
        media = [m for m in _load_media() if m.get("file") != filename]
        with open(MEDIA_FILE, "w", encoding="utf-8") as f:
            json.dump(media, f, ensure_ascii=False, indent=2)

    return {"ok": True}


@app.get("/api/gaps/{filename}")
async def api_gaps(filename: str, min_gap: float = 1.0):
    """Detecta silêncios/respiros entre segmentos de fala e gera texto intercalado."""
    filename = _safe_filename(filename)
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

# Shared kickoff so /api/transcribe-url (single) and /api/transcribe-batch (many)
# don't duplicate yt-dlp setup, hook plumbing, and thread spawning.
def _kickoff_url_transcription(url: str, model: str, language: str, task: str,
                                filter_fillers: bool, folder: str) -> dict:
    task_id = str(uuid.uuid4())
    safe_name = re.sub(r'[^\w.-]', '_', url.split('/')[-1] or 'video')[:50] or 'video'
    original_name = f"{safe_name}.mp3"
    filename = f"{task_id[:8]}_{original_name}"
    upload_path = os.path.join(UPLOAD_DIR, filename)

    _set_task(task_id, status="processing", progress=0, phase="download", phase_progress=0,
              name="Download (Extraindo áudio...)", filename=filename)

    def _hook_main(d):
        # Honour cancel requests: raising aborts yt-dlp; we re-mark the task
        # as 'cancelled' in the runner's exception handler.
        if (_get_task(task_id) or {}).get('cancel_requested'):
            raise _UserCancelled('cancel requested during download')
        if d['status'] == 'downloading':
            pct_str = d.get('_percent_str', '')
            try:
                clean_str = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', pct_str)
                dl_pct = float(clean_str.replace('%', '').strip())  # 0–100 of download
                _set_task(task_id, phase="download", phase_progress=dl_pct,
                          progress=dl_pct * 0.25)  # overall: download owns 0–25%
            except (ValueError, TypeError):
                pass  # malformed progress string — skip this update

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': upload_path.replace('.mp3', '.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
        'quiet': True,
        'nocolor': True,
        'progress_hooks': [_hook_main]
    }
    _save_to_history(filename, {}, model, status="queued", task_id=task_id, original_name=_result_base(original_name), folder=folder, source="url")
    _save_media(filename, original_name, url=url, is_transcribed=True, status="queued")

    def _run_download_and_transcribe():
        try:
            with _download_sem:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
        except _UserCancelled:
            _update_history_status(filename, "cancelled")
            _save_media(filename, original_name, url=url, is_transcribed=True, status="cancelled")
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                      name="Download cancelado", filename=filename)
            return
        except Exception as e:
            _update_history_status(filename, "error", error=f"Erro ao baixar URL: {e}")
            _save_media(filename, original_name, url=url, is_transcribed=True, status="error")
            _set_task(task_id, status="error", progress=0, phase="error",
                      name="Erro no Download", error=str(e), filename=filename)
            return

        # Bridge to transcribe phase — if user cancelled between download end and
        # acquiring the sem, _run_transcription will catch it at the top.
        if (_get_task(task_id) or {}).get('cancel_requested'):
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled")
            _update_history_status(filename, "cancelled")
            return

        final_path = upload_path if os.path.exists(upload_path) else upload_path.replace('.mp3', '') + '.mp3'
        if not os.path.exists(final_path):
            for f in os.listdir(UPLOAD_DIR):
                if task_id[:8] in f:
                    final_path = os.path.join(UPLOAD_DIR, f)
                    break

        _run_transcription(task_id, final_path, filename, model, language, task, filter_fillers)

    t = threading.Thread(target=_run_download_and_transcribe, daemon=True)
    t.start()
    return {"task_id": task_id, "filename": filename}

@app.post("/api/transcribe-url")
async def api_transcribe_url(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    model: str = Form("turbo"),
    language: str = Form("pt"),
    task: str = Form("transcribe"),
    filter_fillers: str = Form("false"),
    folder: str = Form(""),
):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado. Execute: pip install yt-dlp")

    folder = _validate_folder_name(folder) if folder else ""
    if folder:
        with _folders_lock:
            paths = set(_load_folders_paths())
            for a in _ancestors_of(folder):
                paths.add(a)
            _save_folders_paths(sorted(paths))

    return _kickoff_url_transcription(url, model, language, task,
                                       filter_fillers == "true", folder)


# Batch dispatch — accepts a newline / comma separated list of URLs and fires
# each one. Two modes:
#   transcribe="true"  (default) → download + transcribe (full pipeline)
#   transcribe="false"           → download only (media_type + quality apply)
# Returns counts + the first few task_ids so the UI can hook polling.
@app.post("/api/transcribe-batch")
async def api_transcribe_batch(
    urls:           str  = Form(...),
    model:          str  = Form("turbo"),
    language:       str  = Form("pt"),
    task:           str  = Form("transcribe"),
    filter_fillers: str  = Form("false"),
    folder:         str  = Form(""),
    transcribe:     str  = Form("true"),   # NEW: "false" = download-only
    media_type:     str  = Form("video"),  # NEW: used when transcribe=false
    quality:        str  = Form("best"),   # NEW: used when transcribe=false
):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado. Execute: pip install yt-dlp")

    # Split on newline or comma; trim; drop blanks and non-http entries
    raw = re.split(r'[\n,;]+', urls or "")
    clean: list[str] = []
    seen: set = set()
    for line in raw:
        u = line.strip()
        if not u or not u.startswith(("http://", "https://")):
            continue
        if u in seen:  # dedup so accidentally-pasted duplicates don't double-fire
            continue
        seen.add(u)
        clean.append(u)
    if not clean:
        raise HTTPException(400, "Nenhuma URL válida (deve começar com http:// ou https://)")

    # Folder validation only applies to transcribe mode (download-only items
    # live in media.json and don't have a folder concept the user picks here).
    do_transcribe = transcribe == "true"
    if do_transcribe:
        folder = _validate_folder_name(folder) if folder else ""
        if folder:
            with _folders_lock:
                paths = set(_load_folders_paths())
                for a in _ancestors_of(folder):
                    paths.add(a)
                _save_folders_paths(sorted(paths))

    do_filter = filter_fillers == "true"
    task_ids: list[str] = []
    for u in clean:
        try:
            if do_transcribe:
                res = _kickoff_url_transcription(u, model, language, task, do_filter, folder)
            else:
                res = _kickoff_download_only(u, media_type, quality)
            task_ids.append(res["task_id"])
        except Exception:
            # Don't fail the whole batch if one URL trips at kickoff; record skip.
            task_ids.append(None)
    return {
        "submitted":  sum(1 for t in task_ids if t),
        "skipped":    sum(1 for t in task_ids if not t),
        "total":      len(clean),
        "transcribe": do_transcribe,
        "task_ids":   [t for t in task_ids if t][:50],  # cap response size
    }

def _run_download_only(task_id: str, url: str, media_type: str, quality: str):
    is_video = media_type == "video"
    ext = "mp4" if is_video else "mp3"
    filename = f"{task_id[:8]}_download.{ext}"
    try:
        _save_media(filename, f"Obtendo {'vídeo' if is_video else 'áudio'}...", url=url, is_transcribed=False, status="processing")
        _set_task(task_id, status="processing", progress=0,
                  phase="download", phase_progress=0,
                  name="Download Media", filename=filename)

        def _hook(d):
            if (_get_task(task_id) or {}).get('cancel_requested'):
                raise _UserCancelled('cancel requested during download')
            if d['status'] == 'downloading':
                pct_str = d.get('_percent_str', '')
                try:
                    clean_str = re.sub(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', pct_str)
                    pct = float(clean_str.replace('%', '').strip())
                    # Download-only: this IS the whole job, so progress == phase_progress
                    _set_task(task_id, phase="download", phase_progress=pct, progress=pct)
                except (ValueError, TypeError):
                    pass  # malformed progress string — skip this update

        upload_path = os.path.join(UPLOAD_DIR, filename)
        
        ydl_opts = {
            'quiet': True,
            'nocolor': True,
            'progress_hooks': [_hook]
        }

        if is_video:
            if quality == '1080p': format_str = 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best'
            elif quality == '720p': format_str = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
            elif quality == '480p': format_str = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
            else: format_str = 'bestvideo+bestaudio/best'
            ydl_opts['format'] = format_str
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['outtmpl'] = upload_path.replace('.mp4', '.%(ext)s')
        else:
            format_str = 'worstaudio/worst' if quality == 'worst' else 'bestaudio/best'
            ydl_opts['format'] = format_str
            ydl_opts['outtmpl'] = upload_path.replace('.mp3', '.%(ext)s')
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}]

        with _download_sem:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'Media Secundária')
            
        actual_path = upload_path if os.path.exists(upload_path) else upload_path.replace(f'.{ext}', '') + f'.{ext}'
        if not os.path.exists(actual_path):
            for f in os.listdir(UPLOAD_DIR):
                if task_id[:8] in f:
                    actual_path = os.path.join(UPLOAD_DIR, f)
                    filename = f
                    break
                    
        _save_media(filename, f"{title}.{ext}", url=url, is_transcribed=False, status="done")
        _set_task(task_id, status="done", progress=100, phase="done", phase_progress=100,
                  name=f"{title}.{ext}", filename=filename)
    except _UserCancelled:
        # Best-effort: remove the partial file so disk doesn't fill with .part / aborted writes
        for f in os.listdir(UPLOAD_DIR):
            if task_id[:8] in f:
                try: os.remove(os.path.join(UPLOAD_DIR, f))
                except OSError: pass
        _save_media(filename, "Download cancelado", url=url, is_transcribed=False, status="cancelled")
        _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                  name="Download cancelado", filename=filename)
    except Exception as e:
        _save_media(filename, "Erro no Download", url=url, is_transcribed=False, status="error")
        _set_task(task_id, status="error", progress=0, phase="error",
                  name="Erro no Download", error=str(e), filename=filename)

# Thin shared kickoff so single + batch download-only paths spawn the same
# threaded worker. Returns dict with task_id so callers can poll progress.
def _kickoff_download_only(url: str, media_type: str, quality: str) -> dict:
    task_id = str(uuid.uuid4())
    t = threading.Thread(target=_run_download_only,
                         args=(task_id, url, media_type, quality), daemon=True)
    t.start()
    return {"task_id": task_id}

@app.post("/api/yt-download-only")
async def api_yt_download_only(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    media_type: str = Form("video"),
    quality: str = Form("best")
):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    res = _kickoff_download_only(url, media_type, quality)
    return {"message": "Download_start", "task_id": res["task_id"]}

# ── Folders (nested, path-based) ───────────────────────────────
# Folder paths use '/' as separator, e.g. "Projetos/Cliente X/Q1".
# Empty string ("") means root (no folder).
# folders.json stores the canonical list of folder paths (allows empty folders).

_FOLDER_SEG_RE = re.compile(r'^[^/\\\x00]{1,60}$')

def _validate_folder_name(folder: str) -> str:
    """Validate and canonicalise a folder path.
    Empty string or None = root (no folder). Otherwise:
      - Segments separated by single '/'; no leading/trailing/double slash
      - Each segment: 1-60 chars, no backslash, null byte, or '.' / '..'
    Raises HTTPException(400) on bad input."""
    if folder is None:
        return ""
    folder = folder.strip().strip("/")
    if folder == "":
        return ""
    # Reject control chars and backslashes globally
    if "\x00" in folder or "\\" in folder:
        raise HTTPException(400, "Caminho de pasta contém caracteres inválidos")
    segments = folder.split("/")
    for seg in segments:
        seg = seg.strip()
        if not seg:
            raise HTTPException(400, "Caminho de pasta tem segmento vazio (barras duplicadas?)")
        if seg in (".", ".."):
            raise HTTPException(400, f"Segmento de pasta inválido: '{seg}'")
        if not _FOLDER_SEG_RE.match(seg):
            raise HTTPException(400, f"Segmento de pasta inválido: '{seg}' (máx 60 caracteres, sem /, \\ ou controle)")
    # Re-join with single slashes (canonical form)
    return "/".join(s.strip() for s in segments)

def _ancestors_of(path: str) -> list[str]:
    """Returns all ancestor paths of a folder (ordered from shallowest to deepest).
    e.g. 'A/B/C' -> ['A', 'A/B', 'A/B/C']"""
    if not path:
        return []
    segments = path.split("/")
    return ["/".join(segments[:i+1]) for i in range(len(segments))]

def _load_folders_paths() -> list[str]:
    if not os.path.exists(FOLDERS_FILE):
        return []
    try:
        with open(FOLDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str)]
    except (json.JSONDecodeError, ValueError):
        pass
    return []

def _save_folders_paths(paths: list[str]):
    # Deduplicate + sort for determinism, inside the caller's lock context
    paths = sorted(set(paths))
    with open(FOLDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f, ensure_ascii=False, indent=2)

@app.get("/api/folders")
async def api_folders():
    """Returns every folder path (explicit + implicit from entries) with item counts.
    A folder's count includes items in that folder AND all its descendants."""
    # Snapshot state under locks (brief) — counts are informational, no need for
    # a single global transaction across all three files.
    with _folders_lock:
        explicit_paths = set(_load_folders_paths())
    with _history_lock:
        hist = list(_load_history())
    with _media_lock:
        med = list(_load_media())

    # Paths = explicit + every ancestor implied by existing entries.
    # We merge hist + med here so paths inferred from EITHER source are visible,
    # but the COUNTS below are deduplicated by file to avoid double-counting
    # entries that exist in both history.json and media.json (the common case
    # for transcribed files — _save_to_history and _save_media both fire).
    paths = set(explicit_paths)
    for item in hist + med:
        p = (item.get("folder") or "").strip()
        if p:
            paths.update(_ancestors_of(p))

    # Count items per folder, propagating up to ancestors.
    # Deduplication: when a file appears in both history and media, history wins
    # (it's the canonical source for transcribed items, and the transcriptions
    # table is built from history alone, so the sidebar count must match it).
    counts: dict = {p: 0 for p in paths}
    seen_files: set = set()
    for item in hist + med:
        file_id = item.get("file")
        if not file_id or file_id in seen_files:
            continue
        seen_files.add(file_id)
        p = (item.get("folder") or "").strip()
        if not p:
            continue
        for anc in _ancestors_of(p):
            if anc in counts:
                counts[anc] += 1

    return [{"path": p, "count": counts.get(p, 0)} for p in sorted(paths)]

@app.post("/api/folders/create")
async def api_folders_create(path: str = Form(...)):
    """Create a folder (and implicitly all ancestors). No-op if it already exists."""
    path = _validate_folder_name(path)
    if not path:
        raise HTTPException(400, "Caminho de pasta vazio")
    with _folders_lock:
        paths = set(_load_folders_paths())
        # Create path + all ancestors
        for p in _ancestors_of(path):
            paths.add(p)
        _save_folders_paths(sorted(paths))
    return {"ok": True, "path": path}

@app.post("/api/folders/rename")
async def api_folders_rename(old_path: str = Form(...), new_path: str = Form(...)):
    """Rename a folder. Cascades to all descendants and all affected history/media entries."""
    old_path = _validate_folder_name(old_path)
    new_path = _validate_folder_name(new_path)
    if not old_path or not new_path:
        raise HTTPException(400, "Caminhos não podem ser vazios")
    if old_path == new_path:
        return {"ok": True, "path": new_path, "renamed": 0}
    # Refuse to rename ONTO a path that would create a cycle (new_path is a descendant of old_path)
    if new_path == old_path or new_path.startswith(old_path + "/"):
        raise HTTPException(400, "Não é possível mover uma pasta para dentro dela mesma")

    old_prefix = old_path + "/"

    def _remap(p: str) -> str | None:
        if p == old_path:
            return new_path
        if p.startswith(old_prefix):
            return new_path + "/" + p[len(old_prefix):]
        return None

    renamed_count = 0
    # Rename entries in folders.json
    with _folders_lock:
        paths = _load_folders_paths()
        new_paths = []
        for p in paths:
            r = _remap(p)
            new_paths.append(r if r is not None else p)
        # Also ensure new_path + ancestors exist
        for a in _ancestors_of(new_path):
            if a not in new_paths:
                new_paths.append(a)
        _save_folders_paths(new_paths)

    # Rename folder field in history entries
    with _history_lock:
        history = _load_history()
        for entry in history:
            r = _remap(entry.get("folder") or "")
            if r is not None:
                entry["folder"] = r
                renamed_count += 1
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

    # Rename folder field in media entries
    with _media_lock:
        media = _load_media()
        for entry in media:
            r = _remap(entry.get("folder") or "")
            if r is not None:
                entry["folder"] = r
                renamed_count += 1
        with open(MEDIA_FILE, "w", encoding="utf-8") as f:
            json.dump(media, f, ensure_ascii=False, indent=2)

    return {"ok": True, "path": new_path, "renamed": renamed_count}

@app.post("/api/folders/delete")
async def api_folders_delete(path: str = Form(...), cascade: str = Form("move")):
    """Delete a folder. `cascade` controls what happens to items inside:
      - 'move'   (default): move all descendant items to the parent folder
      - 'delete': delete all descendant items (history entries + result files);
                   media files are NOT deleted — they just lose their folder tag.
    Either way, the folder (and its descendants) disappear from folders.json."""
    path = _validate_folder_name(path)
    if not path:
        raise HTTPException(400, "Não é possível excluir a raiz")
    if cascade not in ("move", "delete"):
        raise HTTPException(400, "cascade deve ser 'move' ou 'delete'")

    prefix = path + "/"
    # Parent path of `path` (for move); may be "" (root)
    parent = "/".join(path.split("/")[:-1])

    affected_items = 0
    deleted_items = 0

    # Remove from folders.json: path + descendants
    with _folders_lock:
        paths = _load_folders_paths()
        new_paths = [p for p in paths if p != path and not p.startswith(prefix)]
        _save_folders_paths(new_paths)

    # Update history entries
    with _history_lock:
        history = _load_history()
        if cascade == "delete":
            # Mark items inside this folder for deletion
            to_delete = [h for h in history if (h.get("folder") or "") == path
                         or (h.get("folder") or "").startswith(prefix)]
            keep = [h for h in history if h not in to_delete]
            history = keep
            # Remove their result directories
            for h in to_delete:
                fname = h.get("file")
                if not fname:
                    continue
                d = os.path.join(RESULTS_DIR, _result_base(fname))
                # Defense: only rmtree if path is within RESULTS_DIR
                try:
                    if os.path.commonpath([os.path.realpath(d), os.path.realpath(RESULTS_DIR)]) == os.path.realpath(RESULTS_DIR):
                        if os.path.isdir(d):
                            shutil.rmtree(d)
                except (ValueError, OSError):
                    pass  # path comparison across drives/mounts may fail — skip defensively
            deleted_items += len(to_delete)
        else:  # move
            for entry in history:
                f = (entry.get("folder") or "")
                if f == path or f.startswith(prefix):
                    entry["folder"] = parent
                    affected_items += 1
        with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
            json.dump(history, fh, ensure_ascii=False, indent=2)

    # Update media entries — on 'delete' we only strip the folder tag
    # (we don't delete the physical upload files; user can still do that via DELETE /api/delete-media)
    with _media_lock:
        media = _load_media()
        if cascade == "delete":
            # Items in deleted history already handled above; media entries for those
            # files keep existing but lose folder tag
            for entry in media:
                f = (entry.get("folder") or "")
                if f == path or f.startswith(prefix):
                    entry["folder"] = ""  # orphaned back to root; physical file remains
                    affected_items += 1
        else:
            for entry in media:
                f = (entry.get("folder") or "")
                if f == path or f.startswith(prefix):
                    entry["folder"] = parent
                    affected_items += 1
        with open(MEDIA_FILE, "w", encoding="utf-8") as fh:
            json.dump(media, fh, ensure_ascii=False, indent=2)

    return {
        "ok": True,
        "path": path,
        "cascade": cascade,
        "moved_items": affected_items,
        "deleted_items": deleted_items,
    }

@app.post("/api/move-to-folder")
async def api_move_to_folder(filename: str = Form(...), folder: str = Form("")):
    """Moves a history entry and its matching media entry to a folder.
    Empty folder string = move back to root. Folder path may be nested (e.g. 'A/B/C')."""
    filename = _safe_filename(filename)
    folder   = _validate_folder_name(folder)

    # Auto-create ancestor folders in folders.json so the UI tree keeps them visible
    if folder:
        with _folders_lock:
            paths = set(_load_folders_paths())
            for a in _ancestors_of(folder):
                paths.add(a)
            _save_folders_paths(sorted(paths))

    moved = False
    with _history_lock:
        history = _load_history()
        for entry in history:
            if entry.get("file") == filename:
                entry["folder"] = folder
                moved = True
                break
        if moved:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)

    with _media_lock:
        media = _load_media()
        changed = False
        for entry in media:
            if entry.get("file") == filename:
                entry["folder"] = folder
                changed = True
                break
        if changed:
            with open(MEDIA_FILE, "w", encoding="utf-8") as f:
                json.dump(media, f, ensure_ascii=False, indent=2)

    if not moved and not changed:
        raise HTTPException(404, "Arquivo não encontrado em histórico nem mídia")
    return {"ok": True, "folder": folder}

# ── Entry point ────────────────────────────────────────────────
if __name__ == "__main__":
    print("✅  Whisper Transcritor → http://127.0.0.1:7860")
    uvicorn.run(app, host="127.0.0.1", port=7860, log_level="warning")
