#!/usr/bin/env python3
"""Cascata de motores de download: se um jeito falha, tenta o próximo.

Por que existe
──────────────
O caminho de redes sociais já tinha plano B (3 tentativas de yt-dlp e depois o
navegador logado do ego-lite — ver social/medialink.py). A rota genérica, que
atende YouTube e todo o resto, não tinha nenhum: uma falha de extração matava o
download ali mesmo. E justamente o YouTube é o que quebra mais, porque muda o
player com frequência (SABR/PO-token, yt-dlp#12482) — e o sintoma clássico é
que o MESMO vídeo baixa se você trocar o `player_client`.

Como funciona
─────────────
Cada "motor" é uma variação das opções do yt-dlp aplicada sobre as opções que o
chamador já montou. O motor 1 é sempre `base_opts` INTACTO — ou seja, o caminho
que já funciona hoje continua exatamente igual, e os motores seguintes só entram
em cena quando o primeiro levantou exceção. Entre tentativas os arquivos
parciais são limpos, senão um `.part` truncado envenena a tentativa seguinte.

O que este módulo deliberadamente NÃO faz
─────────────────────────────────────────
Não chama o navegador (ego-lite). O `_download_via_browser` devolve um dicionário
de formato próprio, diferente do `info` do yt-dlp que as rotas genéricas
consomem — encaixá-lo aqui exigiria mudar os chamadores. O caminho social, que
já sabe lidar com esse retorno, continua com o plano B dele intacto.
"""
from __future__ import annotations

import copy
import os
import time
from urllib.parse import urlparse

# Espera entre tentativas (segundos). Falha de extração costuma ser transitória:
# uma pausa curta resolve boa parte, e o backoff evita martelar o site.
_BACKOFF_SECS = (2, 4, 6)

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS)


def _wants_audio(opts: dict) -> bool:
    """Descobre se o chamador quer áudio, para degradar o formato sem trocar a
    intenção (degradar um pedido de áudio para 'best' baixaria vídeo inteiro)."""
    for pp in opts.get("postprocessors") or []:
        if pp.get("key") == "FFmpegExtractAudio":
            return True
    fmt = str(opts.get("format") or "")
    return "audio" in fmt and "bestvideo" not in fmt


# ── mutadores: cada um devolve uma variação das opções ──────────────────────
def _as_is(opts: dict, url: str) -> dict:
    return opts


def _no_cookies(opts: dict, url: str) -> dict:
    """Sem os cookies do Chrome. Cookie velho/expirado é causa comum de 403 —
    e muito conteúdo público baixa melhor sem sessão nenhuma."""
    out = copy.deepcopy(opts)
    out.pop("cookiesfrombrowser", None)
    out.pop("cookiefile", None)
    return out


def _yt_client(clients: list[str]):
    """Troca o player_client do YouTube (e tira os cookies: estes clientes são
    justamente os que funcionam sem sessão)."""
    def _mut(opts: dict, url: str) -> dict:
        out = _no_cookies(opts, url)
        extractor_args = copy.deepcopy(out.get("extractor_args") or {})
        extractor_args.setdefault("youtube", {})["player_client"] = list(clients)
        out["extractor_args"] = extractor_args
        return out
    return _mut


def proxies_from_env() -> list[str]:
    """Lista de proxies em WHISPER_YTDLP_PROXY, separados por vírgula/espaço.

    Um proxy só já ajuda contra o 403 de IP de datacenter, mas proxy público
    morre o tempo todo — medimos 8 de 10 fora do ar numa lista recém-publicada.
    Aceitar vários deixa a cascata trocar sozinha em vez de exigir que alguém
    edite a variável e reinicie o container toda vez que um cai."""
    raw = os.environ.get("WHISPER_YTDLP_PROXY", "") or ""
    vistos, out = set(), []
    for p in raw.replace(",", " ").split():
        p = p.strip()
        if p and p not in vistos:
            vistos.add(p)
            out.append(p)
    return out


def _with_proxy(proxy: str):
    """Troca o proxy. Sem mexer em mais nada: quando o bloqueio é do IP, o
    player_client é irrelevante — o que muda o resultado é por onde se sai."""
    def _mut(opts: dict, url: str) -> dict:
        out = copy.deepcopy(opts)
        out["proxy"] = proxy
        return out
    return _mut


def _short(proxy: str) -> str:
    """Identifica o proxy no log/histórico sem expor usuário e senha."""
    try:
        parsed = urlparse(proxy if "://" in proxy else "http://" + proxy)
        return parsed.hostname or proxy
    except ValueError:
        return "proxy"


def _degraded_format(opts: dict, url: str) -> dict:
    """Último recurso: pede o formato mais simples que existir. Resolve o caso
    de a combinação pedida (ex.: bestvideo+bestaudio numa altura específica) não
    estar disponível — melhor uma qualidade menor do que nenhum arquivo."""
    out = copy.deepcopy(opts)
    out["format"] = "bestaudio/best" if _wants_audio(opts) else "best"
    return out


class _Engine:
    __slots__ = ("name", "label", "mutate")

    def __init__(self, name: str, label: str, mutate):
        self.name = name        # id curto, vai para o histórico
        self.label = label      # texto que o usuário lê na UI
        self.mutate = mutate


def engines_for(url: str) -> list[_Engine]:
    """Cadeia de motores para esta URL. O primeiro é sempre o atual, intacto."""
    chain = [_Engine("padrao", "padrão", _as_is)]
    if _is_youtube(url):
        # Ordem escolhida por taxa de acerto observada nos relatos do yt-dlp
        # quando o cliente 'web' falha por SABR/PO-token.
        chain += [
            _Engine("android", "cliente Android", _yt_client(["android"])),
            _Engine("ios_tv", "cliente iOS/TV", _yt_client(["ios", "tv_embedded"])),
        ]
    else:
        chain.append(_Engine("sem_cookies", "sem cookies", _no_cookies))
    # Os proxies seguintes entram DEPOIS das variações de cliente: se o primeiro
    # proxy está vivo e o vídeo só precisava de outro player_client, resolve sem
    # gastar uma troca de rota. O primeiro proxy da lista já veio aplicado em
    # base_opts pelo chamador, por isso começamos do segundo.
    for proxy in proxies_from_env()[1:]:
        chain.append(_Engine(f"proxy:{_short(proxy)}",
                             f"proxy {_short(proxy)}",
                             _with_proxy(proxy)))
    chain.append(_Engine("formato_simples", "formato simples", _degraded_format))
    return chain


def run_with_fallback(url: str, base_opts: dict, ydl_factory, *,
                      abort_types: tuple = (),
                      on_engine=None,
                      on_before_retry=None,
                      log=None):
    """Baixa `url` tentando cada motor até um funcionar.

    ydl_factory(opts)  → context manager do yt-dlp (permite testar sem yt-dlp).
    abort_types        → exceções que NÃO são falha de motor (cancelamento do
                         usuário, por exemplo): sobem na hora, sem retry.
    on_engine(engine, i, total)   → avisa qual motor vai ser tentado agora.
    on_before_retry()  → chamado antes de cada nova tentativa (limpar parciais).

    Retorna (info, engine_name). Levanta a última exceção se todos falharem.
    """
    chain = engines_for(url)
    total = len(chain)
    last_exc: BaseException | None = None

    for idx, engine in enumerate(chain):
        if idx > 0:
            # Um .part truncado da tentativa anterior faria o yt-dlp retomar
            # bytes inválidos — limpa antes de tentar de novo.
            if on_before_retry:
                try:
                    on_before_retry()
                except Exception:
                    pass
            time.sleep(_BACKOFF_SECS[min(idx - 1, len(_BACKOFF_SECS) - 1)])

        if on_engine:
            try:
                on_engine(engine, idx + 1, total)
            except Exception:
                pass

        try:
            opts = engine.mutate(base_opts, url)
            with ydl_factory(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            if log and idx > 0:
                log(f"download OK com o motor '{engine.name}' "
                    f"(tentativa {idx + 1}/{total})")
            return info, engine.name
        except abort_types:
            raise                      # cancelamento: não é falha, não retenta
        except Exception as exc:        # noqa: BLE001 — qualquer falha vira fallback
            last_exc = exc
            if log:
                log(f"motor '{engine.name}' falhou ({type(exc).__name__}: {exc}); "
                    f"{'tentando o próximo' if idx + 1 < total else 'sem mais motores'}")

    assert last_exc is not None        # o loop sempre roda ao menos uma vez
    raise last_exc
