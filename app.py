#!/usr/bin/env python3
"""Boredom reader: `python3 app.py refresh` to fetch, `python3 app.py` to serve on :8000."""
import html, json, os, random, re, sqlite3, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "articles.db")
UA = {"User-Agent": "Mozilla/5.0 (boredom-reader)"}

# essays: RSS/Atom feeds
FEEDS = [
    ("Aeon", "https://aeon.co/feed.rss"),
    ("JSTOR Daily", "https://daily.jstor.org/feed/"),
    ("Sapiens", "https://www.sapiens.org/feed/"),
    ("Longreads", "https://longreads.com/feed/"),
    ("SwimSwam", "https://swimswam.com/feed/"),
    ("The Conversation: Brazil", "https://theconversation.com/global/topics/brazil-1071/articles.atom"),
    ("The Conversation: Spain", "https://theconversation.com/global/topics/spain-1055/articles.atom"),
]
# papers: OpenAlex searches (open access only, so the link actually opens)
PAPER_QUERIES = [
    "history of Brazilian politics", "Brazil democracy military dictatorship",
    "Spanish politics history", "Spanish Civil War Franco transition",
    "swimming performance biomechanics", "history of competitive swimming",
    "anthropology ethnography", "history social history",
]
# topic weights = your taste. Edit freely.
TOPICS = {
    "brazil": 3, "brazilian": 3, "lula": 2, "bolsonaro": 2, "vargas": 3, "spain": 3, "spanish": 2,
    "franco": 3, "catalonia": 2, "transition": 1, "dictatorship": 2, "democracy": 1,
    "swim": 4, "swimming": 4, "swimmer": 3, "olympic": 1, "freestyle": 2, "butterfly": 2,
    "anthropolog": 3, "ethnograph": 3, "indigenous": 2, "ritual": 1, "kinship": 2,
    "history": 2, "historian": 2, "empire": 1, "colonial": 1, "medieval": 1, "revolution": 1,
}
MIN_ESSAY_WORDS = 1200  # only enforced when the feed gives full text

db = sqlite3.connect(DB)
db.execute("""CREATE TABLE IF NOT EXISTS items(url TEXT PRIMARY KEY, title, source, kind, summary,
              words INT, base REAL, rating INT DEFAULT 0, seen INT DEFAULT 0)""")
try: db.execute("ALTER TABLE items ADD COLUMN read_at TEXT")  # set when you click "Read it"
except sqlite3.OperationalError: pass


def strip(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def toks(text):
    return set(re.findall(r"[a-z]{4,}", text.lower()))


def base_score(text):
    t = text.lower()
    return sum(w for k, w in TOPICS.items() if k in t)


def add(url, title, source, kind, summary, words):
    b = base_score(f"{title} {summary}")
    if not url or not title or b == 0:  # off-topic: drop
        return
    db.execute("INSERT OR IGNORE INTO items(url,title,source,kind,summary,words,base) VALUES(?,?,?,?,?,?,?)",
               (url, title, source, kind, summary[:600], words, b))


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read()


def fetch_feed(name, url):
    for e in ET.fromstring(get(url)).iter():
        if e.tag.split("}")[-1] not in ("item", "entry"):
            continue
        f = {c.tag.split("}")[-1]: c for c in e}
        l = f.get("link")
        link = (l.text or l.get("href")) if l is not None else ""
        full = f.get("encoded") if f.get("encoded") is not None else f.get("content")
        words = len(strip(full.text).split()) if full is not None and full.text else 0
        if 0 < words < MIN_ESSAY_WORDS:
            continue
        summ = next((f[k].text for k in ("description", "summary", "encoded", "content")
                     if k in f and f[k].text), "")
        add(link, strip(f["title"].text), name, "essay", strip(summ), words)


def fetch_papers(q):
    params = {"search": q, "filter": "open_access.is_oa:true,type:article,has_abstract:true",
              "per-page": 25, "select": "title,doi,abstract_inverted_index,primary_location"}
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    for w in json.loads(get("https://api.openalex.org/works?" + urllib.parse.urlencode(params)))["results"]:
        inv = w.get("abstract_inverted_index") or {}
        abstract = " ".join(k for _, k in sorted((p, k) for k, ps in inv.items() for p in ps))
        src = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or "paper"
        add(w.get("doi"), w["title"], src, "paper", abstract, len(abstract.split()))


def refresh():
    for name, url in FEEDS:
        try: fetch_feed(name, url)
        except Exception as e: print("feed failed", name, e)
    for q in PAPER_QUERIES:
        try: fetch_papers(q)
        except Exception as e: print("papers failed", q, e)  # ponytail: OpenAlex anon search flaky, set OPENALEX_API_KEY
    db.commit()
    for k, n in db.execute("SELECT kind,count(*) FROM items GROUP BY kind"):
        print(k, n)


def export():
    """Merge docs/items.json with fresh fetches and write it back (used by the GitHub Action)."""
    p = os.path.join(os.path.dirname(DB), "docs", "items.json")
    keys = ["url", "title", "source", "kind", "summary", "words", "base"]
    if os.path.exists(p):
        for it in json.load(open(p)):
            db.execute("INSERT OR IGNORE INTO items(url,title,source,kind,summary,words,base) VALUES(?,?,?,?,?,?,?)",
                       [it[k] for k in keys])
    refresh()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump([dict(zip(keys, r)) for r in db.execute(f"SELECT {','.join(keys)} FROM items")],
              open(p, "w"), ensure_ascii=False)


def pick(kind):
    w = {}  # token weights learned from ratings: +1 liked, -1 disliked
    for t_, s_, r in db.execute("SELECT title,summary,rating FROM items WHERE rating!=0"):
        for t in toks(f"{t_} {s_}"):
            w[t] = w.get(t, 0) + r
    rows = db.execute("SELECT url,title,source,kind,summary,words,base FROM items WHERE seen=0"
                      + (" AND kind=?" if kind else ""), (kind,) if kind else ()).fetchall()
    if not rows:
        return None
    rows.sort(key=lambda r: -(r[6] + 0.3 * sum(w.get(t, 0) for t in toks(f"{r[1]} {r[4]}"))))
    return random.choice(rows[:10])  # ponytail: random among top 10 = free exploration


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Something to read</title>
<style>
:root{color-scheme:light dark}
body{font:18px/1.6 Georgia,serif;max-width:40rem;margin:4rem auto;padding:0 1rem}
nav a{margin-right:1rem;font:14px sans-serif}
.meta{font:14px sans-serif;opacity:.7}
h1{line-height:1.2}
button{font:16px sans-serif;padding:.5rem 1rem;margin:.25rem .25rem .25rem 0;cursor:pointer}
</style>
<nav><a href="/">any</a><a href="/?kind=essay">essays</a><a href="/?kind=paper">papers</a><a href="/archive">archive</a></nav>
%s"""


class H(BaseHTTPRequestHandler):
    def send(self, body, code=200, loc=None):
        self.send_response(code)
        if loc: self.send_header("Location", loc)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(p.query)
        if p.path == "/go":  # log the read, then bounce to the article
            n = db.execute("UPDATE items SET read_at=datetime('now','localtime'), seen=1 WHERE url=?",
                           (q["url"][0],)).rowcount
            db.commit()
            return self.send("", 302, q["url"][0]) if n else self.send("unknown url", 404)
        if p.path == "/archive":
            rows = db.execute("SELECT url,title,source,kind,rating,read_at FROM items "
                              "WHERE read_at IS NOT NULL ORDER BY read_at DESC").fetchall()
            li = "".join(f'<li><a href="{html.escape(u, quote=True)}" target=_blank>{html.escape(t)}</a>'
                         f'<div class=meta>{k} · {html.escape(s)} · {d}{" · " + "👍👎"[r < 0] if r else ""}</div></li>'
                         for u, t, s, k, r, d in rows)
            return self.send(PAGE % f"<h1>Archive ({len(rows)})</h1><ul>{li or '<li>Nothing read yet.</li>'}</ul>")
        kind = q.get("kind", [""])[0]
        r = pick(kind)
        if not r:
            return self.send(PAGE % "<p>Nothing left. Run <code>python3 app.py refresh</code>.</p>")
        url, title, source, k, summary, words, _ = r
        e, u = html.escape, html.escape(url, quote=True)
        length = f" · ~{words // 200} min" if words and k == "essay" else ""
        form = lambda label, val: (f'<form method=post action=/rate style="display:inline">'
                                   f'<input type=hidden name=url value="{u}"><input type=hidden name=r value={val}>'
                                   f'<input type=hidden name=kind value="{e(kind)}"><button>{label}</button></form>')
        self.send(PAGE % f"""<p class=meta>{e(k)} · {e(source)}{length}</p>
<h1>{e(title)}</h1><p>{e(summary)}…</p>
<a href="/go?url={urllib.parse.quote(url, safe='')}" target=_blank><button>Read it ↗</button></a><br>
{form("👍 more like this", 1)}{form("👎 less", -1)}{form("skip", 0)}""")

    def do_POST(self):
        f = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        db.execute("UPDATE items SET rating=?, seen=1 WHERE url=?", (int(f["r"][0]), f["url"][0]))
        db.commit()
        self.send("", 303, "/?kind=" + f["kind"][0] if f["kind"][0] else "/")


if __name__ == "__main__":
    if sys.argv[1:] == ["refresh"]:
        refresh()
    elif sys.argv[1:] == ["export"]:
        export()
    else:
        print("http://localhost:8000")
        HTTPServer(("127.0.0.1", 8000), H).serve_forever()
