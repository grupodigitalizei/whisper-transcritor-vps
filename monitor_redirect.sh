#!/bin/bash
set -u
BASE="http://127.0.0.1:7860"
OUT="/Users/jonathassilva/Documents/Claude/WHATSAPP 2.0/fontes/transcricoes"
LOG=/tmp/redirect-monitor.log
mkdir -p "$OUT"
: > "$LOG"
log(){ echo "[$(date +%H:%M:%S)] $*" >> "$LOG"; }

IDS="de3cb54e 2651eda9 82d9c536 bc19aaef 5715f5b5 b73b49a7"

log "Monitor iniciado. Aguardando conclusão das 6 transcrições..."
for round in $(seq 1 480); do
  ACT=$(curl -s -m 8 "$BASE/api/active-tasks" 2>/dev/null)
  N=$(echo "$ACT" | grep -o '"task_id"' | wc -l | tr -d ' ')
  log "ativas=$N"
  if [ "$N" = "0" ] && [ "$round" -gt 1 ]; then log "Nenhuma ativa. Coletando resultados."; break; fi
  sleep 20
done

# Coleta: pega o histórico e salva txt das 6 entradas que batem com os IDs
HIST=$(curl -s -m 15 "$BASE/api/history")
echo "$HIST" > /tmp/redirect-history.json
log "Histórico salvo. Extraindo arquivos..."

# Para cada id curto, encontra o "file" no histórico e baixa o txt
python3 - "$OUT" "$IDS" <<'PY' >> "$LOG" 2>&1
import json, sys, urllib.request
out=sys.argv[1]; ids=sys.argv[2].split()
hist=json.load(open('/tmp/redirect-history.json'))
def find(idshort):
    for e in hist:
        f=e.get('file','')
        if f.startswith(idshort+'_'):
            return e
    return None
for idshort in ids:
    e=find(idshort)
    if not e:
        print("NAO ENCONTRADO:", idshort); continue
    f=e['file']; name=e.get('original_name') or f; status=e.get('status')
    try:
        url=f"http://127.0.0.1:7860/api/download/{f}/txt"
        data=urllib.request.urlopen(url, timeout=30).read().decode('utf-8','replace')
        safe="".join(c if c.isalnum() or c in " -_." else "_" for c in name)[:80].strip()
        path=f"{out}/{safe}.txt"
        open(path,'w',encoding='utf-8').write(data)
        print(f"OK [{status}] {name} -> {path} ({len(data)} chars)")
    except Exception as ex:
        print(f"ERRO {name} ({f}): {ex}")
PY
log "FIM da coleta."
touch /tmp/redirect-monitor-DONE
