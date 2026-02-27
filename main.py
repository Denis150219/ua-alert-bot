import os
import time
import requests


def get_env_any(*keys: str, default: str | None = None) -> str | None:
    """
    Повертає першу знайдену змінну оточення з переданих ключів.
    Railway інколи показує/називає змінні по-іншому, тому беремо з кількох варіантів.
    """
    for k in keys:
        v = os.getenv(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return default


def normalize_chat_id(raw: str) -> str:
    """
    Для каналів/супергруп chat_id зазвичай виглядає як -100XXXXXXXXXX.
    Якщо користувач дав 100XXXXXXXXXX (без -100 і без мінуса) — виправимо.
    """
    s = str(raw).strip()

    # вже правильний формат
    if s.startswith("-100"):
        return s

    # якщо просто від’ємний id (наприклад -12345) — залишаємо як є
    if s.startswith("-"):
        return s

    # якщо дали 1002594728892 -> робимо -1002594728892
    # (це типова ситуація з каналами)
    if s.isdigit() and len(s) >= 10:
        return "-100" + s[-10:] if len(s) == 10 else "-100" + s

    return s


def tg_send_message(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, data=payload, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram API error: {r.status_code} {r.text}")


def main():
    bot_token = get_env_any("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
    chat_id_raw = get_env_any(
        "CHAT_ID",
        "CHATID",
        "ID_CHAT",
        "CHAT",
        "CHANNEL_ID",
        "ІДЕНТИФІКАТОР_ЧАТУ",
        "ИДЕНТИФИКАТОР_ЧАТА",
    )

    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not chat_id_raw:
        raise RuntimeError("CHAT_ID (or equivalent) is not set")

    chat_id = normalize_chat_id(chat_id_raw)

    print("=== ua-alert-bot started ===")
    print(f"CHAT_ID(raw)={chat_id_raw} -> CHAT_ID(norm)={chat_id}")

    # тестове повідомлення при старті (можеш потім прибрати)
    try:
        tg_send_message(bot_token, chat_id, "✅ Бот запущено на Railway. Тестове повідомлення.")
        print("Startup test message: OK")
    except Exception as e:
        print(f"Startup test message: FAIL -> {e}")

    # нескінченний цикл, щоб контейнер не зупинявся
    while True:
        try:
            # тут потім вставимо твою основну логіку (RSS/alerts/репост тощо)
            print("Heartbeat: bot is alive")
        except Exception as e:
            # якщо щось впаде — не вбиваємо процес, просто лог + пауза
            print(f"Loop error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    main()
