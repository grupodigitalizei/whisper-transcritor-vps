#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  auditar-tela.sh — renderiza o app no ego lite e mede o layout DE VERDADE
#
#  Por que existe: auditoria estática (ler CSS) e jsdom não têm engine de
#  layout — offsetWidth é sempre 0, nada é posicionado. Overflow, corte por
#  overflow:hidden, botão sem variante e alvo pequeno só aparecem quando algo
#  realmente renderiza. Este script usa o Chromium do ego lite via CDP.
#
#  Uso:
#     ./auditar-tela.sh                  # aba padrão (Transcrições)
#     ./auditar-tela.sh social           # aba Redes Sociais (carrega o último dataset)
#     ./auditar-tela.sh media            # Biblioteca de Mídia
#
#  Saída em ./auditoria-render/ :
#     tela-<aba>-<largura>.png    screenshots de página inteira
#     achados.json                overflow, corte, botões pelados, alvos pequenos
#
#  Pré-requisitos: ego lite instalado e o app rodando em 127.0.0.1:7860.
#  O login é manual: se a página cair em /login, entre na janela do ego lite
#  e rode de novo. Nenhuma senha é gravada neste arquivo.
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

ABA="${1:-transcriptions}"
APP_URL="${APP_URL:-http://127.0.0.1:7860/}"
OUT_DIR="$PWD/auditoria-render"
LARGURAS="${LARGURAS:-1440,1024,768,390,320}"

command -v ego-browser >/dev/null || { echo "erro: 'ego-browser' não encontrado. Instale o ego lite (https://lite.ego.app)."; exit 1; }
curl -sS -o /dev/null --max-time 4 "$APP_URL" || { echo "erro: o app não respondeu em $APP_URL. Suba o servidor primeiro."; exit 1; }

mkdir -p "$OUT_DIR"
echo "→ renderizando '$ABA' em $LARGURAS px"

ego-browser nodejs <<EGOJS
const APP_URL  = '$APP_URL'
const ABA      = '$ABA'
const OUT_DIR  = '$OUT_DIR'
const LARGURAS = '$LARGURAS'.split(',').map(Number)
const fs = require('fs')

const task = await useOrCreateTaskSpace('auditoria ui')
cliLog('task space: ' + task.id)

await openOrReuseTab(APP_URL, { wait: true, timeout: 30 })
await wait(2)

const info = await pageInfo()
if (info && info.dialog) { await cdp('Page.handleJavaScriptDialog', { accept: true }) }

const url = await js(String.raw\`location.pathname\`)
if (String(url).indexOf('/login') === 0) {
  cliLog('LOGIN_REQUIRED')
  cliLog('>> Faça login na janela do ego lite e rode este script de novo.')
} else {

// Abre a aba pedida e, no caso de social, carrega o dataset mais recente para
// medir com CONTEÚDO REAL — tela vazia nunca mostra bug de overflow.
await js(String.raw\`(async () => {
  const map = { transcriptions:'transcriptions', social:'social', media:'media', advanced:'advanced' };
  switchMainTab(map['\${ABA}'] || 'transcriptions');
  if ('\${ABA}' === 'social') {
    const pick = document.getElementById('social-dataset-picker');
    if (pick && pick.options.length) {
      pick.value = pick.options[0].value;
      await loadSocialDataset(pick.value);
    }
  }
  return true;
})()\`)
await wait(3)

// ── SONDA DE LAYOUT ────────────────────────────────────────────────────
const PROBE = String.raw\`(() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.opacity !== '0';
  };
  const desc = el => {
    const id = el.id ? '#' + el.id : '';
    const cl = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\s+/).slice(0,3).join('.') : '';
    const t = (el.textContent || '').trim().replace(/\s+/g,' ').slice(0,44);
    return el.tagName.toLowerCase() + id + cl + (t ? ' « ' + t + ' »' : '');
  };
  // Sobe até o 1º ancestral que recorta. IMPORTANTE: um elemento (ou ancestral)
  // com position:fixed escapa do overflow:hidden dos ancestrais — o único que
  // recorta um fixed é um ancestral que seja o bloco contêiner dele
  // (transform / filter / contain / will-change). Sem esta guarda a sonda
  // acusa "cortado" em toda barra flutuante. Falso positivo real, medido.
  const escapaClip = el => {
    for (let p = el; p; p = p.parentElement) {
      if (getComputedStyle(p).position === 'fixed') return true;
    }
    return false;
  };
  const criaContainingBlock = cs =>
    cs.transform !== 'none' || cs.filter !== 'none' ||
    cs.willChange.includes('transform') || cs.contain.includes('paint') ||
    cs.perspective !== 'none';
  const clipAncestor = el => {
    const fixo = escapaClip(el);
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p);
      const recorta = /hidden|clip|scroll|auto/.test(o.overflowX + o.overflowY);
      if (!recorta) continue;
      if (fixo && !criaContainingBlock(o)) continue;   // fixed passa por cima
      return p;
    }
    return null;
  };

  const out = { viewport: innerWidth, paginaEstoura: null, cortados: [], colados: [],
                botoesPelados: [], alvosPequenos: [], fonteMenorQue16EmCampo: [] };

  // 1. A página inteira estoura horizontalmente? (WCAG 1.4.10 Reflow)
  const de = document.documentElement;
  if (de.scrollWidth > innerWidth + 1)
    out.paginaEstoura = { scrollWidth: de.scrollWidth, viewport: innerWidth,
                          sobra: de.scrollWidth - innerWidth };

  const todos = Array.from(document.querySelectorAll('body *')).filter(vis);

  // 2. Elemento vazando do ancestral que recorta -> foi CORTADO na tela
  for (const el of todos) {
    const anc = clipAncestor(el);
    if (!anc) continue;
    const a = anc.getBoundingClientRect(), r = el.getBoundingClientRect();
    const ov = getComputedStyle(anc);
    const rolaX = /scroll|auto/.test(ov.overflowX);
    if (rolaX) continue;             // scroll horizontal é intencional (tabela)
    const vazaDir = r.right - a.right, vazaEsq = a.left - r.left;
    if (vazaDir > 2 || vazaEsq > 2)
      out.cortados.push({ el: desc(el), dentroDe: desc(anc),
                          vazaDireita: Math.round(vazaDir), vazaEsquerda: Math.round(vazaEsq) });
  }

  // 3. Conteúdo colado na borda do card (padding lateral 0) — regra 22
  for (const card of document.querySelectorAll('.card')) {
    if (!vis(card)) continue;
    const c = card.getBoundingClientRect();
    for (const ch of card.children) {
      if (!vis(ch)) continue;
      const r = ch.getBoundingClientRect();
      const folgaEsq = r.left - c.left, folgaDir = c.right - r.right;
      if (folgaEsq < 8 || folgaDir < 8) {
        const temPaddingProprio = parseFloat(getComputedStyle(ch).paddingLeft) >= 12;
        if (!temPaddingProprio)
          out.colados.push({ el: desc(ch), dentroDe: desc(card),
                             folgaEsquerda: Math.round(folgaEsq), folgaDireita: Math.round(folgaDir) });
      }
    }
  }

  // 4. Botão sem variante: sem fundo, sem borda e sem padding = não parece clicável
  for (const b of document.querySelectorAll('button, [role=button]')) {
    if (!vis(b)) continue;
    const cs = getComputedStyle(b);
    const semFundo = cs.backgroundColor === 'rgba(0, 0, 0, 0)' || cs.backgroundColor === 'transparent';
    const semBorda = cs.borderTopStyle === 'none' || parseFloat(cs.borderTopWidth) === 0;
    const semPad   = parseFloat(cs.paddingLeft) < 4 && parseFloat(cs.paddingTop) < 4;
    const semRaio  = parseFloat(cs.borderTopLeftRadius) < 2;
    const soIcone  = !(b.textContent || '').trim();
    if (semFundo && semBorda && semPad && semRaio && !soIcone)
      out.botoesPelados.push({ el: desc(b) });
  }

  // 5. Alvo de toque abaixo de 24x24 (WCAG 2.5.8)
  for (const el of document.querySelectorAll('button, a[href], input, select, textarea, [role=button], [tabindex="0"]')) {
    if (!vis(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 24 || r.height < 24)
      out.alvosPequenos.push({ el: desc(el), w: Math.round(r.width), h: Math.round(r.height) });
  }

  // 6. Campo com fonte < 16px (zoom automático do iOS)
  for (const el of document.querySelectorAll('input:not([type=checkbox]):not([type=radio]), select, textarea')) {
    if (!vis(el)) continue;
    const fs2 = parseFloat(getComputedStyle(el).fontSize);
    if (fs2 < 16) out.fonteMenorQue16EmCampo.push({ el: desc(el), fontSize: fs2 });
  }

  return JSON.stringify(out);
})()\`;

const achados = {}
for (const w of LARGURAS) {
  await cdp('Emulation.setDeviceMetricsOverride',
            { width: w, height: 900, deviceScaleFactor: 1, mobile: w < 700 })
  await wait(1.5)
  // Mede no TOPO e no FIM da página: barra flutuante só cobre conteúdo quando
  // há conteúdo embaixo dela. Medir só no topo dá falso "0 cobertos".
  await js(String.raw\`(() => { document.documentElement.style.scrollBehavior='auto';
    document.documentElement.scrollTop = document.documentElement.scrollHeight; return 1; })()\`)
  await wait(1)
  const raw = await js(PROBE)
  achados['w' + w] = JSON.parse(raw)
  const shot = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true })
  const file = OUT_DIR + '/tela-' + ABA + '-' + w + '.png'
  fs.writeFileSync(file, Buffer.from(shot.data, 'base64'))
  const a = achados['w' + w]
  cliLog('  ' + w + 'px -> ' + file
    + '  | estoura:' + (a.paginaEstoura ? a.paginaEstoura.sobra + 'px' : 'não')
    + ' cortados:' + a.cortados.length
    + ' colados:' + a.colados.length
    + ' pelados:' + a.botoesPelados.length
    + ' alvos<24:' + a.alvosPequenos.length
    + ' campo<16px:' + a.fonteMenorQue16EmCampo.length)
}
await cdp('Emulation.clearDeviceMetricsOverride')
fs.writeFileSync(OUT_DIR + '/achados.json', JSON.stringify(achados, null, 2))
cliLog('SAVED:' + OUT_DIR + '/achados.json')

}
EGOJS

echo
echo "→ pronto. PNGs e achados.json em: $OUT_DIR"
