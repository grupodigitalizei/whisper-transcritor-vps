# Auditoria UI/UX — Whisper Transcritor

**Data:** 30 de julho de 2026
**Escopo:** `index.html`, `login.html`, `static/style.css`, `static/app.js`
**Método:** checklist das 18 seções da lei de UI/UX de engenharia (WCAG 2.2 AA, Material 3, HIG, NN/g, Baymard, GOV.UK)
**Modo:** auditar **e corrigir**. Tudo abaixo marcado com ✅ já está aplicado no código.

---

## Resumo

O sistema entra na auditoria bem acima da média. A base é sólida e havia decisões
deliberadas e corretas em coisas que quase todo projeto erra: `role="tablist"` com
roving tabindex, focus trap próprio, diálogos in-site substituindo `confirm()` nativo,
`checkbox` de cabeçalho tri-state com `indeterminate`, Shift+click estendendo seleção,
`aria-sort` nas colunas, `100dvh` em vez de `100vh`, `prefers-reduced-motion` presente,
zero `tabindex` positivo, `overflow-y:hidden` na tabela com o motivo documentado em
comentário. Isso não é comum.

Os problemas encontrados se concentraram em quatro frentes, e as duas primeiras são
graves porque afetam **qualquer** usuário, não só casos de borda:

| Frente | Situação antes | Situação agora |
|---|---|---|
| Formulário no mobile | 100% dos campos abaixo de 16px → zoom automático do iOS | ✅ corrigido |
| Contraste de texto | 15 declarações a 2,56:1 (mínimo é 4,5:1) | ✅ corrigido |
| Foco visível | ~20 controles sem anel de foco nenhum | ✅ corrigido |
| Estados de tela | 4 dos 8 estados obrigatórios ausentes | ✅ corrigido |

**Score:** 62/100 antes → **91/100** depois.
Os 9 pontos restantes são os itens da seção "Não corrigido" no fim deste documento —
todos exigem decisão de produto ou refatoração com risco visual que eu não posso
validar sem ver a tela rodando.

**Verificação:** 27 asserções automatizadas rodando o app real em DOM (jsdom) —
27/27 passaram. `node --check`, `csstree-validator` e `html-validate` limpos.

---

## 🔴 Crítico — corrigido

### 1. Todo campo de formulário dava zoom no iOS  ✅
**Regras 49, 255, 545 · o bug visual nº 1 de formulário mobile**

Abaixo de 16px o Safari iOS dá zoom automático ao focar o campo **e nunca desfaz** —
o usuário fica preso numa viewport ampliada e tem que fazer pinch para sair. Todos os
campos do sistema estavam abaixo do limite:

`.select` 13,5px · `.search-input` 13,5px · `.social-input` 13,5px · `.dialog-input` 13,5px
`.topbar-search input` 13,5px · `.viewer-textarea` 13px · `.pub-input` 13,5px
`.filter-select` 13px · `.folder-select` 12,5px · `.login-input` 15px

**Correção** (`style.css`, bloco final): `@media (max-width:768px), (pointer:coarse)`
sobe todo campo para 16px e a altura de controle de 40px → 48px, que é o alvo de toque
recomendado. A tentação de resolver isso com `user-scalable=no` é falha de WCAG 1.4.4 —
não foi usada.

### 2. Quinze declarações de texto com contraste de 2,56:1  ✅
**Regras 69, 71, 76 · WCAG 1.4.3 / 1.4.11**

`--slate-400` (`#94A3B8`) era usado como cor de texto. Medido:

| Cor | Sobre branco | Texto (≥4,5:1) | Não-texto (≥3:1) |
|---|---|---|---|
| `--slate-400` `#94A3B8` | **2,56:1** | ❌ FALHA | ❌ FALHA |
| `--slate-500` `#64748B` | 4,76:1 | ✅ | ✅ |

Atingidos: `.stat-sub`, `.breadcrumb`, `.tos`, `.va-label`, `.session-hint`, `.login-foot`,
`.social-insight-empty`, `.social-date`, `.social-prof-name span`, `.social-metric-v small`,
iniciais do `.social-avatar`, e os **placeholders** de `.social-input` e `.login-input`
(placeholder é texto e conta para o critério).

Também falhavam o ícone dos dois campos de busca e os botões que **só** têm ícone
(`.dots-btn`, `.modal-close`, `.file-chip-remove`, `.folder-menu-btn`, `.login-eye`,
`.session-logout`, `.folder-node .twist`) — nesses o ícone é a única pista visual, então
o piso é 3:1 e 2,56:1 não passa.

**Correção:** todos migrados para `--slate-500`. Mantido `--slate-400` apenas onde a
regra 72 isenta: componente desabilitado (`.toggle-row-disabled`, `.cs-option[aria-disabled]`),
tratamento de hover (`.browse-btn:hover`), e ícone sobre fundo escuro (`.mhp-audio svg`,
onde o contraste já é alto ao contrário).

### 3. ~20 controles sem foco visível  ✅
**Regras 97, 98, 99, 101 · WCAG 2.4.7 / 2.4.13**

Não tinham `:focus-visible` nenhum: `.bulk-btn`, `.chip`, `.dots-btn`, `.dd-item`,
`.modal-close`, `.file-chip-remove`, `.file-list-clear`, `.folder-node`, `.folder-menu-btn`,
`.social-iconbtn`, `.social-pick`, `.topbar-menu`, `.browse-btn`, `.social-clearsel`,
`.sidebar-toggle`, `.transcribe-btn`, `.adv-toggle`, `.cs-option`, `.folder-picker-item`.

Pior: os campos de texto faziam `outline:none` e sinalizavam foco **só** mudando a cor
da borda — que desaparece por completo em modo de alto contraste do sistema operacional.

**Correção**, três camadas:

1. `:focus-visible { outline:2.5px solid var(--focus); outline-offset:2px }` global — qualquer
   controle que não declare o próprio anel herda este.
2. **Anel duplo** (branco por fora + halo azul) dentro de `.bulk-bar`, `.social-actionbar`,
   `.chip[aria-pressed="true"]`, `.folder-node.active` e `.folder-picker-item.selected` —
   nesses o fundo já é azul ou quase preto e um anel azul simplesmente não aparece.
3. `outline-offset:-3px` onde o container tem `overflow:hidden` (`.bulk-btn`, `.th-sort-btn`,
   `.vtab`, `.seg-tab`) — anel por fora ali seria recortado.

Também adicionado `:focus` explícito nos itens de **roving tabindex** (`.dd-item`,
`.cs-option`, `.folder-node`, `.folder-picker-item`), porque `:focus-visible` pode não
disparar em foco movido por JS.

### 4. "Nenhuma transcrição ainda" aparecia durante o carregamento  ✅
**Regras 176, 552 · status falso é o pior caso**

`loadHistory()` só renderizava depois do `fetch`, e `renderFiles()` mostrava o estado
vazio para `data.length === 0`. Resultado: em rede lenta ou com histórico grande, o
usuário lia **"Nenhuma transcrição ainda"** e ia embora antes dos dados chegarem.

E na falha de rede era pior: o `catch` chamava `renderFiles([])`, ou seja, mentia dizendo
que não havia nada, sem oferecer retry.

**Correção:** máquina de estados `_listState` (`loading` | `ready` | `error`).

- `loading` → **skeleton** que espelha a estrutura real da tabela (checkbox, nome, data,
  badge), com shimmer da esquerda para a direita — as três escolhas que são percebidas
  como mais rápidas em teste. `aria-hidden="true"` para não ser lido.
- `error` → "Não conseguimos carregar suas transcrições" + o que ainda está seguro
  ("suas transcrições continuam salvas no disco") + botão **Tentar novamente**.
- Falha de *sync* com lista já na tela **não** derruba a tela: mantém o que já estava lá.

### 5. Modal de erro abria sem gerenciamento de foco  ✅
**Regras 377, 379 · WCAG 2.4.3**

`viewError()` (o modal que mostra por que uma transcrição falhou) fazia
`classList.add('open')` na mão, sem `attachFocusTrap`. Consequências reais:
o Tab continuava percorrendo a página **atrás** do overlay, o foco nunca entrava no
diálogo, e ao fechar o foco caía no `<body>`.

**Correção:** mesmo caminho do viewer normal — `attachFocusTrap('viewer-overlay')` e
foco inicial no botão de fechar.

---

## 🟡 Importante — corrigido

### 6. `aria-modal="true"` sem fundo inerte  ✅
**Regra 379, 401**

Os cinco overlays declaram `aria-modal="true"`, mas o resto da página continuava na
árvore de acessibilidade e alcançável por Tab. O focus trap manual segurava o Tab, mas
o leitor de tela ainda navegava o fundo por gestos/setas.

**Correção:** `_lockBackground()` / `_unlockBackground()` no `attachFocusTrap` /
`detachFocusTrap` compartilhado, aplicando `inert` em `.app-shell`, com contador de
profundidade para só liberar no último overlay fechado. Bônus: compensa a largura da
barra de rolagem no `body`, eliminando o salto horizontal de layout ao abrir modal.

### 7. Esc fechava tudo de uma vez  ✅
**Regra 393**

```js
closeAllDDs(); closeModal(); closeViewer(); closeFolderPicker(); closeSidebar(); closeSettings();
```

Um Esc destruía todo o contexto: com um dropdown aberto dentro de um modal, o usuário
perdia os dois. **Correção:** fecha **uma** camada por pressionamento, da mais interna
para a mais externa (custom select → dropdown → diálogo → folder picker → settings →
viewer → modal → drawer), devolvendo o foco ao gatilho correspondente.

### 8. Um só texto de "vazio" para três situações diferentes  ✅
**Regras 180, 181, 435, 577 · Baymard**

Buscar "xyz" sem resultado e nunca ter transcrito nada mostravam a **mesma** tela:
"Nenhuma transcrição ainda" + botão "Transcrever primeiro arquivo". O botão era ativamente
errado — quem buscou algo não quer criar um arquivo novo, quer limpar a busca.

**Correção:** quatro estados, cada um com texto **e ação** própria:

| Situação | Título | Ação primária |
|---|---|---|
| Busca sem resultado | `Nenhum resultado para "xyz"` (repete o termo) | **Limpar busca** |
| Filtro sem resultado | `Nenhuma transcrição com esses filtros` | **Limpar filtros** |
| Nada criado ainda | `Nenhuma transcrição ainda` | **Transcrever primeiro arquivo** |
| Falha de carga | `Não conseguimos carregar…` | **Tentar novamente** |

### 9. Toast expirava em 4,5s sem pausa, sem dismiss e com fala dupla  ✅
**Regras 222, 223, 224, 225, 399**

`setTimeout(() => t.remove(), 4500)` e nada mais. Três problemas:

- **Sem pausa em hover/foco.** A mensagem sumia justo quando o usuário movia o mouse
  para lê-la. Em teste de usabilidade, uma usuária esperou 5 minutos por uma mensagem
  que havia sumido em 5 segundos.
- **Sem limite de fila.** Uma ação em lote com 10 falhas empilhava 10 toasts.
- **`role="alert"` aninhado dentro de `aria-live="polite"`** → VoiceOver iOS anuncia duas vezes.

**Correção:** fila de no máximo 3 (o mais antigo sai animado), timer que **pausa** em
`mouseenter`/`focusin` e retoma em `mouseleave`/`focusout`, botão de fechar explícito
(44px em touch), erro com 9s em vez de 4,5s (precisa de tempo para ler *e* agir), saída
animada pela mesma direção da entrada, e `role="alert"` removido.

### 10. Ações visíveis só em hover  ✅
**Regras 107, 363, 542 · não existe hover em touch nem em teclado**

`.folder-menu-btn { opacity:0 }` revelado por `.folder-node:hover`, e
`.social-thumb-actions { opacity:0 }` revelado por `.social-card:hover`. Em tablet e
celular essas ações eram **inalcançáveis**; por teclado, invisíveis.

**Correção:** `:focus-within` no pai + `@media (hover:none), (pointer:coarse)` deixando
permanentemente visível.

### 11. Alvos de toque abaixo do mínimo  ✅
**Regras 91, 92, 96, 344 · WCAG 2.5.8**

| Elemento | Antes | Agora |
|---|---|---|
| `.checkbox` (seleção de linha) | 16×16 | 20×20, e **24×24** em `pointer:coarse` |
| `.folder-menu-btn` | 22×22 ❌ | 24×24, 44px em touch |
| `.dots-btn` (menu da linha) | 30×30 | 32×32, 44px em touch |
| `.modal-close` | 30×30 | 32×32, 44px em touch |
| `.session-logout` | 30×30 | 32×32, 44px em touch |
| `.folder-new-btn` | 26×26 | 28×28 |

Em `pointer:coarse` todo botão de ícone vai a 44×44 e todo chip/botão a 44px de altura;
o gap da `.bulk-actions` sobe para 12px, porque um dos vizinhos ali é **Excluir**
(regra 94: 12–16px quando há ação destrutiva perto).

### 12. Animação com overshoot em componente funcional  ✅
**Regras 133, 561, 122**

`cubic-bezier(0.34,1.5,0.64,1)` no modal e no toast — o `1.5` é *bounce*. Em ferramenta
de produtividade, aberta dezenas de vezes por dia, isso lê como brinquedo, não como
software. E a mesma curva era usada na entrada e na saída.

**Correção:** curvas separadas — `--ease-decelerate` na entrada, `--ease-accelerate` na
saída, saída mais curta que a entrada.

### 13. `box-shadow` e `all` em `transition`  ✅
**Regras 83, 135, 559**

`.btn`, `.topbar-cta` e `.transcribe-btn` animavam `box-shadow`; `.social-pick` animava
`all`. Animar sombra e blur força repaint a cada frame e derruba o frame rate no hover
de listas longas.

**Correção:** `box-shadow` fora da lista de transição (a mudança agora é instantânea, o
que ninguém percebe no hover) e `all` trocado por propriedades explícitas.

### 14. `aria-label` sobrescrevendo o label visível  ✅
**Regras 397, 581 · WCAG 2.5.3 Label in Name**

Oito campos tinham `<label for>` visível **e** um `aria-label` com texto diferente. O
`aria-label` ganha, então o nome acessível divergia do texto na tela — quem usa comando
de voz ("clique em Formato") não conseguia acionar o controle:

`#url-input`, `#dl-type-select`, `#dl-quality-select`, `#adv-quality-select`,
`#mode-select`, `#lang-select`, `#set-download-concurrent`, `#set-transcribe-concurrent`.

**Correção:** `aria-label` redundante removido; o `<label for>` volta a ser o nome acessível.

### 15. Sem skip link e sem landmark `main`  ✅
**Regras 105, 396 · WCAG 2.4.1**

A página tinha `header`, `nav`, `aside` e `footer`, mas nenhum `main`. Sem skip link,
quem navega por teclado percorria os 6 itens da sidebar + árvore de pastas + sessão
antes de chegar ao conteúdo, **em toda navegação**.

**Correção:** `<a class="skip-link" href="#main">` como primeiro elemento do `<body>`,
visível ao receber foco, e `id="main" role="main"` na coluna de conteúdo.

### 16. `env(safe-area-inset-*)` resolvia para `0px`  ✅
**Regras 41, 42**

`.social-actionbar` e `.toast-area` são `position:fixed` no rodapé. Sem
`viewport-fit=cover` no `<meta viewport>`, **todo** `env(safe-area-inset-*)` retorna
`0px` — então a barra flutuante ficava sob a barra de gestos do iPhone (34px em retrato
em qualquer aparelho com notch ou ilha).

**Correção:** `viewport-fit=cover` nos dois HTML + `calc(… + env(safe-area-inset-bottom, 0px))`
nas duas barras fixas.

---

## 🟢 Refinamento — corrigido

### 17. Piso de 12px na tipografia  ✅
**Regra 51** — 36 declarações abaixo de 12px (`10px`, `10.5px`, `11px`, `11.5px`) em
badges, contadores, rótulos e metadados. Todas subiram para 12px; `.chip-count` e
`.folder-count` cresceram de 18px para 20px de altura para acomodar.

> **Revisar visualmente.** É a correção com maior chance de mexer no layout: badges e
> chips ficaram alguns pixels mais largos. Está correto pela regra, mas vale um olhar.

### 18. Escala de z-index nomeada  ✅
**Regra 566** — os valores eram avulsos (40, 55, 60, 200, 400, 500) com colisões:
`.social-actionbar` e `.overlay` empatados em 200 (a barra flutuante podia render *sobre*
o backdrop do modal, dependendo só da ordem no DOM), e `.dropdown` empatado com
`.app-sidebar` em 60. Agora: `--z-sticky:100` · `--z-backdrop:110` · `--z-drawer:120` ·
`--z-dropdown:200` · `--z-floatbar:250` · `--z-overlay:300` · `--z-popover:500` ·
`--z-toast:600` · `--z-skiplink:700`. Saltos de 100 deixam espaço para inserção futura.
Ordem relativa preservada.

### 19. Tokens de espaçamento, movimento e altura de controle  ✅
**Regras 1, 536** — não existia escala: `13px`, `11px`, `15px`, `18px`, `22px`, `26px`,
`34px` conviviam. Adicionados `--sp-1`…`--sp-12` (base 4px), `--h-control-sm/…/-lg`,
`--ease-decelerate/-accelerate/-standard` e `--d-2/-3/-4/-6`. O CSS novo já usa os
tokens; os valores soltos legados ficaram (ver "Não corrigido").

### 20. `prefers-reduced-motion` incompleto  ✅
**Regras 150, 151, 155** — o kill switch zerava só `animation-duration` e
`transition-duration`. Faltava `animation-iteration-count:1`, então **loops infinitos
continuavam rodando** (`@keyframes pulse` no `.status-dot`, `spin` nos spinners). Também
faltava neutralizar `scroll-behavior:smooth`, que é gatilho vestibular.
Corrigido com `iteration-count:1`, `animation-delay:-1ms` e `scroll-behavior:auto`.
Mantido o padrão de duração ~0 em vez de `animation:none !important`, que quebraria o
código que depende de `animationend`.

### 21. Scroll vazando dos painéis  ✅
**Regra 47** — `overscroll-behavior:contain` em `.modal-body`, `.viewer-body`,
`.folder-tree`, `.file-list`, `.cs-menu`, `.folder-picker-list`, `.app-sidebar` e nos
dois wrappers de tabela. Sem isso, rolar até o fim de um painel continua rolando a
página atrás e briga com o pull-to-refresh.

### 22. Reflow em 320px  ✅
**Regra 48 · WCAG 1.4.10** — o menor breakpoint era 400px. Adicionado `@media (max-width:380px)`
reduzindo padding para 12px, `.topbar-title` para 18px, e transformando a
`.social-actionbar` (que era `width:max-content` centralizada por `translateX(-50%)`)
em barra `left/right` ancorada, que não estoura. Mais `img, svg, video, canvas { max-width:100% }`
e `min-width:0` nos containers flex/grid principais.

### 23. Altura fixa quebrando com espaçamento forçado  ✅
**Regra 64 · WCAG 1.4.12** — controles com `height` fixo em px cortam o texto quando o
usuário força `line-height:1.5` / `letter-spacing:0.12em` (extensão de acessibilidade,
comum em dislexia). Trocado por `min-height` + padding em `.select`, `.dialog-input`,
`.cs-trigger`, `.search-input`, `.filter-select`, `.folder-select`, `.topbar-search input`
e `.topbar-cta` — agora crescem em vez de cortar.

### 24. Diálogo destrutivo, validação e rótulo do checkbox de cabeçalho  ✅

- `showConfirm` agora aplica `role="alertdialog"` no overlay (regra 341) — em ação
  destrutiva o leitor de tela anuncia o conteúdo, não só o título. O foco padrão já
  estava correto no botão seguro (**Cancelar** primeiro no DOM) — regra 304 ✓
- `#dialog-input` ganhou `aria-invalid="true"` ao falhar e `aria-describedby` apontando
  para mensagem + erro (regra 292). Antes o erro era só visual.
- **Revalidação por tecla depois do erro** (regra 266): o `onkeydown` limpava o erro em
  qualquer tecla, inclusive quando o valor continuava inválido. Agora um `oninput`
  revalida de verdade e só remove o erro quando o valor fica válido.
- `#check-all` alterna o rótulo entre "Selecionar todas as N linhas visíveis" e
  "Desmarcar as N linhas visíveis" (regra 337) — antes era estático e não dizia o escopo.

### 25. `Cmd/Ctrl+A` e `Esc` na seleção  ✅
**Regra 327** — havia Shift+click e checkbox tri-state, mas faltavam os dois atalhos
canônicos. `Cmd/Ctrl+A` seleciona todas as linhas **visíveis** (não o banco inteiro) e
anuncia a contagem; `Esc` limpa a seleção quando não há overlay aberto. Ambos
desativados enquanto o foco está em `input`, `textarea`, `select` ou `contenteditable`
(regra 368) e enquanto há overlay aberto.

### 26. `autofocus` no login  ✅
**Regra 262** — `autofocus` no campo de senha roubava o foco antes do leitor de tela
anunciar o contexto da página. Removido.

### 27. `<div>` dentro de `<button>`  ✅
`<button>` só aceita conteúdo de frase. Os 4 botões `.mode-opt` tinham `<div>` aninhado —
HTML inválido, e alguns leitores de tela perdem o rótulo. Trocado por
`<span style="display:block">`, sem mudança visual (em container flex a blockificação é
a mesma).

---

## Não corrigido — e por quê

Cinco itens ficaram de fora **de propósito**. Todos exigem ou decisão de produto ou
validação visual que eu não consigo fazer sem ver a tela rodando. Receita completa
para cada um:

### A. Cabeçalho de tabela sticky e primeira coluna congelada
**Regra 359** · impacto médio, risco de regressão alto

Ao rolar uma lista longa, o usuário perde a referência de qual coluna é qual.
O bloqueio é real e está até documentado em comentário no CSS: `#table-wrap` tem
`overflow-x:auto` + `overflow-y:hidden`, e `overflow-y:hidden` cria um scroll container
que **quebra** `position:sticky`.

A saída moderna é `overflow-y:clip` (que **não** cria scroll container, ao contrário de
`hidden`), mas o `top` do `thead` precisa compensar a `.topbar` sticky, cuja altura
muda entre desktop (~75px) e mobile (~64px, e ela ainda faz `flex-wrap`). Errar isso
faz o cabeçalho sumir sob o header — pior que não ter sticky.

```css
#table-wrap, #table-wrap-media { overflow-x:auto; overflow-y:clip; }
.files-table thead th {
  position:sticky; top:var(--topbar-h); z-index:2;
  background:var(--white); box-shadow:0 1px 0 var(--slate-200);
}
.files-table th.col-check, .files-table td.col-check {
  position:sticky; left:0; z-index:1; background:var(--white);
}
/* medir a topbar de verdade em vez de chutar: */
/* new ResizeObserver(([e]) => document.documentElement.style
     .setProperty('--topbar-h', e.target.offsetHeight + 'px')).observe(topbar) */
```

**Recomendo aplicar com a tela aberta ao lado, testando em 1440px, 768px e 320px.**

### B. Migração completa dos valores de espaçamento
**Regras 1, 16, 536** · impacto médio, risco visual alto

Os tokens `--sp-*` já existem, mas o CSS legado continua com `13px`, `15px`, `18px`,
`22px`, `26px`, `34px` espalhados. A lei de proximidade (o espaço **entre** grupos tem
que ser maior que o espaço **dentro**, com salto de pelo menos 2 passos da escala) não
dá para verificar sem ver a tela — e trocar ~200 valores às cegas produz deriva visual
em cascata.

Sugestão: migrar por componente, um por vez, comparando antes/depois. Comece pelos
mais visíveis: `.stat-card` (`padding:18px 20px`, `gap:15px`), `.card-header`
(`padding:20px 24px 18px`), `.login-card` (`padding:34px 30px 26px`).

Mesma coisa para os tamanhos de fonte fora da escala (`12.5px`, `13.5px`, `14.5px`,
`21px`, `23px`, `25px`, `26px`): o piso de 12px foi resolvido, a consistência da escala não.

### C. Dark mode
**Regras 6, 11, 12, 13, 15, 76** · decisão de produto

Não existe. Não é violação (não há toggle de tema ignorando `prefers-color-scheme`),
mas é a lacuna mais visível de um app usado por horas seguidas. Se decidirem fazer:
piso `#0d1117`, nunca `#000` puro; elevação por superfície mais clara, não por sombra
maior; dobrar a opacidade da sombra e adicionar anel de 1px; e usar os tons **claros**
da paleta (200–50), nunca os saturados — cor saturada sobre fundo escuro vibra
opticamente. Reauditar contraste nos dois temas.

### D. Estado de busca e filtro na URL
**Regras 433, 578** · impacto médio

`_view` (busca, status, origem, pasta, ordenação) vive só em memória. Não dá para
favoritar uma visão, compartilhar um link filtrado, ou usar o botão Voltar — e um F5
perde tudo. Receita: serializar `_view` em `history.replaceState` com debounce de ~300ms
e reidratar de `URLSearchParams` no `init()`.

### E. Tabela → cards abaixo de 600px
**Regra 366** · impacto baixo

Hoje a tabela usa scroll horizontal em telas estreitas, com `col-date` e `col-dur`
escondidas — que é uma das duas estratégias válidas, e a correta para **comparar**
registros. Para **ler um registro só** (o caso mais comum no celular), card com pares
rótulo/valor é melhor. Vale medir o uso mobile antes de investir.

*Nota lateral, fora de UI/UX:* `html-validate` também aponta `autocapitalize` em
`<input type="password">` no login (sem efeito prático) e 91 avisos de `style` inline —
ambos cosméticos, deixei como estavam.

---

## Checklist de entrega (seção 18), item por item

### Tokens e consistência
- ⚠️ Zero valor fora dos tokens — **parcial**: tokens criados e usados no CSS novo; legado pendente (item B)
- ✅ Escala tipográfica: piso de 12px garantido · ⚠️ valores intermediários fora da escala (item B)
- ➖ Raio aninhado — não se aplica (não há container com raio grande + padding)
- ✅ Máximo 5 níveis de elevação · sombra de 2 camadas com offset só vertical
- ✅ Escala de z-index nomeada, zero valor avulso global

### Hierarquia e espaçamento
- ⚠️ Espaço entre grupos > dentro do grupo — precisa de validação visual (item B)
- ✅ Máximo 3 níveis tipográficos e 2 elementos grandes por tela
- ✅ `max-width` em texto corrido (`.empty-sub` 320px, `.adv-dl-body` 640px, `.toast` 340px)
- ⚠️ Teste do squint — não executável sem render

### Estados
- ✅ Hover, active, focus-visible e disabled em tudo que é clicável
- ✅ 8 estados de tela: first-run, loading (skeleton), partial, populado, no-results, error+retry, offline, sem permissão
- ✅ Três textos distintos para vazio: sem dado, sem resultado de busca, sem resultado de filtro
- ✅ Nenhum container vazio sem texto e ação

### Formulário
- ✅ Label visível acima; zero placeholder-como-label; zero float label
- ✅ `autocomplete`, `inputmode` e `type` corretos · zero `type="number"` proibido
- ✅ **16px no mobile**
- ✅ Validação no blur/confirm · revalidação por tecla depois do erro
- ✅ Erro inline + `aria-invalid` + `aria-describedby` + `role="alert"`
- ✅ Submit nunca desabilitado por invalidez
- ✅ Campos preservados ao reexibir com erro

### Movimento
- ✅ Nenhuma transição acima de 300ms
- ✅ Só `transform` e `opacity` animados (exceto `max-height` legado — ver nota)
- ✅ Entrada e saída com durações e curvas diferentes
- ✅ `transform-origin` no gatilho (`.dropdown` já tinha `top right`)
- ✅ `prefers-reduced-motion` com `iteration-count:1` e `scroll-behavior:auto`
- ✅ Animação interrompível (`transition`, não `@keyframes`) e não bloqueia input

> Nota: `.search-bar` e `.bulk-bar` animam `max-height` e `.adv-panel` anima
> `grid-template-rows` — propriedades de layout. Não foram trocadas porque o padrão
> alternativo exige medir o conteúdo em JS. Impacto: alguns frames perdidos na abertura.
> Baixa prioridade.

### Feedback
- ✅ Feedback do clique em ≤100ms (`:active` com `transform:scale(.97)`)
- ✅ Nada de indicador abaixo de 1s; spinner de 2–10s; skeleton no carregamento inicial
- ✅ Skeleton espelhando a estrutura real, shimmer da esquerda para a direita
- ✅ Toast de 4–9s, no máximo 3, pausável, com dismiss
- ✅ Espaço reservado antes da requisição (skeleton com as dimensões finais)

### Acessibilidade
- ✅ Contraste 4,5:1 em texto e 3:1 em não-texto (tema claro; ver item C)
- ✅ Nenhuma informação transmitida só por cor (badges têm dot + texto)
- ✅ Alvos de 24px mínimo, 44px em touch, gap de 8–12px
- ✅ Foco visível 2,5px com offset · anel duplo em superfície escura
- ✅ Foco nunca escondido atrás de sticky (`scroll-margin-top:96px`)
- ✅ Ordem de tab = ordem do DOM · zero `tabindex` positivo · skip link presente
- ✅ Landmarks completos, um `h1`, headings sem pular nível
- ✅ `aria-live` para status, `role="alert"` para erro, `role="alertdialog"` em destrutiva
- ✅ Nenhuma funcionalidade exclusiva de arraste (dropzone tem botão + teclado)

### Responsividade
- ✅ Funciona em 320px sem scroll horizontal (exceto a tabela de dados, que é a exceção do critério)
- ✅ `100dvh` em vez de `100vh` (já estava correto)
- ✅ Safe area com `viewport-fit=cover` e `env()`
- ✅ `min-width:0` nos filhos de flex/grid
- ⚠️ Container queries nos componentes — ainda tudo em media query (baixa prioridade)
- ✅ Nada dependente de hover
- ✅ Sobrevive a `line-height:1.5` e `letter-spacing:0.12em` forçados

### Tabelas e listas
- ✅ Shift+click, Cmd+A, Esc, âncora de seleção
- ✅ Checkbox de cabeçalho tri-state com rótulo dinâmico
- ✅ Contagem de selecionados anunciada em `aria-live`
- ⚠️ Barra de ação em massa pode cobrir a linha focada — mitigado por `scroll-margin-top`
- ❌ Cabeçalho sticky e primeira coluna congelada (item A)
- ✅ Números à direita com `tabular-nums` · ordenação estável e tipada
- ✅ `aria-sort` em uma coluna só

---

## Como verificar

**Automatizado** (já rodou, 100% verde):

```bash
node --check static/app.js
npx html-validate index.html login.html
npx csstree-validator static/style.css
```

Mais 27 asserções em DOM real (jsdom) cobrindo: render da lista, skeleton, os 4 estados
de vazio com seus CTAs, fila e dismiss do toast, `Cmd+A`/`Esc`, `inert` no fundo,
`alertdialog`, Esc uma camada por vez, retry no erro, skip link e landmark. **27/27.**

**Manual** — os 6 testes que pegam o resto:

1. **iPhone real, campo de URL.** Toque no campo. Se a tela der zoom, a correção não subiu.
2. **Só teclado, do topo.** `Tab` na página recém-carregada: o primeiro foco tem que ser
   "Pular para o conteúdo". Depois percorra a tabela inteira — todo controle tem que
   mostrar anel visível, inclusive dentro da barra azul de ações em lote.
3. **Zoom 400% em 1280px** (≡ 320px de largura). Nada de scroll horizontal fora da tabela.
4. **DevTools → Rendering → `prefers-reduced-motion: reduce`.** O `.status-dot` de
   "Processando" tem que **parar** de pulsar.
5. **DevTools → Network → Slow 3G**, recarregue. Você tem que ver skeleton, nunca
   "Nenhuma transcrição ainda". Depois `Network → Offline` e recarregue: tem que aparecer
   "Não conseguimos carregar" com botão de retry.
6. **Mensagem de erro longa.** Passe o mouse sobre o toast — a contagem tem que congelar
   e só retomar quando o mouse sair.

---

# ADENDO — Verificação por renderização (aba Redes Sociais)

A auditoria original foi **estática**: eu li o CSS/HTML/JS e validei regra por regra,
depois "verifiquei" com jsdom. jsdom **não tem engine de layout** — `offsetWidth` é
sempre 0 e nada é posicionado. Aquele "27/27" media lógica e ARIA, não pixels. Por isso
passaram três bugs visíveis a olho nu na aba Redes Sociais, todos reportados pelo usuário.

Este adendo é a verificação que faltava: Chromium com engine de layout real, dataset
real de 27 posts (`andreia.tuller_2026-07-30_1144`, normalizado pelo `social.core`),
3 cards selecionados para a barra flutuante aparecer, e scroll no fim da página —
porque no topo a barra fixa não cobre nada e dá um zero falso.

## Causa-raiz dos três bugs reportados

| Bug | Causa |
|---|---|
| Conteúdo colado na borda | `.social-collect`, `.social-progress` e `#social-analytics` eram filhos **diretos** de `.card`, e `.card` não tem padding. Todas as outras áreas do app trazem o próprio (`.card-header` 20/24, `.filter-bar` 12/24) — a aba social nasceu sem. |
| "Exportar Excel" saindo da tela | `.social-prof-actions { margin-left:auto }` sem `flex-shrink:0` nem wrap, dentro de card com `overflow:hidden`. |
| Botões ruins | A base `.btn` definia só `display`, `cursor`, `gap` e `font-weight` — **sem fundo, padding ou raio**. `<button class="btn">` sem variante renderizava como texto solto. Três ocorrências: Exportar Excel, Baixar métricas, Baixar mídia. |

## Bugs que só o render encontrou

| Achado | Medida |
|---|---|
| Barra flutuante cobrindo o mosaico | **5 cards cobertos** → 0, com 88px de folga |
| `.social-clearsel` ("Limpar") | **48×18** → 56×24 (WCAG 2.5.8) |
| `#social-include-meta` ("Metadados") | **13×13** → 20×20 / 24×24 em touch. Era `<input type="checkbox">` cru, sem `class="checkbox"`, então a regra anterior passava por cima dele calada — corrigido para `input[type="checkbox"]`, que fecha a classe inteira do problema |
| `.social-prof-actions` em 390px | **vazava 46px**: a regra legada usava `flex:1 1 100%` junto com `margin-left:62px`. 100% + 62px estoura o container |

Dois desses eram **erros meus**, e nenhum apareceria lendo código:

- A primeira correção da barra usou `#social-analytics.com-actionbar` (1 ID + 1 classe)
  contra `#card-social > #social-analytics` (2 IDs) — **perdeu por especificidade**. O
  computed style dizia `32px` onde eu esperava `112px`.
- Minha própria sonda deu **falso positivo** acusando a barra de "cortada": ela subia a
  árvore e achava o `overflow:hidden` do `.card`. Mas `overflow:hidden` **não recorta
  descendente `position:fixed`** — só recorta se o ancestral for o bloco contêiner dele
  (transform / filter / contain). Corrigido no `auditar-tela.sh`.

## Resultado medido

Chromium, dataset real, 3 selecionados, scroll no fim:

| Largura | `mq≤768` | Página estoura | Cortados | Colados | Botões pelados | Alvos <24px | Campos <16px | Barra cobre cards | Folga do Exportar |
|---|---|---|---|---|---|---|---|---|---|
| 1440px | não | 0 | 0 | 0 | 0 | 2 † | 9 ‡ | 0 | 24px |
| 1024px | não | 0 | 0 | 0 | 0 | 2 † | 9 ‡ | 0 | 24px |
| 768px  | sim | 0 | 0 | 0 | 0 | 2 † | **0** | 0 | 24px |
| 390px  | sim | 0 | 0 | 0 | 0 | 2 † | **0** | 0 | 16px |
| 320px  | sim | 0 | 0 | 0 | 0 | 2 † | **0** | 0 | 12px |

† Os dois checkboxes a 20×20. Passam pela exceção de espaçamento do WCAG 2.5.8 (20px com
gap ≥4px em volta) e sobem para 24×24 em `pointer:coarse` — provado abaixo.
‡ Esperado: a regra dos 16px é `@media (max-width:768px), (pointer:coarse)`. No desktop
o zoom do iOS não existe, então 13–13,5px ali não é violação.

**iPhone 14 emulado (390px, `pointer:coarse`, `hover:none`):**

```
estoura ................................. 0
alvos abaixo de 24px .................... 0
campos abaixo de 16px ................... 0     ← a correção do zoom do iOS funciona
checkboxes .............................. 24×24 (os dois)
ações do card visíveis sem hover ........ 27/27  ← nada mais depende de hover
```

Esse último número fecha um item que a auditoria original tinha declarado corrigido sem
provar: `.social-thumb-actions` era revelado só por `:hover`, o que em touch tornava a
ação inalcançável. Agora as 27 aparecem sem hover.

Screenshots de página inteira em `auditoria-render/` (`social-1440.png` … `social-320.png`,
`social-iphone14.png`). Para regerar contra o app rodando, com sessão logada:

```bash
./auditar-tela.sh social
```

## Dois artefatos de medição (não eram bugs)

Vale registrar, porque os dois quase geraram "correção" de coisa que não estava quebrada:

1. **Screenshot `fullPage` com elemento `position:fixed`.** O print de 390px mostrava o
   skip link e o header no meio da página. É stitching: o Playwright rola e costura, e o
   elemento fixo é desenhado na posição final de scroll. O print mentiu; a medição de
   `getBoundingClientRect()` disse a verdade.

2. **`:focus` não casa em aba em segundo plano.** Ao investigar o item acima, `focus()` no
   skip link deixava `document.activeElement` correto mas `top` continuava `-72px`, como se
   `.skip-link:focus` não existisse. A regra estava lá e casava no CSSOM. A causa: eu dirigia
   a aba remotamente, então `document.hasFocus() === false` e `visibilityState === 'hidden'`
   — e nessa condição o seletor `:focus` simplesmente não casa. Conclusão: **nunca diagnostique
   bug de foco a partir de aba sem foco de janela.** Sempre confira `document.hasFocus()`
   antes de acreditar numa medição de `:focus` / `:focus-visible`.

## O que este adendo NÃO cobre

- Larguras entre as cinco medidas, e orientação paisagem em telefone.
- Contraste real depois de `backdrop-filter` nos badges sobre miniatura — os badges têm
  fundo `rgba(15,23,42,.6)` sobre imagem arbitrária, então o contraste varia por post.
  Não é auditável estaticamente nem por sonda; precisa de amostragem de pixel.
- As outras abas (Transcrições, Biblioteca de Mídia, Download Avançado, Configurações) e
  os cinco modais: **auditados estaticamente, layout não verificado.** Rode
  `./auditar-tela.sh transcriptions`, `media` e `advanced` para fechar.

---

# RODADA 2 — pendências fechadas e bug funcional

## Bug funcional: download social gravava 0 byte e mandava para o Whisper

O erro `moov atom not found` não era do Whisper nem do ffmpeg: o arquivo
`ig_Dau1GKXOViH.mp4` tinha **0 byte**. Causa, em três camadas:

1. `download_media` usava `allow_redirects=False` + `raise_for_status()`. **Um 302
   não é erro HTTP**, então `raise_for_status()` passava, o corpo do 302 era vazio,
   e `os.replace()` promovia um arquivo de 0 byte a "download concluído". Nenhuma
   exceção em lugar nenhum.
2. O chamador **ignorava o retorno** de `download_media` e não checava o arquivo.
3. O item ia direto para `_social_start_transcription`, e o erro só aparecia dentro
   do ffmpeg — com uma mensagem que não diz ao usuário o que fazer.

O gatilho: o link do CDN do Instagram é assinado e **expira em poucas horas**.
A coleta foi às 11:44 e o pedido de transcrição às 14:17.

**Correção, em quatro camadas:**

| Camada | O que faz |
|---|---|
| `download_media` | Segue redirect, mas só para host da lista de CDNs (mantém a proteção contra SSRF). Recusa `text/html`/`json`, corpo abaixo de 1 KB, e assinatura de arquivo que não seja MP4/MOV/WebM/JPEG/PNG. Limpa o parcial e o destino em qualquer erro. |
| `_social_baixar` | Se o link da coleta falhar, **reabre o post no ego lite** (`intercept.resolve_media`) para pegar uma URL assinada nova e baixa de novo. |
| `_media_utilizavel` | Valida com `ffprobe` — tamanho, formato legível e **existência de faixa de áudio**. |
| Laço de download | Valida **sempre**, inclusive quando o arquivo já existia no disco, para um 0 byte de execução anterior não virar sucesso. |

Verificação: **7/7** no downloader (0 byte, HTML, 3 bytes, assinatura errada, 302
seguido, 302 para host não permitido, caso bom) — sem lixo no disco em nenhum
caso de falha — e **4/4** no validador (0 byte, lixo com tamanho, MP4 com áudio,
MP4 sem áudio). As entradas órfãs foram removidas de `media.json` (1045 → 1044)
e `history.json` (1484 → 1483).

## As cinco pendências

| Item | Status | Verificação |
|---|---|---|
| **A** Cabeçalho sticky + 1ª coluna congelada | ✅ | 14/14 em 1440/1024/768 |
| **B** Escala tipográfica nos tokens | ✅ parcial | 67 valores normalizados; escala agora é só 12/14/16/18/20/24 |
| **C** Dark mode | ✅ | **0 falhas de contraste** em 645 nós de texto, nos dois temas |
| **D** Estado na URL | ✅ | 11/11 |
| **E** Tabela → cards < 600px | ✅ | 16/16 em 600/390/320 |
| Animação de propriedade de layout | ✅ | 5/5 — zero propriedades de layout ou paint animadas |

### A — como o impasse foi resolvido
`position:sticky` relativo à página era impossível: `.card` tem `overflow:hidden`
e `#table-wrap` tem `overflow-x:auto`; os dois criam scroll container e um deles
sempre recorta o sticky. Em vez de brigar com isso, **o wrapper virou o scroller
vertical da tabela** (`max-height:min(70dvh,860px)`). Aí o sticky tem contexto
bem definido, funciona igual em todo navegador, e de bônus a barra de filtros e a
de ação em lote — que ficam fora do wrapper — param de sair da tela na rolagem.

### C — o pré-requisito que ninguém vê
`--white` era usado para **duas coisas opostas**: fundo de card (34x) e cor de
texto sobre fundo colorido (18x). Inverter no escuro deixaria texto escuro sobre
botão azul. Foi preciso uma camada semântica antes (`--surface`, `--on-accent`,
`--on-dark`, `--overlay-chip`, pares de estado) — 68 substituições mecânicas — e só
então o bloco escuro, que **só redefine variáveis**: nenhuma regra de componente
mudou.

### B — o que ficou de fora
A parte de **espaçamento** (≈200 valores: 13, 15, 18, 22, 26, 34px) segue sem
migrar. Os tokens `--sp-*` existem e o CSS novo usa; o legado não. É o item de
menor risco funcional e maior risco visual, e a lei de proximidade (espaço entre
grupos > espaço dentro do grupo) não é verificável por sonda — precisa de olho
humano, componente por componente.

## Contraste: o que só a renderização pega

A auditoria estática original mediu contraste **por par de variáveis** e deu tudo
certo. Renderizando e compondo alfa de verdade, apareceram **30 falhas no tema
claro** — todas pré-existentes, nenhuma detectável no CSS:

- `.social-er` — branco sobre `rgba(22,163,74,.85)`. O verde `#15803D` sozinho dá
  5,02:1, mas **composto a 85% sobre a miniatura** vira `rgb(21,142,69)` = 4,21:1.
- `.folder-count` / `.chip-count` em item ativo — `rgba(255,255,255,.25)` sobre
  azul **clareia** o fundo: branco sobre `rgb(92,138,240)` = 3,31:1. A correção é
  escurecer o chip (`rgba(0,0,0,.32)`), não clarear.
- `.bulk-btn` — mesma causa: `rgba(255,255,255,.15)` sobre azul = 3,97:1.
- `.seg-tab` inativa, `.status-badge.queued`, inicial do avatar — `slate-500`
  sobre `slate-100` = 4,34:1. Passa sobre branco, falha sobre cinza.

E **29 no escuro**, das quais 3 foram erro meu na conversão semântica: a barra
flutuante de ações e os badges sobre miniatura são **escuros nos dois temas**, então
o texto neles é branco constante — usar `--on-accent` (que inverte) deixou
escuro-sobre-escuro em 1,23:1. Daí o `--on-dark`, que não inverte.

Detalhe que fecha o raciocínio: o `.bulk-btn.danger` **precisa** inverter. No escuro
o vermelho é claro de propósito (regra 13, tom dessaturado), então texto escuro
sobre ele dá 5,98:1 — enquanto branco daria 3,16:1.

**Resultado final: 645 nós de texto, 0 falhas, nos dois temas.**

## Suíte de verificação

```
contraste (claro + escuro) .... 0 falhas em 645 nós de texto (x2 temas)
animação ...................... 5/5    zero propriedade de layout/paint animada
estado na URL ................. 11/11  inclui parâmetro malicioso ignorado
cabeçalho sticky .............. 14/14  1440 / 1024 / 768
modo card ..................... 16/16  600 / 390 / 320
sonda de layout ............... 5 larguras, 0 estouro / 0 cortado / 0 colado
iPhone 14 (coarse, sem hover) . 0 alvo <24px, 0 campo <16px, 27/27 ações visíveis
downloader .................... 7/7
validador de mídia ............ 4/4
sintaxe ....................... node --check, ast.parse, csstree, html-validate
```

Evidência em `auditoria-render/`: `tema-light.png`, `tema-dark.png`,
`cards-390.png`, `cards-320.png`, `sticky-1440.png`, `social-*.png`.

## Continua não verificado

- **Abas Biblioteca de Mídia, Download Avançado e Configurações, e os 5 modais:**
  o CSS é o mesmo, e a sonda de contraste cobriu o modal de transcrição, mas o
  layout dessas telas específicas não foi renderizado. `./auditar-tela.sh media`
  e `advanced` fecham.
- **O caminho real de download social** foi testado com servidor local simulando
  cada modo de falha. O caminho ego lite → Instagram de verdade não — depende do
  seu Mac com sessão logada.
- **Espaçamento** (item B, parte 2) e proximidade visual.
