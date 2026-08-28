"""Download de imagem no Download Avançado.

O yt-dlp não serve para imagem solta, então este caminho é próprio — e por
isso precisa repetir, por conta, as defesas que o resto do app já tem.
"""
import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.modules.setdefault("whisper", types.ModuleType("whisper"))
_spec = importlib.util.spec_from_file_location("wa_img_test", os.path.join(_ROOT, "whisper-app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)


# ── a extensão vem do CONTEÚDO, não do que o servidor promete ──────────────
@pytest.mark.parametrize("head,esperado", [
    (b"\xff\xd8\xff\xe0" + b"\x00" * 28,                    ".jpg"),
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 24,                   ".png"),
    (b"GIF89a" + b"\x00" * 26,                              ".gif"),
    (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 20,        ".webp"),
    (b"\x00" * 4 + b"ftypavif" + b"\x00" * 20,              ".avif"),
    (b"\x00" * 4 + b"ftypheic" + b"\x00" * 20,              ".heic"),
    (b"BM" + b"\x00" * 30,                                  ".bmp"),
])
def test_extensao_vem_dos_magic_bytes(head, esperado):
    assert app._extensao_de_imagem(head, "", "http://x/arquivo") == esperado


def test_magic_bytes_ganham_do_content_type_mentiroso():
    """Um servidor pode prometer image/png e mandar JPEG. O arquivo manda."""
    head = b"\xff\xd8\xff\xe0" + b"\x00" * 28
    assert app._extensao_de_imagem(head, "image/png", "http://x/a.png") == ".jpg"


def test_html_nao_vira_imagem():
    """O erro mais comum: colar o link da PÁGINA em vez do arquivo. Sem esta
    checagem, a página de erro seria salva como .jpg e a Biblioteca mostraria
    um item quebrado."""
    head = b"<!DOCTYPE html><html><head>....."
    assert app._extensao_de_imagem(head, "text/html", "http://x/pagina") is None


def test_cai_para_content_type_quando_nao_ha_assinatura():
    assert app._extensao_de_imagem(b"\x00" * 32, "image/webp", "http://x/a") == ".webp"


def test_cai_para_extensao_da_url_em_ultimo_caso():
    assert app._extensao_de_imagem(b"\x00" * 32, "", "http://x/foto.jpeg") == ".jpeg"


def test_extensao_desconhecida_na_url_nao_passa():
    assert app._extensao_de_imagem(b"\x00" * 32, "", "http://x/arquivo.exe") is None


# ── SSRF: o mesmo guard das outras rotas ───────────────────────────────────
@pytest.mark.parametrize("interno", [
    "http://127.0.0.1:7860/login",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.0.1/foto.jpg",
    "http://localhost/x.png",
    "file:///etc/passwd",
])
def test_endereco_interno_e_bloqueado(interno, tmp_path):
    with pytest.raises(Exception):
        app._baixar_imagem(interno, str(tmp_path / "x"))


# ── classificação e allowlist ──────────────────────────────────────────────
@pytest.mark.parametrize("nome,tipo", [
    ("a.jpg", "image"), ("b.PNG", "image"), ("c.webp", "image"),
    ("d.mp4", "video"), ("e.mp3", "audio"), ("f.txt", "other"),
])
def test_biblioteca_classifica_imagem(nome, tipo):
    assert app._media_type_for(nome) == tipo


def test_tipos_aceitos_no_download():
    for t in ("video", "audio", "image"):
        assert app._validate_media_type(t) == t
    with pytest.raises(app.HTTPException):
        app._validate_media_type("documento")


def test_imagem_nao_e_tratada_como_video_no_retry(monkeypatch):
    """Sem o ramo de imagem, um .jpg seria reenviado como vídeo e o yt-dlp
    falharia com erro incompreensível."""
    capturado = {}
    monkeypatch.setattr(app, "_load_history", lambda: [])
    monkeypatch.setattr(app, "_load_media", lambda: [
        {"file": "abc_foto.jpg", "name": "Foto.jpg", "url": "https://ex.com/f.jpg"}])
    monkeypatch.setattr(app, "_validate_media_url", lambda u: u)
    monkeypatch.setattr(app, "_active_task_for", lambda f: None)
    monkeypatch.setattr(app, "_kickoff_download_only",
                        lambda url, mt, q, **kw: capturado.update(tipo=mt) or {"task_id": "t"})
    app._retry_item("abc_foto.jpg")
    assert capturado["tipo"] == "image"


# ── modo automático: descobrir o tipo em vez de perguntar ──────────────────
@pytest.mark.parametrize("url,esperado", [
    # host conhecido: decide sem tocar na rede
    ("https://www.youtube.com/watch?v=abc", "video"),
    ("https://youtu.be/abc",                "video"),
    ("https://vimeo.com/123",               "video"),
    ("https://www.instagram.com/reel/X/",   "video"),
    ("https://www.tiktok.com/@a/video/1",   "video"),
    # extensão no caminho
    ("https://site.com/foto.jpg",           "image"),
    ("https://site.com/arte.PNG",           "image"),
    ("https://site.com/podcast.mp3",        "audio"),
    ("https://site.com/aula.mp4",           "video"),
])
def test_detecta_sem_rede(url, esperado, monkeypatch):
    """Host conhecido e extensão resolvem localmente — nenhuma requisição."""
    def _proibido(*a, **k):
        raise AssertionError("não deveria consultar a rede neste caso")
    monkeypatch.setattr(app.requests, "head", _proibido)
    monkeypatch.setattr(app.requests, "get", _proibido)
    assert app._detectar_tipo_de_midia(url) == esperado


def test_detecta_pelo_content_type(monkeypatch):
    """URL sem extensão: quem decide é o servidor."""
    class _R:
        status_code = 200
        headers = {"Content-Type": "image/webp"}
    monkeypatch.setattr(app.requests, "head", lambda *a, **k: _R())
    assert app._detectar_tipo_de_midia("https://cdn.site.com/i/98a7f") == "image"


def test_pagina_html_fica_com_o_ytdlp(monkeypatch):
    class _R:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
    monkeypatch.setattr(app.requests, "head", lambda *a, **k: _R())
    assert app._detectar_tipo_de_midia("https://portal.com/materia") == "video"


def test_rede_indisponivel_nao_derruba_o_download(monkeypatch):
    """Falha de detecção não pode impedir o download: cai no yt-dlp."""
    def _erro(*a, **k):
        raise app.requests.RequestException("sem rede")
    monkeypatch.setattr(app.requests, "head", _erro)
    monkeypatch.setattr(app.requests, "get", _erro)
    assert app._detectar_tipo_de_midia("https://site.com/x") == "video"


def test_auto_e_um_tipo_valido():
    assert app._validate_media_type("auto") == "auto"
