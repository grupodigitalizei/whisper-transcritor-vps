#!/usr/bin/env python3
"""Assinaturas: acompanha canais/perfis e baixa (e transcreve) o que sai de novo.

A ideia
───────
Em vez de alguém colar link por link, você "assina" um canal do YouTube ou um
perfil do Instagram/TikTok/Facebook. Um poller de fundo checa de tempos em
tempos se saiu conteúdo novo e, quando sai, manda para o mesmo pipeline de
download/transcrição que o app já usa.

Duas decisões que evitam estrago
────────────────────────────────
1. **A primeira checagem não baixa nada por padrão.** Assinar um canal com 800
   vídeos e ver 800 downloads entrarem na fila seria desastroso: no cadastro,
   o que já existe é apenas marcado como visto. Se quiser trazer os últimos N,
   é uma escolha explícita (`initial_import`).
2. **Teto por checagem** (`max_per_check`): mesmo depois, uma rajada de uploads
   não vira uma avalanche de downloads simultâneos.

Sem dependência circular
────────────────────────
Este módulo não importa o whisper-app: quem sabe baixar/transcrever é injetado
via `configure()`. Isso mantém o módulo testável sozinho e evita o ciclo
whisper-app → subscriptions → whisper-app.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, ".whisper_data")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions.json")

PLATFORMS = ("youtube", "instagram", "tiktok", "facebook")

# Quantos IDs já vistos guardar por assinatura. Alto o bastante para um canal
# ativo nunca reprocessar um vídeo antigo; baixo o bastante para o JSON não
# virar um monstro.
_SEEN_CAP = 800

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 24 * 7
DEFAULT_INTERVAL_HOURS = 6
DEFAULT_MAX_PER_CHECK = 5
# Teto duro: nem o usuário pedindo consegue enfileirar mais que isto de uma vez.
HARD_MAX_PER_CHECK = 25

_lock = threading.Lock()
# Assinaturas com checagem em curso — impede que o poller e o "Checar agora"
# processem o mesmo perfil ao mesmo tempo e baixem tudo duas vezes.
_em_checagem: set = set()

# Injetados por configure() — ver docstring do módulo.
_discover = {}          # platform -> fn(target, limit) -> [{id, url, title}]
_kickoff_transcribe = None
_kickoff_download = None
_log = lambda msg: None  # noqa: E731


def configure(*, discover=None, kickoff_transcribe=None, kickoff_download=None,
              log=None) -> None:
    """Liga o módulo ao resto do app (chamado uma vez, no boot)."""
    global _discover, _kickoff_transcribe, _kickoff_download, _log
    if discover is not None:
        _discover = discover
    if kickoff_transcribe is not None:
        _kickoff_transcribe = kickoff_transcribe
    if kickoff_download is not None:
        _kickoff_download = kickoff_download
    if log is not None:
        _log = log


# ── store ──────────────────────────────────────────────────────────────────
def _atomic_write_json(path: str, data) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_subs_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _load_raw() -> list:
    if not os.path.exists(SUBS_FILE):
        return []
    try:
        with open(SUBS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError, OSError):
        return []


def _save_raw(subs: list) -> None:
    _atomic_write_json(SUBS_FILE, subs)


def _public(sub: dict) -> dict:
    """Versão para a UI — sem a lista de `seen_ids`, que é ruído e pode ser grande."""
    out = {k: v for k, v in sub.items() if k != "seen_ids"}
    out["seen_count"] = len(sub.get("seen_ids") or [])
    return out


def list_subscriptions() -> list:
    with _lock:
        return [_public(s) for s in _load_raw()]


# ── validação ──────────────────────────────────────────────────────────────
_LABEL_RE = re.compile(r"^[^\x00-\x1f]{1,80}$")


def _clean_target(platform: str, target: str) -> str:
    """Normaliza o alvo. YouTube aceita URL; as redes sociais aceitam @perfil
    ou URL, e viram um handle nu (é o que o coletor espera)."""
    target = (target or "").strip()
    if not target:
        raise ValueError("informe o canal ou perfil")
    if platform == "youtube":
        parsed = urlparse(target if "://" in target else "https://" + target)
        host = (parsed.hostname or "").lower()
        # Scheme também é validado: sem isto um "ftp://youtube.com/x" passaria
        # daqui direto para o yt-dlp.
        if parsed.scheme not in ("http", "https"):
            raise ValueError("use um endereço http:// ou https://")
        if not any(host == h or host.endswith("." + h)
                   for h in ("youtube.com", "youtu.be")):
            raise ValueError("para YouTube, informe a URL do canal (youtube.com/@canal)")
        return target if "://" in target else "https://" + target
    # instagram/tiktok/facebook → handle
    if "://" in target:
        target = urlparse(target).path.strip("/").split("/")[0]
    target = target.lstrip("@").strip()
    if not re.fullmatch(r"[\w.\-]{1,50}", target):
        raise ValueError(f"perfil inválido: {target!r}")
    return target


def add_subscription(platform: str, target: str, *, label: str = "",
                     auto_transcribe: bool = True, model: str = "turbo",
                     language: str = "pt", folder: str = "",
                     interval_hours: float = DEFAULT_INTERVAL_HOURS,
                     max_per_check: int = DEFAULT_MAX_PER_CHECK,
                     initial_import: int = 0) -> dict:
    """Cria uma assinatura. `initial_import` = quantos itens JÁ existentes trazer
    agora (0 = nenhum: o passado só é marcado como visto)."""
    if platform not in PLATFORMS:
        raise ValueError(f"plataforma inválida: {platform!r}")
    target = _clean_target(platform, target)
    label = (label or "").strip() or target
    if not _LABEL_RE.match(label):
        raise ValueError("nome inválido")
    interval_hours = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, float(interval_hours)))
    max_per_check = max(1, min(HARD_MAX_PER_CHECK, int(max_per_check)))
    initial_import = max(0, min(HARD_MAX_PER_CHECK, int(initial_import)))

    sub = {
        "id": uuid.uuid4().hex[:12],
        "platform": platform,
        "target": target,
        "label": label,
        "auto_transcribe": bool(auto_transcribe),
        "model": model,
        "language": language,
        "folder": folder or "",
        "interval_hours": interval_hours,
        "max_per_check": max_per_check,
        "paused": False,
        "created_at": time.time(),
        "last_check_at": None,
        "last_status": "novo",
        "last_message": "aguardando a primeira checagem",
        "last_new_count": 0,
        "total_fetched": 0,
        # Cadastro novo: nada foi visto ainda. A primeira checagem decide o que
        # é "passado" (marcado sem baixar) e o que entra por initial_import.
        "seen_ids": [],
        "bootstrapped": False,
        "initial_import": initial_import,
    }
    with _lock:
        subs = _load_raw()
        for s in subs:
            if s.get("platform") == platform and s.get("target") == target:
                raise ValueError("você já assina esse canal/perfil")
        subs.append(sub)
        _save_raw(subs)
    return _public(sub)


def update_subscription(sub_id: str, **changes) -> dict:
    """Altera campos editáveis de uma assinatura."""
    editable = {"label", "auto_transcribe", "model", "language", "folder",
                "interval_hours", "max_per_check", "paused"}
    with _lock:
        subs = _load_raw()
        for s in subs:
            if s.get("id") != sub_id:
                continue
            for k, v in changes.items():
                if k not in editable:
                    continue
                if k == "interval_hours":
                    v = max(MIN_INTERVAL_HOURS, min(MAX_INTERVAL_HOURS, float(v)))
                elif k == "max_per_check":
                    v = max(1, min(HARD_MAX_PER_CHECK, int(v)))
                elif k in ("auto_transcribe", "paused"):
                    v = bool(v)
                s[k] = v
            _save_raw(subs)
            return _public(s)
    raise KeyError("assinatura não encontrada")


def remove_subscription(sub_id: str) -> bool:
    with _lock:
        subs = _load_raw()
        rest = [s for s in subs if s.get("id") != sub_id]
        if len(rest) == len(subs):
            return False
        _save_raw(rest)
        return True


def _mark_result(sub_id: str, *, status: str, message: str,
                 new_ids: list | None = None, fetched: int = 0,
                 bootstrapped: bool | None = None) -> None:
    """Grava o resultado de uma checagem (roda fora do lock da checagem em si)."""
    with _lock:
        subs = _load_raw()
        for s in subs:
            if s.get("id") != sub_id:
                continue
            s["last_check_at"] = time.time()
            s["last_status"] = status
            s["last_message"] = message
            if new_ids:
                seen = list(s.get("seen_ids") or [])
                seen.extend(i for i in new_ids if i not in seen)
                s["seen_ids"] = seen[-_SEEN_CAP:]
            s["last_new_count"] = fetched
            s["total_fetched"] = int(s.get("total_fetched") or 0) + fetched
            if bootstrapped is not None:
                s["bootstrapped"] = bootstrapped
            _save_raw(subs)
            return


def _get(sub_id: str) -> dict | None:
    with _lock:
        for s in _load_raw():
            if s.get("id") == sub_id:
                return dict(s)
    return None


# ── checagem ───────────────────────────────────────────────────────────────
def check_subscription(sub_id: str, *, force: bool = False) -> dict:
    """Checa uma assinatura: descobre o que há de novo e dispara o pipeline.

    Retorna um resumo {status, message, started}. Nunca levanta por falha de
    rede/coletor — o erro vira `last_status='erro'` e a próxima checagem tenta
    de novo (um perfil offline não pode derrubar o poller inteiro).

    Duas execuções simultâneas da MESMA assinatura são recusadas: elas leriam o
    mesmo `seen_ids` (nenhuma gravou ainda), calculariam a mesma lista e
    baixariam tudo em dobro. Acontecia ao clicar "Checar agora" enquanto o
    poller já estava checando aquele perfil — provável, já que uma coleta em
    rede social demora.
    """
    with _lock:
        if sub_id in _em_checagem:
            return {"status": "ocupada",
                    "message": "esta assinatura já está sendo checada agora",
                    "started": 0}
        _em_checagem.add(sub_id)
    try:
        return _check_subscription_locked(sub_id, force=force)
    finally:
        with _lock:
            _em_checagem.discard(sub_id)


def _check_subscription_locked(sub_id: str, *, force: bool = False) -> dict:
    sub = _get(sub_id)
    if not sub:
        raise KeyError("assinatura não encontrada")
    if sub.get("paused") and not force:
        return {"status": "pausada", "message": "assinatura pausada", "started": 0}

    platform = sub["platform"]
    discover = _discover.get(platform)
    if not discover:
        msg = f"sem coletor disponível para {platform}"
        _mark_result(sub_id, status="erro", message=msg)
        return {"status": "erro", "message": msg, "started": 0}

    seen = set(sub.get("seen_ids") or [])
    first_run = not sub.get("bootstrapped")
    # A JANELA DE OBSERVAÇÃO precisa ser bem maior que o teto de download.
    # Se olhássemos só `max_per_check` itens, uma rajada de 10 uploads com teto
    # 2 nos mostraria apenas os 2 mais recentes — eles seriam marcados como
    # vistos e os 8 do meio nunca seriam baixados. Olhamos muito, baixamos
    # pouco: o excedente fica pendente para as próximas checagens.
    per_check = int(sub.get("max_per_check") or DEFAULT_MAX_PER_CHECK)
    limit = max(per_check * 4, 20, int(sub.get("initial_import") or 0))
    if first_run:
        limit = max(limit, 30)

    try:
        items = discover(sub["target"], limit) or []
    except Exception as exc:  # noqa: BLE001 — coletor externo, qualquer falha
        msg = f"falha ao consultar: {exc}"
        _log(f"[subs] {sub['label']}: {msg}")
        _mark_result(sub_id, status="erro", message=msg)
        return {"status": "erro", "message": msg, "started": 0}

    fresh = [it for it in items if it.get("id") and it["id"] not in seen]

    # Primeira checagem: o acervo antigo é só marcado como visto. Baixar tudo
    # que um canal já publicou entupiria a fila — ver docstring do módulo.
    if first_run:
        take = fresh[:int(sub.get("initial_import") or 0)]
        started_ids = _start_items(sub, take)
        started = len(started_ids)
        # No bootstrap o passado inteiro é marcado como visto de propósito (é o
        # que evita baixar o acervo de um canal antigo). A exceção são os itens
        # do initial_import que FALHARAM ao iniciar: esses ficam pendentes para
        # a próxima checagem em vez de se perderem.
        falhos = {it["id"] for it in take} - set(started_ids)
        vistos = [it["id"] for it in fresh if it["id"] not in falhos]
        _mark_result(sub_id,
                     status="ok",
                     message=(f"assinatura iniciada — {len(vistos) - started} itens marcados "
                              f"como já vistos" + (f", {started} baixando" if started else "")),
                     new_ids=vistos,
                     fetched=started,
                     bootstrapped=True)
        return {"status": "ok", "message": "assinatura iniciada", "started": started}

    if not fresh:
        _mark_result(sub_id, status="ok", message="nada novo", fetched=0)
        return {"status": "ok", "message": "nada novo", "started": 0}

    take = fresh[:int(sub.get("max_per_check") or DEFAULT_MAX_PER_CHECK)]
    started_ids = _start_items(sub, take)
    started = len(started_ids)
    skipped = len(fresh) - len(take)
    falhos = len(take) - started
    msg = f"{started} novo(s) em processamento"
    if skipped > 0:
        # Não some com o resto em silêncio: os que passaram do teto ficam para a
        # próxima checagem (não entram em seen_ids).
        msg += f"; {skipped} além do limite ficaram para a próxima"
    if falhos > 0:
        msg += f"; {falhos} falharam ao iniciar e serão tentados de novo"
    # Só o que REALMENTE começou vira "visto".
    _mark_result(sub_id, status="ok", message=msg,
                 new_ids=started_ids, fetched=started)
    return {"status": "ok", "message": msg, "started": started}


def _start_items(sub: dict, items: list) -> list:
    """Manda cada item para o pipeline. Falha de um não impede os outros.

    Devolve os ids que REALMENTE começaram. Marcar como visto um item que falhou
    ao iniciar (rede instável, disco cheio) o condenava: ele nunca seria baixado
    e nunca mais seria tentado.
    """
    started_ids = []
    for it in items:
        url = it.get("url")
        if not url:
            continue
        try:
            if sub.get("auto_transcribe") and _kickoff_transcribe:
                _kickoff_transcribe(url=url, model=sub.get("model") or "turbo",
                                    language=sub.get("language") or "pt",
                                    folder=sub.get("folder") or "")
            elif _kickoff_download:
                _kickoff_download(url=url, folder=sub.get("folder") or "")
            else:
                continue
            started_ids.append(it.get("id"))
        except Exception as exc:  # noqa: BLE001
            _log(f"[subs] {sub.get('label')}: falha ao iniciar {url}: {exc}")
    return [i for i in started_ids if i]


def due_subscriptions(now: float | None = None) -> list:
    """Assinaturas cuja hora de checar chegou (ativas e vencidas)."""
    now = now if now is not None else time.time()
    out = []
    with _lock:
        for s in _load_raw():
            if s.get("paused"):
                continue
            last = s.get("last_check_at")
            if last is None:
                out.append(s["id"])
                continue
            if now - float(last) >= float(s.get("interval_hours") or
                                           DEFAULT_INTERVAL_HOURS) * 3600:
                out.append(s["id"])
    return out


# ── poller ─────────────────────────────────────────────────────────────────
_poller_thread: threading.Thread | None = None
_poller_stop = threading.Event()
# De quanto em quanto tempo o poller ACORDA (não é o intervalo da assinatura —
# cada uma tem o seu; isto é só a granularidade da varredura).
_TICK_SECS = 300


def _poller_loop() -> None:
    # Espera antes da primeira varredura: no boot o servidor tem coisa melhor a
    # fazer do que sair baixando vídeo.
    if _poller_stop.wait(60):
        return
    while not _poller_stop.is_set():
        try:
            for sub_id in due_subscriptions():
                if _poller_stop.is_set():
                    break
                try:
                    res = check_subscription(sub_id)
                    if res.get("started"):
                        _log(f"[subs] {sub_id}: {res['message']}")
                except Exception as exc:  # noqa: BLE001
                    _log(f"[subs] erro inesperado em {sub_id}: {exc}")
        except Exception as exc:  # noqa: BLE001 — o poller nunca pode morrer
            _log(f"[subs] erro no poller: {exc}")
        _poller_stop.wait(_TICK_SECS)


def start_poller() -> bool:
    """Sobe o poller (idempotente: chamar duas vezes não cria dois)."""
    global _poller_thread
    if _poller_thread and _poller_thread.is_alive():
        return False
    _poller_stop.clear()
    _poller_thread = threading.Thread(target=_poller_loop, name="subs-poller",
                                      daemon=True)
    _poller_thread.start()
    return True


def stop_poller() -> None:
    _poller_stop.set()
