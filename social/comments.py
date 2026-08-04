"""Coleta de comentários de um post.

Dois caminhos, na ordem em que valem a pena:

  1. Instagram → API v1 interna paginada via `browserFetch`, o MESMO recurso
     que o collector do IGSorter já usa para o feed do perfil
     (/api/v1/media/<pk>/comments/, paginando por next_min_id, mais
     child_comments/ para as respostas). Traz os comentários completos, com
     os campos crus, sem depender de rolagem nem do DOM.
  2. Demais redes (e plano B do Instagram) → interceptação: hook em fetch/XHR
     instalado ANTES dos scripts da página, rolando o painel e clicando em
     "carregar mais" / "ver respostas".

Cobre Instagram, TikTok e YouTube (Facebook cai no extrator genérico).
"""
import csv
import datetime as dt
import json
import os
import re

from .core import DATA_DIR, EXPORT_DIR
from .collector import ego_available, shortcode_to_id
from .intercept import _run_ego, _num

# Rotas que carregam comentários em cada rede. Vazio = captura tudo que parecer
# JSON de comentário (fallback para redes não mapeadas).
COMMENT_PATTERNS = {
    "instagram": ["/graphql/query", "/api/graphql", "/api/v1/media/", "/comments/"],
    "tiktok": ["/api/comment/list", "/api/comment/reply"],
    "youtube": ["/youtubei/v1/next"],
    "facebook": ["/api/graphql/"],
}

NODE_TEMPLATE = r"""
const TARGET_URL   = '__TARGET_URL__'
const MAX_COMMENTS = __MAX_COMMENTS__
const MAX_ROUNDS   = __MAX_ROUNDS__
const OUT_FILE     = '__OUT_FILE__'
const PATTERNS     = __PATTERNS__

const task = await useOrCreateTaskSpace('igsorter comments')
await openOrReuseTab(TARGET_URL, { wait: true, timeout: 30 })

const HOOK = `
(() => {
  if (window.__igcHook) return;
  window.__igcHook = true;
  window.__igcCap = [];
  const PAT = ${JSON.stringify(PATTERNS)};
  const hit = (u) => { u = String(u || ''); return PAT.length === 0 || PAT.some(p => u.indexOf(p) !== -1) };
  const push = (url, body) => {
    try { if (body && body.length < 12000000) window.__igcCap.push({ url: url, body: body }) } catch (e) {}
  };
  const _f = window.fetch;
  window.fetch = function () {
    const args = arguments;
    const p = _f.apply(this, args);
    try {
      const a0 = args[0];
      const u = typeof a0 === 'string' ? a0 : (a0 && a0.url) || '';
      if (hit(u)) p.then(function (r) {
        r.clone().text().then(function (t) { push(u, t) }).catch(function () {});
      }).catch(function () {});
    } catch (e) {}
    return p;
  };
  const _o = XMLHttpRequest.prototype.open, _s = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__igcU = u; return _o.apply(this, arguments) };
  XMLHttpRequest.prototype.send = function () {
    try {
      this.addEventListener('load', function () {
        try {
          if (hit(this.__igcU) && (this.responseType === '' || this.responseType === 'text'))
            push(this.__igcU, this.responseText);
        } catch (e) {}
      });
    } catch (e) {}
    return _s.apply(this, arguments);
  };
})()
`
await cdp('Page.addScriptToEvaluateOnNewDocument', { source: HOOK })
await gotoAndWait(TARGET_URL, { timeout: 30 })
await wait(4)

// Conta comentários no JSON só para saber quando parar de rolar.
function countComments(body) {
  let n = 0
  try {
    const j = JSON.parse(body)
    const stack = [j]
    let guard = 0
    while (stack.length && guard++ < 40000) {
      const cur = stack.pop()
      if (!cur || typeof cur !== 'object') continue
      if (typeof cur.text === 'string' && (cur.user || cur.owner)) n++
      if (cur.commentEntityPayload) n++
      for (const k in cur) { const v = cur[k]; if (v && typeof v === 'object') stack.push(v) }
    }
  } catch (e) {
    // NDJSON (Facebook): tenta linha a linha
    for (const line of String(body).split('\n')) {
      try { const j = JSON.parse(line); if (j) n += 0 } catch (e2) {}
    }
  }
  return n
}

const captured = []
let total = 0, idle = 0

async function drain() {
  const batch = await js(String.raw`(() => { const c = window.__igcCap || []; window.__igcCap = []; return c })()`)
  if (!batch || !batch.length) return 0
  let n = 0
  for (const b of batch) { captured.push(b); n += countComments(b.body) }
  return n
}

// Um "round" = clicar nos botões de carregar mais + rolar tudo que rola.
// O painel de comentários do Instagram é um div com overflow próprio, então
// rolar só a janela não pagina nada.
const STEP = String.raw`(() => {
  let clicks = 0;
  const RX = /mais coment|more comment|carregar mais|load more|ver respostas|view replies|ver mais respostas|mostrar mais/i;
  const cand = document.querySelectorAll('button, [role="button"], span[role="button"], a[role="button"]');
  for (const b of cand) {
    const t = (b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '');
    if (RX.test(t)) { try { b.click(); clicks++ } catch (e) {} }
    if (clicks >= 12) break;
  }
  // rola todo elemento com barra de rolagem própria + a janela
  let scrolled = 0;
  for (const el of document.querySelectorAll('div, ul, section')) {
    if (el.scrollHeight > el.clientHeight + 120 && el.clientHeight > 150) {
      el.scrollTop = el.scrollHeight; scrolled++;
      if (scrolled >= 6) break;
    }
  }
  window.scrollTo(0, document.body.scrollHeight);
  return { clicks: clicks, scrolled: scrolled };
})()`

total += await drain()
cliLog('PROGRESS ' + total)

for (let i = 0; i < MAX_ROUNDS && total < MAX_COMMENTS; i++) {
  await js(STEP)
  await wait(2.0)
  const got = await drain()
  total += got
  cliLog('PROGRESS ' + total)
  if (got === 0) { idle++; if (idle >= 5) { cliLog('SEM_NOVOS_COMENTARIOS'); break } }
  else idle = 0
}

const loggedIn = await js(String.raw`(() => !!document.cookie.match(/sessionid=|c_user=|SID=|sessionid_ss=/))()`)
const payload = { collected_at: new Date().toISOString(), url: TARGET_URL,
                  logged_in: loggedIn, captured: captured }
const fs = await import('fs')
fs.writeFileSync(OUT_FILE, JSON.stringify(payload))
cliLog('SAVED:' + OUT_FILE + ' | respostas: ' + captured.length)
await completeTaskSpace(task.id, { keep: false })
"""


# ── Instagram: API v1 interna, rodando DENTRO da página do post ──────────────
# Receita idêntica à do ESUIT Comments Exporter (extensão instalada no Chrome):
#
#   1º nível  GET /api/v1/media/{id}/comments/
#             ?can_support_threading=true&sort_order=popular&min_id=<cursor>
#             → data.comments, próxima página em next_min_id enquanto
#               has_more_headload_comments for true
#   respostas GET /api/v1/media/{mediaId}/comments/{parentCommentId}/child_comments/
#             ?min_id=<cursor>
#             → data.child_comments, cursor next_min_child_cursor enquanto
#               has_more_head_child_comments for true
#
# O pulo do gato da extensão é NÃO montar o request: ela pega o cliente de API
# do próprio Instagram (`window.require('PolarisInstapi').apiGet`), que já
# assina tudo (CSRF, X-IG-App-ID, asbd-id, claim). Se o módulo não existir,
# caímos no fetch manual com o app-id público. O ritmo também é o dela: espera
# aleatória que cresce 50 ms por chamada e zera quando passa de 5 s.
NODE_IG_API = r"""
const PK  = '__PK__'
const MAX = __MAX_COMMENTS__
const POST_URL = '__POST_URL__'
const OUT_FILE = '__OUT_FILE__'

const task = await useOrCreateTaskSpace('social comentarios')
await openOrReuseTab(POST_URL, { wait: true, timeout: 30 })
await wait(3)

// `sessionid` é HttpOnly — document.cookie NUNCA a enxerga, então checar por ela
// dá falso negativo. O sinal visível de sessão é ds_user_id; a palavra final,
// porém, é a própria resposta da API (deslogado ela devolve HTML de login).
const loggedIn = await js(String.raw`(() => /\bds_user_id=/.test(document.cookie))()`)
const out = { logged_in: loggedIn, comments: [], error: null, mode: null }

{
  // Dispara o coletor na página e acompanha por polling — js() é Runtime.evaluate
  // e não espera promise, então quem guarda o estado é a própria página.
  const started = await js(String.raw`(() => {
    if (window.__igc && window.__igc.running) return 'already';
    const PK = '__PK__', MAX = __MAX_COMMENTS__;
    const st = { done: false, running: true, error: null, comments: [], mode: null };
    window.__igc = st;

    let inst = null;
    try {
      const m = window.require && window.require('PolarisInstapi');
      if (m && typeof m.apiGet === 'function') inst = m;
    } catch (e) {}
    st.mode = inst ? 'PolarisInstapi' : 'fetch';

    async function get(tpl, pathVars, query) {
      if (inst) {
        const r = await inst.apiGet(tpl, { query: query, path: pathVars });
        return (r && r.data) || r;
      }
      let p = tpl;
      for (const k in pathVars) p = p.replace('{' + k + '}', encodeURIComponent(pathVars[k]));
      const qs = Object.keys(query)
        .filter(k => query[k] !== '' && query[k] !== null && query[k] !== undefined)
        .map(k => encodeURIComponent(k) + '=' + encodeURIComponent(query[k])).join('&');
      const r = await fetch('https://www.instagram.com' + p + (qs ? '?' + qs : ''), {
        credentials: 'include',
        headers: { 'x-ig-app-id': '936619743392459', 'x-requested-with': 'XMLHttpRequest' } });
      if (r.status === 401 || r.status === 403) throw new Error('NOT_LOGGED_IN');
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const t = await r.text();
      // Deslogado o Instagram responde a página de login (HTML), não JSON.
      if (t.trim().charAt(0) === '<') throw new Error('NOT_LOGGED_IN');
      return JSON.parse(t);
    }

    let attempts = 0;
    function throttle() {
      attempts += 1;
      const ms = Math.ceil(Math.random() * (50 * attempts) + 500);
      if (ms < 1000) return Promise.resolve();
      if (ms > 5000) { attempts = 0; return Promise.resolve(); }
      return new Promise(r => setTimeout(r, ms));
    }

    async function replies(parent) {
      let cursor = '', guard = 0;
      while (guard++ < 60 && st.comments.length < MAX) {
        await throttle();
        const j = await get('/api/v1/media/{mediaId}/comments/{parentCommentId}/child_comments/',
                            { mediaId: PK, parentCommentId: parent.pk }, { min_id: cursor });
        const kids = (j && j.child_comments) || [];
        if (!kids.length) break;
        for (const k of kids) { k.__is_reply = true; k.__parent = parent.pk; st.comments.push(k) }
        if (!j.has_more_head_child_comments || !j.next_min_child_cursor) break;
        cursor = j.next_min_child_cursor;
      }
    }

    (async () => {
      try {
        let cursor = '', guard = 0;
        while (guard++ < 300 && st.comments.length < MAX) {
          await throttle();
          const j = await get('/api/v1/media/{id}/comments/', { id: PK },
                              { can_support_threading: true, min_id: cursor, sort_order: 'popular' });
          if (j && (j.message === 'login_required' || j.require_login)) { st.error = 'NOT_LOGGED_IN'; break }
          const batch = (j && j.comments) || [];
          if (!batch.length) break;
          st.comments.push(...batch);
          for (const c of batch) {
            if (st.comments.length >= MAX) break;
            if ((c.child_comment_count || 0) > 0) await replies(c);
          }
          if (!j.has_more_headload_comments || !j.next_min_id) break;
          cursor = j.next_min_id;
        }
      } catch (e) { st.error = String((e && e.message) || e) }
      st.done = true; st.running = false;
    })();
    return 'started';
  })()`)
  cliLog('coletor: ' + started)

  let last = -1
  for (let i = 0; i < 900; i++) {
    const s = await js(String.raw`(() => ({ done: window.__igc.done, n: window.__igc.comments.length,
                                            error: window.__igc.error, mode: window.__igc.mode }))()`)
    out.mode = s.mode
    if (s.n !== last) { cliLog('PROGRESS ' + s.n); last = s.n }
    if (s.done) { out.error = s.error; break }
    await wait(1.5)
  }

  // lê em fatias: devolver milhares de objetos de uma vez estoura o payload do CDP
  let off = 0
  while (true) {
    const chunk = await js('(() => window.__igc.comments.slice(' + off + ', ' + (off + 150) + '))()')
    if (!chunk || !chunk.length) break
    out.comments.push(...chunk)
    off += chunk.length
  }
  await js(String.raw`(() => { try { delete window.__igc } catch (e) {} return 1 })()`)
}

const fs = await import('fs')
fs.writeFileSync(OUT_FILE, JSON.stringify(out))
cliLog('SAVED:' + OUT_FILE + ' | comentarios: ' + out.comments.length + ' | via ' + out.mode)
await completeTaskSpace(task.id, { keep: false })
"""


def _ig_pk(row):
    """PK numérico da mídia, a partir do shortcode da URL (base64 do Instagram)."""
    code = row.get("code") or ""
    m = re.search(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
                  row.get("url") or "", re.I)
    if m:
        code = m.group(1)
    if not code or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
        return None
    try:
        return str(shortcode_to_id(code)), code
    except ValueError:
        return None


def _capture_ig_api(row, max_comments, on_progress=None):
    """Devolve a lista crua de comentários da API v1, ou levanta RuntimeError."""
    ident = _ig_pk(row)
    if not ident:
        raise RuntimeError("não consegui identificar o post do Instagram pela URL")
    pk, code = ident
    tmp = os.path.join(DATA_DIR, f".igcomments_{os.getpid()}_{pk[-8:]}.json")
    script = (NODE_IG_API.replace("__PK__", pk)
              .replace("__MAX_COMMENTS__", str(int(max_comments)))
              .replace("__POST_URL__", f"https://www.instagram.com/p/{code}/")
              .replace("__OUT_FILE__", tmp))
    output = _run_ego(script, on_progress)
    if not os.path.isfile(tmp):
        tail = "\n".join(output.splitlines()[-10:])
        raise RuntimeError("a API de comentários não respondeu:\n" + tail)
    with open(tmp, encoding="utf-8") as f:
        data = json.load(f)
    os.remove(tmp)
    err = data.get("error") or ""
    if "NOT_LOGGED_IN" in err or "LOGIN_REQUIRED" in err or (
            not data.get("comments") and not data.get("logged_in")):
        raise RuntimeError("sessão do Instagram ausente no ego lite — abra o ego lite, "
                           "entre na sua conta do Instagram e tente de novo")
    if err:
        raise RuntimeError(err)
    return data.get("comments") or []


def _capture(url, platform, max_comments, max_rounds, on_progress=None):
    if not ego_available():
        raise RuntimeError("comando 'ego-browser' não encontrado. Instale o ego lite "
                           "(https://lite.ego.app) e abra o app uma vez.")
    tmp = os.path.join(DATA_DIR, f".comments_{os.getpid()}_{abs(hash(url)) % 10 ** 6}.json")
    script = (NODE_TEMPLATE
              .replace("__TARGET_URL__", url)
              .replace("__MAX_COMMENTS__", str(int(max_comments)))
              .replace("__MAX_ROUNDS__", str(int(max_rounds)))
              .replace("__OUT_FILE__", tmp)
              .replace("__PATTERNS__", json.dumps(COMMENT_PATTERNS.get(platform, []))))
    output = _run_ego(script, on_progress)
    if not os.path.isfile(tmp):
        tail = "\n".join(output.splitlines()[-15:])
        raise RuntimeError("não consegui abrir o post para ler os comentários:\n" + tail)
    with open(tmp, encoding="utf-8") as f:
        data = json.load(f)
    os.remove(tmp)
    return data


# ─── Normalização ────────────────────────────────────────────────────────────

def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def _comment(**kw):
    return {"comment_id": str(kw.get("comment_id") or ""),
            "author": kw.get("author") or "",
            "author_name": kw.get("author_name") or "",
            "author_id": str(kw.get("author_id") or ""),
            "author_url": (f"https://www.instagram.com/{kw['author']}/"
                           if kw.get("author") and kw.get("platform") == "instagram" else ""),
            "avatar": kw.get("avatar") or "",
            "text": (kw.get("text") or "").strip(),
            "likes": kw.get("likes") if kw.get("likes") is not None else 0,
            "created_at": kw.get("created_at"),
            "replies": kw.get("replies") or 0,
            "is_reply": bool(kw.get("is_reply")),
            "parent_id": str(kw.get("parent_id") or ""),
            "depth": 1 if kw.get("is_reply") else 0}


def _from_generic(node, is_reply=False):
    """Instagram (v1 e GraphQL), TikTok e afins: objeto com `text` + autor."""
    if not isinstance(node, dict) or not isinstance(node.get("text"), str):
        return None
    user = node.get("user") or node.get("owner") or {}
    if not isinstance(user, dict):
        return None
    author = (user.get("username") or user.get("unique_id")
              or user.get("uniqueId") or "")
    if not author and not user.get("nickname"):
        return None
    # Comentário que é só um GIF vem com texto vazio e a imagem no giphy_media_info
    # — a extensão anexa a URL ao texto para a linha não sair em branco.
    text = node["text"]
    giphy = (((node.get("giphy_media_info") or {}).get("images") or {})
             .get("fixed_height") or {}).get("url")
    if giphy:
        text = (text + "\n" + giphy).strip() if text.strip() else giphy
    likes = node.get("comment_like_count")
    if likes is None:
        likes = node.get("digg_count")
    if likes is None:
        liked = node.get("edge_liked_by") or node.get("edge_media_preview_like")
        likes = liked.get("count") if isinstance(liked, dict) else None
    replies = (node.get("child_comment_count") or node.get("reply_comment_total")
               or ((node.get("edge_threaded_comments") or {}).get("count")
                   if isinstance(node.get("edge_threaded_comments"), dict) else 0))
    return _comment(
        comment_id=node.get("pk") or node.get("id") or node.get("cid") or "",
        author=author, author_name=user.get("full_name") or user.get("nickname") or "",
        author_id=user.get("pk") or user.get("id") or user.get("uid") or "",
        avatar=user.get("profile_pic_url") or user.get("avatar_thumb") or "",
        platform="instagram" if user.get("username") else "",
        text=text, likes=likes,
        created_at=_iso(node.get("created_at") or node.get("created_at_utc")
                        or node.get("create_time")),
        replies=replies, is_reply=is_reply or bool(node.get("__is_reply")),
        parent_id=node.get("__parent") or node.get("parent_comment_id") or "")


def _from_youtube(payload):
    """YouTube: commentEntityPayload (schema novo do /youtubei/v1/next)."""
    props = payload.get("properties") or {}
    author = payload.get("author") or {}
    toolbar = payload.get("toolbar") or {}
    content = (props.get("content") or {}).get("content")
    if not content:
        return None
    return _comment(
        comment_id=props.get("commentId") or "",
        author=(author.get("displayName") or "").lstrip("@"),
        author_name=author.get("displayName") or "",
        text=content, likes=_num(toolbar.get("likeCountNotliked")) or 0,
        created_at=None, replies=_num(toolbar.get("replyCount")) or 0,
        is_reply=bool(props.get("replyLevel")))


def _walk_objs(obj, limit=400000):
    stack, guard = [obj], 0
    while stack and guard < limit:
        guard += 1
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def parse(captured, platform):
    """Extrai comentários das respostas capturadas, sem duplicar."""
    out, seen = [], set()

    def add(c):
        if not c or not c["text"]:
            return
        key = c["comment_id"] or (c["author"] + "|" + c["text"][:80])
        if key in seen:
            return
        seen.add(key)
        out.append(c)

    for cap in captured:
        bodies = [cap.get("body") or ""]
        if platform == "facebook":                     # FB responde em NDJSON
            bodies = (cap.get("body") or "").splitlines()
        for body in bodies:
            try:
                j = json.loads(body)
            except (ValueError, TypeError):
                continue
            for node in _walk_objs(j):
                if "commentEntityPayload" in node and isinstance(node["commentEntityPayload"], dict):
                    add(_from_youtube(node["commentEntityPayload"]))
                    continue
                add(_from_generic(node))
                # respostas aninhadas (Instagram: preview_child_comments / edge_threaded_comments)
                for key in ("preview_child_comments", "child_comments"):
                    for child in (node.get(key) or []) if isinstance(node.get(key), list) else []:
                        add(_from_generic(child, is_reply=True))
                thr = node.get("edge_threaded_comments")
                if isinstance(thr, dict):
                    for e in thr.get("edges") or []:
                        if isinstance(e, dict):
                            add(_from_generic(e.get("node") or e, is_reply=True))
    return out


# ─── API do módulo ───────────────────────────────────────────────────────────

CSV_COLS = [("post_code", "post_codigo"), ("post_url", "post_url"),
            ("comment_id", "comentario_id"), ("author", "autor"),
            ("author_name", "autor_nome"), ("author_id", "autor_id"),
            ("author_url", "autor_url"), ("avatar", "autor_avatar"),
            ("text", "comentario"), ("likes", "curtidas"),
            ("created_at", "data"), ("replies", "respostas"),
            ("depth", "nivel"), ("parent_id", "responde_a"),
            ("is_reply", "e_resposta")]


def sort_tree(comments):
    """Ordena por data e coloca cada resposta logo abaixo do comentário pai.

    Mesmo arranjo do exportador da extensão: a planilha lê como uma conversa,
    não como duas listas soltas (pais primeiro, respostas no fim).
    """
    ordered = sorted(comments, key=lambda c: c.get("created_at") or "")
    by_parent = {}
    roots = []
    ids = {c["comment_id"] for c in ordered if c.get("comment_id")}
    for c in ordered:
        pid = c.get("parent_id")
        if pid and pid in ids:
            by_parent.setdefault(pid, []).append(c)
        else:
            roots.append(c)
    out = []
    for r in roots:
        out.append(r)
        out.extend(by_parent.get(r["comment_id"], []))
    return out


def _platform_key(row):
    p = (row.get("platform") or "Instagram").lower()
    return p if p in COMMENT_PATTERNS else "instagram"


def _collect_one(row, max_comments, max_rounds, on_progress=None, log=None):
    """Comentários de UM post: API v1 no Instagram, interceptação no resto.

    Se a API falhar (rota mudou, post de outra conta, sessão sem permissão), cai
    para a interceptação em vez de devolver erro — os dois caminhos entregam o
    mesmo formato normalizado.
    """
    plat = _platform_key(row)
    if plat == "instagram":
        try:
            raw = _capture_ig_api(row, max_comments, on_progress)
            found, seen = [], set()
            for node in raw:
                c = _from_generic(node, is_reply=bool(node.get("__is_reply")))
                if c and c["text"] and c["comment_id"] not in seen:
                    seen.add(c["comment_id"])
                    found.append(c)
            if found:
                return sort_tree(found)
            if log:
                log("API não trouxe comentários; tentando pela página…")
        except RuntimeError as e:
            if log:
                log(f"API de comentários indisponível ({e}); tentando pela página…")
    data = _capture(row["url"], plat, max_comments, max_rounds, on_progress)
    return parse(data.get("captured") or [], plat)


def collect_for_posts(posts, max_comments=300, max_rounds=40,
                      on_progress=None, on_post=None, log=None):
    """Coleta comentários de vários posts e salva um CSV + JSON em EXPORT_DIR.

    `posts` é uma lista de linhas do dataset (precisa de `url`; usa `code` e
    `platform` quando existem).
    """
    all_rows, falhas = [], []
    for i, row in enumerate(posts, 1):
        url = row.get("url")
        code = row.get("code") or ""
        if not url:
            falhas.append({"code": code, "error": "post sem URL"})
            continue
        if on_post:
            on_post(i, len(posts), code)
        try:
            found = _collect_one(row, max_comments, max_rounds, on_progress, log)
            if not found:
                falhas.append({"code": code, "error":
                               "nenhum comentário lido (post sem comentários, "
                               "privado, ou sessão deslogada no ego lite)"})
                continue
            for c in found[:int(max_comments)]:
                all_rows.append({"post_code": code, "post_url": url, **c})
        except Exception as e:                          # noqa: BLE001 — vira relatório
            falhas.append({"code": code, "error": str(e)})

    if not all_rows:
        detalhe = falhas[0]["error"] if falhas else "nenhum comentário encontrado"
        raise RuntimeError("Não consegui coletar comentários: " + detalhe)

    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_code = re.sub(r"[^-\w.]", "_", posts[0].get("code") or "post")[:60]
    base = (f"comentarios_{safe_code}" if len(posts) == 1
            else f"comentarios_{len(posts)}posts")
    name = f"{base}_{stamp}"
    csv_path = os.path.join(EXPORT_DIR, name + ".csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([c[1] for c in CSV_COLS])
        for r in all_rows:
            w.writerow([r.get(c[0], "") for c in CSV_COLS])
    json_path = os.path.join(EXPORT_DIR, name + ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"collected_at": dt.datetime.now().isoformat(),
                   "posts": len(posts), "count": len(all_rows),
                   "failures": falhas, "comments": all_rows},
                  f, ensure_ascii=False, indent=1)

    return {"csv": os.path.basename(csv_path), "json": os.path.basename(json_path),
            "count": len(all_rows), "posts": len(posts), "failures": falhas}
