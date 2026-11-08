#!/usr/bin/env python3
"""deckwatch (public edition) -- watch second-hand sale boards for the specific things you are looking for.

A small, read-only watcher. For each search in config.json it looks at Reddit's buy/sell subreddits and Reverb's
public listings API, keeps the posts that are SELLING something matching one of your queries, and tells you about
each one once. It never posts, comments, votes, or messages anyone, and it never logs in as a user.

  watch.py               check every search, report new matches
  watch.py --dry-run     check and print, remember nothing, send nothing
  watch.py --list        matches found so far, newest first
  watch.py --config F    use another config file (default: config.json next to this script)

Reddit: without credentials it reads each subreddit's public `new.rss` feed (newest 100 posts), at most one request
every 6 seconds. With REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET in the environment (a "script" app, application-only
OAuth, read-only) it runs a proper subreddit search per query instead, pausing between requests to stay far below
Reddit's rate limit. Every request sends a descriptive User-Agent (see `reddit_user_agent` in config.json).

Stdlib only; Python 3.9+.
"""
import argparse, base64, html, json, os, re, sqlite3, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "state.db")
WEB_UA = "Mozilla/5.0 (X11; Linux x86_64) deckwatch-public/0.1"

# Default boards per category; a search can name its own with "subreddits".
REDDIT_BOARDS = {"audio": ["AVexchange"], "camera": ["photomarket"], "computer": ["hardwareswap"],
                 "typewriter": ["typewriter_swap", "typewriters"]}
SELLING = re.compile(r"\[(WTS|FS|H|SELL|SELLING)\b|\b(for sale|selling|wts|fs|want(?:ing)? to sell)\b", re.I)
BUYING = re.compile(r"^\W*(WTB|want(?:ing)? to buy|looking for|iso)\b", re.I)    # some boards mix buy and sell posts
STALE_DAYS = 60            # a sale post older than this is almost always sold
RSS_GAP = 6.0              # seconds between unauthenticated Reddit requests
OAUTH_GAP = 1.0            # seconds between authenticated Reddit requests (limit is 100/min)


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=sys.stderr)

def _strip(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()

def _get(url, headers, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _price(text):
    m = re.search(r"\$\s?(\d[\d,]*)|(?<![\d.])(\d[\d,]*)\s?\$(?!\s?\d)", text)      # "$120" or "120$"
    return float((m.group(1) or m.group(2)).replace(",", "")) if m else None

def _words(q):
    return [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 1]


# ---------------------------------------------------------------- Reddit

class Reddit:
    def __init__(self, user_agent):
        self.ua = user_agent
        self.cid, self.secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        self.token, self.token_exp, self.last = None, 0.0, 0.0
        self.feeds = {}                                   # board -> posts, fetched once per run

    def _wait(self, gap):
        d = gap - (time.time() - self.last)
        if d > 0: time.sleep(d)
        self.last = time.time()

    def _auth(self):
        if not (self.cid and self.secret): return None
        if self.token and self.token_exp > time.time(): return self.token
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        d = json.loads(_get("https://www.reddit.com/api/v1/access_token", data=b"grant_type=client_credentials",
                            headers={"Authorization": f"Basic {basic}", "User-Agent": self.ua,
                                     "Content-Type": "application/x-www-form-urlencoded"}))
        self.token, self.token_exp = d["access_token"], time.time() + int(d.get("expires_in", 3600)) - 60
        return self.token

    def _search(self, board, query, tok):
        self._wait(OAUTH_GAP)
        params = urllib.parse.urlencode({"q": query, "restrict_sr": "1", "sort": "new", "limit": "50", "t": "month"})
        d = json.loads(_get(f"https://oauth.reddit.com/r/{board}/search?{params}",
                            headers={"Authorization": f"Bearer {tok}", "User-Agent": self.ua}))
        out = []
        for c in d.get("data", {}).get("children", []):
            x = c["data"]
            out.append({"id": x["id"], "title": _strip(x.get("title")), "url": "https://www.reddit.com" + x.get("permalink", ""),
                        "text": _strip(x.get("selftext")), "posted": x.get("created_utc", 0), "flair": x.get("link_flair_text") or ""})
        return out

    def _feed(self, board):
        if board in self.feeds: return self.feeds[board]
        self._wait(RSS_GAP)
        self.feeds[board] = []                            # a failed board is not retried this run
        raw = _get(f"https://www.reddit.com/r/{board}/new.rss?limit=100", headers={"User-Agent": self.ua})
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for e in ET.fromstring(raw).findall("a:entry", ns):
            g = lambda tag: (e.find(f"a:{tag}", ns).text or "") if e.find(f"a:{tag}", ns) is not None else ""
            link = e.find("a:link", ns)
            text = re.sub(r"\s*submitted by\s+/u/\S+.*$", "", _strip(g("content"))).strip()
            try: posted = time.mktime(time.strptime(g("updated")[:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError: posted = 0
            out.append({"id": g("id").split("/")[-1].removeprefix("t3_"), "title": _strip(g("title")),
                        "url": link.get("href") if link is not None else "", "text": text, "posted": posted, "flair": ""})
        self.feeds[board] = out
        return out

    def find(self, search):
        boards = search.get("subreddits") or REDDIT_BOARDS.get(search.get("category", ""), [])
        try: tok = self._auth()
        except Exception as e:
            log(f"reddit: OAuth failed ({str(e)[:80]}), using public feeds"); tok = None
        out, seen = [], set()
        for board in boards:
            implied = {w for w in _words(search.get("category", "")) if w in board.lower()}   # "typewriter" on r/typewriter_swap
            try:
                if tok:
                    pairs = [(q, p) for q in search["queries"] for p in self._search(board, q, tok)]
                else:                                     # the feed has every new post: keep titles with all of a query's words
                    pairs = []
                    for p in self._feed(board):
                        t = p["title"].lower()
                        q = next((q for q in search["queries"]
                                  if all(w in t for w in _words(q) if w.rstrip("s") not in implied)), None)
                        if q: pairs.append((q, p))
            except Exception as e:
                log(f"reddit: r/{board} failed: {str(e)[:100]}"); continue
            for q, p in pairs:
                if p["id"] in seen or BUYING.search(p["title"]): continue
                if not (SELLING.search(p["title"]) or re.search(r"sell|wts|for sale", p["flair"], re.I)): continue
                if p["posted"] and time.time() - p["posted"] > STALE_DAYS * 86400: continue
                price = _price(p["title"] + " " + p["text"])
                if search.get("max_price") and price and price > search["max_price"] * 1.1: continue
                seen.add(p["id"])
                out.append({"source": "reddit", "id": f"rd-{p['id']}", "title": p["title"][:160], "price": price,
                            "where": f"r/{board}", "url": p["url"], "query": q})
        return out


# ---------------------------------------------------------------- Reverb (public API, no key)

def find_reverb(search):
    out = []
    for q in search["queries"]:
        params = {"query": q, "per_page": "50", "condition": "used", "item_region": "US", "sort": "published_at|desc"}
        if search.get("max_price"): params["price_max"] = str(search["max_price"])
        try:
            d = json.loads(_get("https://reverb.com/api/listings?" + urllib.parse.urlencode(params),
                                headers={"Accept": "application/hal+json", "Accept-Version": "3.0", "User-Agent": WEB_UA}))
        except Exception as e:
            log(f"reverb: '{q}' failed: {str(e)[:100]}"); continue
        for l in d.get("listings", []):
            st = l.get("state"); st = st.get("slug") if isinstance(st, dict) else st
            if st and st != "live": continue
            if not all(w in (l.get("title") or "").lower() for w in _words(q)): continue
            url = ((l.get("_links") or {}).get("web") or {}).get("href") or f"https://reverb.com/item/{l['id']}"
            out.append({"source": "reverb", "id": f"rv-{l['id']}", "title": (l.get("title") or "")[:160],
                        "price": float((l.get("price") or {}).get("amount") or 0) or None, "where": "Reverb", "url": url, "query": q})
        time.sleep(1)
    return out


# ---------------------------------------------------------------- matching, memory, telling you

def keep(search, hit):
    t = hit["title"].lower()
    if any(re.search(rf"\b{re.escape(x.lower())}\b", t) for x in search.get("exclude", [])): return False
    if search.get("require") and not any(re.search(rf"\b{re.escape(x.lower())}\b", t) for x in search["require"]): return False
    return True

def db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, search TEXT, source TEXT, title TEXT, price REAL,
                   url TEXT, first_seen TEXT)""")
    return con

def notify(cfg, search, hit):
    price = f"${hit['price']:,.0f}" if hit["price"] else "price not stated"
    line = f"[{search['name']}] {hit['title']} -- {price}, {hit['where']}\n{hit['url']}"
    print(line + "\n")
    hook = os.environ.get("DECKWATCH_WEBHOOK") or cfg.get("webhook_url")
    if hook:                                              # a Discord-style webhook: {"content": "..."}
        try:
            _get(hook, data=json.dumps({"content": line[:1900]}).encode(),
                 headers={"Content-Type": "application/json", "User-Agent": WEB_UA})
        except Exception as e:
            log(f"webhook failed: {str(e)[:100]}")

def run(cfg, dry=False):
    reddit = Reddit(cfg.get("reddit_user_agent") or "python:deckwatch-public:0.1 (personal sale-board watcher)")
    con = None if dry else db()
    found = new = 0
    for s in cfg["searches"]:
        if s.get("paused"): continue
        hits = []
        if "reddit" in s.get("sources", ["reddit"]): hits += reddit.find(s)
        if "reverb" in s.get("sources", []): hits += find_reverb(s)
        hits = [h for h in hits if keep(s, h)]
        found += len(hits)
        for h in hits:
            if con and con.execute("SELECT 1 FROM seen WHERE id=?", (h["id"],)).fetchone(): continue
            new += 1
            notify(cfg if not dry else {}, s, h)
            if con:
                con.execute("INSERT INTO seen VALUES (?,?,?,?,?,?,?)", (h["id"], s["name"], h["source"], h["title"], h["price"],
                                                                       h["url"], time.strftime("%Y-%m-%dT%H:%M:%S")))
                con.commit()
    log(f"{len(cfg['searches'])} searches, {found} matching listings, {new} new")

def list_seen():
    for r in db().execute("SELECT first_seen, search, price, title, url FROM seen ORDER BY first_seen DESC LIMIT 100"):
        print(f"{r[0][:10]}  {r[1][:20]:<20} {('$%.0f' % r[2]) if r[2] else '':>6}  {r[3][:70]}\n            {r[4]}")

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    o = ap.parse_args()
    if o.list: return list_seen()
    if not os.path.exists(o.config):
        sys.exit(f"no {o.config}: copy config.example.json to config.json and edit the searches")
    run(json.load(open(o.config)), o.dry_run)

if __name__ == "__main__":
    main()
