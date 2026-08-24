#!/usr/bin/env python3
"""Whisper Transcritor — FastAPI + HTML frontend"""
from __future__ import annotations
import os, json, shutil, threading, uuid, datetime, re, zipfile, time, tempfile, ipaddress, subprocess
from urllib.parse import urlparse
from contextlib import asynccontextmanager

# yt-dlp's YouTube extractor needs the Deno runtime (via yt-dlp-ejs) to solve
# the `n` challenge. Deno is usually installed at ~/.deno/bin/deno but that's
# not always in $PATH when the server is launched from an IDE/launcher.
# Prepend it here so the subprocess yt-dlp spawns can find it.
_deno_bin = os.path.expanduser("~/.deno/bin")
if os.path.isdir(_deno_bin) and _deno_bin not in os.environ.get("PATH", "").split(":"):
    os.environ["PATH"] = _deno_bin + ":" + os.environ.get("PATH", "")

import whisper
try:
    import yt_dlp
    YT_DLP_OK = True
except ImportError:
    YT_DLP_OK = False

# Módulos de redes sociais (coleta via ego-lite + download HD). Portados do
# sistema IGSorter do usuário — bem mais robustos que o yt-dlp para Instagram.
import gdrive  # download de arquivos públicos do Google Drive (trata token de confirmação)
import download_engine  # cascata de motores: se um jeito de baixar falha, tenta o próximo
import subscriptions    # assinaturas: acompanha canais/perfis e traz o que sai de novo
import storage         # inventário de disco: o que ocupa espaço e como limpar
import compressor       # compressão de vídeo/áudio via FFmpeg (hardware no Apple Silicon)

try:
    from social import core as social_core, collector as social_collector, \
                       downloader as social_downloader, jobs as social_jobs
    SOCIAL_OK = True
    try:
        from social import excel as social_excel
        SOCIAL_EXCEL_OK = True
    except Exception:
        SOCIAL_EXCEL_OK = False
    # Tecnologia multi-rede atualizada: interceptação (IG/TikTok/YT/FB) + download
    # por URL via yt-dlp com plano B no navegador logado.
    try:
        from social import intercept as social_intercept, medialink as social_medialink
        SOCIAL_MULTI_OK = True
    except Exception:
        SOCIAL_MULTI_OK = False
    # Coleta de comentários de um post (mesma interceptação da coleta de feed).
    try:
        from social import comments as social_comments
        SOCIAL_COMMENTS_OK = True
    except Exception:
        SOCIAL_COMMENTS_OK = False
except Exception:
    SOCIAL_OK = False
    SOCIAL_EXCEL_OK = False
    SOCIAL_MULTI_OK = False
    SOCIAL_COMMENTS_OK = False
import auth
from fastapi import FastAPI, File, UploadFile, HTTPException, Form, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse, \
                              RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
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
# Pastas que um usuário público criou. Existem só para uma pasta recém-criada
# (ainda vazia) não desaparecer da tela dele — as pastas com conteúdo público
# já são deduzidas dos próprios itens.
PUBLIC_FOLDERS_FILE = os.path.join(DATA_DIR, "public_folders.json")
HTML_FILE    = os.path.join(SCRIPT_DIR, "index.html")
LOGIN_FILE   = os.path.join(SCRIPT_DIR, "login.html")
STATIC_DIR   = os.path.join(SCRIPT_DIR, "static")

for d in (RESULTS_DIR, UPLOAD_DIR, STATIC_DIR):
    os.makedirs(d, exist_ok=True)

# ── Limits & security config ───────────────────────────────────
# Max upload size (streamed to disk in chunks — never buffered whole in RAM).
# Override with WHISPER_MAX_UPLOAD_MB. Default 4 GB covers long videos.
MAX_UPLOAD_BYTES = int(os.environ.get("WHISPER_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
UPLOAD_CHUNK     = 4 * 1024 * 1024  # 4 MiB streaming chunk

# Hosts allowed to receive the Chrome profile's cookies during a yt-dlp download.
# Cookies are attached ONLY when the target URL's host matches one of these,
# so a random/malicious URL can never harvest the user's authenticated
# Google/YouTube cookies (audit finding #1).
COOKIE_ALLOWED_SUFFIXES = (
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "google.com", "googlevideo.com", "ggpht.com",
)

# Same-origin hosts accepted for state-changing requests (CSRF guard, finding #3).
# O host real da requisição (header Host) é sempre aceito além destes — sem isso
# o app pararia de funcionar assim que fosse publicado num domínio/túnel.
ALLOWED_ORIGIN_HOSTS = {"127.0.0.1:7860", "localhost:7860"}

# ── Área pública ───────────────────────────────────────────────
# Cada item de history.json / media.json carrega um campo `visibility`:
#
#   "private" (ou ausente) → só o administrador vê. Todo o acervo que já existia
#                            antes desta feature cai aqui automaticamente, que é
#                            exatamente o que queremos: nada vaza por acidente.
#   "public"               → aparece na Área Pública, acessível por quem entrar
#                            com a senha compartilhada.
#
# O administrador publica/despublica manualmente (POST /api/visibility); tudo que
# um usuário público cria já nasce "public".
VIS_PRIVATE = "private"
VIS_PUBLIC  = "public"

# Ponte entre o request (que sabe QUEM está enviando) e as threads de background
# de download/transcrição (que não sabem). O endpoint registra aqui a
# visibilidade pretendida para o arquivo; _save_to_history/_save_media leem isso
# na PRIMEIRA gravação. Depois disso o valor gravado é que vale — nada muda a
# visibilidade de um item sem passar por /api/visibility.
_pending_vis: dict = {}
_pending_vis_lock  = threading.Lock()
_PENDING_VIS_MAX   = 500

def _mark_pending_visibility(filename: str, visibility: str) -> None:
    if not filename:
        return
    with _pending_vis_lock:
        if len(_pending_vis) >= _PENDING_VIS_MAX:
            # Descarta os mais antigos (dict preserva ordem de inserção). Só
            # importa até a primeira gravação em disco, que acontece em segundos.
            for k in list(_pending_vis)[:_PENDING_VIS_MAX // 2]:
                _pending_vis.pop(k, None)
        _pending_vis[filename] = visibility

def _pending_visibility(filename: str) -> str:
    with _pending_vis_lock:
        return _pending_vis.get(filename, VIS_PRIVATE)

def _vis_of(entry: dict | None) -> str:
    """Visibilidade de uma entrada de catálogo. Qualquer coisa diferente de
    "public" é tratada como privada (fail-closed)."""
    return VIS_PUBLIC if (entry or {}).get("visibility") == VIS_PUBLIC else VIS_PRIVATE

def _visibility_for_file(filename: str,
                         history: list | None = None,
                         media: list | None = None) -> str:
    """Visibilidade efetiva de um arquivo no disco.

    O que já está gravado vence; se o arquivo ainda não chegou a nenhum catálogo
    (upload/download em andamento), usa a intenção registrada no kickoff."""
    if not filename:
        return VIS_PRIVATE
    for entry in (history if history is not None else _load_history()):
        if entry.get("file") == filename:
            return _vis_of(entry)
    for entry in (media if media is not None else _load_media()):
        if entry.get("file") == filename:
            return _vis_of(entry)
    return _pending_visibility(filename)

def _role_of(request: Request) -> str:
    """Papel da requisição, posto pelo middleware de autenticação."""
    role = getattr(request.state, "role", None)
    if role not in (auth.ROLE_ADMIN, auth.ROLE_PUBLIC):
        raise HTTPException(401, "Não autenticado")
    return role

def _is_admin(request: Request) -> bool:
    return _role_of(request) == auth.ROLE_ADMIN

def _require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(403, "Apenas o administrador pode fazer isso.")

def _visibility_for_new(request: Request) -> str:
    """Visibilidade de um item recém-criado: o que o funcionário envia nasce
    público (é o acervo compartilhado dele); o que o admin envia nasce privado."""
    return VIS_PRIVATE if _is_admin(request) else VIS_PUBLIC

def _scope_entries(entries: list, role: str) -> list:
    if role == auth.ROLE_ADMIN:
        return entries
    return [e for e in entries if _vis_of(e) == VIS_PUBLIC]

def _require_file_access(filename: str, request: Request) -> None:
    """Barra o acesso de um usuário público a um arquivo privado.

    Responde 404 (e não 403) de propósito: para quem não tem acesso, o item
    simplesmente não existe — não confirmamos nem a existência do arquivo."""
    if _is_admin(request):
        return
    if _visibility_for_file(filename) != VIS_PUBLIC:
        raise HTTPException(404, "Arquivo não encontrado")

def _atomic_write_json(path: str, data) -> None:
    """Write JSON to `path` atomically: dump to a temp file in the same dir,
    fsync, then os.replace (atomic on POSIX). A crash mid-write can no longer
    truncate history.json / media.json / folders.json (audit finding #6)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.remove(tmp)
        except OSError: pass
        raise

def _safe_remove(path: str) -> None:
    """Best-effort file removal (used as a response BackgroundTask to delete
    temp ZIPs after they've been streamed to the client — finding #4)."""
    try: os.remove(path)
    except OSError: pass

def _validate_media_url(url: str) -> str:
    """Validate a user-supplied media URL before handing it to yt-dlp.
    Only http/https, must have a hostname, and blocks obvious SSRF targets
    (localhost, link-local, private ranges) — audit finding #10. Returns the
    trimmed URL or raises HTTPException(400)."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "URL inválida: use http:// ou https://")
    host = parsed.hostname
    if not host:
        raise HTTPException(400, "URL inválida: host ausente")
    low = host.lower()
    if low == "localhost" or low.endswith(".local") or low.endswith(".internal"):
        raise HTTPException(400, "URL não permitida (host interno)")
    try:
        ip = ipaddress.ip_address(low)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            raise HTTPException(400, "URL não permitida (endereço interno)")
    except ValueError:
        pass  # not a literal IP — a regular hostname, which is fine
    return url

def _host_allows_cookies(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in COOKIE_ALLOWED_SUFFIXES)

# Navegador de onde o yt-dlp lê os cookies do YouTube. Num servidor (container,
# VPS) não existe Chrome nenhum — e aí o yt-dlp NÃO ignora a opção, ele aborta
# ("could not find chrome cookies database"). Sem esta checagem, todo download
# de YouTube quebrava fora do Mac. Vazio/"none" desliga na mão.
_COOKIES_BROWSER_ENV = os.environ.get("WHISPER_YTDLP_COOKIES", "chrome").strip().lower()

def _chrome_profile_exists() -> bool:
    for d in ("~/Library/Application Support/Google/Chrome",   # macOS
              "~/.config/google-chrome", "~/.config/chromium", # Linux
              "~/AppData/Local/Google/Chrome/User Data"):      # Windows
        if os.path.isdir(os.path.expanduser(d)):
            return True
    return False

def _cookies_browser_for(url: str):
    """Tupla `cookiesfrombrowser` para esta URL, ou None quando não dá/não deve.

    Só para hosts da allowlist (os cookies do Google nunca vazam para um domínio
    qualquer) e só quando há perfil de navegador no disco."""
    if not _COOKIES_BROWSER_ENV or _COOKIES_BROWSER_ENV in ("none", "0", "off"):
        return None
    if not _host_allows_cookies(url):
        return None
    if _COOKIES_BROWSER_ENV == "chrome" and not _chrome_profile_exists():
        return None
    return (_COOKIES_BROWSER_ENV,)

# Saídas para o bloqueio de IP do YouTube. Num IP de datacenter (VPS, cloud) a
# extração funciona mas o download dos dados leva 403 — e nenhum player_client
# resolve, porque o bloqueio é do IP, não do cliente. As duas saídas reais:
#
#   WHISPER_YTDLP_COOKIEFILE=/caminho/cookies.txt
#       Cookies de uma conta logada, no formato Netscape. ATENÇÃO: usar cookies
#       da sua conta pessoal a partir de um IP de datacenter é um bom jeito de
#       ela ser bloqueada pelo Google — use uma conta descartável.
#
#   WHISPER_YTDLP_PROXY=http://user:senha@host:porta
#       Faz o yt-dlp sair por outro IP. Mais robusto que cookies e não põe
#       conta nenhuma em risco, mas proxy residencial é serviço pago.
#
# Ambas valem para qualquer URL, não só YouTube.
_YTDLP_COOKIEFILE = (os.environ.get("WHISPER_YTDLP_COOKIEFILE") or "").strip()
_YTDLP_PROXY      = (os.environ.get("WHISPER_YTDLP_PROXY") or "").strip()

def _apply_network_opts(opts: dict, url: str) -> dict:
    """Anexa cookies/proxy configurados por ambiente. Muta e devolve `opts`."""
    cookies = _cookies_browser_for(url)
    if cookies:
        opts["cookiesfrombrowser"] = cookies
    elif _YTDLP_COOKIEFILE and _host_allows_cookies(url):
        # Mesma regra do navegador: cookie só vai para host da allowlist, nunca
        # para uma URL qualquer que o usuário cole.
        if os.path.isfile(_YTDLP_COOKIEFILE):
            opts["cookiefile"] = _YTDLP_COOKIEFILE
        else:
            print(f"[yt-dlp] WHISPER_YTDLP_COOKIEFILE aponta para um arquivo que "
                  f"não existe: {_YTDLP_COOKIEFILE}")
    if _YTDLP_PROXY:
        opts["proxy"] = _YTDLP_PROXY
    return opts

def _build_ydl_opts(url: str, progress_hook, base: dict | None = None) -> dict:
    """Shared yt-dlp options for every download path (dedups the block that was
    copied across the transcribe + download-only flows — finding #11). Attaches
    Chrome cookies ONLY for allowlisted hosts (finding #1)."""
    opts = {
        'quiet': True,
        'nocolor': True,
        'progress_hooks': [progress_hook],
        # Bypass YouTube's SABR streaming (yt-dlp#12482) by preferring clients
        # that still expose progressive URLs; 'web' stays last as fallback.
        'extractor_args': {'youtube': {'player_client': ['web', 'tv_simply', 'ios', 'mweb']}},
        'retries': 3,
        # Sem isto, uma conexão que abre e depois estagna (CDN mudo, túnel
        # caindo) trava o yt-dlp para sempre SEGURANDO um slot do _download_sem.
        # Três sockets mortos bastavam para parar a fila inteira em silêncio,
        # sem erro e sem log — só reiniciar resolvia.
        'socket_timeout': 60,
    }
    # YouTube/Google need login cookies for progressive URLs. Only sent to
    # allowlisted hosts so cookies never leak to arbitrary domains.
    _apply_network_opts(opts, url)
    if base:
        opts.update(base)
    return opts

def _cleanup_task_files(prefix: str) -> None:
    """Best-effort removal of any (partial/.part) files matching `prefix` in
    UPLOAD_DIR. Called on download error/cancel so aborted writes don't pile up
    (audit finding #5). `prefix` should be the target filename's own stem
    (e.g. os.path.splitext(filename)[0]) — NOT necessarily task_id, since a
    retry reuses the original filename under a brand-new task_id, and yt-dlp's
    outtmpl is built from the filename, not the task_id."""
    try:
        for f in os.listdir(UPLOAD_DIR):
            if prefix in f:
                try: os.remove(os.path.join(UPLOAD_DIR, f))
                except OSError: pass
    except OSError:
        pass

def _ydl_download_with_fallback(url: str, ydl_opts: dict, task_id: str,
                                filename: str, phase_label: str = "download",
                                preserve_partials: bool = False):
    """Baixa via yt-dlp passando pela cascata de motores (download_engine).

    O motor 1 é exatamente `ydl_opts` como o chamador montou — o caminho que já
    funciona hoje não muda. Os seguintes só entram se o anterior levantar. Um
    cancelamento do usuário (_UserCancelled) sobe na hora, sem virar retry.

    Retorna o `info` do yt-dlp (mesmo contrato de ydl.extract_info) e registra na
    task qual motor venceu, para a UI e o histórico mostrarem.
    """
    def _on_engine(engine, idx, total):
        if idx == 1:
            return       # motor padrão: não poluir a UI com "tentativa 1/N"
        _set_task(task_id, phase=phase_label,
                  engine=engine.name,
                  engine_note=f"Plano B: {engine.label} ({idx}/{total})")

    def _on_before_retry():
        # Remove .part/parciais da tentativa anterior, senão o yt-dlp tentaria
        # retomar bytes inválidos no motor seguinte.
        #
        # Exceção: numa RETOMADA de download pausado, os parciais vêm de antes
        # desta execução e são justamente o que a pausa preservou. Limpá-los
        # aqui fazia a pausa se autodestruir — bastava o motor 1 falhar por um
        # motivo transitório e o usuário perdia 80% já baixados, sem aviso.
        if preserve_partials:
            return
        _cleanup_task_files(os.path.splitext(filename)[0])

    info, engine_name = download_engine.run_with_fallback(
        url, ydl_opts, yt_dlp.YoutubeDL,
        # Cancelar e pausar são decisões do usuário, não falha de motor: sobem
        # na hora em vez de disparar a próxima tentativa da cascata.
        abort_types=(_UserCancelled, _DownloadPaused),
        on_engine=_on_engine,
        on_before_retry=_on_before_retry,
        log=lambda msg: print(f"[download_engine] {msg}"),
    )
    _set_task(task_id, engine=engine_name)
    return info

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
    # Encoder de hardware (videotoolbox) tem poucas sessões simultâneas, e por
    # software o ffmpeg come a CPU que o Whisper precisa. 2 é um meio-termo que
    # aproveita a máquina sem competir com a transcrição.
    "compress_concurrent":   2,   # quantos ffmpeg em paralelo
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
        _atomic_write_json(SETTINGS_FILE, out)
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
_compress_sem   = _DynamicSem("compress_concurrent")    # a compressão era a única fila sem teto
_download_sem   = _DynamicSem("download_concurrent")    # NEW — gate yt-dlp jobs

_thread_local = threading.local()

class _UserCancelled(Exception):
    """Raised inside yt-dlp progress hooks to abort an in-flight download.
    Distinct exception type so generic try/except blocks don't swallow it
    silently — we want it to propagate to the runner, which marks the task
    as 'cancelled' instead of 'error'."""
    pass

class _DownloadPaused(Exception):
    """Irmã de _UserCancelled, para PAUSAR em vez de cancelar.

    A diferença que importa está no tratamento: no cancelamento os arquivos
    parciais são apagados; na pausa eles são preservados de propósito — são
    justamente os .part que permitem retomar de onde parou em vez de baixar
    tudo de novo."""
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

# Modelos e tarefas aceitos. Lista fixa de propósito: não chamamos
# whisper.available_models() em tempo de import (os testes stubam o whisper) e,
# mais importante, recusamos um nome arbitrário ANTES de repassá-lo a
# whisper.load_model — que trataria uma string desconhecida como caminho de
# arquivo de checkpoint. Cobre todos os valores oferecidos pela interface.
_VALID_MODELS = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "large", "large-v3-turbo", "turbo",
}
_VALID_TASKS = {"transcribe", "translate"}

def _validate_transcribe_params(model: str, task: str) -> None:
    """Rejeita modelo/tarefa fora da lista conhecida com um 400 claro, em vez de
    deixar o valor inválido virar erro lá dentro da thread de transcrição."""
    if model not in _VALID_MODELS:
        raise HTTPException(400, f"Modelo inválido: {model!r}")
    if task not in _VALID_TASKS:
        raise HTTPException(400, f"Tarefa inválida: {task!r}")

# Quantos modelos Whisper manter carregados. Cada um ocupa de centenas de MB a
# vários GB de RAM; sem teto, experimentar turbo → large-v3 → medium ao longo da
# semana deixava os três residentes até o processo morrer, num Mac que também
# roda ffmpeg e o navegador do ego-lite.
# Num servidor com pouca RAM (VPS de 8 GB, por exemplo) dois modelos residentes
# já são o suficiente para o kernel matar o processo no meio de uma transcrição.
# WHISPER_MAX_CACHED_MODELS=1 troca a lentidão de recarregar do disco pela
# garantia de não levar OOM.
_MAX_CACHED_MODELS = max(1, int(os.environ.get("WHISPER_MAX_CACHED_MODELS", "2")))

def _load_model(name: str):
    # Rede de segurança: qualquer caminho que chegue aqui (upload, URL, retry)
    # passa por esta checagem antes de tocar em whisper.load_model.
    if name not in _VALID_MODELS:
        raise ValueError(f"modelo inválido: {name!r}")
    with _models_lock:
        if name in _models:
            # Marca como usado por último (dict preserva ordem de inserção).
            _models[name] = _models.pop(name)
            return _models[name]
        _models[name] = whisper.load_model(name)
        # Descarta o menos usado recentemente. Só a referência é solta: o
        # próximo uso recarrega do disco, que é barato perto de manter GB presos.
        while len(_models) > _MAX_CACHED_MODELS:
            antigo = next(iter(_models))          # chave mais antiga
            _models.pop(antigo, None)
            print(f"[modelos] descarregado '{antigo}' para liberar memória")
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

def _tem_extensao_real(name: str) -> bool:
    """Se `name` já termina numa extensão de arquivo de verdade.

    `os.path.splitext` sozinho não serve aqui: o nome exibido de uma mídia
    social vem da legenda do post, e uma legenda que termina em "…na mente. "
    devolve ext=". ", que é truthy — o arquivo então era baixado SEM o .mp4 e
    não abria. Exigir algo curto e alfanumérico (.mp4, .jpg, .opus) evita isso."""
    return bool(re.fullmatch(r"\.[A-Za-z0-9]{1,5}", os.path.splitext(name)[1]))

def _original_media_name_for(filename: str) -> str:
    """Return the media's original full filename (with extension) for download."""
    ext = os.path.splitext(filename)[1]
    def _com_ext(name: str) -> str:
        # Espaço/ponto no fim quebra a criação do arquivo no Windows e deixa o
        # nome ambíguo em qualquer sistema — some antes de anexar a extensão.
        name = name.rstrip(" .")
        return name if _tem_extensao_real(name) else name + ext
    try:
        # History is the most reliable source (name stored as original basename without ext)
        for entry in _load_history():
            if entry.get("file") == filename and entry.get("name"):
                return _com_ext(entry["name"])
        for entry in _load_media():
            if entry.get("file") == filename and entry.get("name"):
                return _com_ext(entry["name"])
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

def _build_markdown_text(name: str, text: str, lang: str = "?",
                         duration: str = "—", model: str = "?", date: str = "",
                         source_url: str | None = None) -> str:
    """Compose a Markdown version of a transcription: title + metadata line,
    then the plain text as the body. Pure formatter — no I/O.

    `source_url` (quando o item veio de um link) entra como linha própria: um
    .md que sai daqui costuma ser lido longe do app, e sem a origem não há como
    voltar ao vídeo para conferir um trecho ou citar a fonte."""
    meta = " · ".join(p for p in (
        f"**Duração:** {duration}" if duration not in (None, "", "—") else None,
        f"**Idioma:** {lang}"      if lang     not in (None, "", "?") else None,
        f"**Modelo:** {model}"     if model    not in (None, "", "?") else None,
        f"**Transcrito em:** {date}" if date   not in (None, "") else None,
    ) if p)
    parts = [f"# {name}"]
    if meta:
        parts += ["", meta]
    if source_url:
        # Link explícito (texto + destino) em vez de autolink: assim continua
        # legível mesmo quando o .md é colado num editor que não renderiza.
        parts += ["", f"**Fonte original:** [{source_url}]({source_url})"]
    parts += ["", "---", "", text.strip(), ""]
    return "\n".join(parts)

def _source_url_for(filename: str, media: list | None = None) -> str | None:
    """URL de origem do item, quando ele veio de um link (YouTube, Instagram,
    TikTok, Drive…). Fica só no media.json — o history.json não guarda esse
    campo — então a busca é lá, unida pelo nome do arquivo.

    Aceita `media` já carregado para não reler o catálogo inteiro por item
    quando o chamador está montando um ZIP com dezenas de transcrições."""
    try:
        for entry in (media if media is not None else _load_media()):
            if entry.get("file") == filename:
                url = (entry.get("url") or "").strip()
                # Só http(s): o campo também recebe caminhos locais em itens
                # que vieram de upload, e linkar isso num .md não serve a nada.
                return url if url.startswith(("http://", "https://")) else None
    except Exception:
        pass
    return None

def _ensure_markdown(filename: str, history: list | None = None) -> str | None:
    """Return the path to {base}.md inside the results dir, generating it lazily
    from the already-saved .txt + history metadata if missing (transcriptions
    saved before Markdown export existed). Returns None if there's no result
    directory at all for this filename."""
    base = _result_base(filename)
    d    = os.path.join(RESULTS_DIR, base)
    if not os.path.isdir(d):
        return None
    md_path = os.path.join(d, f"{base}.md")
    source_url = _source_url_for(filename)
    if os.path.exists(md_path):
        # Um .md gerado antes de existir a linha "Fonte original" fica sem ela
        # para sempre, já que este caminho normalmente só reaproveita o arquivo.
        # Se hoje temos a URL e o arquivo ainda não a cita, vale regerar — o .md
        # é um export derivado do .txt, então recriá-lo não perde nada.
        if not source_url:
            return md_path
        try:
            with open(md_path, encoding="utf-8") as f:
                if source_url in f.read():
                    return md_path
        except OSError:
            return md_path
    txt_path = os.path.join(d, f"{base}.txt")
    text = ""
    if os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            text = f.read()
    history = history if history is not None else _load_history()
    entry = next((h for h in history if h.get("file") == filename), {})
    content = _build_markdown_text(
        name=_original_name_for(filename), text=text,
        lang=entry.get("lang", "?"), duration=entry.get("duration", "—"),
        model=entry.get("mode", "?"), date=entry.get("date", ""),
        source_url=source_url,
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)
    return md_path

def _save_result_files(filename: str, result: dict):
    base = _result_base(filename)
    d    = _result_dir(filename)
    # Queued history entry already carries the display name/model/date by the
    # time transcription finishes — reused here so the .md header is complete.
    entry = next((h for h in _load_history() if h.get("file") == filename), {})
    md_content = _build_markdown_text(
        name=_original_name_for(filename), text=result["text"],
        lang=result.get("lang", "?"), duration=result.get("duration", "—"),
        model=entry.get("mode", "?"), date=entry.get("date", ""),
        source_url=_source_url_for(filename),
    )
    for fname, content in [
        (f"{base}.txt",            result["text"]),
        (f"{base}_timestamps.txt", result["timestamped"]),
        (f"{base}.srt",            result["srt"]),
        (f"{base}.md",             md_content),
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
                     folder: str | None = None, source: str | None = None,
                     task_type: str | None = None, filter_fillers: bool | None = None):
    # Atomic read-modify-write under a single lock to prevent lost updates
    with _history_lock:
        history = _load_history()
        # Preserve original date, folder, and timing fields if entry already exists
        existing = next((h for h in history if h.get("file") == filename), {})
        # New 'folder' / 'source' args win on first insert; otherwise preserve.
        folder_to_use = folder if folder is not None else existing.get("folder", "")
        source_to_use = source if source is not None else existing.get("source")
        name_to_use = original_name or existing.get("name") or _result_base(filename)
        # task_type/filter_fillers: só conhecidos no momento do kickoff (status
        # "queued"); chamadas posteriores (ex. status "done") não os repassam,
        # então preservamos o que já foi salvo — usado por /api/retry para
        # refazer com a MESMA escolha original (transcrever vs traduzir, filtro).
        task_type_to_use = task_type if task_type is not None else existing.get("task_type", "transcribe")
        filter_fillers_to_use = filter_fillers if filter_fillers is not None else existing.get("filter_fillers", False)
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
            "task_type":      task_type_to_use,
            "filter_fillers": filter_fillers_to_use,
            # Área pública: o valor já gravado manda (só /api/visibility troca);
            # na primeira gravação, vale a intenção registrada no kickoff.
            "visibility":   existing.get("visibility") or _pending_visibility(filename),
            # Timing fields (filled during transcription by _update_history_status)
            "started_at":   existing.get("started_at"),
            "completed_at": existing.get("completed_at"),
            "processing_secs": existing.get("processing_secs"),
        }
        history = [h for h in history if h.get("file") != filename]
        history.insert(0, entry)
        _atomic_write_json(HISTORY_FILE, history)

# ── Media Tracking ─────────────────────────────────────────────
def _load_media() -> list:
    if not os.path.exists(MEDIA_FILE):
        return []
    try:
        with open(MEDIA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return []

def _save_media(filename: str, original_name: str, url: str | None = None, is_transcribed: bool = False, status: str = "done", force_name: bool = False):
    # Atomic read-modify-write under a single lock to prevent lost updates
    with _media_lock:
        media = _load_media()
        existing = next((m for m in media if m.get("file") == filename), {})

        path = os.path.join(UPLOAD_DIR, filename)
        size_bytes = os.path.getsize(path) if os.path.exists(path) else 0

        # Preserve the name stored on first save (the true original filename).
        # Later calls from _run_transcription pass the hashed base, which would
        # otherwise overwrite the good name. `force_name=True` overrides this —
        # used once the real media title is known after a yt-dlp download.
        name_to_use = original_name if force_name else (existing.get("name") or original_name)
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
            # Mesma regra do history: gravado vence, senão a intenção do kickoff.
            "visibility": existing.get("visibility") or _pending_visibility(filename),
        }

        media = [m for m in media if m.get("file") != filename]
        media.insert(0, entry)

        _atomic_write_json(MEDIA_FILE, media)

def _remove_media_entry(filename: str) -> None:
    """Remove uma entrada do catálogo (sem tocar no arquivo). Usado quando um
    download finaliza com um nome/extensão diferente do provisório."""
    with _media_lock:
        media = _load_media()
        new = [m for m in media if m.get("file") != filename]
        if len(new) != len(media):
            _atomic_write_json(MEDIA_FILE, new)

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
        _atomic_write_json(HISTORY_FILE, history)

# ── Task tracking ──────────────────────────────────────────────
_tasks:      dict = {}
_tasks_lock        = threading.Lock()
_TERMINAL_STATES   = ("done", "error", "cancelled")
_MAX_TERMINAL_TASKS = 300   # cap so _tasks doesn't grow forever (audit finding #8)

def _prune_tasks_locked():
    """Drop the oldest terminal (done/error/cancelled) tasks once they exceed the
    cap. Active (queued/processing) tasks are always kept. Relies on dict
    insertion order — oldest terminal entries are removed first. Caller holds
    _tasks_lock."""
    terminal_ids = [tid for tid, t in _tasks.items()
                    if t.get("status") in _TERMINAL_STATES]
    excess = len(terminal_ids) - _MAX_TERMINAL_TASKS
    for tid in terminal_ids[:max(0, excess)]:
        _tasks.pop(tid, None)

def _set_task(task_id: str, **kw):
    with _tasks_lock:
        _tasks.setdefault(task_id, {}).update(kw)
        if kw.get("status") in _TERMINAL_STATES:
            _prune_tasks_locked()

# Teto de URLs por envio em lote. O Download Avançado já tinha o seu
# (_ADVANCED_MAX_TOTAL_ITEMS); este fluxo não herdou o cuidado.
_BATCH_MAX_ITEMS = 150

_ACTIVE_STATES = ("queued", "processing", "paused")

def _active_task_for(filename: str) -> str | None:
    """task_id de uma tarefa viva para este arquivo, ou None.

    Serve de trava para operações destrutivas: apagar, comprimir ou re-enfileirar
    um arquivo que uma thread ainda está usando corrompe o resultado — a thread
    não sabe que o arquivo sumiu e segue gravando com o nome antigo.
    """
    if not filename:
        return None
    with _tasks_lock:
        for tid, t in _tasks.items():
            if t.get("filename") == filename and t.get("status") in _ACTIVE_STATES:
                return tid
    return None

def _get_task(task_id: str) -> dict:
    # Sempre retorna um dict (vazio se a task não existe) — nunca None. O código
    # a jusante depende disso para poder chamar .get() sem checar antes.
    with _tasks_lock:
        return dict(_tasks.get(task_id, {}))

def _run_transcription(task_id, file_path, filename, model_name,
                       language, task_type, do_filter):
    _thread_local.task_id = task_id
    # Um item que acabou de baixar continua "processing" com phase="download"
    # em 100% até um slot do _transcribe_sem vagar. Sem marcar essa espera, a
    # linha congelava em "Baixando 100%" — com a ETA do download já vencida —
    # e parecia travada justamente quando a fila estava cheia.
    # Uploads diretos chegam aqui como "queued" e devem permanecer assim: a UI
    # já os mostra como "Aguardando" e os chips de filtro contam por status.
    if _get_task(task_id).get("status") == "processing":
        _set_task(task_id, phase="awaiting_transcribe", phase_progress=0)
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
def _reset_stale_on_boot():
    """On boot, no task is in memory, so any history entry still marked
    queued/processing is a leftover from a previous run that was interrupted by
    a restart. Mark them as errored automatically instead of waiting for someone
    to open the UI (audit finding #9)."""
    with _history_lock:
        history = _load_history()
        changed = 0
        for entry in history:
            if entry.get("status") in ("queued", "processing"):
                entry["status"] = "error"
                entry["error"]  = ("Transcrição interrompida — o servidor foi reiniciado "
                                   "durante o processamento. Envie o arquivo novamente.")
                changed += 1
        if changed:
            _atomic_write_json(HISTORY_FILE, history)

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _reset_stale_on_boot()
    # Cria auth.json na primeira execução e imprime as senhas geradas uma única
    # vez no terminal — não há como recuperá-las depois, só trocar.
    created = auth.ensure_initialized()
    if created:
        print("\n" + "═" * 62)
        print("  SENHAS DE ACESSO CRIADAS (anote — só aparecem desta vez)")
        for role, pw in created.items():
            label = "ADMIN (você)" if role == auth.ROLE_ADMIN else "FUNCIONÁRIOS (área pública)"
            print(f"    {label:<28} {pw}")
        print("  Troque quando quiser em Configurações → Área Pública.")
        print("═" * 62 + "\n")
    # Assinaturas: liga o módulo ao pipeline e sobe o poller de fundo.
    try:
        _configure_subscriptions()
        subscriptions.start_poller()
        storage.configure(em_uso=_active_task_for)
    except Exception as exc:   # noqa: BLE001 — nunca impedir o app de subir
        print(f"[subs] poller não iniciou: {exc}")
    yield
    subscriptions.stop_poller()

app = FastAPI(title="Whisper Transcritor", lifespan=_lifespan)

# Rotas que funcionam sem sessão — só o necessário para conseguir logar.
_OPEN_PATHS = {"/login", "/api/auth/login", "/api/auth/state", "/favicon.ico"}

def _is_open_path(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith("/static/")

@app.middleware("http")
async def _auth_and_csrf_guard(request: Request, call_next):
    """Porteiro único: CSRF + autenticação por sessão.

    CSRF (audit finding #3): em métodos que mudam estado, o Origin/Referer tem
    que apontar para o próprio host da requisição. O host vem do header Host —
    o navegador não deixa uma página em evil.com falsificar isso — então o app
    continua protegido depois de publicado num domínio/túnel, sem precisar
    manter uma lista fixa de domínios.

    Autenticação: sem cookie de sessão válido, nada é servido além de /login e
    dos estáticos. Não existe liberação por IP de propósito — atrás de um túnel
    (Tailscale Funnel) TODO request chega como 127.0.0.1, então confiar no IP
    daria acesso de administrador para a internet inteira."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        src = request.headers.get("origin") or request.headers.get("referer")
        if src:
            allowed = set(ALLOWED_ORIGIN_HOSTS)
            own_host = request.headers.get("host")
            if own_host:
                allowed.add(own_host)
            if urlparse(src).netloc not in allowed:
                return JSONResponse({"detail": "Origem não permitida (CSRF)"}, status_code=403)

    role = auth.role_for_token(request.cookies.get(auth.COOKIE_NAME))
    request.state.role = role
    path = request.url.path

    if role is None and not _is_open_path(path):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Não autenticado"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    # A aba Redes Sociais é ferramenta de administração (usa a sessão logada do
    # Instagram do dono via ego-lite) e não faz parte da Área Pública. O bloqueio
    # é por prefixo, aqui, e não endpoint por endpoint: assim um /api/social/*
    # novo já nasce fechado para a equipe, sem depender de ninguém lembrar.
    if role != auth.ROLE_ADMIN and path.startswith("/api/social/"):
        return JSONResponse({"detail": "Apenas o administrador pode fazer isso."},
                            status_code=403)

    return await call_next(request)

def _cookie_is_secure(request: Request) -> bool:
    """Marca o cookie como Secure quando a página está em HTTPS.

    Atrás do túnel o uvicorn vê http (o TLS termina no proxy), então olhamos o
    X-Forwarded-Proto. E se o acesso NÃO é local, tratamos como HTTPS de todo
    jeito: um túnel só serve https, e é justamente o caso em que o cookie
    precisa da proteção — se ficasse sem Secure, um downgrade para http
    conseguiria carregá-lo."""
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    if request.url.scheme == "https":
        return True
    host = (request.headers.get("host") or "").split(":")[0].lower()
    return host not in ("127.0.0.1", "localhost", "::1", "[::1]", "")

# ── Login / sessão ─────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def serve_login(request: Request):
    # Já logado? Não faz sentido mostrar o formulário.
    if getattr(request.state, "role", None):
        return RedirectResponse("/", status_code=303)
    with open(LOGIN_FILE, encoding="utf-8") as f:
        return f.read()

@app.get("/api/auth/state")
async def api_auth_state(request: Request):
    """Usado pela tela de login para avisar de bloqueio por tentativas erradas."""
    return {"authenticated": bool(getattr(request.state, "role", None)),
            "locked_for": auth.login_locked_for()}

@app.post("/api/auth/login")
async def api_auth_login(request: Request, password: str = Form(...)):
    locked = auth.login_locked_for()
    if locked:
        raise HTTPException(429, f"Muitas tentativas erradas. Tente de novo em "
                                 f"{max(1, locked // 60)} min.")
    role = auth.role_for_password(password or "")
    if not role:
        auth.record_login_failure()
        raise HTTPException(401, "Senha incorreta.")
    auth.record_login_success()
    token = auth.create_session(role)
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie(auth.COOKIE_NAME, token, max_age=auth.SESSION_TTL_SECS,
                    httponly=True, samesite="lax", secure=_cookie_is_secure(request),
                    path="/")
    return resp

@app.post("/api/auth/logout")
async def api_auth_logout(request: Request):
    auth.destroy_session(request.cookies.get(auth.COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp

@app.get("/api/me")
async def api_me(request: Request):
    """Quem sou eu — o frontend usa isso para decidir o que mostrar."""
    role = _role_of(request)
    return {
        "role":     role,
        "is_admin": role == auth.ROLE_ADMIN,
        "sessions": {"public": auth.active_session_count(auth.ROLE_PUBLIC),
                     "admin":  auth.active_session_count(auth.ROLE_ADMIN)}
                    if role == auth.ROLE_ADMIN else None,
    }

@app.post("/api/auth/password")
async def api_auth_set_password(request: Request,
                                target: str = Form(...),
                                password: str = Form(...)):
    """Troca a senha do admin ou da área pública. Só o admin pode.

    Trocar a senha derruba todas as sessões ativas daquele papel — é assim que
    se revoga o acesso de um funcionário que saiu."""
    _require_admin(request)
    if target not in (auth.ROLE_ADMIN, auth.ROLE_PUBLIC):
        raise HTTPException(400, "Alvo inválido")
    try:
        auth.set_password(target, password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    resp = JSONResponse({"ok": True, "target": target})
    if target == auth.ROLE_ADMIN:
        # A própria sessão do admin acabou de ser revogada — reemite uma nova
        # para ele não ser expulso da tela que acabou de usar.
        resp.set_cookie(auth.COOKIE_NAME, auth.create_session(auth.ROLE_ADMIN),
                        max_age=auth.SESSION_TTL_SECS, httponly=True, samesite="lax",
                        secure=_cookie_is_secure(request), path="/")
    return resp

@app.post("/api/auth/revoke-public")
async def api_auth_revoke_public(request: Request):
    """Desconecta todos os funcionários agora, sem trocar a senha."""
    _require_admin(request)
    return {"ok": True, "revoked": auth.destroy_sessions_for_role(auth.ROLE_PUBLIC)}

# ── Publicar / despublicar ─────────────────────────────────────
@app.post("/api/visibility")
async def api_set_visibility(request: Request,
                             files: str = Form(...),
                             visibility: str = Form(...)):
    """Marca itens como públicos ou privados. Só o admin.

    `files` é uma lista de nomes de arquivo separados por `\\n`. Os DOIS catálogos
    são atualizados para o mesmo arquivo: publicar uma transcrição sem publicar a
    mídia deixaria o funcionário lendo o texto mas sem conseguir abrir o vídeo."""
    _require_admin(request)
    if visibility not in (VIS_PUBLIC, VIS_PRIVATE):
        raise HTTPException(400, "Visibilidade inválida")
    wanted = {f.strip() for f in (files or "").split("\n") if f.strip()}
    if not wanted:
        raise HTTPException(400, "Nenhum arquivo informado")

    changed = 0
    with _history_lock:
        history = _load_history()
        touched = False
        for entry in history:
            if entry.get("file") in wanted and _vis_of(entry) != visibility:
                entry["visibility"] = visibility
                touched = True
                changed += 1
        if touched:
            _atomic_write_json(HISTORY_FILE, history)
    with _media_lock:
        media = _load_media()
        touched = False
        for entry in media:
            if entry.get("file") in wanted and _vis_of(entry) != visibility:
                entry["visibility"] = visibility
                touched = True
        if touched:
            _atomic_write_json(MEDIA_FILE, media)

    # Um item publicado no meio de uma transcrição ainda em curso: mantém a
    # intenção alinhada para as gravações que a thread ainda vai fazer.
    for f in wanted:
        _mark_pending_visibility(f, visibility)
    return {"ok": True, "visibility": visibility, "changed": changed}

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
    # O cache-busting acima cuida do CSS e do JS, mas o próprio HTML ficava
    # cacheável: o navegador servia uma cópia antiga da página com o JS novo.
    # Como a página é a fonte dos IDs que o JS procura, um elemento adicionado
    # depois (um modal, um painel) simplesmente não existia — e o código que o
    # buscava saía em silêncio, sem erro visível.
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, must-revalidate",
    })

# -- History & stats
@app.get("/api/history")
async def api_history(request: Request):
    """Returns the history with two computed fields injected per entry:
       - has_original: True if the original audio/video upload is still on disk.
                       Lets the UI badge each row as 'Original disponível' vs
                       'Original apagado' (e.g. after the 7-day cleanup ran).
       - source: how the entry first entered the system. Legacy rows without
                 a stored source default to 'upload' as a best-guess fallback
                 so the UI doesn't render a blank chip for them."""
    # Escopo por papel: um usuário público nunca recebe as linhas privadas —
    # o filtro é aqui no servidor, não no frontend.
    history = _scope_entries(_load_history(), _role_of(request))
    # Single fast listdir + set membership beats one stat per entry on big histories
    try:
        on_disk = set(os.listdir(UPLOAD_DIR))
    except OSError:
        on_disk = set()
    # Build a {file -> url} index from media.json so the UI can show/reopen the
    # original link, and so legacy entries (no stored source) can be classified
    # by whether they had a yt-dlp URL.
    media_url_map = {m.get("file"): m.get("url") for m in _load_media()}
    out = []
    for h in history:
        entry = dict(h)
        entry["has_original"] = entry.get("file") in on_disk
        url = media_url_map.get(entry.get("file"))
        if url:
            entry["url"] = url
        if not entry.get("source"):
            entry["source"] = "url" if url else "upload"
        # Normaliza para o frontend saber desenhar o selo "Pública" sem ter que
        # tratar o caso legado (campo ausente = privado).
        entry["visibility"] = _vis_of(entry)
        out.append(entry)
    return out

@app.get("/api/settings")
async def api_get_settings():
    """Returns current concurrency settings."""
    return _load_settings()

@app.post("/api/settings")
async def api_set_settings(
    request: Request,
    download_concurrent:   str = Form(None),
    transcribe_concurrent: str = Form(None),
    compress_concurrent:   str = Form(None),
):
    """Updates concurrency settings. Values are clamped to [1, 16].
    Changes take effect on the NEXT acquire of each semaphore — in-flight
    work isn't interrupted but new work respects the new limit.
    Admin-only: define a carga da máquina inteira, não de um usuário."""
    _require_admin(request)
    new = {}
    if download_concurrent   is not None: new["download_concurrent"]   = download_concurrent
    if transcribe_concurrent is not None: new["transcribe_concurrent"] = transcribe_concurrent
    if compress_concurrent   is not None: new["compress_concurrent"]   = compress_concurrent
    if not new:
        raise HTTPException(400, "Nenhuma configuração informada")
    return _save_settings(new)

# ── yt-dlp version check & self-update ──────────────────────────
# YouTube changes its player/signature logic often, and yt-dlp needs frequent
# updates to keep up — a stale yt-dlp is the #1 cause of "every YouTube
# download suddenly fails at once" (e.g. an internal logger/PO-token API
# mismatch inside a specific yt-dlp release). These endpoints let the UI
# surface that proactively and offer a one-click fix instead of the user
# discovering it only after downloads start failing silently.
_YTDLP_UPDATE_CHECK_CACHE: dict = {"checked_at": 0.0, "latest": None}
_YTDLP_UPDATE_CHECK_TTL   = 6 * 3600  # 6h — avoid hammering PyPI on every page load

def _ytdlp_installed_version() -> str | None:
    if not YT_DLP_OK:
        return None
    try:
        return yt_dlp.version.__version__
    except Exception:
        return None

def _version_tuple(v: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", v))

def _ytdlp_latest_version() -> str | None:
    """Best-effort PyPI lookup, cached for a few hours. Returns None (never
    raises) when offline or PyPI is unreachable — callers must treat None as
    'unknown', not 'up to date', so a network hiccup never triggers a false
    outdated warning."""
    now = time.time()
    cached = _YTDLP_UPDATE_CHECK_CACHE["latest"]
    if cached and (now - _YTDLP_UPDATE_CHECK_CACHE["checked_at"]) < _YTDLP_UPDATE_CHECK_TTL:
        return cached
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://pypi.org/pypi/yt-dlp/json",
            headers={"User-Agent": "whisper-transcritor-update-check"},
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = data.get("info", {}).get("version")
        if latest:
            _YTDLP_UPDATE_CHECK_CACHE["latest"]     = latest
            _YTDLP_UPDATE_CHECK_CACHE["checked_at"] = now
        return latest
    except Exception:
        return None

@app.get("/api/ytdlp/status")
async def api_ytdlp_status():
    """Reports installed vs latest yt-dlp version so the UI can warn the user
    proactively — an outdated yt-dlp is the most common cause of YouTube
    downloads suddenly failing across the board."""
    installed = _ytdlp_installed_version()
    latest    = _ytdlp_latest_version()
    outdated  = False
    if installed and latest:
        try:
            outdated = _version_tuple(installed) < _version_tuple(latest)
        except Exception:
            outdated = False
    return {"installed": installed, "latest": latest, "outdated": outdated}

@app.post("/api/ytdlp/update")
async def api_ytdlp_update(request: Request):
    """Upgrades yt-dlp (+ yt-dlp-ejs) in place via pip, in the same venv this
    server runs from. Takes effect only after the server restarts — the
    module already imported in this process stays on the old version until
    then — so the response makes that explicit for the UI to relay.
    Admin-only: mexe no venv do servidor."""
    _require_admin(request)
    import subprocess, sys
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "yt-dlp-ejs"],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Tempo esgotado ao atualizar — verifique sua conexão e tente de novo.")
    if result.returncode != 0:
        # yt-dlp-ejs exige Python 3.10+. Se o servidor foi iniciado com um
        # Python mais antigo (ex.: o python3 do sistema em vez do venv do
        # projeto), pip nunca vai achar uma distribuição compatível — dar o
        # motivo real em vez de despejar o stderr cru do pip, que não ajuda
        # quem não vai ler linha de comando.
        if sys.version_info < (3, 10):
            raise HTTPException(
                500,
                f"Este servidor está rodando com Python {sys.version_info.major}.{sys.version_info.minor} "
                f"({sys.executable}), mas o yt-dlp-ejs exige Python 3.10 ou mais novo. "
                "Feche o app e abra de novo usando o Python do venv do projeto "
                "(./venv/bin/python whisper-app.py) em vez do Python do sistema.",
            )
        raise HTTPException(500, f"Falha ao atualizar: {(result.stderr or result.stdout)[-500:]}")
    _YTDLP_UPDATE_CHECK_CACHE["latest"] = None  # força recheck na próxima consulta a /status
    return {"ok": True, "restart_required": True, "output": result.stdout[-800:]}

# ── Redes sociais (Instagram via ego-lite) ─────────────────────
# Coleta perfis/URLs com a sessão logada do ego-lite (motor do IGSorter, bem mais
# robusto que o yt-dlp para Instagram), mostra um mosaico 9:16 com metadados ricos
# e deixa o usuário escolher o que baixar e o que transcrever. O download cai no
# mesmo UPLOAD_DIR/media.json do app, então os itens aparecem na Biblioteca de
# Mídia e a transcrição reusa exatamente o pipeline já existente.

# Proxies de mídia só falam com CDNs de redes sociais — trava SSRF (nunca viram
# um proxy aberto para qualquer host). Multi-rede: capas de IG/FB/TikTok/YouTube/X.
_SOCIAL_CDN_SUFFIXES = ("cdninstagram.com", "fbcdn.net", "tiktokcdn.com",
                        "tiktokcdn-us.com", "ytimg.com", "twimg.com", "ggpht.com")

def _is_social_cdn(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    # Fronteira de domínio real: `evilfbcdn.net`/`xcdninstagram.com` NÃO passam
    # (host.endswith("fbcdn.net") passaria). Mesmo padrão de _host_allows_cookies.
    return any(host == s or host.endswith("." + s) for s in _SOCIAL_CDN_SUFFIXES)

def _require_social():
    if not SOCIAL_OK:
        raise HTTPException(500, "Módulo de redes sociais indisponível (falha ao importar social/).")

# Toda esta seção é exclusiva do administrador — o middleware fecha o prefixo
# /api/social/* para a Área Pública, então aqui não há escopo por papel a
# aplicar: quem chega já é admin.

@app.get("/api/social/status")
async def api_social_status():
    """Diz se o motor de coleta está pronto (ego-lite instalado)."""
    ego = SOCIAL_OK and social_collector.ego_available()
    return {"ok": SOCIAL_OK, "ego_browser": bool(ego)}

@app.post("/api/social/collect")
async def api_social_collect(
    username:   str = Form(...),
    max_posts:  int = Form(60),
    since_days: str = Form(""),   # "" = sem limite de período (só Instagram)
    platform:   str = Form("instagram"),
):
    """Coleta um perfil de rede social (roda em background via ego-lite).

    Instagram usa a API do feed (mais posts). TikTok/YouTube/Facebook usam
    interceptação de rede (tecnologia das extensões Sort Feed)."""
    _require_social()
    if not social_collector.ego_available():
        raise HTTPException(400, "ego lite não encontrado. Instale (https://lite.ego.app) e faça login na rede.")
    platform = (platform or "instagram").strip().lower()
    max_posts = max(1, min(int(max_posts or 60), 200))
    sd = int(since_days) if (since_days or "").strip().isdigit() else None

    if platform == "instagram":
        def _task(job, log):
            def prog(n, msg=None):
                if n is not None:
                    job["progress"] = {"collected": n, "target": max_posts}
                if msg:
                    log(msg)
            ds_id, path = social_collector.collect_profile(username, max_posts,
                                                           since_days=sd, on_progress=prog)
            return {"ds_id": ds_id}
        return {"job_id": social_jobs.start("collect", _task)}

    # TikTok / YouTube / Facebook: coleta por yt-dlp (medialink.probe) — lista o
    # canal/perfil e traz views/likes por vídeo. É bem mais confiável que a
    # interceptação para essas redes (o YouTube embute os dados no HTML, não em
    # fetch). A interceptação segue disponível internamente como reserva.
    _PROFILE_URL = {
        "tiktok":   "https://www.tiktok.com/@{u}",
        "youtube":  "https://www.youtube.com/@{u}/videos",
        "facebook": "https://www.facebook.com/{u}/videos",
    }
    if not SOCIAL_MULTI_OK or platform not in _PROFILE_URL:
        raise HTTPException(400, f"Rede não suportada: {platform}")
    target = username.strip()
    if target.startswith("http"):
        # Única rota de coleta que aceitava URL crua direto do formulário: sem
        # isto, um endereço interno (http://127.0.0.1:…) chegaria ao yt-dlp sem
        # passar pelo guard de SSRF que todas as outras rotas aplicam.
        prof_url = _validate_media_url(target)
    else:
        prof_url = _PROFILE_URL[platform].format(u=target.lstrip("@"))

    def _task_multi(job, log):
        def prog(done, total):
            job["progress"] = {"collected": done, "target": total or max_posts}
        res = social_medialink.probe(prof_url, max_items=max_posts, on_progress=prog)
        if not res.get("rows"):
            raise RuntimeError("nenhum vídeo encontrado (perfil privado, vazio ou exige login).")
        ds_id, path = social_medialink.save_dataset(res)
        return {"ds_id": ds_id}

    return {"job_id": social_jobs.start("collect", _task_multi)}

@app.post("/api/social/collect-urls")
async def api_social_collect_urls(urls: str = Form(...)):
    """Resolve uma lista de URLs de posts/reels individuais do Instagram."""
    _require_social()
    if not social_collector.ego_available():
        raise HTTPException(400, "ego lite não encontrado. Instale (https://lite.ego.app) e faça login no Instagram.")
    url_list = [u.strip() for u in re.split(r"[\n,;]+", urls or "") if u.strip()]
    if not url_list:
        raise HTTPException(400, "Cole ao menos uma URL de post/reel do Instagram.")
    try:
        social_collector.parse_instagram_urls(url_list)  # valida cedo
    except ValueError as e:
        raise HTTPException(400, str(e))

    def _task(job, log):
        def prog(n, msg=None):
            if n is not None:
                job["progress"] = {"collected": n, "target": len(url_list)}
            if msg:
                log(msg)
        ds_id, path = social_collector.resolve_urls(url_list, on_progress=prog)
        return {"ds_id": ds_id}

    return {"job_id": social_jobs.start("collect-urls", _task)}

@app.get("/api/social/job/{job_id}")
async def api_social_job(job_id: str):
    _require_social()
    j = social_jobs.get(job_id)
    if not j:
        raise HTTPException(404, "job não encontrado")
    return {"id": j["id"], "kind": j["kind"], "status": j["status"],
            "progress": j["progress"], "log": j["log"][-8:],
            "result": j["result"], "error": j["error"]}

@app.get("/api/social/datasets")
async def api_social_datasets():
    _require_social()
    return social_core.list_datasets()

@app.get("/api/social/dataset/{ds_id}")
async def api_social_dataset(ds_id: str):
    _require_social()
    try:
        ds = social_core.load_dataset(social_core.dataset_path(ds_id))
    except FileNotFoundError:
        raise HTTPException(404, "coleta não encontrada")
    return {"profile": ds["profile"], "collected_at": ds["collected_at"],
            "rows": ds["rows"], "trends": social_core.build_trends(ds["rows"])}

@app.get("/api/social/thumb")
def api_social_thumb(url: str):
    """Proxy + cache de capa (o CDN do IG às vezes bloqueia hotlink direto)."""
    _require_social()
    if not _is_social_cdn(url):
        raise HTTPException(400, "host não permitido")
    import hashlib
    key = hashlib.sha1(url.encode()).hexdigest() + ".jpg"
    path = os.path.join(social_core.CACHE_DIR, key)
    if not (os.path.isfile(path) and os.path.getsize(path) > 0):
        try:
            social_downloader.download_media(url, path, timeout=15)
        except Exception:
            raise HTTPException(502, "não foi possível baixar a capa")
    return FileResponse(path, media_type="image/jpeg")

@app.get("/api/social/media")
def api_social_media(url: str, request: Request):
    """Proxy com suporte a Range para pré-visualizar o vídeo no hover do card."""
    _require_social()
    if not _is_social_cdn(url):
        raise HTTPException(400, "host não permitido")
    headers = {"User-Agent": social_downloader.UA}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    try:
        # allow_redirects=False: o host já foi validado; sem isso um 302 poderia
        # apontar para um alvo interno (SSRF). Links do CDN do IG são diretos.
        r = requests.get(url, headers=headers, stream=True, timeout=30, allow_redirects=False)
    except Exception:
        raise HTTPException(502, "falha ao buscar mídia")
    if r.status_code in (301, 302, 303, 307, 308):
        raise HTTPException(502, "redirecionamento não permitido")
    out_headers = {"Cache-Control": "public, max-age=3600"}
    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if h in r.headers:
            out_headers[h] = r.headers[h]
    return StreamingResponse(r.iter_content(1 << 16), status_code=r.status_code,
                             headers=out_headers)

# -- Download + (opcional) transcrição dos itens selecionados no mosaico
def _social_nice_name(row: dict) -> str:
    cap = (row.get("caption") or "").strip()
    first = next((ln.strip() for ln in cap.splitlines() if ln.strip()), "")
    if first:
        return first[:70]
    user = row.get("username") or "instagram"
    return f"@{user} {row.get('code') or ''}".strip()

def _media_utilizavel(path, precisa_audio=True):
    """Confere com ffprobe que o arquivo é mídia decodificável. Devolve
    (ok, motivo). Existe porque um .mp4 de 0 byte ou truncado passava direto
    para o Whisper e só explodia lá dentro, com um traceback de ffmpeg que
    não diz ao usuário o que fazer ("moov atom not found").
    """
    if not os.path.isfile(path):
        return False, "o arquivo não existe"
    tam = os.path.getsize(path)
    if tam < 1024:
        return False, f"o arquivo tem só {tam} bytes — o download não completou"
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return True, ""          # sem ffprobe não dá para checar; segue o baile
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return True, ""
    if out.returncode != 0:
        motivo = (out.stderr or "").strip().splitlines()
        detalhe = motivo[-1] if motivo else "formato não reconhecido"
        return False, f"o arquivo não é um vídeo/áudio válido ({detalhe})"
    tipos = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    if precisa_audio and "audio" not in tipos:
        return False, "o arquivo não tem faixa de áudio para transcrever"
    return True, ""


def _social_baixar(cdn_url, fpath, post_url, log, code):
    """Baixa a mídia de um post. Duas tentativas, nesta ordem:

      1. URL direta do CDN que veio na coleta (rápido);
      2. se falhar, reabre o post no navegador logado do ego lite para
         RE-RESOLVER uma URL assinada nova e baixa de novo.

    O passo 2 existe porque o link assinado do Instagram expira em poucas
    horas: coletar de manhã e mandar transcrever à tarde caía sempre no erro.
    """
    erro1 = None
    try:
        return social_downloader.download_media(cdn_url, fpath)
    except Exception as e:
        erro1 = e
        log(f"{code}: link da coleta falhou ({e}); tentando reabrir o post no ego lite")

    if not (post_url and SOCIAL_MULTI_OK and social_collector.ego_available()):
        raise RuntimeError(
            f"{erro1}. Sem ego lite disponível para reabrir o post e pegar um link novo.")

    try:
        achado = social_intercept.resolve_media(post_url)
    except Exception as e2:
        raise RuntimeError(f"{erro1}. Reabrir o post também falhou: {e2}")

    quer_video = fpath.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
    nova = achado.get("video") if quer_video else achado.get("image")
    nova = nova or achado.get("video") or achado.get("image")
    if not nova:
        raise RuntimeError(f"{erro1}. O post não tem mídia acessível (privado ou removido).")
    tam = social_downloader.download_media(nova, fpath)
    log(f"{code}: recuperado com link novo do ego lite")
    return tam


def _social_start_transcription(file_path, filename, name, url, model,
                                language, task, filter_fillers, folder):
    """Dispara transcrição de um arquivo social JÁ baixado no disco — mesmo
    pipeline do retry 'arquivo já presente' (linha do _retry_item)."""
    task_id = str(uuid.uuid4())
    _set_task(task_id, status="queued", progress=0, name=name or filename, filename=filename)
    _save_to_history(filename, {}, model, status="queued", task_id=task_id,
                     original_name=name, folder=folder, source="social",
                     task_type=task, filter_fillers=filter_fillers)
    _save_media(filename, name, url=url, is_transcribed=True, status="queued", force_name=True)
    threading.Thread(target=_run_transcription,
                     args=(task_id, file_path, filename, model, language, task, filter_fillers),
                     daemon=True).start()
    return task_id

@app.post("/api/social/fetch")
async def api_social_fetch(
    ds_id:            str = Form(...),
    download_codes:   str = Form("[]"),   # JSON: shortcodes só p/ baixar
    transcribe_codes: str = Form("[]"),   # JSON: shortcodes p/ baixar + transcrever
    model:            str = Form("turbo"),
    language:         str = Form("pt"),
    task:             str = Form("transcribe"),
    filter_fillers:   str = Form("false"),
    folder:           str = Form(""),
    include_meta:     str = Form("false"),
):
    """Baixa a mídia HD dos posts selecionados para a Biblioteca de Mídia e, para
    os marcados como 'transcrever', emenda direto no pipeline de transcrição.
    Roda em background e devolve um job_id para acompanhar o progresso."""
    _require_social()
    want_meta = include_meta == "true"
    try:
        dl_codes = set(json.loads(download_codes) or [])
        tr_codes = set(json.loads(transcribe_codes) or [])
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "listas de seleção inválidas")
    all_codes = dl_codes | tr_codes
    if not all_codes:
        raise HTTPException(400, "Nenhum item selecionado.")

    try:
        ds = social_core.load_dataset(social_core.dataset_path(ds_id))
    except FileNotFoundError:
        raise HTTPException(404, "coleta não encontrada")

    do_filter = filter_fillers == "true"
    folder = _validate_folder_name(folder) if folder else ""
    if tr_codes:
        _ensure_folder_tree(folder)

    by_code = {r["code"]: r for r in ds["rows"] if r.get("code")}
    jobs_list = []  # (code, row, want_transcribe)
    for code in all_codes:
        row = by_code.get(code)
        if row:
            jobs_list.append((code, row, code in tr_codes))

    def _task(job, log):
        downloaded = failed = transcribing = skipped_no_video = 0
        task_ids, media_files = [], []
        total = len(jobs_list)
        for i, (code, row, want_tr) in enumerate(jobs_list, 1):
            job["progress"] = {"done": i - 1, "target": total}
            medias = [m for m in row.get("media_urls", []) if m.get("url")]
            platform = (row.get("platform") or "Instagram")
            is_ig = (platform == "Instagram")
            if is_ig and not medias:
                failed += 1
                log(f"{code}: sem mídia")
                continue
            name = _social_nice_name(row)
            base = f"ig_{re.sub(r'[^A-Za-z0-9_-]', '', code)}"
            if want_meta:
                # Sidecar ao lado da mídia (não polui media.json, que lê só o índice):
                # legenda.txt + meta.json com likes/views/ER/hashtags, como no IGSorter.
                try:
                    with open(os.path.join(UPLOAD_DIR, base + ".legenda.txt"), "w", encoding="utf-8") as f:
                        f.write(row.get("caption") or "")
                    meta = {k: row.get(k) for k in ("code", "url", "type", "date", "likes",
                            "comments", "reshares", "views", "er", "duration_s", "hashtags", "username")}
                    meta["thumb_url"] = row.get("thumb_url")
                    with open(os.path.join(UPLOAD_DIR, base + ".meta.json"), "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
                # Capa como sidecar. O cache de thumbs existe, mas é indexado por
                # hash da URL — a partir do arquivo de mídia não há como chegar
                # nele. Sem gravar aqui, a capa fica inalcançável no download.
                thumb = row.get("thumb_url")
                if thumb:
                    capa = os.path.join(UPLOAD_DIR, base + ".capa.jpg")
                    if not (os.path.exists(capa) and os.path.getsize(capa) > 0):
                        try:
                            social_downloader.download_media(thumb, capa, timeout=20)
                        except Exception:
                            pass   # capa é acessório: nunca derruba o download da mídia
            downloaded_items = []  # (filename, fpath, is_video)
            if is_ig:
                for n, m in enumerate(medias, 1):
                    ext = ".mp4" if m["type"] == "video" else ".jpg"
                    suffix = f"_{n}" if len(medias) > 1 else ""
                    filename = f"{base}{suffix}{ext}"
                    # A coleta é sempre do admin, então a mídia baixada nasce privada
                    # (ele publica depois o que a equipe pode ver).
                    _mark_pending_visibility(filename, VIS_PRIVATE)
                    fpath = os.path.join(UPLOAD_DIR, filename)
                    try:
                        if not (os.path.exists(fpath) and os.path.getsize(fpath) > 0):
                            _social_baixar(m["url"], fpath, row.get("url"), log, code)
                        # Confere o resultado SEMPRE, inclusive quando o arquivo já
                        # existia: um 0 byte de execução anterior não pode virar
                        # "sucesso" e seguir para a transcrição.
                        ok, motivo = _media_utilizavel(fpath, precisa_audio=False)
                        if not ok:
                            try:
                                os.remove(fpath)
                            except OSError:
                                pass
                            raise RuntimeError(motivo)
                        downloaded += 1
                        log(f"{code}: {filename}")
                    except Exception as e:
                        failed += 1
                        log(f"{code}: erro {e}")
                        continue
                    downloaded_items.append((filename, fpath, m["type"] == "video"))
            else:
                # TikTok/YouTube/Facebook: a interceptação não dá uma URL de CDN
                # direta e utilizável, então baixa pelo próprio link do post via
                # medialink (yt-dlp + plano B do navegador logado).
                fn = f"{base}.mp4"
                _mark_pending_visibility(fn, VIS_PRIVATE)
                fpath = os.path.join(UPLOAD_DIR, fn)
                if not (SOCIAL_MULTI_OK and row.get("url")):
                    failed += 1
                    log(f"{code}: sem forma de baixar (rede {platform})")
                else:
                    tmpdir = tempfile.mkdtemp(dir=UPLOAD_DIR)
                    try:
                        if not (os.path.exists(fpath) and os.path.getsize(fpath) > 0):
                            res = social_medialink.download(row["url"], dest=tmpdir, on_progress=None)
                            os.replace(res["file"], fpath)
                        ok, motivo = _media_utilizavel(fpath, precisa_audio=False)
                        if not ok:
                            try:
                                os.remove(fpath)
                            except OSError:
                                pass
                            raise RuntimeError(motivo)
                        downloaded += 1
                        log(f"{code}: {fn}")
                        downloaded_items.append((fn, fpath, True))
                    except Exception as e:
                        failed += 1
                        log(f"{code}: erro {e}")
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)

            # O que vai para a transcrição (se pedido): o primeiro vídeo baixado.
            transcribe_item = next((it for it in downloaded_items if it[2]), None) if want_tr else None
            if transcribe_item:
                fpath, filename = transcribe_item[1], transcribe_item[0]
                # Última barreira antes do Whisper: sem faixa de áudio a
                # transcrição só produziria um erro de ffmpeg incompreensível.
                ok, motivo = _media_utilizavel(fpath, precisa_audio=True)
                if not ok:
                    failed += 1
                    log(f"{code}: não enviado para transcrição — {motivo}")
                else:
                    tid = _social_start_transcription(fpath, filename, name, row.get("url"),
                                                      model, language, task, do_filter, folder)
                    task_ids.append(tid)
                    transcribing += 1
            elif want_tr:
                skipped_no_video += 1
                log(f"{code}: sem vídeo p/ transcrever")

            # Registra em media.json TODOS os baixados — menos o que foi p/ a
            # transcrição (esse é registrado por _social_start_transcription).
            # Cobre carrosséis com vários vídeos: sem isso, os vídeos além do
            # primeiro ficariam órfãos no disco (fora do media.json).
            for filename, fpath, is_video in downloaded_items:
                if transcribe_item and filename == transcribe_item[0]:
                    continue
                _save_media(filename, name, url=row.get("url"), is_transcribed=False, status="done")
                media_files.append(filename)
            time.sleep(0.3)  # respiro entre downloads
        job["progress"] = {"done": total, "target": total}
        return {"downloaded": downloaded, "failed": failed,
                "transcribing": transcribing, "skipped_no_video": skipped_no_video,
                "task_ids": task_ids, "media_files": media_files}

    return {"job_id": social_jobs.start("fetch", _task)}

@app.post("/api/social/export")
async def api_social_export(ds_id: str = Form(...), sort: str = Form("er")):
    """Gera Excel (+ CSV) da coleta, com aba de Tendências. Miniaturas entram só
    se o Pillow estiver instalado — sem ele, sai sem thumbs (resto igual)."""
    _require_social()
    if not SOCIAL_EXCEL_OK:
        raise HTTPException(500, "Exportação indisponível (openpyxl não instalado).")
    try:
        ds = social_core.load_dataset(social_core.dataset_path(ds_id))
    except FileNotFoundError:
        raise HTTPException(404, "coleta não encontrada")
    res = social_excel.export_excel(ds, sort=sort, thumbs=social_excel.thumbs_supported())
    return {"excel": os.path.basename(res["excel"]),
            "csv": os.path.basename(res.get("csv", "")) if res.get("csv") else None,
            "thumbs": social_excel.thumbs_supported()}

@app.post("/api/social/comments")
async def api_social_comments(
    ds_id:        str = Form(...),
    codes:        str = Form(...),      # JSON com os códigos selecionados
    max_comments: int = Form(300),      # teto por post
):
    """Baixa os comentários dos posts selecionados (roda em background).

    Abre cada post no ego lite e lê as respostas de comentário que a própria
    rede busca — mesma técnica da coleta de feed. Gera um CSV (+ JSON) em
    EXPORT_DIR, baixável por /api/social/export-file/."""
    _require_social()
    if not SOCIAL_COMMENTS_OK:
        raise HTTPException(500, "Coleta de comentários indisponível (falha ao importar social/comments.py).")
    if not social_collector.ego_available():
        raise HTTPException(400, "ego lite não encontrado — instale-o para coletar comentários.")
    try:
        code_list = [str(c) for c in (json.loads(codes) or [])]
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(400, "lista de seleção inválida")
    if not code_list:
        raise HTTPException(400, "Nenhum post selecionado.")
    if len(code_list) > 25:
        raise HTTPException(400, "Selecione no máximo 25 posts por vez (cada post abre o navegador).")
    max_comments = max(10, min(int(max_comments), 5000))

    try:
        ds = social_core.load_dataset(social_core.dataset_path(ds_id))
    except FileNotFoundError:
        raise HTTPException(404, "coleta não encontrada")
    by_code = {r["code"]: r for r in ds["rows"] if r.get("code")}
    posts = [by_code[c] for c in code_list if c in by_code]
    if not posts:
        raise HTTPException(400, "Os posts selecionados não estão nesta coleta.")

    def _task(job, log):
        def on_post(i, total, code):
            job["progress"] = {"done": i - 1, "target": total}
            log(f"Lendo comentários de {code} ({i}/{total})…")

        def on_progress(n, msg=None):
            if n is not None:
                job["progress"] = dict(job.get("progress") or {}, comments=n)
            elif msg:
                log(msg)

        res = social_comments.collect_for_posts(
            posts, max_comments=max_comments, on_progress=on_progress,
            on_post=on_post, log=log)
        log(f"{res['count']} comentário(s) de {res['posts']} post(s).")
        return res

    return {"job_id": social_jobs.start("comments", _task)}

@app.get("/api/social/export-file/{name}")
async def api_social_export_file(name: str):
    """Baixa uma planilha já gerada (só arquivos dentro de EXPORT_DIR)."""
    _require_social()
    safe = os.path.basename(name)
    path = os.path.join(social_core.EXPORT_DIR, safe)
    if not os.path.isfile(path):
        raise HTTPException(404, "arquivo não encontrado")
    low = safe.lower()
    media = "text/csv" if low.endswith(".csv") else \
            "application/json" if low.endswith(".json") else \
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=safe)

@app.get("/api/stats")
async def api_stats(request: Request):
    # Os números do topo da tela têm que bater com a lista que o usuário vê.
    history = _scope_entries(_load_history(), _role_of(request))
    total_secs = sum(h.get("duration_secs", 0) for h in history)
    h, rem = divmod(int(total_secs), 3600)
    m      = rem // 60
    return {
        "total":    len(history),
        "duration": f"{h}h {m}m" if h else (f"{m}m" if m else "0m"),
    }

# -- Media
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".mpeg", ".mpg", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma", ".flac"}

def _media_type_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower()
    if ext in _VIDEO_EXTS: return "video"
    if ext in _AUDIO_EXTS: return "audio"
    return "other"

@app.get("/api/media-history")
async def api_media_history(request: Request):
    """Returns the media catalog enriched with live disk state:
       - on_disk: whether the original upload file is still physically present
         (the Biblioteca de Mídia tab only lists what's actually still on disk)
       - size_bytes: recomputed from disk when on_disk — the stored value is
         only a snapshot from when the file was first saved, and can go stale
       - type: 'audio' | 'video' | 'other', derived from the extension, so the
         UI can filter without re-deriving it per row"""
    media = _scope_entries(_load_media(), _role_of(request))
    try:
        on_disk_set = set(os.listdir(UPLOAD_DIR))
    except OSError:
        on_disk_set = set()
    out = []
    for m in media:
        entry = dict(m)
        filename = entry.get("file") or ""
        on_disk = filename in on_disk_set
        entry["on_disk"] = on_disk
        if on_disk:
            try:
                entry["size_bytes"] = os.path.getsize(os.path.join(UPLOAD_DIR, filename))
            except OSError:
                pass
        entry["type"] = _media_type_for(filename)
        entry["visibility"] = _vis_of(entry)
        out.append(entry)
    return out

@app.get("/api/download-media/{filename}")
async def api_download_media(filename: str, request: Request):
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Mídia não encontrada no servidor")
    return FileResponse(path, filename=_original_media_name_for(filename))

_SIDECAR_EXTS = (".legenda.txt", ".meta.json", ".capa.jpg")

def _sidecars_for(filename: str) -> list[tuple[str, str]]:
    """Sidecars de metadados gravados ao lado de uma mídia social baixada com a
    opção "Metadados": legenda, métricas (likes/views/ER/hashtags) e capa.

    Retorna [(caminho_no_disco, sufixo)] — o sufixo é anexado ao nome de
    exibição pelo chamador, para que no ZIP a legenda fique ao lado do vídeo
    com o mesmo nome-base e a relação entre eles seja óbvia.

    Num carrossel (`<base>_1.mp4`, `<base>_2.mp4`) os sidecars são do POST, não
    de cada arquivo — por isso o sufixo `_N` é removido para achar a base."""
    stem = os.path.splitext(filename)[0]
    if not stem.startswith("ig_"):
        return []
    base = re.sub(r"_\d+$", "", stem)
    out = []
    for ext in _SIDECAR_EXTS:
        p = os.path.join(UPLOAD_DIR, base + ext)
        if os.path.isfile(p):
            out.append((p, ext))
    return out

@app.post("/api/download-media-zip")
async def api_download_media_zip(request: Request, files: str = Form(...),
                                 include_meta: str = Form("true")):
    """Baixa em um único ZIP os originais selecionados na Biblioteca de Mídia.

    Sem isto, "baixar vários" só poderia disparar um download por arquivo — o
    navegador bloqueia downloads múltiplos automáticos depois dos primeiros, e
    o usuário terminaria com parte da seleção sem perceber."""
    try:
        filenames = json.loads(files)
        if not isinstance(filenames, list):
            raise ValueError("files deve ser uma lista")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Parâmetro 'files' inválido: {e}")
    if not filenames:
        raise HTTPException(400, "Nenhum arquivo selecionado")

    uploads_real = os.path.realpath(UPLOAD_DIR)
    entries = []
    for fn in filenames:
        if not isinstance(fn, str):
            raise HTTPException(400, "Cada item de 'files' deve ser string")
        fn = _safe_filename(fn)
        # Mesma regra da rota individual: um item privado no meio da seleção é
        # pedido forjado, então derruba o ZIP inteiro em vez de sair de fora.
        _require_file_access(fn, request)
        path = os.path.join(UPLOAD_DIR, fn)
        if os.path.commonpath([os.path.realpath(path), uploads_real]) != uploads_real:
            continue
        if os.path.isfile(path):
            entries.append((fn, path, _original_media_name_for(fn)))

    if not entries:
        raise HTTPException(404, "Nenhum dos arquivos selecionados está no servidor")

    # Nomes de exibição podem repetir (dois posts com a mesma legenda viram o
    # mesmo nome); o ZIP é plano, então desempata com " (2)", " (3)"…
    used: set[str] = set()
    def _unique(name: str) -> str:
        if name not in used:
            used.add(name)
            return name
        stem, ext = os.path.splitext(name)
        n = 2
        while f"{stem} ({n}){ext}" in used:
            n += 1
        out = f"{stem} ({n}){ext}"
        used.add(out)
        return out

    zip_name = f"midias_selecionadas_{uuid.uuid4().hex[:8]}.zip"
    zip_path = os.path.join(DATA_DIR, zip_name)
    # ZIP_STORED, não DEFLATED: vídeo/áudio já vêm comprimidos, então deflate
    # gastaria CPU (e tempo, em seleções de centenas de MB) para ganhar ~0%.
    want_meta = str(include_meta).lower() != "false"
    # Sidecars são do POST, não de cada arquivo: num carrossel os dois vídeos
    # compartilham a mesma legenda/capa. Sem este controle, o mesmo .meta.json
    # entraria duas vezes (como "… (2)"), sugerindo metadados diferentes.
    sidecars_feitos: set[str] = set()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for fn, path, display in entries:
            stem_exibicao = os.path.splitext(_unique(display))[0]
            zf.write(path, stem_exibicao + os.path.splitext(display)[1])
            if not want_meta:
                continue
            for sc_path, sc_ext in _sidecars_for(fn):
                if sc_path in sidecars_feitos:
                    continue
                sidecars_feitos.add(sc_path)
                # Mesmo nome-base do vídeo + sufixo do sidecar, para que legenda,
                # métricas e capa fiquem visivelmente atreladas ao arquivo certo.
                zf.write(sc_path, _unique(stem_exibicao + sc_ext))

    return FileResponse(zip_path, filename="midias_selecionadas.zip",
                        background=BackgroundTask(_safe_remove, zip_path))

def _cleanup_social_sidecars(filename: str) -> None:
    """Ao deletar uma mídia social (ig_<code>[_N].ext), remove os sidecars
    ig_<code>.legenda.txt / .meta.json / .capa.jpg quando não sobra mais nenhuma
    mídia do mesmo post — senão os metadados ficariam órfãos no disco.

    A capa é `<base>.capa.jpg`, cujo stem (`<base>.capa`) não casa com o padrão
    de mídia `<base>(_N)?` testado abaixo — então ela nunca é confundida com um
    item de carrossel que ainda restaria."""
    stem = os.path.splitext(filename)[0]
    if not stem.startswith("ig_"):
        return
    base = re.sub(r"_\d+$", "", stem)  # tira sufixo _1/_2 de carrossel
    try:
        remaining = any(
            f.startswith(base) and os.path.splitext(f)[1].lower() in (_VIDEO_EXTS | _AUDIO_EXTS | {".jpg", ".jpeg", ".png"})
            and re.fullmatch(re.escape(base) + r"(_\d+)?", os.path.splitext(f)[0])
            for f in os.listdir(UPLOAD_DIR)
        )
    except OSError:
        remaining = True
    if not remaining:
        for ext in (".legenda.txt", ".meta.json", ".capa.jpg"):
            p = os.path.join(UPLOAD_DIR, base + ext)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

@app.delete("/api/delete-media/{filename}")
async def api_delete_media(filename: str, request: Request):
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    if _active_task_for(filename):
        raise HTTPException(409, "Este arquivo está em uso agora. "
                                 "Cancele a tarefa antes de excluir.")
    path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as e:
            raise HTTPException(500, f"Erro ao deletar: {e}")
    _cleanup_social_sidecars(filename)

    with _media_lock:
        media = _load_media()
        media = [m for m in media if m.get("file") != filename]
        _atomic_write_json(MEDIA_FILE, media)
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
        # Nunca listar arquivo em uso. `queued_at` é PRESERVADO num retry, então
        # um arquivo antigo re-enfileirado hoje continua parecendo velho aqui —
        # e a faxina apagava o original no meio do model.transcribe().
        if entry.get("status") in _ACTIVE_STATES or _active_task_for(filename):
            continue
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
async def api_media_older_than(request: Request, days: float = 7.0):
    """List media files older than N days (default 7) so the UI can prompt
    cleanup. Transcriptions are NEVER included — only the audio/video originals
    in uploads/ that have aged past the retention window.
    Admin-only: a faxina varre o disco inteiro, incluindo itens privados."""
    _require_admin(request)
    days = max(0.0, float(days))
    return _audit_old_media(days)

@app.post("/api/media/cleanup")
async def api_media_cleanup(request: Request, files: str = Form(...)):
    """Bulk-delete a list of media uploads (audio/video originals only).
    `files` is a comma-separated list of safe filenames. Transcriptions linked
    to these files are preserved — they keep working from results/.
    Admin-only: é uma exclusão em massa sobre todo o acervo."""
    _require_admin(request)
    requested = [_safe_filename(f.strip()) for f in (files or "").split(",") if f.strip()]
    if not requested:
        raise HTTPException(400, "Nenhum arquivo informado")

    deleted = 0
    freed_bytes = 0
    failed: list[str] = []
    skipped_active: list[str] = []
    for filename in requested:
        # Segunda linha de defesa (a primeira é _audit_old_media não listar):
        # a exclusão em lote da Biblioteca chega aqui direto, sem passar por lá.
        if _active_task_for(filename):
            skipped_active.append(filename)
            continue
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

    # Drop those entries from media.json (transcriptions stay in history.json untouched).
    # Os pulados por estarem em uso ficam FORA desta remoção: tirar a entrada do
    # catálogo sem apagar o arquivo deixaria o item invisível na tela e o disco
    # ocupado do mesmo jeito.
    removidos = set(requested) - set(skipped_active)
    with _media_lock:
        media = [m for m in _load_media() if m.get("file") not in removidos]
        _atomic_write_json(MEDIA_FILE, media)

    return {"deleted": deleted, "freed_bytes": freed_bytes, "failed": failed,
            "skipped_active": skipped_active}


# -- Transcription
@app.post("/api/transcribe")
async def api_transcribe(
    request:         Request,
    background_tasks: BackgroundTasks,
    file:            UploadFile = File(...),
    model:           str        = Form("turbo"),
    language:        str        = Form("pt"),
    task:            str        = Form("transcribe"),
    filter_fillers:  str        = Form("false"),
    folder:          str        = Form(""),
):
    _validate_transcribe_params(model, task)
    original_name = file.filename or f"audio_{uuid.uuid4().hex[:8]}.mp3"
    task_id  = str(uuid.uuid4())
    # O nome de exibição (original_name) é preservado como veio. Já o nome que vai
    # para o disco tem separadores e caracteres de controle trocados por "_": o
    # prefixo aleatório já barrava traversal na prática, mas sanitizar é defesa
    # bem menos frágil e ainda evita que um "\n" no nome contamine as listas
    # separadas por quebra de linha usadas nas operações em lote.
    safe_stem = re.sub(r"[/\\\x00-\x1f]+", "_", original_name).strip() \
                or f"audio_{uuid.uuid4().hex[:8]}.mp3"
    filename = f"{task_id[:8]}_{safe_stem}"
    # Enviado por funcionário nasce público; enviado pelo admin nasce privado.
    _mark_pending_visibility(filename, _visibility_for_new(request))

    # Validate/normalize destination folder (auto-create ancestors so the tree shows it)
    folder = _validate_folder_name(folder) if folder else ""
    _ensure_folder_tree(folder)

    # Stream the upload to disk in chunks (never buffer the whole file in RAM)
    # and enforce a size ceiling — prevents memory exhaustion (audit finding #2).
    upload_path = os.path.join(UPLOAD_DIR, filename)
    size = 0
    try:
        with open(upload_path, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413, f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024*1024)} MB")
                f.write(chunk)
    except HTTPException:
        _safe_remove(upload_path)   # drop the partial upload
        raise
    except Exception as e:
        _safe_remove(upload_path)
        raise HTTPException(500, f"Erro ao salvar upload: {e}")

    _set_task(task_id, status="queued", progress=0,
              name=original_name, filename=filename)

    # Register immediately in history so it survives refresh
    _save_to_history(filename, {}, model, status="queued", task_id=task_id, original_name=_result_base(original_name),
                     folder=folder, source="upload", task_type=task, filter_fillers=(filter_fillers == "true"))
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
async def api_progress(task_id: str, request: Request):
    # task_id is a UUID; reject anything else to prevent abuse
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # O progresso carrega nome do arquivo e erros — mesmo escopo do item.
    _require_file_access(task.get("filename") or "", request)
    return task

@app.post("/api/transcribe/{task_id}/pause")
async def api_pause_download(task_id: str, request: Request):
    """Pausa um download em andamento, preservando o que já baixou.

    Só vale para download: uma transcrição é uma única chamada ao Whisper que
    não dá para interromper no meio (só cancelar, descartando o resultado).
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _require_file_access(task.get("filename") or "", request)
    if task.get("status") not in ("queued", "processing"):
        raise HTTPException(400, "Só dá para pausar um download em andamento.")
    if task.get("phase") not in ("download", None, ""):
        raise HTTPException(400, "Esta etapa não pode ser pausada — só o download.")
    _set_task(task_id, pause_requested=True)
    return {"ok": True, "message": "Pausando… o download para no próximo trecho."}

@app.post("/api/transcribe/{task_id}/resume")
async def api_resume_download(task_id: str, request: Request):
    """Retoma um download pausado, reaproveitando os arquivos parciais."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        # Depois de um restart do servidor a task some da memória (e com ela os
        # dados do resume). O item continua no acervo: reenviar pela tela de
        # download recomeça — os .part ainda no disco são reaproveitados.
        raise HTTPException(404, "Este download não está mais na fila do servidor. "
                                 "Envie a URL de novo — o que já baixou é reaproveitado.")
    _require_file_access(task.get("filename") or "", request)
    if task.get("status") != "paused":
        raise HTTPException(400, "Este download não está pausado.")
    args = task.get("resume_args") or {}
    if not args.get("url"):
        raise HTTPException(400, "Não há dados suficientes para retomar este download.")

    _set_task(task_id, status="queued", phase="download", pause_requested=False,
              name="Retomando download…")
    kwargs = {k: v for k, v in args.items() if k not in ("url", "media_type", "quality")}
    # Avisa a cascata de motores que os parciais no disco são para APROVEITAR,
    # não para limpar — do contrário a retomada apagaria o que a pausa guardou.
    kwargs["resuming"] = True
    threading.Thread(target=_run_download_only,
                     args=(task_id, args["url"], args.get("media_type", "video"),
                           args.get("quality", "best")),
                     kwargs=kwargs, daemon=True).start()
    return {"ok": True, "message": "Retomando de onde parou."}

@app.delete("/api/transcribe/{task_id}")
async def api_cancel_transcribe(task_id: str, request: Request):
    """Request cancellation of any task (download-only, URL→transcribe, or
    file→transcribe). Queued tasks are skipped immediately. In-flight downloads
    abort at the next yt-dlp progress tick. In-flight transcriptions finish the
    current Whisper call (uninterruptible) but discard the result."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", task_id or ""):
        raise HTTPException(400, "task_id inválido")
    task = _get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    _require_file_access(task.get("filename") or "", request)
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
            _atomic_write_json(MEDIA_FILE, media)
    return {"status": "cancel_requested", "task_id": task_id}

@app.get("/api/active-tasks")
async def api_active_tasks(request: Request, include_paused: bool = False):
    """Return all in-memory tasks that are still queued or processing.
    Um usuário público só recebe as tarefas dos itens que ele pode ver.

    `include_paused` fica FORA do padrão de propósito: quem chama isto no boot
    religa o polling de progresso, e uma task pausada não tem progresso a
    acompanhar. A Biblioteca de Mídia pede com o parâmetro ligado só para
    descobrir o task_id na hora de retomar (media.json não guarda esse id).
    """
    role = _role_of(request)
    wanted = ("queued", "processing") + (("paused",) if include_paused else ())
    with _tasks_lock:
        alive = {tid: dict(t) for tid, t in _tasks.items()
                 if t.get("status") in wanted}
    if role == auth.ROLE_ADMIN:
        return alive
    # Carrega os catálogos uma vez e reaproveita para todas as tarefas.
    history, media = _load_history(), _load_media()
    return {tid: t for tid, t in alive.items()
            if _visibility_for_file(t.get("filename") or "", history, media) == VIS_PUBLIC}

@app.post("/api/reset-stale")
async def api_reset_stale(request: Request):
    """Mark queued/processing history entries whose task is no longer in memory as interrupted.
    Called by the frontend on page load after a server restart.
    Um usuário público só mexe nas entradas públicas — a tela dele não pode
    reescrever o status de itens privados que ele nem enxerga."""
    role = _role_of(request)
    with _tasks_lock:
        active_ids = set(_tasks.keys())
    with _history_lock:
        history = _load_history()
        changed = 0
        for entry in history:
            if role != auth.ROLE_ADMIN and _vis_of(entry) != VIS_PUBLIC:
                continue
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
            _atomic_write_json(HISTORY_FILE, history)
    return {"reset": changed}

# -- Results
@app.get("/api/result/{filename}")
async def api_result(filename: str, request: Request):
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    result = _load_result_files(filename)
    if result is None:
        raise HTTPException(404, "Resultado não encontrado")
    return result

# -- Downloads
@app.get("/api/download/{filename}/{fmt}")
async def api_download(filename: str, fmt: str, request: Request):
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    base = _result_base(filename)
    d    = os.path.join(RESULTS_DIR, base)
    MAP  = {
        "txt":        (f"{base}.txt",            "text/plain"),
        "srt":        (f"{base}.srt",            "text/plain"),
        "json":       (f"{base}.json",           "application/json"),
        "timestamps": (f"{base}_timestamps.txt", "text/plain"),
        "md":         (f"{base}.md",             "text/markdown"),
    }
    if fmt not in MAP:
        raise HTTPException(400, "Formato inválido")
    if fmt == "md":
        _ensure_markdown(filename)  # lazily generate for transcrições salvas antes do export .md existir
    fname, media = MAP[fmt]
    path = os.path.join(d, fname)
    if not os.path.exists(path):
        raise HTTPException(404, "Arquivo não encontrado")
    original = _original_name_for(filename)
    ext_map = {"txt": ".txt", "srt": ".srt", "json": ".json", "timestamps": "_timestamps.txt", "md": ".md"}
    download_name = f"{original}{ext_map[fmt]}"
    return FileResponse(path, media_type=media, filename=download_name)

@app.get("/api/download-with-original/{filename}")
async def api_download_with_original(filename: str, request: Request):
    """Zip the transcription files (txt, srt, json, timestamps, md) together with the
    original audio/video upload — if it's still on disk. Lets the user grab the
    transcription AND the source media in a single download. If the original was
    already cleaned up, the ZIP still contains the transcription files."""
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    base = _result_base(filename)
    d = os.path.join(RESULTS_DIR, base)
    # Defense-in-depth: keep the results dir inside RESULTS_DIR
    if os.path.commonpath([os.path.realpath(d), os.path.realpath(RESULTS_DIR)]) != os.path.realpath(RESULTS_DIR):
        raise HTTPException(400, "Filename inválido")
    if not os.path.isdir(d):
        raise HTTPException(404, "Resultado não encontrado")

    _ensure_markdown(filename)  # inclui .md mesmo em transcrições salvas antes do export existir

    display    = _original_name_for(filename)        # user-facing stem, e.g. "Minha Aula"
    media_name = _original_media_name_for(filename)   # with extension, e.g. "Minha Aula.mp3"

    zip_name = f"{base}_completo_{uuid.uuid4().hex[:8]}.zip"
    zip_path = os.path.join(DATA_DIR, zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Transcription files: rename from internal base to the friendly display name
        for fname in sorted(os.listdir(d)):
            if fname.startswith(base):
                suffix = fname[len(base):]              # ".txt", ".srt", "_timestamps.txt", ...
                zf.write(os.path.join(d, fname), display + suffix)
        # Original media, only if it's still present inside UPLOAD_DIR
        upload_path = os.path.join(UPLOAD_DIR, filename)
        if (os.path.commonpath([os.path.realpath(upload_path), os.path.realpath(UPLOAD_DIR)])
                == os.path.realpath(UPLOAD_DIR) and os.path.exists(upload_path)):
            zf.write(upload_path, media_name)

    # Delete the temp ZIP once it's been streamed to the client (finding #4)
    return FileResponse(zip_path, filename=f"{display}_completo.zip",
                        background=BackgroundTask(_safe_remove, zip_path))

@app.get("/api/download-all")
async def api_download_all(request: Request):
    # Unique temp name so concurrent requests don't corrupt each other's ZIP
    # (finding #4); deleted after the response is streamed.
    zip_path = os.path.join(DATA_DIR, f"todas_transcricoes_{uuid.uuid4().hex[:8]}.zip")
    # ATENÇÃO: não dá para varrer results/ direto — a pasta contém o acervo
    # inteiro. Montamos a lista de diretórios permitidos a partir do histórico
    # já filtrado pelo papel de quem pediu.
    allowed_dirs = {_result_base(h.get("file") or "")
                    for h in _scope_entries(_load_history(), _role_of(request))}
    allowed_dirs.discard("")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in os.listdir(RESULTS_DIR):
            if d not in allowed_dirs:
                continue
            full_d = os.path.join(RESULTS_DIR, d)
            if os.path.isdir(full_d):
                for fname in os.listdir(full_d):
                    zf.write(os.path.join(full_d, fname), os.path.join(d, fname))
    return FileResponse(zip_path, filename="todas_transcricoes.zip",
                        background=BackgroundTask(_safe_remove, zip_path))

@app.post("/api/download-selected-zip")
async def api_download_selected_zip(request: Request, files: str = Form(...),
                                    formats: str = Form("txt,srt,json,timestamps")):
    """Download only the selected transcriptions as a ZIP.
    `files` is a JSON array of filenames (history ids) to include.
    `formats` is a comma-separated list of formats to include:
    any subset of {txt, srt, json, timestamps, md}."""
    try:
        filenames = json.loads(files)
        if not isinstance(filenames, list):
            raise ValueError("files deve ser uma lista")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Parâmetro 'files' inválido: {e}")

    if not filenames:
        raise HTTPException(400, "Nenhum arquivo selecionado")

    # Parse format whitelist
    valid_formats = {"txt", "srt", "json", "timestamps", "md"}
    chosen = {f.strip().lower() for f in formats.split(",") if f.strip()}
    chosen &= valid_formats
    if not chosen:
        raise HTTPException(400, "Selecione ao menos um formato (txt, srt, json, timestamps, md)")

    # Map each format to the suffix that appears after the internal base filename
    suffix_for = {
        "txt":        ".txt",
        "srt":        ".srt",
        "json":       ".json",
        "timestamps": "_timestamps.txt",
        "md":         ".md",
    }
    allowed_suffixes = {suffix_for[f] for f in chosen}

    # Validate each filename (prevents path traversal) and collect existing dirs
    results_real = os.path.realpath(RESULTS_DIR)
    # Loaded once and reused for every _ensure_markdown() call below, instead of
    # each call re-reading history.json for its own lookup.
    history_cache = _load_history() if "md" in chosen else None
    entries = []
    for fn in filenames:
        if not isinstance(fn, str):
            raise HTTPException(400, "Cada item de 'files' deve ser string")
        fn = _safe_filename(fn)
        # Um item privado no meio da seleção derruba o ZIP inteiro com 404, em
        # vez de sair sem ele: a tela de um usuário público só lista itens
        # públicos, então uma seleção mista significa pedido forjado.
        _require_file_access(fn, request)
        base = _result_base(fn)
        d = os.path.join(RESULTS_DIR, base)
        # Defense-in-depth: ensure path stays inside RESULTS_DIR
        if os.path.commonpath([os.path.realpath(d), results_real]) != results_real:
            continue
        if os.path.isdir(d):
            if "md" in chosen:
                _ensure_markdown(fn, history_cache)  # gera .md se a transcrição é anterior ao export
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
    return FileResponse(zip_path, filename="transcricoes_selecionadas.zip",
                        background=BackgroundTask(_safe_remove, zip_path))

# -- Delete
@app.delete("/api/delete/{filename}")
async def api_delete(filename: str, request: Request, scope: str = "both"):
    """User-initiated delete from the transcriptions screen.

    `scope` controls what actually gets removed:
      - "both" (default, backward-compatible): history entry + transcription
        result files + the original upload + the media.json catalog entry —
        frees everything related to this item.
      - "transcription": only the history entry + result files. The original
        media stays on disk and keeps showing in the Biblioteca de Mídia.
      - "media": only the physical original upload (+ its media.json entry) —
        same as /api/delete-media/{filename}. The transcription/history stays
        intact; the row just starts showing "Original apagado".
    """
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    if scope not in ("both", "transcription", "media"):
        scope = "both"

    # Excluir um item que uma thread ainda está processando não funcionava: a
    # thread seguia até o fim e _save_to_history RECRIAVA a entrada (ela sempre
    # remove e reinsere), junto com a pasta de resultados. Da tela, o item sumia
    # e reaparecia sozinho segundos depois — a exclusão era desfeita em silêncio.
    active = _active_task_for(filename)
    if active:
        raise HTTPException(409, "Este item ainda está sendo processado. "
                                 "Cancele a tarefa antes de excluir.")

    if scope in ("both", "transcription"):
        with _history_lock:
            history = [h for h in _load_history() if h.get("file") != filename]
            _atomic_write_json(HISTORY_FILE, history)
        base = _result_base(filename)
        if not base:
            raise HTTPException(400, "Filename inválido")
        d = os.path.join(RESULTS_DIR, base)
        # Defense-in-depth: ensure final path is inside RESULTS_DIR
        if os.path.commonpath([os.path.realpath(d), os.path.realpath(RESULTS_DIR)]) != os.path.realpath(RESULTS_DIR):
            raise HTTPException(400, "Filename inválido")
        if os.path.isdir(d):
            shutil.rmtree(d)

    if scope in ("both", "media"):
        upload_path = os.path.join(UPLOAD_DIR, filename)
        if os.path.commonpath([os.path.realpath(upload_path), os.path.realpath(UPLOAD_DIR)]) == os.path.realpath(UPLOAD_DIR):
            if os.path.exists(upload_path):
                try: os.remove(upload_path)
                except OSError: pass  # best-effort; we still drop the entries below

    if scope in ("both", "media"):
        # "media": remove the catalog entry too (matches /api/delete-media) —
        # nothing left on disk for it to describe. "transcription" leaves
        # media.json untouched on purpose, so the file keeps showing there.
        with _media_lock:
            media = [m for m in _load_media() if m.get("file") != filename]
            _atomic_write_json(MEDIA_FILE, media)

    return {"ok": True, "scope": scope}


@app.get("/api/gaps/{filename}")
async def api_gaps(filename: str, request: Request, min_gap: float = 1.0):
    """Detecta silêncios/respiros entre segmentos de fala e gera texto intercalado."""
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
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
# ── Google Drive (arquivos públicos) ──────────────────────────
# O yt-dlp é frágil com Drive (arquivos grandes, página de confirmação). Estes
# runners usam gdrive.download, que trata o token de confirmação. São roteados
# a partir dos kickoffs de URL quando o link é do Google Drive, então o usuário
# só cola o link nos mesmos campos de sempre.
def _run_gdrive_download_only(task_id, url, existing_filename=None, visibility=VIS_PRIVATE):
    stem = os.path.splitext(existing_filename)[0] if existing_filename else f"{task_id[:8]}_gdrive"
    state = {"filename": existing_filename or (stem + ".mp4")}  # provisório até saber a extensão real
    # Marca a visibilidade já no nome provisório para que uma eventual entrada de
    # ERRO (falha antes de saber o nome real) herde a visibilidade certa — senão
    # o download que falhou nasceria privado e sumiria para o membro da equipe.
    _mark_pending_visibility(state["filename"], visibility)
    _set_task(task_id, status="processing", progress=0, phase="download", phase_progress=0,
              name="Baixando do Google Drive…", filename=state["filename"])

    def _on_start(dest, title, total):
        fn = os.path.basename(dest)
        state["filename"] = fn
        _mark_pending_visibility(fn, visibility)
        _save_media(fn, f"{title}{os.path.splitext(fn)[1]}", url=url, is_transcribed=False, status="processing")
        _set_task(task_id, filename=fn, name=title or fn)

    def _progress(pct):
        if (_get_task(task_id) or {}).get('cancel_requested'):
            raise gdrive.GDriveCancelled()
        _set_task(task_id, phase="download", phase_progress=pct, progress=pct)

    def _cancel():
        return bool((_get_task(task_id) or {}).get('cancel_requested'))

    try:
        with _download_sem:
            dest, title = gdrive.download(url, UPLOAD_DIR, stem, force_filename=existing_filename,
                                          on_start=_on_start, progress_cb=_progress, cancel_cb=_cancel)
        fn = os.path.basename(dest)
        _save_media(fn, f"{title}{os.path.splitext(fn)[1]}", url=url, is_transcribed=False, status="done")
        _set_task(task_id, status="done", progress=100, phase="done", phase_progress=100,
                  name=title or fn, filename=fn)
    except gdrive.GDriveCancelled:
        _cleanup_task_files(os.path.splitext(state["filename"])[0])
        _save_media(state["filename"], "Download cancelado", url=url, is_transcribed=False, status="cancelled")
        _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                  name="Download cancelado", filename=state["filename"])
    except Exception as e:
        _cleanup_task_files(os.path.splitext(state["filename"])[0])
        _save_media(state["filename"], "Erro no Download", url=url, is_transcribed=False, status="error")
        _set_task(task_id, status="error", progress=0, phase="error",
                  name="Erro no Download", error=str(e), filename=state["filename"])

def _kickoff_gdrive_transcription(url, model, language, task, filter_fillers, folder,
                                  existing_filename=None, visibility=VIS_PRIVATE):
    task_id = str(uuid.uuid4())
    stem = os.path.splitext(existing_filename)[0] if existing_filename else f"{task_id[:8]}_gdrive"
    state = {"filename": existing_filename or (stem + ".mp4")}
    # Visibilidade no nome provisório (ver comentário em _run_gdrive_download_only).
    _mark_pending_visibility(state["filename"], visibility)
    _set_task(task_id, status="processing", progress=0, phase="download", phase_progress=0,
              name="Baixando do Google Drive…", filename=state["filename"])

    def _on_start(dest, title, total):
        fn = os.path.basename(dest)
        state["filename"] = fn
        _mark_pending_visibility(fn, visibility)
        _save_to_history(fn, {}, model, status="queued", task_id=task_id, original_name=title or fn,
                         folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
        _save_media(fn, f"{title}{os.path.splitext(fn)[1]}", url=url, is_transcribed=True, status="queued", force_name=True)
        _set_task(task_id, filename=fn)

    def _progress(pct):
        if (_get_task(task_id) or {}).get('cancel_requested'):
            raise gdrive.GDriveCancelled()
        _set_task(task_id, phase="download", phase_progress=pct, progress=pct * 0.25)

    def _cancel():
        return bool((_get_task(task_id) or {}).get('cancel_requested'))

    def _run():
        try:
            with _download_sem:
                dest, title = gdrive.download(url, UPLOAD_DIR, stem, force_filename=existing_filename,
                                              on_start=_on_start, progress_cb=_progress, cancel_cb=_cancel)
        except gdrive.GDriveCancelled:
            _cleanup_task_files(os.path.splitext(state["filename"])[0])
            _update_history_status(state["filename"], "cancelled")
            _save_media(state["filename"], "Download cancelado", url=url, is_transcribed=True, status="cancelled")
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                      name="Download cancelado", filename=state["filename"])
            return
        except Exception as e:
            _cleanup_task_files(os.path.splitext(state["filename"])[0])
            _save_to_history(state["filename"], {}, model, status="error",
                             error=f"Erro ao baixar do Google Drive: {e}", task_id=task_id,
                             folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
            _save_media(state["filename"], "Erro no Download", url=url, is_transcribed=True, status="error")
            _set_task(task_id, status="error", progress=0, phase="error",
                      name="Erro no Download", error=str(e), filename=state["filename"])
            return
        if _cancel():
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled")
            _update_history_status(state["filename"], "cancelled")
            return
        _run_transcription(task_id, dest, os.path.basename(dest), model, language, task, filter_fillers)

    threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "filename": state["filename"]}

# ── Redes sociais por URL (yt-dlp + plano B no navegador logado) ──────
# Instagram, TikTok, Facebook e X são o ponto fraco do yt-dlp puro; o
# medialink cai no navegador logado (intercept.resolve_media) quando o yt-dlp
# falha. YouTube/Vimeo/genérico continuam no fluxo yt-dlp normal (que tem as
# opções de qualidade/legenda do Download Avançado).
_SOCIAL_DL_SUFFIXES = ("instagram.com", "tiktok.com", "facebook.com", "fb.watch",
                       "twitter.com", "x.com", "kwai.com", "kwai-video.com")

def _is_social_dl_url(url: str) -> bool:
    if not SOCIAL_MULTI_OK:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == s or host.endswith("." + s) for s in _SOCIAL_DL_SUFFIXES)

def _run_social_download_only(task_id, url, media_type, existing_filename=None, visibility=VIS_PRIVATE):
    audio_only = (media_type == "audio")
    prov = existing_filename or f"{task_id[:8]}_social.{'mp3' if audio_only else 'mp4'}"
    state = {"filename": prov}
    _mark_pending_visibility(prov, visibility)
    _save_media(prov, "Obtendo mídia…", url=url, is_transcribed=False, status="processing")
    _set_task(task_id, status="processing", progress=0, phase="download", phase_progress=0,
              name="Baixando da rede social…", filename=prov)

    def _prog(done, total, name):
        pct = (done / total * 100) if total else 0
        _set_task(task_id, phase="download", phase_progress=pct, progress=pct)

    tmpdir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    try:
        with _download_sem:
            res = social_medialink.download(url, dest=tmpdir, audio_only=audio_only, on_progress=_prog)
        src = res["file"]
        ext = os.path.splitext(src)[1].lower() or (".mp3" if audio_only else ".mp4")
        fn = existing_filename or f"{task_id[:8]}_social{ext}"
        if fn != prov:
            _remove_media_entry(prov)   # nome/ext real difere do provisório
        state["filename"] = fn
        os.replace(src, os.path.join(UPLOAD_DIR, fn))
        title = res.get("title") or os.path.splitext(fn)[0]
        _mark_pending_visibility(fn, visibility)
        _save_media(fn, f"{title}{ext}", url=url, is_transcribed=False, status="done", force_name=True)
        _set_task(task_id, status="done", progress=100, phase="done", phase_progress=100,
                  name=title, filename=fn)
    except Exception as e:
        _cleanup_task_files(os.path.splitext(state["filename"])[0])
        _save_media(state["filename"], "Erro no Download", url=url, is_transcribed=False, status="error")
        _set_task(task_id, status="error", progress=0, phase="error",
                  name="Erro no Download", error=str(e), filename=state["filename"])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

def _kickoff_social_transcription(url, model, language, task, filter_fillers, folder,
                                  existing_filename=None, visibility=VIS_PRIVATE):
    task_id = str(uuid.uuid4())
    fn = existing_filename or f"{task_id[:8]}_social.mp4"   # vídeo é sempre mesclado p/ mp4
    _mark_pending_visibility(fn, visibility)
    _save_to_history(fn, {}, model, status="queued", task_id=task_id, original_name=_result_base(fn),
                     folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
    _save_media(fn, "Obtendo mídia…", url=url, is_transcribed=True, status="queued")
    _set_task(task_id, status="processing", progress=0, phase="download", phase_progress=0,
              name="Baixando da rede social…", filename=fn)

    def _prog(done, total, name):
        pct = (done / total * 100) if total else 0
        _set_task(task_id, phase="download", phase_progress=pct, progress=pct * 0.25)

    def _run():
        tmpdir = tempfile.mkdtemp(dir=UPLOAD_DIR)
        try:
            with _download_sem:
                res = social_medialink.download(url, dest=tmpdir, audio_only=False, on_progress=_prog)
            src = res["file"]
            final = os.path.join(UPLOAD_DIR, fn)
            os.replace(src, final)   # força o nome .mp4 esperado pela entrada
            title = res.get("title") or os.path.splitext(fn)[0]
            _save_to_history(fn, {}, model, status="queued", task_id=task_id, original_name=title,
                             folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
            _save_media(fn, f"{title}.mp4", url=url, is_transcribed=True, status="queued", force_name=True)
        except Exception as e:
            _cleanup_task_files(os.path.splitext(fn)[0])
            _save_to_history(fn, {}, model, status="error",
                             error=f"Erro ao baixar da rede social: {e}", task_id=task_id,
                             folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
            _save_media(fn, "Erro no Download", url=url, is_transcribed=True, status="error")
            _set_task(task_id, status="error", progress=0, phase="error",
                      name="Erro no Download", error=str(e), filename=fn)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return
        shutil.rmtree(tmpdir, ignore_errors=True)
        _run_transcription(task_id, os.path.join(UPLOAD_DIR, fn), fn, model, language, task, filter_fillers)

    threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "filename": fn}

def _kickoff_url_transcription(url: str, model: str, language: str, task: str,
                                filter_fillers: bool, folder: str,
                                existing_filename: str | None = None,
                                visibility: str = VIS_PRIVATE) -> dict:
    # Google Drive: caminho dedicado (o yt-dlp não é confiável aqui).
    if gdrive.is_gdrive_url(url):
        return _kickoff_gdrive_transcription(url, model, language, task, filter_fillers, folder,
                                             existing_filename=existing_filename, visibility=visibility)
    # Redes sociais (IG/TikTok/FB/X): yt-dlp com plano B no navegador logado.
    if _is_social_dl_url(url):
        return _kickoff_social_transcription(url, model, language, task, filter_fillers, folder,
                                             existing_filename=existing_filename, visibility=visibility)
    task_id = str(uuid.uuid4())
    safe_name = re.sub(r'[^\w.-]', '_', url.split('/')[-1] or 'video')[:50] or 'video'
    original_name = f"{safe_name}.mp3"
    # Retry: reaproveita o MESMO filename — atualiza o item já existente em
    # history.json/media.json em vez de criar um novo (só um envio novo gera
    # um filename fresco a partir do task_id).
    filename = existing_filename or f"{task_id[:8]}_{original_name}"
    # Num retry (existing_filename) a entrada já existe e a visibilidade gravada
    # nela vence — este marcador só decide o caso de um item novo.
    _mark_pending_visibility(filename, visibility)
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

    ydl_opts = _build_ydl_opts(url, _hook_main, base={
        'format': 'bestaudio/best',
        'outtmpl': upload_path.replace('.mp3', '.%(ext)s'),
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}],
    })
    _save_to_history(filename, {}, model, status="queued", task_id=task_id, original_name=_result_base(original_name),
                     folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
    _save_media(filename, original_name, url=url, is_transcribed=True, status="queued")

    def _run_download_and_transcribe():
        try:
            with _download_sem:
                info = _ydl_download_with_fallback(url, ydl_opts, task_id, filename)
            # Swap the URL-slug placeholder for the real media title so the UI
            # shows "Minha Aula" instead of "watch?v=abc123". The original link
            # stays saved in media.json (the `url` field) for re-use.
            title = (info or {}).get('title')
            if title:
                _save_to_history(filename, {}, model, status="queued",
                                 task_id=task_id, original_name=title,
                                 folder=folder, source="url", task_type=task, filter_fillers=filter_fillers)
                _save_media(filename, f"{title}.mp3", url=url,
                            is_transcribed=True, status="queued", force_name=True)
        except _UserCancelled:
            _cleanup_task_files(os.path.splitext(filename)[0])   # drop partial/.part downloads (finding #5)
            _update_history_status(filename, "cancelled")
            _save_media(filename, original_name, url=url, is_transcribed=True, status="cancelled")
            _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                      name="Download cancelado", filename=filename)
            return
        except Exception as e:
            _cleanup_task_files(os.path.splitext(filename)[0])   # drop partial/.part downloads (finding #5)
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
            # Busca pelo stem do FILENAME (não do task_id) — outtmpl foi construído
            # a partir do filename, que num retry é reaproveitado sob um task_id novo.
            search_prefix = os.path.splitext(filename)[0]
            for f in os.listdir(UPLOAD_DIR):
                if search_prefix in f:
                    final_path = os.path.join(UPLOAD_DIR, f)
                    break

        _run_transcription(task_id, final_path, filename, model, language, task, filter_fillers)

    t = threading.Thread(target=_run_download_and_transcribe, daemon=True)
    t.start()
    return {"task_id": task_id, "filename": filename}

@app.post("/api/transcribe-url")
async def api_transcribe_url(
    request: Request,
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

    url = _validate_media_url(url)   # SSRF + scheme guard (finding #10)
    folder = _validate_folder_name(folder) if folder else ""
    _ensure_folder_tree(folder)

    return _kickoff_url_transcription(url, model, language, task,
                                       filter_fillers == "true", folder,
                                       visibility=_visibility_for_new(request))


# Batch dispatch — accepts a newline / comma separated list of URLs and fires
# each one. Two modes:
#   transcribe="true"  (default) → download + transcribe (full pipeline)
#   transcribe="false"           → download only (media_type + quality apply)
# Returns counts + the first few task_ids so the UI can hook polling.
@app.post("/api/transcribe-batch")
async def api_transcribe_batch(
    request:        Request,
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

    # Teto de itens, como o Download Avançado já fazia (_ADVANCED_MAX_TOTAL_ITEMS).
    # Sem ele, colar 2 mil linhas criava 2 mil threads paradas no semáforo, 2 mil
    # entradas em memória e um history.json reescrito 2 mil vezes — além de 2 mil
    # polls de 800ms no navegador, um auto-DoS.
    batch_truncated = len(clean) > _BATCH_MAX_ITEMS
    clean = clean[:_BATCH_MAX_ITEMS]

    # Folder validation only applies to transcribe mode (download-only items
    # live in media.json and don't have a folder concept the user picks here).
    do_transcribe = transcribe == "true"
    if do_transcribe:
        folder = _validate_folder_name(folder) if folder else ""
        _ensure_folder_tree(folder)

    do_filter = filter_fillers == "true"
    new_vis = _visibility_for_new(request)
    task_ids: list[str] = []
    for u in clean:
        try:
            u = _validate_media_url(u)   # SSRF + scheme guard per URL (finding #10)
            if do_transcribe:
                res = _kickoff_url_transcription(u, model, language, task, do_filter, folder,
                                                 visibility=new_vis)
            else:
                res = _kickoff_download_only(u, media_type, quality, visibility=new_vis)
            task_ids.append(res["task_id"])
        except Exception:
            # Don't fail the whole batch if one URL trips validation/kickoff; skip it.
            task_ids.append(None)
    return {
        "submitted":  sum(1 for t in task_ids if t),
        "skipped":    sum(1 for t in task_ids if not t),
        "total":      len(clean),
        "transcribe": do_transcribe,
        "truncated":  batch_truncated,
        "task_ids":   [t for t in task_ids if t][:50],  # cap response size
    }

# ── Retry — refazer manualmente um item com erro/cancelado ──────
# O usuário seleciona itens (na tela de Transcrições OU na Biblioteca de
# Mídia) e pede pra tentar de novo. O que é refeito depende do que foi
# pedido originalmente:
#   - havia uma entrada em history.json → era uma transcrição (upload ou URL);
#     refaz com o MESMO modelo/idioma/modo/filtro salvos no momento do pedido.
#     Se a fonte era uma URL, isso também baixa de novo (cobre o caso de o
#     download ter sido o que falhou — "os dois" ficam cobertos pelo mesmo retry).
#   - só havia entrada em media.json (sem history) → era um download avulso;
#     refaz o download com a mesma URL (média/qualidade não são guardadas,
#     então infere o tipo pela extensão do nome salvo e usa qualidade "best").
_AUDIO_EXT_SET = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma", ".flac"}

def _retry_item(filename: str) -> dict:
    filename = _safe_filename(filename)
    # Dois retries no mesmo arquivo lançavam DUAS threads gravando no mesmo
    # destino: _save_result_files usa open(...,"w") sem lock, então o resultado
    # saía misturado (o .txt de uma execução com o .json de outra) e a última a
    # terminar sobrescrevia o status da outra.
    if _active_task_for(filename):
        raise HTTPException(409, "Este item já está sendo processado.")
    hist_entry  = next((h for h in _load_history() if h.get("file") == filename), None)
    media_entry = next((m for m in _load_media()   if m.get("file") == filename), None)

    if hist_entry:
        source = hist_entry.get("source") or ("url" if (media_entry and media_entry.get("url")) else "upload")
        model  = hist_entry.get("mode") or "turbo"
        lang   = hist_entry.get("lang") if hist_entry.get("lang") not in (None, "?") else "pt"
        task_type = hist_entry.get("task_type") or "transcribe"
        filter_fillers = bool(hist_entry.get("filter_fillers"))
        folder = hist_entry.get("folder") or ""
        upload_path = os.path.join(UPLOAD_DIR, filename)

        # O arquivo original (upload OU já baixado de uma URL antes) ainda
        # está no disco — não baixa de novo, só transcreve direto. Cobre o
        # caso comum de o download ter dado certo e só a transcrição ter
        # falhado depois (ex.: erro do Whisper) — refazer o download seria
        # desnecessário e mais lento.
        if os.path.exists(upload_path):
            task_id = str(uuid.uuid4())
            _set_task(task_id, status="queued", progress=0, name=hist_entry.get("name") or filename, filename=filename)
            _save_to_history(filename, {}, model, status="queued", task_id=task_id,
                             folder=folder, task_type=task_type, filter_fillers=filter_fillers)
            t = threading.Thread(target=_run_transcription,
                                 args=(task_id, upload_path, filename, model, lang, task_type, filter_fillers),
                                 daemon=True)
            t.start()
            return {"kind": "transcribe", "task_id": task_id}

        if source == "url":
            url = (media_entry or {}).get("url")
            if not url:
                raise HTTPException(400, "Não foi possível encontrar a URL original deste item.")
            url = _validate_media_url(url)
            res = _kickoff_url_transcription(url, model, lang, task_type, filter_fillers, folder,
                                             existing_filename=filename)
            return {"kind": "transcribe", "task_id": res["task_id"]}

        # source == "upload" e o arquivo já não existe mais — não tem como
        # recuperar os bytes perdidos, só reenviando manualmente.
        raise HTTPException(400, "O arquivo original não está mais disponível — envie de novo para tentar.")

    if media_entry:
        url = media_entry.get("url")
        if not url:
            raise HTTPException(400, "Não há URL associada a este item — não é possível tentar de novo.")
        url = _validate_media_url(url)
        ext = os.path.splitext(media_entry.get("name") or filename)[1].lower()
        media_type = "audio" if ext in _AUDIO_EXT_SET else "video"
        # Reaproveita a extensão REAL do arquivo como container/formato. Sem
        # isto o retry voltava ao padrão (mp4/mp3): o outtmpl não casava mais
        # com o nome existente e o yt-dlp gravava conteúdo de um formato dentro
        # de um arquivo com a extensão de outro.
        real = ext.lstrip(".").lower()
        if media_type == "video":
            fmt_kwargs = {"container": real if real in _VALID_CONTAINERS else "auto"}
        else:
            fmt_kwargs = {"audio_format": real if real in _VALID_AUDIO_FORMATS else "auto"}
        res = _kickoff_download_only(url, media_type, "best",
                                     existing_filename=filename, **fmt_kwargs)
        return {"kind": "download", "task_id": res["task_id"]}

    raise HTTPException(404, "Item não encontrado em histórico nem em mídia.")

@app.post("/api/retry/{filename}")
async def api_retry(filename: str, request: Request):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    _require_file_access(filename, request)
    return _retry_item(filename)

@app.post("/api/retry-batch")
async def api_retry_batch(request: Request, files: str = Form(...)):
    """`files` é um array JSON de filenames (mesmo id usado nas tabelas)."""
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    try:
        filenames = json.loads(files)
        if not isinstance(filenames, list):
            raise ValueError("files deve ser uma lista")
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(400, f"Parâmetro 'files' inválido: {e}")
    if not filenames:
        raise HTTPException(400, "Nenhum item selecionado")

    results = []
    for fn in filenames:
        if not isinstance(fn, str):
            continue
        try:
            _require_file_access(fn, request)
            r = _retry_item(fn)
            results.append({"file": fn, "ok": True, **r})
        except HTTPException as e:
            results.append({"file": fn, "ok": False, "error": e.detail})
        except Exception as e:
            results.append({"file": fn, "ok": False, "error": str(e)})
    return {
        "submitted": sum(1 for r in results if r["ok"]),
        "failed":    sum(1 for r in results if not r["ok"]),
        "total":     len(results),
        "results":   results[:100],
    }

def _run_download_only(task_id: str, url: str, media_type: str, quality: str,
                       subtitles: bool = False, sub_langs: str = "pt,en",
                       auto_subs: bool = False, thumbnail: bool = False,
                       metadata: bool = False, audio_lang: str | None = None,
                       existing_filename: str | None = None,
                       visibility: str = VIS_PRIVATE,
                       container: str = "auto", audio_format: str = "mp3",
                       resuming: bool = False, folder: str = ""):
    is_video = media_type == "video"
    # Formato de saída escolhido pelo usuário. "auto"/"original" preservam o
    # comportamento histórico (mp4 para vídeo, mp3 para áudio); os demais
    # trocam o container do remux. Já chegam validados pelo endpoint.
    if is_video:
        ext = "mp4" if container in ("auto", "original") else container
    else:
        ext = "mp3" if audio_format in ("auto", "original") else audio_format
    # Retry: reaproveita o MESMO filename — atualiza o item já existente em
    # media.json em vez de criar um novo.
    filename = existing_filename or f"{task_id[:8]}_download.{ext}"
    # O nome só existe aqui dentro (roda em thread), então é aqui que a
    # visibilidade herdada do request é registrada, antes do primeiro _save_media.
    _mark_pending_visibility(filename, visibility)
    try:
        _save_media(filename, f"Obtendo {'vídeo' if is_video else 'áudio'}...", url=url, is_transcribed=False, status="processing")
        _set_task(task_id, status="processing", progress=0,
                  phase="download", phase_progress=0,
                  name="Download Media", filename=filename)

        def _hook(d):
            _t = _get_task(task_id)
            if _t.get('cancel_requested'):
                raise _UserCancelled('cancel requested during download')
            if _t.get('pause_requested'):
                raise _DownloadPaused('pause requested during download')
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

        ydl_opts = _build_ydl_opts(url, _hook)

        # Faixa de áudio (dublagem): filtra o componente de áudio por idioma —
        # cai de volta pro melhor disponível se o idioma pedido não existir.
        audio_sel = f'bestaudio[language^={audio_lang}]/bestaudio' if audio_lang else 'bestaudio'

        if is_video:
            height_caps = {'1080p': 1080, '720p': 720, '480p': 480}
            video_sel = f'bestvideo[height<={height_caps[quality]}]' if quality in height_caps else 'bestvideo'
            ydl_opts['format'] = f'{video_sel}+{audio_sel}/best'
            ydl_opts['outtmpl'] = upload_path.replace(f'.{ext}', '.%(ext)s')
            if container == 'original':
                # Sem remux: fica no container que o site entregou. Evita uma
                # recodificação/remux desnecessária quando não faz diferença.
                ydl_opts['format'] = 'best'
            else:
                ydl_opts['merge_output_format'] = ext
        else:
            worst_sel = f'worstaudio[language^={audio_lang}]/worstaudio' if audio_lang else 'worstaudio'
            ydl_opts['format'] = worst_sel if quality == 'worst' else audio_sel
            ydl_opts['outtmpl'] = upload_path.replace(f'.{ext}', '.%(ext)s')
            if audio_format != 'original':
                ydl_opts['postprocessors'] = [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': ext}]

        # Extras opcionais (Download Avançado): legendas, thumbnail e metadados
        # embutidos no próprio arquivo — mantém o modelo de "1 arquivo por item"
        # em vez de espalhar .srt/.jpg soltos que o app não rastreia.
        # Legendas só fazem sentido em vídeo — um container de áudio não tem
        # como embutir uma trilha de legenda, e escrevê-la à parte deixaria um
        # .srt órfão no disco que nenhuma entrada do catálogo aponta para.
        if subtitles and is_video:
            ydl_opts['writesubtitles'] = True
            ydl_opts['writeautomaticsub'] = auto_subs
            ydl_opts['subtitleslangs'] = [l.strip() for l in (sub_langs or '').split(',') if l.strip()] or ['pt']
            ydl_opts.setdefault('postprocessors', []).append({'key': 'FFmpegSubtitlesConvertor', 'format': 'srt'})
            ydl_opts['postprocessors'].append({'key': 'FFmpegEmbedSubtitle'})
        if thumbnail:
            ydl_opts['writethumbnail'] = True
            ydl_opts.setdefault('postprocessors', []).append({'key': 'EmbedThumbnail'})
        if metadata:
            ydl_opts.setdefault('postprocessors', []).append(
                {'key': 'FFmpegMetadata', 'add_metadata': True, 'add_chapters': True})

        with _download_sem:
            info = _ydl_download_with_fallback(url, ydl_opts, task_id, filename,
                                               preserve_partials=resuming)
            title = (info or {}).get('title', 'Media Secundária')


        actual_path = upload_path if os.path.exists(upload_path) else upload_path.replace(f'.{ext}', '') + f'.{ext}'
        if not os.path.exists(actual_path):
            # Busca pelo stem do FILENAME (não do task_id) — outtmpl foi construído
            # a partir do filename, que num retry é reaproveitado sob um task_id novo.
            search_prefix = os.path.splitext(filename)[0]
            for f in os.listdir(UPLOAD_DIR):
                if search_prefix in f:
                    actual_path = os.path.join(UPLOAD_DIR, f)
                    filename = f
                    break

        # A extensão REAL pode não ser a que pedimos: com container/audio_format
        # "original" não há remux, então o yt-dlp entrega o container nativo do
        # site (.webm, .m4a…). Usar `ext` aqui gravaria um nome de exibição
        # mentindo sobre o conteúdo — e é esse nome que o navegador recebe ao
        # baixar o arquivo depois.
        real_ext = os.path.splitext(filename)[1].lstrip(".") or ext
        _save_media(filename, f"{title}.{real_ext}", url=url, is_transcribed=False, status="done")
        # Pasta de destino (usada pelas assinaturas em modo "só baixar"):
        # _save_media não recebe folder, então gravamos logo depois — ele
        # preserva o campo nas gravações seguintes.
        if folder:
            with _media_lock:
                media = _load_media()
                for m in media:
                    if m.get("file") == filename:
                        m["folder"] = folder
                _atomic_write_json(MEDIA_FILE, media)
        _set_task(task_id, status="done", progress=100, phase="done", phase_progress=100,
                  name=f"{title}.{real_ext}", filename=filename)
    except _DownloadPaused:
        # NÃO limpa os parciais: são eles que permitem retomar de onde parou.
        # Guarda os argumentos para o resume poder recriar o mesmo download.
        _save_media(filename, "Download pausado", url=url, is_transcribed=False, status="paused")
        _set_task(task_id, status="paused", phase="paused",
                  name="Download pausado", filename=filename,
                  pause_requested=False,
                  resume_args={"url": url, "media_type": media_type, "quality": quality,
                               "subtitles": subtitles, "sub_langs": sub_langs,
                               "auto_subs": auto_subs, "thumbnail": thumbnail,
                               "metadata": metadata, "audio_lang": audio_lang,
                               "existing_filename": filename, "visibility": visibility,
                               "container": container, "audio_format": audio_format})
    except _UserCancelled:
        _cleanup_task_files(os.path.splitext(filename)[0])   # remove partial/.part downloads (finding #5)
        _save_media(filename, "Download cancelado", url=url, is_transcribed=False, status="cancelled")
        _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                  name="Download cancelado", filename=filename)
    except Exception as e:
        _cleanup_task_files(os.path.splitext(filename)[0])   # remove partial/.part downloads (finding #5)
        _save_media(filename, "Erro no Download", url=url, is_transcribed=False, status="error")
        _set_task(task_id, status="error", progress=0, phase="error",
                  name="Erro no Download", error=str(e), filename=filename)

# Thin shared kickoff so single + batch + advanced download-only paths spawn
# the same threaded worker. Returns dict with task_id so callers can poll
# progress. The extra kwargs (subtitles/thumbnail/metadata/audio_lang) are
# the "Download Avançado" options — every existing caller keeps working
# unchanged since they all default to off.
def _kickoff_download_only(url: str, media_type: str, quality: str, **advanced) -> dict:
    # `visibility` chega junto com os kwargs avançados e segue direto para o
    # worker — ver _run_download_only.
    task_id = str(uuid.uuid4())
    # Google Drive: caminho dedicado. As opções avançadas do yt-dlp (legendas,
    # thumbnail, qualidade) não se aplicam a um arquivo cru do Drive — só
    # herdamos existing_filename (retry) e visibility.
    if gdrive.is_gdrive_url(url):
        t = threading.Thread(target=_run_gdrive_download_only,
                             args=(task_id, url),
                             kwargs={"existing_filename": advanced.get("existing_filename"),
                                     "visibility": advanced.get("visibility", VIS_PRIVATE)},
                             daemon=True)
        t.start()
        return {"task_id": task_id}
    # Redes sociais (IG/TikTok/FB/X): medialink (yt-dlp + plano B no navegador).
    if _is_social_dl_url(url):
        t = threading.Thread(target=_run_social_download_only,
                             args=(task_id, url, media_type),
                             kwargs={"existing_filename": advanced.get("existing_filename"),
                                     "visibility": advanced.get("visibility", VIS_PRIVATE)},
                             daemon=True)
        t.start()
        return {"task_id": task_id}
    t = threading.Thread(target=_run_download_only,
                         args=(task_id, url, media_type, quality), kwargs=advanced, daemon=True)
    t.start()
    return {"task_id": task_id}

@app.post("/api/yt-download-only")
async def api_yt_download_only(
    request: Request,
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    media_type: str = Form("video"),
    quality: str = Form("best")
):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    url = _validate_media_url(url)   # SSRF + scheme guard (finding #10)
    res = _kickoff_download_only(url, media_type, quality,
                                 visibility=_visibility_for_new(request))
    return {"message": "Download_start", "task_id": res["task_id"]}

# ── Download Avançado — playlist, legendas, metadados, thumbnail, faixa de
# áudio. Reaproveita _kickoff_download_only/_run_download_only (mesma fila,
# mesmo polling de progresso, mesmo destino na Biblioteca de Mídia) — só
# adiciona a expansão de playlist e o repasse das opções extras.
_ADVANCED_MAX_PLAYLIST_ITEMS = 100

# Formatos de saída oferecidos no Download Avançado. Allowlist explícita: o
# valor vai para o merge_output_format/preferredcodec do yt-dlp, então não pode
# ser string livre vinda do formulário.
_VALID_CONTAINERS    = {"auto", "mp4", "mkv", "webm", "original"}
_VALID_AUDIO_FORMATS = {"auto", "mp3", "m4a", "wav", "opus", "original"}

def _validate_output_format(container: str, audio_format: str) -> tuple[str, str]:
    if container not in _VALID_CONTAINERS:
        raise HTTPException(400, f"Formato de vídeo inválido: {container!r}")
    if audio_format not in _VALID_AUDIO_FORMATS:
        raise HTTPException(400, f"Formato de áudio inválido: {audio_format!r}")
    return container, audio_format

def _resolve_playlist_urls(url: str) -> tuple[list[str], str | None]:
    """extract_flat (sem baixar nada) para listar os vídeos de uma playlist/canal.
    Retorna (urls, playlist_title). Se a URL não for uma playlist, retorna [url]."""
    opts = _build_ydl_opts(url, lambda d: None, base={'extract_flat': True, 'skip_download': True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get('entries')
    if not entries:
        return [url], None
    urls = []
    for e in entries:
        if not e:
            continue
        u = e.get('webpage_url') or e.get('url') or (f"https://www.youtube.com/watch?v={e['id']}" if e.get('id') else None)
        if u:
            urls.append(u)
    return urls[:_ADVANCED_MAX_PLAYLIST_ITEMS], (info or {}).get('title')

@app.get("/api/resolve-playlist")
async def api_resolve_playlist(url: str):
    """Deixa a UI mostrar 'N vídeos encontrados' e pedir confirmação antes de
    disparar vários downloads de uma vez."""
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    url = _validate_media_url(url)
    try:
        urls, playlist_title = _resolve_playlist_urls(url)
    except Exception as e:
        raise HTTPException(400, f"Não foi possível ler a URL: {e}")
    return {
        "is_playlist":    len(urls) > 1 or bool(playlist_title),
        "count":          len(urls),
        "truncated":      len(urls) >= _ADVANCED_MAX_PLAYLIST_ITEMS,
        "playlist_title": playlist_title,
    }


# Teto de segurança para o TOTAL de downloads de uma chamada — importa quando
# o modo lote combina várias URLs com "playlist inteira" ligado (cada uma
# podendo expandir até 100 itens); sem isso, colar 10 links de playlist
# poderia disparar 1000 downloads de uma vez.
_ADVANCED_MAX_TOTAL_ITEMS = 150

@app.post("/api/download-advanced")
async def api_download_advanced(
    request:    Request,
    url:        str = Form(""),
    urls:       str = Form(""),   # modo lote: uma URL por linha/vírgula
    media_type: str = Form("video"),
    quality:    str = Form("best"),
    playlist:   str = Form("false"),
    subtitles:  str = Form("false"),
    sub_langs:  str = Form("pt,en"),
    auto_subs:  str = Form("false"),
    thumbnail:  str = Form("false"),
    metadata:   str = Form("false"),
    audio_lang: str = Form(""),
    container:    str = Form("auto"),   # mp4/mkv/webm/original — só vídeo
    audio_format: str = Form("auto"),   # mp3/m4a/wav/opus/original — só áudio
):
    if not YT_DLP_OK:
        raise HTTPException(400, "yt-dlp não instalado.")
    container, audio_format = _validate_output_format(container, audio_format)

    # Junta a URL única (modo normal) e/ou a lista em lote — dedup preservando
    # ordem, pra colagens acidentalmente duplicadas não disparar 2x.
    raw = ([url] if url and url.strip() else []) + re.split(r'[\n,;]+', urls or "")
    seen: set = set()
    seeds: list[str] = []
    for u in raw:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        seeds.append(u)
    if not seeds:
        raise HTTPException(400, "Informe ao menos uma URL")

    validated_seeds = []
    for u in seeds:
        try:
            validated_seeds.append(_validate_media_url(u))
        except HTTPException:
            continue  # URL inválida — pula sem derrubar o restante do lote
    if not validated_seeds:
        raise HTTPException(400, "Nenhuma URL válida informada")

    # "Playlist inteira" expande CADA seed (link único ou cada linha do lote)
    final_urls: list[str] = []
    if playlist == "true":
        for seed in validated_seeds:
            try:
                expanded, _ = _resolve_playlist_urls(seed)
            except Exception:
                expanded = [seed]  # falhou ao expandir — tenta como item único mesmo assim
            final_urls.extend(expanded)
    else:
        final_urls = validated_seeds

    truncated = len(final_urls) > _ADVANCED_MAX_TOTAL_ITEMS
    final_urls = final_urls[:_ADVANCED_MAX_TOTAL_ITEMS]

    advanced = dict(
        subtitles=subtitles == "true", sub_langs=sub_langs,
        auto_subs=auto_subs == "true", thumbnail=thumbnail == "true",
        metadata=metadata == "true", audio_lang=(audio_lang or "").strip() or None,
        visibility=_visibility_for_new(request),
        container=container, audio_format=audio_format,
    )
    task_ids: list[str | None] = []
    for u in final_urls:
        try:
            u = _validate_media_url(u)   # SSRF + scheme guard per URL (finding #10)
            res = _kickoff_download_only(u, media_type, quality, **advanced)
            task_ids.append(res["task_id"])
        except Exception:
            # Não derruba o lote inteiro por causa de um item; pula.
            task_ids.append(None)
    return {
        "submitted":  sum(1 for t in task_ids if t),
        "skipped":    sum(1 for t in task_ids if not t),
        "total":      len(final_urls),
        "truncated":  truncated,
        "task_ids":   [t for t in task_ids if t][:50],
    }

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
    _atomic_write_json(FOLDERS_FILE, paths)

def _ensure_folder_tree(folder: str) -> None:
    """Persist `folder` and all its ancestors into folders.json so the sidebar
    tree keeps them visible. Idempotent no-op for the root ("") — dedups the
    ancestor-creation block that was copied across 4 endpoints (finding #11)."""
    if not folder:
        return
    with _folders_lock:
        paths = set(_load_folders_paths())
        for a in _ancestors_of(folder):
            paths.add(a)
        _save_folders_paths(sorted(paths))

def _load_public_folders() -> list[str]:
    if not os.path.exists(PUBLIC_FOLDERS_FILE):
        return []
    try:
        with open(PUBLIC_FOLDERS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, str)]
    except (json.JSONDecodeError, ValueError, OSError):
        return []

def _add_public_folder(folder: str) -> None:
    if not folder:
        return
    with _folders_lock:
        paths = set(_load_public_folders())
        paths.update(_ancestors_of(folder))
        _atomic_write_json(PUBLIC_FOLDERS_FILE, sorted(paths))

@app.get("/api/folders")
async def api_folders(request: Request):
    """Returns every folder path (explicit + implicit from entries) with item counts.
    A folder's count includes items in that folder AND all its descendants.

    Para um usuário público a árvore é reconstruída do zero: só aparecem as
    pastas que contêm algum item público (mais os ancestrais) e as que ele
    mesmo criou. Nome de pasta é informação — "Psicologas", "Vendas" — e não
    deve escapar junto com a contagem."""
    role = _role_of(request)
    # Snapshot state under locks (brief) — counts are informational, no need for
    # a single global transaction across all three files.
    with _folders_lock:
        explicit_paths = set(_load_folders_paths()) if role == auth.ROLE_ADMIN \
                         else set(_load_public_folders())
    with _history_lock:
        hist = _scope_entries(list(_load_history()), role)
    with _media_lock:
        med = _scope_entries(list(_load_media()), role)

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
async def api_folders_create(request: Request, path: str = Form(...)):
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
    if not _is_admin(request):
        _add_public_folder(path)
    return {"ok": True, "path": path}

@app.post("/api/folders/rename")
async def api_folders_rename(request: Request, old_path: str = Form(...),
                             new_path: str = Form(...)):
    """Rename a folder. Cascades to all descendants and all affected history/media entries.
    Admin-only: a cascata reescreve o campo `folder` de itens privados também."""
    _require_admin(request)
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
        _atomic_write_json(HISTORY_FILE, history)

    # Rename folder field in media entries
    with _media_lock:
        media = _load_media()
        for entry in media:
            r = _remap(entry.get("folder") or "")
            if r is not None:
                entry["folder"] = r
                renamed_count += 1
        _atomic_write_json(MEDIA_FILE, media)

    return {"ok": True, "path": new_path, "renamed": renamed_count}

@app.post("/api/folders/delete")
async def api_folders_delete(request: Request, path: str = Form(...),
                             cascade: str = Form("move")):
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
    # Admin-only: apagar uma pasta mexe (ou remove) itens privados dentro dela.
    _require_admin(request)

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
            # Itens dentro desta pasta (ou de qualquer subpasta) vão embora.
            def _in_folder(h):
                f = h.get("folder") or ""
                return f == path or f.startswith(prefix)
            to_delete = [h for h in history if _in_folder(h)]
            history = [h for h in history if not _in_folder(h)]  # O(n), não O(n²)
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
        _atomic_write_json(HISTORY_FILE, history)

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
        _atomic_write_json(MEDIA_FILE, media)

    return {
        "ok": True,
        "path": path,
        "cascade": cascade,
        "moved_items": affected_items,
        "deleted_items": deleted_items,
    }

@app.post("/api/move-to-folder")
async def api_move_to_folder(request: Request, filename: str = Form(...),
                             folder: str = Form("")):
    """Moves a history entry and its matching media entry to a folder.
    Empty folder string = move back to root. Folder path may be nested (e.g. 'A/B/C')."""
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    folder   = _validate_folder_name(folder)

    # Auto-create ancestor folders in folders.json so the UI tree keeps them visible
    _ensure_folder_tree(folder)
    if not _is_admin(request):
        # A pasta de destino passa a fazer parte da árvore que o funcionário vê.
        _add_public_folder(folder)

    moved = False
    with _history_lock:
        history = _load_history()
        for entry in history:
            if entry.get("file") == filename:
                entry["folder"] = folder
                moved = True
                break
        if moved:
            _atomic_write_json(HISTORY_FILE, history)

    with _media_lock:
        media = _load_media()
        changed = False
        for entry in media:
            if entry.get("file") == filename:
                entry["folder"] = folder
                changed = True
                break
        if changed:
            _atomic_write_json(MEDIA_FILE, media)

    if not moved and not changed:
        raise HTTPException(404, "Arquivo não encontrado em histórico nem mídia")
    return {"ok": True, "folder": folder}

def _validate_display_name(name: str) -> str:
    """Validate a user-supplied display name (rename). Unlike folder/file
    names this is just a label, not a path segment — only rejects empty
    input, path separators (would break downloads that build a filename
    from it) and an unreasonable length."""
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, "Nome não pode ficar em branco")
    if "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(400, "Nome não pode conter / \\ ou caracteres de controle")
    if len(name) > 150:
        raise HTTPException(400, "Nome muito longo (máx. 150 caracteres)")
    return name

@app.post("/api/rename/{filename}")
async def api_rename(filename: str, request: Request, new_name: str = Form(...)):
    """Renames the display name of a transcription (history entry) and, if
    present, its matching media-library entry. Does not touch the internal
    filename on disk — only the user-facing label shown in the UI and used
    as the download filename stem."""
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    new_name = _validate_display_name(new_name)

    updated = False
    with _history_lock:
        history = _load_history()
        for entry in history:
            if entry.get("file") == filename:
                entry["name"] = new_name
                updated = True
                break
        if updated:
            _atomic_write_json(HISTORY_FILE, history)

    with _media_lock:
        media = _load_media()
        for entry in media:
            if entry.get("file") == filename:
                # Media entries store the name WITH extension — keep whatever
                # extension was already there (or derive from the stored
                # filename) so downloads/original-name lookups stay correct.
                ext = os.path.splitext(entry.get("name", ""))[1] or os.path.splitext(filename)[1]
                entry["name"] = new_name + ext
                updated = True
                break
        if updated:
            _atomic_write_json(MEDIA_FILE, media)

    if not updated:
        raise HTTPException(404, "Arquivo não encontrado em histórico nem mídia")
    return {"ok": True, "name": new_name}

# ── Compressão de mídia ────────────────────────────────────────
# Vídeo em qualidade máxima come disco rápido — a faxina por idade existe por
# isso. Comprimir preserva o material: em vez de apagar um vídeo de 2 GB, ele
# vira 300 MB e continua assistível. No Apple Silicon a codificação roda no
# chip de mídia (videotoolbox), então é rápida e não briga com o Whisper pela CPU.

def _compress_update_catalogs(old_file: str, new_file: str, new_size: int) -> None:
    """Reflete nos catálogos o arquivo que acabou de ser comprimido.

    Compressão pode trocar o container (.mkv → .mp4), e aí o nome do arquivo
    muda. Sem atualizar media.json/history.json, o item apontaria para um
    arquivo que não existe mais.
    """
    if old_file == new_file:
        with _media_lock:
            media = _load_media()
            for m in media:
                if m.get("file") == old_file:
                    m["size_bytes"] = new_size
            _atomic_write_json(MEDIA_FILE, media)
        return

    with _media_lock:
        media = _load_media()
        for m in media:
            if m.get("file") == old_file:
                m["file"] = new_file
                m["size_bytes"] = new_size
                stem, ext = os.path.splitext(m.get("name") or "")
                if stem:
                    m["name"] = stem + os.path.splitext(new_file)[1]
        _atomic_write_json(MEDIA_FILE, media)
    with _history_lock:
        history = _load_history()
        touched = False
        for h in history:
            if h.get("file") == old_file:
                h["file"] = new_file
                touched = True
        if touched:
            _atomic_write_json(HISTORY_FILE, history)

def _run_compression(task_id: str, filename: str, preset: str,
                     replace: bool, prefer_hevc: bool) -> None:
    path = os.path.join(UPLOAD_DIR, filename)
    try:
        _set_task(task_id, status="processing", progress=0, phase="compress",
                  phase_progress=0, name=f"Comprimindo {filename}", filename=filename)

        # Selecionar 60 vídeos e clicar em Comprimir criava 60 threads e 60
        # processos ffmpeg de uma vez — a única fila do app sem teto. As demais
        # esperam aqui, e a task fica visível como "queued" enquanto isso.
        with _compress_sem:
            if _get_task(task_id).get("cancel_requested"):
                _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                          filename=filename, name="Compressão cancelada")
                return
            result = compressor.compress(
                path, preset, replace=replace, prefer_hevc=prefer_hevc,
                on_progress=lambda pct: _set_task(task_id, progress=pct,
                                                  phase="compress", phase_progress=pct),
                is_cancelled=lambda: bool(_get_task(task_id).get("cancel_requested")),
            )

        if result.get("skipped"):
            # Não é erro: o arquivo simplesmente não valia a pena comprimir.
            _set_task(task_id, status="done", progress=100, phase="done",
                      phase_progress=100, filename=filename,
                      skipped=True, message=result.get("reason") or "nada a fazer")
            return

        new_file = result["file"]
        _compress_update_catalogs(filename, new_file, result["new_bytes"])
        _set_task(task_id, status="done", progress=100, phase="done",
                  phase_progress=100, filename=new_file,
                  saved_bytes=result["saved_bytes"], saved_pct=result["saved_pct"],
                  old_bytes=result["old_bytes"], new_bytes=result["new_bytes"],
                  message=f"{result['saved_pct']}% menor")
    except compressor.CompressCancelled:
        _set_task(task_id, status="cancelled", progress=0, phase="cancelled",
                  filename=filename, name="Compressão cancelada")
    except Exception as e:   # noqa: BLE001
        _set_task(task_id, status="error", progress=0, phase="error",
                  filename=filename, error=str(e))

@app.get("/api/compress/capabilities")
async def api_compress_capabilities(request: Request):
    _role_of(request)
    caps = compressor.capabilities()
    return {**caps,
            "presets": [{"id": p, "label": compressor.PRESET_LABEL[p]}
                        for p in compressor.PRESETS]}

@app.get("/api/compress/plan/{filename}")
async def api_compress_plan(filename: str, request: Request, preset: str = "medio"):
    """Estimativa: quanto o arquivo encolheria, sem comprimir nada."""
    filename = _safe_filename(filename)
    _require_file_access(filename, request)
    if preset not in compressor.PRESETS:
        raise HTTPException(400, "Preset inválido")
    path = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Arquivo não encontrado")
    try:
        return compressor.plan(path, preset)
    except compressor.CompressError as e:
        raise HTTPException(400, str(e))

@app.post("/api/compress/upload")
async def api_compress_upload(request: Request,
                              file: UploadFile = File(...),
                              preset: str = Form("medio"),
                              prefer_hevc: str = Form("false")):
    """Comprime um arquivo enviado do computador do usuário.

    Diferente de /api/compress (que age sobre o que já está no acervo), aqui o
    arquivo chega agora, pelo navegador. Depois de comprimido ele fica na
    Biblioteca de Mídia, de onde pode ser baixado de volta — ou transcrito,
    já que cai no mesmo UPLOAD_DIR de sempre.
    """
    _require_admin(request)
    if not compressor.capabilities()["available"]:
        raise HTTPException(400, "FFmpeg não está instalado nesta máquina.")
    if preset not in compressor.PRESETS:
        raise HTTPException(400, "Preset inválido")

    original_name = file.filename or f"video_{uuid.uuid4().hex[:8]}.mp4"
    # Mesma sanitização do upload de transcrição: nome de exibição preservado,
    # nome de disco sem separadores nem controle.
    safe_stem = re.sub(r"[/\\\x00-\x1f]+", "_", original_name).strip() \
                or f"video_{uuid.uuid4().hex[:8]}.mp4"
    filename = f"{uuid.uuid4().hex[:8]}_{safe_stem}"
    upload_path = os.path.join(UPLOAD_DIR, filename)

    if compressor.media_kind(upload_path) == "other":
        raise HTTPException(400, "Formato não suportado — envie áudio ou vídeo.")

    # Streaming em blocos com teto, como o upload de transcrição.
    size = 0
    try:
        with open(upload_path, "wb") as f:
            while True:
                chunk = await file.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413, f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024*1024)} MB")
                f.write(chunk)
    except HTTPException:
        _safe_remove(upload_path)
        raise
    except Exception as e:
        _safe_remove(upload_path)
        raise HTTPException(500, f"Erro ao receber o arquivo: {e}")

    _mark_pending_visibility(filename, _visibility_for_new(request))
    _save_media(filename, original_name, is_transcribed=False, status="processing")

    task_id = str(uuid.uuid4())
    _set_task(task_id, status="queued", progress=0, phase="compress",
              name=f"Comprimindo {original_name}", filename=filename)
    threading.Thread(target=_run_compression,
                     args=(task_id, filename, preset, True, prefer_hevc == "true"),
                     daemon=True).start()
    return {"ok": True, "task_id": task_id, "file": filename,
            "name": original_name, "size_bytes": size}

@app.post("/api/compress")
async def api_compress(request: Request, files: str = Form(...),
                       preset: str = Form("medio"),
                       replace: str = Form("true"),
                       prefer_hevc: str = Form("false")):
    """Comprime um ou mais arquivos (lista separada por vírgula).

    Admin-only: reescreve arquivos do acervo — inclusive itens privados — e é
    irreversível para quem só tinha aquela cópia.
    """
    _require_admin(request)
    if not compressor.capabilities()["available"]:
        raise HTTPException(400, "FFmpeg não está instalado nesta máquina.")
    if preset not in compressor.PRESETS:
        raise HTTPException(400, "Preset inválido")

    requested = [_safe_filename(f.strip()) for f in (files or "").split(",") if f.strip()]
    if not requested:
        raise HTTPException(400, "Nenhum arquivo informado")

    started = []
    ocupados = []
    for filename in requested:
        path = os.path.join(UPLOAD_DIR, filename)
        if not os.path.exists(path) or compressor.media_kind(path) == "other":
            continue
        # Comprimir durante uma transcrição desalinhava tudo: a compressão pode
        # trocar o container (.mkv → .mp4) e renomear o arquivo, enquanto a
        # thread da transcrição segue com o nome ANTIGO capturado no início.
        # _save_media não achava mais aquele nome, criava uma entrada fantasma, e
        # o item real ficava presO em "processando" para sempre.
        if _active_task_for(filename):
            ocupados.append(filename)
            continue
        task_id = str(uuid.uuid4())
        _set_task(task_id, status="queued", progress=0, phase="compress",
                  name=f"Comprimindo {filename}", filename=filename)
        threading.Thread(
            target=_run_compression,
            args=(task_id, filename, preset, replace == "true", prefer_hevc == "true"),
            daemon=True).start()
        started.append({"file": filename, "task_id": task_id})

    if not started:
        if ocupados:
            raise HTTPException(409, "Os arquivos selecionados estão em uso agora "
                                     "(baixando ou transcrevendo). Tente quando terminarem.")
        raise HTTPException(400, "Nenhum arquivo compatível para compressão")
    return {"ok": True, "started": started, "count": len(started),
            "skipped_active": ocupados}

# ── Armazenamento (o que ocupa disco e como limpar) ────────────
def _storage_sincronizar_catalogos(cat_id: str, ids: list) -> None:
    """Depois de apagar arquivos, tira do catálogo o que ficou órfão.

    Sem isto a tela continuaria listando transcrições cujo conteúdo já não
    existe — o item viraria uma linha que não abre.
    """
    if cat_id == "uploads":
        alvos = set(ids)
        with _media_lock:
            media = [m for m in _load_media() if m.get("file") not in alvos]
            _atomic_write_json(MEDIA_FILE, media)
        # A transcrição continua válida; só perde o original. É o mesmo
        # comportamento da faxina por idade.
        with _history_lock:
            history = _load_history()
            mexeu = False
            for h in history:
                if h.get("file") in alvos and h.get("has_original") is not False:
                    h["has_original"] = False
                    mexeu = True
            if mexeu:
                _atomic_write_json(HISTORY_FILE, history)
    elif cat_id == "results":
        # Em results/ o item é a PASTA, cujo nome é o filename sem extensão.
        bases = set(ids)
        with _history_lock:
            history = [h for h in _load_history()
                       if _result_base(h.get("file") or "") not in bases]
            _atomic_write_json(HISTORY_FILE, history)

@app.get("/api/storage")
async def api_storage(request: Request):
    """Quanto cada tipo de dado ocupa. Admin-only: enxerga o acervo inteiro."""
    _require_admin(request)
    resumo = storage.overview()
    resumo["sobras"] = storage.sobras()
    return resumo

@app.get("/api/storage/{cat_id}")
async def api_storage_itens(request: Request, cat_id: str):
    _require_admin(request)
    try:
        return storage.listar_itens(cat_id)
    except KeyError:
        raise HTTPException(404, "Categoria desconhecida")

@app.get("/api/storage/{cat_id}/preview/{item_id}")
async def api_storage_preview(request: Request, cat_id: str, item_id: str):
    """Prévia do conteúdo de um item, para conferir antes de apagar."""
    _require_admin(request)
    try:
        return storage.preview(cat_id, item_id)
    except KeyError:
        raise HTTPException(404, "Categoria desconhecida")
    except FileNotFoundError:
        raise HTTPException(404, "Item não encontrado")

_MIME_POR_EXT = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}

@app.get("/api/storage/{cat_id}/stream/{item_id}")
async def api_storage_stream(request: Request, cat_id: str, item_id: str):
    """Serve mídia para TOCAR na página, não para baixar.

    /api/download-media manda Content-Disposition: attachment — correto para o
    botão "baixar", mas faz o navegador tratar a resposta como download, e um
    <video> apontado para lá não reproduz. Além disso os arquivos aqui chegam a
    2 GB: sem Range o navegador tentaria puxar tudo antes de mostrar o primeiro
    quadro, o que travava a página. FileResponse responde 206 por trecho, então
    com preload="metadata" só o cabeçalho do arquivo é lido.
    """
    _require_admin(request)
    try:
        caminho = storage.caminho_de(cat_id, item_id)
    except KeyError:
        raise HTTPException(404, "Categoria desconhecida")
    except FileNotFoundError:
        raise HTTPException(404, "Item não encontrado")
    ext = os.path.splitext(caminho)[1].lower()
    return FileResponse(caminho,
                        media_type=_MIME_POR_EXT.get(ext, "application/octet-stream"))

@app.get("/api/storage/{cat_id}/arquivo/{item_id}")
async def api_storage_arquivo(request: Request, cat_id: str, item_id: str):
    """Serve o arquivo em si (miniatura, planilha) para a prévia."""
    _require_admin(request)
    try:
        return FileResponse(storage.caminho_de(cat_id, item_id))
    except KeyError:
        raise HTTPException(404, "Categoria desconhecida")
    except FileNotFoundError:
        raise HTTPException(404, "Item não encontrado")

@app.post("/api/storage/{cat_id}/delete")
async def api_storage_delete(request: Request, cat_id: str, ids: str = Form(...)):
    """Apaga itens escolhidos. `ids` vem separado por \\n (nome de arquivo
    pode conter vírgula)."""
    _require_admin(request)
    lista = [i for i in (ids or "").split("\n") if i.strip()]
    if not lista:
        raise HTTPException(400, "Nenhum item informado")
    try:
        if cat_id == "sobras":
            res = storage.apagar_sobras(lista)
        else:
            res = storage.apagar(cat_id, lista)
            _storage_sincronizar_catalogos(cat_id, lista)
    except KeyError:
        raise HTTPException(404, "Categoria desconhecida")
    return res

@app.post("/api/storage/{cat_id}/clear")
async def api_storage_clear(request: Request, cat_id: str):
    """Esvazia uma categoria inteira — oferecido só para as regeneráveis."""
    _require_admin(request)
    cfg = storage.CATEGORIAS.get(cat_id)
    if not cfg:
        raise HTTPException(404, "Categoria desconhecida")
    if not cfg["regeneravel"]:
        raise HTTPException(400, "Esta categoria guarda conteúdo seu — "
                                 "apague item por item.")
    return storage.limpar_categoria(cat_id)

# ── Assinaturas (acompanhar canais/perfis) ─────────────────────
# Assina um canal/perfil e o poller traz o que sai de novo, já mandando para o
# mesmo pipeline de download/transcrição do app. A lógica de agendamento e o
# store vivem em subscriptions.py; aqui ficam só os "descobridores" (que sabem
# listar o conteúdo de cada rede) e as rotas.

def _discover_youtube(target: str, limit: int) -> list:
    """Lista os vídeos mais recentes de um canal/playlist do YouTube.

    Usa o yt-dlp em modo raso (extract_flat): não baixa nada, só lê a listagem —
    é barato e aceita qualquer forma de URL (@handle, /channel/, /c/, playlist).
    """
    if not YT_DLP_OK:
        raise RuntimeError("yt-dlp não está instalado")
    url = target
    # Numa URL de canal, a raiz devolve as abas (vídeos, shorts, lives) como
    # playlists aninhadas. Apontar direto para /videos dá a lista limpa.
    if re.search(r"youtube\.com/(@[\w.-]+|c/[\w.-]+|channel/[\w-]+|user/[\w.-]+)/?$", url):
        url = url.rstrip("/") + "/videos"
    opts = {
        "quiet": True, "nocolor": True, "skip_download": True,
        "extract_flat": "in_playlist", "playlistend": max(1, int(limit)),
    }
    _apply_network_opts(opts, url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    out = []
    for e in (info or {}).get("entries") or []:
        if not isinstance(e, dict):
            continue
        vid = e.get("id")
        if not vid:
            continue
        out.append({
            "id": f"youtube:{vid}",
            "url": e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title") or vid,
        })
    return out

def _make_social_discover(platform: str):
    """Descobridor para IG/TikTok/Facebook: reusa a interceptação do ego-lite,
    que é o que já funciona para essas redes (o yt-dlp não dá conta)."""
    def _discover(target: str, limit: int) -> list:
        if not (SOCIAL_OK and SOCIAL_MULTI_OK):
            raise RuntimeError("módulo de redes sociais indisponível")
        res = social_intercept.collect(platform, target, max_items=max(1, int(limit)))
        ds = social_core.load_dataset(res["path"])
        out = []
        for r in ds.get("rows") or []:
            code, link = r.get("code"), r.get("url")
            if not code or not link:
                continue
            out.append({"id": f"{platform}:{code}", "url": link,
                        "title": (r.get("caption") or "")[:80] or code})
        return out
    return _discover

def _subs_kickoff_transcribe(url: str, model: str, language: str, folder: str) -> None:
    """Ponte para o pipeline de transcrição (o poller não conhece as rotas)."""
    url = _validate_media_url(url)
    _validate_transcribe_params(model, "transcribe")
    _kickoff_url_transcription(url, model, language, "transcribe", False, folder,
                               visibility=VIS_PRIVATE)

def _subs_kickoff_download(url: str, folder: str) -> None:
    url = _validate_media_url(url)
    # `folder` era recebido e descartado: assinatura em modo "só baixar" ignorava
    # a pasta de destino escolhida no cadastro.
    if folder:
        _ensure_folder_tree(folder)
    _kickoff_download_only(url, "video", "best", visibility=VIS_PRIVATE,
                           folder=folder or "")

def _configure_subscriptions() -> None:
    subscriptions.configure(
        discover={
            "youtube":   _discover_youtube,
            "instagram": _make_social_discover("instagram"),
            "tiktok":    _make_social_discover("tiktok"),
            "facebook":  _make_social_discover("facebook"),
        },
        kickoff_transcribe=_subs_kickoff_transcribe,
        kickoff_download=_subs_kickoff_download,
        log=lambda msg: print(msg),
    )

@app.get("/api/subscriptions")
async def api_subscriptions(request: Request):
    """Assinaturas são ferramenta de administração: baixam sozinhas para o
    acervo e usam a sessão logada do ego-lite. Só o admin."""
    _require_admin(request)
    return {"subscriptions": subscriptions.list_subscriptions(),
            "platforms": list(subscriptions.PLATFORMS),
            "poller_running": True}

@app.post("/api/subscriptions")
async def api_subscriptions_add(request: Request,
                                platform: str = Form(...),
                                target: str = Form(...),
                                label: str = Form(""),
                                auto_transcribe: str = Form("true"),
                                model: str = Form("turbo"),
                                language: str = Form("pt"),
                                folder: str = Form(""),
                                interval_hours: float = Form(6.0),
                                max_per_check: int = Form(5),
                                initial_import: int = Form(0)):
    _require_admin(request)
    if auto_transcribe == "true":
        _validate_transcribe_params(model, "transcribe")
    folder = _validate_folder_name(folder) if folder else ""
    _ensure_folder_tree(folder)
    try:
        return subscriptions.add_subscription(
            platform, target, label=label,
            auto_transcribe=(auto_transcribe == "true"),
            model=model, language=language, folder=folder,
            interval_hours=interval_hours, max_per_check=max_per_check,
            initial_import=initial_import)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/subscriptions/{sub_id}")
async def api_subscriptions_update(request: Request, sub_id: str,
                                   label: str = Form(None),
                                   auto_transcribe: str = Form(None),
                                   model: str = Form(None),
                                   language: str = Form(None),
                                   folder: str = Form(None),
                                   interval_hours: str = Form(None),
                                   max_per_check: str = Form(None),
                                   paused: str = Form(None)):
    _require_admin(request)
    changes: dict = {}
    if label is not None:           changes["label"] = label
    if auto_transcribe is not None: changes["auto_transcribe"] = auto_transcribe == "true"
    if model is not None:
        _validate_transcribe_params(model, "transcribe")
        changes["model"] = model
    if language is not None:        changes["language"] = language
    if folder is not None:
        changes["folder"] = _validate_folder_name(folder) if folder else ""
        _ensure_folder_tree(changes["folder"])
    if interval_hours is not None:  changes["interval_hours"] = float(interval_hours)
    if max_per_check is not None:   changes["max_per_check"] = int(max_per_check)
    if paused is not None:          changes["paused"] = paused == "true"
    try:
        return subscriptions.update_subscription(sub_id, **changes)
    except KeyError:
        raise HTTPException(404, "Assinatura não encontrada")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))

@app.delete("/api/subscriptions/{sub_id}")
async def api_subscriptions_delete(request: Request, sub_id: str):
    _require_admin(request)
    if not subscriptions.remove_subscription(sub_id):
        raise HTTPException(404, "Assinatura não encontrada")
    return {"ok": True}

@app.post("/api/subscriptions/{sub_id}/download-latest")
async def api_subscriptions_download_latest(request: Request, sub_id: str,
                                            quantidade: int = Form(5)):
    """Baixa agora os N mais recentes do canal, sem esperar sair novidade."""
    _require_admin(request)
    if not subscriptions._get(sub_id):
        raise HTTPException(404, "Assinatura não encontrada")
    # Em thread: a coleta abre o navegador nas redes sociais e demora.
    threading.Thread(
        target=lambda: subscriptions.download_latest(sub_id, quantidade),
        daemon=True).start()
    return {"ok": True, "message": f"Buscando os {quantidade} mais recentes…"}

@app.post("/api/subscriptions/{sub_id}/check")
async def api_subscriptions_check(request: Request, sub_id: str):
    """Checa agora, sem esperar o intervalo. Roda em thread: a coleta pode
    demorar (abre o navegador nas redes sociais) e não pode travar o request."""
    _require_admin(request)
    if not subscriptions._get(sub_id):
        raise HTTPException(404, "Assinatura não encontrada")
    threading.Thread(
        target=lambda: subscriptions.check_subscription(sub_id, force=True),
        daemon=True).start()
    return {"ok": True, "message": "Checagem iniciada — o resultado aparece aqui em instantes."}

# ── Entry point ────────────────────────────────────────────────
def _reexec_into_venv_if_needed() -> None:
    """Garante que o app rode SEMPRE no Python do venv do projeto.

    Se alguém iniciar com o `python3` do sistema (ex.: `python3 whisper-app.py`),
    o processo acaba usando um yt-dlp mais antigo — e o `/api/ytdlp/status` passa
    a reportar "desatualizado" para sempre, além do botão "Atualizar agora" falhar
    (o yt-dlp-ejs exige Python 3.10+). Aqui detectamos isso e re-executamos o
    próprio script no `venv/bin/python`, corrigindo sozinho em vez de depender de
    o usuário lembrar de usar o interpretador certo."""
    import sys, os
    script_dir = os.path.dirname(os.path.abspath(__file__))
    venv_dir   = os.path.join(script_dir, "venv")
    venv_py    = os.path.join(venv_dir, "bin", "python")
    # sys.prefix aponta para a raiz do venv quando já estamos dentro dele; isso
    # evita o problema de resolver symlinks (venv/bin/python costuma apontar para
    # o Python-base) que poderia causar um loop de re-exec.
    already_in_venv = os.path.abspath(sys.prefix) == os.path.abspath(venv_dir)
    if os.path.exists(venv_py) and not already_in_venv:
        print(f"↻  Reiniciando no Python do venv ({venv_py})…")
        os.execv(venv_py, [venv_py, os.path.abspath(__file__), *sys.argv[1:]])

if __name__ == "__main__":
    _reexec_into_venv_if_needed()
    # Padrão continua 127.0.0.1: no Mac o app não deve escutar na rede local
    # sozinho (quem publica é o túnel). Dentro de um container é o oposto — se
    # não escutar em 0.0.0.0 o proxy do host não alcança nada, então lá o
    # WHISPER_HOST=0.0.0.0 é obrigatório.
    host = os.environ.get("WHISPER_HOST", "127.0.0.1")
    port = int(os.environ.get("WHISPER_PORT", "7860"))
    print(f"✅  Whisper Transcritor → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
