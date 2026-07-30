"""Coleta via ego-lite (subprocesso `ego-browser nodejs`).

Dois modos:
  - collect_profile(username, ...)  → pagina o feed do perfil (API interna logada)
  - resolve_urls(urls, ...)         → resolve URLs de posts/reels individuais

Ambos gravam um dataset JSON em social/data e devolvem (ds_id, path). O formato do
payload é o mesmo do IGSorter, então social/core.normalize_item consome os dois.
"""
import datetime as dt
import os
import re
import shutil
import subprocess

from .core import DATA_DIR

# ── modo perfil (feed) ──────────────────────────────────────────
# Igual ao IGSorter: pagina https://www.instagram.com/api/v1/feed/user/<user>/
NODE_PROFILE = r"""
const USERNAME = '%(username)s'
const MAX_POSTS = %(max_posts)d
const SINCE_TS = %(since_ts)d  // 0 = sem limite de período
const OUT_FILE = '%(out_file)s'

const task = await useOrCreateTaskSpace('social coleta')
cliLog('task space id: ' + task.id)

await openOrReuseTab('https://www.instagram.com/' + USERNAME + '/', { wait: true, timeout: 30 })
await wait(3)

const info = await pageInfo()
if (info && info.dialog) { await cdp('Page.handleJavaScriptDialog', { accept: true }) }
const loggedIn = await js(String.raw`(() => !!document.cookie.match(/sessionid=/) || !document.querySelector('input[name="username"]'))()`)
if (!loggedIn) {
  cliLog('NOT_LOGGED_IN')
} else {

let profile = null
try {
  profile = await js(String.raw`(() => {
    const parseNum = (s) => {
      if (!s) return null
      const str = String(s).trim()
      const m = str.match(/([\d][\d.,\s]*)\s*(K|M|B|mil|mi|bi)?/i)
      if (!m) return null
      let raw = m[1].replace(/\s/g, '')
      const suf = (m[2] || '').toLowerCase()
      let n
      if (suf) {
        n = parseFloat(raw.replace(',', '.'))
        if (suf === 'k' || suf === 'mil') n *= 1e3
        else if (suf === 'm' || suf === 'mi') n *= 1e6
        else if (suf === 'b' || suf === 'bi') n *= 1e9
      } else {
        n = parseFloat(raw.replace(/[.,]/g, ''))
      }
      return isNaN(n) ? null : Math.round(n)
    }
    const out = { username: null, full_name: null, followers: null, following: null,
                  posts_total: null, is_private: null, profile_pic: null }
    const pick = (re) => {
      const el = [...document.querySelectorAll('header a, header span, a[href*="followers"], a[href*="following"]')]
        .find(e => re.test(e.innerText || ''))
      return el ? parseNum(el.innerText) : null
    }
    out.followers = pick(/seguidores|followers/i)
    out.following = pick(/seguindo|following/i)
    const meta = document.querySelector('meta[property="og:description"]')
    if (meta && meta.content) {
      const c = meta.content
      const g = (re) => { const m = c.match(re); return m ? parseNum(m[1]) : null }
      if (out.followers == null) out.followers = g(/([\d.,]+\s*(?:K|M|B|mil|mi|bi)?)\s*(?:Followers|seguidores)/i)
      if (out.following == null) out.following = g(/([\d.,]+\s*(?:K|M|B|mil|mi|bi)?)\s*(?:Following|seguindo)/i)
      out.posts_total = g(/([\d.,]+\s*(?:K|M|B|mil|mi|bi)?)\s*(?:Posts|publica)/i)
    }
    const img = document.querySelector('header img')
    if (img) out.profile_pic = img.src
    const mt = (document.title || '').match(/^(.*?)\s*\(@/)
    if (mt) out.full_name = mt[1].trim()
    return out
  })()`)
} catch (e) { cliLog('perfil da pagina falhou: ' + e.message) }

const items = []
let maxId = ''
let pages = 0
while (items.length < MAX_POSTS && pages < Math.ceil(MAX_POSTS / 33) + 2) {
  pages++
  const url = 'https://www.instagram.com/api/v1/feed/user/' + USERNAME +
    '/username/?count=33' + (maxId ? '&max_id=' + encodeURIComponent(maxId) : '')
  let body
  try {
    body = await browserFetch(url, { headers: { 'x-ig-app-id': '936619743392459', 'x-requested-with': 'XMLHttpRequest' } })
  } catch (e) { cliLog('PAGE_FAIL ' + pages + ': ' + e.message); break }
  const j = typeof body === 'string' ? JSON.parse(body) : body
  if (j && j.message === 'login_required') { cliLog('LOGIN_REQUIRED'); break }
  if (!j || !Array.isArray(j.items) || j.items.length === 0) { cliLog('NO_ITEMS page ' + pages); break }
  items.push(...j.items)
  cliLog('PROGRESS ' + items.length)
  const oldest = j.items[j.items.length - 1]
  if (SINCE_TS > 0 && oldest && oldest.taken_at && oldest.taken_at < SINCE_TS) break
  if (!j.more_available || !j.next_max_id) break
  maxId = j.next_max_id
  await wait(2.5 + Math.random() * 2.5)
}

let finalItems = items
if (SINCE_TS > 0) finalItems = finalItems.filter(it => !it.taken_at || it.taken_at >= SINCE_TS)
finalItems = finalItems.slice(0, MAX_POSTS)

const u0 = finalItems.length ? (finalItems[0].user || {}) : {}
if (!profile) profile = {}
profile.username   = profile.username   || u0.username || USERNAME
profile.full_name   = u0.full_name || profile.full_name || null
profile.profile_pic = u0.profile_pic_url || profile.profile_pic || null
if (profile.is_private == null) profile.is_private = u0.is_private ?? null
profile.is_verified = u0.is_verified ?? null
cliLog('PERFIL ' + profile.username + ' | seguidores: ' + profile.followers)
const payload = { collected_at: new Date().toISOString(), source: 'feed_api',
  since_ts: SINCE_TS || null,
  profile, count: finalItems.length, items: finalItems }

try {
  const fs = require('fs')
  fs.writeFileSync(OUT_FILE, JSON.stringify(payload))
  cliLog('SAVED:' + OUT_FILE)
} catch (e) {
  cliLog('BEGIN_IGSORTER_JSON')
  cliLog(JSON.stringify(payload))
  cliLog('END_IGSORTER_JSON')
}
}
await completeTaskSpace(task.id, { keep: false })
"""

# ── modo URLs (posts/reels individuais) ─────────────────────────
# Recebe uma lista de media_ids (calculados em Python a partir do shortcode) e
# busca cada um na API interna /media/<pk>/info/, que devolve o item no MESMO
# formato do feed — reaproveitando toda a normalização.
NODE_URLS = r"""
const IDS = %(ids_json)s   // [{pk:'123', code:'ABC'}, ...]
const OUT_FILE = '%(out_file)s'

const task = await useOrCreateTaskSpace('social urls')
cliLog('task space id: ' + task.id)

await openOrReuseTab('https://www.instagram.com/', { wait: true, timeout: 30 })
await wait(2)

const loggedIn = await js(String.raw`(() => !!document.cookie.match(/sessionid=/) || !document.querySelector('input[name="username"]'))()`)
if (!loggedIn) {
  cliLog('NOT_LOGGED_IN')
} else {
  const items = []
  for (let i = 0; i < IDS.length; i++) {
    const { pk, code } = IDS[i]
    const url = 'https://www.instagram.com/api/v1/media/' + pk + '/info/'
    let body
    try {
      body = await browserFetch(url, { headers: { 'x-ig-app-id': '936619743392459', 'x-requested-with': 'XMLHttpRequest' } })
    } catch (e) { cliLog('URL_FAIL ' + code + ': ' + e.message); continue }
    const j = typeof body === 'string' ? JSON.parse(body) : body
    if (j && j.message === 'login_required') { cliLog('LOGIN_REQUIRED'); break }
    if (j && Array.isArray(j.items) && j.items[0]) {
      items.push(j.items[0])
      cliLog('PROGRESS ' + items.length)
    } else {
      cliLog('URL_EMPTY ' + code)
    }
    await wait(1.5 + Math.random() * 1.5)
  }
  const u0 = items.length ? (items[0].user || {}) : {}
  const profile = { username: u0.username || null, full_name: u0.full_name || null,
                    profile_pic: u0.profile_pic_url || null, followers: null }
  const payload = { collected_at: new Date().toISOString(), source: 'urls',
    profile, count: items.length, items }
  try {
    const fs = require('fs')
    fs.writeFileSync(OUT_FILE, JSON.stringify(payload))
    cliLog('SAVED:' + OUT_FILE)
  } catch (e) {
    cliLog('BEGIN_IGSORTER_JSON'); cliLog(JSON.stringify(payload)); cliLog('END_IGSORTER_JSON')
  }
}
await completeTaskSpace(task.id, { keep: false })
"""

_SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def ego_available():
    return shutil.which("ego-browser") is not None


def shortcode_to_id(shortcode: str) -> int:
    """Converte o shortcode de uma URL de post (…/p/<code>/ ou …/reel/<code>/)
    no PK numérico da mídia — decodificação base64 padrão do Instagram."""
    pk = 0
    for ch in shortcode:
        pk = pk * 64 + _SHORTCODE_ALPHABET.index(ch)
    return pk


def parse_instagram_urls(urls):
    """Extrai (pk, shortcode) de cada URL de post/reel do Instagram. Ignora o que
    não casar. Levanta ValueError se nenhuma URL for válida."""
    out, seen = [], set()
    pat = re.compile(r"instagram\.com/(?:[^/]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", re.I)
    for u in urls:
        m = pat.search(u or "")
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)
        try:
            out.append({"pk": str(shortcode_to_id(code)), "code": code})
        except ValueError:
            continue
    if not out:
        raise ValueError("nenhuma URL de post/reel do Instagram reconhecida")
    return out


def _run_ego(script, on_progress, timeout):
    proc = subprocess.Popen(["ego-browser", "nodejs"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    try:
        proc.stdin.write(script)
        proc.stdin.close()
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            if on_progress:
                m = re.search(r"PROGRESS (\d+)", line)
                if m:
                    on_progress(int(m.group(1)))
                elif line.strip():
                    on_progress(None, line[:200])
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError("coleta excedeu o tempo limite")
    return "\n".join(lines), lines


def _finish(out_file, output, lines):
    if "NOT_LOGGED_IN" in output or "LOGIN_REQUIRED" in output:
        raise RuntimeError("Instagram não está logado no ego lite. Abra o ego lite, "
                           "faça login no instagram.com e tente novamente.")
    if os.path.isfile(out_file) and os.path.getsize(out_file) > 2:
        return out_file
    m = re.search(r"BEGIN_IGSORTER_JSON\s*(\{.*\})\s*END_IGSORTER_JSON", output, re.S)
    if m:
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(m.group(1))
        return out_file
    tail = "\n".join(lines[-15:])
    raise RuntimeError("coleta não retornou dados. Últimas linhas:\n" + tail)


def collect_profile(username, max_posts=100, since_days=None, on_progress=None,
                    timeout=1800):
    """Coleta o feed de um perfil. Retorna (ds_id, path). Levanta RuntimeError."""
    username = username.strip().lstrip("@").lower()
    if not re.fullmatch(r"[\w.]{1,40}", username):
        raise RuntimeError(f"username inválido: {username!r}")
    if not ego_available():
        raise RuntimeError("comando 'ego-browser' não encontrado. Instale o ego lite "
                           "(https://lite.ego.app) e abra o app uma vez.")

    since_ts = 0
    if since_days:
        since_ts = int((dt.datetime.now() - dt.timedelta(days=int(since_days))).timestamp())

    ds_id = f"{username}_{dt.datetime.now():%Y-%m-%d_%H%M}"
    out_file = os.path.join(DATA_DIR, ds_id + ".json")
    script = NODE_PROFILE % {"username": username, "max_posts": int(max_posts),
                             "since_ts": since_ts, "out_file": out_file}
    output, lines = _run_ego(script, on_progress, timeout)
    _finish(out_file, output, lines)
    return ds_id, out_file


def resolve_urls(urls, on_progress=None, timeout=1800):
    """Resolve URLs de posts/reels individuais. Retorna (ds_id, path)."""
    import json as _json
    if not ego_available():
        raise RuntimeError("comando 'ego-browser' não encontrado. Instale o ego lite "
                           "(https://lite.ego.app) e abra o app uma vez.")
    ids = parse_instagram_urls(urls)
    ds_id = f"urls_{dt.datetime.now():%Y-%m-%d_%H%M%S}"
    out_file = os.path.join(DATA_DIR, ds_id + ".json")
    script = NODE_URLS % {"ids_json": _json.dumps(ids), "out_file": out_file}
    output, lines = _run_ego(script, on_progress, timeout)
    _finish(out_file, output, lines)
    return ds_id, out_file
