"""Testes das assinaturas (subscriptions.py).

O foco é a regra que evita estrago: assinar um canal com muito conteúdo NÃO
pode disparar uma avalanche de downloads. Os "descobridores" e o pipeline são
injetados por configure(), então nada aqui toca a rede, o yt-dlp ou o ego-lite.

Run:  ./venv/bin/python -m pytest tests/ -v
"""
import os
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import subscriptions as subs


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Store em tmp_path + pipeline fake. `started` acumula o que foi disparado."""
    monkeypatch.setattr(subs, "SUBS_FILE", str(tmp_path / "subscriptions.json"))
    started = []

    def _fake_kickoff(url, model=None, language=None, folder=""):
        started.append(url)

    subs.configure(
        discover={"youtube": lambda target, limit: []},
        kickoff_transcribe=_fake_kickoff,
        kickoff_download=lambda url, folder="": started.append(url),
        log=lambda msg: None,
    )
    return {"started": started, "monkeypatch": monkeypatch}


def _feed(n, start=0):
    """Simula um canal com n vídeos (o mais novo primeiro)."""
    return [{"id": f"youtube:v{i}", "url": f"https://youtu.be/v{i}", "title": f"Vídeo {i}"}
            for i in range(start, start + n)]


def _set_discover(monkeypatch, items):
    subs.configure(discover={"youtube": lambda target, limit: items[:limit]})


# ── CRUD ───────────────────────────────────────────────────────────────────
def test_add_and_list(iso):
    sub = subs.add_subscription("youtube", "https://youtube.com/@canal", label="Meu Canal")
    assert sub["platform"] == "youtube" and sub["label"] == "Meu Canal"
    assert subs.list_subscriptions()[0]["id"] == sub["id"]


def test_list_omits_seen_ids(iso):
    """seen_ids pode ter centenas de itens — não vai para a UI."""
    subs.add_subscription("youtube", "https://youtube.com/@canal")
    row = subs.list_subscriptions()[0]
    assert "seen_ids" not in row and "seen_count" in row


def test_rejects_duplicate(iso):
    subs.add_subscription("youtube", "https://youtube.com/@canal")
    with pytest.raises(ValueError, match="já assina"):
        subs.add_subscription("youtube", "https://youtube.com/@canal")


def test_rejects_bad_platform(iso):
    with pytest.raises(ValueError):
        subs.add_subscription("myspace", "quem_lembra")


def test_rejects_non_youtube_url_for_youtube(iso):
    with pytest.raises(ValueError, match="URL do canal"):
        subs.add_subscription("youtube", "https://vimeo.com/canal")


def test_social_target_normalized_to_handle(iso):
    s = subs.add_subscription("instagram", "https://www.instagram.com/perfil/")
    assert s["target"] == "perfil"
    s2 = subs.add_subscription("tiktok", "@outro")
    assert s2["target"] == "outro"


def test_interval_is_clamped(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@a", interval_hours=9999)
    assert s["interval_hours"] == subs.MAX_INTERVAL_HOURS
    s2 = subs.add_subscription("youtube", "https://youtube.com/@b", interval_hours=0)
    assert s2["interval_hours"] == subs.MIN_INTERVAL_HOURS


def test_max_per_check_hard_cap(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@a", max_per_check=10_000)
    assert s["max_per_check"] == subs.HARD_MAX_PER_CHECK


def test_update_and_remove(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.update_subscription(s["id"], paused=True, label="Novo nome")
    row = subs.list_subscriptions()[0]
    assert row["paused"] is True and row["label"] == "Novo nome"
    assert subs.remove_subscription(s["id"]) is True
    assert subs.list_subscriptions() == []


def test_update_ignores_non_editable_fields(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.update_subscription(s["id"], target="outro-canal", id="hackeado")
    row = subs.list_subscriptions()[0]
    assert row["target"] == "https://youtube.com/@canal" and row["id"] == s["id"]


# ── a regra que evita a avalanche ──────────────────────────────────────────
def test_first_check_downloads_nothing_by_default(iso):
    """O ponto mais importante: assinar um canal com 500 vídeos não pode
    enfileirar 500 downloads. O passado só é marcado como visto."""
    _set_discover(iso["monkeypatch"], _feed(500))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    res = subs.check_subscription(s["id"])
    assert res["started"] == 0
    assert iso["started"] == []
    assert subs.list_subscriptions()[0]["seen_count"] > 0   # marcou como visto


def test_initial_import_takes_only_requested_amount(iso):
    _set_discover(iso["monkeypatch"], _feed(500))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal", initial_import=3)
    res = subs.check_subscription(s["id"])
    assert res["started"] == 3
    assert len(iso["started"]) == 3


def test_second_check_downloads_only_new_items(iso):
    feed = _feed(5)
    _set_discover(iso["monkeypatch"], feed)
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.check_subscription(s["id"])          # bootstrap: marca os 5 como vistos
    assert iso["started"] == []

    novo = [{"id": "youtube:NOVO", "url": "https://youtu.be/NOVO", "title": "Novo"}]
    _set_discover(iso["monkeypatch"], novo + feed)
    res = subs.check_subscription(s["id"])
    assert res["started"] == 1
    assert iso["started"] == ["https://youtu.be/NOVO"]


def test_max_per_check_limits_burst_and_leaves_rest_for_later(iso):
    """Uma rajada de uploads respeita o teto — e o excedente NÃO é marcado como
    visto, senão sumiria para sempre."""
    _set_discover(iso["monkeypatch"], _feed(1))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal", max_per_check=2)
    subs.check_subscription(s["id"])                     # bootstrap

    _set_discover(iso["monkeypatch"], _feed(10, start=100))   # 10 novos de uma vez
    res = subs.check_subscription(s["id"])
    assert res["started"] == 2
    assert "ficaram para a próxima" in res["message"]

    res2 = subs.check_subscription(s["id"])              # os próximos 2
    assert res2["started"] == 2
    assert len(iso["started"]) == 4


def test_nothing_new_is_quiet(iso):
    _set_discover(iso["monkeypatch"], _feed(3))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.check_subscription(s["id"])
    res = subs.check_subscription(s["id"])
    assert res["started"] == 0 and res["message"] == "nada novo"


# ── robustez: uma falha não pode derrubar o poller ─────────────────────────
def test_discover_failure_is_recorded_not_raised(iso):
    def _boom(target, limit):
        raise RuntimeError("ego-lite fechado")
    subs.configure(discover={"youtube": _boom})
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    res = subs.check_subscription(s["id"])               # não levanta
    assert res["status"] == "erro"
    assert "ego-lite fechado" in subs.list_subscriptions()[0]["last_message"]


def test_one_bad_item_does_not_block_the_others(iso):
    calls = []
    def _flaky(url, model=None, language=None, folder=""):
        if "BAD" in url:
            raise RuntimeError("falhou")
        calls.append(url)
    subs.configure(discover={"youtube": lambda t, l: [
        {"id": "a", "url": "https://youtu.be/BAD"},
        {"id": "b", "url": "https://youtu.be/OK"},
    ]}, kickoff_transcribe=_flaky)
    s = subs.add_subscription("youtube", "https://youtube.com/@c", initial_import=5)
    res = subs.check_subscription(s["id"])
    assert calls == ["https://youtu.be/OK"]              # o bom passou
    assert res["started"] == 1


def test_paused_subscription_is_skipped(iso):
    _set_discover(iso["monkeypatch"], _feed(3))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.update_subscription(s["id"], paused=True)
    res = subs.check_subscription(s["id"])
    assert res["status"] == "pausada" and res["started"] == 0


def test_force_overrides_pause(iso):
    """O botão 'Checar agora' vale mesmo numa assinatura pausada."""
    _set_discover(iso["monkeypatch"], _feed(2))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal", initial_import=1)
    subs.update_subscription(s["id"], paused=True)
    res = subs.check_subscription(s["id"], force=True)
    assert res["started"] == 1


def test_missing_discoverer_is_graceful(iso):
    subs.configure(discover={})            # nenhuma plataforma disponível
    s = subs.add_subscription("tiktok", "@perfil")
    res = subs.check_subscription(s["id"])
    assert res["status"] == "erro" and "sem coletor" in res["message"]


# ── agendamento ────────────────────────────────────────────────────────────
def test_due_includes_never_checked(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    assert s["id"] in subs.due_subscriptions()


def test_due_respects_interval(iso):
    _set_discover(iso["monkeypatch"], _feed(1))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal", interval_hours=6)
    subs.check_subscription(s["id"])                     # acabou de checar
    assert s["id"] not in subs.due_subscriptions()
    # 7 horas depois já está vencida
    assert s["id"] in subs.due_subscriptions(now=__import__("time").time() + 7 * 3600)


def test_due_skips_paused(iso):
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.update_subscription(s["id"], paused=True)
    assert subs.due_subscriptions() == []


def test_seen_ids_are_capped(iso):
    """O JSON não pode crescer sem fim num canal muito ativo."""
    _set_discover(iso["monkeypatch"], _feed(subs._SEEN_CAP + 200))
    s = subs.add_subscription("youtube", "https://youtube.com/@canal")
    subs.check_subscription(s["id"])
    assert subs.list_subscriptions()[0]["seen_count"] <= subs._SEEN_CAP
