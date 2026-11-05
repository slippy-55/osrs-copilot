"""
GE Copilot robot. GitHub runs this every 30 minutes.

Every run:   checks your watchlist and posts Discord alerts.
Once a day:  quick AI scan of news + roadmap (Claude Haiku, cheap).
Sundays:     deep AI scan (Claude Sonnet).
Results are saved in the data/ folder, which the app reads.
"""
import os, re, json, time, math, difflib, datetime as dt
import xml.etree.ElementTree as ET
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
API = "https://prices.runescape.wiki/api/v2/osrs"
HEAD = {"User-Agent": "osrs-copilot personal tool - github.com/slippy-55"}
NEWS_RSS = "https://secure.runescape.com/m=news/latest_news.rss?oldschool=true"
UPCOMING = ("https://oldschool.runescape.wiki/api.php?action=parse&page=Upcoming_updates"
            "&prop=wikitext&format=json&formatversion=2")

COOLDOWN = 6 * 3600          # don't repeat the same alert within 6 hours
AI_HOUR_UTC = 7              # daily scan happens after 7am UTC
SEARCH_PRICE = 0.01          # $ per web search
MODES = {                    # model, $/M input, $/M output, max searches
    "quick": (os.environ.get("QUICK_MODEL", "claude-haiku-5-5"), 0.10, 0.50, 3),
    "deep": (os.environ.get("DEEP_MODEL", "claude-sonnet-5-5"), 2.00, 10.00, 6),
}

PLAYBOOK = """You are an Old School RuneScape market speculator working for one player.
Find TRADEABLE items whose price should move because of upcoming or just-released content.
Patterns that work:
- New boss/monster with a weakness or attribute (golem, dragon, demon, undead, kalphite, vampyre,
  leafy, shade...) -> the few tradeable items that exploit it. Fewer substitutes = bigger spike.
  Example: the Mad Angel was a golem, the granite hammer was the only tradeable golembane weapon.
- Tradeable items required or consumed by a new quest on release day.
- New recipes or upgrades that consume an existing item.
- New drop sources or easier methods -> supply up -> price down.
- Gear nerfs/buffs, best-in-slot changes, buy-limit changes, new Slayer monsters,
  new Farming patches (seed demand), skill reworks, seasonal events.
- Second-order effects: what is the in-demand item made from?
Only name items that can be traded on the Grand Exchange. If content affects an untradeable
item, follow it to the tradeable item it turns into (e.g. Marks of Grace -> Amylase crystal ->
Stamina potion). Ideas with no tradeable items are thrown away.
Stress-test every idea: cheaper substitutes? already priced in? how elastic is supply
(boss-drop-only items spike harder than skilled or botted items)?
Use web search on oldschool.runescape.wiki to verify weaknesses, requirements and recipes.
Every item must be directly affected by a confirmed fact you can point to (a Jagex blog, poll,
patch note or Wiki page). If you can't point to the fact, leave the item out. Don't guess at
drops, weaknesses or recipes that haven't been announced: search to confirm them first.
Keep every text field under 40 words.
"action" must be exactly one of: Buy now, Buy on announcement, Sell if holding, Avoid holding, Watch.
Reply with ONLY a JSON object, starting with { and ending with }, no other text:
{"theses":[{"title":"","trigger":"","release":"","evidence":"the confirmed fact, plus its URL",
"mechanism":"","direction":"up or down","items":["exact OSRS item names"],"substitutes":"",
"confidence":"low, medium or high","action":"","action_detail":"what to do and when",
"exit_plan":"","what_would_kill_it":""}]}"""


# ---------- small helpers ----------
def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def get(url, params=None):
    r = requests.get(url, params=params, headers=HEAD, timeout=30)
    r.raise_for_status()
    return r


def tax(price):
    """GE tax: 2% rounded down, max 5m, items under 50gp are exempt."""
    if not price or price < 50:
        return 0
    return min(math.floor(price * 0.02), 5_000_000)


def fmt(n):
    if n is None:
        return "-"
    a = abs(n)
    s = f"{a/1e9:.2f}b" if a >= 1e9 else f"{a/1e6:.1f}m" if a >= 1e7 else \
        f"{round(a/1e3)}k" if a >= 1e5 else f"{a:,}"
    return ("-" if n < 0 else "") + s


def discord(text):
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        print("[no Discord webhook set]\n" + text)
        return
    while text:
        chunk, text = text[:1900], text[1900:]
        try:
            requests.post(url, json={"content": chunk, "username": "GE Bot"}, timeout=20)
        except Exception as e:
            print("Discord post failed:", e)
        time.sleep(1)


def discord_embeds(content, embeds):
    url = os.environ.get("DISCORD_WEBHOOK")
    if not url:
        print("[no Discord webhook set]\n" + content + "\n" + json.dumps(embeds, indent=1)[:3000])
        return
    for i in range(0, max(len(embeds), 1), 4):  # small batches stay under Discord's size limit
        body = {"username": "GE Bot", "content": content if i == 0 else "", "embeds": embeds[i:i + 4]}
        try:
            r = requests.post(url, json=body, timeout=20)
            if r.status_code >= 400:
                print("Discord rejected the message:", r.text[:300])
        except Exception as e:
            print("Discord post failed:", e)
        time.sleep(1)


def clip(text, n):
    text = str(text or "").strip() or "n/a"
    return text if len(text) <= n else text[: n - 1] + "…"


def thesis_embed(t):
    up = str(t.get("direction", "")).lower().startswith("up")
    items = []
    for i in t["items"]:
        if not i.get("id"):
            continue
        items.append(f"[{i['name']}](https://prices.runescape.wiki/osrs/item/{i['id']}): "
                     f"buy {fmt(i.get('high_then'))}, sell {fmt(i.get('low_then'))}, "
                     f"limit {i.get('limit') or '?'}")
    link = re.search(r"https?://\S+", str(t.get("evidence", "")))
    e = {
        "title": clip(("📈 " if up else "📉 ") + str(t.get("title")), 250),
        "color": 0x5ED57D if up else 0xEA6550,
        "description": clip(f"**{t.get('action') or 'Watch'}**: {t.get('action_detail') or ''}", 900),
        "fields": [
            {"name": "Items (prices now)", "value": clip("\n".join(items), 1000)},
            {"name": "Why", "value": clip(t.get("mechanism"), 600)},
            {"name": "Evidence", "value": clip(t.get("evidence"), 400)},
            {"name": "Sell when", "value": clip(t.get("exit_plan"), 400)},
            {"name": "What would kill it", "value": clip(t.get("what_would_kill_it"), 400)},
        ],
        "footer": {"text": clip(f"{t.get('confidence', '?')} confidence · {t.get('release') or 'date unknown'}", 200)},
    }
    if link:
        e["url"] = link.group(0).rstrip(").,")
    return e


def hour_data(ts):
    try:
        return get(f"{API}/1h", {"timestamp": ts}).json().get("data", {})
    except Exception:
        return {}


def volume(d):
    return (d.get("highPriceVolume") or 0) + (d.get("lowPriceVolume") or 0)


# ---------- market data ----------
def market():
    mapping = get(f"{API}/mapping").json()
    by_id = {m["id"]: m for m in mapping}
    by_name = {m["name"].lower(): m for m in mapping}
    latest = get(f"{API}/latest").json()["data"]
    # last 2 full hours vs the same 2 hours yesterday (controls for time of day)
    h = int(time.time()) // 3600 * 3600
    now_b = [hour_data(h - 3600), hour_data(h - 7200)]
    yday_b = [hour_data(h - 3600 - 86400), hour_data(h - 7200 - 86400)]
    vol_now, vol_yday = {}, {}
    for b in now_b:
        for k, d in b.items():
            vol_now[k] = vol_now.get(k, 0) + volume(d)
    for b in yday_b:
        for k, d in b.items():
            vol_yday[k] = vol_yday.get(k, 0) + volume(d)
    return by_id, by_name, latest, vol_now, vol_yday


def find_item(name, by_name):
    m = by_name.get(name.lower().strip())
    if m:
        return m
    close = difflib.get_close_matches(name.lower().strip(), by_name.keys(), n=1, cutoff=0.85)
    return by_name[close[0]] if close else None


# ---------- watchlist alerts ----------
def run_alerts(by_id, by_name, latest, vol_now, vol_yday):
    items = load("watchlist.json", {"items": []}).get("items", [])
    state = load("alert_state.json", {})
    now, lines = time.time(), []
    for w in items:
        m = by_id.get(w.get("id")) or find_item(w.get("name", ""), by_name)
        if not m:
            continue
        lt = latest.get(str(m["id"])) or {}
        hi, lo = lt.get("high"), lt.get("low")
        checks = []
        if w.get("below") and lo and lo <= w["below"]:
            checks.append(("below", f"selling at {fmt(lo)}, under your {fmt(w['below'])} target"))
        if w.get("above") and hi and hi >= w["above"]:
            checks.append(("above", f"buying at {fmt(hi)}, over your {fmt(w['above'])} target"))
        vn, vy = vol_now.get(str(m["id"]), 0), vol_yday.get(str(m["id"]), 0)
        if w.get("spike") and vn >= 20 and vn >= w["spike"] * max(vy, 1):
            checks.append(("spike", f"volume x{vn / max(vy, 1):.1f} vs same time yesterday "
                                    f"({vn:,} traded in 2h)"))
        for kind, msg in checks:
            key = f"{m['id']}:{kind}"
            if now - state.get(key, 0) < COOLDOWN:
                continue
            state[key] = now
            lines.append(f"**{m['name']}**: {msg}. "
                         f"<https://prices.runescape.wiki/osrs/item/{m['id']}>")
    if lines:
        discord("🔔 **Watchlist alerts**\n" + "\n".join(lines))
        save("alert_state.json", state)
    print(f"Alerts sent: {len(lines)}")


# ---------- AI picks ----------
def ai_mode():
    forced = os.environ.get("AI_MODE", "auto").strip().lower()
    if forced in ("quick", "deep"):
        return forced
    if forced == "none":
        return None
    now = dt.datetime.now(dt.timezone.utc)
    picks = load("picks.json", {})
    today = now.date().isoformat()
    if now.hour < AI_HOUR_UTC:
        return None
    if now.weekday() == 6 and picks.get("last_deep") != today:
        return "deep"
    if (picks.get("generated") or "")[:10] != today:
        return "quick"
    return None


def news_text():
    parts = []
    try:
        root = ET.fromstring(get(NEWS_RSS).content)
        for i, it in enumerate(root.iter("item")):
            if i >= 12:
                break
            parts.append(f"- {it.findtext('pubDate')}: {it.findtext('title')}: "
                         f"{(it.findtext('description') or '')[:300]}")
    except Exception as e:
        parts.append(f"(news feed unavailable: {e})")
    try:
        wt = get(UPCOMING).json()["parse"]["wikitext"]
        parts.append("\nUPCOMING UPDATES PAGE (OSRS Wiki):\n" + wt[:20000])
    except Exception as e:
        parts.append(f"(upcoming updates page unavailable: {e})")
    return "\n".join(parts)


def call_claude(mode, prompt):
    model, p_in, p_out, searches = MODES[mode]
    key = os.environ["ANTHROPIC_API_KEY"]
    messages = [{"role": "user", "content": prompt}]
    usage = {"in": 0, "out": 0, "searches": 0}
    text = ""
    for _ in range(4):  # handle "pause_turn" when searches run long
        body = {"model": model, "max_tokens": 8000, "system": PLAYBOOK, "messages": messages,
                "tools": [{"type": "web_search_20250305", "name": "web_search",
                           "max_uses": searches}]}
        r = requests.post("https://api.anthropic.com/v1/messages", json=body, timeout=600,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:500]}")
        res = r.json()
        u = res.get("usage", {})
        usage["in"] += (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
            + (u.get("cache_creation_input_tokens") or 0)
        usage["out"] += u.get("output_tokens") or 0
        usage["searches"] += (u.get("server_tool_use") or {}).get("web_search_requests") or 0
        text += "".join(b.get("text", "") for b in res["content"] if b.get("type") == "text")
        if res.get("stop_reason") != "pause_turn":
            break
        messages.append({"role": "assistant", "content": res["content"]})
    # Haiku costs 5x more when one request is over 100k tokens
    if mode == "quick" and usage["in"] > 100_000:
        p_in, p_out = p_in * 5, p_out * 5
    usage["usd"] = round(usage["in"] / 1e6 * p_in + usage["out"] / 1e6 * p_out
                         + usage["searches"] * SEARCH_PRICE, 4)
    usage["model"] = model
    return text, usage


def parse_theses(text):
    """Read the AI's JSON. If the reply was cut off, rescue the ideas that are complete."""
    t = text.replace("```json", "").replace("```", "")
    dec = json.JSONDecoder()
    start = t.find("{")
    if start >= 0:
        try:
            obj, _ = dec.raw_decode(t[start:])
            if isinstance(obj, dict) and isinstance(obj.get("theses"), list):
                return obj["theses"]
        except Exception:
            pass
    out = []
    for m in re.finditer(r'\{\s*"title"', t):
        try:
            obj, _ = dec.raw_decode(t[m.start():])
            if isinstance(obj, dict) and obj.get("title"):
                out.append(obj)
        except Exception:
            continue
    return out


def run_ai(mode, by_id, by_name, latest, vol_now, vol_yday):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("No ANTHROPIC_API_KEY set, skipping AI picks")
        return
    spikes = []
    for k, vn in vol_now.items():
        vy = vol_yday.get(k, 0)
        m = by_id.get(int(k))
        if m and vy >= 50 and vn >= 3 * vy:
            spikes.append((vn / vy, m["name"]))
    spikes = "\n".join(f"{n}: volume x{r:.1f} vs yesterday"
                       for r, n in sorted(spikes, reverse=True)[:15]) or "none"
    old = load("picks.json", {})
    prev = "\n".join(f"- {t.get('title')}" for t in old.get("theses", [])) or "none"
    prompt = (f"Today is {dt.date.today()}.\n\nNEWS AND ROADMAP:\n{news_text()}\n\n"
              f"UNUSUAL TRADING RIGHT NOW (could be early buyers):\n{spikes}\n\n"
              f"YOUR PREVIOUS PICKS (update or drop them, don't just repeat):\n{prev}\n\n"
              f"Give {'4-6' if mode == 'quick' else '6-10'} theses.")
    try:
        text, usage = call_claude(mode, prompt)
    except Exception as e:
        discord(f"⚠️ AI picks failed: {e}")
        print(e)
        return
    theses = parse_theses(text)
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    for t in theses:
        found = []
        for name in t.get("items", []):
            m = find_item(name, by_name)
            if m:
                lt = latest.get(str(m["id"])) or {}
                found.append({"name": m["name"], "id": m["id"], "limit": m.get("limit"),
                              "high_then": lt.get("high"), "low_then": lt.get("low")})
            else:
                found.append({"name": name, "id": None})
        t["items"] = found
    total = len(theses)
    theses = [t for t in theses if any(i["id"] for i in t["items"])]
    dropped = total - len(theses)
    picks = {"generated": now_iso, "mode": mode, "model": usage["model"], "theses": theses,
             "cost_usd": usage["usd"], "last_deep": old.get("last_deep")}
    if mode == "deep":
        picks["last_deep"] = dt.date.today().isoformat()
    if not theses:
        picks["raw"] = text[:3000]
    save("picks.json", picks)

    hist = load("picks_history.json", {"entries": []})
    for t in theses:
        hist["entries"].append({"date": now_iso, "title": t.get("title"),
                                "direction": t.get("direction"), "items": t["items"]})
    hist["entries"] = hist["entries"][-300:]
    save("picks_history.json", hist)

    costs = load("costs.json", {"runs": []})
    costs["runs"].append({"date": now_iso, "mode": mode, **usage})
    costs["runs"] = costs["runs"][-400:]
    save("costs.json", costs)

    month = now_iso[:7]
    month_total = sum(r["usd"] for r in costs["runs"] if r["date"].startswith(month))
    header = (f"🧠 **New {'deep' if mode == 'deep' else 'daily'} picks**: {len(theses)} idea(s). "
              f"Cost ${usage['usd']:.3f}, this month ${month_total:.2f}. "
              f"<https://slippy-55.github.io/osrs-copilot/#picks>")
    if dropped:
        header += f"\n_{dropped} idea(s) dropped because their items can't be traded._"
    if not theses:
        header += "\nThe AI reply couldn't be read. Open the app's Picks tab for details."
    discord_embeds(header, [thesis_embed(t) for t in theses])


def main():
    by_id, by_name, latest, vol_now, vol_yday = market()
    run_alerts(by_id, by_name, latest, vol_now, vol_yday)
    mode = ai_mode()
    if mode:
        run_ai(mode, by_id, by_name, latest, vol_now, vol_yday)


if __name__ == "__main__":
    main()
