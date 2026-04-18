import re
import os
import json

# ── 1. Adiciona endpoint /api/gaps no whisper-app.py ──────────────────────────
with open("whisper-app.py", "r", encoding="utf-8") as f:
    app_code = f.read()

gap_endpoint = '''
@app.get("/api/gaps/{filename}")
async def api_gaps(filename: str, min_gap: float = 1.0):
    """Detecta silêncios/respiros entre segmentos de fala."""
    base = _result_base(filename)
    json_path = os.path.join(RESULTS_DIR, base, f"{base}.json")
    if not os.path.exists(json_path):
        raise HTTPException(404, "Resultado não encontrado")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    segments = data.get("segments", [])
    gaps = []
    for i in range(1, len(segments)):
        prev_end = segments[i-1]["end"]
        curr_start = segments[i]["start"]
        duration = round(curr_start - prev_end, 2)
        if duration >= min_gap:
            gaps.append({
                "index": i,
                "start": round(prev_end, 2),
                "end": round(curr_start, 2),
                "duration": duration,
                "start_fmt": _fmt_ts(prev_end),
                "end_fmt": _fmt_ts(curr_start),
                "before": segments[i-1]["text"].strip()[-60:],
                "after": segments[i]["text"].strip()[:60],
            })
    return {"filename": filename, "total_gaps": len(gaps), "min_gap": min_gap, "gaps": gaps}

'''

# Insere antes do entry point
insert_before = '# ── Entry point'
app_code = app_code.replace(insert_before, gap_endpoint + insert_before, 1)

with open("whisper-app.py", "w", encoding="utf-8") as f:
    f.write(app_code)

print("✅ Endpoint /api/gaps adicionado ao whisper-app.py")

# ── 2. Adiciona aba Respiros no index.html ────────────────────────────────────
with open("index.html", "r", encoding="utf-8") as f:
    html = f.read()

# Adiciona botão da aba Respiros após os outros botões de aba no modal
old_tab_area = 'showTab(\'json\')'
new_tab_area = '''showTab('json')'''

json_tab_btn = re.search(r'(<button[^>]*onclick="showTab\(\'json\'\)"[^>]*>.*?</button>)', html, re.DOTALL)
if json_tab_btn:
    respiros_btn = '''
        <button class="tab-btn" onclick="showTab('gaps')" id="tab-gaps">🫁 Respiros</button>'''
    html = html[:json_tab_btn.end()] + respiros_btn + html[json_tab_btn.end():]
    print("✅ Botão Respiros adicionado")
else:
    print("⚠️ Botão JSON não encontrado, adicionando via outro método...")
    tab_btn_pattern = r'(<button[^>]*onclick="showTab\([^)]+\)"[^>]*>[^<]*</button>)'
    all_tabs = list(re.finditer(tab_btn_pattern, html, re.DOTALL))
    if all_tabs:
        last_tab = all_tabs[-1]
        respiros_btn = '\\n        <button class="tab-btn" onclick="showTab(\\\'gaps\\\')" id="tab-gaps">🫁 Respiros</button>'
        html = html[:last_tab.end()] + respiros_btn + html[last_tab.end():]
        print("✅ Botão Respiros adicionado (fallback)")

tab_content_pattern = r'(<div[^>]*id="tab-content-json"[^>]*>.*?</div>\s*)'
tab_json_content = re.search(tab_content_pattern, html, re.DOTALL)
if tab_json_content:
    gaps_panel = '''
        <div id="tab-content-gaps" class="tab-content" style="display:none">
          <div style="margin-bottom:10px;display:flex;align-items:center;gap:10px">
            <label style="font-size:13px">Silêncio mínimo:</label>
            <input type="number" id="gaps-min-sec" value="1.0" min="0.1" step="0.1" 
                   style="width:70px;padding:4px 8px;border-radius:6px;border:1px solid #444;background:#1a1a2e;color:#fff"
                   onchange="loadGaps(currentFile)">
            <span style="font-size:12px;color:#aaa">segundos</span>
            <span id="gaps-count" style="margin-left:auto;font-size:12px;color:#7c83ff;font-weight:600"></span>
          </div>
          <div id="gaps-list" style="max-height:400px;overflow-y:auto"></div>
        </div>'''
    html = html[:tab_json_content.end()] + gaps_panel + html[tab_json_content.end():]
    print("✅ Painel Respiros adicionado")
else:
    print("⚠️ Painel JSON não encontrado, tentando método alternativo...")
    modal_close = re.search(r'(</div>\s*</div>\s*<!-- ?modal)', html, re.IGNORECASE)
    if not modal_close:
        modal_close = re.search(r'(id="result-modal")', html)
    if modal_close:
        print("⚠️ Usando fallback para inserir painel")

js_func = '''
    function loadGaps(filename) {
        if (!filename) return;
        const minSec = parseFloat(document.getElementById('gaps-min-sec')?.value || 1.0);
        const listEl = document.getElementById('gaps-list');
        const countEl = document.getElementById('gaps-count');
        if (!listEl) return;
        listEl.innerHTML = '<p style="color:#aaa;font-size:13px">Carregando...</p>';
        fetch(`/api/gaps/${encodeURIComponent(filename)}?min_gap=${minSec}`)
            .then(r => r.json())
            .then(data => {
                countEl.textContent = data.total_gaps + ' respiro(s) encontrado(s)';
                if (!data.gaps || data.gaps.length === 0) {
                    listEl.innerHTML = '<p style="color:#aaa;font-size:13px">Nenhum respiro ≥ ' + minSec + 's encontrado.</p>';
                    return;
                }
                listEl.innerHTML = data.gaps.map((g, i) => `
                    <div style="background:#1a1a2e;border:1px solid #333;border-radius:8px;padding:12px;margin-bottom:8px">
                        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
                            <span style="background:#7c83ff22;color:#7c83ff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600">#${i+1}</span>
                            <span style="color:#fff;font-size:14px;font-weight:600">${g.start_fmt} → ${g.end_fmt}</span>
                            <span style="background:#ff6b6b22;color:#ff6b6b;padding:2px 8px;border-radius:4px;font-size:13px;font-weight:700;margin-left:auto">${g.duration}s</span>
                        </div>
                        <div style="font-size:12px;color:#aaa;margin-top:4px">
                            <span style="color:#888">...${g.before}</span>
                            <span style="color:#7c83ff;margin:0 6px">【silêncio】</span>
                            <span style="color:#888">${g.after}...</span>
                        </div>
                    </div>`).join('');
            })
            .catch(() => { listEl.innerHTML = '<p style="color:#ff6b6b">Erro ao carregar respiros.</p>'; });
    }

    const _origShowTab = typeof showTab === 'function' ? showTab : null;
    const _showTabWrapper = function(name) {
        if (_origShowTab) _origShowTab(name);
        if (name === 'gaps' && typeof currentFile !== 'undefined') loadGaps(currentFile);
    };
'''

script_close = html.rfind('</script>')
if script_close != -1:
    html = html[:script_close] + js_func + html[script_close:]
    print("✅ Função loadGaps adicionada ao JS")
else:
    body_close = html.rfind('</body>')
    if body_close != -1:
        html = html[:body_close] + '<script>' + js_func + '</script>\\n' + html[body_close:]
        print("✅ Função loadGaps adicionada via script tag")

show_tab_pattern = r'(function showTab\s*\([^)]*\)\s*\{)'
show_tab_match = re.search(show_tab_pattern, html)
if show_tab_match:
    hook = '''
        if (name === 'gaps' && typeof currentFile !== 'undefined') { loadGaps(currentFile); }'''
    func_start = show_tab_match.end()
    html = html[:func_start] + hook + html[func_start:]
    print("✅ Hook de aba Respiros adicionado ao showTab")

with open("index.html", "w", encoding="utf-8") as f:
    f.write(html)
