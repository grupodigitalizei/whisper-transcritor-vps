"""Testes da compressão (compressor.py).

Os testes de ponta a ponta usam FFmpeg de verdade — com vídeos minúsculos
gerados na hora, então rodam em segundos. Se a máquina não tiver FFmpeg, esses
testes são pulados (o resto do app não depende dele).

Run:  ./venv/bin/python -m pytest tests/ -v
"""
import os
import subprocess
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import compressor as c

_HAS_FFMPEG = c.capabilities()["available"]
needs_ffmpeg = pytest.mark.skipif(not _HAS_FFMPEG, reason="FFmpeg não instalado")


def _make_video(path, *, seconds=2, size="640x480", qp=0, fps=15):
    """Gera um vídeo de ruído (qp=0 → praticamente sem compressão, arquivo gordo)."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s={size}:r={fps}:d={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-vf", "geq=random(1)*255:128:128",
         "-c:v", "libx264", "-preset", "ultrafast", "-qp", str(qp),
         "-c:a", "aac", str(path)],
        check=True, capture_output=True, timeout=120)
    return str(path)


def _make_small_video(path, seconds=2):
    """Vídeo já bem comprimido — serve para testar a recusa."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=blue:s=320x240:r=10:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "80k",
         "-an", str(path)],
        check=True, capture_output=True, timeout=120)
    return str(path)


# ── unidades puras (não precisam de FFmpeg) ────────────────────────────────
def test_media_kind():
    assert c.media_kind("a.mp4") == "video"
    assert c.media_kind("a.MOV") == "video"
    assert c.media_kind("a.mp3") == "audio"
    assert c.media_kind("a.txt") == "other"


def test_target_bitrate_scales_with_height():
    """Menos resolução, menos bitrate — e sempre menor no preset mais forte."""
    assert c._target_bitrate(1080, "forte") < c._target_bitrate(1080, "medio")
    assert c._target_bitrate(1080, "medio") < c._target_bitrate(1080, "leve")
    assert c._target_bitrate(480, "medio") < c._target_bitrate(1080, "medio")


def test_target_bitrate_handles_odd_heights():
    """Uma altura fora da tabela (ex.: 900p) cai no degrau adequado."""
    assert c._target_bitrate(900, "medio") == c._VIDEO_BITRATE["medio"][720]
    assert c._target_bitrate(50, "medio") == c._VIDEO_BITRATE["medio"][360]


def test_presets_are_consistent():
    assert set(c.PRESETS) == set(c.PRESET_LABEL)
    for p in c.PRESETS:
        assert p in c._VIDEO_BITRATE and p in c._AUDIO_BITRATE and p in c._MAX_HEIGHT


def test_probe_rejects_missing_file():
    with pytest.raises(c.CompressError):
        c.probe("/caminho/que/nao/existe.mp4")


def test_plan_rejects_bad_preset(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"nao e video")
    with pytest.raises(c.CompressError):
        c.plan(str(f), "ultra-mega")


def test_plan_rejects_unsupported_extension(tmp_path):
    f = tmp_path / "doc.txt"
    f.write_text("oi")
    with pytest.raises(c.CompressError):
        c.plan(str(f), "medio")


# ── ponta a ponta com FFmpeg ───────────────────────────────────────────────
@needs_ffmpeg
def test_probe_reads_real_video(tmp_path):
    v = _make_video(tmp_path / "v.mp4")
    info = c.probe(v)
    assert info["has_video"] and info["width"] == 640 and info["height"] == 480
    assert info["duration_secs"] > 1 and info["size_bytes"] > 0


@needs_ffmpeg
def test_plan_estimates_saving_on_fat_file(tmp_path):
    v = _make_video(tmp_path / "v.mp4")
    p = c.plan(v, "medio")
    assert p["worth_it"] is True
    assert p["estimated_bytes"] < p["size_bytes"]
    assert p["estimated_saving_pct"] > 0


@needs_ffmpeg
def test_compress_actually_shrinks_and_replaces(tmp_path):
    v = _make_video(tmp_path / "v.mp4")
    before = os.path.getsize(v)
    seen = []
    r = c.compress(v, "medio", replace=True, on_progress=seen.append)
    assert r["ok"] and not r["skipped"]
    assert r["new_bytes"] < before
    assert r["saved_pct"] > 0
    assert os.path.exists(r["path"]) and os.path.getsize(r["path"]) == r["new_bytes"]
    assert seen, "o progresso deve ser reportado"


@needs_ffmpeg
def test_compress_keep_original_when_not_replacing(tmp_path):
    v = _make_video(tmp_path / "v.mp4")
    before = os.path.getsize(v)
    r = c.compress(v, "medio", replace=False)
    assert r["ok"]
    assert os.path.exists(v) and os.path.getsize(v) == before   # original intacto
    assert "comprimido" in os.path.basename(r["path"])


@needs_ffmpeg
def test_refuses_when_already_compact(tmp_path):
    """Recodificar um arquivo já enxuto piora a imagem sem ganho — tem que recusar."""
    v = _make_small_video(tmp_path / "small.mp4")
    before = os.path.getsize(v)
    r = c.compress(v, "medio", replace=True)
    assert r["ok"] is False and r["skipped"] is True
    assert r["reason"]
    assert os.path.getsize(v) == before          # não encostou no arquivo


@needs_ffmpeg
def test_stronger_preset_produces_smaller_file(tmp_path):
    a = _make_video(tmp_path / "a.mp4", size="1280x720")
    b = _make_video(tmp_path / "b.mp4", size="1280x720")
    ra = c.compress(a, "leve", replace=True)
    rb = c.compress(b, "forte", replace=True)
    assert rb["new_bytes"] < ra["new_bytes"]


@needs_ffmpeg
def test_cancel_stops_and_leaves_original_untouched(tmp_path):
    v = _make_video(tmp_path / "v.mp4", seconds=6, size="1280x720")
    before = os.path.getsize(v)
    with pytest.raises(c.CompressCancelled):
        c.compress(v, "medio", replace=True, is_cancelled=lambda: True)
    assert os.path.exists(v) and os.path.getsize(v) == before


@needs_ffmpeg
def test_no_temp_files_left_behind(tmp_path):
    """Nem no sucesso nem no cancelamento pode sobrar .compress_* no diretório."""
    v = _make_video(tmp_path / "v.mp4")
    c.compress(v, "medio", replace=True)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".compress_")]
    assert leftovers == []

    v2 = _make_video(tmp_path / "v2.mp4", seconds=6, size="1280x720")
    with pytest.raises(c.CompressCancelled):
        c.compress(v2, "medio", is_cancelled=lambda: True)
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".compress_")]
    assert leftovers == []


@needs_ffmpeg
def test_audio_only_compression(tmp_path):
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                    "-c:a", "pcm_s16le", str(wav)], check=True, capture_output=True)
    r = c.compress(str(wav), "medio", replace=False)
    assert r["ok"] and r["new_bytes"] < r["old_bytes"]
    assert r["path"].endswith(".m4a")
