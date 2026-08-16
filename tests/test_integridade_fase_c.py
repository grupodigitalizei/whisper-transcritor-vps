"""Fase C — integridade de dados sob concorrência.

Cada teste reproduz um cenário em que o app perdia ou corrompia dados quando
duas coisas aconteciam ao mesmo tempo.
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
_spec = importlib.util.spec_from_file_location("wa_fase_c", os.path.join(_ROOT, "whisper-app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

import subscriptions as subs


@pytest.fixture
def tasks_limpas(monkeypatch):
    monkeypatch.setattr(app, "_tasks", {})
    return app


# ── trava "arquivo em uso" ───────────────────────────────────────────────────
@pytest.mark.parametrize("status", ["queued", "processing", "paused"])
def test_arquivo_em_uso_e_detectado(tasks_limpas, status):
    app._set_task("t1", status=status, filename="abc_video.mp4")
    assert app._active_task_for("abc_video.mp4") == "t1"


@pytest.mark.parametrize("status", ["done", "error", "cancelled"])
def test_tarefa_terminada_nao_bloqueia(tasks_limpas, status):
    app._set_task("t1", status=status, filename="abc_video.mp4")
    assert app._active_task_for("abc_video.mp4") is None


def test_outro_arquivo_nao_bloqueia(tasks_limpas):
    app._set_task("t1", status="processing", filename="outro.mp4")
    assert app._active_task_for("abc_video.mp4") is None


def test_sem_filename_nao_quebra(tasks_limpas):
    assert app._active_task_for("") is None
    assert app._active_task_for(None) is None


# ── faxina não lista arquivo em uso ──────────────────────────────────────────
def test_faxina_ignora_arquivo_em_transcricao(tmp_path, monkeypatch):
    """O cenário real: retry preserva queued_at, então um arquivo antigo
    re-enfileirado HOJE continuava parecendo velho — e era apagado no meio da
    transcrição."""
    uploads = tmp_path / "uploads"; uploads.mkdir()
    f = uploads / "abc_aula.mp4"; f.write_bytes(b"x" * 5000)
    monkeypatch.setattr(app, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(app, "_tasks", {})
    # timestamp antigo mas TRUTHY: `entry.get("queued_at") or mtime` cairia
    # no mtime do arquivo (criado agora) se usássemos 0.
    velho = 1_000_000  # jan/1970
    monkeypatch.setattr(app, "_load_media",
                        lambda: [{"file": "abc_aula.mp4", "queued_at": velho,
                                  "status": "processing"}])
    assert app._audit_old_media(7.0)["count"] == 0

    # o mesmo arquivo, agora ocioso, DEVE aparecer
    monkeypatch.setattr(app, "_load_media",
                        lambda: [{"file": "abc_aula.mp4", "queued_at": velho,
                                  "status": "done"}])
    assert app._audit_old_media(7.0)["count"] == 1


def test_faxina_ignora_por_task_ativa_mesmo_com_status_done(tmp_path, monkeypatch):
    """Defesa dupla: mesmo se o catálogo disser 'done', uma task viva protege."""
    uploads = tmp_path / "uploads"; uploads.mkdir()
    (uploads / "abc_aula.mp4").write_bytes(b"x" * 5000)
    monkeypatch.setattr(app, "UPLOAD_DIR", str(uploads))
    monkeypatch.setattr(app, "_tasks", {})
    monkeypatch.setattr(app, "_load_media",
                        lambda: [{"file": "abc_aula.mp4", "queued_at": 0, "status": "done"}])
    app._set_task("t9", status="processing", filename="abc_aula.mp4")
    assert app._audit_old_media(7.0)["count"] == 0


# ── assinaturas: item que falha ao iniciar NÃO vira "visto" ──────────────────
@pytest.fixture
def subs_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(subs, "SUBS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(subs, "_em_checagem", set())
    return subs


def test_item_que_falha_ao_iniciar_e_tentado_de_novo(subs_iso):
    feed = [{"id": f"youtube:v{i}", "url": f"https://youtu.be/v{i}"} for i in range(3)]
    tentativas = []

    def _falha_sempre(url, model=None, language=None, folder=""):
        tentativas.append(url)
        raise RuntimeError("rede caiu")

    subs.configure(discover={"youtube": lambda t, l: feed},
                   kickoff_transcribe=_falha_sempre, log=lambda m: None)
    s = subs.add_subscription("youtube", "https://youtube.com/@c", initial_import=3)
    subs.check_subscription(s["id"])
    n_primeira = len(tentativas)
    assert n_primeira == 3, "deveria ter tentado os 3"

    # segunda checagem: como falharam, precisam ser tentados DE NOVO
    subs.check_subscription(s["id"])
    assert len(tentativas) > n_primeira, "itens falhos foram perdidos para sempre"


def test_item_que_inicia_vira_visto(subs_iso):
    feed = [{"id": "youtube:ok", "url": "https://youtu.be/ok"}]
    chamadas = []
    subs.configure(discover={"youtube": lambda t, l: feed},
                   kickoff_transcribe=lambda url, **k: chamadas.append(url),
                   log=lambda m: None)
    s = subs.add_subscription("youtube", "https://youtube.com/@c", initial_import=1)
    subs.check_subscription(s["id"])
    subs.check_subscription(s["id"])
    assert len(chamadas) == 1, "item bem-sucedido foi baixado duas vezes"


# ── trava contra checagem concorrente ────────────────────────────────────────
def test_checagem_concorrente_e_recusada(subs_iso):
    """Poller e 'Checar agora' no mesmo perfil baixavam tudo em dobro."""
    subs.configure(discover={"youtube": lambda t, l: []}, log=lambda m: None)
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    subs._em_checagem.add(s["id"])          # simula checagem em curso
    res = subs.check_subscription(s["id"])
    assert res["status"] == "ocupada" and res["started"] == 0


def test_trava_e_liberada_mesmo_com_erro(subs_iso):
    def _boom(target, limit):
        raise RuntimeError("falhou")
    subs.configure(discover={"youtube": _boom}, log=lambda m: None)
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    subs.check_subscription(s["id"])
    assert s["id"] not in subs._em_checagem, "a trava vazou e travaria a assinatura para sempre"
