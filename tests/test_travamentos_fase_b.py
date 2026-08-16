"""Fase B — o que travava o sistema para sempre.

Aqui não basta 'a função retorna certo': o que se testa é que ela NÃO fica
presa. Por isso os testes usam processos reais de curta duração e cronômetro.
"""
import os
import subprocess
import sys
import time
import types

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.modules.setdefault("whisper", types.ModuleType("whisper"))
import importlib.util
_spec = importlib.util.spec_from_file_location("wa_fase_b", os.path.join(_ROOT, "whisper-app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

from social import collector, intercept
import compressor


# ── socket_timeout: a fila não pode mais travar em socket morto ──────────────
def test_ydl_opts_tem_socket_timeout():
    opts = app._build_ydl_opts("https://youtube.com/watch?v=x", lambda d: None)
    assert opts.get("socket_timeout"), "sem socket_timeout um socket mudo trava o semáforo"
    assert 0 < opts["socket_timeout"] <= 300


def test_socket_timeout_sobrevive_ao_fallback():
    """A cascata de motores não pode perder a proteção pelo caminho."""
    import download_engine as de
    base = app._build_ydl_opts("https://www.youtube.com/watch?v=x", lambda d: None)
    for engine in de.engines_for("https://www.youtube.com/watch?v=x"):
        assert engine.mutate(base, "https://www.youtube.com/watch?v=x").get("socket_timeout")


# ── _run_ego: watchdog mata processo mudo ────────────────────────────────────
def _fake_ego(monkeypatch, mod, argv):
    """Troca 'ego-browser nodejs' por um processo Python controlado pelo teste."""
    real_popen = subprocess.Popen
    def _popen(cmd, **kw):
        return real_popen([sys.executable] + argv, **kw)
    monkeypatch.setattr(mod.subprocess, "Popen", _popen)


@pytest.mark.parametrize("mod", [collector, intercept])
def test_run_ego_nao_fica_preso_em_processo_mudo(monkeypatch, mod):
    """O caso que travava: processo vivo, stdout aberto, nada escrito.
    Antes o laço de leitura bloqueava para sempre (o timeout só valia depois)."""
    # dorme muito mais que o timeout, sem escrever nada
    _fake_ego(monkeypatch, mod, ["-c", "import time; time.sleep(120)"])
    t = time.time()
    with pytest.raises(RuntimeError, match="tempo limite"):
        if mod is collector:
            mod._run_ego("script", None, 2)
        else:
            mod._run_ego("script", None, 2)
    assert time.time() - t < 30, "o watchdog não interrompeu a leitura"


@pytest.mark.parametrize("mod", [collector, intercept])
def test_run_ego_nao_deixa_processo_orfao(monkeypatch, mod):
    """Qualquer saída (inclusive erro) tem que matar o subprocesso."""
    capturados = []
    real_popen = subprocess.Popen
    def _popen(cmd, **kw):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(120)"], **kw)
        capturados.append(p)
        return p
    monkeypatch.setattr(mod.subprocess, "Popen", _popen)
    with pytest.raises(RuntimeError):
        mod._run_ego("script", None, 1) if mod is intercept else mod._run_ego("script", None, 1)
    time.sleep(0.4)
    assert capturados and capturados[0].poll() is not None, "subprocesso ficou órfão"


@pytest.mark.parametrize("mod", [collector, intercept])
def test_run_ego_funciona_normalmente(monkeypatch, mod):
    """O caminho feliz não pode ter regredido."""
    _fake_ego(monkeypatch, mod, ["-c", "print('PROGRESS 7'); print('SAVED:/tmp/x')"])
    vistos = []
    out = mod._run_ego("script", lambda n, *a: vistos.append(n), 30)
    texto = out[0] if isinstance(out, tuple) else out
    assert "PROGRESS 7" in texto and "SAVED" in texto
    assert 7 in vistos


# ── ffmpeg: stderr junto do stdout (senão o buffer enche e trava) ────────────
def test_compressor_nao_usa_pipe_separado_para_stderr():
    import inspect
    src = inspect.getsource(compressor.compress)
    assert "stderr=subprocess.STDOUT" in src, \
        "stderr como PIPE separado não é drenado e trava o ffmpeg quando enche"
    assert "stderr=subprocess.PIPE" not in src


@pytest.mark.skipif(not compressor.capabilities()["available"], reason="sem ffmpeg")
def test_compressor_reporta_erro_mesmo_com_stderr_unificado(tmp_path):
    """Unificar os streams não pode ter cegado a mensagem de erro."""
    ruim = tmp_path / "nao_e_video.mp4"
    ruim.write_bytes(b"isto nao e um video" * 100)
    with pytest.raises(compressor.CompressError):
        compressor.compress(str(ruim), "medio", replace=False)
