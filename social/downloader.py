"""Download de mídia HD do CDN do Instagram.

Enxuto de propósito: só baixa uma URL para um caminho. A escolha de quais posts
baixar, a nomeação dos arquivos, o registro em media.json e a ponte para a
transcrição ficam no whisper-app.py (que é quem conhece UPLOAD_DIR/_save_media).
"""
import os

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def download_media(url: str, filepath: str, timeout: int = 60) -> int:
    """Baixa `url` para `filepath` (streaming). Retorna o tamanho em bytes.
    Em erro, remove o arquivo parcial e propaga a exceção."""
    tmp = filepath + ".part"
    try:
        # allow_redirects=False: os hosts são validados pelo chamador e os links do
        # CDN do IG são diretos; não seguir 302 evita SSRF via redirecionamento.
        with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=timeout,
                          allow_redirects=False) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp, filepath)
        return os.path.getsize(filepath)
    except Exception:
        for p in (tmp, filepath):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        raise
