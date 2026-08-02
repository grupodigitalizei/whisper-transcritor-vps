#!/usr/bin/env python3
"""Autenticação por senha compartilhada + sessões persistidas em arquivo.

Dois papéis, uma senha para cada:

  • admin   → você. Vê e administra TUDO (itens privados + públicos).
  • public  → funcionários. Só veem/mexem no que está marcado como público.

Nada aqui depende de biblioteca externa: PBKDF2-HMAC-SHA256 vem do `hashlib` da
stdlib. As senhas nunca são gravadas em texto puro — só o hash + salt.

Sessões vivem em `.whisper_data/sessions.json` para sobreviverem a um restart do
servidor (senão todo funcionário teria que logar de novo a cada reinício).
"""
from __future__ import annotations
import os, json, time, hmac, hashlib, secrets, threading

SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.join(SCRIPT_DIR, ".whisper_data")
AUTH_FILE     = os.path.join(DATA_DIR, "auth.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")

ROLE_ADMIN  = "admin"
ROLE_PUBLIC = "public"
ROLES       = (ROLE_ADMIN, ROLE_PUBLIC)

# Sessão longa de propósito: o funcionário abre o link no celular e não quer
# digitar senha toda hora. Revogação é feita trocando a senha (derruba as
# sessões daquele papel) — ver `set_password`.
SESSION_TTL_SECS = 30 * 24 * 3600
COOKIE_NAME      = "wt_session"

# PBKDF2: custo alto o suficiente para tornar força-bruta offline caro, baixo
# o suficiente para o login não travar (~150 ms num Mac Apple Silicon).
_PBKDF2_ROUNDS = 260_000

MIN_PASSWORD_LEN = 8

_lock = threading.Lock()


# ── util ───────────────────────────────────────────────────────
def _atomic_write_json(path: str, data) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".tmp_auth_{secrets.token_hex(6)}.json")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # 0600: só o dono lê. auth.json guarda hashes; sessions.json guarda
        # tokens válidos — ambos merecem o mesmo cuidado de um ~/.ssh.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return default


# ── hashing ────────────────────────────────────────────────────
def _derive(password: str, salt: str, rounds: int) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds
    ).hex()


def hash_password(password: str) -> dict:
    salt = secrets.token_hex(16)
    return {"salt": salt, "rounds": _PBKDF2_ROUNDS,
            "hash": _derive(password, salt, _PBKDF2_ROUNDS)}


def _matches(record: dict | None, password: str) -> bool:
    if not record or not password:
        return False
    try:
        got = _derive(password, record["salt"], int(record.get("rounds", _PBKDF2_ROUNDS)))
    except (KeyError, ValueError, TypeError):
        return False
    # compare_digest: tempo constante, não vaza o prefixo correto da senha.
    return hmac.compare_digest(got, record.get("hash", ""))


# ── senhas geradas na primeira execução ────────────────────────
# Palavras curtas, sem acento e sem ambiguidade — a senha vai ser digitada no
# celular por gente que não usa gerenciador de senha.
_WORDS = (
    "abacaxi ancora antena arvore aurora bambu bandeja barco bilhete bonde bussola "
    "cacau cachoeira caderno camarao canela capivara cascata cavalo cebola cometa "
    "coragem cristal deserto diamante domino duna eclipse escada esfera estrela "
    "fabrica farol fivela floresta fogueira folhagem fortaleza galope garoa girassol "
    "goiaba granito harpa hexagono horizonte ilha imperio janela jangada jardim "
    "labirinto lagoa lanterna limao lousa luneta manjericao maracuja marfim molde "
    "montanha muralha navio nevoa oceano oficina orvalho paisagem palmeira pantano "
    "pedreira pimenta pinheiro planalto pomar portal quartzo quiosque rabanete "
    "raizes relampago represa riacho rodovia sabia salgueiro semente serrote "
    "sombra tamboril tapete telhado tomilho torneira trilha tulipa turmalina "
    "vagalume varanda veleiro vertente vinhedo violeta xicara zimbro"
).split()


def generate_password(words: int = 3) -> str:
    """Senha legível tipo `farol-capivara-tulipa-47`. ~57 bits de entropia com
    3 palavras + 2 dígitos, o que é bem mais forte que qualquer senha que um
    humano escolheria — e ainda dá para ditar por telefone."""
    parts = [secrets.choice(_WORDS) for _ in range(words)]
    parts.append(f"{secrets.randbelow(90) + 10}")
    return "-".join(parts)


# ── store de senhas ────────────────────────────────────────────
def _load_auth() -> dict:
    return _read_json(AUTH_FILE, {}) or {}


def ensure_initialized() -> dict:
    """Cria auth.json na primeira execução com senhas fortes aleatórias.

    Retorna `{role: senha_em_texto}` APENAS para as senhas recém-criadas, para o
    caller imprimir no terminal uma única vez. Se o arquivo já existe (ou a
    variável de ambiente definiu a senha), retorna {} — não há como recuperar
    uma senha depois, só trocar.
    """
    with _lock:
        data = _load_auth()
        created: dict = {}
        changed = False
        for role, env_var in ((ROLE_ADMIN, "WHISPER_ADMIN_PASSWORD"),
                              (ROLE_PUBLIC, "WHISPER_PUBLIC_PASSWORD")):
            env_pw = os.environ.get(env_var)
            if env_pw:
                # Env vence sempre — permite resetar uma senha esquecida sem
                # editar JSON na mão: WHISPER_ADMIN_PASSWORD=... python whisper-app.py
                if not _matches(data.get(role), env_pw):
                    data[role] = hash_password(env_pw)
                    changed = True
                continue
            if not data.get(role):
                pw = generate_password()
                data[role] = hash_password(pw)
                created[role] = pw
                changed = True
        if changed:
            data.setdefault("created_at", time.time())
            _atomic_write_json(AUTH_FILE, data)
        return created


def role_for_password(password: str) -> str | None:
    """Descobre o papel a partir da senha digitada. Admin é testado primeiro:
    se por descuido as duas senhas forem iguais, o dono não perde acesso."""
    data = _load_auth()
    for role in ROLES:
        if _matches(data.get(role), password):
            return role
    return None


def set_password(role: str, password: str) -> None:
    if role not in ROLES:
        raise ValueError("papel inválido")
    password = (password or "").strip()
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"a senha precisa ter pelo menos {MIN_PASSWORD_LEN} caracteres")
    with _lock:
        data = _load_auth()
        data[role] = hash_password(password)
        data[f"{role}_changed_at"] = time.time()
        _atomic_write_json(AUTH_FILE, data)
    # Trocar a senha é o mecanismo de revogação: derruba todas as sessões
    # daquele papel (ex.: funcionário saiu da empresa).
    destroy_sessions_for_role(role)


def password_changed_at(role: str) -> float | None:
    data = _load_auth()
    v = data.get(f"{role}_changed_at") or data.get("created_at")
    return float(v) if v else None


# ── sessões ────────────────────────────────────────────────────
# Mantidas em memória e espelhadas em disco. Só gravamos em create/destroy —
# um "touch" por request tornaria cada GET um write de arquivo.
_sessions: dict = {}
_sessions_loaded = False


def _ensure_sessions_loaded() -> None:
    global _sessions, _sessions_loaded
    if _sessions_loaded:
        return
    raw = _read_json(SESSIONS_FILE, {}) or {}
    now = time.time()
    _sessions = {
        tok: meta for tok, meta in raw.items()
        if isinstance(meta, dict)
        and meta.get("role") in ROLES
        and float(meta.get("expires_at", 0)) > now
    }
    _sessions_loaded = True


def _persist_sessions_locked() -> None:
    _atomic_write_json(SESSIONS_FILE, _sessions)


def create_session(role: str) -> str:
    if role not in ROLES:
        raise ValueError("papel inválido")
    with _lock:
        _ensure_sessions_loaded()
        now = time.time()
        # Aproveita a escrita para limpar as expiradas (evita crescer sem fim).
        for tok in [t for t, m in _sessions.items() if float(m.get("expires_at", 0)) <= now]:
            _sessions.pop(tok, None)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {"role": role, "created_at": now,
                            "expires_at": now + SESSION_TTL_SECS}
        _persist_sessions_locked()
        return token


def role_for_token(token: str | None) -> str | None:
    if not token:
        return None
    with _lock:
        _ensure_sessions_loaded()
        meta = _sessions.get(token)
        if not meta:
            return None
        if float(meta.get("expires_at", 0)) <= time.time():
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return None
        return meta.get("role")


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with _lock:
        _ensure_sessions_loaded()
        if _sessions.pop(token, None) is not None:
            _persist_sessions_locked()


def destroy_sessions_for_role(role: str) -> int:
    with _lock:
        _ensure_sessions_loaded()
        doomed = [t for t, m in _sessions.items() if m.get("role") == role]
        for t in doomed:
            _sessions.pop(t, None)
        if doomed:
            _persist_sessions_locked()
        return len(doomed)


def active_session_count(role: str | None = None) -> int:
    with _lock:
        _ensure_sessions_loaded()
        now = time.time()
        return sum(1 for m in _sessions.values()
                   if float(m.get("expires_at", 0)) > now
                   and (role is None or m.get("role") == role))


# ── freio de força-bruta ───────────────────────────────────────
# O app fica atrás de um túnel (Tailscale Funnel), então TODO request chega
# como 127.0.0.1 — bloquear por IP seria inútil. O contador é global: depois de
# muitas falhas seguidas, o /login inteiro esfria por alguns minutos.
_FAIL_WINDOW_SECS = 10 * 60
_FAIL_MAX         = 10
_LOCKOUT_SECS     = 5 * 60

_failures: list = []      # timestamps de tentativas erradas
_locked_until = 0.0


def login_locked_for() -> int:
    """Segundos restantes de bloqueio (0 = pode tentar)."""
    with _lock:
        return max(0, int(_locked_until - time.time()))


def record_login_failure() -> None:
    global _locked_until
    with _lock:
        now = time.time()
        _failures.append(now)
        del _failures[:max(0, len(_failures) - 100)]
        recent = [t for t in _failures if now - t <= _FAIL_WINDOW_SECS]
        if len(recent) >= _FAIL_MAX:
            _locked_until = now + _LOCKOUT_SECS
            _failures.clear()


def record_login_success() -> None:
    with _lock:
        _failures.clear()
