"""Testes da cascata de motores de download (download_engine.py).

Nada aqui toca a rede ou o yt-dlp de verdade: `ydl_factory` é um fake que falha
ou funciona conforme o teste pedir, o que deixa a máquina de decisão (ordem dos
motores, backoff, aborto por cancelamento) testável de forma determinística.

Run:  ./venv/bin/python -m pytest tests/ -v
"""
import os
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import download_engine as de


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Backoff real deixaria a suíte lenta — o que importa é a ordem das tentativas."""
    monkeypatch.setattr(de.time, "sleep", lambda s: None)


class _FakeYDL:
    """Context manager no formato do yt_dlp.YoutubeDL, com falha programável."""
    def __init__(self, opts, script, seen):
        self._opts = opts
        self._script = script
        self._seen = seen

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True):
        attempt = len(self._seen)
        self._seen.append(self._opts)
        outcome = self._script[min(attempt, len(self._script) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return {"title": outcome, "attempt": attempt}


def _factory(script, seen):
    return lambda opts: _FakeYDL(opts, script, seen)


BASE = {
    "quiet": True,
    "format": "bestvideo*+bestaudio/best",
    "extractor_args": {"youtube": {"player_client": ["web", "tv_simply", "ios", "mweb"]}},
    "cookiesfrombrowser": ("chrome",),
}
YT = "https://www.youtube.com/watch?v=abc123"


# ── cadeia de motores ──────────────────────────────────────────────────────────
def test_first_engine_is_untouched_base():
    """Regra de ouro: o motor 1 tem que ser exatamente o que o chamador montou —
    é o que garante que o caminho que já funciona hoje não mude."""
    seen = []
    info, engine = de.run_with_fallback(YT, BASE, _factory(["ok"], seen))
    assert engine == "padrao"
    assert seen[0] is BASE          # mesmo objeto, sem cópia nem mutação
    assert info["title"] == "ok"


def test_youtube_chain_order():
    names = [e.name for e in de.engines_for(YT)]
    assert names == ["padrao", "android", "ios_tv", "formato_simples"]


def test_non_youtube_chain_order():
    names = [e.name for e in de.engines_for("https://vimeo.com/12345")]
    assert names == ["padrao", "sem_cookies", "formato_simples"]


def test_youtu_be_short_link_is_youtube():
    assert de._is_youtube("https://youtu.be/abc")
    assert not de._is_youtube("https://notyoutube.com/abc")
    # Fronteira de domínio: sufixo colado não conta como YouTube.
    assert not de._is_youtube("https://evilyoutube.com/abc")


# ── fallback de verdade ────────────────────────────────────────────────────────
def test_falls_through_to_second_engine():
    seen = []
    script = [RuntimeError("SABR: unable to extract"), "salvo no plano B"]
    info, engine = de.run_with_fallback(YT, BASE, _factory(script, seen))
    assert engine == "android"
    assert info["title"] == "salvo no plano B"
    # 2ª tentativa: cliente trocado e cookies retirados
    assert seen[1]["extractor_args"]["youtube"]["player_client"] == ["android"]
    assert "cookiesfrombrowser" not in seen[1]


def test_raises_last_error_when_all_engines_fail():
    seen = []
    script = [RuntimeError("falha 1"), RuntimeError("falha 2"),
              RuntimeError("falha 3"), RuntimeError("falha final")]
    with pytest.raises(RuntimeError, match="falha final"):
        de.run_with_fallback(YT, BASE, _factory(script, seen))
    assert len(seen) == 4          # tentou a cadeia inteira


def test_cancel_aborts_immediately_without_retry():
    """Cancelamento do usuário NÃO é falha de motor: tem que subir na primeira,
    senão cancelar um download viraria 'tentar de novo com outro motor'."""
    class Cancelled(Exception):
        pass
    seen = []
    with pytest.raises(Cancelled):
        de.run_with_fallback(YT, BASE, _factory([Cancelled("stop")], seen),
                             abort_types=(Cancelled,))
    assert len(seen) == 1          # não houve segunda tentativa


def test_cleanup_called_between_attempts():
    """Um .part truncado envenenaria a tentativa seguinte — o callback de limpeza
    tem que rodar antes de cada retry (e nunca antes da primeira)."""
    seen, cleanups = [], []
    script = [RuntimeError("x"), RuntimeError("y"), "ok"]
    de.run_with_fallback(YT, BASE, _factory(script, seen),
                         on_before_retry=lambda: cleanups.append(len(seen)))
    assert cleanups == [1, 2]      # após 1ª e 2ª falhas, não antes da 1ª


def test_on_engine_reports_alternatives_only():
    """A UI só deve avisar 'plano B' — o motor padrão não gera ruído."""
    seen, notes = [], []
    script = [RuntimeError("x"), "ok"]
    de.run_with_fallback(YT, BASE, _factory(script, seen),
                         on_engine=lambda eng, i, total: notes.append((eng.name, i, total)))
    assert notes[0] == ("padrao", 1, 4)     # reportado, mas o caller filtra i==1
    assert notes[1] == ("android", 2, 4)


def test_base_opts_never_mutated():
    """Os mutadores trabalham em cópia: o dict do chamador não pode ser alterado."""
    seen = []
    original = {k: (v.copy() if isinstance(v, dict) else v) for k, v in BASE.items()}
    script = [RuntimeError("x"), RuntimeError("y"), RuntimeError("z"), "ok"]
    de.run_with_fallback(YT, BASE, _factory(script, seen))
    assert BASE["cookiesfrombrowser"] == original["cookiesfrombrowser"]
    assert BASE["extractor_args"]["youtube"]["player_client"] == \
           ["web", "tv_simply", "ios", "mweb"]


# ── degradação de formato preserva a intenção áudio/vídeo ─────────────────────
def test_degraded_format_keeps_audio_intent():
    audio_base = {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    }
    out = de._degraded_format(audio_base, YT)
    assert out["format"] == "bestaudio/best"      # não virou vídeo

def test_degraded_format_for_video():
    out = de._degraded_format({"format": "bestvideo+bestaudio"}, YT)
    assert out["format"] == "best"


# ── pausa: precisa abortar como o cancelamento, sem virar retry ───────────────
def test_pause_aborts_immediately_like_cancel():
    """Pausar não é falha de motor: se a cascata tentasse o próximo motor, o
    download continuaria depois de o usuário mandar parar."""
    class Paused(Exception):
        pass
    class Cancelled(Exception):
        pass
    seen = []
    with pytest.raises(Paused):
        de.run_with_fallback(YT, BASE, _factory([Paused("pause")], seen),
                             abort_types=(Cancelled, Paused))
    assert len(seen) == 1


def test_cleanup_not_called_when_aborted():
    """A limpeza entre tentativas apagaria os .part — e são justamente eles que
    permitem retomar. Num aborto ela não pode rodar."""
    class Paused(Exception):
        pass
    seen, cleanups = [], []
    with pytest.raises(Paused):
        de.run_with_fallback(YT, BASE, _factory([Paused("x")], seen),
                             abort_types=(Paused,),
                             on_before_retry=lambda: cleanups.append(1))
    assert cleanups == []


# ── rotação de proxies ───────────────────────────────────────────────────────
def test_proxies_from_env_aceita_virgula_espaco_e_dedup(monkeypatch):
    """A lista é colada à mão no painel: vírgula, espaço e repetição acontecem."""
    monkeypatch.setenv("WHISPER_YTDLP_PROXY",
                       "http://a:8080, http://b:80  http://a:8080")
    assert de.proxies_from_env() == ["http://a:8080", "http://b:80"]


def test_sem_proxy_configurado_nao_muda_a_cascata(monkeypatch):
    """Quem não usa proxy não pode ganhar tentativa extra nenhuma."""
    monkeypatch.delenv("WHISPER_YTDLP_PROXY", raising=False)
    nomes = [e.name for e in de.engines_for(YT)]
    assert not any(n.startswith("proxy:") for n in nomes)


def test_primeiro_proxy_nao_vira_motor(monkeypatch):
    """Ele já vem aplicado em base_opts pelo chamador — repetir seria gastar uma
    tentativa refazendo exatamente o que o motor 1 já fez."""
    monkeypatch.setenv("WHISPER_YTDLP_PROXY", "http://um:8080 http://dois:8080")
    nomes = [e.name for e in de.engines_for(YT)]
    assert "proxy:um" not in nomes
    assert "proxy:dois" in nomes


def test_proxy_morto_cai_para_o_proximo(monkeypatch):
    """O caso real: proxy público morre e a cascata precisa virar sozinha."""
    monkeypatch.setenv("WHISPER_YTDLP_PROXY", "http://morto:8080 http://vivo:8080")
    usados = []

    class _YDL:
        def __init__(self, opts):
            self.opts = opts
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def extract_info(self, url, download=False):
            usados.append(self.opts.get("proxy"))
            if self.opts.get("proxy") != "http://vivo:8080":
                raise RuntimeError("HTTP Error 403: Forbidden")
            return {"ok": True}

    info, motor = de.run_with_fallback(YT, {"proxy": "http://morto:8080"}, _YDL)
    assert info == {"ok": True}
    assert motor == "proxy:vivo"
    assert usados[-1] == "http://vivo:8080"


def test_credenciais_do_proxy_nao_vazam_no_nome_do_motor(monkeypatch):
    """O nome vai para log e histórico — senha não pode aparecer ali."""
    monkeypatch.setenv("WHISPER_YTDLP_PROXY",
                       "http://p1:1 http://user:senha-secreta@proxy.exemplo:8080")
    nomes = [e.name for e in de.engines_for(YT)]
    assert "proxy:proxy.exemplo" in nomes
    assert not any("senha-secreta" in n for n in nomes)
