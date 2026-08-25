#!/bin/sh
# Sobe o provedor de PO Token antes do app.
#
# Ele é um processo separado de propósito: o plugin do yt-dlp conversa com ele
# por HTTP, e mantê-lo fora do processo do app significa que uma falha dele
# derruba só o download do YouTube, não a transcrição inteira.
set -e

if [ "${WHISPER_POT_DISABLE:-0}" != "1" ] && [ -f /opt/bgutil/server/build/main.js ]; then
  node /opt/bgutil/server/build/main.js --port "${WHISPER_POT_PORT:-4416}" \
       >/tmp/bgutil.log 2>&1 &
  echo "▶  provedor de PO Token na porta ${WHISPER_POT_PORT:-4416} (log: /tmp/bgutil.log)"
fi

exec "$@"
