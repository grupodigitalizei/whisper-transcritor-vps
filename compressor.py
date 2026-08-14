#!/usr/bin/env python3
"""Compressão de vídeo/áudio com FFmpeg — para o acervo não comer o disco.

Por que existe
──────────────
Vídeo baixado em qualidade máxima ocupa muito, e o app já tinha uma faxina por
idade (apagar mídia com mais de 7 dias) justamente por causa disso. Comprimir é
a alternativa que preserva o material: em vez de apagar um vídeo de 2 GB, ele
vira 300 MB e continua assistível.

Rápido de verdade no Apple Silicon
──────────────────────────────────
Quando o FFmpeg tem os encoders `*_videotoolbox`, a codificação roda no chip de
mídia do Mac em vez da CPU — ordens de magnitude mais rápido e sem fritar a
máquina enquanto o Whisper transcreve. Sem eles, caímos em libx264/libx265 por
software, mais lento porém equivalente no resultado.

Regras de segurança
───────────────────
- O original NUNCA é sobrescrito no meio do caminho: comprimimos para um
  arquivo temporário e só no fim, se tudo deu certo e o resultado ficou menor,
  a troca acontece (os.replace, atômico).
- Se o vídeo já está enxuto, não comprimimos: recodificar um arquivo já
  comprimido só piora a qualidade — e às vezes aumenta o tamanho.
- Nada de shell: sempre lista de argumentos.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile

# ── presets ────────────────────────────────────────────────────────────────
# Bitrate-alvo (kbps) por altura de vídeo. Bitrate (e não CRF) porque o
# videotoolbox não suporta CRF e porque o objetivo aqui é tamanho previsível.
_VIDEO_BITRATE = {
    "leve":  {1080: 4000, 720: 2500, 480: 1200, 360: 800},
    "medio": {1080: 2500, 720: 1500, 480: 800,  360: 500},
    "forte": {1080: 1200, 720: 800,  480: 500,  360: 350},
}
_AUDIO_BITRATE = {"leve": "160k", "medio": "128k", "forte": "96k"}

# Altura máxima por preset: reduzir resolução é o maior ganho real de tamanho.
# 'leve' preserva a resolução original.
_MAX_HEIGHT = {"leve": None, "medio": 1080, "forte": 720}

PRESETS = ("leve", "medio", "forte")
PRESET_LABEL = {
    "leve":  "Leve — qualidade quase intacta",
    "medio": "Médio — bom equilíbrio (recomendado)",
    "forte": "Forte — máxima economia de espaço",
}

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".mpeg", ".mpg", ".m4v"}
_AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".opus", ".wma", ".flac"}


class CompressError(Exception):
    pass


class CompressCancelled(Exception):
    """Cancelamento pedido pelo usuário — tratado à parte de uma falha real."""


# ── descoberta de capacidades ──────────────────────────────────────────────
_caps_cache: dict | None = None


def _ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def capabilities() -> dict:
    """O que esta máquina consegue fazer (cacheado: chamar ffmpeg é caro)."""
    global _caps_cache
    if _caps_cache is not None:
        return _caps_cache
    available = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    encoders = ""
    if available:
        try:
            encoders = subprocess.run([_ffmpeg_bin(), "-hide_banner", "-encoders"],
                                      capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            available = False
    _caps_cache = {
        "available": available,
        "hw_h264": "h264_videotoolbox" in encoders,
        "hw_hevc": "hevc_videotoolbox" in encoders,
        "x264": "libx264" in encoders,
        "x265": "libx265" in encoders,
    }
    return _caps_cache


def media_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "other"


# ── inspeção ───────────────────────────────────────────────────────────────
def probe(path: str) -> dict:
    """Metadados do arquivo (duração, resolução, bitrate, codec)."""
    if not os.path.isfile(path):
        raise CompressError("arquivo não encontrado")
    try:
        out = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompressError(f"ffprobe falhou: {exc}")
    if out.returncode != 0:
        raise CompressError("não foi possível ler o arquivo (formato não suportado?)")
    try:
        data = json.loads(out.stdout or "{}")
    except ValueError:
        raise CompressError("resposta inválida do ffprobe")

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    def _f(v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    size = os.path.getsize(path)
    duration = _f(fmt.get("duration"))
    return {
        "size_bytes": size,
        "duration_secs": duration,
        "bitrate_kbps": int(_f(fmt.get("bit_rate")) / 1000) or
                        (int(size * 8 / duration / 1000) if duration else 0),
        "width":  int(_f((video or {}).get("width"))),
        "height": int(_f((video or {}).get("height"))),
        "video_codec": (video or {}).get("codec_name") or "",
        "audio_codec": (audio or {}).get("codec_name") or "",
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def _target_bitrate(height: int, preset: str) -> int:
    """Bitrate-alvo para a altura dada, escolhendo o degrau mais próximo."""
    table = _VIDEO_BITRATE[preset]
    for step in sorted(table, reverse=True):
        if height >= step:
            return table[step]
    return table[min(table)]


def plan(path: str, preset: str = "medio") -> dict:
    """Diz o que aconteceria, sem comprimir nada — alimenta a estimativa da UI."""
    if preset not in PRESETS:
        raise CompressError(f"preset inválido: {preset!r}")
    info = probe(path)
    kind = media_kind(path)
    if kind == "other":
        raise CompressError("formato não suportado para compressão")

    if kind == "audio" or not info["has_video"]:
        target_kbps = int(_AUDIO_BITRATE[preset].rstrip("k"))
        scale_to = None
    else:
        height = info["height"] or 1080
        cap = _MAX_HEIGHT[preset]
        scale_to = cap if (cap and height > cap) else None
        eff_height = scale_to or height
        target_kbps = _target_bitrate(eff_height, preset) + \
                      int(_AUDIO_BITRATE[preset].rstrip("k"))

    dur = info["duration_secs"]
    estimated = int(target_kbps * 1000 / 8 * dur) if dur else 0
    current = info["size_bytes"]

    # Recodificar algo que já está enxuto piora a imagem e pode até aumentar o
    # arquivo. A margem de 10% evita ganhos irrisórios que não compensam.
    worth_it = bool(dur) and estimated < current * 0.9

    return {
        **info,
        "kind": kind,
        "preset": preset,
        "target_kbps": target_kbps,
        "scale_to_height": scale_to,
        "estimated_bytes": estimated,
        "estimated_saving_bytes": max(0, current - estimated),
        "estimated_saving_pct": round(100 * (current - estimated) / current, 1) if current else 0,
        "worth_it": worth_it,
        "reason": "" if worth_it else
                  ("arquivo sem duração legível" if not dur else
                   "este arquivo já está compacto — comprimir de novo pioraria a "
                   "qualidade sem ganho real de espaço"),
    }


# ── compressão ─────────────────────────────────────────────────────────────
def _build_cmd(src: str, dst: str, p: dict, caps: dict, prefer_hevc: bool) -> list:
    cmd = [_ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-i", src]

    if p["kind"] == "audio" or not p["has_video"]:
        cmd += ["-vn", "-c:a", "aac", "-b:a", _AUDIO_BITRATE[p["preset"]]]
    else:
        if prefer_hevc and caps["hw_hevc"]:
            vcodec = ["-c:v", "hevc_videotoolbox", "-tag:v", "hvc1"]
        elif caps["hw_h264"]:
            vcodec = ["-c:v", "h264_videotoolbox"]
        elif caps["x264"]:
            # Software: -preset veryfast equilibra tempo e tamanho.
            vcodec = ["-c:v", "libx264", "-preset", "veryfast"]
        else:
            raise CompressError("nenhum encoder de vídeo disponível no FFmpeg")

        vbit = p["target_kbps"] - int(_AUDIO_BITRATE[p["preset"]].rstrip("k"))
        cmd += vcodec + [
            "-b:v", f"{max(200, vbit)}k",
            "-maxrate", f"{int(max(200, vbit) * 1.5)}k",
            "-bufsize", f"{max(200, vbit) * 2}k",
        ]
        if p["scale_to_height"]:
            # -2 mantém a proporção e garante largura par (exigência dos codecs).
            cmd += ["-vf", f"scale=-2:{p['scale_to_height']}"]
        if p["has_audio"]:
            cmd += ["-c:a", "aac", "-b:a", _AUDIO_BITRATE[p["preset"]]]
        else:
            cmd += ["-an"]
        # faststart: o vídeo começa a tocar sem baixar o arquivo inteiro.
        cmd += ["-movflags", "+faststart"]

    cmd += ["-progress", "pipe:1", "-nostats", dst]
    return cmd


_TIME_RE = re.compile(r"out_time_ms=(\d+)")


def compress(path: str, preset: str = "medio", *, replace: bool = True,
             prefer_hevc: bool = False, on_progress=None, is_cancelled=None) -> dict:
    """Comprime `path`. Devolve o resumo do que aconteceu.

    replace=True troca o original pelo comprimido (atômico, só no fim e só se
    ficou menor). replace=False deixa o resultado ao lado, com sufixo.
    on_progress(pct) recebe 0–100. is_cancelled() é consultado durante a
    codificação: retornando True, o processo é morto e nada é substituído.
    """
    caps = capabilities()
    if not caps["available"]:
        raise CompressError("FFmpeg não encontrado nesta máquina")

    p = plan(path, preset)
    if not p["worth_it"]:
        return {"ok": False, "skipped": True, "reason": p["reason"], **p}

    src_ext = os.path.splitext(path)[1].lower()
    # Container de saída: mp4 para vídeo (compatível com tudo), m4a para áudio.
    out_ext = ".mp4" if p["kind"] == "video" and p["has_video"] else \
              (".m4a" if p["kind"] == "audio" else src_ext)

    tmp_fd, tmp_out = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                                       prefix=".compress_", suffix=out_ext)
    os.close(tmp_fd)
    os.remove(tmp_out)   # o ffmpeg cria; só queríamos reservar o nome

    cmd = _build_cmd(path, tmp_out, p, caps, prefer_hevc)
    duration = p["duration_secs"] or 0

    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            if is_cancelled and is_cancelled():
                proc.kill()
                raise CompressCancelled("compressão cancelada")
            m = _TIME_RE.search(line)
            if m and duration and on_progress:
                secs = int(m.group(1)) / 1_000_000
                on_progress(max(0.0, min(100.0, secs / duration * 100)))
        proc.wait(timeout=30)
        if proc.returncode != 0:
            err = (proc.stderr.read() or "")[-500:] if proc.stderr else ""
            raise CompressError(f"FFmpeg falhou: {err.strip() or 'erro desconhecido'}")

        if not os.path.isfile(tmp_out) or os.path.getsize(tmp_out) == 0:
            raise CompressError("a compressão não gerou saída")

        new_size = os.path.getsize(tmp_out)
        old_size = p["size_bytes"]
        # Rede de segurança final: se o resultado não ficou menor, o original
        # continua sendo a melhor versão — descartamos o trabalho.
        if new_size >= old_size:
            os.remove(tmp_out)
            return {"ok": False, "skipped": True,
                    "reason": "o resultado ficaria maior que o original — mantido como estava",
                    **p}

        if replace:
            final_path = os.path.splitext(path)[0] + out_ext
            os.replace(tmp_out, final_path)
            if final_path != path and os.path.exists(path):
                # Trocou de container (ex.: .mkv → .mp4): o antigo sai de cena.
                try:
                    os.remove(path)
                except OSError:
                    pass
        else:
            final_path = os.path.splitext(path)[0] + "_comprimido" + out_ext
            os.replace(tmp_out, final_path)

        return {
            "ok": True, "skipped": False,
            "path": final_path,
            "file": os.path.basename(final_path),
            "old_bytes": old_size,
            "new_bytes": new_size,
            "saved_bytes": old_size - new_size,
            "saved_pct": round(100 * (old_size - new_size) / old_size, 1),
            "preset": preset,
            "hardware": bool(caps["hw_h264"] or caps["hw_hevc"]),
        }
    except BaseException:
        if proc and proc.poll() is None:
            proc.kill()
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        raise
