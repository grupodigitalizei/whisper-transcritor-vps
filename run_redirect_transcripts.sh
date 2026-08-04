#!/bin/bash
# Espera o servidor whisper abrir, dispara os 6 vídeos do escopo MD e acompanha até concluir.
set -u
BASE="http://127.0.0.1:7860"
LOG=/tmp/redirect-transcripts.log
: > "$LOG"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# URLs (escopo MD): 5 passos Grupos + Como Usar Pixel
URLS="https://www.youtube.com/watch?v=vQgC0CNXyEA
https://www.youtube.com/watch?v=H4HuS0eFei8
https://www.youtube.com/watch?v=rMYUeZ3Gloc
https://www.youtube.com/watch?v=MHF6AQ7UXz0
https://www.youtube.com/watch?v=Pm-NS7NhIm4
https://www.youtube.com/watch?v=Ykwb75Cry5o"

log "Aguardando servidor ficar pronto..."
for i in $(seq 1 240); do
  if curl -s -m 3 "$BASE/api/stats" >/dev/null 2>&1; then log "Servidor PRONTO (~$((i*3))s)"; break; fi
  sleep 3
done
if ! curl -s -m 3 "$BASE/api/stats" >/dev/null 2>&1; then log "TIMEOUT: servidor não respondeu"; exit 1; fi

log "Enviando lote de 6 vídeos (model=turbo, lang=pt, pasta=redirect-tutoriais)..."
RESP=$(curl -s -m 60 -X POST "$BASE/api/transcribe-batch" \
  --data-urlencode "urls=$URLS" \
  --data-urlencode "model=turbo" \
  --data-urlencode "language=pt" \
  --data-urlencode "folder=redirect-tutoriais")
log "Resposta do lote: $RESP"

# Acompanha tarefas ativas até zerar
log "Acompanhando progresso (cada 15s)..."
for i in $(seq 1 400); do
  ACT=$(curl -s -m 5 "$BASE/api/active-tasks" 2>/dev/null)
  N=$(echo "$ACT" | grep -o '"task_id"' | wc -l | tr -d ' ')
  log "ativas=$N :: $(echo "$ACT" | cut -c1-300)"
  if [ "$N" = "0" ] && [ "$i" -gt 2 ]; then log "Sem tarefas ativas — concluído."; break; fi
  sleep 15
done

log "=== HISTÓRICO FINAL ==="
curl -s -m 10 "$BASE/api/history" | tee -a "$LOG" >/dev/null
log "FIM."
