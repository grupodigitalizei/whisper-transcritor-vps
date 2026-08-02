"""Testes de comportamento para o núcleo de segurança (auth.py) e para a máquina
de estados do runner de transcrição (_run_transcription em whisper-app.py).

Tudo roda 100% isolado em tmp_path — nenhum teste toca em .whisper_data real:
os caminhos de arquivo do módulo são redirecionados por fixture antes de cada
teste, e o `whisper` (torch) é stubado para o import ser rápido e sem efeitos.

Run:  ./venv/bin/python -m pytest tests/ -v
"""
import os
import sys
import json
import time
import types
import importlib.util

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")

# Permite `import auth` (auth.py vive na raiz do repo, só depende da stdlib).
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Carrega whisper-app.py com `whisper` stubado (evita importar torch) ─────────
sys.modules.setdefault("whisper", types.ModuleType("whisper"))
_APP_PATH = os.path.join(_ROOT, "whisper-app.py")
_spec = importlib.util.spec_from_file_location("whisper_app_runner_test", _APP_PATH)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


# ════════════════════════════════════════════════════════════════════════════
#  auth.py — hashing, sessões, revogação, freio de força-bruta
# ════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def auth_iso(tmp_path, monkeypatch):
    """auth.py isolado: arquivos em tmp_path e estado em memória zerado."""
    import auth
    monkeypatch.setattr(auth, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth, "AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr(auth, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(auth, "_sessions", {})
    monkeypatch.setattr(auth, "_sessions_loaded", False)
    monkeypatch.setattr(auth, "_failures", [])
    monkeypatch.setattr(auth, "_locked_until", 0.0)
    return auth


def test_hash_matches_roundtrip(auth_iso):
    rec = auth_iso.hash_password("uma-senha-forte")
    assert auth_iso._matches(rec, "uma-senha-forte")
    assert not auth_iso._matches(rec, "outra-senha")
    assert not auth_iso._matches(None, "qualquer")


def test_hash_uses_random_salt(auth_iso):
    a, b = auth_iso.hash_password("igual"), auth_iso.hash_password("igual")
    assert a["salt"] != b["salt"] and a["hash"] != b["hash"]  # salt por senha


def test_role_for_password(auth_iso):
    auth_iso.set_password(auth_iso.ROLE_ADMIN, "admin-secreta-1")
    auth_iso.set_password(auth_iso.ROLE_PUBLIC, "equipe-secreta-2")
    assert auth_iso.role_for_password("admin-secreta-1") == auth_iso.ROLE_ADMIN
    assert auth_iso.role_for_password("equipe-secreta-2") == auth_iso.ROLE_PUBLIC
    assert auth_iso.role_for_password("errada") is None


def test_set_password_rejects_short(auth_iso):
    with pytest.raises(ValueError):
        auth_iso.set_password(auth_iso.ROLE_ADMIN, "curta")


def test_set_password_rejects_bad_role(auth_iso):
    with pytest.raises(ValueError):
        auth_iso.set_password("root", "senha-boa-1234")


def test_session_lifecycle(auth_iso):
    tok = auth_iso.create_session(auth_iso.ROLE_ADMIN)
    assert auth_iso.role_for_token(tok) == auth_iso.ROLE_ADMIN
    auth_iso.destroy_session(tok)
    assert auth_iso.role_for_token(tok) is None


def test_session_expiry(auth_iso):
    tok = auth_iso.create_session(auth_iso.ROLE_PUBLIC)
    auth_iso._sessions[tok]["expires_at"] = time.time() - 1  # já expirou
    assert auth_iso.role_for_token(tok) is None


def test_change_password_revokes_sessions(auth_iso):
    auth_iso.set_password(auth_iso.ROLE_PUBLIC, "senha-inicial-1")
    tok = auth_iso.create_session(auth_iso.ROLE_PUBLIC)
    assert auth_iso.role_for_token(tok) == auth_iso.ROLE_PUBLIC
    auth_iso.set_password(auth_iso.ROLE_PUBLIC, "senha-nova-2")   # troca = revoga
    assert auth_iso.role_for_token(tok) is None


def test_revoke_role_keeps_other_role(auth_iso):
    ta = auth_iso.create_session(auth_iso.ROLE_ADMIN)
    tp = auth_iso.create_session(auth_iso.ROLE_PUBLIC)
    n = auth_iso.destroy_sessions_for_role(auth_iso.ROLE_PUBLIC)
    assert n == 1
    assert auth_iso.role_for_token(tp) is None          # público caiu
    assert auth_iso.role_for_token(ta) == auth_iso.ROLE_ADMIN  # admin fica


def test_lockout_triggers_after_max_failures(auth_iso):
    assert auth_iso.login_locked_for() == 0
    for _ in range(auth_iso._FAIL_MAX):
        auth_iso.record_login_failure()
    assert auth_iso.login_locked_for() > 0


def test_success_resets_failure_streak(auth_iso):
    for _ in range(auth_iso._FAIL_MAX - 1):     # um a menos que o gatilho
        auth_iso.record_login_failure()
    assert auth_iso.login_locked_for() == 0
    auth_iso.record_login_success()             # zera o histórico
    for _ in range(auth_iso._FAIL_MAX - 1):     # de novo não trava
        auth_iso.record_login_failure()
    assert auth_iso.login_locked_for() == 0


# ════════════════════════════════════════════════════════════════════════════
#  whisper-app.py — máquina de estados do runner + allowlist de modelo
# ════════════════════════════════════════════════════════════════════════════
_FAKE_RESULT = {
    "text": "oi mundo",
    "timestamped": "[00:00] oi mundo",
    "srt": "1\n00:00:00,000 --> 00:00:01,000\noi mundo\n",
    "json_data": {"text": "oi mundo", "segments": []},
    "duration": "0:01",
    "duration_secs": 1.0,
    "words": 2,
    "segments": 1,
    "lang": "pt",
}


@pytest.fixture
def app_iso(tmp_path, monkeypatch):
    """Runner isolado: catálogos/resultados em tmp_path, sem tocar Whisper real."""
    results = tmp_path / "results"
    uploads = tmp_path / "uploads"
    results.mkdir(); uploads.mkdir()
    monkeypatch.setattr(app, "HISTORY_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(app, "MEDIA_FILE", str(tmp_path / "media.json"))
    monkeypatch.setattr(app, "RESULTS_DIR", str(results))
    monkeypatch.setattr(app, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(app, "_tasks", {})
    # Concorrência determinística, sem ler settings.json real.
    monkeypatch.setattr(app, "_load_settings",
                        lambda: {"transcribe_concurrent": 2, "download_concurrent": 1})
    return app


def test_run_transcription_done_path(app_iso, monkeypatch):
    monkeypatch.setattr(app_iso, "_load_model", lambda name: object())
    monkeypatch.setattr(app_iso, "_transcribe_one", lambda *a, **k: dict(_FAKE_RESULT))
    app_iso._run_transcription("t-done", "/tmp/x.mp3", "abcd_x.mp3",
                               "turbo", "pt", "transcribe", False)
    t = app_iso._get_task("t-done")
    assert t["status"] == "done"
    assert t["words"] == 2 and t["lang"] == "pt"
    hist = json.load(open(app_iso.HISTORY_FILE))
    assert any(h.get("status") == "done" for h in hist)  # persistiu no history


def test_run_transcription_error_path(app_iso, monkeypatch):
    def _boom(name):
        raise RuntimeError("modelo explodiu")
    monkeypatch.setattr(app_iso, "_load_model", _boom)
    app_iso._run_transcription("t-err", "/tmp/x.mp3", "abcd_x.mp3",
                               "turbo", "pt", "transcribe", False)
    t = app_iso._get_task("t-err")
    assert t["status"] == "error"
    assert "explodiu" in (t.get("error") or "")


def test_run_transcription_cancel_while_queued(app_iso, monkeypatch):
    monkeypatch.setattr(app_iso, "_load_model", lambda name: object())
    called = {"n": 0}
    def _spy(*a, **k):
        called["n"] += 1
        return dict(_FAKE_RESULT)
    monkeypatch.setattr(app_iso, "_transcribe_one", _spy)
    app_iso._set_task("t-cancel", status="queued", cancel_requested=True)
    app_iso._run_transcription("t-cancel", "/tmp/x.mp3", "abcd_x.mp3",
                               "turbo", "pt", "transcribe", False)
    assert app_iso._get_task("t-cancel")["status"] == "cancelled"
    assert called["n"] == 0  # nem chegou a transcrever


def test_load_model_rejects_unknown_name(app_iso):
    # Não deve nem tocar em whisper.load_model — rejeita antes.
    with pytest.raises(ValueError):
        app_iso._load_model("../../etc/passwd")


def test_validate_transcribe_params(app_iso):
    app_iso._validate_transcribe_params("turbo", "transcribe")  # ok, não levanta
    with pytest.raises(app_iso.HTTPException):
        app_iso._validate_transcribe_params("modelo-fake", "transcribe")
    with pytest.raises(app_iso.HTTPException):
        app_iso._validate_transcribe_params("turbo", "tarefa-fake")
