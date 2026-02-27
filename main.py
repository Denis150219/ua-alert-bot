import requests
import feedparser
import hashlib
import time
import os
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("ІДЕНТИФІКАТОР_ЧАТУ")

sent_posts = set()


def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    })


def send_photo(photo_url, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    requests.post(url, data={
        "chat_id": CHAT_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML"
    })


def process_news():
    feed = feedparser.parse("https://www.ukr.net/rss/")
    for entry in feed.entries:
        unique_id = hashlib.md5(entry.link.encode()).hexdigest()
        if unique_id in sent_posts:
            continue

        title = entry.title
        link = entry.link

        response = requests.get(link, timeout=10)
        soup = BeautifulSoup(response.text, "html.parser")

        paragraphs = soup.find_all("p")
        text = "\n".join([p.get_text() for p in paragraphs[:5]])

        message = f"<b>{title}</b>\n\n{text}\n\n🔗 {link}"

        img = soup.find("img")
        if img and img.get("src"):
            send_photo(img.get("src"), message)
        else:
            send_message(message)

        sent_posts.add(unique_id)
        time.sleep(2)


def process_alerts():
    response = requests.get("https://alerts.in.ua/")
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, "html.parser")
        alerts = soup.find_all("div", class_="alert")

        for alert in alerts:
            text = alert.get_text(strip=True)
            unique_id = hashlib.md5(text.encode()).hexdigest()
            if unique_id in sent_posts:
                continue

            message = f"🚨 <b>ПОВІТРЯНА ТРИВОГА</b>\n\n{text}"
            send_message(message)
            sent_posts.add(unique_id)
            time.sleep(1)


if __name__ == "__main__":
    while True:
        try:
            process_news()
            process_alerts()
        except Exception as e:
            print("Error:", e)

        time.sleep(120)
