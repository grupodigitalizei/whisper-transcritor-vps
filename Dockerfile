# Imagem do Whisper Transcritor para rodar em servidor (Easypanel / Docker).
#
# Decisões que não são óbvias e custaram tempo:
#   - Python 3.12: é a faixa testada (3.10–3.12). Em 3.13+ torch/whisper
#     costumam ficar meses sem wheel pronta e o build trava compilando.
#   - torch vem do índice CPU da PyTorch. O torch do PyPI para Linux é a
#     variante CUDA: arrasta ~2,5 GB de bibliotecas NVIDIA que não servem para
#     nada numa VPS sem GPU. O CPU-only corta a imagem para ~1/3.
#   - Deno: o extractor do YouTube no yt-dlp usa o yt-dlp-ejs (JavaScript) para
#     resolver o desafio `n`. Sem Deno, download de YouTube falha. O app já
#     procura o binário em ~/.deno/bin, então instalamos exatamente ali.
#   - ffmpeg é obrigatório (Whisper e compressor).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/root

# ── dependências de sistema ────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Deno (yt-dlp-ejs / desafio `n` do YouTube) ─────────────────────────────
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/root/.deno sh -s -- -y \
    && /root/.deno/bin/deno --version
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app

# ── dependências Python ────────────────────────────────────────────────────
# Camada separada do código: mudar o app não reinstala 2 GB de torch.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 \
    && pip install -r requirements.txt \
    && pip install Pillow

# ── código ─────────────────────────────────────────────────────────────────
COPY . .

# O app se re-executa dentro de venv/ se encontrar um. Aqui as dependências
# são globais no container — um venv/ vindo do COPY (build local, sem
# .dockerignore em dia) apontaria para caminhos do Mac e quebraria o boot.
RUN rm -rf venv venv.nosync .whisper_data .whisper_data.nosync

# 0.0.0.0 é obrigatório: o proxy do Easypanel fala com o container pela rede
# interna, e em 127.0.0.1 o app só responderia a si mesmo.
ENV WHISPER_HOST=0.0.0.0 \
    WHISPER_PORT=7860 \
    WHISPER_YTDLP_COOKIES=none

EXPOSE 7860

# Dados (transcrições, uploads, senhas, histórico) e o cache dos modelos
# Whisper (~3 GB no large-v3) precisam sobreviver a um redeploy.
VOLUME ["/app/.whisper_data", "/root/.cache/whisper"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WHISPER_PORT}/login" > /dev/null || exit 1

CMD ["python", "whisper-app.py"]
