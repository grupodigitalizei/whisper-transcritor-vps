#!/usr/bin/env bash
#
# publicar.sh — coloca o Transcritor no ar para os funcionários.
#
# O app continua rodando 100% neste Mac (o Whisper precisa da CPU/GPU daqui).
# O que este script faz é abrir um túnel HTTPS do Tailscale apontando para o
# servidor local, gerando um endereço público que os funcionários acessam com a
# senha da área pública.
#
# Usa o Tailscale em modo "userspace-networking": o daemon roda como o seu
# usuário, sem precisar de senha de administrador do Mac e sem instalar extensão
# de kernel. A contrapartida é que este Mac não entra na sua rede Tailscale como
# um nó normal — ele só serve este túnel, que é exatamente o que queremos aqui.
#
# Uso:
#   ./publicar.sh          → sobe o daemon (se preciso) e o túnel; mostra o link
#   ./publicar.sh status   → mostra se o túnel está no ar e qual o endereço
#   ./publicar.sh parar    → derruba o túnel (o app continua rodando local)
#   ./publicar.sh login    → mostra o link de login na conta Tailscale
#
set -euo pipefail

PORT=7860
STATE_DIR="$HOME/.tailscale-userspace"
SOCK="$STATE_DIR/tailscaled.sock"
TS="/opt/homebrew/bin/tailscale"
TSD="/opt/homebrew/bin/tailscaled"

die() { printf '\n\033[31m✗ %s\033[0m\n\n' "$1" >&2; exit 1; }
ok()  { printf '\033[32m✓\033[0m %s\n' "$1"; }
warn(){ printf '\033[33m!\033[0m %s\n' "$1"; }

ts() { "$TS" --socket="$SOCK" "$@"; }

[ -x "$TS" ] || die "Tailscale não encontrado em $TS.
  Instale com:  brew install tailscale"

# ── sobe o daemon se ele não estiver rodando ──────────────────
ensure_daemon() {
  if ts status >/dev/null 2>&1 || ts status 2>&1 | grep -qi "logged out\|log in at"; then
    return 0
  fi
  mkdir -p "$STATE_DIR"
  nohup "$TSD" --tun=userspace-networking --socket="$SOCK" \
        --statedir="$STATE_DIR" >>"$STATE_DIR/tailscaled.log" 2>&1 &
  for _ in $(seq 1 20); do
    sleep 0.5
    [ -S "$SOCK" ] && return 0
  done
  die "O daemon do Tailscale não subiu. Veja o log:  $STATE_DIR/tailscaled.log"
}

# ── precisa estar logado na conta Tailscale ───────────────────
ensure_logged_in() {
  local out
  out="$(ts status 2>&1 || true)"
  if printf '%s' "$out" | grep -qi "logged out\|needs login\|log in at"; then
    printf '\n'
    warn "Este Mac ainda não está conectado a uma conta Tailscale."
    nohup ts up --hostname=transcritor --accept-dns=false \
          >"$STATE_DIR/up.log" 2>&1 &
    sleep 8
    local url
    url="$(grep -oE 'https://login\.tailscale\.com/a/[a-z0-9]+' "$STATE_DIR/up.log" | head -1 || true)"
    [ -n "$url" ] || url="$(printf '%s' "$out" | grep -oE 'https://login\.tailscale\.com/a/[a-z0-9]+' | head -1 || true)"
    printf '\n  Abra este link no navegador e entre (ou crie) sua conta:\n\n    %s\n\n' "${url:-<não foi possível obter o link — rode: ./publicar.sh login>}"
    printf '  Depois rode de novo:  ./publicar.sh\n\n'
    exit 1
  fi
}

case "${1:-subir}" in
  status)
    ensure_daemon
    ts funnel status 2>&1 || true
    exit 0
    ;;
  parar)
    ensure_daemon
    ts funnel --https=443 off 2>/dev/null || ts funnel reset 2>/dev/null || true
    ok "Túnel derrubado. O app continua acessível em http://127.0.0.1:$PORT"
    exit 0
    ;;
  login)
    ensure_daemon
    grep -oE 'https://login\.tailscale\.com/a/[a-z0-9]+' "$STATE_DIR/up.log" 2>/dev/null | head -1 \
      || ts up --hostname=transcritor --accept-dns=false
    exit 0
    ;;
esac

# 1. O app tem que estar no ar — o túnel sozinho não serve nada.
if ! curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/login"; then
  die "O servidor do Transcritor não está respondendo na porta $PORT.
  Suba ele primeiro:  ./venv/bin/python whisper-app.py"
fi
ok "Servidor local respondendo na porta $PORT"

ensure_daemon
ok "Daemon do Tailscale rodando"

ensure_logged_in
ok "Conectado à conta Tailscale"

# 2. Sobe o Funnel. `--bg` deixa rodando em background, então o túnel sobrevive
#    ao fechamento deste terminal (mas NÃO a um reboot — rode de novo depois).
printf '\n… abrindo o túnel público\n\n'
if ! ts funnel --bg "$PORT" 2>&1 | tee "$STATE_DIR/funnel.log"; then
  if grep -qiE 'funnel|not enabled|attribute' "$STATE_DIR/funnel.log"; then
    printf '\n'
    warn "O recurso Funnel ainda não está liberado na sua conta Tailscale."
    printf '  Abra o link que apareceu acima (ou o admin console em\n'
    printf '  https://login.tailscale.com/admin/settings/keys) para liberar,\n'
    printf '  e rode de novo:  ./publicar.sh\n\n'
  fi
  exit 1
fi

URL="$(ts funnel status 2>/dev/null | grep -oE 'https://[a-zA-Z0-9._-]+' | head -1 || true)"

printf '\n'
if [ -n "$URL" ]; then
  ok "No ar: $URL"
  printf '\n  Mande este link para os funcionários junto com a senha da área pública.\n'
  printf '  A senha você troca em Configurações → Área Pública.\n\n'
  printf '  Enquanto este Mac estiver ligado e o app rodando, o link funciona.\n'
  printf '  Para derrubar:  ./publicar.sh parar\n\n'
else
  printf '  Túnel criado. Veja o endereço com:  ./publicar.sh status\n\n'
fi
