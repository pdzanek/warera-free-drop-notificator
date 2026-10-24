#!/usr/bin/env python3
"""WarEra: phone notification when a 'FREE' entry appears (<1000 dmg).

Local usage:  NTFY_TOPIC=my-topic python notify.py
On GitHub Actions the topic comes from secrets.NTFY_TOPIC (see .github/workflows/warera.yml).

Requires only the Python standard library. On the phone: ntfy app + subscription to the same topic.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api2.warera.io/trpc"
GAME_BATTLE = "https://app.warera.io/battle/"

TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_USERNAME = os.environ.get("DISCORD_USERNAME", "").strip() or "Jan Darmowy II"
DISCORD_AVATAR_URL = os.environ.get("DISCORD_AVATAR_URL", "").strip()
MAX_PAGES = 15
MIN_INTERVAL = 0.6

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notified.json")
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

DEFAULTS = {"max_pts_left": 100, "free_dmg": 1500, "seen_minutes": 180}


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError("config root must be an object")
    except FileNotFoundError:
        return dict(DEFAULTS)
    except Exception as e:
        print(f"WARN: bad config.json ({e}), using defaults", file=sys.stderr)
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for key in out:
        try:
            out[key] = float(cfg.get(key, out[key]))
        except (TypeError, ValueError):
            print(f"WARN: bad config.json value for {key!r}, using default", file=sys.stderr)
    if "seen_minutes" not in cfg and "seen_hours" in cfg:
        try:
            out["seen_minutes"] = float(cfg["seen_hours"]) * 60
        except (TypeError, ValueError):
            print("WARN: bad config.json value for 'seen_hours', using default", file=sys.stderr)
    return out


_CFG = load_config()

FREE_DMG = int(os.environ.get("FREE_DMG", _CFG["free_dmg"]))
MAX_LEFT = int(os.environ.get("MAX_PTS_LEFT", _CFG["max_pts_left"]))
if "SEEN_MINUTES" in os.environ:
    SEEN_MINUTES = float(os.environ["SEEN_MINUTES"])
elif "SEEN_HOURS" in os.environ:
    SEEN_MINUTES = float(os.environ["SEEN_HOURS"]) * 60
else:
    SEEN_MINUTES = float(_CFG["seen_minutes"])

WEAPONS = {"knife": "common", "gun": "uncommon", "rifle": "rare",
           "sniper": "epic", "tank": "legendary", "jet": "mythic"}
ORDER = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.8",
}

_last_req = [0.0]


def throttle():
    wait = MIN_INTERVAL - (time.time() - _last_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_req[0] = time.time()


def _call(url):
    throttle()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, f"__FETCH_FAILED__ {e}"


def _parse(proc, status, text):
    if text.startswith("__FETCH_FAILED__"):
        return "retry", f"{proc}: {text}"
    try:
        p = json.loads(text)
    except Exception:
        if status in (429, 503):
            return "retry", f"{proc}: HTTP {status} non-JSON (rate limit?)"
        if status == 403:
            return "fatal", (f"{proc}: HTTP 403 (Cloudflare is blocking this server, error 1010). "
                             f"Run the script on a home PC instead of GitHub Actions.")
        return "fatal", f"{proc}: HTTP {status} non-JSON: {text[:200]}"
    if isinstance(p, list):
        p = p[0] if p else {}
    if not isinstance(p, dict):
        return "fatal", f"{proc}: HTTP {status} bad shape"
    if p.get("status") == 429 or p.get("statusCode") == 429:
        return "retry", f"{proc}: HTTP 429 {str(p.get('message', ''))[:120]}"
    err = p.get("error")
    if err:
        msg = err.get("message") if isinstance(err.get("message"), str) else json.dumps(err)[:300]
        if status == 429 or "rate limit" in msg.lower() or "too many" in msg.lower():
            return "retry", f"{proc}: HTTP 429 {msg[:120]}"
        return "fatal", f"{proc}: HTTP {status} {msg[:300]}"
    res = p.get("result")
    if isinstance(res, dict) and "data" in res:
        d = res["data"]
        if isinstance(d, dict) and set(d.keys()) == {"json"}:
            d = d["json"]
        return "ok", d
    return "fatal", f"{proc}: HTTP {status} unexpected shape: {text[:200]}"


def trpc(proc, inp=None):
    variants = [None] if inp is None else [inp, {"json": inp}]
    last = f"{proc}: empty response"
    for vi, v in enumerate(variants):
        url = f"{API}/{proc}" if v is None else \
            f"{API}/{proc}?input={urllib.parse.quote(json.dumps(v, separators=(',', ':')), safe='')}"
        for attempt in range(6):
            status, text = _call(url)
            kind, val = _parse(proc, status, text)
            if kind == "ok":
                return val
            last = val
            if kind == "retry":
                time.sleep(2.5 * (attempt + 1))
                continue
            if vi == 0 and v is not None and ("invalid_type" in val or "Required" in val or "Expected" in val):
                break  # try wrapped format
            raise RuntimeError(val)
    raise RuntimeError(last)


def rarity_of(code):
    if not code:
        return "unknown"
    if code in WEAPONS:
        return WEAPONS[code]
    m = re.match(r"^(helmet|chest|boots|gloves|pants)([1-6])$", str(code))
    if m:
        return ORDER[int(m.group(2)) - 1]
    return "unknown"


def flag(cc):
    if not cc or len(cc) != 2:
        return "🏳"
    return "".join(chr(127397 + ord(c)) for c in cc.upper())


def all_battles():
    out, cursor = [], None
    for _ in range(12):
        inp = {"limit": 100, "isActive": True}
        if cursor:
            inp["cursor"] = cursor
        data = trpc("battle.getBattles", inp)
        items = data.get("items", []) if isinstance(data, dict) else []
        out.extend(items)
        cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not items or not cursor:
            break
    seen, act = set(), []
    for b in out:
        if b.get("isActive") and b.get("_id") not in seen:
            seen.add(b.get("_id"))
            act.append(b)
    return act


def ranking_rows(round_id, paying_slots):
    rows, cursor = [], None
    cap = min(paying_slots, MAX_PAGES * 100)
    while True:
        inp = {"roundId": round_id, "dataType": "damage", "type": "user",
               "side": "merged", "limit": 100}
        if cursor:
            inp["cursor"] = cursor
        data = trpc("battleRanking.getRanking", inp)
        items = data.get("items", [])
        if not items:
            break
        for row in items:
            if row.get("rank", 0) > cap or row.get("rank", 0) > paying_slots:
                return rows
            if row.get("lootItem"):
                rows.append(row)
        last = items[-1]
        if not data.get("nextCursor") or last.get("rank", 0) >= cap or len(items) < 100:
            break
        cursor = data.get("nextCursor")
    return rows


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
            return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception as e:
        print(f"WARN: nie zapisano notified.json: {e}", file=sys.stderr)


def dedupe_offers(best, saved, now):
    # Drop offers already notified within SEEN_MINUTES, unless the entry is
    # now at most half the previously notified damage (twice as cheap).
    # State values may be plain timestamps (format before damage tracking).
    state = {}
    for k, v in saved.items():
        entry = v if isinstance(v, dict) else {"at": v, "need": None}
        try:
            at = float(entry.get("at", 0))
        except (TypeError, ValueError):
            continue
        if now - at < SEEN_MINUTES * 60:
            state[k] = entry
    fresh = []
    for o in best:
        key = f"{o['battle'].get('_id')}:{o['rarity']}"
        prev_need = state.get(key, {}).get("need")
        o["price_drop"] = False
        o["replace_discord_id"] = None
        if key not in state or (isinstance(prev_need, (int, float)) and o["need"] * 2 <= prev_need):
            o["price_drop"] = key in state
            if key in state:
                o["replace_discord_id"] = state[key].get("discord_id")
            fresh.append(o)
            state[key] = {"at": now, "need": o["need"]}
    return fresh, state


def send_ntfy(topic, title, body, click):
    # HTTP headers are latin-1, so encode the title as a single RFC 2047
    # base64 word (ntfy decodes it). Built manually on purpose: str(Header)
    # does not encode at all, and Header.encode() may fold long lines.
    safe_title = ("=?utf-8?b?" + base64.b64encode(title.encode("utf-8")).decode("ascii") + "?=")
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), method="POST",
        headers=dict(HEADERS, **{"Title": safe_title, "Priority": "high", "Tags": "tada", "Click": click}))
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def eta_mins(o):
    t = o["timing"]
    per = t["perTick"] if t["perTick"] > 0 else 1
    return max(1, -(-(o["winPts"] - max(t["atk"], t["def"])) // per)) * 2


def discord_embeds(title, offers):
    fields = []
    for o in offers:
        t = o["timing"]
        drop = "\U0001F4C9 " if o.get("price_drop") else ""
        name = (f"{drop}{flag(o['names']['atkCode'])}→{flag(o['names']['defCode'])} "
                f"{o['names']['region'] or o['names']['type']} · {o['rarity']}")
        value = (f"**{o['need']} dmg** · R{t['no']} {t['wonA']}:{t['wonD']} "
                 f"· ~{eta_mins(o)} min\n"
                 f"[Otwórz bitwę]({GAME_BATTLE}{o['battle'].get('_id')})")
        fields.append({"name": name[:256], "value": value[:1024], "inline": False})
    return [{
        "title": title[:256],
        "color": 0xFFD700,
        "fields": fields[:25],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]


def send_discord(webhook, embeds):
    # ?wait=true makes Discord return the posted message (with its id).
    payload = {"username": DISCORD_USERNAME, "embeds": embeds}
    if DISCORD_AVATAR_URL:
        payload["avatar_url"] = DISCORD_AVATAR_URL
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook + "?wait=true", data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        try:
            return json.loads(r.read().decode("utf-8")).get("id")
        except Exception:
            return None


def delete_discord_message(webhook, message_id):
    req = urllib.request.Request(
        f"{webhook}/messages/{message_id}", method="DELETE",
        headers={"User-Agent": HEADERS["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True  # already gone (manual delete, replaced webhook)
        raise


def cleanup_discord_messages(webhook, message_ids):
    # Best effort: cleanup must never fail the notification run.
    for mid in dict.fromkeys(message_ids):
        if not mid:
            continue
        try:
            delete_discord_message(webhook, mid)
        except Exception as e:
            print(f"WARN: nie usunieto wiadomosci Discord {mid}: {e}", file=sys.stderr)


def collect_stale_discord_ids(saved, now):
    ids = []
    for v in saved.values():
        if not isinstance(v, dict) or not v.get("discord_id"):
            continue
        try:
            at = float(v.get("at", 0))
        except (TypeError, ValueError):
            continue
        if now - at >= SEEN_MINUTES * 60:
            ids.append(v["discord_id"])
    return ids


def main():
    cfg = trpc("gameConfig.getGameConfig")
    round_div = (cfg.get("loot") or {}).get("damagePerLootItem", 200000)
    win_pts = (cfg.get("battle") or {}).get("pointsToWinRound", 300)
    countries_raw = trpc("country.getAllCountries") or []
    regions = trpc("region.getRegionsObject") or {}
    countries = {c.get("_id"): c for c in countries_raw if isinstance(c, dict)}

    battles = all_battles()
    print(f"zywych bitew: {len(battles)}")

    offers = []
    for b in battles:
        if b.get("type") in ("tournament", "revolution"):
            continue
        cr = b.get("currentRound") or {}
        atk_pts = (cr.get("attacker") or {}).get("points", 0)
        def_pts = (cr.get("defender") or {}).get("points", 0)
        left = win_pts - max(atk_pts, def_pts)
        if not (left < MAX_LEFT):
            continue
        rd = (cr.get("attacker") or {}).get("damages", 0) + (cr.get("defender") or {}).get("damages", 0)
        slots = rd // round_div
        rid = cr.get("_id")
        if not rid or slots <= 0:
            continue
        try:
            rows = ranking_rows(rid, slots)
        except Exception as e:
            print(f"ranking {str(rid)[-6:]} blad: {e}", file=sys.stderr)
            continue
        atk_c = countries.get((b.get("attacker") or {}).get("country"), {})
        def_c = countries.get((b.get("defender") or {}).get("country"), {})
        region = (regions.get((b.get("defender") or {}).get("region"), {}) or {}).get("name", "")
        names = {"atk": atk_c.get("name", "?"), "def": def_c.get("name", "?"),
                 "atkCode": (atk_c.get("code") or "").lower(),
                 "defCode": (def_c.get("code") or "").lower(), "region": region,
                 "type": b.get("type", "war")}
        timing = {"no": cr.get("number", "?"), "atk": atk_pts, "def": def_pts,
                  "wonA": (b.get("attacker") or {}).get("wonRoundsCount", 0),
                  "wonD": (b.get("defender") or {}).get("wonRoundsCount", 0),
                  "perTick": ((cr.get("live") or {}).get("actualTickPoints") or 1)}
        for row in rows:
            code = (row.get("lootItem") or {}).get("code")
            rar = rarity_of(code)
            if rar not in ("rare", "epic"):
                continue
            need = (row.get("value") or 0) + 1
            if need < FREE_DMG:
                offers.append({"need": need, "rarity": rar, "rank": row.get("rank"),
                               "battle": b, "names": names, "timing": timing, "winPts": win_pts})

    # one battle = cheapest free entry
    offers.sort(key=lambda o: (o["need"], o["rank"] or 0))
    seen, best = set(), []
    for o in offers:
        key = o["battle"].get("_id")
        if key in seen:
            continue
        seen.add(key)
        best.append(o)

    now = time.time()
    saved = load_state()
    stale_ids = collect_stale_discord_ids(saved, now) if DISCORD_WEBHOOK else []
    fresh, state = dedupe_offers(best, saved, now)
    save_state(state)
    if DISCORD_WEBHOOK:
        cleanup_discord_messages(DISCORD_WEBHOOK, stale_ids)

    if not fresh:
        print("brak nowych okazji ZA DARMO.")
        return

    lines = []
    for o in fresh:
        t = o["timing"]
        mins = eta_mins(o)
        drop = "\U0001F4C9 " if o.get("price_drop") else ""
        lines.append(
            f"{drop}{flag(o['names']['atkCode'])}→{flag(o['names']['defCode'])} "
            f"{o['names']['region'] or o['names']['type']} · {o['rarity']} · "
            f"{o['need']} dmg · R{t['no']} {t['wonA']}:{t['wonD']} · ~{mins} min\n"
            f"{GAME_BATTLE}{o['battle'].get('_id')}")
    n = len(fresh)
    if n == 1:
        noun = "okazja"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        noun = "okazje"
    else:
        noun = "okazji"
    if fresh and all(o.get("price_drop") for o in fresh):
        title = f"\U0001F4C9 Jeszcze taniej: {n} {noun}"
    else:
        title = f"\U0001F389 WarEra ZA DARMO: {n} {noun}"
    # No project name in the Discord title (custom emoji did not render).
    discord_title = title.replace("WarEra ", "")
    body = "\n\n".join(lines)
    print(title + "\n" + body)
    if not TOPIC and not DISCORD_WEBHOOK:
        print("[dry-run] brak NTFY_TOPIC i DISCORD_WEBHOOK_URL - nie wyslano.", file=sys.stderr)
        return
    errors = []
    if TOPIC:
        try:
            send_ntfy(TOPIC, title, body, f"{GAME_BATTLE}{fresh[0]['battle'].get('_id')}")
            print("wyslano na telefon.")
        except Exception as e:
            errors.append(f"ntfy: {e}")
            print(f"WARN: nie wyslano na telefon: {e}", file=sys.stderr)
    if DISCORD_WEBHOOK:
        try:
            message_id = send_discord(DISCORD_WEBHOOK, discord_embeds(discord_title, fresh))
            print("wyslano na Discorda.")
            replaced = [o.get("replace_discord_id") for o in fresh]
            if message_id:
                for o in fresh:
                    key = f"{o['battle'].get('_id')}:{o['rarity']}"
                    if key in state:
                        state[key]["discord_id"] = message_id
                save_state(state)
            owned = {e.get("discord_id") for e in state.values() if isinstance(e, dict)}
            cleanup_discord_messages(
                DISCORD_WEBHOOK, [mid for mid in replaced if mid and mid not in owned])
        except Exception as e:
            errors.append(f"discord: {e}")
            print(f"WARN: nie wyslano na Discorda: {e}", file=sys.stderr)
    if errors:
        raise RuntimeError("; ".join(errors))


if __name__ == "__main__":
    main()
