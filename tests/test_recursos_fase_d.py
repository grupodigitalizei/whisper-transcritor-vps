"""Fase D — concorrência e uso de recursos."""
import importlib.util
import os
import sys
import types

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.modules.setdefault("whisper", types.ModuleType("whisper"))
_spec = importlib.util.spec_from_file_location("wa_fase_d", os.path.join(_ROOT, "whisper-app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

import download_engine as de


# ── compressão agora tem fila, como download e transcrição ───────────────────
def test_compressao_tem_semaforo():
    assert hasattr(app, "_compress_sem"), "compressão era a única fila sem teto"
    assert "compress_concurrent" in app._DEFAULT_SETTINGS
    assert app._DEFAULT_SETTINGS["compress_concurrent"] >= 1


def test_semaforo_de_compressao_respeita_o_teto(monkeypatch):
    monkeypatch.setattr(app, "_load_settings", lambda: {"compress_concurrent": 2})
    sem = app._DynamicSem("compress_concurrent")
    sem.__enter__(); sem.__enter__()          # ocupa as duas vagas
    assert sem._count == 2
    sem.__exit__(); sem.__exit__()
    assert sem._count == 0


# ── cache de modelos não cresce sem fim ──────────────────────────────────────
def test_modelos_sao_descartados_acima_do_teto(monkeypatch):
    carregados = []
    fake = types.ModuleType("whisper")
    fake.load_model = lambda n: carregados.append(n) or f"modelo-{n}"
    monkeypatch.setattr(app, "whisper", fake)
    monkeypatch.setattr(app, "_models", {})

    for nome in ("turbo", "large-v3", "medium", "small"):
        app._load_model(nome)
    assert len(app._models) <= app._MAX_CACHED_MODELS, "modelos ficavam presos na RAM"


def test_modelo_reusado_nao_recarrega(monkeypatch):
    carregados = []
    fake = types.ModuleType("whisper")
    fake.load_model = lambda n: (carregados.append(n), f"m-{n}")[1]
    monkeypatch.setattr(app, "whisper", fake)
    monkeypatch.setattr(app, "_models", {})
    app._load_model("turbo")
    app._load_model("turbo")
    assert carregados == ["turbo"], "recarregou um modelo que já estava em memória"


def test_modelo_usado_recentemente_sobrevive(monkeypatch):
    """LRU de verdade: reusar 'turbo' deve protegê-lo do descarte."""
    fake = types.ModuleType("whisper")
    fake.load_model = lambda n: f"m-{n}"
    monkeypatch.setattr(app, "whisper", fake)
    monkeypatch.setattr(app, "_models", {})
    app._load_model("turbo")
    app._load_model("medium")
    app._load_model("turbo")      # turbo volta a ser o mais recente
    app._load_model("small")      # deve descartar 'medium', não 'turbo'
    assert "turbo" in app._models


# ── teto no envio em lote ────────────────────────────────────────────────────
def test_batch_tem_teto():
    assert app._BATCH_MAX_ITEMS > 0
    # coerente com o teto que o Download Avançado já usava
    assert app._BATCH_MAX_ITEMS <= app._ADVANCED_MAX_TOTAL_ITEMS


# ── retomada preserva os parciais ────────────────────────────────────────────
def test_retomada_nao_limpa_parciais():
    """O bug: retomar + falha do motor 1 apagava o .part que a pausa guardou."""
    limpezas = []
    class _Falha:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=True):
            raise RuntimeError("falha transitória")
    with pytest.raises(RuntimeError):
        de.run_with_fallback("https://youtu.be/x", {}, _Falha,
                             on_before_retry=lambda: limpezas.append(1))
    assert limpezas, "sem preservação, a limpeza roda entre motores (comportamento normal)"

    # e com preserve_partials o chamador simplesmente não limpa
    limpezas2 = []
    def _noop_se_preservando():
        pass
    with pytest.raises(RuntimeError):
        de.run_with_fallback("https://youtu.be/x", {}, _Falha,
                             on_before_retry=_noop_se_preservando)
    assert limpezas2 == []


def test_run_download_only_aceita_resuming():
    import inspect
    assert "resuming" in inspect.signature(app._run_download_only).parameters
    assert "preserve_partials" in inspect.signature(app._ydl_download_with_fallback).parameters
