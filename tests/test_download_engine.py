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
