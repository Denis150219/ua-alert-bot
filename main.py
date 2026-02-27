import os
import time
import re
import html
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlparse

import requests
import feedparser
from bs4 import BeautifulSoup


# -------- ENV --------
def env_any(*keys, default=None):
    for k in keys:
        v = os.getenv(k)
        if v and str(v).strip():
            return str(v).strip()
    return default

BOT_TOKEN = env_any("BOT_TOKEN")
CHAT_ID = env_any("CHAT_ID", "ІДЕНТИФІКАТОР_ЧАТУ", "ИДЕНТИФИКАТОР_ЧАТА")
NEWS_INTERVAL = int(env_any("NEWS_INTERVAL", default="300"))
MAX_NEWS_PER_CYCLE = int(env_any("MAX_NEWS_PER_CYCLE", default="6"))

# SQLite краще тримати в /tmp (гарантовано writable)
DB_PATH = env_any("DB_PATH", default="/tmp/state.sqlite")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID / ІДЕНТИФІКАТОР_ЧАТУ is not set")


# -------- Keepalive HTTP (для Railway healthcheck) --------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        return

def start_http():
    port = int(os.getenv("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[http] listening on :{port}", flush=True)
    srv.serve_forever()


# -------- HTTP session --------
S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ua-alert-bot/1.0)",
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.7",
})


# -------- DB (anti-duplicates) --------
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


# -------- Telegram --------
def tg(method: str, data: dict):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = S.post(url, data=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram API error: {r.status_code} {r.text}")
    return r.json()

def send_message(text: str, disable_preview=True):
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


# -------- News via ukr.net RSS (стабільніше, ніж парсити HTML головної) --------
RSS_URLS = [
    "https://www.ukr.net/rss/",
    "https://www.ukr.net/news/rss/",
]

def resolve_ukrnet_to_source(url: str) -> str:
    # якщо це ukr.net/news/details/... — витягнемо перше зовнішнє посилання
    try:
        if "ukr.net" in urlparse(url).netloc:
            r = S.get(url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith("http") and "ukr.net" not in urlparse(href).netloc:
                    return href
    except Exception:
        pass
    return url

def parse_article(source_url: str):
    r = S.get(source_url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop=None, name=None):
        m = soup.find("meta", attrs={"property": prop}) if prop else soup.find("meta", attrs={"name": name})
        return m["content"].strip() if m and m.get("content") else None

    title = meta(prop="og:title") or meta(name="twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ").strip() if h1 else None
    if not title:
        return None

    desc = meta(prop="og:description") or meta(name="description") or ""
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > 450:
        desc = desc[:450].rsplit(" ", 1)[0] + "…"

    img = meta(prop="og:image") or meta(name="twitter:image")
    return title.strip(), desc, img, source_url

def news_cycle():
    feed = None
    for u in RSS_URLS:
        f = feedparser.parse(u)
        if getattr(f, "entries", None):
            feed = f
            break

    if not feed or not feed.entries:
        print("[news] rss empty", flush=True)
        return

    sent = 0
    for e in feed.entries:
        if sent >= MAX_NEWS_PER_CYCLE:
            break
        link = getattr(e, "link", None)
        if not link:
            continue

        src = resolve_ukrnet_to_source(link)
        if is_posted(src):
            continue

        try:
            parsed = parse_article(src)
            if not parsed:
                continue
            title, desc, img, real_url = parsed

            msg = (
                f"📰 <b>{html.escape(title)}</b>\n\n"
                f"{html.escape(desc)}\n\n"
                f'🔗 <a href="{html.escape(real_url)}">Читати повністю</a>'
            )

            try:
                if img:
                    send_photo(img, msg)
                else:
                    send_message(msg, disable_preview=False)
            except Exception:
                # якщо фото “не дає” — шлемо без фото
                send_message(msg, disable_preview=False)

            mark_posted(src)
            sent += 1
            print("[news] posted:", src, flush=True)
            time.sleep(2)

        except Exception as ex:
            print("[news] error:", ex, flush=True)


def main():
    db_init()

    # старт http для healthcheck
    threading.Thread(target=start_http, daemon=True).start()

    # тест — але не валимо процес, якщо не пройшло
    try:
        send_message("✅ Бот запущено. Починаю новини.", disable_preview=True)
        print("[tg] startup message OK", flush=True)
    except Exception as e:
        print("[tg] startup message FAIL:", e, flush=True)

    next_news = 0
    while True:
        try:
            now = time.time()
            if now >= next_news:
                news_cycle()
                next_news = now + NEWS_INTERVAL

            print("heartbeat: alive", flush=True)
            time.sleep(30)
        except Exception as e:
            # головне — не виходити
            print("[main] loop error:", e, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
