#!/bin/bash
# Espera o rate-limit do YouTube esfriar e baixa os 6 vídeos do escopo MD devagar,
# extrai mp3, envia ao Whisper (file upload) e salva as transcrições.
set -u
COOLDOWN=${1:-2100}          # segundos de espera inicial (default 35 min)
WORK=/tmp/redirect_dl
OUTAUDIO="$WORK/audio"
TRANS="/Users/jonathassilva/Documents/Claude/WHATSAPP 2.0/fontes/transcricoes"
LOG=/tmp/redirect-cooldown.log
BASE="http://127.0.0.1:7860"
POT="http://127.0.0.1:4416"
mkdir -p "$OUTAUDIO" "$TRANS"; : > "$LOG"
log(){ echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

declare -a IDS=(vQgC0CNXyEA H4HuS0eFei8 rMYUeZ3Gloc MHF6AQ7UXz0 Pm-NS7NhIm4 Ykwb75Cry5o)
declare -a NAMES=("1-Conectar-WhatsApp" "2-Criar-Campanha" "3-Criar-Grupos" "4-Mensagem-Grupo-Cheio" "5-Programar-Mensagens" "6-Como-Usar-Pixel")

log "Cooldown inicial de ${COOLDOWN}s para o rate-limit do YouTube esfriar..."
sleep "$COOLDOWN"

dl_one(){ # $1=id  $2=outbase ; baixa áudio gentilmente, tenta com e sem token
  local id="$1" out="$2" url="https://www.youtube.com/watch?v=$id"
  for attempt in 1 2 3; do
    log "  download $id tentativa $attempt"
    python3 -m yt_dlp -f "bestaudio/best[protocol*=m3u8]/best" -x --audio-format mp3 \
      --hls-prefer-native --concurrent-fragments 1 --sleep-requests 3 \
      --fragment-retries 20 --retries 10 \
      --cookies-from-browser chrome \
      --extractor-args "youtubepot-bgutilhttp:base_url=$POT" \
      --extractor-args "youtube:player_client=web_safari,tv" \
      -o "${out}.%(ext)s" "$url" >> "$LOG" 2>&1
    [ -f "${out}.mp3" ] && { log "  OK $id"; return 0; }
    # fallback sem token explícito (default clients)
    python3 -m yt_dlp -f "bestaudio/best" -x --audio-format mp3 \
      --concurrent-fragments 1 --sleep-requests 4 --fragment-retries 20 \
      --cookies-from-browser chrome -o "${out}.%(ext)s" "$url" >> "$LOG" 2>&1
    [ -f "${out}.mp3" ] && { log "  OK(fallback) $id"; return 0; }
    log "  falhou $id (tentativa $attempt) — esperando 300s antes de retry"
    sleep 300
  done
  return 1
}

transcribe_file(){ # $1=mp3 path  $2=nome
  local f="$1" name="$2"
  log "  enviando ao Whisper: $name"
  RESP=$(curl -s -m 120 -X POST "$BASE/api/transcribe" \
    -F "files=@${f}" -F "model=turbo" -F "language=pt" -F "folder=redirect-tutoriais")
  log "  resp upload: $(echo "$RESP" | cut -c1-200)"
}

for i in "${!IDS[@]}"; do
  id="${IDS[$i]}"; name="${NAMES[$i]}"
  log "=== ($((i+1))/6) $name [$id] ==="
  if dl_one "$id" "$OUTAUDIO/$name"; then
    transcribe_file "$OUTAUDIO/$name.mp3" "$name"
  else
    log "=== $name: download falhou após retries ==="
  fi
  sleep 30   # espaça entre vídeos para não re-disparar rate-limit
done

# Espera todas as transcrições do Whisper terminarem
log "Aguardando Whisper finalizar transcrições..."
for r in $(seq 1 240); do
  N=$(curl -s -m 8 "$BASE/api/active-tasks" | grep -o '"task_id"' | wc -l | tr -d ' ')
  [ "$N" = "0" ] && [ "$r" -gt 1 ] && break
  sleep 20
done

# Coleta os txt da pasta redirect-tutoriais (concluídos)
curl -s -m 15 "$BASE/api/history" > /tmp/redirect-history2.json
python3 - "$TRANS" <<'PY' >> "$LOG" 2>&1
import json,urllib.request,sys
out=sys.argv[1]
h=json.load(open('/tmp/redirect-history2.json'))
got=0
for e in h:
    if e.get('folder')=='redirect-tutoriais' and e.get('status')=='done':
        f=e['file']; nm=e.get('original_name') or f
        try:
            d=urllib.request.urlopen(f"http://127.0.0.1:7860/api/download/{f}/txt",timeout=30).read().decode('utf-8','replace')
            safe="".join(c if c.isalnum() or c in " -_." else "_" for c in nm)[:80].strip()
            open(f"{out}/{safe}.txt","w",encoding="utf-8").write(d); got+=1
            print("SALVO:",safe,len(d),"chars")
        except Exception as ex: print("ERRO baixar",nm,ex)
print("TOTAL salvos:",got)
PY
log "FIM."
touch /tmp/redirect-cooldown-DONE
