"""Coleta por interceptação — a mesma técnica das extensões (Sort Feed).

Em vez de chamar a API da rede (que exige doc_id/tokens que a Meta rotaciona e
dispara rate limit), instala um hook em fetch/XHR ANTES da página carregar e lê
as respostas que a própria rede busca enquanto rolamos o feed. A paginação é a
do site: o tráfego é o de um humano navegando, então não há request nosso para
ser bloqueado nem token para expirar.

Cobre Instagram, TikTok, YouTube e Facebook com a sessão logada do ego lite.
"""
import datetime as dt
import json
import os
import re
import subprocess

from .core import DATA_DIR
from .collector import ego_available

# Padrões de URL que carregam o feed de cada rede
PLATFORMS = {
    "instagram": {
        "name": "Instagram",
        "patterns": ["/graphql/query", "/api/graphql", "/api/v1/feed/"],
        "profile_url": "https://www.instagram.com/{user}/",
    },
    "tiktok": {
        "name": "TikTok",
        "patterns": ["/api/post/item_list", "/api/search/item", "/api/user/detail"],
        "profile_url": "https://www.tiktok.com/@{user}",
    },
    "youtube": {
        "name": "YouTube",
        "patterns": ["/youtubei/v1/browse", "/youtubei/v1/next"],
        "profile_url": "https://www.youtube.com/@{user}/videos",
    },
    "facebook": {
        "name": "Facebook",
        "patterns": ["/api/graphql/"],
        "profile_url": "https://www.facebook.com/{user}/videos",
    },
}

NODE_TEMPLATE = r"""
const TARGET_URL = __TARGET_URL__
const MAX_ITEMS  = __MAX_ITEMS__
const MAX_SCROLLS= __MAX_SCROLLS__
const OUT_FILE   = '__OUT_FILE__'
const PATTERNS   = __PATTERNS__

const task = await useOrCreateTaskSpace('igsorter intercept')
cliLog('task space id: ' + task.id)

await openOrReuseTab(TARGET_URL, { wait: true, timeout: 30 })

// Hook instalado via CDP para rodar ANTES de qualquer script da página —
// equivale ao run_at:"document_start" de um content script de extensão.
const HOOK = `
(() => {
  if (window.__igsHook) return;
  window.__igsHook = true;
  window.__igsCap = [];
  const PAT = ${JSON.stringify(PATTERNS)};
  const hit = (u) => { u = String(u || ''); return PAT.some(p => u.indexOf(p) !== -1) };
  const push = (url, body) => {
    try { if (body && body.length < 12000000) window.__igsCap.push({ url: url, body: body }) } catch (e) {}
  };
  const _f = window.fetch;
  window.fetch = function () {
    const args = arguments;
    const p = _f.apply(this, args);
    try {
      const a0 = args[0];
      const u = typeof a0 === 'string' ? a0 : (a0 && a0.url) || '';
      if (hit(u)) p.then(function (r) {
        // clone() é o truque: lê o corpo sem consumir a resposta original
        r.clone().text().then(function (t) { push(u, t) }).catch(function () {});
      }).catch(function () {});
    } catch (e) {}
    return p;
  };
  const _o = XMLHttpRequest.prototype.open, _s = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__igsU = u; return _o.apply(this, arguments) };
  XMLHttpRequest.prototype.send = function () {
    try {
      this.addEventListener('load', function () {
        try {
          if (hit(this.__igsU) && (this.responseType === '' || this.responseType === 'text'))
            push(this.__igsU, this.responseText);
        } catch (e) {}
      });
    } catch (e) {}
    return _s.apply(this, arguments);
  };
})()
`
await cdp('Page.addScriptToEvaluateOnNewDocument', { source: HOOK })

// recarrega para o hook capturar já a primeira página do feed
await gotoAndWait(TARGET_URL, { timeout: 30 })
await wait(3)

// Conta itens varrendo o JSON inteiro: a conexão fica em profundidades
// diferentes conforme a rota/rede. Serve só para saber quando parar de rolar.
function countItems(body) {
  try {
    const j = JSON.parse(body)
    let n = 0, guard = 0
    const stack = [j]
    while (stack.length && guard++ < 30000) {
      const cur = stack.pop()
      if (!cur || typeof cur !== 'object') continue
      if (Array.isArray(cur.edges)) n += cur.edges.length
      if (Array.isArray(cur.itemList)) n += cur.itemList.length
      if (Array.isArray(cur.aweme_list)) n += cur.aweme_list.length
      if (cur.videoId) n += 1
      for (const k in cur) { const v = cur[k]; if (v && typeof v === 'object') stack.push(v) }
    }
    if (!n && Array.isArray(j.items)) n = j.items.length
    return n
  } catch (e) { return 0 }
}

const captured = []
let total = 0, idle = 0

async function drain() {
  const batch = await js(String.raw`(() => { const c = window.__igsCap || []; window.__igsCap = []; return c })()`)
  if (!batch || !batch.length) return 0
  let n = 0
  for (const b of batch) { captured.push(b); n += countItems(b.body) }
  return n
}

total += await drain()
cliLog('PROGRESS ' + total)

for (let i = 0; i < MAX_SCROLLS && total < MAX_ITEMS; i++) {
  // rolar até o fim do documento: em scroll profundo o scrollIntoView({block:'center'})
  // rola para CIMA e nunca dispara a próxima página
  await js(String.raw`window.scrollTo(0, document.body.scrollHeight)`)
  await wait(2.2)
  const got = await drain()
  total += got
  cliLog('PROGRESS ' + total)
  if (got === 0) { idle++; if (idle >= 4) { cliLog('SEM_NOVOS_ITENS'); break } }
  else idle = 0
}

const loggedIn = await js(String.raw`(() => !!document.cookie.match(/sessionid=|c_user=|SID=|sessionid_ss=/))()`)
const payload = { collected_at: new Date().toISOString(), url: TARGET_URL,
                  logged_in: loggedIn, captured: captured }
const fs = await import('fs')
fs.writeFileSync(OUT_FILE, JSON.stringify(payload))
cliLog('SAVED:' + OUT_FILE + ' | respostas: ' + captured.length + ' | itens: ' + total)
await completeTaskSpace(task.id, { keep: false })
"""


def _run_ego(script, on_progress=None, timeout=1800):
    proc = subprocess.Popen(["ego-browser", "nodejs"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    lines = []
    try:
        proc.stdin.write(script)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            m = re.search(r"PROGRESS (\d+)", line)
            if on_progress:
                if m:
                    on_progress(int(m.group(1)))
                elif line.strip():
                    on_progress(None, line[:200])
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("interceptação excedeu o tempo limite")
    return "\n".join(lines)


def capture(platform, target, max_items=100, max_scrolls=40, on_progress=None):
    """Roda o navegador, intercepta o feed e devolve as respostas cruas."""
    if platform not in PLATFORMS:
        raise RuntimeError(f"plataforma não suportada: {platform}")
    if not ego_available():
        raise RuntimeError("comando 'ego-browser' não encontrado. Instale o ego lite "
                           "(https://lite.ego.app) e abra o app uma vez.")
    cfg = PLATFORMS[platform]
    url = target if target.startswith("http") else \
        cfg["profile_url"].format(user=target.strip().lstrip("@"))

    tmp = os.path.join(DATA_DIR, f".intercept_{platform}_{os.getpid()}.json")
    script = (NODE_TEMPLATE
              .replace("__TARGET_URL__", json.dumps(url))
              .replace("__MAX_ITEMS__", str(int(max_items)))
              .replace("__MAX_SCROLLS__", str(int(max_scrolls)))
              .replace("__OUT_FILE__", tmp)
              .replace("__PATTERNS__", json.dumps(cfg["patterns"])))
    output = _run_ego(script, on_progress)

    if not os.path.isfile(tmp):
        tail = "\n".join(output.splitlines()[-15:])
        raise RuntimeError("interceptação não retornou dados:\n" + tail)
    with open(tmp, encoding="utf-8") as f:
        data = json.load(f)
    os.remove(tmp)
    return data, url


RESOLVE_TEMPLATE = r"""
const TARGET_URL = __TARGET_URL__
const OUT_FILE   = '__OUT_FILE__'

const task = await useOrCreateTaskSpace('igsorter resolve')
await openOrReuseTab(TARGET_URL, { wait: true, timeout: 30 })

// mesmo hook da coleta: guarda as respostas de API que contêm as URLs de mídia
const HOOK = `
(() => {
  if (window.__igsR) return; window.__igsR = true; window.__igsB = [];
  const keep = (t) => { try { if (t && t.length < 8000000 && t.indexOf('video_versions') !== -1) window.__igsB.push(t) } catch (e) {} };
  const _f = window.fetch;
  window.fetch = function () {
    const p = _f.apply(this, arguments);
    try { p.then(r => { r.clone().text().then(keep).catch(() => {}) }).catch(() => {}) } catch (e) {}
    return p;
  };
  const _o = XMLHttpRequest.prototype.open, _s = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u) { this.__u = u; return _o.apply(this, arguments) };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', function () {
      try { if (this.responseType === '' || this.responseType === 'text') keep(this.responseText) } catch (e) {}
    });
    return _s.apply(this, arguments);
  };
})()
`
await cdp('Page.addScriptToEvaluateOnNewDocument', { source: HOOK })
await gotoAndWait(TARGET_URL, { timeout: 30 })
await wait(4)

const found = await js(String.raw`(() => {
  const out = { video: null, image: null, title: document.title || '' };
  // 1) elemento <video> já renderizado
  const v = document.querySelector('video[src^="http"], video source[src^="http"]');
  if (v) out.video = v.src || v.getAttribute('src');
  // 2) metatags og:video / og:image (funcionam deslogado)
  const og = (p) => document.querySelector('meta[property="' + p + '"]')?.content || null;
  out.video = out.video || og('og:video') || og('og:video:secure_url');
  out.image = og('og:image');
  // 3) respostas de API capturadas (melhor qualidade, URL assinada do CDN)
  for (const body of (window.__igsB || [])) {
    const m = body.match(/"video_versions":\s*\[\s*\{[^}]*?"url":\s*"([^"]+)"/);
    if (m) { out.video = m[1].replace(/\\u0026/g, '&').replace(/\\\//g, '/'); break }
  }
  return out;
})()`)

const fs = await import('fs')
fs.writeFileSync(OUT_FILE, JSON.stringify(found))
cliLog('RESOLVED:' + JSON.stringify({ video: !!found.video, image: !!found.image }))
await completeTaskSpace(task.id, { keep: false })
"""


def resolve_media(url):
    """Descobre a URL direta da mídia abrindo a página no navegador logado.

    As URLs de CDN vêm assinadas, então o download em si roda no Python sem
    precisar de cookies — é assim que o downloader do Instagram já funciona.
    """
    if not ego_available():
        raise RuntimeError("ego lite não encontrado para resolver a mídia")
    tmp = os.path.join(DATA_DIR, f".resolve_{os.getpid()}.json")
    script = (RESOLVE_TEMPLATE.replace("__TARGET_URL__", json.dumps(url))
                              .replace("__OUT_FILE__", tmp))
    _run_ego(script)
    if not os.path.isfile(tmp):
        raise RuntimeError("não consegui abrir a página para achar a mídia")
    with open(tmp, encoding="utf-8") as f:
        found = json.load(f)
    os.remove(tmp)
    if not found.get("video") and not found.get("image"):
        raise RuntimeError("nenhuma mídia encontrada na página (privada ou removida)")
    return found


# ─── Parsers por rede ────────────────────────────────────────────────────────

def _num(x):
    """Aceita 1234, '1.234', '1.2K views', '1,2 mi de visualizações'."""
    if isinstance(x, (int, float)):
        return int(x)
    if not x:
        return None
    s = str(x).strip()
    m = re.search(r"([\d][\d.,\s]*)\s*(K|M|B|mil|mi|bi)?", s, re.I)
    if not m:
        return None
    raw, suf = m.group(1).replace(" ", ""), (m.group(2) or "").lower()
    try:
        if suf:
            n = float(raw.replace(",", "."))
            n *= {"k": 1e3, "mil": 1e3, "m": 1e6, "mi": 1e6, "b": 1e9, "bi": 1e9}[suf]
        else:
            n = float(raw.replace(".", "").replace(",", ""))
    except (ValueError, KeyError):
        return None
    return int(n)


# Identificador de post aceito. Todas as redes usam shortcode alfanumérico ou id
# numérico — nada além disso é legítimo. Este é o funil por onde passam os posts
# das 4 redes, então validar aqui protege de uma vez todos os lugares que
# confiam no `code` depois: o script Node do ego-lite (onde uma aspa vira
# execução de código), o atributo onclick do mosaico e a URL do card.
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

def _safe_code(code) -> str:
    code = str(code or "").strip()
    return code if _CODE_RE.match(code) else ""

def _row(**kw):
    caption = kw.get("caption") or ""
    ts = kw.get("ts")
    return {"code": _safe_code(kw.get("code")), "url": kw.get("url") or "",
            "type": kw.get("type") or "Reel/Vídeo", "pinned": kw.get("pinned", False),
            "date": dt.datetime.fromtimestamp(ts).isoformat() if ts else None,
            "ts": ts, "caption": caption,
            "hashtags": re.findall(r"#(\w+)", caption),
            "likes": kw.get("likes") or 0, "comments": kw.get("comments") or 0,
            "reshares": kw.get("reshares") or 0, "views": kw.get("views"),
            "duration_s": kw.get("duration_s"), "followers": kw.get("followers"),
            "thumb_url": kw.get("thumb_url"), "media_urls": kw.get("media_urls") or [],
            "username": kw.get("username") or "", "platform": kw.get("platform"),
            "title": kw.get("title") or ""}


# O pk do Instagram é Snowflake: os bits altos guardam o instante de criação.
# Reels não trazem taken_at, então a data sai daqui (erro de ~2min).
IG_PK_EPOCH_MS = 1314220021721


def pk_to_ts(pk):
    try:
        return ((int(pk) >> 23) + IG_PK_EPOCH_MS) / 1000
    except (TypeError, ValueError):
        return None


TYPENAMES = {"GraphImage": "Foto", "GraphVideo": "Reel/Vídeo",
             "GraphSidecar": "Carrossel", "XDTGraphImage": "Foto",
             "XDTGraphVideo": "Reel/Vídeo", "XDTGraphSidecar": "Carrossel"}


def _ig_node(m):
    """Normaliza um post do Instagram nos DOIS schemas que a web usa.

    Logado: `code`/`like_count`/`play_count`/`taken_at`.
    Deslogado (rota Polaris antiga): `shortcode`/`edge_media_preview_like`/
    `video_view_count`/`taken_at_timestamp`. Aceitar os dois faz a coleta
    funcionar com ou sem sessão.
    """
    from .core import MEDIA_TYPES
    if not isinstance(m, dict):
        return None
    m = m.get("media") or m
    code = m.get("code") or m.get("shortcode")
    if not code:
        return None

    def count(*keys):
        for k in keys:
            v = m.get(k)
            if isinstance(v, dict) and v.get("count") is not None:
                return v["count"]
            if isinstance(v, (int, float)):
                return int(v)
        return None

    caption = m.get("caption")
    if isinstance(caption, dict):
        caption = caption.get("text")
    if not caption:
        edges = ((m.get("edge_media_to_caption") or {}).get("edges") or [])
        caption = ((edges[0].get("node") or {}).get("text") if edges else "") or ""

    mtype = (MEDIA_TYPES.get(m.get("media_type"))
             or TYPENAMES.get(m.get("__typename"))
             or ("Reel/Vídeo" if m.get("is_video") else "Foto"))

    imgs = ((m.get("image_versions2") or {}).get("candidates") or [])
    thumb = (max(imgs, key=lambda c: c.get("width", 0)).get("url") if imgs
             else m.get("display_url") or m.get("thumbnail_src"))
    vids = m.get("video_versions") or []
    media = ([{"type": "video", "url": vids[0].get("url")}] if vids
             else [{"type": "video", "url": m["video_url"]}] if m.get("video_url")
             else [{"type": "image", "url": thumb}] if thumb else [])

    dur = m.get("video_duration")
    return _row(
        code=code, url=f"https://www.instagram.com/p/{code}/", type=mtype,
        ts=m.get("taken_at") or m.get("taken_at_timestamp")
           or pk_to_ts(m.get("pk") or m.get("id")),
        caption=caption,
        likes=count("like_count", "edge_media_preview_like", "edge_liked_by"),
        comments=count("comment_count", "edge_media_to_comment"),
        views=count("play_count", "ig_play_count", "view_count",
                    "video_play_count", "video_view_count"),
        duration_s=round(dur, 1) if dur else None, thumb_url=thumb,
        media_urls=media, username=(m.get("owner") or m.get("user") or {}).get("username", ""),
        pinned=bool(m.get("timeline_pinned_user_ids")
                    or m.get("clips_tab_pinned_user_ids")
                    or m.get("pinned_for_users")),
        platform="Instagram")


def _parse_instagram(captured):
    rows, seen = [], set()
    for cap in captured:
        try:
            j = json.loads(cap["body"])
        except (ValueError, TypeError):
            continue
        nodes = []
        # varre o JSON inteiro: a conexão fica em profundidades diferentes
        # conforme a rota (data.xdt_… logado, data.user.edge_… deslogado)
        for holder in _walk(j, "edges"):
            if isinstance(holder.get("edges"), list):
                for e in holder["edges"]:
                    if isinstance(e, dict):
                        nodes.append(e.get("node") or e)
        for key in ("items", "medias"):
            if isinstance(j.get(key), list):
                nodes.extend(j[key])

        for n in nodes:
            row = _ig_node(n)
            if row and row["code"] not in seen:
                seen.add(row["code"])
                rows.append(row)
    return rows


def _parse_tiktok(captured):
    rows, seen = [], set()
    for cap in captured:
        try:
            j = json.loads(cap["body"])
        except (ValueError, TypeError):
            continue
        for it in (j.get("itemList") or j.get("item_list") or j.get("aweme_list") or []):
            if not isinstance(it, dict):
                continue
            vid = it.get("id") or it.get("aweme_id")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            stats = it.get("stats") or it.get("statistics") or {}
            author = it.get("author") or {}
            uname = author.get("uniqueId") or author.get("unique_id") or ""
            video = it.get("video") or {}
            rows.append(_row(
                code=vid, url=f"https://www.tiktok.com/@{uname}/video/{vid}",
                ts=it.get("createTime") or it.get("create_time"),
                caption=it.get("desc") or "",
                likes=stats.get("diggCount") or stats.get("digg_count"),
                comments=stats.get("commentCount") or stats.get("comment_count"),
                views=stats.get("playCount") or stats.get("play_count"),
                reshares=stats.get("shareCount") or stats.get("share_count"),
                duration_s=video.get("duration"),
                thumb_url=video.get("cover") or video.get("dynamicCover"),
                media_urls=[{"type": "video", "url": (video.get("playAddr") or "")}]
                           if video.get("playAddr") else [],
                username=uname, platform="TikTok"))
    return rows


def _walk(obj, key):
    """Percorre um JSON aninhado devolvendo todo dicionário que tenha `key`."""
    stack, out = [obj], []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if key in cur:
                out.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return out


def _parse_youtube(captured):
    rows, seen = [], set()
    for cap in captured:
        try:
            j = json.loads(cap["body"])
        except (ValueError, TypeError):
            continue
        for r in _walk(j, "videoRenderer") + _walk(j, "richItemRenderer"):
            v = r.get("videoRenderer") or (r.get("richItemRenderer") or {}).get("content", {}).get("videoRenderer")
            if not isinstance(v, dict):
                continue
            vid = v.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title = "".join(t.get("text", "") for t in
                            (v.get("title") or {}).get("runs", [])) or \
                    (v.get("title") or {}).get("simpleText", "")
            views = _num(((v.get("viewCountText") or {}).get("simpleText")) or
                         "".join(t.get("text", "") for t in
                                 (v.get("viewCountText") or {}).get("runs", [])))
            thumbs = ((v.get("thumbnail") or {}).get("thumbnails") or [])
            dur = (v.get("lengthText") or {}).get("simpleText")
            secs = None
            if dur and re.fullmatch(r"[\d:]+", dur):
                parts = [int(p) for p in dur.split(":")]
                secs = sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
            rows.append(_row(
                code=vid, url=f"https://www.youtube.com/watch?v={vid}",
                caption=title, title=title, views=views, duration_s=secs,
                thumb_url=thumbs[-1].get("url") if thumbs else None,
                platform="YouTube"))
    return rows


def _parse_facebook(captured):
    rows, seen = [], set()
    for cap in captured:
        for line in cap["body"].splitlines():      # FB responde em NDJSON
            try:
                j = json.loads(line)
            except ValueError:
                continue
            for node in _walk(j, "creation_story"):
                post_id = node.get("post_id") or node.get("id")
                if not post_id or post_id in seen:
                    continue
                seen.add(post_id)
                fb = node.get("feedback") or {}
                rows.append(_row(
                    code=str(post_id),
                    url=f"https://www.facebook.com/{post_id}",
                    ts=node.get("creation_time"),
                    caption=((node.get("message") or {}).get("text") or ""),
                    likes=(fb.get("reaction_count") or {}).get("count"),
                    comments=(fb.get("comment_rendering_instance") or {})
                             .get("comments", {}).get("total_count"),
                    reshares=(fb.get("share_count") or {}).get("count"),
                    views=(fb.get("video_view_count")),
                    platform="Facebook"))
    return rows


PARSERS = {"instagram": _parse_instagram, "tiktok": _parse_tiktok,
           "youtube": _parse_youtube, "facebook": _parse_facebook}


def collect(platform, target, max_items=100, max_scrolls=40, on_progress=None):
    """Intercepta o feed e salva um dataset pronto para a interface."""
    data, url = capture(platform, target, max_items, max_scrolls, on_progress)
    rows = PARSERS[platform](data.get("captured") or [])
    if not rows:
        raise RuntimeError(
            "nenhum post foi capturado. Verifique se você está logado nessa rede "
            "no ego lite e se o perfil/URL existe." if not data.get("logged_in")
            else "nenhum post foi reconhecido nas respostas capturadas.")
    rows = rows[:int(max_items)]

    uname = next((r["username"] for r in rows if r.get("username")), "") or \
        re.sub(r"[^\w.]", "_", target.strip().lstrip("@"))[:40]
    prof = {"username": uname, "full_name": None, "followers": None,
            "profile_pic": None, "platform": PLATFORMS[platform]["name"]}
    safe = re.sub(r"[^-\w.]", "_", uname)
    ds_id = f"{safe}_{dt.datetime.now():%Y-%m-%d_%H%M}"
    path = os.path.join(DATA_DIR, ds_id + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"collected_at": dt.datetime.now().isoformat(),
                   "source": "intercept", "platform": PLATFORMS[platform]["name"],
                   "profile": prof, "count": len(rows), "rows": rows},
                  f, ensure_ascii=False)

    # Deslogado, a maioria das redes entrega só a primeira página do perfil.
    note = ""
    if not data.get("logged_in") and len(rows) < int(max_items):
        note = (f"Você não está logado no {PLATFORMS[platform]['name']} dentro do "
                "ego lite — por isso só vieram os primeiros posts. Faça login lá "
                "e colete de novo para pegar o perfil inteiro.")
    return {"ds_id": ds_id, "path": path, "count": len(rows),
            "logged_in": bool(data.get("logged_in")), "note": note,
            "platform": PLATFORMS[platform]["name"]}
