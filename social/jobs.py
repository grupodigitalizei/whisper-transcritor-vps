"""Registro simples de jobs em background (threads). Portado de ~/IGSorter."""
import threading
import time
import uuid

_jobs = {}
_lock = threading.Lock()
_MAX_JOBS = 100


def _prune_locked():
    if len(_jobs) <= _MAX_JOBS:
        return
    # Remove os terminados mais antigos (ordem de inserção).
    done = [jid for jid, j in _jobs.items() if j["status"] in ("done", "error")]
    for jid in done[: max(0, len(_jobs) - _MAX_JOBS)]:
        _jobs.pop(jid, None)


def start(kind, target, *args, **kwargs):
    job_id = uuid.uuid4().hex[:10]
    job = {"id": job_id, "kind": kind, "status": "running", "progress": None,
           "log": [], "result": None, "error": None, "started": time.time()}
    with _lock:
        _prune_locked()
        _jobs[job_id] = job

    def log(msg):
        job["log"] = (job["log"] + [str(msg)])[-40:]

    def run():
        try:
            job["result"] = target(job, log, *args, **kwargs)
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)

    threading.Thread(target=run, daemon=True).start()
    return job_id


def get(job_id):
    return _jobs.get(job_id)
