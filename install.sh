#!/bin/bash
# Instalação automática do Whisper Transcritor.
#
# Resolve as pegadinhas mais comuns de instalar isso num servidor Linux
# do zero, sem precisar descobrir cada erro na mão:
#   - servidor com Python bem mais novo que o testado (3.13/3.14+) — pacotes
#     como torch/whisper costumam demorar a ter build pra versão mais recente,
#     então preferimos um Python já instalado na faixa 3.10–3.12 se existir;
#   - Debian/Ubuntu separam o módulo `venv` num pacote à parte
#     (python3.X-venv) — sem ele, `python3 -m venv` falha com "ensurepip is
#     not available" e o venv criado fica sem pip;
#   - ffmpeg é obrigatório para o Whisper e não vem por padrão em muitas
#     instalações mínimas de servidor.
#
# Uso:
#   ./install.sh                  # detecta tudo automaticamente
#   PYTHON_BIN=python3.11 ./install.sh   # força um interpretador específico
set -u

log()  { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31mERRO:\033[0m %s\n' "$1" >&2; exit 1; }

cd "$(dirname "$0")"

OS="$(uname -s)"
IS_LINUX=false; IS_MACOS=false
[ "$OS" = "Linux" ] && IS_LINUX=true
[ "$OS" = "Darwin" ] && IS_MACOS=true

SUDO=""
if [ "$(id -u)" != "0" ] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

# ── 1. Escolher o interpretador Python ──────────────────────────────────
# yt-dlp-ejs (download do YouTube) exige 3.10+. Preferimos 3.10–3.12 quando
# disponíveis: são a faixa testada, e versões mais novas do Python costumam
# ficar meses sem build pronto (wheel) de torch/openai-whisper no PyPI —
# nesse caso pip install trava tentando compilar do zero ou falha.
version_ge_310() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 && version_ge_310 "$cand"; then
      PYTHON_BIN="$cand"
      break
    fi
  done
fi

[ -z "$PYTHON_BIN" ] && die "Nenhum Python 3.10+ encontrado. Instale Python 3.10, 3.11 ou 3.12 e rode de novo (ou defina PYTHON_BIN=... apontando pro interpretador certo)."
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "PYTHON_BIN='$PYTHON_BIN' não encontrado no PATH."
version_ge_310 "$PYTHON_BIN" || die "$PYTHON_BIN é mais antigo que 3.10 (necessário para o yt-dlp-ejs)."

PY_FULL_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR_MINOR_NUM="$("$PYTHON_BIN" -c 'import sys; print(sys.version_info.major*100+sys.version_info.minor)')"
if [ "$PY_MAJOR_MINOR_NUM" -ge 313 ]; then
  warn "Usando Python $PY_FULL_VER — mais novo que a faixa testada (3.10–3.12)."
  warn "Se 'pip install -r requirements.txt' falhar, instale Python 3.11 ou 3.12"
  warn "e rode: PYTHON_BIN=python3.11 ./install.sh"
fi
log "Usando $PYTHON_BIN (Python $PY_FULL_VER)"

# ── 2. Dependências de sistema (ffmpeg + módulo venv) ───────────────────
if $IS_LINUX && command -v apt-get >/dev/null 2>&1; then
  venv_pkg="python${PY_FULL_VER}-venv"
  need_pkgs=""
  command -v ffmpeg >/dev/null 2>&1 || need_pkgs="$need_pkgs ffmpeg"
  # Testa se o módulo venv do interpretador escolhido funciona de verdade —
  # em Debian/Ubuntu ele existe mas quebra em runtime sem o pacote -venv
  # (é isso que dá "ensurepip is not available").
  if ! "$PYTHON_BIN" -c "import ensurepip" >/dev/null 2>&1; then
    need_pkgs="$need_pkgs $venv_pkg"
  fi
  if [ -n "$need_pkgs" ]; then
    log "Instalando pacotes de sistema:$need_pkgs"
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq $need_pkgs \
      || die "Falha ao instalar$need_pkgs via apt. Instale manualmente e rode ./install.sh de novo."
  else
    log "ffmpeg e módulo venv já presentes."
  fi
elif $IS_MACOS; then
  if ! command -v ffmpeg >/dev/null 2>&1; then
    if command -v brew >/dev/null 2>&1; then
      log "Instalando ffmpeg via Homebrew..."
      brew install ffmpeg || die "Falha ao instalar ffmpeg via brew."
    else
      die "ffmpeg não encontrado e Homebrew não está instalado. Instale o ffmpeg manualmente (https://ffmpeg.org) e rode de novo."
    fi
  fi
else
  command -v ffmpeg >/dev/null 2>&1 \
    || warn "ffmpeg não encontrado — instale-o manualmente pelo gerenciador de pacotes do seu sistema antes de transcrever."
fi

# ── 3. Criar o venv e instalar as dependências Python ───────────────────
if [ -d venv ] && [ ! -x venv/bin/pip ]; then
  warn "venv/ existe mas está quebrado (sem pip) — recriando."
  rm -rf venv
fi
if [ ! -d venv ]; then
  log "Criando o ambiente virtual (venv/)..."
  "$PYTHON_BIN" -m venv venv || die "Falha ao criar o venv."
fi

log "Instalando dependências Python (pode demorar alguns minutos)..."
./venv/bin/python -m pip install --quiet --upgrade pip \
  || die "Falha ao atualizar o pip dentro do venv."
./venv/bin/python -m pip install -r requirements.txt || die "Falha ao instalar requirements.txt.
Se o erro for sobre torch/whisper sem build para Python $PY_FULL_VER, instale
Python 3.11 ou 3.12 e rode: PYTHON_BIN=python3.11 ./install.sh"

echo
log "Instalação concluída."
echo "Para rodar:"
echo "    ./venv/bin/python whisper-app.py"
echo
echo "Na primeira execução, o app gera duas senhas (admin e equipe) e imprime"
echo "no terminal — anote na hora, elas não podem ser recuperadas depois."
echo "Para definir senhas suas em vez de aleatórias:"
echo "    WHISPER_ADMIN_PASSWORD=... WHISPER_PUBLIC_PASSWORD=... ./venv/bin/python whisper-app.py"
