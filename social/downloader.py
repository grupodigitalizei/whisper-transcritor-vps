"""Download de mídia HD do CDN do Instagram.

Enxuto de propósito: só baixa uma URL para um caminho. A escolha de quais posts
baixar, a nomeação dos arquivos, o registro em media.json e a ponte para a
transcrição ficam no whisper-app.py (que é quem conhece UPLOAD_DIR/_save_media).

HISTÓRICO DE BUG (não remova as validações abaixo):
    A versão anterior usava `allow_redirects=False` + `raise_for_status()`. Um
    302 NÃO é erro HTTP, então `raise_for_status()` passava, o corpo do 302 era
    vazio, e o arquivo final nascia com 0 BYTES sem exceção nenhuma. O chamador
    ignorava o retorno, marcava como baixado com sucesso e enfileirava para o
    Whisper — que morria com "moov atom not found". O link do CDN do Instagram
    é assinado e expira em poucas horas; passado o prazo ele redireciona ou
    devolve corpo vazio, e era justamente esse o caminho do erro.
"""
import os
from urllib.parse import urlparse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Hosts de CDN aceitos. Redirect só é seguido se o destino também estiver aqui —
# mantém a proteção contra SSRF da versão original, sem quebrar no 302 legítimo
# que o CDN usa para servir o arquivo assinado.
CDN_SUFFIXES = (
    "cdninstagram.com", "fbcdn.net", "tiktokcdn.com", "tiktokcdn-us.com",
    "ttwstatic.com", "akamaized.net", "fna.fbcdn.net", "googlevideo.com",
)

MAX_REDIRECTS = 4
MIN_BYTES = 1024          # abaixo disso não existe vídeo nem foto de verdade


class DownloadInvalido(Exception):
    """O download terminou mas o resultado não é mídia utilizável."""


def _host_ok(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == s or host.endswith("." + s) for s in CDN_SUFFIXES)


def _magic_ok(path: str, esperado_video: bool) -> bool:
    """Confere a assinatura do arquivo. Pega o caso clássico de página de erro
    (HTML) ou JSON salvos com extensão .mp4."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    if len(head) < 12:
        return False
    if head[:3] == b"\xff\xd8\xff":                 # JPEG
        return not esperado_video
    if head[:8] == b"\x89PNG\r\n\x1a\n":             # PNG
        return not esperado_video
    if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"skip"):   # MP4/MOV
        return True
    if head[:4] == b"\x1aE\xdf\xa3":                # Matroska/WebM
        return True
    return False


def download_media(url: str, filepath: str, timeout: int = 60) -> int:
    """Baixa `url` para `filepath` (streaming). Retorna o tamanho em bytes.

    Levanta `DownloadInvalido` quando o resultado não é mídia utilizável —
    corpo vazio, HTML de erro, tamanho irrisório ou assinatura errada. Em
    qualquer erro remove o parcial e o destino, para não deixar arquivo
    quebrado no disco (que depois iria para a transcrição).
    """
    tmp = filepath + ".part"
    espera_video = filepath.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))
    try:
        atual = url
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_ok(atual):
                raise DownloadInvalido(
                    f"host fora da lista de CDNs permitidos: {urlparse(atual).hostname}")
            r = requests.get(atual, headers={"User-Agent": UA}, stream=True,
                             timeout=timeout, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                destino = r.headers.get("Location")
                r.close()
                if not destino:
                    raise DownloadInvalido("redirecionamento sem destino")
                atual = requests.compat.urljoin(atual, destino)
                continue
            break
        else:
            raise DownloadInvalido("cadeia de redirecionamentos longa demais")

        with r:
            r.raise_for_status()

            # Página de erro / JSON servido no lugar da mídia.
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype in ("text/html", "application/json", "text/plain", "application/xml"):
                raise DownloadInvalido(
                    f"o servidor devolveu {ctype} em vez de mídia "
                    "(link assinado provavelmente expirou)")

            declarado = r.headers.get("Content-Length")
            if declarado is not None and declarado.isdigit() and int(declarado) < MIN_BYTES:
                raise DownloadInvalido(
                    f"corpo de {declarado} bytes — link assinado provavelmente expirou")

            escrito = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
                        escrito += len(chunk)

        if escrito < MIN_BYTES:
            raise DownloadInvalido(
                f"baixou só {escrito} bytes — link assinado provavelmente expirou")
        if not _magic_ok(tmp, espera_video):
            raise DownloadInvalido(
                "o conteúdo não é " + ("vídeo" if espera_video else "imagem") +
                " válido (assinatura do arquivo não confere)")

        os.replace(tmp, filepath)
        return os.path.getsize(filepath)
    except Exception:
        for caminho in (tmp, filepath):
            if os.path.exists(caminho):
                try:
                    os.remove(caminho)
                except OSError:
                    pass
        raise
