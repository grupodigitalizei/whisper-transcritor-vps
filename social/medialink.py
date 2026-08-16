"""Métricas e download de qualquer rede social, via yt-dlp.

Cobre TikTok, YouTube, Facebook, X/Twitter, Kwai, Vimeo, Twitch e +1000 sites.
Normaliza tudo para o MESMO formato de row do Instagram, então a grade, os
filtros, o outlier score e o Excel funcionam sem código específico por rede.
"""
import datetime as dt
import os
import re

from .core import DATA_DIR, MEDIA_DIR

# Redes cujo perfil/canal rende uma lista de vídeos (não um vídeo só)
PLATFORM_NAMES = {
    "TikTok": "TikTok", "Youtube": "YouTube", "YoutubeTab": "YouTube",
    "Instagram": "Instagram", "Facebook": "Facebook", "Twitter": "X (Twitter)",
    "Kwai": "Kwai", "Vimeo": "Vimeo", "Twitch": "Twitch",
}


def _safe(s):
    return re.sub(r"[^\w\-.]", "_", s or "")[:60]


def platform_of(info):
    key = (info.get("extractor_key") or info.get("extractor") or "").split(":")[0]
    return PLATFORM_NAMES.get(key, key or "Desconhecido")


def _ts(info):
    if info.get("timestamp"):
        return int(info["timestamp"])
    d = info.get("upload_date")  # YYYYMMDD
    if d and len(str(d)) == 8:
        try:
            return int(dt.datetime.strptime(str(d), "%Y%m%d").timestamp())
        except ValueError:
            pass
    return None


def _thumb(info):
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    if thumbs:
        return max(thumbs, key=lambda t: (t.get("width") or 0)).get("url")
    return None


def _row(info, platform=None):
    """Converte um item do yt-dlp no row usado pelo resto do app."""
    ts = _ts(info)
    dur = info.get("duration")
    caption = info.get("description") or info.get("title") or ""
    return {
        "code": info.get("id") or "",
        "url": info.get("webpage_url") or info.get("url") or "",
        "type": "Reel/Vídeo",
        "pinned": False,
        "date": dt.datetime.fromtimestamp(ts).isoformat() if ts else None,
        "ts": ts,
        "caption": caption,
        "hashtags": re.findall(r"#(\w+)", caption),
        "likes": info.get("like_count") or 0,
        "comments": info.get("comment_count") or 0,
        "reshares": info.get("repost_count") or 0,
        "views": info.get("view_count"),
        "duration_s": round(dur, 1) if dur else None,
        "followers": info.get("channel_follower_count"),
        "thumb_url": _thumb(info),
        "media_urls": [{"type": "video", "url": info.get("url")}]
                      if info.get("url", "").startswith("http") else [],
        "username": (info.get("uploader_id") or info.get("uploader")
                     or info.get("channel") or ""),
        "platform": platform or platform_of(info),
        "title": info.get("title") or "",
    }


def _ydl(extra=None, cookies_browser=None):
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "noprogress": True,
            "ignoreerrors": True, "extract_flat": False}
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _detail(video_url, platform, cookies_browser, fallback, tries=3):
    """Extração completa de um vídeo — é aqui que views/likes aparecem.

    O YouTube devolve erros transitórios ("page needs to be reloaded") e
    bot-checks sob concorrência, então vale repetir com espera crescente.
    """
    import time
    for attempt in range(tries):
        try:
            with _ydl({"noplaylist": True}, cookies_browser) as ydl:
                info = ydl.extract_info(video_url, download=False)
            if info and (info.get("view_count") is not None or info.get("duration")):
                return _row(info, platform)
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
    return _row(fallback, platform)      # ao menos título/thumb da lista rasa


def probe(url, max_items=60, cookies_browser=None, on_progress=None):
    """Lê métricas SEM baixar. Aceita link de vídeo único ou de perfil/canal.

    Perfil roda em duas fases: lista rasa (rápida) e depois os detalhes de cada
    vídeo em paralelo — a lista rasa não traz views/likes em nenhuma rede.
    """
    url = url.strip()
    if not url.startswith("http"):
        raise RuntimeError("informe uma URL completa (https://…)")

    with _ydl({"extract_flat": "in_playlist", "playlistend": int(max_items)},
              cookies_browser) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise RuntimeError("não consegui ler essa URL (privada, removida ou "
                           "exige login — tente marcar 'usar login do Chrome')")

    platform = platform_of(info)
    entries = info.get("entries")

    if entries is None:                      # vídeo único: info já veio completa
        rows = [_row(info, platform)]
        profile = {"username": rows[0]["username"],
                   "full_name": info.get("uploader") or info.get("channel"),
                   "followers": info.get("channel_follower_count"),
                   "profile_pic": None, "platform": platform}
        if on_progress:
            on_progress(1, 1)
        return {"platform": platform, "profile": profile, "rows": rows}

    import concurrent.futures
    entries = [e for e in entries if e][:int(max_items)]
    total, done = len(entries), [0]

    def work(e):
        row = _detail(e.get("url") or e.get("webpage_url"), platform,
                      cookies_browser, e)
        done[0] += 1
        if on_progress:
            on_progress(done[0], total)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        rows = list(ex.map(work, entries))

    followers = next((r["followers"] for r in rows if r.get("followers")), None)
    profile = {
        "username": (info.get("uploader_id") or info.get("channel")
                     or info.get("title") or ""),
        "full_name": info.get("uploader") or info.get("channel") or info.get("title"),
        "followers": followers or info.get("channel_follower_count"),
        "profile_pic": None, "platform": platform,
        "posts_total": info.get("playlist_count"),
    }
    return {"platform": platform, "profile": profile, "rows": rows}


def save_dataset(probe_result):
    """Grava o resultado como dataset, para aparecer na lista lateral."""
    rows = probe_result["rows"]
    prof = probe_result["profile"]
    name = _safe(prof.get("username") or probe_result["platform"]) or "link"
    ds_id = f"{name}_{dt.datetime.now():%Y-%m-%d_%H%M}"
    path = os.path.join(DATA_DIR, ds_id + ".json")
    payload = {"collected_at": dt.datetime.now().isoformat(),
               "source": "yt-dlp", "platform": probe_result["platform"],
               "profile": prof, "count": len(rows), "rows": rows}
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return ds_id, path


def _download_via_browser(url, dest, on_progress=None):
    """Plano B: o navegador logado resolve a URL do CDN e o Python baixa.

    Cobre o que o yt-dlp não alcança — Instagram, YouTube com proteção de
    token e qualquer conteúdo que exija a sessão do usuário.
    """
    from . import intercept
    from . import downloader
    found = intercept.resolve_media(url)
    media = found.get("video") or found.get("image")
    ext = ".mp4" if found.get("video") else ".jpg"
    name = _safe(re.sub(r"^https?://", "", url).replace("/", "_")) + ext
    path = os.path.join(dest, name)

    # A URL vem do og:video/og:image da página — ou seja, de conteúdo controlado
    # por quem publicou o post. Baixar isso com um requests.get solto deixaria o
    # servidor buscar qualquer endereço (inclusive interno) a mando de terceiro.
    # download_media aplica a mesma defesa do caminho principal: allowlist de CDN
    # verificada a cada hop de redirect, tamanho mínimo e conferência de magic
    # bytes (para não salvar uma página de erro com extensão .mp4).
    if not media:
        raise RuntimeError("nenhuma mídia encontrada na página")
    downloader.download_media(media, path, timeout=120)
    if on_progress:
        try:
            on_progress(os.path.getsize(path), os.path.getsize(path), name)
        except OSError:
            pass
    note = ""
    if not found.get("video"):
        note = ("Só a imagem estava disponível nessa página. Se era um vídeo, "
                "faça login nessa rede dentro do ego lite e tente de novo.")
    return {"dest": dest, "file": path, "title": found.get("title") or name,
            "platform": "navegador", "via": "browser", "note": note, "row": None}


def download(url, dest=None, audio_only=False, cookies_browser=None,
             on_progress=None):
    """Baixa o vídeo (ou só o áudio) na melhor qualidade disponível."""
    dest = dest or os.path.join(MEDIA_DIR, "links")
    os.makedirs(dest, exist_ok=True)
    saved = []

    def hook(d):
        if d.get("status") == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            on_progress(d.get("downloaded_bytes", 0), total,
                        os.path.basename(d.get("filename") or ""))
        elif d.get("status") == "finished":
            saved.append(d.get("filename"))

    opts = {
        "outtmpl": os.path.join(dest, "%(extractor_key)s_%(uploader_id)s_%(id)s.%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "format": "bestaudio/best" if audio_only else
                  "bestvideo*+bestaudio/best",
        "merge_output_format": "mp4",
    }
    if audio_only:
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                   "preferredcodec": "mp3"}]
    opts["ignoreerrors"] = False        # queremos ver a falha p/ cair no plano B
    # TikTok em particular quebra de forma intermitente ("Unable to extract
    # universal data for rehydration" — yt-dlp/yt-dlp#17332, em aberto): a
    # mesma URL falha numa tentativa e baixa normalmente na próxima, sem
    # nenhuma mudança de nossa parte. Repetir algumas vezes antes de cair
    # pro plano B (navegador) resolve a maioria desses casos.
    import time
    info = None
    for attempt in range(3):
        try:
            with _ydl(opts, cookies_browser) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception:
            info = None
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    # o arquivo final pode ser o mesclado (vídeo+áudio), com nome diferente
    final = None
    if info:
        for d in (info.get("requested_downloads") or []):
            final = d.get("filepath") or d.get("_filename") or final
    if not final and saved:
        final = saved[-1]
    if final and audio_only:
        mp3 = os.path.splitext(final)[0] + ".mp3"
        if os.path.isfile(mp3):
            final = mp3

    if not final or not os.path.isfile(final):
        if audio_only:
            raise RuntimeError("não consegui extrair o áudio dessa URL "
                               "(tente baixar o vídeo e converter depois)")
        return _download_via_browser(url, dest, on_progress)

    return {"dest": dest, "file": final, "title": info.get("title"),
            "platform": platform_of(info), "via": "yt-dlp", "row": _row(info)}
