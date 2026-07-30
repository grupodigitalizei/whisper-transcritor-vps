"""Normalização de posts do Instagram, cálculo de ER e tendências.

Adaptado de ~/IGSorter/igsorter/core.py. A diferença principal: os datasets e o
cache de thumbs vivem sob `.whisper_data/social/`, e não em pastas próprias, para
tudo caber no mesmo diretório de dados do app de transcrição.
"""
import datetime as dt
import json
import os
import re

MEDIA_TYPES = {1: "Foto", 2: "Reel/Vídeo", 8: "Carrossel"}
WEEKDAYS_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

# Paths — ancorados no diretório de dados do app (.whisper_data/social).
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOCIAL_DIR = os.path.join(_APP_DIR, ".whisper_data", "social")
DATA_DIR = os.path.join(SOCIAL_DIR, "data")          # datasets coletados (JSON)
CACHE_DIR = os.path.join(SOCIAL_DIR, "cache", "thumbs")
EXPORT_DIR = os.path.join(SOCIAL_DIR, "exports")     # planilhas Excel/CSV geradas
for _d in (DATA_DIR, CACHE_DIR, EXPORT_DIR):
    os.makedirs(_d, exist_ok=True)


def load_payload(path):
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    m = re.search(r"BEGIN_IGSORTER_JSON\s*(\{.*\})\s*END_IGSORTER_JSON", raw, re.S)
    if m:
        raw = m.group(1)
    data = json.loads(raw)
    if isinstance(data, list):
        data = {"profile": None, "items": data}
    return data


def _best_image(candidates):
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.get("width", 0) or 0))


def normalize_item(it, profile):
    code = it.get("code")
    media_type = it.get("media_type")
    taken_at = it.get("taken_at")
    when = dt.datetime.fromtimestamp(taken_at) if taken_at else None
    cap = it.get("caption")
    caption = cap.get("text") if isinstance(cap, dict) else (cap or "")
    caption = caption or ""

    likes = it.get("like_count") or 0
    comments = it.get("comment_count") or 0
    reshares = it.get("reshare_count") or it.get("share_count") or 0
    views = it.get("play_count") or it.get("ig_play_count") or it.get("view_count") or None
    duration = it.get("video_duration")

    media_urls, thumb = [], None

    def extract(node):
        nonlocal thumb
        vids = node.get("video_versions") or []
        imgs = (node.get("image_versions2") or {}).get("candidates") or []
        bi = _best_image(imgs)
        if thumb is None and bi:
            thumb = bi.get("url")
        if vids:
            media_urls.append({"type": "video", "url": vids[0].get("url")})
        elif bi:
            media_urls.append({"type": "image", "url": bi.get("url")})

    if media_type == 8:
        for c in it.get("carousel_media") or []:
            extract(c)
    else:
        extract(it)

    has_video = any(m["type"] == "video" for m in media_urls)

    return {
        "code": code,
        "url": f"https://www.instagram.com/p/{code}/" if code else "",
        "type": MEDIA_TYPES.get(media_type, str(media_type)),
        "is_video": has_video,
        "date": when.isoformat() if when else None,
        "ts": taken_at,
        "caption": caption,
        "hashtags": re.findall(r"#(\w+)", caption),
        "likes": likes,
        "comments": comments,
        "reshares": reshares,
        "views": views,
        "duration_s": round(duration, 1) if duration else None,
        "followers": (profile or {}).get("followers"),
        "thumb_url": thumb,
        "media_urls": media_urls,
        "username": (it.get("user") or {}).get("username") or (profile or {}).get("username", ""),
    }


def compute_er(row, wl=1.0, wc=4.0, wr=4.0):
    if row["views"]:
        return round((row["likes"] * wl + row["comments"] * wc + row["reshares"] * wr)
                     / row["views"] * 100, 2)
    if row["followers"]:
        return round((row["likes"] + row["comments"]) / row["followers"] * 100, 2)
    return None


def load_dataset(path, weights=(1, 4, 4)):
    payload = load_payload(path)
    profile = payload.get("profile")
    rows = [normalize_item(it, profile) for it in payload.get("items", [])]
    for r in rows:
        r["er"] = compute_er(r, *weights)
    return {"profile": profile, "rows": rows,
            "collected_at": payload.get("collected_at")}


def list_datasets():
    out = []
    for f in sorted(os.listdir(DATA_DIR), reverse=True):
        if not f.endswith(".json"):
            continue
        path = os.path.join(DATA_DIR, f)
        try:
            p = load_payload(path)
        except Exception:
            continue
        prof = p.get("profile") or {}
        out.append({
            "id": f[:-5],
            "file": f,
            "username": prof.get("username") or f.split("_")[0],
            "followers": prof.get("followers"),
            "count": p.get("count") or len(p.get("items", [])),
            "collected_at": p.get("collected_at"),
        })
    return out


def dataset_path(ds_id):
    # Impede path traversal — ds_id vem da URL.
    ds_id = os.path.basename(ds_id)
    path = os.path.join(DATA_DIR, ds_id + ".json")
    if not os.path.isfile(path):
        raise FileNotFoundError(ds_id)
    return path


def build_trends(rows):
    def parse_date(r):
        return dt.datetime.fromisoformat(r["date"]) if r["date"] else None

    def agg(keyfn):
        buckets = {}
        for r in rows:
            k = keyfn(r)
            if k is None:
                continue
            b = buckets.setdefault(k, {"n": 0, "er": [], "views": [], "likes": []})
            b["n"] += 1
            if r["er"] is not None:
                b["er"].append(r["er"])
            if r["views"]:
                b["views"].append(r["views"])
            b["likes"].append(r["likes"])
        return [{"key": k, "posts": b["n"],
                 "er_medio": round(sum(b["er"]) / len(b["er"]), 2) if b["er"] else None,
                 "views_medias": int(sum(b["views"]) / len(b["views"])) if b["views"] else None,
                 "likes_medios": int(sum(b["likes"]) / len(b["likes"]))}
                for k, b in buckets.items()]

    weekday = agg(lambda r: WEEKDAYS_PT[parse_date(r).weekday()] if r["date"] else None)
    weekday.sort(key=lambda x: WEEKDAYS_PT.index(x["key"]))
    hour = sorted(agg(lambda r: parse_date(r).hour if r["date"] else None),
                  key=lambda x: x["key"])

    def dur_bucket(r):
        d = r["duration_s"]
        if not d:
            return None
        for lim, label in [(15, "0–15s"), (30, "15–30s"), (60, "30–60s"), (90, "60–90s")]:
            if d <= lim:
                return label
        return "90s+"

    order = ["0–15s", "15–30s", "30–60s", "60–90s", "90s+"]
    duration = sorted(agg(dur_bucket), key=lambda x: order.index(x["key"]))

    hb = {}
    for r in rows:
        for h in set(r["hashtags"]):
            b = hb.setdefault(h.lower(), {"n": 0, "er": []})
            b["n"] += 1
            if r["er"] is not None:
                b["er"].append(r["er"])
    hashtags = sorted(
        [{"key": "#" + h, "posts": b["n"],
          "er_medio": round(sum(b["er"]) / len(b["er"]), 2) if b["er"] else None}
         for h, b in hb.items() if b["n"] >= 2],
        key=lambda x: (x["er_medio"] or 0), reverse=True)[:15]

    return {"weekday": weekday, "hour": hour, "type": agg(lambda r: r["type"]),
            "duration": duration, "hashtag": hashtags}
