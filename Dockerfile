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
        ffmpeg curl unzip ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Deno (yt-dlp-ejs / desafio `n` do YouTube) ─────────────────────────────
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/root/.deno sh -s -- -y \
    && /root/.deno/bin/deno --version
ENV PATH="/root/.deno/bin:${PATH}"

# ── provedor de PO Token (SABR do YouTube) ─────────────────────────────────
# Num IP de datacenter o YouTube passa a exigir GVS PO Token, e sem ele o
# yt-dlp descarta os formatos dos clientes tv_simply/mweb e o download termina
# em 403 — sintoma diferente do bloqueio puro de IP, e que proxy não resolve.
# Este provedor gera os tokens localmente. Roda como servidor HTTP na 4416
# (modo recomendado: o modo script criaria um processo por requisição).
ARG BGUTIL_VERSION=1.3.2
# git e npm entram e saem NA MESMA CAMADA: removê-los depois, numa camada
# seguinte, não devolveria espaço nenhum — os arquivos continuariam na camada
# em que foram criados. Só o `node` sobrevive, que é o runtime do provedor.
RUN apt-get update && apt-get install -y --no-install-recommends git npm \
    && git clone --depth 1 --single-branch --branch ${BGUTIL_VERSION} \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci --no-audit --no-fund \
    && npx tsc \
    && npm prune --omit=dev \
    && rm -rf /root/.npm /opt/bgutil/.git \
    && apt-get purge -y --auto-remove git npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── dependências Python ────────────────────────────────────────────────────
# Camada separada do código: mudar o app não reinstala 2 GB de torch.
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 \
    && pip install -r requirements.txt \
    && pip install Pillow bgutil-ytdlp-pot-provider

# ── código ─────────────────────────────────────────────────────────────────
COPY . .

# O app se re-executa dentro de venv/ se encontrar um. Aqui as dependências
# são globais no container — um venv/ vindo do COPY (build local, sem
# .dockerignore em dia) apontaria para caminhos do Mac e quebraria o boot.
RUN rm -rf venv venv.nosync .whisper_data .whisper_data.nosync

# 0.0.0.0 é obrigatório: o proxy do Easypanel fala com o container pela rede
# interna, e em 127.0.0.1 o app só responderia a si mesmo.
#
# Porta 80 e não 7860 (o padrão local): o Easypanel aponta o domínio para a 80
# por default, e errar isso dá "Service is not reachable" — um 502 do proxy que
# parece o app estar quebrado quando ele está no ar, só noutra porta. Escutando
# na 80 o container casa com o painel sem configuração nenhuma. Rodamos como
# root aqui, então portas baixas não são problema.
ENV WHISPER_HOST=0.0.0.0 \
    WHISPER_PORT=80 \
    WHISPER_YTDLP_COOKIES=none

EXPOSE 80

# Dados (transcrições, uploads, senhas, histórico) e o cache dos modelos
# Whisper (~3 GB no large-v3) precisam sobreviver a um redeploy.
VOLUME ["/app/.whisper_data", "/root/.cache/whisper"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WHISPER_PORT}/login" > /dev/null || exit 1

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "whisper-app.py"]
