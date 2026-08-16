"""Fase E — correções menores, mas todas com efeito visível para o usuário."""
import importlib.util
import inspect
import os
import sys
import types

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.modules.setdefault("whisper", types.ModuleType("whisper"))
_spec = importlib.util.spec_from_file_location("wa_fase_e", os.path.join(_ROOT, "whisper-app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

import subscriptions as subs
import gdrive


# ── assinatura "só baixar" respeita a pasta ──────────────────────────────────
def test_download_only_aceita_folder():
    assert "folder" in inspect.signature(app._run_download_only).parameters


def test_ponte_de_assinatura_repassa_a_pasta(monkeypatch):
    """O parâmetro era recebido e descartado: a pasta escolhida no cadastro
    simplesmente não era aplicada."""
    recebido = {}
    monkeypatch.setattr(app, "_validate_media_url", lambda u: u)
    monkeypatch.setattr(app, "_ensure_folder_tree", lambda f: None)
    monkeypatch.setattr(app, "_kickoff_download_only",
                        lambda url, mt, q, **kw: recebido.update(kw) or {"task_id": "x"})
    app._subs_kickoff_download(url="https://youtu.be/x", folder="Aulas/2026")
    assert recebido.get("folder") == "Aulas/2026"


# ── retry preserva o formato do arquivo ──────────────────────────────────────
def test_retry_reaproveita_extensao_real(monkeypatch):
    """Sem isto o retry voltava a mp4 e gravava conteúdo num arquivo .webm."""
    capturado = {}
    monkeypatch.setattr(app, "_load_history", lambda: [])
    monkeypatch.setattr(app, "_load_media", lambda: [
        {"file": "abc_video.webm", "name": "Aula.webm", "url": "https://youtu.be/x"}])
    monkeypatch.setattr(app, "_validate_media_url", lambda u: u)
    monkeypatch.setattr(app, "_kickoff_download_only",
                        lambda url, mt, q, **kw: capturado.update(kw) or {"task_id": "t"})
    app._retry_item("abc_video.webm")
    assert capturado.get("container") == "webm"


def test_retry_extensao_desconhecida_cai_no_padrao(monkeypatch):
    capturado = {}
    monkeypatch.setattr(app, "_load_history", lambda: [])
    monkeypatch.setattr(app, "_load_media", lambda: [
        {"file": "abc_video.xyz", "name": "Aula.xyz", "url": "https://youtu.be/x"}])
    monkeypatch.setattr(app, "_validate_media_url", lambda u: u)
    monkeypatch.setattr(app, "_kickoff_download_only",
                        lambda url, mt, q, **kw: capturado.update(kw) or {"task_id": "t"})
    app._retry_item("abc_video.xyz")
    assert capturado.get("container") == "auto"


# ── gdrive limpa o parcial da tentativa que falhou ───────────────────────────
def test_gdrive_limpa_parcial_entre_metodos(tmp_path):
    (tmp_path / "abc123.mp4.part").write_bytes(b"lixo")
    (tmp_path / "abc123.mp4").write_bytes(b"parcial")
    (tmp_path / "outro.mp4").write_bytes(b"nao mexer")
    gdrive._limpar_parciais(str(tmp_path), "abc123", None)
    restantes = sorted(os.listdir(tmp_path))
    assert restantes == ["outro.mp4"], "parcial órfão ficaria no disco para sempre"


def test_gdrive_limpeza_nao_quebra_com_dir_inexistente():
    gdrive._limpar_parciais("/caminho/que/nao/existe", "abc", None)   # não levanta


# ── update de assinatura valida o label ──────────────────────────────────────
@pytest.fixture
def subs_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(subs, "SUBS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(subs, "_em_checagem", set())
    subs.configure(discover={"youtube": lambda t, l: []}, log=lambda m: None)
    return subs


def test_update_valida_label(subs_iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    with pytest.raises(ValueError):
        subs.update_subscription(s["id"], label="x" * 500)
    with pytest.raises(ValueError):
        subs.update_subscription(s["id"], label="quebra\x00linha")


def test_update_aceita_label_valido(subs_iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    r = subs.update_subscription(s["id"], label="Canal do Cliente")
    assert r["label"] == "Canal do Cliente"
