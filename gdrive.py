"""Download de arquivos PÚBLICOS do Google Drive.

O yt-dlp tem um extractor de Drive, mas ele quebra com frequência em arquivos
grandes (a página de confirmação "não foi possível verificar vírus") e nem
sempre reconhece o arquivo. Este módulo baixa direto pelo endpoint de download
do Drive, tratando o token de confirmação — funciona para qualquer arquivo
público (vídeo, áudio, etc.), de qualquer tamanho.

Self-contained: depende só de `requests`. Nada de FastAPI aqui.
"""
import os
import re
import urllib.parse

import requests

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_GDRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
# Formatos de link aceitos:
#   https://drive.google.com/file/d/<ID>/view?usp=sharing
#   https://drive.google.com/open?id=<ID>
#   https://drive.google.com/uc?id=<ID>&export=download
#   https://docs.google.com/uc?id=<ID>
#   https://drive.usercontent.google.com/download?id=<ID>
_ID_PATTERNS = (
    r"/file/d/([A-Za-z0-9_-]{10,})",
    r"[?&]id=([A-Za-z0-9_-]{10,})",
    r"/d/([A-Za-z0-9_-]{10,})",
)


class GDriveCancelled(Exception):
    """Cancelamento cooperativo pedido pelo chamador (cancel_cb retornou True)."""


def is_gdrive_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in _GDRIVE_HOSTS


def file_id(url: str) -> str | None:
    for pat in _ID_PATTERNS:
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    return None


def _filename_from_headers(resp, fallback: str) -> str:
    cd = resp.headers.get("Content-Disposition", "") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(1))
        except Exception:
            pass
    m = re.search(r'filename="([^"]+)"', cd)
    if m:
        return m.group(1)
    return fallback


def _looks_like_html(resp) -> bool:
    return "text/html" in (resp.headers.get("Content-Type", "") or "").lower()


def _parse_confirm_form(html: str):
    """Extrai (action, {inputs}) do <form> da página de confirmação do Drive.

    Robusto à ordem dos atributos: lê cada <input> e pega name/value separados
    (o Google varia a ordem, então um regex `name=…\\s+value=…` rígido falha)."""
    fm = re.search(r'<form\b[^>]*\bid="download-form"[^>]*>', html, re.I) \
        or re.search(r'<form\b[^>]*>', html, re.I)
    action = None
    if fm:
        am = re.search(r'\baction="([^"]+)"', fm.group(0))
        if am:
            action = am.group(1).replace("&amp;", "&")
    inputs = {}
    for tag in re.findall(r'<input\b[^>]*>', html, re.I):
        nm = re.search(r'\bname="([^"]+)"', tag)
        if not nm:
            continue
        vm = re.search(r'\bvalue="([^"]*)"', tag)
        inputs[nm.group(1)] = vm.group(1).replace("&amp;", "&") if vm else ""
    return action, inputs


def download(url: str, dest_dir: str, base_stem: str, force_filename: str | None = None,
             on_start=None, progress_cb=None, cancel_cb=None, timeout: int = 30):
    """Baixa o arquivo público do Drive.

    - dest_dir/base_stem: destino; a extensão real vem do Content-Disposition
      (a menos que force_filename seja dado — usado em retry).
    - on_start(dest_path, title, total_bytes): chamado assim que o nome/tamanho
      reais são conhecidos, ANTES de começar a gravar (para o app criar a
      entrada no catálogo e mostrar progresso).
    - progress_cb(pct 0..100): progresso; pode levantar para abortar.
    - cancel_cb() -> bool: se True, aborta com GDriveCancelled.

    Retorna (dest_path, title). Levanta RuntimeError com motivo amigável.
    """
    fid = file_id(url)
    if not fid:
        raise RuntimeError("Não consegui identificar o arquivo no link do Google Drive. "
                           "Use um link de arquivo (…/file/d/ID/… ou …?id=ID).")
    # Dois métodos independentes, do mais robusto ao de reserva:
    #  1) yt-dlp (extractor de Google Drive — trata o token de confirmação e
    #     arquivos grandes de forma testada em produção);
    #  2) downloader manual via requests (se o yt-dlp não der conta).
    erros = []
    for metodo in (_download_ytdlp, _download_requests):
        try:
            return metodo(url, fid, dest_dir, base_stem, force_filename,
                          on_start, progress_cb, cancel_cb, timeout)
        except GDriveCancelled:
            raise
        except Exception as e:
            erros.append(f"{metodo.__name__.replace('_download_', '')}: {e}")
    raise RuntimeError("Não consegui baixar do Google Drive — confira se o arquivo "
                       "está como 'Qualquer pessoa com o link'. (" + " | ".join(erros)[-260:] + ")")


def _download_ytdlp(url, fid, dest_dir, base_stem, force_filename,
                    on_start, progress_cb, cancel_cb, timeout):
    """Método 1: yt-dlp resolve o token de confirmação do Drive nativamente."""
    import yt_dlp
    box = {"path": None}

    def hook(d):
        if cancel_cb and cancel_cb():
            raise GDriveCancelled()
        if d.get("status") == "downloading" and progress_cb:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                progress_cb(min(99.0, d.get("downloaded_bytes", 0) / total * 100))
        elif d.get("status") == "finished":
            box["path"] = d.get("filename")

    out_stem = os.path.splitext(force_filename)[0] if force_filename else base_stem
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "outtmpl": os.path.join(dest_dir, out_stem + ".%(ext)s"),
            "progress_hooks": [hook], "socket_timeout": timeout}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    path = None
    for rd in (info.get("requested_downloads") or []):
        path = rd.get("filepath") or rd.get("_filename") or path
    path = path or box["path"]
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RuntimeError("yt-dlp não produziu arquivo")
    title = info.get("title") or os.path.splitext(os.path.basename(path))[0]
    if force_filename:
        dest = os.path.join(dest_dir, force_filename)
        if os.path.abspath(path) != os.path.abspath(dest):
            os.replace(path, dest)
            path = dest
    if on_start:
        on_start(path, title, os.path.getsize(path))
    if progress_cb:
        progress_cb(100)
    return path, title


def _download_requests(url, fid, dest_dir, base_stem, force_filename,
                       on_start, progress_cb, cancel_cb, timeout):
    """Método 2 (reserva): baixa direto via requests, tratando a página de
    confirmação de antivírus dos arquivos grandes."""
    session = requests.Session()
    session.headers["User-Agent"] = _UA
    base = "https://drive.usercontent.google.com/download"
    params = {"id": fid, "export": "download"}
    # Entrada pelo endpoint clássico `uc` (o que o gdown usa).
    resp = session.get("https://drive.google.com/uc", params=params, stream=True, timeout=timeout)
    if _looks_like_html(resp):
        html = resp.text
        action, inputs = _parse_confirm_form(html)
        if action and inputs:
            resp = session.get(action, params=inputs, stream=True, timeout=timeout)
        else:
            token = None
            m = re.search(r"confirm=([0-9A-Za-z_\-]+)", html)
            if m:
                token = m.group(1)
            for k, v in session.cookies.items():
                if k.startswith("download_warning"):
                    token = v
            resp = session.get(base, params={**params, "confirm": token or "t"}, stream=True, timeout=timeout)
        if _looks_like_html(resp):
            raise RuntimeError("página de confirmação não resolvida")
    resp.raise_for_status()
    if force_filename:
        dest = os.path.join(dest_dir, force_filename)
        title = os.path.splitext(force_filename)[0]
    else:
        name = _filename_from_headers(resp, fid)
        ext = os.path.splitext(name)[1] or ".mp4"
        dest = os.path.join(dest_dir, base_stem + ext)
        title = os.path.splitext(name)[0]
    total = int(resp.headers.get("Content-Length") or 0)
    if on_start:
        on_start(dest, title, total)
    tmp = dest + ".part"
    done = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                if cancel_cb and cancel_cb():
                    raise GDriveCancelled()
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(done / total * 100)
        if progress_cb and not total:
            progress_cb(100)
        os.replace(tmp, dest)
    except BaseException:
        for p in (tmp, dest):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        raise
    return dest, title
