import os
import time
import re
import html
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup


# ------------------ ENV ------------------

def env_any(*keys, default=None):
    for k in keys:
        v = os.getenv(k)
        if v and str(v).strip():
            return str(v).strip()
    return default

BOT_TOKEN = env_any("BOT_TOKEN")
CHAT_ID_RAW = env_any("CHAT_ID", "ІДЕНТИФІКАТОР_ЧАТУ", "ИДЕНТИФИКАТОР_ЧАТА")
NEWS_INTERVAL = int(env_any("NEWS_INTERVAL", default="300"))      # 5 хв
MAX_NEWS_PER_CYCLE = int(env_any("MAX_NEWS_PER_CYCLE", default="8"))
DB_PATH = env_any("DB_PATH", default="state.sqlite")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAT_ID_RAW:
    raise RuntimeError("CHAT_ID / ІДЕНТИФІКАТОР_ЧАТУ is not set")

def normalize_chat_id(raw: str) -> str:
    s = str(raw).strip()
    if s.startswith("@"):
        return s
    if s.startswith("-"):
        return s
    if s.isdigit() and s.startswith("100"):
        return "-" + s
    if s.isdigit():
        return "-100" + s
    return s

CHAT_ID = normalize_chat_id(CHAT_ID_RAW)


# ------------------ HTTP keepalive (PORT) ------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return  # без шуму

def start_http_server():
    port = int(os.getenv("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[http] listening on :{port}")
    srv.serve_forever()


# ------------------ HTTP session ------------------

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ua-alert-bot/1.0)",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
})

TRACKING = {"utm_source","utm_medium","utm_campaign","utm_term","utm_content","fbclid","gclid","yclid"}

def normalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
        return urlunparse(p._replace(query=urlencode(q, doseq=True), fragment=""))
    except Exception:
        return url


# ------------------ DB (anti-duplicates) ------------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def db_init():
    con = db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS posted(url TEXT PRIMARY KEY, ts INTEGER)")
        con.commit()
    finally:
        con.close()

def is_posted(url: str) -> bool:
    con = db()
    try:
        cur = con.execute("SELECT 1 FROM posted WHERE url=? LIMIT 1", (url,))
        return cur.fetchone() is not None
    finally:
        con.close()

def mark_posted(url: str):
    con = db()
    try:
        con.execute("INSERT OR IGNORE INTO posted(url, ts) VALUES(?, ?)", (url, int(time.time())))
        con.commit()
    finally:
        con.close()


# ------------------ Telegram ------------------

def tg(method: str, data: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = S.post(url, data=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram API error: {r.status_code} {r.text}")
    return r.json()

def send_message(text: str, disable_preview=False):
    return tg("sendMessage", {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    })

def send_photo(photo_url: str, caption: str):
    return tg("sendPhoto", {
        "chat_id": CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    })


# ------------------ NEWS (ukr.net) ------------------

UKRNET_PAGES = [
    "https://www.ukr.net/news/main.html",
    "https://www.ukr.net/news/world.html",
    "https://www.ukr.net/news/politics.html",
    "https://www.ukr.net/news/economics.html",
    "https://www.ukr.net/news/events.html",
    "https://www.ukr.net/news/society.html",
    "https://www.ukr.net/news/technologies.html",
    "https://www.ukr.net/news/russianaggression.html",
]

def extract_detail_links(page_url: str) -> list[str]:
    r = S.get(page_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        u = urljoin(page_url, a["href"].strip())
        if "ukr.net" in urlparse(u).netloc and "/news/details/" in u:
            links.append(normalize_url(u))

    out, seen = [], set()
    for u in links:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

def resolve_source_url(details_url: str) -> str | None:
    r = S.get(details_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # пріоритет — “читати докладніше”
    for a in soup.find_all("a", href=True):
        t = (a.get_text() or "").strip().lower()
        if "читати" in t or "доклад" in t or "подроб" in t:
            href = a["href"].strip()
            if href.startswith("http") and "ukr.net" not in urlparse(href).netloc:
                return normalize_url(href)

    # fallback — перший зовнішній
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("http") and "ukr.net" not in urlparse(href).netloc:
            return normalize_url(href)

    return None

def parse_source_article(url: str):
    r = S.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop=None, name=None):
        m = soup.find("meta", attrs={"property": prop}) if prop else soup.find("meta", attrs={"name": name})
        return m["content"].strip() if m and m.get("content") else None

    title = meta(prop="og:title") or meta(name="twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ").strip() if h1 else None
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        return None

    desc = meta(prop="og:description") or meta(name="description") or ""
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > 450:
        desc = desc[:450].rsplit(" ", 1)[0] + "…"

    image = meta(prop="og:image") or meta(name="twitter:image")

    return {
        "title": re.sub(r"\s+", " ", title).strip(),
        "text": desc,
        "image": normalize_url(image) if image else None,
        "url": normalize_url(url)
    }

def post_news_cycle():
    posted_count = 0

    for page in UKRNET_PAGES:
        for details in extract_detail_links(page):
            if posted_count >= MAX_NEWS_PER_CYCLE:
                return

            src = resolve_source_url(details)
            if not src:
                continue

            if is_posted(src):
                continue

            art = parse_source_article(src)
            if not art:
                continue

            msg = (
                f"📰 <b>{html.escape(art['title'])}</b>\n\n"
                f"{html.escape(art['text'])}\n\n"
                f'🔗 <a href="{art["url"]}">Читати повністю</a>'
            )

            try:
                if art["image"]:
                    send_photo(art["image"], msg)
                else:
                    send_message(msg, disable_preview=False)

                mark_posted(src)
                posted_count += 1
                print("[news] posted:", src)
                time.sleep(2)

            except Exception as e:
                # fallback без фото
                try:
                    send_message(msg, disable_preview=False)
                    mark_posted(src)
                    posted_count += 1
                    print("[news] posted fallback:", src)
                except Exception as e2:
                    print("[news] send failed:", e, e2)


# ------------------ MAIN ------------------

def main():
    db_init()

    # стартуємо HTTP сервер (щоб Railway не перезапускав)
    threading.Thread(target=start_http_server, daemon=True).start()

    print("=== bot started ===")
    print("CHAT_ID(raw)=", CHAT_ID_RAW, "->", CHAT_ID)

    send_message("✅ Бот запущено. Починаю тягнути новини.", disable_preview=True)

    next_news = 0
    while True:
        now = time.time()
        if now >= next_news:
            try:
                post_news_cycle()
            except Exception as e:
                print("[news] cycle error:", e)
            next_news = now + NEWS_INTERVAL

        time.sleep(3)

if __name__ == "__main__":
    main()
