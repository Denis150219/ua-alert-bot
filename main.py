import os
import time
import re
import html
import sqlite3
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse

import requests
import feedparser
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
ALERTS_TOKEN = env_any("ALERTS_TOKEN")  # додаси коли отримаєш токен

NEWS_INTERVAL = int(env_any("NEWS_INTERVAL", default="300"))      # 5 хв
ALERTS_INTERVAL = int(env_any("ALERTS_INTERVAL", default="45"))  # 45 сек
MAX_NEWS_PER_CYCLE = int(env_any("MAX_NEWS_PER_CYCLE", default="8"))

DB_PATH = env_any("DB_PATH", default="state.sqlite")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not CHAT_ID_RAW:
    raise RuntimeError("CHAT_ID (or ІДЕНТИФІКАТОР_ЧАТУ) is not set")


def normalize_chat_id(raw: str) -> str:
    s = str(raw).strip()
    if s.startswith("@"):
        return s
    if s.startswith("-"):
        return s
    # якщо дали 100........ (типово для каналу без мінуса)
    if s.isdigit() and s.startswith("100"):
        return "-" + s
    # інші числа
    if s.isdigit():
        return "-100" + s
    return s

CHAT_ID = normalize_chat_id(CHAT_ID_RAW)


# ------------------ HTTP ------------------

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


# ------------------ DB (anti-duplicates + alerts state) ------------------

def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def db_init():
    con = db()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS posted(url TEXT PRIMARY KEY, ts INTEGER)")
        con.execute("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, val TEXT)")
        con.commit()
    finally:
        con.close()

def posted(url: str) -> bool:
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

def kv_get(key: str):
    con = db()
    try:
        cur = con.execute("SELECT val FROM kv WHERE key=? LIMIT 1", (key,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        con.close()

def kv_set(key: str, val: str):
    con = db()
    try:
        con.execute(
            "INSERT INTO kv(key, val) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET val=excluded.val",
            (key, val)
        )
        con.commit()
    finally:
        con.close()


# ------------------ Telegram send ------------------

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
    # якщо хост не дає хотлінк — може впасти, тоді просто message
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

def extract_ukrnet_detail_links(page_url: str) -> list[str]:
    r = S.get(page_url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        u = urljoin(page_url, href)
        if "ukr.net" in urlparse(u).netloc and "/news/details/" in u:
            links.append(normalize_url(u))
    # uniq keep order
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

    # спочатку шукаємо “читати докладніше”
    for a in soup.find_all("a", href=True):
        t = (a.get_text() or "").strip().lower()
        if "читати" in t or "доклад" in t or "подроб" in t:
            href = a["href"].strip()
            if href.startswith("http") and "ukr.net" not in urlparse(href).netloc:
                return normalize_url(href)

    # fallback: перше зовнішнє посилання
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
        if prop:
            m = soup.find("meta", attrs={"property": prop})
        else:
            m = soup.find("meta", attrs={"name": name})
        if m and m.get("content"):
            return m["content"].strip()
        return None

    title = meta(prop="og:title") or meta(name="twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = h1.get_text(" ").strip() if h1 else None
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        return None

    desc = meta(prop="og:description") or meta(name="description")
    if not desc:
        ps = []
        for p in soup.find_all("p"):
            t = re.sub(r"\s+", " ", p.get_text(" ").strip())
            if len(t) >= 60:
                ps.append(t)
            if len(ps) >= 4:
                break
        desc = " ".join(ps)

    image = meta(prop="og:image") or meta(name="twitter:image")
    video = meta(prop="og:video") or meta(prop="og:video:url") or meta(prop="og:video:secure_url")

    # youtube iframe fallback
    if not video:
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"].strip()
            if "youtube.com" in src or "youtu.be" in src:
                if src.startswith("//"):
                    src = "https:" + src
                video = src
                break

    title = re.sub(r"\s+", " ", title).strip()
    desc = re.sub(r"\s+", " ", (desc or "")).strip()

    # обрізаємо текст
    if len(desc) > 450:
        desc = desc[:450].rsplit(" ", 1)[0] + "…"

    return {
        "title": title,
        "text": desc,
        "image": normalize_url(image) if image else None,
        "video": normalize_url(video) if video else None,
        "url": normalize_url(url)
    }

def post_news_cycle():
    count = 0
    for page in UKRNET_PAGES:
        for details in extract_ukrnet_detail_links(page):
            if count >= MAX_NEWS_PER_CYCLE:
                return
            src = resolve_source_url(details)
            if not src:
                continue
            src = normalize_url(src)
            if posted(src):
                continue

            art = parse_source_article(src)
            if not art:
                continue

            msg = (
                f"📰 <b>{html.escape(art['title'])}</b>\n\n"
                f"{html.escape(art['text'])}\n\n"
                f'🔗 <a href="{art["url"]}">Читати повністю</a>'
            )
            if art["video"]:
                msg += f'\n🎥 <a href="{art["video"]}">Відео</a>'

            try:
                if art["image"]:
                    send_photo(art["image"], msg)
                else:
                    send_message(msg, disable_preview=False)
                mark_posted(src)
                count += 1
                print("Posted news:", src)
                time.sleep(2)
            except Exception as e:
                # якщо фото не пройшло — пробуємо без фото
                try:
                    send_message(msg, disable_preview=False)
                    mark_posted(src)
                    count += 1
                    print("Posted news (fallback msg):", src)
                except Exception as e2:
                    print("Send failed:", e, e2)


# ------------------ ALERTS (optional, when token exists) ------------------

OBLASTS_ORDER = [
    "АР Крим","Волинська","Вінницька","Дніпропетровська","Донецька","Житомирська","Закарпатська",
    "Запорізька","Івано-Франківська","м. Київ","Київська","Кіровоградська","Луганська","Львівська",
    "Миколаївська","Одеська","Полтавська","Рівненська","м. Севастополь","Сумська","Тернопільська",
    "Харківська","Херсонська","Хмельницька","Черкаська","Чернівецька","Чернігівська"
]

ALERTS_URL = "https://api.alerts.in.ua/v1/iot/active_air_raid_alerts_by_oblast.json"

def fetch_alerts_line() -> str | None:
    if not ALERTS_TOKEN:
        return None
    r = S.get(ALERTS_URL, params={"token": ALERTS_TOKEN}, timeout=20)
    if r.status_code == 304:
        return ""
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, str) else None

def post_alerts_cycle():
    line = fetch_alerts_line()
    if line is None:
        return
    if line == "":
        return

    prev = kv_get("alerts_line")
    if prev is None:
        kv_set("alerts_line", line)
        return

    if prev == line:
        return

    started = []
    ended = []

    for i, oblast in enumerate(OBLASTS_ORDER):
        if i >= len(line) or i >= len(prev):
            break
        old = prev[i]
        new = line[i]
        was = old in ("A", "P")
        now = new in ("A", "P")
        if (not was) and now:
            started.append(oblast)
        elif was and (not now):
            ended.append(oblast)

    if started:
        txt = "🚨 <b>ТРИВОГА</b>\n" + "\n".join([f"📍 {html.escape(x)}" for x in started])
        try:
            send_message(txt, disable_preview=True)
        except Exception as e:
            print("Alerts send (start) failed:", e)

    if ended:
        txt = "🟢 <b>ВІДБІЙ</b>\n" + "\n".join([f"📍 {html.escape(x)}" for x in ended])
        try:
            send_message(txt, disable_preview=True)
        except Exception as e:
            print("Alerts send (end) failed:", e)

    kv_set("alerts_line", line)


# ------------------ MAIN LOOP ------------------

def main():
    db_init()
    print("=== Bot started ===")
    print("CHAT_ID:", CHAT_ID)

    # тест
    send_message("✅ Бот запущено на Railway. Пішов робочий режим.", disable_preview=True)

    next_news = 0
    next_alerts = 0

    while True:
        now = time.time()

        if now >= next_news:
            try:
                post_news_cycle()
            except Exception as e:
                print("News cycle error:", e)
            next_news = now + NEWS_INTERVAL

        if now >= next_alerts:
            try:
                post_alerts_cycle()
            except Exception as e:
                print("Alerts cycle error:", e)
            next_alerts = now + ALERTS_INTERVAL

        time.sleep(3)

if __name__ == "__main__":
    main()
