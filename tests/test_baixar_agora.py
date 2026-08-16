"""Botão 'Baixar vídeos agora' — traz o acervo do canal sob demanda."""
import os, sys
import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import subscriptions as subs


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(subs, "SUBS_FILE", str(tmp_path / "s.json"))
    monkeypatch.setattr(subs, "_em_checagem", set())
    baixados = []
    subs.configure(
        discover={"youtube": lambda t, n: [
            {"id": f"youtube:v{i}", "url": f"https://youtu.be/v{i}"} for i in range(n)]},
        kickoff_transcribe=lambda url, **k: baixados.append(url),
        kickoff_download=lambda url, **k: baixados.append(url),
        log=lambda m: None)
    return baixados


def test_baixa_a_quantidade_pedida(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    r = subs.download_latest(s["id"], 3)
    assert r["started"] == 3 and len(iso) == 3


def test_funciona_mesmo_sem_novidade(iso):
    """O ponto do botão: trazer o que JÁ existe, mesmo tudo já 'visto'."""
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    subs.check_subscription(s["id"])          # marca o passado como visto
    iso.clear()
    r = subs.download_latest(s["id"], 2)
    assert r["started"] == 2, "deveria baixar mesmo já tendo visto os itens"


def test_respeita_o_teto_duro(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    r = subs.download_latest(s["id"], 9999)
    assert r["started"] <= subs.HARD_MAX_PER_CHECK


def test_nao_roda_junto_com_outra_checagem(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    subs._em_checagem.add(s["id"])
    r = subs.download_latest(s["id"], 3)
    assert r["status"] == "ocupada" and r["started"] == 0


def test_libera_a_trava_no_fim(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    subs.download_latest(s["id"], 1)
    assert s["id"] not in subs._em_checagem


def test_erro_do_coletor_nao_levanta(iso, monkeypatch):
    def _boom(t, n): raise RuntimeError("ego fechado")
    subs.configure(discover={"youtube": _boom})
    s = subs.add_subscription("youtube", "https://youtube.com/@c")
    r = subs.download_latest(s["id"], 3)
    assert r["status"] == "erro" and r["started"] == 0


def test_assinatura_inexistente(iso):
    with pytest.raises(KeyError):
        subs.download_latest("nao-existe", 3)
