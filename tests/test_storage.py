"""Tela de Armazenamento: inventário e limpeza."""
import os, sys
import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import storage


@pytest.fixture
def disco(tmp_path, monkeypatch):
    """Estrutura de pastas isolada, com as mesmas categorias do app."""
    d = tmp_path / ".whisper_data"
    up, res = d / "uploads", d / "results"
    soc = d / "social"
    data, cache, exp, med = soc/"data", soc/"cache"/"thumbs", soc/"exports", soc/"media"
    for p in (up, res, data, cache, exp, med):
        p.mkdir(parents=True)
    (up / "abc_video.mp4").write_bytes(b"x" * 5000)
    (up / "def_audio.mp3").write_bytes(b"y" * 1000)
    (res / "abc_video").mkdir(); (res / "abc_video" / "t.txt").write_bytes(b"z" * 300)
    (data / "perfil_2026-08-11_1430.json").write_bytes(b"{}" * 100)
    (cache / "thumb1.jpg").write_bytes(b"j" * 700)
    (exp / "planilha.xlsx").write_bytes(b"e" * 400)
    (d / "history.json").write_text("[]")
    (d / "history 2.json").write_text("[]")          # cópia do iCloud
    (d / "auth.json.bak-2026").write_text("{}")      # backup antigo

    monkeypatch.setattr(storage, "DATA_DIR", str(d))
    monkeypatch.setattr(storage, "UPLOAD_DIR", str(up))
    monkeypatch.setattr(storage, "RESULTS_DIR", str(res))
    monkeypatch.setattr(storage, "SOCIAL_DATA_DIR", str(data))
    monkeypatch.setattr(storage, "SOCIAL_CACHE_DIR", str(cache))
    monkeypatch.setattr(storage, "SOCIAL_EXPORT_DIR", str(exp))
    monkeypatch.setattr(storage, "SOCIAL_MEDIA_DIR", str(med))
    cats = {k: dict(v) for k, v in storage.CATEGORIAS.items()}
    cats["uploads"]["pasta"] = str(up);          cats["results"]["pasta"] = str(res)
    cats["social_data"]["pasta"] = str(data);    cats["social_cache"]["pasta"] = str(cache)
    cats["social_exports"]["pasta"] = str(exp);  cats["social_media"]["pasta"] = str(med)
    monkeypatch.setattr(storage, "CATEGORIAS", cats)
    storage.configure(em_uso=lambda f: None)
    return {"up": up, "res": res, "data": data, "cache": cache, "d": d}


def test_overview_soma_tudo(disco):
    o = storage.overview()
    assert o["total_bytes"] > 0
    ids = {c["id"] for c in o["categorias"]}
    assert "uploads" in ids and "social_data" in ids
    # ordenado do maior para o menor
    bytes_ = [c["bytes"] for c in o["categorias"]]
    assert bytes_ == sorted(bytes_, reverse=True)


def test_lista_itens_ordenada(disco):
    r = storage.listar_itens("uploads")
    assert [i["nome"] for i in r["itens"]] == ["abc_video.mp4", "def_audio.mp3"]


def test_coleta_ganha_nome_legivel(disco):
    r = storage.listar_itens("social_data")
    item = r["itens"][0]
    assert item["nome"] == "@perfil"
    assert "2026-08-11" in item["detalhe"]


def test_apagar_libera_espaco(disco):
    r = storage.apagar("uploads", ["def_audio.mp3"])
    assert r["apagados"] == 1 and r["liberados_bytes"] == 1000
    assert not (disco["up"] / "def_audio.mp3").exists()
    assert (disco["up"] / "abc_video.mp4").exists()   # não levou o vizinho


def test_apagar_pasta_de_resultado(disco):
    r = storage.apagar("results", ["abc_video"])
    assert r["apagados"] == 1
    assert not (disco["res"] / "abc_video").exists()


def test_nunca_apaga_arquivo_em_uso(disco):
    storage.configure(em_uso=lambda f: "task-123" if f == "abc_video.mp4" else None)
    r = storage.apagar("uploads", ["abc_video.mp4"])
    assert r["apagados"] == 0 and r["em_uso"] == ["abc_video.mp4"]
    assert (disco["up"] / "abc_video.mp4").exists()


@pytest.mark.parametrize("malicioso", ["../history.json", "/etc/passwd", "..", "."])
def test_nao_escapa_da_pasta(disco, malicioso):
    storage.apagar("uploads", [malicioso])
    assert (disco["d"] / "history.json").exists()      # nada fora foi tocado


def test_limpar_so_vale_para_regeneravel(disco):
    r = storage.limpar_categoria("social_cache")
    assert r["apagados"] == 1
    assert storage.listar_itens("social_cache")["total"] == 0


def test_sobras_detecta_copia_do_icloud(disco):
    nomes = {s["nome"]: s["motivo"] for s in storage.sobras()}
    assert "history 2.json" in nomes and "iCloud" in nomes["history 2.json"]
    assert "auth.json.bak-2026" in nomes
    assert "history.json" not in nomes                 # o legítimo fica fora


def test_apagar_sobras_nao_toca_no_legitimo(disco):
    r = storage.apagar_sobras(["history.json", "history 2.json"])
    assert r["apagados"] == 1
    assert (disco["d"] / "history.json").exists()
    assert not (disco["d"] / "history 2.json").exists()


def test_categoria_invalida(disco):
    with pytest.raises(KeyError):
        storage.listar_itens("inventada")


# ── prévia antes de apagar ─────────────────────────────────────────────────
def test_preview_transcricao_mostra_trecho(disco):
    (disco["res"] / "abc_video" / "abc_video.txt").write_text(
        "Este é o conteúdo falado no vídeo, para conferir antes de apagar.",
        encoding="utf-8")
    p = storage.preview("results", "abc_video")
    assert p["tipo"] == "texto"
    assert "conteúdo falado" in p["texto"]


def test_preview_coleta_le_perfil_e_posts(disco):
    import json
    (disco["data"] / "perfil_2026-08-11_1430.json").write_text(json.dumps({
        "profile": {"username": "canal", "platform": "Instagram"},
        "collected_at": "2026-08-11T14:30:00",
        "rows": [{"caption": "Primeiro post", "likes": 10},
                 {"caption": "Segundo post", "likes": 20}],
    }), encoding="utf-8")
    p = storage.preview("social_data", "perfil_2026-08-11_1430.json")
    assert p["tipo"] == "lista"
    assert p["itens"][0]["titulo"] == "Primeiro post"
    rotulos = {d["rotulo"]: d["valor"] for d in p["detalhes"]}
    assert rotulos["Perfil"] == "@canal" and rotulos["Posts"] == "2"


def test_preview_aceita_caption_como_objeto(disco):
    """A API do Instagram devolve caption como {"text": ...}, não string —
    o formato cru quebrava a prévia."""
    import json
    (disco["data"] / "perfil_2026-08-11_1430.json").write_text(json.dumps({
        "profile": {"username": "canal"},
        "items": [{"caption": {"text": "Legenda em objeto"}}],
    }), encoding="utf-8")
    p = storage.preview("social_data", "perfil_2026-08-11_1430.json")
    assert p["itens"][0]["titulo"] == "Legenda em objeto"


def test_preview_video_aponta_para_a_midia(disco):
    p = storage.preview("uploads", "abc_video.mp4")
    assert p["tipo"] == "video" and p["url"].endswith("abc_video.mp4")


@pytest.mark.parametrize("malicioso", ["../history.json", "/etc/passwd", ".."])
def test_preview_nao_escapa_da_pasta(disco, malicioso):
    with pytest.raises((FileNotFoundError, KeyError)):
        storage.preview("uploads", malicioso)


def test_caminho_de_valida_pasta(disco):
    assert storage.caminho_de("uploads", "abc_video.mp4").endswith("abc_video.mp4")
    with pytest.raises(FileNotFoundError):
        storage.caminho_de("uploads", "../history.json")


# ── a página não pode ficar em cache ───────────────────────────────────────
def test_html_servido_com_no_cache():
    """O CSS e o JS têm cache-busting por ?v=, mas o HTML não tinha nenhum
    header: o navegador servia a página antiga com o JS novo, e qualquer
    elemento adicionado depois (um modal) simplesmente não existia no DOM."""
    import asyncio, importlib.util, sys, types, os
    sys.modules.setdefault("whisper", types.ModuleType("whisper"))
    raiz = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    spec = importlib.util.spec_from_file_location("wa_cache", os.path.join(raiz, "whisper-app.py"))
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    resp = asyncio.run(app.serve_html())
    assert "no-cache" in (resp.headers.get("cache-control") or "")


# ── prévia de mídia: tocar, não baixar ─────────────────────────────────────
def test_preview_video_usa_rota_de_stream(disco):
    """A rota de download manda Content-Disposition: attachment, e um <video>
    apontado para lá não reproduz — a prévia precisa da rota inline."""
    p = storage.preview("uploads", "abc_video.mp4")
    assert "/stream/" in p["url"], "prévia apontando para rota de download"
    assert "download-media" not in p["url"]
    # o botão de baixar continua existindo, separado
    assert p["url_download"].startswith("/api/download-media/")


def test_preview_escapa_nome_na_url(disco):
    (disco["up"] / "com espaço & sinal.mp4").write_bytes(b"x" * 10)
    p = storage.preview("uploads", "com espaço & sinal.mp4")
    assert " " not in p["url"] and "%20" in p["url"]


def test_stream_serve_mime_correto():
    """Sem media_type o navegador não sabe que é vídeo."""
    import importlib.util, sys, types, os
    sys.modules.setdefault("whisper", types.ModuleType("whisper"))
    raiz = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    spec = importlib.util.spec_from_file_location("wa_mime", os.path.join(raiz, "whisper-app.py"))
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    assert app._MIME_POR_EXT[".mp4"] == "video/mp4"
    assert app._MIME_POR_EXT[".mp3"] == "audio/mpeg"
    assert app._MIME_POR_EXT[".mov"].startswith("video/")


def test_mime_do_stream_cobre_formatos_de_gravacao():
    """O .mov é o caso que travava: precisa de Content-Type correto para o
    navegador conseguir dizer que NÃO sabe tocar, em vez de tentar."""
    import importlib.util, sys, types, os
    sys.modules.setdefault("whisper", types.ModuleType("whisper"))
    raiz = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    spec = importlib.util.spec_from_file_location("wa_mime2", os.path.join(raiz, "whisper-app.py"))
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    assert app._MIME_POR_EXT[".mov"] == "video/quicktime"
    for ext in (".mp4", ".webm", ".mkv", ".mp3", ".wav"):
        assert ext in app._MIME_POR_EXT, f"{ext} sem MIME — vira octet-stream"
